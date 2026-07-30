from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


EVENT_SCHEMA_VERSION = 1
WORKFLOW_EVENT_TYPES = frozenset({
    "workflow_stage_started",
    "workflow_stage_finished",
    "content_guard_started",
    "content_guard_result",
    "content_guard_error",
    "security_decision",
    "approval_requested",
    "approval_granted",
    "approval_denied",
    "approval_consumed",
    "tool_execution_started",
    "tool_execution_finished",
    "actor_started",
    "actor_finished",
    "verification_started",
    "verification_finished",
    "checkpoint_saved",
    "output_redacted",
    "data_egress_allowed",
    "data_egress_denied",
    "run_paused",
    "run_resumed",
    "run_completed",
    "run_failed",
})


@dataclass(frozen=True)
class DomainEvent:
    event_type: str
    run_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: int = EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.event_type not in WORKFLOW_EVENT_TYPES:
            raise ValueError(f"unknown domain event type: {self.event_type}")


__all__ = ["DomainEvent", "EVENT_SCHEMA_VERSION", "WORKFLOW_EVENT_TYPES"]
