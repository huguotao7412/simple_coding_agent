"""Git worktree lifecycle manager for Actor isolation.

Each Actor gets a dedicated worktree on a throwaway branch. File changes are
collected as unified diffs for Planner merge.
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any


WORKTREES_DIR = ".worktrees"
DIFF_GIT_PATH_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_SHADOW_IGNORES = (
    ".git",
    ".sca",
    WORKTREES_DIR,
    ".venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
)


@dataclass
class _ShadowSession:
    workspace_dir: str
    root_dir: str
    repo_dir: str
    worktrees_dir: str
    baseline_hashes: dict[str, str] = field(default_factory=dict)


_shadow_sessions: dict[str, _ShadowSession] = {}
_shadow_sessions_lock = threading.RLock()


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


def is_git_repository(workspace_dir: str) -> bool:
    """Return whether ``workspace_dir`` is the root of a usable Git work tree."""
    rc, stdout, _ = _run_git(
        "rev-parse",
        "--show-toplevel",
        cwd=workspace_dir,
        timeout=10,
    )
    if rc != 0 or not stdout:
        return False
    return os.path.normcase(os.path.realpath(stdout)) == os.path.normcase(
        os.path.realpath(workspace_dir)
    )


def uses_shadow_repository(workspace_dir: str) -> bool:
    """Return whether Actor isolation should snapshot instead of using Git HEAD."""
    return not is_git_repository(workspace_dir) or not is_clean(workspace_dir)


def _hash_file(path: str) -> str:
    if os.path.islink(path):
        return "symlink:" + os.readlink(path)
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_hashes(workspace_dir: str) -> dict[str, str]:
    hashes: dict[str, str] = {}
    ignored = set(_SHADOW_IGNORES)
    for root, directories, files in os.walk(workspace_dir):
        directories[:] = [name for name in directories if name not in ignored]
        for filename in files:
            path = os.path.join(root, filename)
            relative = os.path.relpath(path, workspace_dir).replace(os.sep, "/")
            try:
                hashes[relative] = _hash_file(path)
            except OSError:
                continue
    return hashes


def _shadow_copy_ignored(path: str, names: list[str]) -> set[str]:
    ignored = {name for name in names if name in _SHADOW_IGNORES}
    if os.path.basename(path) == ".sca":
        ignored.update(name for name in names if name != "quality-gates.toml")
    else:
        ignored.discard(".sca")
    return ignored


def _tracked_files(workspace_dir: str) -> list[str]:
    if not is_git_repository(workspace_dir):
        return []
    rc, stdout, _ = _run_git("ls-files", "-z", cwd=workspace_dir, timeout=30)
    if rc != 0:
        return []
    return [path for path in stdout.split("\0") if path]


def _create_shadow_session(workspace_dir: str) -> _ShadowSession:
    workspace_dir = os.path.realpath(workspace_dir)
    root_dir = tempfile.mkdtemp(prefix="sca-shadow-")
    repo_dir = os.path.join(root_dir, "repo")
    worktrees_dir = os.path.join(root_dir, "worktrees")
    try:
        shutil.copytree(
            workspace_dir,
            repo_dir,
            symlinks=True,
            ignore=_shadow_copy_ignored,
        )
        os.makedirs(worktrees_dir, exist_ok=True)
        setup_commands = (
            ("init", "-q"),
            ("config", "user.email", "simple-coding-agent@local"),
            ("config", "user.name", "Simple Coding Agent"),
        )
        for command in setup_commands:
            rc, stdout, stderr = _run_git(*command, cwd=repo_dir, timeout=60)
            if rc != 0:
                detail = stderr or stdout
                raise RuntimeError(
                    f"failed to create shadow repository ({' '.join(command)}): {detail}"
                )
        tracked = [
            path
            for path in _tracked_files(workspace_dir)
            if os.path.lexists(os.path.join(repo_dir, path.replace("/", os.sep)))
        ]
        for offset in range(0, len(tracked), 100):
            tracked_command = (
                "add",
                "-f",
                "--",
                *tracked[offset:offset + 100],
            )
            rc, stdout, stderr = _run_git(
                *tracked_command,
                cwd=repo_dir,
                timeout=60,
            )
            if rc != 0:
                detail = stderr or stdout
                raise RuntimeError(f"failed to add tracked shadow files: {detail}")
        for final_command in (
            ("add", "-A"),
            ("commit", "-q", "--allow-empty", "-m", "Actor workspace baseline"),
        ):
            rc, stdout, stderr = _run_git(
                *final_command,
                cwd=repo_dir,
                timeout=60,
            )
            if rc != 0:
                detail = stderr or stdout
                raise RuntimeError(
                    "failed to create shadow repository "
                    f"({' '.join(final_command)}): {detail}"
                )
        return _ShadowSession(
            workspace_dir=workspace_dir,
            root_dir=root_dir,
            repo_dir=repo_dir,
            worktrees_dir=worktrees_dir,
            baseline_hashes=_snapshot_hashes(workspace_dir),
        )
    except Exception:
        shutil.rmtree(root_dir, ignore_errors=True)
        raise


def _shadow_session(workspace_dir: str) -> _ShadowSession:
    key = os.path.realpath(workspace_dir)
    with _shadow_sessions_lock:
        session = _shadow_sessions.get(key)
        if session is None:
            session = _create_shadow_session(key)
            _shadow_sessions[key] = session
        return session


def setup_worktree(base_dir: str, task_id: str) -> str:
    """Create an isolated git worktree for a single Actor."""
    ts = int(time.time())
    suffix = f"{random.randint(0, 0xFFFF):04x}"
    branch = f"actor-{task_id}-{ts}-{suffix}"
    git_base_dir = base_dir
    if uses_shadow_repository(base_dir):
        shadow = _shadow_session(base_dir)
        git_base_dir = shadow.repo_dir
        worktrees_root = shadow.worktrees_dir
    else:
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
            cwd=git_base_dir,
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
    if not is_git_repository(base_dir):
        return []
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


def shadow_patch_conflicts(workspace_dir: str, diff: str) -> list[str]:
    """Return paths changed since the shadow baseline that a patch would touch."""
    key = os.path.realpath(workspace_dir)
    with _shadow_sessions_lock:
        session = _shadow_sessions.get(key)
        if session is None:
            return []
        baseline = dict(session.baseline_hashes)

    conflicts: list[str] = []
    for relative in parse_diff_file_paths(diff):
        normalized = relative.replace("/", os.sep)
        candidate = os.path.realpath(os.path.join(key, normalized))
        try:
            if os.path.commonpath([key, candidate]) != key:
                conflicts.append(relative)
                continue
        except ValueError:
            conflicts.append(relative)
            continue
        try:
            current = _hash_file(candidate) if os.path.lexists(candidate) else ""
        except OSError:
            current = "<unreadable>"
        if current != baseline.get(relative, ""):
            conflicts.append(relative)
    return sorted(set(conflicts))


def has_shadow_baseline(workspace_dir: str) -> bool:
    """Return whether this process owns a baseline for the workspace."""
    key = os.path.realpath(workspace_dir)
    with _shadow_sessions_lock:
        return key in _shadow_sessions


def refresh_shadow_baseline(workspace_dir: str, diff: str) -> None:
    """Advance shadow conflict hashes after a patch is applied successfully."""
    key = os.path.realpath(workspace_dir)
    with _shadow_sessions_lock:
        session = _shadow_sessions.get(key)
        if session is None:
            return
        for relative in parse_diff_file_paths(diff):
            candidate = os.path.join(key, relative.replace("/", os.sep))
            if os.path.lexists(candidate):
                try:
                    session.baseline_hashes[relative] = _hash_file(candidate)
                except OSError:
                    session.baseline_hashes[relative] = "<unreadable>"
            else:
                session.baseline_hashes.pop(relative, None)


def cleanup_shadow_sessions() -> None:
    """Remove all process-owned ephemeral shadow repositories."""
    with _shadow_sessions_lock:
        sessions = list(_shadow_sessions.values())
        _shadow_sessions.clear()
    for session in sessions:
        shutil.rmtree(session.root_dir, ignore_errors=True)


atexit.register(cleanup_shadow_sessions)


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

    def _on_error(func: Any, p: str, exc_info: Any) -> None:
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onerror=_on_error)
