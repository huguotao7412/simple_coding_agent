# Agent 四大增强能力 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enhance SCA with dynamic environment detection, output truncation + unified diff, circuit breaking, and hierarchical memory preservation — four independent capabilities.

**Architecture:** Four standalone phases touching 7 existing files in `core/`. No new files created. Each phase adds self-contained logic: Phase 1 (env injection at init), Phase 2 (tool output formatting), Phase 3 (loop detection in run), Phase 4 (scratchpad extraction in compress).

**Tech Stack:** Python 3.13+, stdlib only (`subprocess`, `os`, `platform`, `sys`, `difflib`, `re`, `hashlib`, `collections.deque`)

---

## File Structure

| File | Phase | Responsibility |
|---|---|---|
| `core/agent.py` | 1, 3 | Env detection functions + system prompt assembly + circuit breaking |
| `core/system_prompt.py` | 4 | Add scratchpad instruction to static prompt |
| `core/tools/base.py` | 2 | `truncate_long_output()` shared utility |
| `core/tools/bash.py` | 2 | Apply truncation to stdout |
| `core/tools/read.py` | 2 | Apply truncation to file output |
| `core/tools/edit.py` | 2 | Return Unified Diff via `difflib` |
| `core/context.py` | 4 | Extract scratchpad + restructure `compress()` |

---

### Task 1: Phase 1 — Add `get_workspace_tree()` and `get_runtime_env()` to `core/agent.py`

**Files:**
- Modify: `core/agent.py`

- [ ] **Step 1: Add imports and `get_workspace_tree()` function**

Add at the top of `core/agent.py`, after the existing `from collections.abc import ...` line:

```python
import os
import platform
import subprocess
import sys
```

Add after the `AgentEvent` dataclass and before the `Agent` class:

```python
def _walk_tree_pure_python(workspace_dir: str, max_depth: int = 2) -> str:
    """Fallback: generate tree-like directory listing using os.scandir()."""
    import stat as _stat

    ignore_dirs = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"}

    def _walk(dirpath: str, prefix: str = "", depth: int = 0) -> list[str]:
        if depth >= max_depth:
            return []
        lines: list[str] = []
        try:
            entries = sorted(os.scandir(dirpath), key=lambda e: e.name.lower())
        except (PermissionError, OSError):
            return lines
        dirs = [e for e in entries if e.is_dir(follow_symlinks=False) and e.name not in ignore_dirs]
        files = [e for e in entries if e.is_file(follow_symlinks=False)]
        items = dirs + files
        for i, entry in enumerate(items):
            is_last = i == len(items) - 1
            connector = "└── " if is_last else "├── "
            next_prefix = prefix + ("    " if is_last else "│   ")
            if entry.is_dir(follow_symlinks=False):
                lines.append(f"{prefix}{connector}{entry.name}/")
                lines.extend(_walk(entry.path, next_prefix, depth + 1))
            else:
                lines.append(f"{prefix}{connector}{entry.name}")
        return lines

    root_name = os.path.basename(workspace_dir) or workspace_dir
    lines = [root_name + "/"]
    lines.extend(_walk(workspace_dir))
    return "\n".join(lines)


def get_workspace_tree(workspace_dir: str) -> str:
    """Get directory structure of workspace. Tries `tree` command first, falls back to pure Python."""
    try:
        result = subprocess.run(
            [
                "tree", "-L", "2", "-I",
                ".git|__pycache__|.venv|node_modules|.mypy_cache|.pytest_cache",
                workspace_dir,
            ],
            capture_output=True,
            timeout=10,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return _walk_tree_pure_python(workspace_dir)


def get_runtime_env() -> str:
    """Get OS info and exact Python version using stdlib only."""
    lines = [
        f"Operating System: {platform.system()} {platform.release()} ({platform.machine()})",
        f"Python Version: {sys.version}",
    ]
    return "\n".join(lines)
```

- [ ] **Step 2: Verify the functions work in isolation**

Run:

```bash
cd F:/yan/python/2itemst/simple_coding_agent && python -c "
from core.agent import get_workspace_tree, get_runtime_env
print('=== WORKSPACE TREE ===')
print(get_workspace_tree('.'))
print()
print('=== RUNTIME ENV ===')
print(get_runtime_env())
"
```

Expected: tree output of the project directory (2 levels deep, ignoring .git/.venv/__pycache__), followed by OS name and Python version like `3.13.x (...)`.

