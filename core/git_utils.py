"""Git worktree lifecycle manager for Actor isolation.

Each Actor gets a dedicated worktree on a throwaway branch.
File changes are collected as unified diffs for Planner merge.
"""

from __future__ import annotations

import asyncio
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
        encoding="utf-8",
        errors="replace",
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


async def extract_diff(worktree_path: str) -> str:
    """Extract all uncommitted changes from a worktree as a unified diff.

    Uses native git pipeline for 100% reliable diff generation:
      1. git reset HEAD  — clear any stale staging (Actor may have run git add)
      2. git add -A      — stage ALL changes (modified + untracked + deleted)
      3. git diff --cached --binary  — generate standard, well-formed patch
      4. git reset HEAD  — restore unstaged state for idempotency

    Returns a unified diff string suitable for `git apply`.
    """
    loop = asyncio.get_running_loop()

    def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",  
            errors="replace",  cwd=worktree_path, timeout=timeout,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()

    # Step 0: Clear any stale staging from Actor's own git operations
    await loop.run_in_executor(None, _run, ["git", "reset", "HEAD"], 10)

    # Step 1: Stage all changes (modified + untracked + deleted)
    rc, stdout, stderr = await loop.run_in_executor(
        None, _run, ["git", "add", "-A"], 10,
    )
    if rc != 0:
        import logging
        logging.warning(f"extract_diff: git add -A failed: {stderr}")

    # Step 2: Generate the standard patch
    rc, stdout, stderr = await loop.run_in_executor(
        None, _run, ["git", "diff", "--cached", "--binary"], 30,
    )

    # Step 3: Unstage to restore clean state (best-effort)
    await loop.run_in_executor(None, _run, ["git", "reset", "HEAD"], 10)

    if rc != 0:
        import logging
        logging.warning(f"extract_diff: git diff --cached failed: {stderr}")
        return ""

    return stdout


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
