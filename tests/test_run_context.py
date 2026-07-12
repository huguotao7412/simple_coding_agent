from __future__ import annotations

import pytest

from core.context import ContextManager
from core.events import AgentEvent
from core.planner import Planner
from core.run_context import RunContext


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
