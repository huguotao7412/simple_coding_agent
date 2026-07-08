"""Git worktree lifecycle manager for Actor isolation.

Each Actor gets a dedicated worktree on a throwaway branch. File changes are
collected as unified diffs for Planner merge.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import subprocess
import time


WORKTREES_DIR = ".worktrees"
DIFF_GIT_PATH_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


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
    """Create an isolated git worktree for a single Actor."""
    ts = int(time.time())
    suffix = f"{random.randint(0, 0xFFFF):04x}"
    branch = f"actor-{task_id}-{ts}-{suffix}"
    worktrees_root = os.path.join(base_dir, WORKTREES_DIR)
    worktree_path = os.path.join(worktrees_root, branch)

    os.makedirs(worktrees_root, exist_ok=True)

    last_error = ""
    for attempt in range(3):
        if attempt > 0:
            suffix = f"{random.randint(0, 0xFFFF):04x}"
            branch = f"actor-{task_id}-{ts}-{suffix}"
            worktree_path = os.path.join(worktrees_root, branch)

        rc, stdout, stderr = _run_git(
            "worktree",
            "add",
            "-b",
            branch,
            worktree_path,
            cwd=base_dir,
            timeout=60,
        )
        if rc == 0:
            return worktree_path
        last_error = stderr
        if "already exists" not in stderr and "already exists" not in stdout:
            raise RuntimeError(f"git worktree add failed: {stderr}")

    raise RuntimeError(f"git worktree add failed after 3 attempts: {last_error}")


def teardown_worktree(worktree_path: str) -> None:
    """Remove a worktree and its associated branch on a best-effort basis."""
    if not os.path.isdir(worktree_path):
        return

    base_dir = _get_main_workspace(worktree_path)

    rc, _, stderr = _run_git(
        "worktree",
        "remove",
        "--force",
        worktree_path,
        cwd=base_dir,
        timeout=30,
    )
    if rc != 0 and "not a working tree" not in stderr:
        import logging

        logging.warning("teardown_worktree: git worktree remove failed: %s", stderr)

    _run_git("worktree", "prune", cwd=base_dir, timeout=10)
    _run_git("branch", "-D", os.path.basename(worktree_path), cwd=base_dir, timeout=10)


async def extract_diff(worktree_path: str) -> str:
    """Extract uncommitted worktree changes as a unified diff."""
    loop = asyncio.get_running_loop()

    def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=worktree_path,
            timeout=timeout,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()

    await loop.run_in_executor(None, _run, ["git", "reset", "HEAD"], 10)

    rc, _, stderr = await loop.run_in_executor(None, _run, ["git", "add", "-A"], 10)
    if rc != 0:
        import logging

        logging.warning("extract_diff: git add -A failed: %s", stderr)

    rc, stdout, stderr = await loop.run_in_executor(
        None,
        _run,
        ["git", "diff", "--cached", "--binary"],
        30,
    )

    await loop.run_in_executor(None, _run, ["git", "reset", "HEAD"], 10)

    if rc != 0:
        import logging

        logging.warning("extract_diff: git diff --cached failed: %s", stderr)
        return ""

    return stdout


def parse_diff_file_paths(diff: str) -> list[str]:
    """Return modified file paths mentioned by a git unified diff."""
    paths: set[str] = set()
    for line in diff.splitlines():
        match = DIFF_GIT_PATH_RE.match(line)
        if not match:
            continue
        for path in match.groups():
            if path != "/dev/null":
                paths.add(path)
    return sorted(paths)


def cleanup_orphans(base_dir: str) -> list[str]:
    """Remove worktree directories not tracked by `git worktree list`."""
    worktrees_root = os.path.join(base_dir, WORKTREES_DIR)
    if not os.path.isdir(worktrees_root):
        return []

    rc, stdout, _ = _run_git("worktree", "list", "--porcelain", cwd=base_dir, timeout=10)
    tracked_paths: set[str] = set()
    if rc == 0:
        for line in stdout.split("\n"):
            if line.startswith("worktree "):
                tracked_paths.add(os.path.abspath(line[len("worktree "):]))

    removed: list[str] = []
    for entry in os.listdir(worktrees_root):
        entry_path = os.path.abspath(os.path.join(worktrees_root, entry))
        if os.path.isdir(entry_path) and entry_path not in tracked_paths:
            _force_remove_dir(entry_path)
            removed.append(entry_path)

    _run_git("worktree", "prune", cwd=base_dir, timeout=10)
    return removed


def is_clean(workspace_dir: str) -> bool:
    """Check if the git workspace has no uncommitted changes."""
    rc, stdout, _ = _run_git("status", "--porcelain", cwd=workspace_dir, timeout=10)
    return rc == 0 and stdout == ""


def _get_main_workspace(worktree_path: str) -> str:
    """Given a worktree path, find the main workspace."""
    git_file = os.path.join(worktree_path, ".git")
    if os.path.isfile(git_file):
        with open(git_file, encoding="utf-8") as f:
            content = f.read().strip()
        if content.startswith("gitdir:"):
            gitdir = content[len("gitdir:"):].strip()
            worktrees_parent = os.path.dirname(os.path.dirname(gitdir))
            return os.path.dirname(worktrees_parent)
    return os.path.dirname(os.path.dirname(worktree_path))


def _force_remove_dir(path: str) -> None:
    """Recursively remove a directory, handling Windows permission issues."""
    import shutil
    import stat

    def _on_error(func, p, exc_info):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onerror=_on_error)