- [ ] **Step 3: Modify `Agent.__init__` to assemble dynamic system prompt**

Replace the existing `Agent.__init__` (lines 26-36) with:

```python
def __init__(
    self,
    llm_client: LLMClient,
    context_manager: ContextManager,
    tools: list[BaseTool],
    workspace_dir: str,
):
    self.llm = llm_client
    self.tools_by_name = {t.name: t for t in tools}
    self.workspace_dir = workspace_dir

    # Build dynamic system prompt with environment context
    from .system_prompt import SYSTEM_PROMPT

    workspace_tree = get_workspace_tree(workspace_dir)
    runtime_env = get_runtime_env()
    dynamic_prompt = (
        SYSTEM_PROMPT
        + f"\n\n<workspace_context>\n{workspace_tree}\n</workspace_context>"
        + f"\n\n<environment_context>\n{runtime_env}\n</environment_context>"
    )
    # Override context_manager's system prompt with our dynamic version
    context_manager.messages[0] = {"role": "system", "content": dynamic_prompt}
    self.ctx = context_manager
```

- [ ] **Step 4: Verify Agent init produces correct system prompt**

Run:

```bash
cd F:/yan/python/2itemst/simple_coding_agent && python -c "
from core.llm import LLMClient
from core.context import ContextManager
from core.system_prompt import SYSTEM_PROMPT
from core.agent import Agent

# Use dummy values — we only care about init
class DummyLLM:
    pass

ctx = ContextManager(system_prompt=SYSTEM_PROMPT, max_tokens=8000)
agent = Agent(DummyLLM(), ctx, [], '.')
msg = agent.ctx.messages[0]['content']
assert '<workspace_context>' in msg, 'Missing workspace_context tag'
assert '</workspace_context>' in msg, 'Missing closing tag'
assert '<environment_context>' in msg, 'Missing environment_context tag'
assert '</environment_context>' in msg, 'Missing closing tag'
assert 'Python Version:' in msg, 'Missing python version'
print('OK: Dynamic prompt assembled correctly')
print()
print(msg)
"
```

Expected: `OK: Dynamic prompt assembled correctly` followed by the full system prompt with workspace and environment blocks.

- [ ] **Step 5: Commit**

```bash
git add core/agent.py
git commit -m "feat: add dynamic environment detection and system prompt injection (Phase 1)"
```

---

### Task 2: Phase 2 — Add `truncate_long_output()` to `core/tools/base.py`

**Files:**
- Modify: `core/tools/base.py`

- [ ] **Step 1: Add truncation function**

Add after the `BaseTool` class (end of file):

```python
TRUNCATION_THRESHOLD = 8000


def truncate_long_output(text: str, threshold: int = TRUNCATION_THRESHOLD) -> str:
    """Truncate long text, keeping first 20% and last 30% of threshold chars.

    Inserts a visible marker so the LLM knows content was omitted.
    """
    if len(text) <= threshold:
        return text

    keep_head = int(threshold * 0.2)
    keep_tail = int(threshold * 0.3)
    omitted = len(text) - keep_head - keep_tail

    head = text[:keep_head]
    tail = text[-keep_tail:]
    return (
        head
        + f"\n... [Output truncated: {omitted} chars omitted for brevity] ...\n"
        + tail
    )
```

- [ ] **Step 2: Verify truncation logic**

Run:

```bash
cd F:/yan/python/2itemst/simple_coding_agent && python -c "
from core.tools.base import truncate_long_output

# Short text: no change
short = 'hello world'
assert truncate_long_output(short) == short, 'Short text should not be truncated'

# Long text: should be truncated with marker
long_text = 'x' * 10000
result = truncate_long_output(long_text)
assert len(result) < len(long_text), 'Long text should be truncated'
assert 'Output truncated' in result, 'Should contain truncation marker'
assert 'chars omitted for brevity' in result, 'Should contain brevity note'
print('OK: truncation works')
print(f'  Short: {len(short)} chars -> {len(truncate_long_output(short))} chars')
print(f'  Long: {len(long_text)} chars -> {len(result)} chars')
"
```

Expected: `OK: truncation works` with length statistics.

- [ ] **Step 3: Commit**

```bash
git add core/tools/base.py
git commit -m "feat: add truncate_long_output() utility for tool output compression (Phase 2a)"
```

---

### Task 3: Phase 2 — Apply truncation to `bash.py` and `read.py`

