from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="Simple Coding Agent",
    page_icon=":hammer_and_wrench:",
    layout="wide",
)

from core.config import load_runtime_environment

load_runtime_environment(os.getcwd())

from core.runtime.conversation import ContextManager
from core.llm import LLMClient
from core.planner import Planner
from core.orchestration.interactive import InteractiveOrchestrationSession
from core.runs.context import RunContext
from core.runs.task_state import GlobalState
from core.system_prompt import PLANNER_SYSTEM_PROMPT
from core.tools import PLANNER_TOOLS
from web.bridge import WebBridge
from web.components.chat import render_chat_history, render_current_events
from web.components.sidebar import render_sidebar
from web.dashboard import render_dashboard


def init_planner() -> Planner:
    api_key = os.getenv("SCA_API_KEY", "")
    if not api_key:
        st.error("SCA_API_KEY is not configured. Run `sca config init` first.")
        st.stop()

    base_url = os.getenv("SCA_API_BASE", "https://api.deepseek.com")
    model = os.getenv("SCA_MODEL", "deepseek-v4-pro")
    max_tokens = int(os.getenv("SCA_MAX_TOKENS", "128000"))
    workspace = os.path.abspath(os.getenv("SCA_WORKSPACE", "./workspaces"))

    llm = LLMClient(
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_tokens=max_tokens,
    )

    from core.git_utils import cleanup_orphans

    try:
        cleanup_orphans(workspace)
    except Exception:
        pass

    ctx = ContextManager(system_prompt=PLANNER_SYSTEM_PROMPT)
    tools = [tool_cls() for tool_cls in PLANNER_TOOLS]
    GlobalState.get()
    return Planner(
        llm_client=llm,
        context_manager=ctx,
        tools=tools,
        workspace_dir=workspace,
    )


def init_interactive_session(planner: Planner) -> InteractiveOrchestrationSession:
    from cli.main import build_planner
    from cli.runs import create_durable_run_context

    workspace = planner.workspace_dir
    model = planner.llm.model

    async def context_factory(messages):
        return await create_durable_run_context(
            workspace_dir=workspace,
            model=model,
            messages=messages,
        )

    def planner_factory(context_manager, run_context: RunContext):
        return build_planner(
            workspace_dir=workspace,
            model=model,
            context_manager=context_manager,
            run_context=run_context,
        )

    return InteractiveOrchestrationSession(
        system_prompt=PLANNER_SYSTEM_PROMPT,
        context_factory=context_factory,
        planner_factory=planner_factory,
    )


def render_live_agent() -> None:
    if "agent" not in st.session_state:
        st.session_state.agent = init_planner()
        st.session_state.bridge = WebBridge(
            st.session_state.agent,
            init_interactive_session(st.session_state.agent),
            session_factory=init_interactive_session,
        )
        st.session_state.bridge.init_session(st)

    bridge: WebBridge = st.session_state.bridge
    st.sidebar.caption(f"Model: {st.session_state.agent.llm.model}")

    selected_file = render_sidebar(
        st.session_state.workspace_root,
        st.session_state.current_project,
    )

    selected = st.session_state.get("project_selector")
    if selected and selected != st.session_state.current_project:
        bridge.switch_project(selected, st)
        st.rerun()

    col_main, col_preview = st.columns([3, 2])

    with col_main:
        st.title(st.session_state.current_project)
        render_chat_history()
        render_current_events()

        if bridge.pending_run is not None:
            st.warning("This run is paused for high-risk approval.")
            if bridge.approval_payload:
                st.json(bridge.approval_payload)
            approve_col, reject_col = st.columns(2)
            if approve_col.button("Approve and resume", type="primary"):
                for _ in bridge.resume_pending_sync(True, st):
                    pass
                st.rerun()
            if reject_col.button("Reject"):
                for _ in bridge.resume_pending_sync(False, st):
                    pass
                st.rerun()

        user_input = st.chat_input("Enter an instruction...")
        if user_input and not st.session_state.streaming:
            event_placeholder = st.empty()
            for _ in bridge.handle_user_input_sync(user_input, st):
                with event_placeholder.container():
                    render_current_events()
            st.rerun()

    with col_preview:
        if selected_file and Path(selected_file).exists():
            file_path = Path(selected_file)
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                lang = file_path.suffix.lstrip(".")
                st.caption(f"Preview: {file_path.name}")
                st.code(content, language=lang or "text", line_numbers=True)
            except Exception:
                st.warning("Unable to read file")
        else:
            st.info("Select a file from the sidebar to preview it.")


mode = st.sidebar.radio("View", ["Trace/Eval Dashboard", "Live Agent"], index=0)
if mode == "Trace/Eval Dashboard":
    render_dashboard(Path("eval_results.json"))
else:
    render_live_agent()
