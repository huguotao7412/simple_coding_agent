from __future__ import annotations

import pytest
from pathlib import Path
from core.agent import AgentEvent
from core.context import ContextManager
from core.tools.base import ToolResult
from web.bridge import WebBridge


class _SessionState(dict):
    """dict subclass that also supports dot access like Streamlit's st.session_state."""
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)

    def __setattr__(self, key, value):
        self[key] = value


class MockSt:
    def __init__(self):
        self.session_state = _SessionState()
        self.rerun_calls = 0

    def rerun(self):
        self.rerun_calls += 1


class FakeAgentForBridge:
    def __init__(self, events=None, workspace_dir="/tmp/ws"):
        self.events = events or []
        self.workspace_dir = workspace_dir
        self.ctx = ContextManager(system_prompt="Test prompt")
        self.llm = None

    async def run_stream(self, user_input):
        for event in self.events:
            yield event


class TestWebBridge:
    @pytest.fixture
    def bridge(self):
        agent = FakeAgentForBridge(workspace_dir="/tmp/ws")
        return WebBridge(agent)

    @pytest.fixture
    def st(self):
        return MockSt()

    def test_init_session_sets_defaults(self, bridge, st):
        bridge.init_session(st)

        assert st.session_state["messages"] == []
        assert st.session_state["events"] == []
        assert st.session_state["streaming"] is False
        assert st.session_state["workspace_root"] == "/tmp/ws"
        assert "current_project" in st.session_state

    def test_init_session_preserves_existing_values(self, bridge, st):
        st.session_state["messages"] = [{"role": "user", "content": "hello"}]
        bridge.init_session(st)
        assert st.session_state["messages"] == [{"role": "user", "content": "hello"}]

    @pytest.mark.asyncio
    async def test_handle_user_input_adds_user_message(self, bridge, st):
        bridge.init_session(st)
        bridge.agent.events = [
            AgentEvent(type="thought", token="H"),
            AgentEvent(type="done", content="Hi"),
        ]

        await bridge.handle_user_input("Hi", st)

        assert st.session_state["messages"][0] == {"role": "user", "content": "Hi"}
        assert st.session_state["streaming"] is False

    @pytest.mark.asyncio
    async def test_handle_user_input_appends_assistant_message_on_done(self, bridge, st):
        bridge.init_session(st)
        bridge.agent.events = [AgentEvent(type="done", content="Reply")]

        await bridge.handle_user_input("Q", st)

        assert st.session_state["streaming"] is False
        assert st.session_state["events"] == []
        assert st.session_state["messages"][0] == {"role": "user", "content": "Q"}

    @pytest.mark.asyncio
    async def test_handle_user_input_triggers_rerun_on_tool_result(self, bridge, st):
        bridge.init_session(st)
        bridge.agent.events = [
            AgentEvent(type="tool_call", tool_name="read", tool_args={}),
            AgentEvent(type="tool_result", tool_name="read", tool_result=ToolResult.ok("data")),
            AgentEvent(type="done", content="OK"),
        ]

        await bridge.handle_user_input("read", st)

        assert st.rerun_calls >= 1

    def test_switch_project_resets_context(self, bridge, st):
        bridge.init_session(st)
        st.session_state["messages"] = [{"role": "user", "content": "old"}]
        st.session_state["current_project"] = "old-project"
        bridge.agent.ctx.add_user_message("some context")
        assert len(bridge.agent.ctx.messages) > 1

        Path("/tmp/ws/new-project").mkdir(parents=True, exist_ok=True)

        bridge.switch_project("new-project", st)

        assert bridge.agent.workspace_dir == str(Path("/tmp/ws/new-project"))
        assert st.session_state["messages"] == []
        assert st.session_state["events"] == []
        assert st.session_state["current_project"] == "new-project"
        assert len(bridge.agent.ctx.messages) == 1
        assert bridge.agent.ctx.messages[0]["role"] == "system"