**Files:**
- Modify: `core/tools/bash.py`
- Modify: `core/tools/read.py`

- [ ] **Step 1: Apply truncation in `bash.py`**

Edit `core/tools/bash.py`: add import and wrap the stdout success path.

After the existing `from .base import BaseTool, ToolResult` line, change to:

```python
from .base import BaseTool, ToolResult, truncate_long_output
```

In the `execute` method, change the success return (line 57):

```python
# Before:
return ToolResult.ok(stdout_str or "(no output)")

# After:
return ToolResult.ok(truncate_long_output(stdout_str or "(no output)"))
```

- [ ] **Step 2: Apply truncation in `read.py`**

Edit `core/tools/read.py`: add import and wrap output.

After the existing `from .base import BaseTool, ToolResult` line, change to:

```python
from .base import BaseTool, ToolResult, truncate_long_output
```

In the `execute` method, change the return (line 31):

```python
# Before:
return ToolResult.ok(output)

# After:
return ToolResult.ok(truncate_long_output(output))
```

- [ ] **Step 3: Verify truncation in bash and read tools**

Run:

```bash
cd F:/yan/python/2itemst/simple_coding_agent && python -c "
import asyncio
from core.tools.bash import BashTool
from core.tools.read import ReadTool

async def test():
    # Bash: generate a lot of output
    bash = BashTool()
    # Windows-compatible command to generate many lines
    result = await bash.execute(command='python -c \"for i in range(500): print(i)\"')
    print('=== BASH RESULT (head 200 chars) ===')
    print(result.content[:200])
    print(f'Total length: {len(result.content)}')
    print()

    # Read a file larger than threshold
    read = ReadTool()
    result = await read.execute(file_path='core/agent.py', workspace_dir='.')
    print('=== READ RESULT (head 200 chars) ===')
    print(result.content[:200])

asyncio.run(test())
"
```

Expected: bash output of 500 lines (well over 8000 chars) is truncated with the marker; agent.py should be under threshold so returned in full.

- [ ] **Step 4: Commit**

```bash
git add core/tools/bash.py core/tools/read.py
git commit -m "feat: apply output truncation to bash and read tools (Phase 2b)"
```

---

### Task 4: Phase 2 — Add Unified Diff to `edit.py`

**Files:**
- Modify: `core/tools/edit.py`

- [ ] **Step 1: Add import**

Add after the existing `import os` line:

```python
import difflib
```

- [ ] **Step 2: Replace exact match return (line 51-55)**

Change the exact match success block from:

```python
        if count == 1:
            new_content = content.replace(search_block, replace_block, 1)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            return ToolResult.ok(f"Exact match replaced in {file_path}")
```

To:

```python
        if count == 1:
            new_content = content.replace(search_block, replace_block, 1)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            diff = difflib.unified_diff(
                content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=file_path,
                tofile=file_path,
            )
            diff_text = "".join(diff)
            return ToolResult.ok(diff_text if diff_text else "No changes made.")
```

- [ ] **Step 3: Replace fuzzy match return (lines 120-127)**

Change the fuzzy match success block (the `new_content = ...` + `with open` + `return` triplet) from:

```python
        new_content = "".join(new_lines)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        return ToolResult.ok(
            f"Fuzzy match replaced lines [{start_idx}:{end_idx}] in {file_path}"
        )
```

To:

```python
        new_content = "".join(new_lines)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        diff = difflib.unified_diff(
            content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=file_path,
            tofile=file_path,
        )
        diff_text = "".join(diff)
        return ToolResult.ok(diff_text if diff_text else "No changes made.")
```

- [ ] **Step 4: Verify Unified Diff output**

Run:

```bash
cd F:/yan/python/2itemst/simple_coding_agent && python -c "
import asyncio
from core.tools.edit import EditTool

async def test():
    # Create a temp file to test edit
    import tempfile, os
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, dir='.') as f:
        f.write('def hello():\n    print(\"hello\")\n    return 42\n')
        tmp_path = f.name

    edit = EditTool()
    result = await edit.execute(
        file_path=tmp_path,
        search_block='    print(\"hello\")\n    return 42',
        replace_block='    print(\"hello world\")\n    return 43',
        workspace_dir='.',
    )
    print('=== EDIT DIFF ===')
    print(result.content)
    print()
    # Verify diff format
    assert result.success, f'Edit should succeed: {result.error}'
    assert '@@' in result.content, 'Diff should contain @@ hunk header'
    assert '+' in result.content, 'Diff should contain added line'
    assert '-' in result.content, 'Diff should contain removed line'
    print('OK: Unified Diff format confirmed')
    os.unlink(tmp_path)

asyncio.run(test())
"
```

