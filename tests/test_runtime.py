from __future__ import annotations

import pytest

from core.context import ContextManager
from core.events import AgentEvent
from core.agent import ActorAgent
from core.planner import Planner
from core.runtime import AgentRuntime, parse_tool_call
from core.run_context import RunContext
from core.state import GlobalState
from core.tools.base import BaseTool, ToolResult


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.max_tokens = 128000

    def count_messages_tokens(self, messages):
        return 1

    def count_tokens(self, text):
        return max(1, len(text) // 3)

    async def chat(self, messages, tools=None, on_token=None):
        if on_token:
            on_token("thinking")
        if not self.responses:
            return {"role": "assistant", "content": "done"}
        return self.responses.pop(0)


class EchoTool(BaseTool):
    name = "echo"
    description = "Echo a value."
    parameters = {"value": {"type": "string"}}
    required_params = ["value"]

    async def execute(self, **kwargs):
        return ToolResult.ok(f"echo:{kwargs.get('value')}")


class FailingTool(BaseTool):
    name = "fail"
    description = "Always fails internally."
    parameters = {}
    required_params = []

    async def execute(self, **kwargs):
        raise RuntimeError("boom")


class FakeDelegateTool(BaseTool):
    name = "delegate"
    description = "Fake delegate for planner wrapper tests."
    parameters = {}
    required_params = []

    async def execute(self, **kwargs):
        return ToolResult.ok("delegated")


class FakeNestedEventTool(BaseTool):
    name = "delegate"
    description = "Emit one nested Actor event while the parent tool is running."
    parameters = {}
    required_params = []

    def __init__(self):
        super().__init__()
        self._run_context = None

    async def execute(self, **kwargs):
        await self._run_context.emit(AgentEvent(
            type="tool_call",
            tool_name="child_read",
            actor_id="task_child",
            task_id="task_child",
        ))
        return ToolResult.ok("nested event emitted")


def _tool_call(arguments: str) -> dict:
    return {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "echo",
            "arguments": arguments,
        },
    }


def _named_tool_call(name: str, arguments: str, call_id: str = "call_1") -> dict:
    tc = _tool_call(arguments)
    tc["id"] = call_id
    tc["function"]["name"] = name
    return tc


@pytest.mark.asyncio
async def test_runtime_returns_final_answer_without_tools():
    llm = FakeLLM([{"role": "assistant", "content": "final answer"}])
    ctx = ContextManager(system_prompt="system")
    runtime = AgentRuntime(
        llm_client=llm,
        context_manager=ctx,
        tools=[],
        workspace_dir=".",
        max_steps=3,
    )

    result = await runtime.run("hello")

    assert result == "final answer"
    assert ctx.messages[-1]["role"] == "assistant"
    assert ctx.messages[-1]["content"] == "final answer"


def test_parse_tool_call_valid_json_arguments():
    parsed = parse_tool_call(_tool_call('{"value": "abc"}'))

    assert parsed.tool_name == "echo"
    assert parsed.args == {"value": "abc"}
    assert parsed.error is None


def test_parse_tool_call_empty_arguments():
    parsed = parse_tool_call(_tool_call(""))

    assert parsed.args == {}
    assert parsed.error is None


def test_parse_tool_call_fenced_json_arguments():
    parsed = parse_tool_call(_tool_call('```json\n{"value": "abc"}\n```'))

    assert parsed.args == {"value": "abc"}
    assert parsed.error is None


def test_parse_tool_call_invalid_json_arguments():
    parsed = parse_tool_call(_tool_call('{"value": '))

    assert parsed.args == {}
    assert parsed.error is not None
    assert "Invalid JSON" in parsed.error


def test_parse_tool_call_non_object_json_arguments():
    parsed = parse_tool_call(_tool_call('["abc"]'))

    assert parsed.args == {}
    assert parsed.error is None


@pytest.mark.asyncio
async def test_runtime_executes_local_tool_and_continues_to_final_answer():
    llm = FakeLLM([
        {"role": "assistant", "content": None, "tool_calls": [_tool_call('{"value": "abc"}')]},
        {"role": "assistant", "content": "finished"},
    ])
    ctx = ContextManager(system_prompt="system")
    runtime = AgentRuntime(
        llm_client=llm,
        context_manager=ctx,
        tools=[EchoTool()],
        workspace_dir=".",
        max_steps=3,
    )

    result = await runtime.run("hello")

    assert result == "finished"
    tool_messages = [m for m in ctx.messages if m["role"] == "tool"]
    assert tool_messages[-1]["content"] == "echo:abc"


