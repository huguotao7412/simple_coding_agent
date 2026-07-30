from __future__ import annotations

import pytest

from core.runs.context import RunContext
from core.runs.models import RunRecord, RunStatus


@pytest.mark.asyncio
async def test_checkpoint_is_derived_from_run_aggregate_and_round_trips() -> None:
    context = RunContext.create("run-aggregate")
    context.aggregate.lifecycle_status = RunStatus.RUNNING.value
    context.completed_tool_calls["done"] = '{"observation":"redacted"}'
    context.aggregate.approval_references.add("approval:fingerprint")
    checkpoint = await context.checkpoint([{"role": "user", "content": "safe"}])

    assert checkpoint.aggregate_snapshot is not None
    assert checkpoint.aggregate_snapshot["lifecycle_status"] == "running"
    record = RunRecord(
        run_id="run-aggregate",
        workspace_dir="C:/workspace",
        model="test",
        status=RunStatus.RUNNING,
    )
    restored = RunContext.from_checkpoint(record, checkpoint)
    assert restored.aggregate.lifecycle_status == "running"
    assert restored.completed_tool_calls == context.completed_tool_calls
    assert restored.aggregate.approval_references == {"approval:fingerprint"}


def test_graph_state_contract_has_no_heavy_domain_or_provider_fields() -> None:
    from core.orchestration.state import GraphState

    forbidden = {
        "conversation",
        "diff",
        "tool_output",
        "verification_log",
        "provider",
        "session",
        "secret",
    }
    assert forbidden.isdisjoint(GraphState.__annotations__)
