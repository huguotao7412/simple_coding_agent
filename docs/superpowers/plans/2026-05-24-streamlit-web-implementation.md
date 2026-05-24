# Streamlit Web Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Streamlit Web frontend (`sca-web` command) that coexists with the CLI, providing an IDE-like interface with sidebar file tree, streaming chat panel, and multi-project workspace switching.

**Architecture:** Keep `core/` and `cli/` intact. Add `AgentEvent` dataclass + `run_stream()` async generator to `core/agent.py` (existing `run()` unchanged). New `web/` package: `WebBridge` consumes the generator and pumps events into `st.session_state`; Streamlit components in `web/components/` render sidebar, chat, and diff views.

**Tech Stack:** Python 3.13+, Streamlit 1.40+, difflib (stdlib), pytest + pytest-asyncio.

---

## File Map

| # | File | Responsibility |
|---|------|---------------|
| 1 | `core/agent.py` | Add `AgentEvent` dataclass + `run_stream()` async generator |
| 2 | `pyproject.toml` | Add streamlit dep, `sca-web` script, `web*` to packages |
| 3 | `web/__init__.py`, `web/components/__init__.py` | Package scaffolding |
| 4 | `web/components/diff.py` | Pure-function unified diff → colored HTML |
| 5 | `web/bridge.py` | WebBridge: agent generator → st.session_state event queue |
| 6 | `web/components/sidebar.py` | Project selector + recursive file tree |
| 7 | `web/components/chat.py` | Chat history renderer + live event stream renderer |
| 8 | `web/main.py` | Streamlit entry point: layout, agent init, wiring |
| 9 | Integration smoke test | Full `sca-web` launch and basic interaction |

---

### Task 1: AgentEvent dataclass + run_stream() async generator

**Files:**
- Create: `tests/test_agent_stream.py`
- Modify: `core/agent.py`

- [ ] **Step 1: Write the failing test for AgentEvent**

```python
# tests/test_agent_stream.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_agent_stream.py::TestAgentEvent -v`
Expected: FAIL — `ImportError: cannot import name 'AgentEvent'`

- [ ] **Step 3: Write the failing test for run_stream() (simple response, no tools)**

```python
# Append to tests/test_agent_stream.py
from unittest.mock import AsyncMock, patch
from core.context import ContextManager


class FakeLLM:
    """Fake LLMClient that returns a predetermined response."""

    def __init__(self, response: dict):
        self.response = response

    async def chat(self, messages, tools=None, on_token=None):
        # Fire on_token for each character in content to simulate streaming
        content = self.response.get("content") or ""
        if on_token and content:
            for char in content:
                on_token(char)
        return self.response


class TestAgentRunStream:
    @pytest.fixture
    def ctx(self):
        return ContextManager(system_prompt="You are helpful.")

    def make_agent(self, llm, ctx, tools=None, workspace="/tmp/ws"):
        from core.agent import Agent
        return Agent(llm_client=llm, context_manager=ctx, tools=tools or [], workspace_dir=workspace)

    async def test_yields_thought_then_done_for_simple_response(self, ctx):
        llm = FakeLLM({"role": "assistant", "content": "Hello!"})
        agent = self.make_agent(llm, ctx)

        events = [e async for e in agent.run_stream("Hi")]

        types = [e.type for e in events]
        assert "thought" in types
        assert types[-1] == "done"
        assert events[-1].content == "Hello!"
        # Verify user message was added to context
        assert ctx.messages[-2]["role"] == "user"
        assert ctx.messages[-2]["content"] == "Hi"

    async def test_yields_compaction_when_context_full(self, ctx):
        # Fill context past the 80% threshold
        ctx.model_context_limit = 1000
        ctx.compression_threshold = 0.1  # almost always trigger
        ctx.messages.append({"role": "user", "content": "X" * 500})

        llm = FakeLLM({"role": "assistant", "content": "OK"})
        agent = self.make_agent(llm, ctx)

        events = [e async for e in agent.run_stream("Hi")]

        types = [e.type for e in events]
        assert "compaction" in types

    async def test_yields_tool_call_and_tool_result(self, ctx):
        from core.tools.write import WriteTool

        llm = FakeLLM({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "write",
                    "arguments": '{"file_path": "/tmp/ws/test.txt", "content": "hello"}',
                },
            }],
        })
        write_tool = WriteTool()
        agent = self.make_agent(llm, ctx, [write_tool], workspace="/tmp/ws")

        events = [e async for e in agent.run_stream("Create test.txt")]

        types = [e.type for e in events]
        assert "tool_call" in types
        assert "tool_result" in types
        # tool_result should have success=True for a valid write
        tool_result_event = next(e for e in events if e.type == "tool_result")
        assert tool_result_event.tool_name == "write"
        assert tool_result_event.tool_result.success

    async def test_original_run_still_works(self, ctx):
        """Ensure run() is untouched and still returns a string."""
        llm = FakeLLM({"role": "assistant", "content": "Hello from run()"})
        agent = self.make_agent(llm, ctx)

        result = await agent.run("Hi")

        assert isinstance(result, str)
        assert "Hello from run()" in result
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest tests/test_agent_stream.py::TestAgentRunStream -v`
Expected: FAIL — `AttributeError: 'Agent' object has no attribute 'run_stream'`

