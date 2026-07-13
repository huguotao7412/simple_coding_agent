from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from core.actors.contracts import (
    ActorExecutionResult,
    ActorExecutor,
    ActorTaskSpec,
)
from core.runs.context import RunContext


class FakeExecutor:
    async def execute(
        self,
        spec: ActorTaskSpec,
        run_context: RunContext,
    ) -> ActorExecutionResult:
        return ActorExecutionResult(task_id=spec.task_id, status="done")


def test_actor_task_spec_is_immutable_and_normalizes_collections() -> None:
    spec = ActorTaskSpec(
        task_id="task_1",
        description="Change the module",
        context_files=("module.py",),
        context_summaries=("Scout found module.py",),
        role="verifier",
        max_steps=12,
        dependencies=("task_0",),
    )

    assert spec.context_files == ("module.py",)
    assert spec.context_summaries == ("Scout found module.py",)
    assert spec.dependencies == ("task_0",)
    with pytest.raises(FrozenInstanceError):
        spec.description = "mutated"  # type: ignore[misc]


def test_actor_execution_result_has_stable_defaults() -> None:
    result = ActorExecutionResult(task_id="task_1", status="failed", error="boom")

    assert result.files_modified == ()
    assert result.bugs_found == ()
    assert result.key_findings == ""
    assert result.suggested_next_steps == ""
    assert result.diff_artifact == ""
    assert result.diff == ""


def test_actor_executor_protocol_is_runtime_checkable() -> None:
    assert isinstance(FakeExecutor(), ActorExecutor)
