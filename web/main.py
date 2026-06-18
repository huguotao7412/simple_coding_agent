from __future__ import annotations

import asyncio
import os
from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="Simple Coding Agent",
    page_icon=":hammer_and_wrench:",
    layout="wide",
)

from dotenv import load_dotenv

load_dotenv()

from core.llm import LLMClient
from core.context import ContextManager
from core.agent import Agent
from core.tools.read import ReadTool
from core.tools.write import WriteTool
from core.tools.edit import EditTool
from core.tools.bash import BashTool
from core.tools.search import SearchCodebaseTool
from core.tools.list_dir import ListDirTool
from core.tools.read_outline import ReadOutlineTool
from core.system_prompt import SYSTEM_PROMPT
from web.bridge import WebBridge
from web.components.sidebar import render_sidebar
from web.components.chat import render_chat_history, render_current_events


def init_agent() -> Agent:
    api_key = os.getenv("SCA_API_KEY", "")
    if not api_key:
        st.error("SCA_API_KEY not set in .env file")
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
    ctx = ContextManager(system_prompt=SYSTEM_PROMPT)
    tools = [ReadTool(), WriteTool(), EditTool(), BashTool(), SearchCodebaseTool(), ListDirTool(), ReadOutlineTool()]
    return Agent(llm_client=llm, context_manager=ctx, tools=tools, workspace_dir=workspace)


# --- Page render ---

if "agent" not in st.session_state:
    st.session_state.agent = init_agent()
    st.session_state.bridge = WebBridge(st.session_state.agent)
    st.session_state.bridge.init_session(st)

bridge: WebBridge = st.session_state.bridge

with st.sidebar:
    st.caption(f"模型: {st.session_state.agent.llm.model}")

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

    user_input = st.chat_input("输入你的指令...")
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
            st.caption(f"预览: {file_path.name}")
            st.code(content, language=lang or "text", line_numbers=True)
        except Exception:
            st.warning("无法读取文件")
    else:
        st.info("点击侧边栏文件以预览")
