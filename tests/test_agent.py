import asyncio
import tempfile
import os
import pytest
from core.agent import Agent
from core.context import ContextManager
from core.tools.read import ReadTool
from core.tools.write import WriteTool
from core.tools.edit import EditTool
from core.tools.bash import BashTool
from core.system_prompt import SYSTEM_PROMPT


class FakeLLMClient:
    """Mock LLM that returns predetermined responses for testing the agent loop."""
    def __init__(self, responses: list[dict]):
        self.responses = responses
        self.call_count = 0

    async def chat(self, messages, tools=None, on_token=None):
        if self.call_count >= len(self.responses):
            return {"role": "assistant", "content": "done"}
        resp = self.responses[self.call_count]
        self.call_count += 1
        return resp


class TestAgent:
    def test_agent_returns_text_when_no_tool_calls(self):
        llm = FakeLLMClient([
            {"role": "assistant", "content": "Hello, how can I help?"}
        ])
        agent = Agent(
            llm_client=llm,
            context_manager=ContextManager(SYSTEM_PROMPT),
            tools=[ReadTool(), WriteTool(), EditTool(), BashTool()],
            workspace_dir="/tmp/test",
        )
        result = asyncio.run(agent.run("hi"))
        assert result == "Hello, how can I help?"

    def test_agent_executes_tool_and_continues(self):
        with tempfile.TemporaryDirectory() as d:
            llm = FakeLLMClient([
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command": "echo test"}'}
                    }]
                },
                {"role": "assistant", "content": "Command executed successfully."}
            ])
            agent = Agent(
                llm_client=llm,
                context_manager=ContextManager(SYSTEM_PROMPT),
                tools=[BashTool()],
                workspace_dir=d,
            )
            result = asyncio.run(agent.run("run a test command"))
            assert "Command executed" in result

    def test_agent_error_feeding(self):
        """When a tool fails, the error should be fed back to the model."""
        llm = FakeLLMClient([
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"file_path": "/nonexistent/file.txt"}'}
                }]
            },
            {"role": "assistant", "content": "The file doesn't exist. Let me create it."}
        ])
        agent = Agent(
            llm_client=llm,
            context_manager=ContextManager(SYSTEM_PROMPT),
            tools=[ReadTool()],
            workspace_dir="/tmp",
        )
        result = asyncio.run(agent.run("read /nonexistent/file.txt"))
        assert "file doesn't exist" in result.lower() or "create" in result.lower()
