from __future__ import annotations

import pytest

from core.application.finalization import FINALIZATION_ORDER, FinalizationStep
from core.application.workflow import AgentWorkflow, WorkflowStage


def test_agent_workflow_is_the_transition_authority() -> None:
    workflow = AgentWorkflow()
    cursor = workflow.start("run-1")
    cursor = workflow.transition(cursor, WorkflowStage.TASK_ASSESSMENT)
    cursor = workflow.transition(cursor, WorkflowStage.EXECUTION_POLICY)
    cursor = workflow.transition(cursor, WorkflowStage.INPUT_APPROVAL)
    cursor = workflow.transition(cursor, WorkflowStage.PAUSED)
    cursor = workflow.resume(cursor, WorkflowStage.INPUT_APPROVAL)
    assert cursor.stage is WorkflowStage.INPUT_APPROVAL


def test_terminal_workflow_cannot_resume_or_transition() -> None:
    workflow = AgentWorkflow()
    cursor = workflow.start("run-1")
    cursor = workflow.transition(cursor, WorkflowStage.FAILED)
    with pytest.raises(RuntimeError, match="terminal"):
        workflow.resume(cursor, WorkflowStage.PLANNING)
    with pytest.raises(RuntimeError, match="terminal"):
        workflow.transition(cursor, WorkflowStage.PLANNING)


def test_finalization_order_is_security_and_persistence_safe() -> None:
    assert FINALIZATION_ORDER == (
        FinalizationStep.VALIDATE_ARTIFACTS,
        FinalizationStep.PERSIST_VERIFICATION,
        FinalizationStep.COMMIT_GRAPH_CURSOR,
        FinalizationStep.TRANSITION_RUN_TERMINAL,
    )
