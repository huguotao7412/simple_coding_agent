from __future__ import annotations

import pytest

from core.a2a_lite.models import AgentHandoff, AgentMessage
from core.runtime.conversation import ContextManager
from core.events import AgentEvent
from core.planner import Planner
from core.runs.context import RunContext
from core.runs.models import RunCheckpoint, RunRecord, RunStatus
from core.runs.task_state import GlobalState


class MinimalLLM:
    max_tokens = 128000

    def count_messages_tokens(self, messages):
        return 1

    async def chat(self, messages, tools=None, on_token=None):
        return {"role": "assistant", "content": "done"}


@pytest.mark.asyncio
async def test_run_contexts_do_not_share_task_state():
    left = RunContext.create()
    right = RunContext.create()

    task_id = await left.state.add_task("left task")

    assert task_id in left.state.task_tree
    assert right.state.task_tree == {}
    assert left.run_id != right.run_id


@pytest.mark.asyncio
async def test_event_bus_adds_run_metadata():
    context = RunContext.create(run_id="run_test")

    await context.emit(AgentEvent(type="done", content="ok"))

    event = await context.events.get()
    assert event.run_id == "run_test"


def test_planners_do_not_share_task_state():
    left = Planner(
        llm_client=MinimalLLM(),
        context_manager=ContextManager(system_prompt="system"),
        tools=[],
        workspace_dir=".",
    )
    right = Planner(
        llm_client=MinimalLLM(),
        context_manager=ContextManager(system_prompt="system"),
        tools=[],
        workspace_dir=".",
    )

    assert left.state is not right.state
    assert left.run_context.run_id != right.run_context.run_id


def test_context_manager_restores_messages_and_tool_result_deduplication():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "stable output"},
    ]
    context = ContextManager(system_prompt="discarded")

    context.restore_messages(messages)
    context.add_tool_result("call_2", "stable output")

    assert context.messages[:3] == messages
    assert context.messages[3]["content"] == "[Same result as previous call, omitted]"
    messages[0]["content"] = "mutated outside"
    assert context.messages[0]["content"] == "system"


@pytest.mark.asyncio
async def test_global_state_round_trips_full_task_snapshot():
    state = GlobalState()
    task_id = await state.add_task("implement persistence", dependencies=["task_parent"])
    full_diff = "diff --git a/a.py b/a.py\n" + ("+line\n" * 200)
    await state.add_summary(
        task_id,
        "implemented",
        diff=full_diff,
        files_modified=["a.py"],
        diff_artifact=".sca/artifacts/a.patch",
        handoff_message=AgentMessage.handoff_message(
            run_id="run_snapshot",
            task_id=task_id,
            sender_id=f"actor:{task_id}",
            recipient_id="planner",
            handoff=AgentHandoff(findings=("implemented",)),
        ),
    )
    await state.update_task(task_id, status="done", assigned_actor="coder_1")

    snapshot = await state.snapshot(truncate_diffs=False)
    restored = GlobalState.from_snapshot(snapshot)

    node = restored.task_tree[task_id]
    assert node.description == "implement persistence"
    assert node.dependencies == ["task_parent"]
    assert node.status == "done"
    assert node.assigned_actor == "coder_1"
    assert node.diff == full_diff
    assert node.files_modified == ["a.py"]
    assert node.diff_artifact == ".sca/artifacts/a.patch"
    assert node.handoff_message is not None
    assert node.handoff_message.schema_version == "a2a-lite/1.0"
    assert node.handoff_message.handoff.findings == ("implemented",)


@pytest.mark.asyncio
async def test_run_context_builds_and_restores_checkpoint():
    record = RunRecord(
        run_id="run_restore",
        workspace_dir="C:/workspace",
        model="test-model",
        status=RunStatus.PAUSED,
        version=4,
        created_at=100.0,
        updated_at=200.0,
    )
    context = RunContext.create(run_id=record.run_id, record=record)
    task_id = await context.state.add_task("resume me")
    await context.state.update_task(task_id, status="running")
    await context.record_usage(12, 7, True)
    context.completed_tool_calls["call_done"] = "tool observation"
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "do work"},
    ]

    checkpoint = await context.checkpoint(messages, saved_at=250.0)
    restored = RunContext.from_checkpoint(record, checkpoint)

    assert checkpoint.messages == tuple(messages)
    assert checkpoint.saved_at == 250.0
    assert restored.run_id == record.run_id
    assert restored.record == record
    assert restored.usage.prompt_tokens == 12
    assert restored.usage.completion_tokens == 7
    assert restored.usage.estimated is True
    assert restored.completed_tool_calls == {"call_done": "tool observation"}
    assert restored.state.task_tree[task_id].status == "running"


def test_run_context_refuses_checkpoint_for_another_run():
    record = RunRecord(
        run_id="run_left",
        workspace_dir="C:/workspace",
        model="test-model",
        created_at=100.0,
        updated_at=100.0,
    )
    checkpoint = RunCheckpoint(
        run_id="run_right",
        messages=(),
        task_snapshot={},
        saved_at=101.0,
    )

    with pytest.raises(ValueError, match="run_id"):
        RunContext.from_checkpoint(record, checkpoint)
