from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

from core.runs.context import RunContext
from core.runs.models import RunRecord, RunStatus
from core.runs.store import RunStore, StoredRun
from core.runs.sqlite_store import SQLiteRunStore
from core.paths import touch_workspace_state, workspace_state_dir


class RunLookupError(ValueError):
    """Raised when a durable run cannot be inspected or resumed safely."""


def run_database_path(workspace_dir: str) -> Path:
    return workspace_state_dir(workspace_dir) / "runs.db"


async def open_run_store(workspace_dir: str) -> SQLiteRunStore:
    touch_workspace_state(workspace_dir)
    store = SQLiteRunStore(run_database_path(workspace_dir))
    await store.initialize()
    return store


async def create_durable_run_context(
    *,
    workspace_dir: str,
    model: str,
    messages: list[dict[str, Any]],
    run_id: str | None = None,
    now: float | None = None,
    store: RunStore | None = None,
) -> RunContext:
    durable_store = store or await open_run_store(workspace_dir)
    timestamp = time.time() if now is None else now
    record = RunRecord(
        run_id=run_id or f"run_{uuid.uuid4().hex[:12]}",
        workspace_dir=os.path.abspath(workspace_dir),
        model=model,
        status=RunStatus.CREATED,
        version=0,
        created_at=timestamp,
        updated_at=timestamp,
    )
    context = RunContext.create(record=record, store=durable_store)
    await durable_store.create_run(record, await context.checkpoint(messages))
    return context


async def load_resumable_run(
    store: RunStore,
    run_id: str,
    workspace_dir: str,
) -> StoredRun:
    stored = await store.load_run(run_id)
    if stored is None:
        raise RunLookupError(f"durable run {run_id} was not found")
    expected_workspace = os.path.normcase(os.path.realpath(workspace_dir))
    actual_workspace = os.path.normcase(os.path.realpath(stored.record.workspace_dir))
    if actual_workspace != expected_workspace:
        raise RunLookupError(
            f"durable run {run_id} belongs to workspace "
            f"{stored.record.workspace_dir}, not {workspace_dir}"
        )
    if stored.record.status is RunStatus.COMPLETED:
        raise RunLookupError(f"durable run {run_id} is already completed")
    if stored.checkpoint is None:
        raise RunLookupError(f"durable run {run_id} has no checkpoint to resume")
    return stored


def render_run_list(records: list[RunRecord]) -> str:
    if not records:
        return "No durable runs found."
    lines = ["RUN_ID\tSTATUS\tVERSION\tMODEL\tUPDATED_AT"]
    for record in records:
        lines.append(
            f"{record.run_id}\t{record.status.value}\t{record.version}\t"
            f"{record.model}\t{record.updated_at:.3f}"
        )
    return "\n".join(lines)


def render_run_detail(
    record: RunRecord,
    *,
    event_count: int,
    message_count: int,
) -> str:
    lines = [
        f"Run: {record.run_id}",
        f"Status: {record.status.value}",
        f"Version: {record.version}",
        f"Model: {record.model}",
        f"Workspace: {record.workspace_dir}",
        f"Created: {record.created_at:.3f}",
        f"Updated: {record.updated_at:.3f}",
        f"Events: {event_count}",
        f"Messages: {message_count}",
    ]
    if record.error:
        lines.append(f"Error: {record.error}")
    return "\n".join(lines)


__all__ = [
    "RunLookupError",
    "create_durable_run_context",
    "load_resumable_run",
    "open_run_store",
    "render_run_detail",
    "render_run_list",
    "run_database_path",
]