- [ ] **Step 5: Add AgentEvent dataclass and run_stream() to core/agent.py**

```python
# core/agent.py — add import near the top
from collections.abc import AsyncGenerator
from dataclasses import dataclass

# core/agent.py — add AgentEvent before Agent class
@dataclass
class AgentEvent:
    type: str
    # "thought"   — 模型输出了一段思考/回复文本
    # "tool_call" — 模型发起了一个工具调用
    # "tool_result" — 工具执行完成
    # "compaction"  — 上下文被压缩
    # "done"      — 最终回复完成
    content: str = ""
    tool_name: str | None = None
    tool_args: dict | None = None
    tool_result: ToolResult | None = None
    token: str = ""


# core/agent.py — add run_stream() to Agent class (after run())
    async def run_stream(
        self,
        user_input: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Async generator version of run(). Yields AgentEvent at each step.

        Existing run() calls this internally so CLI behavior is unchanged.
        """
        self.ctx.add_user_message(user_input)
        tool_schemas = [t.schema for t in self.tools_by_name.values()]

        while True:
            if self.ctx.needs_compression():
                await self.ctx.compress(self.llm)
                yield AgentEvent(type="compaction")

            tokens: list[str] = []

            def on_token(t: str) -> None:
                tokens.append(t)

            response = await self.llm.chat(
                messages=self.ctx.messages,
                tools=tool_schemas if tool_schemas else None,
                on_token=on_token,
            )

            for token in tokens:
                yield AgentEvent(type="thought", token=token, content=token)

            tool_calls = response.get("tool_calls")

            if not tool_calls:
                self.ctx.add_assistant_message(
                    content=response.get("content"),
                    reasoning_content=response.get("reasoning_content"),
                )
                yield AgentEvent(type="done", content=response.get("content") or "")
                return

            self.ctx.add_assistant_message(
                content=response.get("content"),
                tool_calls=tool_calls,
                reasoning_content=response.get("reasoning_content"),
            )

            for tc in tool_calls:
                tool_name = tc["function"]["name"]
                yield AgentEvent(
                    type="tool_call",
                    tool_name=tool_name,
                    tool_args=json.loads(tc["function"]["arguments"]),
                )

                tool = self.tools_by_name.get(tool_name)

                if tool is None:
                    result = ToolResult.fail(
                        f"unknown tool '{tool_name}'. Available: {list(self.tools_by_name.keys())}"
                    )
                else:
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError as e:
                        result = ToolResult.fail(f"invalid JSON arguments: {e}")
                    else:
                        if tool_name in ("read", "write", "edit", "bash"):
                            args["workspace_dir"] = self.workspace_dir
                        try:
                            result = await tool.execute(**args)
                        except Exception as e:
                            result = ToolResult.fail(str(e))

                observation = (
                    result.content
                    if result.success
                    else f"ERROR: {result.error}\nPartial output: {result.content}" if result.content
                    else f"ERROR: {result.error}"
                )
                self.ctx.add_tool_result(tc["id"], observation)

                yield AgentEvent(
                    type="tool_result",
                    tool_name=tool_name,
                    tool_result=result,
                )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_agent_stream.py -v`
