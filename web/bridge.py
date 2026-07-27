from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from core.planner import Planner
from core.orchestration.interactive import (
    InteractiveOrchestrationSession,
    InteractiveRun,
)


class WebBridge:
    """Connects Agent run_stream() generator to Streamlit st.session_state."""

    def __init__(
        self,
        agent: Planner,
        session: InteractiveOrchestrationSession,
        session_factory: (
            Callable[[Planner], InteractiveOrchestrationSession] | None
        ) = None,
    ):
        self.agent = agent
        self.session = session
        self._session_factory = session_factory
        self.pending_run: InteractiveRun | None = None
        self.approval_payload: dict | None = None

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

        import threading
        import queue

        q = queue.Queue()

        # 将事件循环与 LLM 网络请求封装到独立线程
        def run_agent_in_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def runner():
                try:
                    active_run = await self.session.start(user_input)
                    final_output = ""
                    async for event in active_run.start_stream():
                        if event.type == "graph_interrupted":
                            import json

                            try:
                                self.approval_payload = json.loads(event.content)
                            except json.JSONDecodeError:
                                self.approval_payload = {}
                            active_run.interrupted = True
                            self.pending_run = active_run
                        elif event.type in {"done", "error"}:
                            final_output = event.content
                        q.put(event)
                    if not active_run.interrupted:
                        self.session.complete(active_run, final_output)
                finally:
                    q.put(None)  # 结束哨兵

            loop.run_until_complete(runner())
            loop.close()

        t = threading.Thread(target=run_agent_in_thread)
        t.start()

        # 主线程只负责从队列高速消费并 yield 给 Streamlit
        while True:
           try:
                event = q.get(timeout=0.05)
                if event is None:
                    break
                st.session_state.events.append(event)
                yield event
           except queue.Empty:
              # 如果没有取到数据，短暂让出控制权，保持 UI 线程活跃
              continue
        

        st.session_state.streaming = False

    def resume_pending_sync(self, approved: bool, st):
        active_run = self.pending_run
        if active_run is None:
            return
        st.session_state.streaming = True
        st.session_state.events = []

        import threading
        import queue

        q = queue.Queue()

        def run_agent_in_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def runner():
                final_output = ""
                try:
                    async for event in active_run.resume_stream(approved):
                        if event.type in {"done", "error"}:
                            final_output = event.content
                        q.put(event)
                    self.session.complete(active_run, final_output)
                    self.pending_run = None
                    self.approval_payload = None
                finally:
                    q.put(None)

            loop.run_until_complete(runner())
            loop.close()

        threading.Thread(target=run_agent_in_thread).start()
        while True:
            try:
                event = q.get(timeout=0.05)
                if event is None:
                    break
                st.session_state.events.append(event)
                yield event
            except queue.Empty:
                continue
        st.session_state.streaming = False

    def switch_project(self, project_name: str, st) -> None:
        root = Path(st.session_state.workspace_root)
        new_path = root / project_name
        new_path.mkdir(parents=True, exist_ok=True)
        self.agent.workspace_dir = str(new_path)
        # Dynamic context is now injected per-request; just reset the
        # conversation to the static system prompt (messages[0]).
        self.agent.ctx.messages = [self.agent.ctx.messages[0]]
        if self._session_factory is not None:
            self.session = self._session_factory(self.agent)
        else:
            self.session.reset_history()
        self.pending_run = None
        self.approval_payload = None

        # 清空 Planner 和 Actor 依赖的全局共享状态
        from core.runs.task_state import GlobalState
        GlobalState.reset()

        st.session_state.messages = []
        st.session_state.events = []
        st.session_state.current_project = project_name
