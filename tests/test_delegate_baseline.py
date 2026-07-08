from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from core.git_utils import extract_diff, setup_worktree, teardown_worktree
from core.state import GlobalState
from core.tools.delegate import _apply_dependency_diffs_to_worktree


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.local")
    _git(path, "config", "user.name", "Test User")
    (path / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "initial")


def test_dependency_diffs_become_actor_baseline(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)

    (repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    coder_diff = _git(repo, "diff", "--", "module.py")
    if coder_diff and not coder_diff.endswith("\n"):
        coder_diff += "\n"
    _git(repo, "checkout", "--", "module.py")

    GlobalState.reset()
    state = GlobalState.get()
    coder_id = asyncio.run(state.add_task("Change module value"))
    verifier_id = asyncio.run(state.add_task("Verify module value", dependencies=[coder_id]))
    asyncio.run(state.add_summary(coder_id, "changed value", diff=coder_diff))
    asyncio.run(state.update_task(coder_id, status="done"))

    worktree_path = Path(setup_worktree(str(repo), verifier_id))
    try:
        applied = _apply_dependency_diffs_to_worktree(
            str(worktree_path),
            [coder_id],
            state,
        )

        assert applied == [coder_id]
        assert (worktree_path / "module.py").read_text(encoding="utf-8") == "VALUE = 2\n"
        assert _git(worktree_path, "diff") == ""

        (worktree_path / "test_module.py").write_text(
            "from module import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n",
            encoding="utf-8",
        )
        verifier_diff = asyncio.run(extract_diff(str(worktree_path)))

        assert "test_module.py" in verifier_diff
        assert "diff --git a/module.py b/module.py" not in verifier_diff
    finally:
        teardown_worktree(str(worktree_path))
