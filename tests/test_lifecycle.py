from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.lifecycle import garbage_collect
from core.paths import (
    WorkspaceStateMetadata,
    read_workspace_metadata,
    touch_workspace_state,
    write_workspace_metadata,
)
from core.runs.models import RunRecord, RunStatus
from core.runs.sqlite_store import SQLiteRunStore


def _record(run_id: str, workspace: Path, updated_at: float) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        workspace_dir=str(workspace),
        model="test",
        status=RunStatus.COMPLETED,
        created_at=updated_at,
        updated_at=updated_at,
    )


@pytest.mark.asyncio
async def test_gc_retains_recent_fifty_runs_and_deletes_older_runs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("SCA_STATE_HOME", str(state_root))
    state_dir = touch_workspace_state(workspace, now=1000.0)
    store = SQLiteRunStore(state_dir / "runs.db")
    await store.initialize()
    for index in range(52):
        run_id = f"run_{index:02d}"
        await store.create_run(_record(run_id, workspace, float(index)))
        report = state_dir / "reports" / f"{run_id}.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(run_id, encoding="utf-8")
        artifact = state_dir / "artifacts" / "runs" / run_id / "diff.patch"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("patch", encoding="utf-8")

    report = await garbage_collect(
        state_root=state_root,
        now=40 * 86400,
        retention_days=30,
        retain_runs=50,
        artifact_max_bytes=10_000,
    )

    assert [action.target for action in report.actions if action.action == "delete_run"] == [
        "run_01",
        "run_00",
    ]
    assert len(await store.list_runs(limit=100)) == 50
    assert not (state_dir / "reports" / "run_00.md").exists()
    assert not (state_dir / "artifacts" / "runs" / "run_00").exists()


@pytest.mark.asyncio
async def test_gc_dry_run_does_not_mark_or_delete_orphan(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_dir = state_root / "workspaces" / "missing-abc"
    metadata = WorkspaceStateMetadata(
        schema_version=1,
        workspace_path=str(tmp_path / "missing"),
        created_at=0.0,
        last_accessed_at=0.0,
    )
    write_workspace_metadata(state_dir, metadata)

    preview = await garbage_collect(
        state_root=state_root,
        dry_run=True,
        now=100.0,
    )
    assert [action.action for action in preview.actions] == ["mark_orphan"]
    assert read_workspace_metadata(state_dir) == metadata

    marked = await garbage_collect(state_root=state_root, now=100.0)
    assert [action.action for action in marked.actions] == ["mark_orphan"]
    assert read_workspace_metadata(state_dir).orphaned_at == 100.0  # type: ignore[union-attr]

    deleted = await garbage_collect(
        state_root=state_root,
        now=100.0 + 31 * 86400,
    )
    assert [action.action for action in deleted.actions] == ["delete_orphan"]
    assert not state_dir.exists()


@pytest.mark.asyncio
async def test_gc_enforces_global_artifact_capacity_oldest_first(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("SCA_STATE_HOME", str(state_root))
    state_dir = touch_workspace_state(workspace, now=100.0)
    older = state_dir / "artifacts" / "old.patch"
    newer = state_dir / "artifacts" / "new.patch"
    older.parent.mkdir(parents=True)
    older.write_bytes(b"123456")
    newer.write_bytes(b"abcdef")
    os.utime(older, (10.0, 10.0))
    os.utime(newer, (20.0, 20.0))

    preview = await garbage_collect(
        state_root=state_root,
        dry_run=True,
        artifact_max_bytes=6,
        now=100.0,
    )
    assert [action.target for action in preview.actions] == [str(older)]
    assert older.exists()

    await garbage_collect(
        state_root=state_root,
        artifact_max_bytes=6,
        now=100.0,
    )
    assert not older.exists()
    assert newer.exists()


@pytest.mark.asyncio
async def test_gc_prunes_interactive_report_history_without_database(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("SCA_STATE_HOME", str(state_root))
    state_dir = touch_workspace_state(workspace, now=100.0)
    reports = state_dir / "reports"
    reports.mkdir()
    for index in range(3):
        report = reports / f"interactive_{index}.md"
        report.write_text(str(index), encoding="utf-8")
        os.utime(report, (float(index), float(index)))

    result = await garbage_collect(
        state_root=state_root,
        now=40 * 86400,
        retention_days=30,
        retain_runs=2,
    )

    assert [
        action.target
        for action in result.actions
        if action.action == "delete_run_history"
    ] == ["interactive_0"]
    assert not (reports / "interactive_0.md").exists()
