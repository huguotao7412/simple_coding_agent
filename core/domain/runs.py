from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..execution.policy import ExecutionPolicy, RunBudgetLedger
from ..runs.task_state import GlobalState


RUN_AGGREGATE_SCHEMA_VERSION = 1


@dataclass
class UsageTotals:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class RunAggregate:
    """The sole live authority for domain state belonging to one Run."""

    run_id: str
    state: GlobalState
    lifecycle_status: str = "created"
    usage: UsageTotals = field(default_factory=UsageTotals)
    execution_policy: ExecutionPolicy | None = None
    budget_ledger: RunBudgetLedger | None = None
    completed_tool_calls: dict[str, str] = field(default_factory=dict)
    conversation: tuple[dict[str, Any], ...] = ()
    approval_references: set[str] = field(default_factory=set)
    artifact_references: list[dict[str, Any]] = field(default_factory=list)
    verification_summary: dict[str, Any] = field(default_factory=dict)
    security_metric_references: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id must not be empty")

    def snapshot(
        self,
        *,
        task_snapshot: dict[str, Any],
        budget_snapshot: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "schema_version": RUN_AGGREGATE_SCHEMA_VERSION,
            "run_id": self.run_id,
            "lifecycle_status": self.lifecycle_status,
            "conversation": self.conversation,
            "task_graph": task_snapshot,
            "execution_policy": (
                self.execution_policy.to_dict()
                if self.execution_policy is not None
                else None
            ),
            "budget_snapshot": budget_snapshot,
            "completed_tool_calls": dict(self.completed_tool_calls),
            "approval_references": sorted(self.approval_references),
            "artifact_references": list(self.artifact_references),
            "verification_summary": dict(self.verification_summary),
            "usage_totals": {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
                "estimated": self.usage.estimated,
            },
            "security_metric_references": dict(self.security_metric_references),
        }


__all__ = [
    "RUN_AGGREGATE_SCHEMA_VERSION",
    "RunAggregate",
    "UsageTotals",
]