Expected: `OK: Unified Diff format confirmed` with a proper unified diff showing `-print("hello")` and `+print("hello world")`.

- [ ] **Step 5: Commit**

```bash
git add core/tools/edit.py
git commit -m "feat: return unified diff from edit tool instead of plain text (Phase 2c)"
```

---

### Task 5: Phase 3 — Add circuit breaking to `core/agent.py`

**Files:**
- Modify: `core/agent.py`

- [ ] **Step 1: Add `action_history` to `Agent.__init__`**

Add import at top (after `from collections.abc import ...`):

```python
from collections import deque
```

In `Agent.__init__`, add after `self.workspace_dir = workspace_dir`:

```python
        # Circuit breaker: track recent tool calls to detect loops
        self.action_history: deque[int] = deque(maxlen=5)
```

- [ ] **Step 2: Add `_hash_action()` and `detect_loop()` methods**

Add these methods to the `Agent` class, before `run()`:

```python
    def _hash_action(self, tool_name: str, args: dict) -> int:
        """Create a deterministic hash for a tool_name + args combination."""
        return hash(tool_name + json.dumps(args, sort_keys=True))

    def detect_loop(self, action_hash: int) -> bool:
        """Return True if action_hash appears >= 2 times in recent history."""
        return sum(1 for h in self.action_history if h == action_hash) >= 2
```

- [ ] **Step 3: Insert circuit breaker into `run()` method**

In the `run()` method, inside the `for tc in tool_calls:` loop, add after line 79 (`if tool is None:` check) and the args parsing (lines 86-89), and before the workspace_dir injection (line 93-94):

```python
                # --- Circuit breaker: detect repeated failed tool calls ---
                action_hash = self._hash_action(tool_name, args)
                if self.detect_loop(action_hash):
                    intervention = (
                        "System Alert: Detected repeated failed tool calls. "
                        "STOP current action. Please reason about why it failed "
                        "and use read or search codebase to gather new context."
                    )
                    self.ctx.add_tool_result(tc["id"], intervention)
                    self.action_history.append(action_hash)
                    continue
                # --- End circuit breaker ---
```

Then after the successful tool execution (after line 106 `self.ctx.add_tool_result(tc["id"], observation)`), add:

```python
                self.action_history.append(action_hash)
```

- [ ] **Step 4: Insert circuit breaker into `run_stream()` method**

Apply the same pattern in `run_stream()`. Inside the `for tc in tool_calls:` loop, after the json.JSONDecodeError handling (line 167), insert the same circuit breaker block before the workspace_dir injection:

```python
                    # --- Circuit breaker: detect repeated failed tool calls ---
                    action_hash = self._hash_action(tool_name, args)
                    if self.detect_loop(action_hash):
                        intervention = (
                            "System Alert: Detected repeated failed tool calls. "
                            "STOP current action. Please reason about why it failed "
                            "and use read or search codebase to gather new context."
                        )
                        self.ctx.add_tool_result(tc["id"], intervention)
                        self.action_history.append(action_hash)
                        yield AgentEvent(
                            type="tool_result",
                            tool_name=tool_name,
                            tool_result=ToolResult.fail("circuit breaker: repeated action"),
                        )
                        continue
                    # --- End circuit breaker ---
```

Then after the `self.ctx.add_tool_result(tc["id"], observation)` line (line 183), add:

```python
                    self.action_history.append(action_hash)
```

- [ ] **Step 5: Verify circuit breaker**

Run:

```bash
cd F:/yan/python/2itemst/simple_coding_agent && python -c "
from collections import deque
from core.agent import Agent

# Create a minimal agent to test the detection logic
class DummyLLM:
    pass

from core.context import ContextManager
from core.system_prompt import SYSTEM_PROMPT

ctx = ContextManager(system_prompt=SYSTEM_PROMPT, max_tokens=8000)
agent = Agent(DummyLLM(), ctx, [], '.')

# Simulate: same action 3 times should trigger detect_loop
h1 = agent._hash_action('read', {'file_path': '/x', 'workspace_dir': '.'})
agent.action_history.append(h1)
agent.action_history.append(h1)
assert agent.detect_loop(h1) == True, 'Same action 2x in history should detect loop'

# Different action should NOT trigger
h2 = agent._hash_action('bash', {'command': 'ls'})
agent.action_history.append(h2)
assert agent.detect_loop(h2) == False, 'New action should not trigger loop'
assert agent.detect_loop(h1) == True, 'Old repeated action still detected'

print('OK: Circuit breaker logic works')
print(f'  History size: {len(agent.action_history)} (maxlen={agent.action_history.maxlen})')
"
```

