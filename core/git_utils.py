"""Git worktree lifecycle manager for Actor isolation.

Each Actor gets a dedicated worktree on a throwaway branch.
File changes are collected as unified diffs for Planner merge.
"""

from __future__ import annotations

import os
import random
import subprocess
import time


WORKTREES_DIR = ".worktrees"


def _run_git(*args: str, cwd: str | None = None, timeout: int = 30) -> tuple[int, str, str]:
    """Run a git command. Returns (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def setup_worktree(base_dir: str, task_id: str) -> str:
    """Create an isolated git worktree for a single Actor.

    Branch: actor-{task_id}-{timestamp}-{4hex_random}
    Path:   {base_dir}/.worktrees/{branch_name}

    Returns the absolute path to the new worktree directory.

    Raises RuntimeError if git worktree add fails.
    """
    ts = int(time.time())
    suffix = f"{random.randint(0, 0xFFFF):04x}"
    branch = f"actor-{task_id}-{ts}-{suffix}"
    worktrees_root = os.path.join(base_dir, WORKTREES_DIR)
    worktree_path = os.path.join(worktrees_root, branch)

    os.makedirs(worktrees_root, exist_ok=True)

    # Retry up to 3 times if branch name collision (extremely unlikely)
    last_error = ""
    for attempt in range(3):
        if attempt > 0:
            suffix = f"{random.randint(0, 0xFFFF):04x}"
            branch = f"actor-{task_id}-{ts}-{suffix}"
            worktree_path = os.path.join(worktrees_root, branch)

        rc, stdout, stderr = _run_git(
            "worktree", "add", "-b", branch, worktree_path,
            cwd=base_dir, timeout=60,
        )
        if rc == 0:
            return worktree_path
        last_error = stderr
        # If it's not a branch-exists error, fail immediately
        if "already exists" not in stderr and "already exists" not in stdout:
            raise RuntimeError(f"git worktree add failed: {stderr}")

    raise RuntimeError(f"git worktree add failed after 3 attempts: {last_error}")


def teardown_worktree(worktree_path: str) -> None:
    """Remove a worktree and its associated branch.

    Best-effort: logs warnings but never raises.
    """
    if not os.path.isdir(worktree_path):
        return

    base_dir = _get_main_workspace(worktree_path)

    # 1. Remove the worktree (--force skips safety checks)
    rc, stdout, stderr = _run_git(
        "worktree", "remove", "--force", worktree_path,
        cwd=base_dir, timeout=30,
    )
    if rc != 0:
        # If the worktree is already gone, that's fine
        if "not a working tree" not in stderr:
            import logging
            logging.warning(f"teardown_worktree: git worktree remove failed: {stderr}")

    # 2. Clean up stale worktree metadata
    _run_git("worktree", "prune", cwd=base_dir, timeout=10)

    # 3. Delete the branch (attempt; branch may have been auto-deleted)
    branch_name = os.path.basename(worktree_path)
    _run_git("branch", "-D", branch_name, cwd=base_dir, timeout=10)


def extract_diff(worktree_path: str) -> str:
    """Extract all uncommitted changes from a worktree as a unified diff.

    Includes:
    - Modified tracked files (git diff HEAD)
    - Untracked files (new file diff against /dev/null)

    Returns a unified diff string suitable for `git apply`.
    """
    parts: list[str] = []

    # 1. Diff for modified tracked files
    rc, stdout, stderr = _run_git("diff", "HEAD", "--binary", cwd=worktree_path, timeout=30)
    if rc == 0 and stdout:
        parts.append(stdout)

    # 2. Capture untracked files
    untracked = _list_untracked(worktree_path)
    for filepath in untracked:
        diff = _diff_untracked_file(worktree_path, filepath)
        if diff:
            parts.append(diff)

    return "\n".join(parts)


def _list_untracked(worktree_path: str) -> list[str]:
    """List untracked files in the worktree (excluding .git directory entries)."""
    rc, stdout, stderr = _run_git(
        "ls-files", "--others", "--exclude-standard",
        cwd=worktree_path, timeout=10,
    )
    if rc != 0:
        return []
    return [f for f in stdout.split("\n") if f]


def _diff_untracked_file(worktree_path: str, filepath: str) -> str:
    """Generate a diff for a single untracked (new) file by diffing against /dev/null."""
    full_path = os.path.join(worktree_path, filepath)
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        # Binary file — skip diff, just note it
        return f"# Binary/Unreadable new file: {filepath}\n"

    lines = content.splitlines(keepends=True)
    diff_header = (
        f"diff --git a/{filepath} b/{filepath}\n"
        f"new file mode 100644\n"
        f"--- /dev/null\n"
        f"+++ b/{filepath}\n"
    )
    n = len(lines)
    hunks = []
    for line in lines:
        if line.endswith("\n"):
            hunks.append(f"+{line}")
        else:
            hunks.append(f"+{line}\n")
    return diff_header + f"@@ -0,0 +1,{n} @@\n" + "".join(hunks)


def cleanup_orphans(base_dir: str) -> list[str]:
    """Remove worktree directories not tracked by git worktree list.

    Call on startup or before each delegate batch to recover from crashes.
    Returns list of removed directory paths.
    """
    worktrees_root = os.path.join(base_dir, WORKTREES_DIR)
    if not os.path.isdir(worktrees_root):
        return []

    # Get currently tracked worktree paths
    rc, stdout, stderr = _run_git("worktree", "list", "--porcelain", cwd=base_dir, timeout=10)
    tracked_paths: set[str] = set()
    if rc == 0:
        for line in stdout.split("\n"):
            if line.startswith("worktree "):
                tracked_paths.add(os.path.abspath(line[len("worktree "):]))

    # Scan .worktrees/ for orphaned directories
    removed: list[str] = []
    for entry in os.listdir(worktrees_root):
        entry_path = os.path.abspath(os.path.join(worktrees_root, entry))
        if os.path.isdir(entry_path) and entry_path not in tracked_paths:
            _force_remove_dir(entry_path)
            removed.append(entry_path)

    # Also prune stale git worktree metadata
    _run_git("worktree", "prune", cwd=base_dir, timeout=10)

    return removed


def is_clean(workspace_dir: str) -> bool:
    """Check if the git workspace has no uncommitted changes."""
    rc, stdout, stderr = _run_git("status", "--porcelain", cwd=workspace_dir, timeout=10)
    return rc == 0 and stdout == ""


def _get_main_workspace(worktree_path: str) -> str:
    """Given a worktree path, find the main workspace (the repo root).

    The worktree's .git file points back to the main repo.
    """
    git_file = os.path.join(worktree_path, ".git")
    if os.path.isfile(git_file):
        with open(git_file, "r") as f:
            content = f.read().strip()
        # Format: "gitdir: <path-to-main-repo>/.git/worktrees/<name>"
        if content.startswith("gitdir:"):
            gitdir = content[len("gitdir:"):].strip()
            # Walk up: .git/worktrees/<name> → .git → repo root
            worktrees_parent = os.path.dirname(os.path.dirname(gitdir))
            return os.path.dirname(worktrees_parent)
    # Fallback: assume standard structure
    return os.path.dirname(os.path.dirname(worktree_path))


def _force_remove_dir(path: str) -> None:
    """Recursively remove a directory, handling permission issues on Windows."""
    import shutil
    import stat

    def _on_error(func, p, exc_info):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onerror=_on_error)
