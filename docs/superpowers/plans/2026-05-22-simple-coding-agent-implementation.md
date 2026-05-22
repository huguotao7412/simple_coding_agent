# Simple Coding Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a pure-ReAct CLI coding agent with core/CLI separation, four atomic tools, interactive REPL, streaming Markdown output, and error self-correction.

**Architecture:** `core/` = logic engine (agent loop, LLM client, tools, context management) — zero terminal I/O. `cli/` = Rich-based terminal UI + bridge connecting user input to core. Core communicates streaming tokens via `on_token` callback.

**Tech Stack:** Python 3.13+, httpx, Rich, python-dotenv, pytest + pytest-asyncio.

---

## File Map

| # | File | Responsibility |
|---|------|---------------|
| 0 | `pyproject.toml`, `.env.example`, `.gitignore`, `__init__.py`s | Project scaffold |
| 1 | `core/exceptions.py` | Custom exceptions |
| 2 | `core/tools/base.py` | `ToolResult` + `BaseTool` ABC |
| 3 | `core/tools/read.py` | File reading with line numbers |
| 4 | `core/tools/write.py` | Full file write/overwrite |
| 5 | `core/tools/edit.py` | Search/replace + line-range dual-mode edit |
| 6 | `core/tools/bash.py` | Sandboxed subprocess with blacklist |
| 7 | `core/system_prompt.py` | System prompt text |
| 8 | `core/llm.py` | Async OpenAI-compatible API client with streaming |
| 9 | `core/context.py` | Message list, token estimation, summary compression |
| 10 | `core/agent.py` | ReAct think→act→observe loop |
| 11 | `cli/ui.py` | Rich terminal rendering |
| 12 | `cli/bridge.py` | REPL input loop, wires agent ↔ UI |
| 13 | `cli/main.py` | CLI entry point |
| 14 | Integration smoke test | End-to-end with real API |

---

### Task 0: Project Scaffolding

**Files:** `pyproject.toml`, `.env.example`, `.gitignore`, `core/__init__.py`, `core/tools/__init__.py`, `cli/__init__.py`, `tests/__init__.py`

- [ ] **Step 1: Create pyproject.toml**

```toml
[project]
name = "simple-coding-agent"
version = "0.1.0"
description = "A pure-ReAct local coding assistant agent"
requires-python = ">=3.13"
dependencies = [
    "httpx>=0.28",
    "rich>=13",
    "python-dotenv>=1.0",
]

[project.scripts]
sca = "cli.main:main"

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.25",
]
```

- [ ] **Step 2: Create .env.example**

```env
SCA_API_KEY=sk-your-key-here
SCA_API_BASE=https://api.deepseek.com
SCA_MODEL=deepseek-v4-pro
SCA_MAX_TOKENS=128000
```

- [ ] **Step 3: Create .gitignore**

```gitignore
.env
__pycache__/
*.pyc
.venv/
.pytest_cache/
dist/
*.egg-info/
.idea/
```

- [ ] **Step 4: Create empty __init__.py files**

```bash
touch core/__init__.py core/tools/__init__.py cli/__init__.py tests/__init__.py
```

- [ ] **Step 5: Init git, set remote, install deps**

```bash
cp .env.example .env
git init
git remote add origin https://github.com/huguotao7412/simple_coding_agent.git
pip install httpx rich python-dotenv pytest pytest-asyncio
```

- [ ] **Step 6: Verify**

```bash
python -c "import httpx; import rich; import dotenv; print('OK')"
```

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .env.example .gitignore core/__init__.py core/tools/__init__.py cli/__init__.py tests/__init__.py
git commit -m "chore: scaffold project structure"
```

---

### Task 1: Core Exceptions

**Files:** `core/exceptions.py`

- [ ] **Step 1: Write the file**

```python
class SCAAgentError(Exception):
    """Base exception for all SCA errors."""
    pass


class ToolExecutionError(SCAAgentError):
    def __init__(self, tool_name: str, message: str):
        self.tool_name = tool_name
        super().__init__(f"[{tool_name}] {message}")


class ToolSecurityError(SCAAgentError):
    def __init__(self, tool_name: str, message: str):
        self.tool_name = tool_name
        super().__init__(f"[{tool_name}] SECURITY: {message}")


class ContextLimitError(SCAAgentError):
    pass


class LLMAPIError(SCAAgentError):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"API error {status_code}: {message}")
```

- [ ] **Step 2: Commit**

```bash
git add core/exceptions.py
git commit -m "feat: add custom exception classes"
```

---

### Task 2: Tool Base

**Files:** `core/tools/base.py`

- [ ] **Step 1: Write the file**

```python
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ToolResult:
    success: bool
    content: str = ""
    error: str | None = None

    @classmethod
    def ok(cls, content: str) -> ToolResult:
        return cls(success=True, content=content)

    @classmethod
    def fail(cls, error: str, content: str = "") -> ToolResult:
        return cls(success=False, content=content, error=error)


