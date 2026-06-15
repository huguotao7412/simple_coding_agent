from __future__ import annotations

from pathlib import Path
from core.agent import Agent


class WebBridge:
    """Connects Agent run_stream() generator to Streamlit st.session_state."""

    def __init__(self, agent: Agent):
        self.agent = agent

    def init_session(self, st) -> None:
        defaults = {
            "messages": [],
            "events": [],
            "streaming": False,
            "workspace_root": str(self.agent.workspace_dir),
            "current_project": Path(self.agent.workspace_dir).name,
        }
        for key, val in defaults.items():
            if key not in st.session_state:
                st.session_state[key] = val

    async def handle_user_input(self, user_input: str, st) -> None:
        if st.session_state.get("streaming", False):
            return

        st.session_state.messages.append({"role": "user", "content": user_input})
        st.session_state.streaming = True
        st.session_state.events = []

        async for event in self.agent.run_stream(user_input):
            st.session_state.events.append(event)
            if event.type in ("tool_result", "done"):
                st.rerun()
            elif event.type == "tool_call":
                st.rerun()

        st.session_state.streaming = False
        st.session_state.events = []

    def switch_project(self, project_name: str, st) -> None:
        root = Path(st.session_state.workspace_root)
        new_path = root / project_name
        new_path.mkdir(parents=True, exist_ok=True)
        self.agent.workspace_dir = str(new_path)
        self.agent.refresh_system_prompt()
        self.agent.ctx.messages = [self.agent.ctx.messages[0]]
        st.session_state.messages = []
        st.session_state.events = []
        st.session_state.current_project = project_name