Expected: `OK: Circuit breaker logic works`.

- [ ] **Step 6: Commit**

```bash
git add core/agent.py
git commit -m "feat: add circuit breaking with action hash detection (Phase 3)"
```

---

### Task 6: Phase 4 — Add scratchpad instruction to system prompt

**Files:**
- Modify: `core/system_prompt.py`

- [ ] **Step 1: Append scratchpad instruction**

Append to the `SYSTEM_PROMPT` string (before the closing `"""`):

```python

## Scratchpad (Engineering Ledger)
Before making file edits or executing terminal commands, maintain a scratchpad block in your response. This block is preserved during context compression and serves as your working memory:

```xml
<scratchpad>
  <completed_tasks>
    - Task you have finished
  </completed_tasks>
  <current_bugs>
    - Bug you are investigating and what you've tried
  </current_bugs>
  <key_files_in_focus>
    - /absolute/path/to/key/file.py
  </key_files_in_focus>
</scratchpad>
```

Update this block at the end of each response. Be concise — only list active items, not everything from the entire conversation.
```

- [ ] **Step 2: Verify the prompt loads correctly**

Run:

```bash
cd F:/yan/python/2itemst/simple_coding_agent && python -c "
from core.system_prompt import SYSTEM_PROMPT
assert '<scratchpad>' in SYSTEM_PROMPT, 'Missing scratchpad opening tag'
assert '</scratchpad>' in SYSTEM_PROMPT, 'Missing scratchpad closing tag'
assert 'completed_tasks' in SYSTEM_PROMPT, 'Missing completed_tasks field'
assert 'current_bugs' in SYSTEM_PROMPT, 'Missing current_bugs field'
assert 'key_files_in_focus' in SYSTEM_PROMPT, 'Missing key_files_in_focus field'
print('OK: Scratchpad instruction in system prompt')
"
```

Expected: `OK: Scratchpad instruction in system prompt`.

- [ ] **Step 3: Commit**

```bash
git add core/system_prompt.py
git commit -m "feat: add scratchpad instruction to system prompt (Phase 4a)"
```

---

### Task 7: Phase 4 — Refactor `compress()` with scratchpad preservation

**Files:**
- Modify: `core/context.py`

- [ ] **Step 1: Add regex and extraction method**

Add `import re` at top of `core/context.py` (after `from __future__ import annotations`).

Add `_extract_last_scratchpad()` static method to `ContextManager`:

```python
    _SCRATCHPAD_RE = re.compile(r"<scratchpad>.*?</scratchpad>", re.DOTALL)

    @classmethod
    def _extract_last_scratchpad(cls, messages: list[dict]) -> str | None:
        """Extract the last scratchpad block from a list of messages.

        Scans in reverse order to find the most recent scratchpad.
        Returns the full XML block string, or None if not found.
        """
        for msg in reversed(messages):
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            matches = list(cls._SCRATCHPAD_RE.finditer(content))
            if matches:
                return matches[-1].group(0)
        return None
```

Add the method inside the `ContextManager` class, before the `add_user_message` method.

- [ ] **Step 2: Refactor `compress()` to preserve scratchpad**

Replace the existing `compress()` method (lines 80-110) with:

```python
    async def compress(self, llm_client, compression_model: str | None = None) -> None:
        """Summarize oldest messages using the LLM, preserving scratchpad if present."""
        start, end = self.get_compressible_range()
        if start >= end:
            return

        messages_to_summarize = self.messages[start:end]

        # --- Extract latest scratchpad before compression ---
        saved_scratchpad = self._extract_last_scratchpad(messages_to_summarize)

        # --- Existing summary logic ---
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

        # --- Reassemble: system prompt -> scratchpad (if found) -> summary -> recent ---
        tail = self.messages[end:]
        new_messages = self.messages[:start]  # Keep system prompt

        if saved_scratchpad:
            new_messages.append({
                "role": "system",
                "content": f"[Engineering Scratchpad]:\n{saved_scratchpad}",
            })

        new_messages.append({
            "role": "system",
            "content": f"[Conversation summary]: {summary}",
        })
        new_messages.extend(tail)
        self.messages = new_messages
```

