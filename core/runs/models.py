from __future__ import annotations

import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class RunTransitionError(ValueError):
    """Raised when a durable run attempts an illegal state transition."""


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    workspace_dir: str
    model: str
    status: RunStatus = RunStatus.CREATED
    version: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    error: str = ""

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id must not be empty")
        if self.version < 0:
            raise ValueError("version must not be negative")


@dataclass(frozen=True)
class RunCheckpoint:
    run_id: str
    messages: tuple[dict[str, Any], ...]
    task_snapshot: dict[str, Any]
    saved_at: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usage_estimated: bool = False
    completed_tool_calls: dict[str, str] | None = None
    execution_policy: dict[str, Any] | None = None
    budget_snapshot: dict[str, Any] | None = None
    schema_version: int = 2
    aggregate_snapshot: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id must not be empty")
        if self.prompt_tokens < 0 or self.completion_tokens < 0:
            raise ValueError("token counts must not be negative")
        if self.completed_tool_calls is None:
            object.__setattr__(self, "completed_tool_calls", {})
        if self.execution_policy is not None and not isinstance(
            self.execution_policy, dict
        ):
            raise ValueError("execution_policy must be an object")
        if self.budget_snapshot is not None and not isinstance(
            self.budget_snapshot, dict
        ):
            raise ValueError("budget_snapshot must be an object")
        if self.schema_version not in {1, 2}:
            raise ValueError("unsupported RunCheckpoint schema")
        if self.aggregate_snapshot is not None and not isinstance(
            self.aggregate_snapshot, dict
        ):
            raise ValueError("aggregate_snapshot must be an object")


_LEGAL_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset({RunStatus.RUNNING, RunStatus.FAILED}),
    RunStatus.RUNNING: frozenset({
        RunStatus.PAUSED,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    }),
    RunStatus.PAUSED: frozenset({RunStatus.RUNNING, RunStatus.FAILED}),
    RunStatus.FAILED: frozenset({RunStatus.RUNNING}),
    RunStatus.COMPLETED: frozenset(),
}


def transition_run(
    record: RunRecord,
    target: RunStatus,
    *,
    error: str = "",
    now: float | None = None,
) -> RunRecord:
    """Return a new record after validating and applying one transition."""
    if target not in _LEGAL_TRANSITIONS[record.status]:
        raise RunTransitionError(
            f"illegal durable run transition: {record.status} -> {target}"
        )
    next_error = error if target is RunStatus.FAILED else ""
    return replace(
        record,
        status=target,
        version=record.version + 1,
        updated_at=time.time() if now is None else now,
        error=next_error,
    )


__all__ = [
    "RunCheckpoint",
    "RunRecord",
    "RunStatus",
    "RunTransitionError",
    "transition_run",
]