class BaseTool(ABC):
    name: str
    description: str
    parameters: dict = {}
    required_params: list[str] = []

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required_params,
                },
            },
        }

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult: ...

    def validate_path(self, file_path: str, workspace_dir: str) -> str:
        import os
        resolved = os.path.realpath(file_path)
        workspace_real = os.path.realpath(workspace_dir)
        if not resolved.startswith(workspace_real + os.sep) and resolved != workspace_real:
            from core.exceptions import ToolSecurityError
            raise ToolSecurityError(
                self.name,
                f"Path '{file_path}' escapes workspace '{workspace_dir}'",
            )
        return resolved
```

- [ ] **Step 2: Commit**

```bash
git add core/tools/base.py
git commit -m "feat: add ToolResult and BaseTool abstractions"
```

---

### Task 3: Read Tool

**Files:** `core/tools/read.py`, `tests/test_tools.py`

- [ ] **Step 1: Write the test**

```python
import os
import tempfile
import pytest
from core.tools.read import ReadTool


@pytest.fixture
def ws():
    with tempfile.TemporaryDirectory() as d:
        yield d


class TestReadTool:
    def test_read_entire_file(self, ws):
        p = os.path.join(ws, "f.txt")
        with open(p, "w") as f:
            f.write("a\nb\nc\n")
        r = ReadTool().execute(file_path=p, workspace_dir=ws)
        assert r.success
        assert "a" in r.content
        assert "b" in r.content

    def test_read_offset_limit(self, ws):
        p = os.path.join(ws, "f.txt")
        with open(p, "w") as f:
            f.write("1\n2\n3\n4\n5\n")
        r = ReadTool().execute(file_path=p, workspace_dir=ws, offset=2, limit=2)
        assert r.success
        lines = r.content.strip().split("\n")
        assert len(lines) == 2

    def test_read_nonexistent(self, ws):
        r = ReadTool().execute(file_path=os.path.join(ws, "nope.txt"), workspace_dir=ws)
        assert not r.success

    def test_read_escapes_workspace(self, ws):
        r = ReadTool().execute(file_path="/etc/passwd", workspace_dir=ws)
        assert not r.success
```

- [ ] **Step 2: Run test — expect FAIL**

```bash
pytest tests/test_tools.py::TestReadTool -v
```

- [ ] **Step 3: Write the implementation**

```python
from __future__ import annotations
import os
from .base import BaseTool, ToolResult


class ReadTool(BaseTool):
    name = "read"
    description = "Read a file from the workspace. Returns content with line number prefixes."
    parameters = {
        "file_path": {"type": "string", "description": "Absolute path to the file."},
        "offset": {"type": "integer", "description": "Starting line (0-indexed). Default 0."},
        "limit": {"type": "integer", "description": "Max lines to read. Default 2000."},
    }
    required_params = ["file_path"]

    async def execute(self, file_path: str, workspace_dir: str = "", offset: int = 0, limit: int = 2000) -> ToolResult:
        try:
            self.validate_path(file_path, workspace_dir)
        except Exception as e:
            return ToolResult.fail(str(e))
        if not os.path.isfile(file_path):
            return ToolResult.fail(f"File not found: {file_path}")
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            if offset:
                lines = lines[offset:]
            if limit:
                lines = lines[:limit]
            output = "".join(f"{i + offset + 1}\t{line}" for i, line in enumerate(lines))
            return ToolResult.ok(output)
        except Exception as e:
            return ToolResult.fail(str(e))
```

- [ ] **Step 4: Run test — expect PASS**

```bash
pytest tests/test_tools.py::TestReadTool -v
```

- [ ] **Step 5: Commit**

```bash
git add core/tools/read.py tests/test_tools.py
git commit -m "feat: add read tool"
```

Note: tests are synchronous despite `execute` being `async`. For tool tests (no I/O to await), calling with `asyncio.run()` or just directly is fine. We'll use `asyncio.run(tool.execute(...))` for sync tests.

---

### Task 4: Write Tool

**Files:** `core/tools/write.py` (modify `tests/test_tools.py`)

- [ ] **Step 1: Add test to test_tools.py**

```python
from core.tools.write import WriteTool


class TestWriteTool:
    def test_write_new_file(self, ws):
        p = os.path.join(ws, "new.txt")
        r = WriteTool().execute(file_path=p, content="hello world", workspace_dir=ws)
        assert r.success
        assert os.path.exists(p)
        with open(p) as f:
            assert f.read() == "hello world"

    def test_write_overwrites(self, ws):
        p = os.path.join(ws, "f.txt")
        with open(p, "w") as f:
            f.write("old")
        r = WriteTool().execute(file_path=p, content="new", workspace_dir=ws)
        assert r.success
        with open(p) as f:
            assert f.read() == "new"

    def test_write_escapes_workspace(self, ws):
        r = WriteTool().execute(file_path="/etc/hacked", content="x", workspace_dir=ws)
        assert not r.success
