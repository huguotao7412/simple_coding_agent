from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from core.run_state import RunCheckpoint, RunRecord, RunStatus
from core.run_store import RunStoreConflictError, RunStoreCorruptionError
from core.sqlite_run_store import SQLiteRunStore


def make_record(
    run_id: str = "run_test",
    *,
    version: int = 0,
    updated_at: float = 100.0,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        workspace_dir="C:/workspace",
        model="test-model",
        status=RunStatus.CREATED,
        version=version,
        created_at=100.0,
        updated_at=updated_at,
    )


def make_checkpoint(run_id: str = "run_test", content: str = "system") -> RunCheckpoint:
    return RunCheckpoint(
        run_id=run_id,
        messages=({"role": "system", "content": content},),
        task_snapshot={"task_tree": {}, "change_count": 0},
        prompt_tokens=10,
        completion_tokens=5,
        usage_estimated=False,
        completed_tool_calls={"call_1": "observation"},
        saved_at=101.0,
    )


@pytest.mark.asyncio
async def test_sqlite_store_creates_and_loads_a_complete_run(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path / "runs.db")
    await store.initialize()

    await store.create_run(make_record(), make_checkpoint())

    stored = await store.load_run("run_test")
    assert stored is not None
    assert stored.record == make_record()
    assert stored.checkpoint == make_checkpoint()


@pytest.mark.asyncio
async def test_sqlite_store_persists_across_store_instances(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    first = SQLiteRunStore(path)
    await first.initialize()
    await first.create_run(make_record(), make_checkpoint())

    second = SQLiteRunStore(path)
    await second.initialize()

    assert await second.load_run("run_test") is not None


@pytest.mark.asyncio
async def test_sqlite_store_lists_newest_runs_and_filters_workspace(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path / "runs.db")
    await store.initialize()
    older = make_record("run_old", updated_at=100.0)
    newer = replace(
        make_record("run_new", updated_at=200.0),
        workspace_dir="C:/other",
    )
    await store.create_run(older, make_checkpoint("run_old"))
    await store.create_run(newer, make_checkpoint("run_new"))

    assert [item.run_id for item in await store.list_runs()] == ["run_new", "run_old"]
    assert [
        item.run_id
        for item in await store.list_runs(workspace_dir="C:/workspace")
    ] == ["run_old"]


@pytest.mark.asyncio
async def test_save_run_replaces_checkpoint_with_version_check(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path / "runs.db")
    await store.initialize()
    record = make_record()
    await store.create_run(record, make_checkpoint())
    updated = replace(
        record,
        status=RunStatus.RUNNING,
        version=1,
        updated_at=110.0,
    )

    await store.save_run(
        updated,
        make_checkpoint(content="restored context"),
        expected_version=0,
    )

    stored = await store.load_run(record.run_id)
    assert stored is not None
    assert stored.record == updated
    assert stored.checkpoint is not None
    assert stored.checkpoint.messages[0]["content"] == "restored context"


@pytest.mark.asyncio
async def test_save_run_rejects_a_stale_version(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path / "runs.db")
    await store.initialize()
    record = make_record()
    await store.create_run(record, make_checkpoint())
    updated = replace(record, version=1, updated_at=110.0)
    await store.save_run(updated, make_checkpoint(), expected_version=0)

    with pytest.raises(RunStoreConflictError, match="run_test"):
        await store.save_run(
            updated,
            make_checkpoint(),
            expected_version=0,
        )


@pytest.mark.asyncio
async def test_store_appends_events_in_durable_order(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path / "runs.db")
    await store.initialize()
    await store.create_run(make_record(), make_checkpoint())

    first = await store.append_event("run_test", "created", {"source": "cli"}, 100.0)
    second = await store.append_event("run_test", "checkpoint", {"step": 1}, 101.0)

    assert second > first
    events = await store.list_events("run_test")
    assert [event.sequence for event in events] == [first, second]
    assert events[1].payload == {"step": 1}


@pytest.mark.asyncio
async def test_load_run_reports_corrupt_checkpoint_json(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    store = SQLiteRunStore(path)
    await store.initialize()
    await store.create_run(make_record(), make_checkpoint())
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE runs SET checkpoint_json = ? WHERE run_id = ?",
            ("{broken", "run_test"),
        )

    with pytest.raises(RunStoreCorruptionError, match="run_test"):
        await store.load_run("run_test")


@pytest.mark.asyncio
async def test_checkpoint_payload_is_valid_json_without_pickle(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    store = SQLiteRunStore(path)
    await store.initialize()
    await store.create_run(make_record(), make_checkpoint())

    with sqlite3.connect(path) as connection:
        raw = connection.execute(
            "SELECT checkpoint_json FROM runs WHERE run_id = ?",
            ("run_test",),
        ).fetchone()[0]

    assert json.loads(raw)["completed_tool_calls"] == {"call_1": "observation"}


@pytest.mark.asyncio
async def test_concurrent_store_initialization_is_safe(tmp_path: Path) -> None:
    path = tmp_path / "runs.db"
    stores = [SQLiteRunStore(path) for _ in range(4)]

    await asyncio.gather(*(store.initialize() for store in stores))

    await stores[0].create_run(make_record(), make_checkpoint())
    assert await stores[-1].load_run("run_test") is not None