- [ ] **Step 3: Verify scratchpad extraction**

Run:

```bash
cd F:/yan/python/2itemst/simple_coding_agent && python -c "
from core.context import ContextManager

# Test extraction with scratchpad in last message
msgs = [
    {'role': 'user', 'content': 'hello'},
    {'role': 'assistant', 'content': 'Hi! <scratchpad>\n  <completed_tasks>\n    - done\n  </completed_tasks>\n</scratchpad>'},
]
result = ContextManager._extract_last_scratchpad(msgs)
assert result is not None, 'Should find scratchpad'
assert 'completed_tasks' in result, 'Should contain completed_tasks'
assert '<scratchpad>' in result, 'Should contain opening tag'
print('OK: Found scratchpad in last message')

# No scratchpad
msgs2 = [{'role': 'user', 'content': 'no scratchpad here'}]
result2 = ContextManager._extract_last_scratchpad(msgs2)
assert result2 is None, 'Should return None when no scratchpad'
print('OK: None returned when scratchpad absent')

# Takes the LAST scratchpad when multiple exist
msgs3 = [
    {'role': 'assistant', 'content': '<scratchpad>first</scratchpad> some text <scratchpad>second</scratchpad>'},
]
result3 = ContextManager._extract_last_scratchpad(msgs3)
assert result3 == '<scratchpad>second</scratchpad>', f'Should find last scratchpad, got: {result3}'
print('OK: Finds last scratchpad when multiple exist')
"
```

Expected: Three `OK` lines confirming extraction behavior.

- [ ] **Step 4: Commit**

```bash
git add core/context.py
git commit -m "feat: preserve scratchpad during context compression (Phase 4b)"
```

---

### Task 8: End-to-end smoke test

**Files:**
- No file changes. Verification only.

- [ ] **Step 1: Run a full import + init check**

```bash
cd F:/yan/python/2itemst/simple_coding_agent && python -c "
from core.system_prompt import SYSTEM_PROMPT
from core.context import ContextManager
from core.tools.base import BaseTool, ToolResult, truncate_long_output
from core.tools.bash import BashTool
from core.tools.read import ReadTool
from core.tools.edit import EditTool
from core.agent import Agent, AgentEvent, get_workspace_tree, get_runtime_env

# Create a full Agent instance
ctx = ContextManager(system_prompt=SYSTEM_PROMPT, max_tokens=8000)

class DummyLLM:
    pass

agent = Agent(DummyLLM(), ctx, [
    BashTool(),
    ReadTool(),
    EditTool(),
], '.')

# Check that system prompt was assembled with dynamic context
msg = agent.ctx.messages[0]['content']
assert '<workspace_context>' in msg
assert '<environment_context>' in msg
assert hasattr(agent, 'action_history')
assert len(agent.tools_by_name) == 3

print('OK: Full agent init successful')
print(f'  Tools: {sorted(agent.tools_by_name.keys())}')
print(f'  Action history maxlen: {agent.action_history.maxlen}')
"
```

Expected: `OK: Full agent init successful` with tools list and maxlen=5.

- [ ] **Step 2: Commit (if any fixes needed)**

If all passes, no commit needed. If any fixes were made:

```bash
git add -A
git commit -m "fix: end-to-end integration fixes"
```

---

## Verification Checklist

After all tasks complete, verify each phase independently:

- [ ] **Phase 1**: `python -c "from core.agent import get_workspace_tree, get_runtime_env; print(get_workspace_tree('.')); print(get_runtime_env())"`
- [ ] **Phase 2a**: `python -c "from core.tools.base import truncate_long_output; print(truncate_long_output('x'*10000))"` — should show truncated output with marker
- [ ] **Phase 2b**: Bash + Read both apply truncation (tested in Task 3)
- [ ] **Phase 2c**: Edit returns unified diff with `@@`, `+`, `-` markers (tested in Task 4)
- [ ] **Phase 3**: `python -c "from core.agent import Agent; ..."` — `detect_loop()` returns True for repeated hash (tested in Task 5)
- [ ] **Phase 4**: `python -c "from core.context import ContextManager; ..."` — scratchpad extracted and preserved (tested in Tasks 6-7)