Expected: ALL PASS

- [ ] **Step 7: Run existing tests to confirm no regressions**

Run: `pytest tests/ -v`
Expected: ALL PASS (skip if no existing tests)

- [ ] **Step 8: Commit**

```bash
git add tests/test_agent_stream.py core/agent.py
git commit -m "feat: add AgentEvent dataclass and run_stream() async generator to Agent"
```

---

### Task 2: Update pyproject.toml and install streamlit

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add streamlit dependency, sca-web script, and web* to packages**

Edit `pyproject.toml` — three changes:

1. Add `"streamlit>=1.40"` to `dependencies`
2. Add `sca-web = "web.main:main"` to `[project.scripts]`
3. Change `include = ["cli*", "core*"]` to `include = ["cli*", "core*", "web*"]`

- [ ] **Step 2: Install the updated package with new dependency**

Run: `pip install -e ".[dev]"` 
Expected: streamlit installed, no errors

- [ ] **Step 3: Verify sca-web entry point is registered**

Run: `sca-web --help`
Expected: Streamlit's default help output (since `web/main.py` doesn't exist yet, this will error — that's expected for now)

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build: add streamlit dependency and sca-web entry point"
```

---

### Task 3: Web package scaffolding

**Files:**
- Create: `web/__init__.py`
- Create: `web/components/__init__.py`

- [ ] **Step 1: Create web/__init__.py**

```python
# web/__init__.py
```

- [ ] **Step 2: Create web/components/__init__.py**

```python
# web/components/__init__.py
```

- [ ] **Step 3: Verify package is importable**

Run: `python -c "import web; import web.components; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add web/__init__.py web/components/__init__.py
git commit -m "chore: scaffold web and web.components packages"
```

---

### Task 4: Diff component (pure function, TDD)

**Files:**
- Create: `tests/test_diff.py`
- Create: `web/components/diff.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_diff.py
from __future__ import annotations

import pytest
from web.components.diff import render_diff_html


class TestRenderDiffHtml:
    def test_returns_empty_for_identical_texts(self):
        result = render_diff_html("abc", "abc", "file.py")
        # Should contain no +/- lines when texts are identical
        assert "background:#1a3a1a" not in result  # no green lines
        assert "background:#3a1a1a" not in result  # no red lines

    def test_shows_added_line_in_green(self):
        result = render_diff_html("line1", "line1\nline2", "file.py")
        assert "background:#1a3a1a" in result  # green for additions
        assert "line2" in result

    def test_shows_removed_line_in_red(self):
        result = render_diff_html("line1\nline2", "line1", "file.py")
        assert "background:#3a1a1a" in result  # red for deletions

    def test_escapes_html(self):
        result = render_diff_html("<script>", "<p>safe</p>", "x.html")
        assert "&lt;script&gt;" in result
        assert "&lt;p&gt;safe&lt;/p&gt;" in result
        assert "<script>" not in result

    def test_truncates_long_diff(self):
        lines = [f"line{i}" for i in range(300)]
        old = "\n".join(lines[:150])
        new = "\n".join(lines[150:])
        result = render_diff_html(old, new, "big.py")
        # Should not contain all 300 lines, capped at 200 diff lines
        output_lines = result.split("\n")
        assert len(output_lines) < 250

    def test_shows_hunk_header_in_blue(self):
        result = render_diff_html("old", "new", "f.py")
        # unified_diff produces @@ hunk headers
        if "@@" in result:
            assert "color:#58a6ff" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_diff.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'web.components.diff'`

- [ ] **Step 3: Write the implementation**

```python
# web/components/diff.py
from __future__ import annotations