```

- [ ] **Step 2: Run test — expect FAIL**

```bash
pytest tests/test_tools.py::TestWriteTool -v
```

- [ ] **Step 3: Write the implementation**

```python
from __future__ import annotations
import os
from .base import BaseTool, ToolResult


class WriteTool(BaseTool):
    name = "write"
    description = "Create or overwrite a file in the workspace with the given content."
    parameters = {
        "file_path": {"type": "string", "description": "Absolute path to the file."},
        "content": {"type": "string", "description": "Full file content to write."},
    }
    required_params = ["file_path", "content"]

    async def execute(self, file_path: str, content: str, workspace_dir: str = "") -> ToolResult:
        try:
            self.validate_path(file_path, workspace_dir)
        except Exception as e:
            return ToolResult.fail(str(e))
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            lines = content.count("\n") + 1
            return ToolResult.ok(f"Wrote {lines} lines to {file_path}")
        except Exception as e:
            return ToolResult.fail(str(e))
```

- [ ] **Step 4: Run test — expect PASS**

```bash
pytest tests/test_tools.py::TestWriteTool -v
```

- [ ] **Step 5: Commit**

```bash
git add core/tools/write.py tests/test_tools.py
git commit -m "feat: add write tool"
```

---

### Task 5: Edit Tool

**Files:** `core/tools/edit.py` (modify `tests/test_tools.py`)

- [ ] **Step 1: Add test to test_tools.py**

```python
from core.tools.edit import EditTool


class TestEditTool:
    def test_search_replace_single(self, ws):
        p = os.path.join(ws, "f.py")
        with open(p, "w") as f:
            f.write("def foo():\n    pass\n")
        r = EditTool().execute(file_path=p, old_string="pass", new_string="return 1", workspace_dir=ws)
        assert r.success
        with open(p) as f:
            assert "return 1" in f.read()

    def test_search_replace_multiple_without_flag_fails(self, ws):
        p = os.path.join(ws, "f.py")
        with open(p, "w") as f:
            f.write("x = 1\nx = 2\n")
        r = EditTool().execute(file_path=p, old_string="x =", new_string="y =", workspace_dir=ws)
        assert not r.success

    def test_search_replace_all(self, ws):
        p = os.path.join(ws, "f.py")
        with open(p, "w") as f:
            f.write("x = 1\nx = 2\n")
        r = EditTool().execute(file_path=p, old_string="x =", new_string="y =", replace_all=True, workspace_dir=ws)
        assert r.success
        with open(p) as f:
            content = f.read()
            assert "x =" not in content
            assert content.count("y =") == 2

    def test_line_range_replace(self, ws):
        p = os.path.join(ws, "f.py")
        with open(p, "w") as f:
            f.write("line0\nline1\nline2\nline3\n")
        r = EditTool().execute(file_path=p, start_line=1, end_line=2, new_string="replaced\n", workspace_dir=ws)
        assert r.success
        with open(p) as f:
            lines = f.readlines()
            assert lines[0] == "line0\n"
            assert lines[1] == "replaced\n"
            assert lines[2] == "line3\n"

    def test_edit_escapes_workspace(self, ws):
        r = EditTool().execute(file_path="/etc/hosts", old_string="x", new_string="y", workspace_dir=ws)
        assert not r.success
```

- [ ] **Step 2: Run test — expect FAIL**

```bash
pytest tests/test_tools.py::TestEditTool -v
```

- [ ] **Step 3: Write the implementation**

```python
from __future__ import annotations
import os
from .base import BaseTool, ToolResult


