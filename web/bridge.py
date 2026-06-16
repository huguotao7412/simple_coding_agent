from __future__ import annotations

import asyncio
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

    def handle_user_input_sync(self, user_input: str, st):
        if st.session_state.get("streaming", False):
            return

        st.session_state.messages.append({"role": "user", "content": user_input})
        st.session_state.streaming = True
        st.session_state.events = []

        # 创建一个独立的事件循环驱动 async 任务，并在主线程中 yield 释放控制权给 Streamlit
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        gen = self.agent.run_stream(user_input)

        try:
            while True:
                # 每次获取一个 token 或事件
                event = loop.run_until_complete(gen.__anext__())
                st.session_state.events.append(event)
                yield event
        except StopAsyncIteration:
            pass
        finally:
            loop.close()

        st.session_state.streaming = False

    def switch_project(self, project_name: str, st) -> None:
        root = Path(st.session_state.workspace_root)
        new_path = root / project_name
        new_path.mkdir(parents=True, exist_ok=True)
        self.agent.workspace_dir = str(new_path)
        # Dynamic context is now injected per-request; just reset the
        # conversation to the static system prompt (messages[0]).
        self.agent.ctx.messages = [self.agent.ctx.messages[0]]
        st.session_state.messages = []
        st.session_state.events = []
        st.session_state.current_project = project_name