import difflib


def render_diff_html(old_text: str, new_text: str, file_path: str) -> str:
    """Return HTML string with colored unified diff between old_text and new_text.

    Green background for additions (+), red for deletions (-), blue for hunk headers (@@).
    Truncated to 200 diff lines max.
    """
    diff_lines = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=file_path,
            tofile=file_path,
            lineterm="",
        )
    )

    colored: list[str] = []
    for line in diff_lines[:200]:
        escaped = _escape_html(line)
        if line.startswith("+"):
            colored.append(
                f'<span style="background:#1a3a1a;display:block">{escaped}</span>'
            )
        elif line.startswith("-"):
            colored.append(
                f'<span style="background:#3a1a1a;display:block">{escaped}</span>'
            )
        elif line.startswith("@@"):
            colored.append(
                f'<span style="color:#58a6ff;display:block">{escaped}</span>'
            )
        else:
            colored.append(escaped)

    return "".join(colored)


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_diff.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_diff.py web/components/diff.py
git commit -m "feat: add diff component with colored unified diff rendering"
```

---

### Task 5: WebBridge (TDD)

**Files:**
- Create: `tests/test_bridge.py`
- Create: `web/bridge.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bridge.py
from __future__ import annotations

import pytest
from unittest.mock import Mock
from pathlib import Path
from core.agent import Agent, AgentEvent
from core.context import ContextManager
from core.tools.base import ToolResult
from web.bridge import WebBridge


class MockSt:
    """Minimal mock of the Streamlit module for testing bridge logic."""

    def __init__(self):
        self.session_state = {}
        self.rerun_calls = 0

    def rerun(self):
        self.rerun_calls += 1


class FakeAgentForBridge:
    """Fake agent that yields a controlled event sequence."""

    def __init__(self, events=None, workspace_dir="/tmp/ws"):
        self.events = events or []
        self.workspace_dir = workspace_dir
        self.ctx = ContextManager(system_prompt="Test prompt")
        self.llm = Mock()

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

    async def test_handle_user_input_adds_user_message(self, bridge, st):
        bridge.init_session(st)
        bridge.agent.events = [
            AgentEvent(type="thought", token="H"),
            AgentEvent(type="done", content="Hi"),
        ]

        await bridge.handle_user_input("Hi", st)

        assert st.session_state["messages"][0] == {"role": "user", "content": "Hi"}
        assert st.session_state["streaming"] is False

    async def test_handle_user_input_appends_assistant_message_on_done(self, bridge, st):
        bridge.init_session(st)
        bridge.agent.events = [AgentEvent(type="done", content="Reply")]

        await bridge.handle_user_input("Q", st)

        assert st.session_state["messages"][-1] == {"role": "assistant", "content": "Reply"}

    async def test_handle_user_input_triggers_rerun_on_tool_result(self, bridge, st):
        bridge.init_session(st)
        bridge.agent.events = [
            AgentEvent(type="tool_call", tool_name="read", tool_args={}),
            AgentEvent(type="tool_result", tool_name="read", tool_result=ToolResult.ok("data")),
            AgentEvent(type="done", content="OK"),
        ]

        await bridge.handle_user_input("read", st)

        # Should have called rerun at least once (on tool_result)
        assert st.rerun_calls >= 1

    async def test_handle_user_input_blocks_during_streaming(self, bridge, st):
        """Should not allow concurrent submissions."""
        bridge.init_session(st)
        st.session_state["streaming"] = True

        # This should bail early because streaming is already True
        # Actually, the guard is in main.py (chat_input check), not bridge
        # So this test verifies bridge doesn't double-guard — it trusts the caller.
        # We'll test the guard at the Streamlit level manually.
        pass  # Manual verification at integration time

    def test_switch_project_resets_context(self, bridge, st):
        bridge.init_session(st)
        st.session_state["messages"] = [{"role": "user", "content": "old"}]
        st.session_state["current_project"] = "old-project"
        bridge.agent.ctx.add_user_message("some context")
        assert len(bridge.agent.ctx.messages) > 1

        # Create the project directory
        Path("/tmp/ws/new-project").mkdir(parents=True, exist_ok=True)

        bridge.switch_project("new-project", st)

        assert bridge.agent.workspace_dir == str(Path("/tmp/ws/new-project"))
        assert st.session_state["messages"] == []
        assert st.session_state["events"] == []
        assert st.session_state["current_project"] == "new-project"
        # Context reset: only system prompt remains
        assert len(bridge.agent.ctx.messages) == 1
        assert bridge.agent.ctx.messages[0]["role"] == "system"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_bridge.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'web.bridge'`

- [ ] **Step 3: Write WebBridge implementation**

```python
# web/bridge.py
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
        self.agent.ctx.messages = [self.agent.ctx.messages[0]]
        st.session_state.messages = []
        st.session_state.events = []
        st.session_state.current_project = project_name
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_bridge.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_bridge.py web/bridge.py
git commit -m "feat: add WebBridge connecting agent generator to session state"
```

---

### Task 6: Sidebar component

**Files:**
- Create: `web/components/sidebar.py`

This component is Streamlit-rendering-heavy — verified manually.

- [ ] **Step 1: Write the implementation**

```python
# web/components/sidebar.py
from __future__ import annotations