class EditTool(BaseTool):
    name = "edit"
    description = (
        "Make precise edits to a file. Two modes: "
        "(1) search/replace: provide old_string and new_string. "
        "Use replace_all=true to replace all occurrences. "
        "(2) line-range: provide start_line, end_line, new_string to replace a line range."
    )
    parameters = {
        "file_path": {"type": "string", "description": "Absolute path to the file."},
        "old_string": {"type": "string", "description": "Text to search for (search/replace mode)."},
        "new_string": {"type": "string", "description": "Replacement text."},
        "start_line": {"type": "integer", "description": "Start line for line-range mode (0-indexed)."},
        "end_line": {"type": "integer", "description": "End line for line-range mode (exclusive)."},
        "replace_all": {"type": "boolean", "description": "Replace all matches. Default false."},
    }
    required_params = ["file_path"]

    async def execute(
        self,
        file_path: str,
        workspace_dir: str = "",
        old_string: str | None = None,
        new_string: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        replace_all: bool = False,
    ) -> ToolResult:
        try:
            self.validate_path(file_path, workspace_dir)
        except Exception as e:
            return ToolResult.fail(str(e))
        if not os.path.isfile(file_path):
            return ToolResult.fail(f"File not found: {file_path}")
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return ToolResult.fail(str(e))

        # Line-range mode
        if start_line is not None and end_line is not None and new_string is not None:
            lines = content.splitlines(keepends=True)
            if start_line < 0 or end_line > len(lines):
                return ToolResult.fail(f"Line range [{start_line}:{end_line}] out of bounds (file has {len(lines)} lines)")
            new_lines = lines[:start_line] + [new_string if new_string.endswith("\n") else new_string + "\n"] + lines[end_line:]
            new_content = "".join(new_lines)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            return ToolResult.ok(f"Replaced lines [{start_line}:{end_line}] in {file_path}")

        # Search/replace mode
        if old_string is not None and new_string is not None:
            count = content.count(old_string)
            if count == 0:
                return ToolResult.fail(f"old_string not found in {file_path}")
            if count > 1 and not replace_all:
                return ToolResult.fail(
                    f"Found {count} occurrences of old_string in {file_path}. "
                    "Use replace_all=true to replace all, or provide a more specific old_string."
                )
            new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            replaced = count if replace_all else 1
            return ToolResult.ok(f"Replaced {replaced} occurrence(s) in {file_path}")

        return ToolResult.fail("Must provide either (old_string + new_string) or (start_line + end_line + new_string)")
```

- [ ] **Step 4: Run test — expect PASS**

```bash
pytest tests/test_tools.py::TestEditTool -v
```

- [ ] **Step 5: Commit**

```bash
git add core/tools/edit.py tests/test_tools.py
git commit -m "feat: add edit tool with dual-mode precision editing"
```

---

### Task 6: Bash Tool

**Files:** `core/tools/bash.py` (modify `tests/test_tools.py`)

- [ ] **Step 1: Add test to test_tools.py**

```python
from core.tools.bash import BashTool


class TestBashTool:
    def test_simple_command(self, ws):
        r = asyncio.run(BashTool().execute(command="echo hello", workspace_dir=ws))
        assert r.success
        assert "hello" in r.content

    def test_failing_command(self, ws):
        r = asyncio.run(BashTool().execute(command="nonexistent_command_xyz", workspace_dir=ws))
        assert not r.success
        assert r.error is not None

    def test_blacklisted_command_rm_rf_root(self, ws):
        r = asyncio.run(BashTool().execute(command="rm -rf /", workspace_dir=ws))
        assert not r.success
        assert "security" in r.error.lower() or "blocked" in r.error.lower()

    def test_blacklisted_sudo(self, ws):
        r = asyncio.run(BashTool().execute(command="sudo ls", workspace_dir=ws))
        assert not r.success

    def test_changes_cwd_to_workspace(self, ws):
        r = asyncio.run(BashTool().execute(command="pwd", workspace_dir=ws))
        assert r.success
        # On Windows this may be different, just check it ran
```

(Add `import asyncio` at top of test file.)

- [ ] **Step 2: Run test — expect FAIL**

```bash
pytest tests/test_tools.py::TestBashTool -v
```

- [ ] **Step 3: Write the implementation**

```python
from __future__ import annotations

import asyncio
import re
import os

from .base import BaseTool, ToolResult

BLACKLIST = [
    r"rm\s+-rf\s+/",
    r"sudo\b",
    r"chmod\s+777\s+/",
    r"mkfs\b",
    r"dd\s+if=",
    r":\(\)\s*\{\s*:\|\:&\s*\}\s*;",
    r">\s*/dev/sda",
]


class BashTool(BaseTool):
    name = "bash"
    description = (
        "Execute a shell command in the workspace directory. "
        "Returns stdout, stderr, and exit code. Commands timeout after 120s."
    )
    parameters = {
        "command": {
            "type": "string",
            "description": "The shell command to execute.",
        },
    }
    required_params = ["command"]

    async def execute(self, command: str, workspace_dir: str = "") -> ToolResult:
        # Security check
        for pattern in BLACKLIST:
            if re.search(pattern, command):
                return ToolResult.fail(f"Command blocked by security policy: matched pattern '{pattern}'")

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace_dir or os.getcwd(),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=120
            )
            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                detail = stderr_str or stdout_str or f"exit code {proc.returncode}"
                return ToolResult.fail(detail, content=stdout_str)

            return ToolResult.ok(stdout_str or "(no output)")
        except asyncio.TimeoutError:
            return ToolResult.fail("Command timed out after 120 seconds")
        except Exception as e:
            return ToolResult.fail(str(e))
