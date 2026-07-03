from __future__ import annotations

import streamlit as st
from core.agent import AgentEvent
from core.tools.base import ToolResult


def render_chat_history():
    """Render all historical user/assistant messages from st.session_state."""
    for msg in st.session_state.messages:
        if msg["role"] == "user":
            with st.chat_message("user"):
                st.markdown(msg["content"])
        elif msg["role"] == "assistant":
            with st.chat_message("assistant"):
                st.markdown(msg["content"])


def render_current_events():
    """Render the current turn's AgentEvent stream from st.session_state.events."""
    events: list[AgentEvent] = st.session_state.get("events", [])
    if not events:
        return

    i = 0
    while i < len(events):
        event = events[i]

        if event.type == "thought":
            thought_text, consumed = _collect_thought_tokens(events, i)
            i += consumed
            if thought_text.strip():
                if st.session_state.streaming:
                    placeholder = st.empty()
                    placeholder.markdown(thought_text + "▌")
                else:
                    with st.chat_message("assistant"):
                        st.markdown(thought_text)

        elif event.type == "tool_call":
            tool_name = event.tool_name or "unknown"
            with st.status(f"执行: {tool_name}...", expanded=True) as status:
                result = _find_matching_result(events, i)
                if result and result.success:
                    status.update(label=f"{tool_name} 完成", state="complete")
                    _render_tool_output(tool_name, result)
                elif result:
                    status.update(label=f"{tool_name} 失败", state="error")
                    st.error(result.error)
                else:
                    st.text("等待执行结果...")
            i += 1
            if result:
                i += 1

        elif event.type == "compaction":
            st.toast("上下文已压缩，释放空间")
            i += 1

        elif event.type == "actor_update":
            import json
            try:
                snapshot = json.loads(event.content)
                st.session_state["actor_snapshot"] = snapshot.get(
                    "task_tree", {}
                )
            except Exception:
                pass
            i += 1

        elif event.type == "error":
            st.error(event.content)
            i += 1

        elif event.type == "done":
            content = event.content or ""
            if content.strip():
                st.session_state.messages.append({"role": "assistant", "content": content})
            i += 1

        elif event.type == "token_stats":
            import json
            try:
                stats = json.loads(event.content)
                prompt_t = stats.get("prompt_tokens", 0)
                completion_t = stats.get("completion_tokens", 0)
                total_t = prompt_t + completion_t
                st.caption(
                    f"📊 Token — prompt: {prompt_t:,}  "
                    f"completion: {completion_t:,}  "
                    f"total: {total_t:,}"
                )
            except Exception:
                pass
            i += 1

        else:
            i += 1


def _collect_thought_tokens(events: list[AgentEvent], start: int) -> tuple[str, int]:
    """Collect consecutive thought tokens starting at `start`."""
    parts = []
    for e in events[start:]:
        if e.type == "thought":
            parts.append(e.token)
        else:
            break
    return "".join(parts), len(parts)


def _find_matching_result(events: list[AgentEvent], tool_call_idx: int) -> ToolResult | None:
    """Find the tool_result event that follows a tool_call at tool_call_idx."""
    for e in events[tool_call_idx + 1:]:
        if e.type == "thought":
            continue
        if e.type == "tool_result":
            return e.tool_result
        break
    return None


def _render_tool_output(tool_name: str, result: ToolResult):
    """Render tool execution output based on tool type."""
    content = result.content[:3000] if result.content else ""
    if not content:
        return

    if tool_name == "edit":
        st.code(content, language="diff")
    elif tool_name in ("read", "write"):
        st.code(content, language="text")
    elif tool_name == "bash":
        st.code(content, language="bash")
    else:
        st.text(content)
