from __future__ import annotations

import asyncio
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core.git_utils import (
    cleanup_shadow_sessions,
    extract_diff,
    has_shadow_baseline,
    setup_worktree,
    teardown_worktree,
    uses_shadow_repository,
)
from core.tools.apply_patch import ApplyPatchTool


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


@pytest.fixture(autouse=True)
def _clean_shadow_sessions() -> None:
    cleanup_shadow_sessions()
    yield
    cleanup_shadow_sessions()


def test_non_git_workspace_uses_ephemeral_shadow_worktree(tmp_path: Path) -> None:
    workspace = tmp_path / "plain"
    workspace.mkdir()
    (workspace / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (workspace / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace / "ignored.txt").write_text("secret\n", encoding="utf-8")
    (workspace / ".sca").mkdir()
    (workspace / ".sca" / "quality-gates.toml").write_text(
        "[[gates]]\nname = 'tests'\n",
        encoding="utf-8",
    )
    (workspace / ".sca" / "runtime.db").write_text("runtime", encoding="utf-8")

    worktree = Path(setup_worktree(str(workspace), "plain"))
    try:
        assert uses_shadow_repository(str(workspace))
        assert has_shadow_baseline(str(workspace))
        assert not (workspace / ".git").exists()
        assert (worktree / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        assert not (worktree / "ignored.txt").exists()
        assert (worktree / ".sca" / "quality-gates.toml").exists()
        assert not (worktree / ".sca" / "runtime.db").exists()

        (worktree / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
        diff = asyncio.run(extract_diff(str(worktree)))
        assert "-VALUE = 1" in diff
        assert "+VALUE = 2" in diff
    finally:
        teardown_worktree(str(worktree))


def test_dirty_git_workspace_snapshot_includes_uncommitted_changes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.email", "test@example.local")
    _git(workspace, "config", "user.name", "Test User")
    (workspace / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "initial")
    (workspace / "module.py").write_text("VALUE = 2\n", encoding="utf-8")

    worktree = Path(setup_worktree(str(workspace), "dirty"))
    try:
        assert uses_shadow_repository(str(workspace))
        assert (worktree / "module.py").read_text(encoding="utf-8") == "VALUE = 2\n"
        assert workspace not in worktree.parents
    finally:
        teardown_worktree(str(worktree))


def test_shadow_session_supports_concurrent_actor_worktrees(tmp_path: Path) -> None:
    workspace = tmp_path / "plain"
    workspace.mkdir()
    (workspace / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(
            pool.map(
                lambda task_id: setup_worktree(str(workspace), task_id),
                ("one", "two"),
            )
        )
    try:
        assert len(set(paths)) == 2
        assert all(Path(path, "module.py").exists() for path in paths)
    finally:
        for path in paths:
            teardown_worktree(path)


def test_non_git_patch_applies_and_advances_shadow_baseline(tmp_path: Path) -> None:
    workspace = tmp_path / "plain"
    workspace.mkdir()
    target = workspace / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    worktree = Path(setup_worktree(str(workspace), "patch"))
    try:
        (worktree / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
        diff = asyncio.run(extract_diff(str(worktree)))
        result = asyncio.run(
            ApplyPatchTool().execute(
                diff=diff,
                task_id="patch",
                workspace_dir=str(workspace),
            )
        )
        assert result.success, result.error
        assert target.read_text(encoding="utf-8") == "VALUE = 2\n"
    finally:
        teardown_worktree(str(worktree))


def test_shadow_patch_refuses_concurrent_user_change(tmp_path: Path) -> None:
    workspace = tmp_path / "plain"
    workspace.mkdir()
    target = workspace / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    worktree = Path(setup_worktree(str(workspace), "conflict"))
    try:
        (worktree / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
        diff = asyncio.run(extract_diff(str(worktree)))
        target.write_text("VALUE = 3\n", encoding="utf-8")

        result = asyncio.run(
            ApplyPatchTool().execute(
                diff=diff,
                task_id="conflict",
                workspace_dir=str(workspace),
            )
        )
        assert not result.success
        assert result.error is not None
        assert "concurrent user changes" in result.error
        assert target.read_text(encoding="utf-8") == "VALUE = 3\n"
    finally:
        teardown_worktree(str(worktree))