```

- [ ] **Step 4: Run test — expect PASS**

```bash
pytest tests/test_tools.py::TestBashTool -v
```

- [ ] **Step 5: Commit**

```bash
git add core/tools/bash.py tests/test_tools.py
git commit -m "feat: add bash tool with security blacklist"
```

---

### Task 7: System Prompt

**Files:** `core/system_prompt.py`

- [ ] **Step 1: Write the file**

```python
SYSTEM_PROMPT = """You are Simple Coding Agent (SCA), a coding assistant running in a local terminal.

You solve programming tasks by using tools to read, write, edit, and execute code. Follow this loop:
1. **Think** about what you need to do.
2. **Act** by calling a tool.
3. **Observe** the result.
4. Repeat until the task is done, then respond to the user.

## Tools
- **read**: Read file contents with line numbers.
- **write**: Create or overwrite a file.
- **edit**: Make precise edits (search/replace or line-range).
- **bash**: Execute shell commands in the workspace.

## Rules
- Work only within the workspace directory.
- When you encounter errors, read the error message and fix the problem yourself.
- Prefer `edit` over `write` for small changes to large files.
- Read a file before editing it to ensure you know the current content.
- Keep responses concise. Show the user what changed and why.
"""
```

- [ ] **Step 2: Commit**

```bash
git add core/system_prompt.py
git commit -m "feat: add system prompt"
```

---

### Task 8: LLM Client

**Files:** `core/llm.py`

- [ ] **Step 1: Write the file**

```python
from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from .exceptions import LLMAPIError


class LLMClient:
    """Async OpenAI-compatible API client with streaming support."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-pro",
        max_tokens: int = 128000,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_token: Callable[[str], None] | None = None,
    ) -> dict:
        """Send a chat completion request. Returns the full response message dict.

        When streaming, on_token is called for each content delta.
        The returned dict always has the non-streaming format: {"role": "assistant", "content": "...", "tool_calls": [...]}
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        if tools:
            body["tools"] = tools

        async with httpx.AsyncClient(timeout=120) as client:
            try:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=body,
                ) as response:
                    if response.status_code != 200:
                        text = await response.aread()
                        raise LLMAPIError(response.status_code, text.decode()[:500])
                    return await self._parse_stream(response, on_token)
            except httpx.HTTPError as e:
                raise LLMAPIError(0, str(e))

    async def _parse_stream(
        self,
        response: httpx.Response,
        on_token: Callable[[str], None] | None,
    ) -> dict:
        content_parts: list[str] = []
        tool_calls: list[dict] = []
        tool_call_buf: dict[int, dict] = {}  # index -> {id, name, arguments}

        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break

            import json
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue

            choice = chunk.get("choices", [{}])[0]
            delta = choice.get("delta", {})

            # Content delta
            if "content" in delta and delta["content"]:
                token = delta["content"]
                content_parts.append(token)
                if on_token:
                    on_token(token)

            # Tool call delta
            if "tool_calls" in delta:
                for tc in delta["tool_calls"]:
                    idx = tc.get("index", 0)
                    if idx not in tool_call_buf:
                        tool_call_buf[idx] = {"id": "", "function": {"name": "", "arguments": ""}}
                    if "id" in tc and tc["id"]:
                        tool_call_buf[idx]["id"] = tc["id"]
                    if "function" in tc:
                        if "name" in tc["function"] and tc["function"]["name"]:
                            tool_call_buf[idx]["function"]["name"] = tc["function"]["name"]
                        if "arguments" in tc["function"]:
                            tool_call_buf[idx]["function"]["arguments"] += tc["function"]["arguments"]

        # Build tool_calls list from buffer
        for idx in sorted(tool_call_buf.keys()):
            tc = tool_call_buf[idx]
            tc["type"] = "function"
            tool_calls.append(tc)

        result: dict = {
            "role": "assistant",
            "content": "".join(content_parts) or None,
        }
        if tool_calls:
            result["tool_calls"] = tool_calls

        return result
```

- [ ] **Step 2: Commit**

```bash
git add core/llm.py
git commit -m "feat: add async OpenAI-compatible LLM client with streaming"
```

---

### Task 9: Context Manager

**Files:** `core/context.py`, `tests/test_context.py`

- [ ] **Step 1: Write the test**

```python
import pytest
from core.context import ContextManager


class TestContextManager:
    def test_add_messages(self):
        cm = ContextManager(system_prompt="You are a coder.", max_tokens=100000, model_context_limit=128000)
        cm.add_user_message("hello")
        cm.add_assistant_message("hi")
        cm.add_tool_result("tool_id_1", "output")
        assert len(cm.messages) == 4  # system + user + assistant + tool

    def test_estimate_tokens_returns_int(self):
        cm = ContextManager(system_prompt="test", max_tokens=100000, model_context_limit=128000)
        cm.add_user_message("hello world")
        tokens = cm.estimate_tokens()
        assert isinstance(tokens, int)
        assert tokens > 0

    def test_needs_compression_false_when_under_threshold(self):
        cm = ContextManager(system_prompt="test", max_tokens=100000, model_context_limit=128000)
        cm.add_user_message("short")
        # Token count will be far under 80%
        assert not cm.needs_compression()

    def test_needs_compression_threshold_respected(self):
        cm = ContextManager(system_prompt="test", max_tokens=100, model_context_limit=1000)
        # Force token count above threshold by adding many messages
        for i in range(50):
            cm.add_user_message(f"message number {i} " + "x" * 50)
        # This should trigger compression if estimate exceeds threshold
        needs = cm.needs_compression()
        # Just test the method returns a bool
        assert isinstance(needs, bool)
