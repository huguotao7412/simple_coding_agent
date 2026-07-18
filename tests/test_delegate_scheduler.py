from __future__ import annotations

from typing import Any

import pytest

from core.actors.contracts import ActorExecutionResult, ActorTaskSpec
from core.runs.context import RunContext
from core.tools.delegate import DelegateTool
from core.verification.models import GateResult, VerificationReport


class RecordingExecutor:
    def __init__(self, outcomes: dict[str, str]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []
        self.specs: list[ActorTaskSpec] = []

    async def execute(
        self,
        spec: ActorTaskSpec,
        run_context: RunContext,
    ) -> ActorExecutionResult:
        self.calls.append(spec.task_id)
        self.specs.append(spec)
        outcome = self.outcomes.get(spec.task_id, "done")
        if outcome == "raise":
            raise RuntimeError("executor crashed")
        if outcome == "failed":
            return ActorExecutionResult(
                task_id=spec.task_id,
                status="failed",
                error="actor failed",
                key_findings="actor failed",
            )
        return ActorExecutionResult(
            task_id=spec.task_id,
            status="done",
            files_modified=(f"{spec.task_id}.py",),
            key_findings=f"completed {spec.task_id}",
            diff=f"diff for {spec.task_id}",
        )


def _tool(context: RunContext, executor: RecordingExecutor) -> DelegateTool:
    return DelegateTool(
        workspace_dir=".",
        state=context.state,
        run_context=context,
        actor_executor=executor,
    )


def _subtask(task_id: str) -> dict[str, Any]:
    return {"task_id": task_id, "description": f"Execute {task_id}"}


def test_result_payload_preserves_verification_attempt_evidence() -> None:
    report = VerificationReport(
        attempt=1,
        results=(
            GateResult(
                gate_name="unit",
                command=("pytest",),
                required=True,
                passed=False,
                exit_code=1,
                duration_ms=12,
                output_artifact="unit.log",
                output_excerpt="failed",
            ),
        ),
    )

    payload = DelegateTool._result_payload(
        ActorExecutionResult(
            task_id="task",
            status="failed",
            verification_reports=(report,),
        )
    )

    verification = payload["verification"]
    assert verification["attempts"] == 1
    assert verification["passed"] is False
    assert verification["reports"][0]["gates"][0]["output_artifact"] == "unit.log"


@pytest.mark.asyncio
async def test_delegate_runs_dependencies_in_order_and_records_results() -> None:
    context = RunContext.create(run_id="run_scheduler")
    first = await context.state.add_task("first")
    second = await context.state.add_task("second", dependencies=[first])
    executor = RecordingExecutor({})

    result = await _tool(context, executor).execute(
        subtasks=[_subtask(first), _subtask(second)]
    )

    assert result.success
    assert executor.calls == [first, second]
    assert context.state.task_tree[first].status == "done"
    assert context.state.task_tree[second].status == "done"
    assert context.state.task_tree[first].result_summary == f"completed {first}"
    assert context.state.task_tree[first].diff == f"diff for {first}"
    assert context.state.task_tree[first].handoff_message is not None
    assert executor.specs[1].dependency_handoffs == (
        context.state.task_tree[first].handoff_message,
    )
    assert executor.specs[1].dependency_handoffs[0].handoff.findings == (
        f"completed {first}",
    )
    assert "2 done, 0 failed, 0 blocked" in result.content
    assert "<a2a_lite_messages>" in result.content
    assert '"schema_version": "a2a-lite/1.0"' in result.content

    first_event = await context.events.get()
    second_event = await context.events.get()
    assert first_event.type == "a2a_lite_message"
    assert second_event.type == "a2a_lite_message"


@pytest.mark.asyncio
async def test_delegate_blocks_dependents_after_executor_failure() -> None:
    context = RunContext.create(run_id="run_blocked")
    first = await context.state.add_task("first")
    second = await context.state.add_task("second", dependencies=[first])
    executor = RecordingExecutor({first: "failed"})

    result = await _tool(context, executor).execute(
        subtasks=[_subtask(first), _subtask(second)]
    )

    assert result.success is False
    assert result.error == "All delegated subtasks failed or were blocked."
    assert executor.calls == [first]
    assert context.state.task_tree[first].status == "failed"
    assert context.state.task_tree[second].status == "blocked"
    assert "0 done, 1 failed, 1 blocked" in result.content


@pytest.mark.asyncio
async def test_delegate_preserves_sibling_result_when_executor_crashes() -> None:
    context = RunContext.create(run_id="run_siblings")
    crashing = await context.state.add_task("crashing")
    healthy = await context.state.add_task("healthy")
    executor = RecordingExecutor({crashing: "raise"})

    result = await _tool(context, executor).execute(
        subtasks=[_subtask(crashing), _subtask(healthy)]
    )

    assert result.success
    assert context.state.task_tree[crashing].status == "failed"
    assert context.state.task_tree[healthy].status == "done"
    assert context.state.task_tree[healthy].result_summary == f"completed {healthy}"
    assert "1 done, 1 failed, 0 blocked" in result.content
