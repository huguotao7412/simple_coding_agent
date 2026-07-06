from __future__ import annotations

import pytest

from core.context import ContextManager
from core.runtime import AgentRuntime, parse_tool_call
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


def _tool_call(arguments: str) -> dict:
    return {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "echo",
            "arguments": arguments,
        },
    }


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