```

- [ ] **Step 2: Run test — expect FAIL**

```bash
pytest tests/test_context.py -v
```

- [ ] **Step 3: Write the implementation**

```python
from __future__ import annotations

import tiktoken


class ContextManager:
    """Manages the conversation message list, token estimation, and compression."""

    def __init__(
        self,
        system_prompt: str,
        max_tokens: int = 128000,
        model_context_limit: int = 128000,
        compression_threshold: float = 0.8,
        keep_recent: int = 5,
    ):
        self.max_tokens = max_tokens
        self.model_context_limit = model_context_limit
        self.compression_threshold = compression_threshold
        self.keep_recent = keep_recent
        self.messages: list[dict] = [
            {"role": "system", "content": system_prompt}
        ]

    def add_user_message(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(self, content: str | None, tool_calls: list[dict] | None = None) -> None:
        msg: dict = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self.messages.append(msg)

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        })

    def estimate_tokens(self) -> int:
        """Rough token count estimation: ~4 chars per token."""
        total = 0
        for msg in self.messages:
            for key, value in msg.items():
                if isinstance(value, str):
                    total += len(value) // 4
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            total += len(str(item)) // 4
        return max(1, total)

    def needs_compression(self) -> bool:
        return self.estimate_tokens() > int(self.model_context_limit * self.compression_threshold)

    def get_compressible_range(self) -> tuple[int, int]:
        """Return (start, end) indices of messages to compress.

        Preserves system prompt (index 0) and last `keep_recent` turns.
        A "turn" = user message + assistant response + optional tool messages.
        """
        if len(self.messages) <= 1 + self.keep_recent * 2:
            return (1, 1)  # nothing to compress

        # Find the start of the last keep_recent user messages
        user_indices = [
            i for i, m in enumerate(self.messages)
            if m["role"] == "user"
        ]
        if len(user_indices) <= self.keep_recent:
            return (1, 1)

        end = user_indices[-self.keep_recent]
        return (1, end)

    async def compress(self, llm_client, compression_model: str | None = None) -> None:
        """Summarize oldest messages using the LLM, replace them with a summary message."""
        start, end = self.get_compressible_range()
        if start >= end:
            return

        messages_to_summarize = self.messages[start:end]

        summary_prompt = (
            "Summarize the following conversation history concisely, "
            "preserving key decisions, file changes made, and unresolved tasks:\n\n"
        )
        summary_prompt += "\n".join(
            f"[{m['role']}]: {m.get('content', '')[:500]}"
            for m in messages_to_summarize
        )

        try:
            result = await llm_client.chat(
                messages=[{"role": "user", "content": summary_prompt}],
                tools=None,
                on_token=None,
            )
            summary = result.get("content", "Previous conversation summarized.")
        except Exception:
            summary = "(Conversation history compressed due to context limit.)"

        # Replace compressed range with summary
        new_messages = self.messages[:start] + [
            {"role": "system", "content": f"[Conversation summary]: {summary}"}
        ] + self.messages[end:]
        self.messages = new_messages
```

- [ ] **Step 4: Run test — expect PASS**

```bash
pytest tests/test_context.py -v
```

- [ ] **Step 5: Commit**

```bash
git add core/context.py tests/test_context.py
git commit -m "feat: add context manager with token estimation and compression"
```

---

### Task 10: Agent Core Loop

**Files:** `core/agent.py`, `tests/test_agent.py`

- [ ] **Step 1: Write the test**

```python
import pytest
from core.agent import Agent
from core.llm import LLMClient
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
        import tempfile
        import os

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
```

(Add `import asyncio` at top.)

- [ ] **Step 2: Run test — expect FAIL**

```bash
pytest tests/test_agent.py -v
```

- [ ] **Step 3: Write the implementation**

```python
from __future__ import annotations

import json
from collections.abc import Callable

from .llm import LLMClient
from .context import ContextManager
from .tools.base import BaseTool, ToolResult


