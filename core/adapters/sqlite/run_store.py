from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, cast

from ...runs.models import RunCheckpoint, RunRecord, RunStatus
from ...runs.store import (
    RunStoreConflictError,
    RunStoreCorruptionError,
    StoredRun,
    StoredRunEvent,
)


class SQLiteRunStore:
    """Local durable run store using transactional, version-checked SQLite writes."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def create_run(
        self,
        record: RunRecord,
        checkpoint: RunCheckpoint | None = None,
    ) -> None:
        await asyncio.to_thread(self._create_run_sync, record, checkpoint)

    async def load_run(self, run_id: str) -> StoredRun | None:
        return await asyncio.to_thread(self._load_run_sync, run_id)

    async def delete_run(self, run_id: str) -> bool:
        return await asyncio.to_thread(self._delete_run_sync, run_id)

    async def list_runs(
        self,
        workspace_dir: str | None = None,
        limit: int = 50,
    ) -> list[RunRecord]:
        if limit < 1:
            return []
        return await asyncio.to_thread(
            self._list_runs_sync,
            workspace_dir,
            limit,
        )

    async def save_run(
        self,
        record: RunRecord,
        checkpoint: RunCheckpoint,
        *,
        expected_version: int,
    ) -> None:
        if checkpoint.run_id != record.run_id:
            raise ValueError("checkpoint run_id does not match record run_id")
        if record.version != expected_version + 1:
            raise ValueError("saved record version must increment expected_version by one")
        await asyncio.to_thread(
            self._save_run_sync,
            record,
            checkpoint,
            expected_version,
        )

    async def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: float,
    ) -> int:
        return await asyncio.to_thread(
            self._append_event_sync,
            run_id,
            event_type,
            payload,
            created_at,
        )

    async def list_events(self, run_id: str) -> list[StoredRunEvent]:
        return await asyncio.to_thread(self._list_events_sync, run_id)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(8):
            try:
                self._initialize_once()
                return
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or attempt == 7:
                    raise
                time.sleep(0.025 * (attempt + 1))

    def _initialize_once(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    workspace_dir TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    error TEXT NOT NULL,
                    checkpoint_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_runs_updated_at
                    ON runs(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_runs_workspace_updated
                    ON runs(workspace_dir, updated_at DESC);

                CREATE TABLE IF NOT EXISTS run_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_run_events_run_sequence
                    ON run_events(run_id, sequence);
                """
            )

    def _create_run_sync(
        self,
        record: RunRecord,
        checkpoint: RunCheckpoint | None,
    ) -> None:
        if checkpoint is not None and checkpoint.run_id != record.run_id:
            raise ValueError("checkpoint run_id does not match record run_id")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, workspace_dir, model, status, version,
                    created_at, updated_at, error, checkpoint_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*self._record_values(record), self._encode_checkpoint(checkpoint)),
            )

    def _load_run_sync(self, run_id: str) -> StoredRun | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return StoredRun(
                record=self._record_from_row(row),
                checkpoint=self._decode_checkpoint(row["checkpoint_json"], run_id),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RunStoreCorruptionError(
                f"stored run {run_id} is corrupt: {error}"
            ) from error

    def _delete_run_sync(self, run_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM runs WHERE run_id = ?",
                (run_id,),
            )
        return cursor.rowcount == 1

    def _list_runs_sync(
        self,
        workspace_dir: str | None,
        limit: int,
    ) -> list[RunRecord]:
        with self._connect() as connection:
            if workspace_dir is None:
                rows = connection.execute(
                    "SELECT * FROM runs ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM runs
                    WHERE workspace_dir = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (workspace_dir, limit),
                ).fetchall()
        try:
            return [self._record_from_row(row) for row in rows]
        except (KeyError, TypeError, ValueError) as error:
            raise RunStoreCorruptionError(f"stored run list is corrupt: {error}") from error

    def _save_run_sync(
        self,
        record: RunRecord,
        checkpoint: RunCheckpoint,
        expected_version: int,
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE runs SET
                    workspace_dir = ?, model = ?, status = ?, version = ?,
                    created_at = ?, updated_at = ?, error = ?, checkpoint_json = ?
                WHERE run_id = ? AND version = ?
                """,
                (
                    record.workspace_dir,
                    record.model,
                    record.status.value,
                    record.version,
                    record.created_at,
                    record.updated_at,
                    record.error,
                    self._encode_checkpoint(checkpoint),
                    record.run_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise RunStoreConflictError(
                    f"stale durable run version for {record.run_id}: "
                    f"expected {expected_version}"
                )

    def _append_event_sync(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: float,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO run_events (run_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, event_type, json.dumps(payload, ensure_ascii=False), created_at),
            )
            sequence = cursor.lastrowid
        if sequence is None:
            raise RuntimeError("SQLite did not return an event sequence")
        return sequence

    def _list_events_sync(self, run_id: str) -> list[StoredRunEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence, run_id, event_type, payload_json, created_at
                FROM run_events WHERE run_id = ? ORDER BY sequence
                """,
                (run_id,),
            ).fetchall()
        try:
            return [
                StoredRunEvent(
                    sequence=int(row["sequence"]),
                    run_id=str(row["run_id"]),
                    event_type=str(row["event_type"]),
                    payload=cast(dict[str, Any], json.loads(row["payload_json"])),
                    created_at=float(row["created_at"]),
                )
                for row in rows
            ]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RunStoreCorruptionError(
                f"stored events for {run_id} are corrupt: {error}"
            ) from error

    @staticmethod
    def _record_values(record: RunRecord) -> tuple[Any, ...]:
        return (
            record.run_id,
            record.workspace_dir,
            record.model,
            record.status.value,
            record.version,
            record.created_at,
            record.updated_at,
            record.error,
        )

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=str(row["run_id"]),
            workspace_dir=str(row["workspace_dir"]),
            model=str(row["model"]),
            status=RunStatus(str(row["status"])),
            version=int(row["version"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            error=str(row["error"]),
        )

    @staticmethod
    def _encode_checkpoint(checkpoint: RunCheckpoint | None) -> str | None:
        if checkpoint is None:
            return None
        return json.dumps(
            {
                "run_id": checkpoint.run_id,
                "messages": checkpoint.messages,
                "task_snapshot": checkpoint.task_snapshot,
                "prompt_tokens": checkpoint.prompt_tokens,
                "completion_tokens": checkpoint.completion_tokens,
                "usage_estimated": checkpoint.usage_estimated,
                "completed_tool_calls": checkpoint.completed_tool_calls,
                "execution_policy": checkpoint.execution_policy,
                "budget_snapshot": checkpoint.budget_snapshot,
                "saved_at": checkpoint.saved_at,
                "schema_version": checkpoint.schema_version,
                "aggregate_snapshot": checkpoint.aggregate_snapshot,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _decode_checkpoint(raw: str | None, run_id: str) -> RunCheckpoint | None:
        if raw is None:
            return None
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("checkpoint payload must be an object")
        stored_run_id = str(payload["run_id"])
        if stored_run_id != run_id:
            raise ValueError("checkpoint run_id does not match its run record")
        raw_messages = payload["messages"]
        raw_snapshot = payload["task_snapshot"]
        raw_completed = payload.get("completed_tool_calls", {})
        raw_policy = payload.get("execution_policy")
        raw_budget = payload.get("budget_snapshot")
        schema_version = int(payload.get("schema_version", 1))
        raw_aggregate = payload.get("aggregate_snapshot")
        if not isinstance(raw_messages, list):
            raise ValueError("checkpoint messages must be a list")
        if not all(isinstance(message, dict) for message in raw_messages):
            raise ValueError("checkpoint messages must contain objects")
        if not isinstance(raw_snapshot, dict):
            raise ValueError("checkpoint task snapshot must be an object")
        if not isinstance(raw_completed, dict):
            raise ValueError("completed tool calls must be an object")
        if raw_policy is not None and not isinstance(raw_policy, dict):
            raise ValueError("execution policy must be an object")
        if raw_budget is not None and not isinstance(raw_budget, dict):
            raise ValueError("budget snapshot must be an object")
        if raw_aggregate is not None and not isinstance(raw_aggregate, dict):
            raise ValueError("aggregate snapshot must be an object")
        if schema_version == 1:
            raw_aggregate = {
                "schema_version": 1,
                "run_id": stored_run_id,
                "conversation": raw_messages,
                "task_graph": raw_snapshot,
                "execution_policy": raw_policy,
                "budget_snapshot": raw_budget,
                "completed_tool_calls": raw_completed,
                "approval_references": [],
                "artifact_references": [],
                "verification_summary": {},
                "usage_totals": {
                    "prompt_tokens": int(payload.get("prompt_tokens", 0)),
                    "completion_tokens": int(payload.get("completion_tokens", 0)),
                    "estimated": bool(payload.get("usage_estimated", False)),
                },
                "security_metric_references": {},
            }
            schema_version = 2
        if schema_version != 2:
            raise ValueError("unsupported RunCheckpoint schema")
        return RunCheckpoint(
            run_id=stored_run_id,
            messages=tuple(cast(list[dict[str, Any]], raw_messages)),
            task_snapshot=cast(dict[str, Any], raw_snapshot),
            prompt_tokens=int(payload.get("prompt_tokens", 0)),
            completion_tokens=int(payload.get("completion_tokens", 0)),
            usage_estimated=bool(payload.get("usage_estimated", False)),
            completed_tool_calls={
                str(key): str(value) for key, value in raw_completed.items()
            },
            execution_policy=cast(dict[str, Any] | None, raw_policy),
            budget_snapshot=cast(dict[str, Any] | None, raw_budget),
            saved_at=float(payload["saved_at"]),
            schema_version=schema_version,
            aggregate_snapshot=cast(dict[str, Any] | None, raw_aggregate),
        )


__all__ = ["SQLiteRunStore"]
