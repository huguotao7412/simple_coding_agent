from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .run_state import RunCheckpoint, RunRecord


class RunStoreError(RuntimeError):
    """Base error for durable run persistence."""


class RunStoreConflictError(RunStoreError):
    """Raised when an optimistic version check fails."""


class RunStoreCorruptionError(RunStoreError):
    """Raised when stored data cannot be decoded safely."""


@dataclass(frozen=True)
class StoredRun:
    record: RunRecord
    checkpoint: RunCheckpoint | None = None


@dataclass(frozen=True)
class StoredRunEvent:
    sequence: int
    run_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: float


class RunStore(Protocol):
    async def initialize(self) -> None: ...

    async def create_run(
        self,
        record: RunRecord,
        checkpoint: RunCheckpoint | None = None,
    ) -> None: ...

    async def load_run(self, run_id: str) -> StoredRun | None: ...

    async def list_runs(
        self,
        workspace_dir: str | None = None,
        limit: int = 50,
    ) -> list[RunRecord]: ...

    async def save_run(
        self,
        record: RunRecord,
        checkpoint: RunCheckpoint,
        *,
        expected_version: int,
    ) -> None: ...

    async def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: float,
    ) -> int: ...

    async def list_events(self, run_id: str) -> list[StoredRunEvent]: ...


__all__ = [
    "RunStore",
    "RunStoreConflictError",
    "RunStoreCorruptionError",
    "RunStoreError",
    "StoredRun",
    "StoredRunEvent",
]
