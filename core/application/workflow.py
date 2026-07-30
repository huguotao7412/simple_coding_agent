from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar


class WorkflowStage(StrEnum):
    INGRESS_SECURITY = "ingress_security"
    TASK_ASSESSMENT = "task_assessment"
    EXECUTION_POLICY = "execution_policy"
    INPUT_APPROVAL = "input_approval"
    PLANNING = "planning"
    PLAN_VALIDATION = "plan_validation"
    ACTOR_SCHEDULING = "actor_scheduling"
    ACTOR_EXECUTION = "actor_execution"
    VERIFICATION = "verification"
    BOUNDED_REPAIR = "bounded_repair"
    FINALIZATION = "finalization"
    FINAL_OUTPUT_SECURITY = "final_output_security"
    PERSISTENCE_REPORT = "persistence_report"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_STAGES = frozenset({
    WorkflowStage.COMPLETED,
    WorkflowStage.FAILED,
})


_ALLOWED: dict[WorkflowStage, frozenset[WorkflowStage]] = {
    WorkflowStage.INGRESS_SECURITY: frozenset({
        WorkflowStage.TASK_ASSESSMENT,
        WorkflowStage.FAILED,
    }),
    WorkflowStage.TASK_ASSESSMENT: frozenset({
        WorkflowStage.EXECUTION_POLICY,
        WorkflowStage.FAILED,
    }),
    WorkflowStage.EXECUTION_POLICY: frozenset({
        WorkflowStage.INPUT_APPROVAL,
        WorkflowStage.PLANNING,
        WorkflowStage.FINALIZATION,
        WorkflowStage.FAILED,
    }),
    WorkflowStage.INPUT_APPROVAL: frozenset({
        WorkflowStage.INPUT_APPROVAL,
        WorkflowStage.PLANNING,
        WorkflowStage.PAUSED,
        WorkflowStage.FINALIZATION,
        WorkflowStage.FAILED,
    }),
    WorkflowStage.PLANNING: frozenset({
        WorkflowStage.PLAN_VALIDATION,
        WorkflowStage.FAILED,
    }),
    WorkflowStage.PLAN_VALIDATION: frozenset({
        WorkflowStage.ACTOR_SCHEDULING,
        WorkflowStage.FAILED,
    }),
    WorkflowStage.ACTOR_SCHEDULING: frozenset({
        WorkflowStage.ACTOR_SCHEDULING,
        WorkflowStage.ACTOR_EXECUTION,
        WorkflowStage.VERIFICATION,
        WorkflowStage.FAILED,
    }),
    WorkflowStage.ACTOR_EXECUTION: frozenset({
        WorkflowStage.ACTOR_EXECUTION,
        WorkflowStage.ACTOR_SCHEDULING,
        WorkflowStage.VERIFICATION,
        WorkflowStage.FAILED,
    }),
    WorkflowStage.VERIFICATION: frozenset({
        WorkflowStage.VERIFICATION,
        WorkflowStage.BOUNDED_REPAIR,
        WorkflowStage.FINALIZATION,
        WorkflowStage.FAILED,
    }),
    WorkflowStage.BOUNDED_REPAIR: frozenset({
        WorkflowStage.VERIFICATION,
        WorkflowStage.FAILED,
    }),
    WorkflowStage.FINALIZATION: frozenset({
        WorkflowStage.FINAL_OUTPUT_SECURITY,
        WorkflowStage.PERSISTENCE_REPORT,
        WorkflowStage.COMPLETED,
        WorkflowStage.FAILED,
    }),
    WorkflowStage.FINAL_OUTPUT_SECURITY: frozenset({
        WorkflowStage.PERSISTENCE_REPORT,
        WorkflowStage.FAILED,
    }),
    WorkflowStage.PERSISTENCE_REPORT: frozenset({
        WorkflowStage.COMPLETED,
        WorkflowStage.FAILED,
    }),
    WorkflowStage.PAUSED: frozenset({
        WorkflowStage.INPUT_APPROVAL,
        WorkflowStage.ACTOR_SCHEDULING,
        WorkflowStage.ACTOR_EXECUTION,
        WorkflowStage.VERIFICATION,
        WorkflowStage.FAILED,
    }),
    WorkflowStage.COMPLETED: frozenset(),
    WorkflowStage.FAILED: frozenset(),
}


@dataclass(frozen=True)
class WorkflowCursor:
    schema_version: int
    run_id: str
    stage: WorkflowStage
    interrupted: bool = False


T = TypeVar("T")


class AgentWorkflow:
    """The only component allowed to transition application workflow stages."""

    schema_version = 1

    def start(self, run_id: str) -> WorkflowCursor:
        if not run_id:
            raise ValueError("run_id must not be empty")
        return WorkflowCursor(
            schema_version=self.schema_version,
            run_id=run_id,
            stage=WorkflowStage.INGRESS_SECURITY,
        )

    def transition(
        self,
        cursor: WorkflowCursor,
        target: WorkflowStage,
    ) -> WorkflowCursor:
        if cursor.stage in TERMINAL_STAGES:
            raise RuntimeError("terminal workflow cannot transition or resume")
        if target is not cursor.stage and target not in _ALLOWED[cursor.stage]:
            raise RuntimeError(
                f"illegal workflow transition: {cursor.stage} -> {target}"
            )
        return WorkflowCursor(
            schema_version=self.schema_version,
            run_id=cursor.run_id,
            stage=target,
            interrupted=target is WorkflowStage.PAUSED,
        )

    def resume(
        self,
        cursor: WorkflowCursor,
        target: WorkflowStage,
    ) -> WorkflowCursor:
        if cursor.stage in TERMINAL_STAGES:
            raise RuntimeError("terminal workflow cannot be resumed")
        if cursor.stage is not WorkflowStage.PAUSED:
            raise RuntimeError("workflow is not paused")
        return self.transition(cursor, target)

    async def run_stage(
        self,
        cursor: WorkflowCursor,
        target: WorkflowStage,
        use_case: Callable[[], Awaitable[T]],
    ) -> tuple[WorkflowCursor, T]:
        next_cursor = self.transition(cursor, target)
        return next_cursor, await use_case()


__all__ = [
    "AgentWorkflow",
    "TERMINAL_STAGES",
    "WorkflowCursor",
    "WorkflowStage",
]