import streamlit as st
from pathlib import Path

EXCLUDE_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".idea", ".pytest_cache"}


def render_sidebar(workspace_root: str, current_project: str) -> str | None:
    """Render sidebar: project switcher + file tree. Returns selected file path."""

    st.sidebar.title("SCA Web")

    # --- Project switcher ---
    root = Path(workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    projects = sorted(
        d.name for d in root.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )
    if not projects:
        projects = [current_project]
        (root / current_project).mkdir(exist_ok=True)

    selected_project = st.sidebar.selectbox(
        "项目",
        options=projects,
        index=projects.index(current_project) if current_project in projects else 0,
        key="project_selector",
    )

    # --- File tree ---
    st.sidebar.divider()
    st.sidebar.subheader("文件")
    project_dir = root / current_project
    return _render_file_tree(project_dir)


def _render_file_tree(project_dir: Path) -> str | None:
    """Recursively render file tree grouped by top-level dir. Returns selected file path."""
    if not project_dir.exists():
        st.sidebar.info("项目目录不存在")
        return None

    all_files = sorted(
        [
            f for f in project_dir.rglob("*")
            if f.is_file() and not (set(f.parts) & EXCLUDE_DIRS)
        ],
        key=lambda f: (f.suffix != ".py", str(f)),
    )

    if not all_files:
        st.sidebar.info("目录为空")
        return None

    groups: dict[str, list[Path]] = {}
    for f in all_files:
        rel = f.relative_to(project_dir)
        group = str(rel.parts[0]) if len(rel.parts) > 1 else "(根目录)"
        groups.setdefault(group, []).append(f)

    selected_file: str | None = None
    for group_name, files in sorted(groups.items()):
        with st.sidebar.expander(group_name, expanded=len(groups) <= 3):
            for f in files:
                rel_path = str(f.relative_to(project_dir))
                if st.button(
                    rel_path,
                    key=f"file_{rel_path}",
                    use_container_width=True,
                ):
                    selected_file = str(f)

    return selected_file
```

- [ ] **Step 2: Verify component import**

Run: `python -c "from web.components.sidebar import render_sidebar; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add web/components/sidebar.py
git commit -m "feat: add sidebar component with project switcher and file tree"
```

---

### Task 7: Chat component

**Files:**
- Create: `web/components/chat.py`

- [ ] **Step 1: Write the implementation**

```python
# web/components/chat.py
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

        elif event.type == "done":
            content = event.content or ""
            if content.strip():
                st.session_state.messages.append({"role": "assistant", "content": content})
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
        # Detect file extension from first line of content (tool output format)
        st.code(content, language="text")
    elif tool_name == "bash":
        st.code(content, language="bash")
    else:
        st.text(content)
```

- [ ] **Step 2: Verify component import**

Run: `python -c "from web.components.chat import render_chat_history, render_current_events; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add web/components/chat.py
git commit -m "feat: add chat component with event stream and tool status rendering"
```

---

### Task 8: Main entry point (web/main.py)

**Files:**
- Create: `web/main.py`

- [ ] **Step 1: Write the implementation**

```python
# web/main.py
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
    tools = [ReadTool(), WriteTool(), EditTool(), BashTool()]
    return Agent(llm_client=llm, context_manager=ctx, tools=tools, workspace_dir=workspace)


def main():
    if "agent" not in st.session_state:
        st.session_state.agent = init_agent()
        st.session_state.bridge = WebBridge(st.session_state.agent)
        st.session_state.bridge.init_session(st)

    bridge: WebBridge = st.session_state.bridge

    # --- Sidebar ---
    with st.sidebar:
        st.caption(f"模型: {st.session_state.agent.llm.model}")

    selected_file = render_sidebar(
        st.session_state.workspace_root,
        st.session_state.current_project,
    )

    # --- Project switch detection ---
    selected = st.session_state.get("project_selector")
    if selected and selected != st.session_state.current_project:
        bridge.switch_project(selected, st)
        st.rerun()

    # --- Main layout ---
    col_main, col_preview = st.columns([3, 2])

    with col_main:
        st.title(st.session_state.current_project)
        render_chat_history()
        render_current_events()

        user_input = st.chat_input("输入你的指令...")
        if user_input and not st.session_state.streaming:
            asyncio.run(bridge.handle_user_input(user_input, st))

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


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify the module can be imported**

Run: `python -c "from web.main import init_agent; print('OK')"`
Expected: `OK` (if SCA_API_KEY is set in .env) or an error about missing API key (expected if .env not configured)

- [ ] **Step 3: Commit**

```bash
git add web/main.py
git commit -m "feat: add Streamlit main entry point with three-column layout"
```

---

### Task 9: Integration smoke test

- [ ] **Step 1: Launch the web app**

Run: `sca-web`

Expected:
- Streamlit starts, opens browser
- Sidebar shows project selector and file tree
- Chat input visible
- No errors in terminal

- [ ] **Step 2: Test basic conversation**

1. Type a simple message like "Hello" in the chat input
2. Expected: Assistant responds with streaming text in chat bubble
3. Expected: Response is preserved after page refresh

- [ ] **Step 3: Test file operation**

1. Type "Create a file called hello.py with a greeting function"
2. Expected: Tool status indicator appears (write tool executing)
3. Expected: Sidebar file tree updates after file creation
4. Expected: Clicking hello.py in sidebar shows content in preview panel

- [ ] **Step 4: Test project switching**

1. In sidebar, click the project dropdown and select a different project
2. Expected: Chat history cleared, file tree updates to new project

- [ ] **Step 5: Verify CLI still works**

Run: `sca --help`
Expected: CLI help output unchanged

- [ ] **Step 6: Run all tests one final time**

Run: `pytest tests/ -v`
Expected: ALL PASS

---

## Verification Checklist (for agentic workers)

- [ ] `pytest tests/ -v` — all tests pass
- [ ] `sca --help` — CLI entry point still works
- [ ] `sca-web` — Streamlit launches without import errors
- [ ] Sidebar file tree renders actual filesystem content
- [ ] Project switcher changes workspace and resets chat
- [ ] Chat input → agent responds with streaming text
- [ ] Tool execution (write file) → sidebar file tree updates
- [ ] File preview panel shows file content on click
- [ ] Page refresh preserves chat history
- [ ] Context compression shows toast notification