@pytest.mark.asyncio
async def test_runtime_records_invalid_json_tool_call_and_continues():
    llm = FakeLLM([
        {"role": "assistant", "content": None, "tool_calls": [_tool_call('{"value": ')]},
        {"role": "assistant", "content": "recovered"},
    ])
    ctx = ContextManager(system_prompt="system")
    runtime = AgentRuntime(
        llm_client=llm,
        context_manager=ctx,
        tools=[EchoTool()],
        workspace_dir=".",
        max_steps=3,
    )

    result = await runtime.run("hello")

    assert result == "recovered"
    tool_messages = [m for m in ctx.messages if m["role"] == "tool"]
    assert "Invalid JSON" in tool_messages[-1]["content"]


@pytest.mark.asyncio
async def test_runtime_records_unknown_tool_as_tool_result():
    llm = FakeLLM([
        {"role": "assistant", "content": None, "tool_calls": [_named_tool_call("missing", "{}")]},
        {"role": "assistant", "content": "finished"},
    ])
    ctx = ContextManager(system_prompt="system")
    runtime = AgentRuntime(
        llm_client=llm,
        context_manager=ctx,
        tools=[EchoTool()],
        workspace_dir=".",
        max_steps=3,
    )

    result = await runtime.run("hello")

    assert result == "finished"
    tool_messages = [m for m in ctx.messages if m["role"] == "tool"]
    assert "unknown tool" in tool_messages[-1]["content"]


@pytest.mark.asyncio
async def test_runtime_converts_tool_exception_to_tool_result():
    llm = FakeLLM([
        {"role": "assistant", "content": None, "tool_calls": [_named_tool_call("fail", "{}")]},
        {"role": "assistant", "content": "finished"},
    ])
    ctx = ContextManager(system_prompt="system")
    runtime = AgentRuntime(
        llm_client=llm,
        context_manager=ctx,
        tools=[FailingTool()],
        workspace_dir=".",
        max_steps=3,
    )

    result = await runtime.run("hello")

    assert result == "finished"
    tool_messages = [m for m in ctx.messages if m["role"] == "tool"]
    assert "Internal Tool Error" in tool_messages[-1]["content"]


@pytest.mark.asyncio
async def test_runtime_streams_tool_call_tool_result_and_done_events():
    llm = FakeLLM([
        {"role": "assistant", "content": None, "tool_calls": [_tool_call('{"value": "abc"}')]},
        {"role": "assistant", "content": "finished"},
    ])
    ctx = ContextManager(system_prompt="system")
    runtime = AgentRuntime(
        llm_client=llm,
        context_manager=ctx,
        tools=[EchoTool()],
        workspace_dir=".",
        max_steps=3,
    )

    events = [event async for event in runtime.run_stream("hello")]
    event_types = [event.type for event in events]

    assert "thought" in event_types
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert event_types[-1] == "done"
    assert [event.tool_name for event in events if event.type == "tool_call"] == ["echo"]
    assert [event.content for event in events if event.type == "done"] == ["finished"]


@pytest.mark.asyncio
async def test_runtime_repeated_tool_call_triggers_circuit_breaker():
    repeated = _tool_call('{"value": "abc"}')
    llm = FakeLLM([
        {"role": "assistant", "content": None, "tool_calls": [repeated]},
        {"role": "assistant", "content": None, "tool_calls": [repeated]},
        {"role": "assistant", "content": None, "tool_calls": [repeated]},
        {"role": "assistant", "content": "finished"},
    ])
    ctx = ContextManager(system_prompt="system")
    runtime = AgentRuntime(
        llm_client=llm,
        context_manager=ctx,
        tools=[EchoTool()],
        workspace_dir=".",
        max_steps=5,
    )

    result = await runtime.run("hello")

    assert result == "finished"
    tool_messages = [m for m in ctx.messages if m["role"] == "tool"]
    assert "Repeated tool call detected" in tool_messages[-1]["content"]


@pytest.mark.asyncio
async def test_runtime_stops_at_max_steps():
    llm = FakeLLM([
        {"role": "assistant", "content": None, "tool_calls": [_tool_call('{"value": "abc"}')]},
        {"role": "assistant", "content": None, "tool_calls": [_tool_call('{"value": "def"}')]},
    ])
    ctx = ContextManager(system_prompt="system")
    runtime = AgentRuntime(
        llm_client=llm,
        context_manager=ctx,
        tools=[EchoTool()],
        workspace_dir=".",
        max_steps=1,
    )

    result = await runtime.run("hello")

    assert "maximum step limit" in result
    assert ctx.messages[-1]["role"] == "assistant"
    assert "maximum step limit" in ctx.messages[-1]["content"]


