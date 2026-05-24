from __future__ import annotations

import pytest
from core.agent import AgentEvent
from core.tools.base import ToolResult


class TestAgentEvent:
    def test_default_values(self):
        event = AgentEvent(type="done")
        assert event.type == "done"
        assert event.content == ""
        assert event.tool_name is None
        assert event.tool_args is None
        assert event.tool_result is None
        assert event.token == ""

    def test_tool_call_event(self):
        event = AgentEvent(
            type="tool_call",
            tool_name="edit",
            tool_args={"file_path": "/a.py", "old_string": "x", "new_string": "y"},
        )
        assert event.tool_name == "edit"
        assert event.tool_args == {"file_path": "/a.py", "old_string": "x", "new_string": "y"}

    def test_tool_result_event(self):
        result = ToolResult.ok("done")
        event = AgentEvent(type="tool_result", tool_name="write", tool_result=result)
        assert event.tool_result is result
        assert event.tool_result.success is True


from unittest.mock import AsyncMock, patch
from core.context import ContextManager


class FakeLLM:
    """Fake LLMClient that returns a sequence of responses, then repeats the last."""

    def __init__(self, *responses: dict):
        self.responses = list(responses)
        self.call_count = 0

    async def chat(self, messages, tools=None, on_token=None):
        idx = min(self.call_count, len(self.responses) - 1)
        response = self.responses[idx]
        self.call_count += 1
        content = response.get("content") or ""
        if on_token and content:
            for char in content:
                on_token(char)
        return response


class TestAgentRunStream:
    @pytest.fixture
    def ctx(self):
        return ContextManager(system_prompt="You are helpful.")

    def make_agent(self, llm, ctx, tools=None, workspace="/tmp/ws"):
        from core.agent import Agent
        return Agent(llm_client=llm, context_manager=ctx, tools=tools or [], workspace_dir=workspace)

    @pytest.mark.asyncio
    async def test_yields_thought_then_done_for_simple_response(self, ctx):
        llm = FakeLLM({"role": "assistant", "content": "Hello!"})
        agent = self.make_agent(llm, ctx)

        events = [e async for e in agent.run_stream("Hi")]

        types = [e.type for e in events]
        assert "thought" in types
        assert types[-1] == "done"
        assert events[-1].content == "Hello!"
        assert ctx.messages[-2]["role"] == "user"
        assert ctx.messages[-2]["content"] == "Hi"

    @pytest.mark.asyncio
    async def test_yields_compaction_when_context_full(self, ctx):
        ctx.model_context_limit = 1000
        ctx.compression_threshold = 0.1
        ctx.messages.append({"role": "user", "content": "X" * 500})

        llm = FakeLLM({"role": "assistant", "content": "OK"})
        agent = self.make_agent(llm, ctx)

        events = [e async for e in agent.run_stream("Hi")]

        types = [e.type for e in events]
        assert "compaction" in types

    @pytest.mark.asyncio
    async def test_yields_tool_call_and_tool_result(self, ctx, tmp_path):
        from core.tools.write import WriteTool

        workspace = str(tmp_path)
        file_path = str(tmp_path / "test.txt")
        import json
        llm = FakeLLM(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "write",
                        "arguments": json.dumps({"file_path": file_path, "content": "hello"}),
                    },
                }],
            },
            {"role": "assistant", "content": "Done!"},
        )
        write_tool = WriteTool()
        agent = self.make_agent(llm, ctx, [write_tool], workspace=workspace)

        events = [e async for e in agent.run_stream("Create test.txt")]

        types = [e.type for e in events]
        assert "tool_call" in types
        assert "tool_result" in types
        tool_result_event = next(e for e in events if e.type == "tool_result")
        assert tool_result_event.tool_name == "write"
        assert tool_result_event.tool_result.success
        assert types[-1] == "done"

    @pytest.mark.asyncio
    async def test_original_run_still_works(self, ctx):
        """Ensure run() is untouched and still returns a string."""
        llm = FakeLLM({"role": "assistant", "content": "Hello from run()"})
        agent = self.make_agent(llm, ctx)

        result = await agent.run("Hi")

        assert isinstance(result, str)
        assert "Hello from run()" in result