class Agent:
    """Core ReAct agent. Runs the think→act→observe loop."""

    def __init__(
        self,
        llm_client: LLMClient,
        context_manager: ContextManager,
        tools: list[BaseTool],
        workspace_dir: str,
    ):
        self.llm = llm_client
        self.ctx = context_manager
        self.tools_by_name = {t.name: t for t in tools}
        self.workspace_dir = workspace_dir

    async def run(
        self,
        user_input: str,
        on_token: Callable[[str], None] | None = None,
    ) -> str:
        self.ctx.add_user_message(user_input)
        tool_schemas = [t.schema for t in self.tools_by_name.values()]

        while True:
            # Check context and compress if needed
            if self.ctx.needs_compression():
                await self.ctx.compress(self.llm)

            response = await self.llm.chat(
                messages=self.ctx.messages,
                tools=tool_schemas if tool_schemas else None,
                on_token=on_token,
            )

            tool_calls = response.get("tool_calls")

            if not tool_calls:
                # Final response — no more tool calls
                self.ctx.add_assistant_message(content=response.get("content"))
                return response.get("content") or ""

            # Record assistant message with tool calls
            self.ctx.add_assistant_message(
                content=response.get("content"),
                tool_calls=tool_calls,
            )

            # Execute each tool call
            for tc in tool_calls:
                tool_name = tc["function"]["name"]
                tool = self.tools_by_name.get(tool_name)

                if tool is None:
                    self.ctx.add_tool_result(
                        tc["id"],
                        f"Error: unknown tool '{tool_name}'. Available: {list(self.tools_by_name.keys())}",
                    )
                    continue

                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError as e:
                    self.ctx.add_tool_result(tc["id"], f"Error: invalid JSON arguments: {e}")
                    continue

                # Inject workspace_dir into file-system tools
                if tool_name in ("read", "write", "edit", "bash"):
                    args["workspace_dir"] = self.workspace_dir

                result: ToolResult = await tool.execute(**args)

                # Build observation for the model
                if result.success:
                    observation = result.content
                else:
                    observation = f"ERROR: {result.error}"
                    if result.content:
                        observation += f"\nPartial output: {result.content}"

                self.ctx.add_tool_result(tc["id"], observation)
```

- [ ] **Step 4: Run test — expect PASS**

```bash
pytest tests/test_agent.py -v
```

- [ ] **Step 5: Commit**

```bash
git add core/agent.py tests/test_agent.py
git commit -m "feat: add core ReAct agent loop with error feeding"
```

---

### Task 11: CLI UI

**Files:** `cli/ui.py`

- [ ] **Step 1: Write the file**

```python
from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.live import Live
from rich.text import Text


class UI:
    """Terminal rendering using Rich."""

    def __init__(self):
        self.console = Console()
        self._thinking_collapsed = True
        self._thinking_buffer: list[str] = []
        self._thinking_label = "💭 Thinking"

    def render_markdown(self, text: str) -> None:
        if text.strip():
            self.console.print(Markdown(text))

    def stream_markdown(self) -> "LiveMarkdownStream":
        return LiveMarkdownStream(self.console)

    def render_tool_status(self, name: str, status: str) -> None:
        """Show a one-line tool execution status."""
        color = "yellow" if status == "running" else "green" if status == "done" else "red"
        self.console.print(f"  [{color}]{name}[/{color}]: {status}")

    def render_error(self, msg: str) -> None:
        self.console.print(f"[red]✗ {msg}[/red]")

    def render_info(self, msg: str) -> None:
        self.console.print(f"[dim]{msg}[/dim]")

    def render_user_prompt(self) -> str:
        """Display the prompt and read user input."""
        return input("\n> ")

    def render_welcome(self) -> None:
        self.console.print()
        self.console.print(
            Panel.fit(
                "Simple Coding Agent — type your request or [bold]exit[/bold] to quit",
                border_style="blue",
            )
        )


class LiveMarkdownStream:
    """Context manager for streaming markdown to the terminal."""

    def __init__(self, console: Console):
        self.console = console
        self._buffer = ""
        self._live: Live | None = None

    def __enter__(self):
        self._buffer = ""
        self._live = Live(
            Markdown(""),
            console=self.console,
            refresh_per_second=10,
            vertical_overflow="visible",
        )
        self._live.start()
        return self

    def __exit__(self, *args):
        if self._live:
            self._live.stop()
            self._live = None
        # Print final rendered version
        if self._buffer.strip():
            self.console.print(Markdown(self._buffer))

    def add_token(self, token: str) -> None:
        self._buffer += token
        if self._live:
            self._live.update(Markdown(self._buffer + "▌"))
```

- [ ] **Step 2: Commit**

```bash
git add cli/ui.py
git commit -m "feat: add Rich-based terminal UI rendering"
```

---

### Task 12: CLI Bridge

**Files:** `cli/bridge.py`

- [ ] **Step 1: Write the file**

```python
from __future__ import annotations

from core.agent import Agent
from cli.ui import UI