@pytest.mark.asyncio
async def test_actor_agent_uses_shared_runtime_for_run():
    llm = FakeLLM([{"role": "assistant", "content": "actor finished"}])
    ctx = ContextManager(system_prompt="system")
    actor = ActorAgent(
        llm_client=llm,
        context_manager=ctx,
        tools=[],
        workspace_dir=".",
        actor_id="task_1",
        max_steps=3,
    )

    summary = await actor.run("hello")

    assert summary.task_id == "task_1"
    assert summary.status == "done"
    assert summary.key_findings == "actor finished"


@pytest.mark.asyncio
async def test_actor_stream_events_include_run_and_task_metadata():
    llm = FakeLLM([{"role": "assistant", "content": "actor finished"}])
    ctx = ContextManager(system_prompt="system")
    run_context = RunContext.create(run_id="run_actor")
    actor = ActorAgent(
        llm_client=llm,
        context_manager=ctx,
        tools=[],
        workspace_dir=".",
        actor_id="task_1",
        run_context=run_context,
        max_steps=3,
    )

    events = [event async for event in actor.run_stream("hello")]

    assert {event.run_id for event in events} == {"run_actor"}
    assert {event.actor_id for event in events} == {"task_1"}
    assert {event.task_id for event in events} == {"task_1"}


@pytest.mark.asyncio
async def test_runtime_run_uses_stream_path_and_publishes_events_once():
    llm = FakeLLM([
        {"role": "assistant", "content": None, "tool_calls": [_tool_call('{"value": "abc"}')]},
        {"role": "assistant", "content": "finished"},
    ])
    run_context = RunContext.create(run_id="run_shared")
    runtime = AgentRuntime(
        llm_client=llm,
        context_manager=ContextManager(system_prompt="system"),
        tools=[EchoTool()],
        workspace_dir=".",
        run_context=run_context,
        max_steps=3,
    )

    result = await runtime.run("hello")
    published = []
    while not run_context.events.empty():
        published.append(await run_context.events.get())

    assert result == "finished"
    assert [event.type for event in published].count("tool_call") == 1
    assert [event.type for event in published].count("tool_result") == 1
    assert [event.type for event in published].count("done") == 1
    assert {event.run_id for event in published} == {"run_shared"}


@pytest.mark.asyncio
async def test_planner_stream_preserves_delegate_actor_update_and_token_stats():
    GlobalState.reset()
    delegate_call = _named_tool_call("delegate", "{}", call_id="call_delegate")
    llm = FakeLLM([
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [delegate_call],
            "_usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
        {
            "role": "assistant",
            "content": "planner finished",
            "_usage": {"prompt_tokens": 5, "completion_tokens": 3},
        },
    ])
    ctx = ContextManager(system_prompt="system")
    planner = Planner(
        llm_client=llm,
        context_manager=ctx,
        tools=[FakeDelegateTool()],
        workspace_dir=".",
        max_steps=3,
    )

    events = [event async for event in planner.run_stream("hello")]
    event_types = [event.type for event in events]

    assert "actor_update" in event_types
    assert "token_stats" in event_types
    assert event_types[-1] == "done"
    token_stats = [event for event in events if event.type == "token_stats"][-1]
    assert '"prompt_tokens": 15' in token_stats.content
    assert '"completion_tokens": 5' in token_stats.content


@pytest.mark.asyncio
async def test_planner_stream_includes_nested_actor_events_once():
    delegate_call = _named_tool_call("delegate", "{}", call_id="call_delegate")
    llm = FakeLLM([
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [delegate_call],
        },
        {"role": "assistant", "content": "planner finished"},
    ])
    nested_tool = FakeNestedEventTool()
    planner = Planner(
        llm_client=llm,
        context_manager=ContextManager(system_prompt="system"),
        tools=[nested_tool],
        workspace_dir=".",
        max_steps=3,
    )

    events = [event async for event in planner.run_stream("hello")]
    tool_calls = [event for event in events if event.type == "tool_call"]

    assert nested_tool._run_context is planner.run_context
    assert [event.actor_id for event in tool_calls] == ["", "task_child"]
    assert [event.tool_name for event in tool_calls] == ["delegate", "child_read"]
    assert len([event for event in events if event.type == "done"]) == 1
    assert {event.run_id for event in events} == {planner.run_context.run_id}