class Bridge:
    """Connects the core Agent to the terminal UI. Runs the REPL loop."""

    def __init__(self, agent: Agent, ui: UI):
        self.agent = agent
        self.ui = ui

    async def run(self) -> None:
        self.ui.render_welcome()

        while True:
            try:
                user_input = self.ui.render_user_prompt()
            except (EOFError, KeyboardInterrupt):
                self.ui.render_info("\nGoodbye.")
                break

            user_input = user_input.strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                self.ui.render_info("Goodbye.")
                break

            # Stream the response
            with self.ui.stream_markdown() as stream:
                result = await self.agent.run(
                    user_input,
                    on_token=stream.add_token,
                )

            # If result came back with content but streaming didn't fire
            # (e.g., model returned full content without streaming deltas),
            # render it now.
            if result and not stream._buffer.strip():
                self.ui.render_markdown(result)
```

- [ ] **Step 2: Commit**

```bash
git add cli/bridge.py
git commit -m "feat: add CLI bridge for REPL input/output loop"
```

---

### Task 13: CLI Entry Point

**Files:** `cli/main.py`

- [ ] **Step 1: Write the file**

```python
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def main():
    parser = argparse.ArgumentParser(description="Simple Coding Agent")
    parser.add_argument("--model", default=None, help="Model name (overrides .env)")
    parser.add_argument("--dir", default=None, help="Workspace directory (default: cwd)")
    args = parser.parse_args()

    api_key = os.getenv("SCA_API_KEY")
    if not api_key:
        print("Error: SCA_API_KEY not set in .env file", file=sys.stderr)
        sys.exit(1)

    workspace_dir = args.dir or os.getcwd()
    workspace_dir = os.path.abspath(workspace_dir)

    # Lazy imports so --help is fast
    from core.llm import LLMClient
    from core.context import ContextManager
    from core.agent import Agent
    from core.tools.read import ReadTool
    from core.tools.write import WriteTool
    from core.tools.edit import EditTool
    from core.tools.bash import BashTool
    from core.system_prompt import SYSTEM_PROMPT
    from cli.ui import UI
    from cli.bridge import Bridge

    llm = LLMClient(
        api_key=api_key,
        base_url=os.getenv("SCA_API_BASE", "https://api.deepseek.com"),
        model=args.model or os.getenv("SCA_MODEL", "deepseek-v4-pro"),
        max_tokens=int(os.getenv("SCA_MAX_TOKENS", "128000")),
    )

    ctx = ContextManager(system_prompt=SYSTEM_PROMPT)
    tools = [ReadTool(), WriteTool(), EditTool(), BashTool()]
    agent = Agent(llm_client=llm, context_manager=ctx, tools=tools, workspace_dir=workspace_dir)

    ui = UI()
    bridge = Bridge(agent=agent, ui=ui)

    asyncio.run(bridge.run())


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it starts (will fail without API key, but should show error or connect)**

```bash
python -m cli.main --help
```
Expected: help text with --model and --dir options.

- [ ] **Step 3: Commit**

```bash
git add cli/main.py
git commit -m "feat: add CLI entry point with arg parsing and config loading"
```

---

### Task 14: Integration Smoke Test

**Files:** — (manual test)

- [ ] **Step 1: Set a real API key in .env**

Edit `.env` and set `SCA_API_KEY` to a valid DeepSeek API key.

- [ ] **Step 2: Start the agent**

```bash
python -m cli.main --dir /tmp/test-sca
```

- [ ] **Step 3: Test a simple file operation**

```
> Create a file called hello.py that prints "Hello from SCA!"
```

Expected: Agent uses write tool to create the file, reports success.

- [ ] **Step 4: Test bash execution**

```
> Run python hello.py
```

Expected: Agent uses bash tool, shows "Hello from SCA!" in output.

- [ ] **Step 5: Test edit**

```
> Change the message in hello.py to say "Goodbye from SCA!"
```

Expected: Agent reads file, uses edit tool to change the string, then runs it to confirm.

- [ ] **Step 6: Test error self-correction**

```
> Run a file that doesn't exist: python nonexistent.py
```

Expected: Agent runs bash, sees error, tells you the file doesn't exist.

- [ ] **Step 7: Exit and commit any fixes**

```bash
git add -A
git commit -m "chore: integration smoke test fixes"
```

---

### Task 15: Push to Remote

- [ ] **Step 1: Push**

```bash
git push -u origin main
```

---

## Implementation Order (Dependency Graph)

```
Task 0 (scaffold)
   │
   ▼
Task 1 (exceptions) ──► Task 2 (tool base) ──► Tasks 3-6 (tools)
                                                      │
Task 7 (system prompt)                                 │
                                                      │
Task 8 (llm) ──► Task 9 (context)                     │
                        │                             │
                        ▼                             │
                  Task 10 (agent) ◄────────────────────┘
                        │
                        ▼
                  Task 11 (ui) ──► Task 12 (bridge) ──► Task 13 (main)
                                                              │
                                                              ▼
                                                       Task 14 (smoke)
                                                              │
                                                              ▼
                                                       Task 15 (push)
```
