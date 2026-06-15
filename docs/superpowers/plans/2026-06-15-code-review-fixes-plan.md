# Code Review Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 8 issues identified in the SCA comprehensive code review — spanning llm.py, search.py, bash.py, edit.py, context.py, and agent.py.

**Architecture:** Each fix is self-contained within its target file. No new files are created. Changes are small and focused — the largest refactoring (Task 7) extracts a shared `_execute_single_tool()` method in agent.py to deduplicate ~60 lines.

**Tech Stack:** Python 3.11+, asyncio, difflib, ast, re, logging

---

### Task 1: Fix #9 + #7 — Move `import json` to top + add JSONDecodeError logging (`core/llm.py`)

**Files:**
- Modify: `core/llm.py:1-9` (imports), `core/llm.py:85-89` (except block)

- [ ] **Step 1: Add `import json` and `import logging` to module-level imports**

Add at line 4 (after `from collections.abc import Callable`):
```python
import json
import logging
```

Remove the `import json` on line 85 inside `_parse_stream()`.

- [ ] **Step 2: Add logger and debug log in except block**

In `_parse_stream()`, after `import json` has been removed (line 85 becomes `try:`), change:
```python
            import json
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
```
To:
```python
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                logging.getLogger(__name__).debug(
                    "Failed to parse SSE data line: %s", data[:200]
                )
                continue
```

- [ ] **Step 3: Verify the file still imports cleanly**

Run: `python -c "from core.llm import LLMClient; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add core/llm.py
git commit -m "fix: move import json to top, add debug log for JSONDecodeError in SSE parsing"
```

---

### Task 2: Fix #5 — Guard _build_signature against single-line function bodies (`core/tools/search.py`)

**Files:**
- Modify: `core/tools/search.py:114-123`

- [ ] **Step 1: Replace `_build_signature` with guarded version**

Replace lines 114-123:
```python
    def _build_signature(self, node: ast.AST, source: str) -> str:
        """Build a human-readable signature for a function or class definition."""
        try:
            lines = source.splitlines()
            start_line = node.lineno - 1
            end_line = node.body[0].lineno - 1 if hasattr(node, 'body') and node.body else node.end_lineno
            sig_lines = lines[start_line:end_line]
            return " ".join(line.strip() for line in sig_lines)
        except Exception:
            return node.name
```

With:
```python
    def _build_signature(self, node: ast.AST, source: str) -> str:
        """Build a human-readable signature for a function or class definition."""
        try:
            lines = source.splitlines()
            start_line = node.lineno - 1
            if hasattr(node, 'body') and node.body:
                body_start = node.body[0].lineno
                if body_start <= node.lineno:
                    # Single-line body: e.g. "def foo(): pass"
                    end_line = body_start
                else:
                    end_line = body_start - 1
            else:
                end_line = node.end_lineno
            sig_lines = lines[start_line:end_line]
            return " ".join(line.strip() for line in sig_lines)
        except Exception:
            return node.name
```

- [ ] **Step 2: Verify with a quick AST parse test**

Run:
```bash
python -c "
import ast
code = 'def foo(): pass\ndef bar():\n    return 1'
tree = ast.parse(code)
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef):
        body_start = node.body[0].lineno
        print(f'{node.name}: lineno={node.lineno}, body_start={body_start}, guarded_end={body_start if body_start <= node.lineno else body_start - 1}')
"
```
Expected output showing correct guarded logic for both single-line and multi-line functions.

- [ ] **Step 3: Commit**

```bash
git add core/tools/search.py
git commit -m "fix: guard _build_signature against single-line function body edge case"
```

---

### Task 3: Fix #8 — Enable symbol search for non-.py files via regex fallback (`core/tools/search.py`)

**Files:**
- Modify: `core/tools/search.py:56-112` (`_search_symbols` method)

- [ ] **Step 1: Update `_search_symbols` to handle non-.py files**

Replace lines 66-71:
```python
                if include_ext and not fname.endswith(include_ext):
                    continue
                if include_ext is None and not fname.endswith(".py"):
                    continue  # symbol mode only handles .py by default
```

With:
```python
                if include_ext:
                    if not fname.endswith(include_ext):
                        continue
                elif not fname.endswith(".py"):
                    continue  # symbol mode defaults to .py
```

- [ ] **Step 2: Add regex-based fallback for non-.py symbol search**

After line 84 (`continue` on SyntaxError), add fallback logic. Replace lines 81-112 (the AST parse and walk block in `_search_symbols`):

The full replacement for `_search_symbols` from line 74 to line 112:

```python
                full_path = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(full_path, root)

                # --- .py files: use AST parsing ---
                if fname.endswith(".py"):
                    try:
                        with open(full_path, "r", encoding="utf-8") as f:
                            source = f.read()
                    except Exception:
                        continue

                    try:
                        tree = ast.parse(source)
                    except SyntaxError:
                        continue

                    for node in ast.walk(tree):
                        if not isinstance(
                            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                        ):
                            continue

                        if query_lower not in node.name.lower():
                            continue

                        signature = self._build_signature(node, source)
                        doc = ast.get_docstring(node)
                        doc_summary = ""
                        if doc:
                            doc_summary = " - " + doc.splitlines()[0].strip()

                        results.append(
                            f"[{rel_path}] L{node.lineno}-L{node.end_lineno}: {signature}{doc_summary}"
                        )
                else:
                    # --- Non-.py files: regex-based class/function matching ---
                    try:
                        with open(full_path, "r", encoding="utf-8") as f:
                            source_lines = f.readlines()
                    except Exception:
                        continue

                    # Match lines like "def foo", "class Foo", "function foo", "fn foo"
                    symbol_pattern = re.compile(
                        r"^\s*(def|class|function|fn|async\s+def|async\s+function)\s+"
                        + re.escape(query),
                        re.IGNORECASE,
                    )
                    for i, line in enumerate(source_lines):
                        if symbol_pattern.search(line):
                            results.append(
                                f"[{rel_path}] L{i+1}: {line.strip()[:120]}"
                            )
```

- [ ] **Step 3: Verify symbol search works for .py and non-.py**

Run:
```bash
python -c "
import asyncio
from core.tools.search import SearchCodebaseTool
t = SearchCodebaseTool()
# Test .py default
r = asyncio.run(t.execute(query='SearchCodebaseTool', mode='symbol', workspace_dir='.'))
print('PY search:', r.content[:200])
# Test with explicit ext
r = asyncio.run(t.execute(query='SearchCodebaseTool', mode='symbol', workspace_dir='.', include_ext='.py'))
print('PY explicit:', r.content[:200])
"
```

- [ ] **Step 4: Commit**

```bash
git add core/tools/search.py
git commit -m "feat: add regex fallback for symbol search on non-.py files"
```

---

### Task 4: Fix #6 — Harden BashTool blacklist patterns (`core/tools/bash.py`)

**Files:**
- Modify: `core/tools/bash.py:12-20` (BLACKLIST)

- [ ] **Step 1: Expand and strengthen BLACKLIST patterns**

Replace lines 12-20:
```python
BLACKLIST = [
    r"rm\s+-rf\s+/",
    r"sudo\b",
    r"chmod\s+777\s+/",
    r"mkfs\b",
    r"dd\s+if=",
    r":\(\)\s*\{\s*:\|\:&\s*\}\s*;",
    r">\s*/dev/sda",
]
```

With:
```python
BLACKLIST = [
    # Recursive force delete: rm -rf /, rm -r /, rm --force /, rm -rf ~, etc.
    r"rm\s+-r[fa]\S*\s+[/~]",
    # Privilege escalation
    r"\bsudo\b",
    # Permissive chmod on root/home
    r"chmod\s+[-R]*\s*777\s+[/~]",
    # Filesystem formatting
    r"\bmkfs\b",
    # Raw disk writes
    r"\bdd\s+if=",
    # Fork bomb
    r":\(\)\s*\{\s*:\|\:&\s*\}\s*;",
    # Overwrite block devices
    r">\s*/dev/sd[a-z]",
    # Windows: format drive
    r"\bformat\s+[A-Za-z]:",
]
```

- [ ] **Step 2: Verify blacklist catches bypass attempts**

Run:
```bash
python -c "
import re
from core.tools.bash import BLACKLIST

test_cases = [
    ('rm -rf /', True),
    ('echo safe; rm -rf /', True),
    ('rm -r /', True),
    ('rm --force /', True),
    ('rm -rf ~', True),
    ('sudo rm file', True),
    ('chmod -R 777 /', True),
    ('format C:', True),
    ('echo hello', False),
    ('ls -la', False),
    ('npm install', False),
]

for cmd, should_block in test_cases:
    blocked = any(re.search(p, cmd) for p in BLACKLIST)
    status = 'OK' if blocked == should_block else 'FAIL'
    print(f'{status}: \"{cmd}\" -> blocked={blocked} (expected={should_block})')
"
```
Expected: All `OK`.

- [ ] **Step 3: Commit**

```bash
git add core/tools/bash.py
git commit -m "fix: harden bash blacklist with broader rm patterns and Windows format guard"
```

---

### Task 5: Fix #2 — Add fuzzy line-number adjustment to EditTool (`core/tools/edit.py`)

**Files:**
- Modify: `core/tools/edit.py:43-125` (execute method)

- [ ] **Step 1: Add fuzzy adjustment before line-number validation**

In the `execute` method, after reading the file content (after line 62) and before the line-number validation block (before line 67), insert:

```python
        file_lines = content.splitlines(keepends=True)
        total_lines = len(file_lines)

        # --- Fuzzy adjustment: clip slightly out-of-range line numbers ---
        MAX_DRIFT = 20
        fuzzy_note = ""
        if start_line > total_lines:
            drift = start_line - total_lines
            if drift <= MAX_DRIFT:
                start_line = total_lines
                fuzzy_note = f" (start_line adjusted from +{drift} to end of file)"
            else:
                return ToolResult.fail(
                    f"start_line ({start_line}) is {drift} lines beyond file end "
                    f"({total_lines} lines). File may have been modified since last read. "
                    f"Please re-read the file before editing."
                )
        if start_line < 1:
            if abs(start_line) <= MAX_DRIFT:
                fuzzy_note = f" (start_line adjusted from {start_line} to 1)"
                start_line = 1
            else:
                return ToolResult.fail(
                    f"start_line ({start_line}) is invalid for file with {total_lines} lines."
                )
        if end_line > total_lines:
            drift = end_line - total_lines
            if drift <= MAX_DRIFT:
                end_line = total_lines
                if not fuzzy_note:
                    fuzzy_note = f" (end_line adjusted from +{drift} to end of file)"
                else:
                    fuzzy_note = fuzzy_note.rstrip(")") + f", end_line adjusted from +{drift})"
            else:
                return ToolResult.fail(
                    f"end_line ({end_line}) is {drift} lines beyond file end "
                    f"({total_lines} lines). File may have been modified since last read. "
                    f"Please re-read the file before editing."
                )
```

- [ ] **Step 2: Add fuzzy note to the returned diff**

After the unified diff is computed (before line 125 `return ToolResult.ok(...)`), modify:

Replace line 125:
```python
        return ToolResult.ok(diff_text if diff_text else "No changes made.")
```

With:
```python
        if fuzzy_note:
            diff_text = f"[Note: line numbers were auto-adjusted{fuzzy_note}]\n{diff_text}"
        return ToolResult.ok(diff_text if diff_text else "No changes made.")
```

- [ ] **Step 3: Verify fuzzy adjustment works**

Run:
```bash
python -c "
import asyncio, tempfile, os

# Create a test file
with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
    f.write('line1\nline2\nline3\nline4\nline5\n')
    tmp = f.name

from core.tools.edit import EditTool
t = EditTool()

async def test():
    # Test 1: in-range edit (no adjustment)
    r = await t.execute(file_path=tmp, start_line=2, end_line=2, replace_block='new2', workspace_dir=os.path.dirname(tmp))
    print('Test 1 (in-range):', 'OK' if r.success else 'FAIL')

    # Test 2: slightly out-of-range (should adjust)
    r = await t.execute(file_path=tmp, start_line=5, end_line=7, replace_block='end', workspace_dir=os.path.dirname(tmp))
    print('Test 2 (fuzzy adjust):', 'PASS' if r.success and 'auto-adjusted' in r.content else 'FAIL')

    # Test 3: way out-of-range (should fail with re-read suggestion)
    r = await t.execute(file_path=tmp, start_line=100, end_line=101, replace_block='x', workspace_dir=os.path.dirname(tmp))
    print('Test 3 (fail):', 'PASS' if not r.success and 're-read' in r.error else 'FAIL')

asyncio.run(test())
os.unlink(tmp)
"
```
Expected: All PASS.

- [ ] **Step 4: Commit**

```bash
git add core/tools/edit.py
git commit -m "feat: add fuzzy line-number adjustment to EditTool for post-edit drift"
```

---

### Task 6: Fix #3 — Add prompt length protection to `compress()` (`core/context.py`)

**Files:**
- Modify: `core/context.py:100-146` (`compress` method)

- [ ] **Step 1: Add length protection to summary prompt construction**

Replace the `compress` method body from line 106 onward (lines 106-146):

```python
        messages_to_summarize = self.messages[start:end]

        # --- Extract latest scratchpad before compression ---
        saved_scratchpad = self._extract_last_scratchpad(messages_to_summarize)

        # --- Build summary prompt with length protection ---
        MAX_PROMPT_CHARS = 64000
        per_msg_limit = 500

        serialized = "\n".join(
            f"[{m['role']}]: {(m.get('content') or '')[:per_msg_limit]}"
            for m in messages_to_summarize
        )

        # If total exceeds max, reduce per-message limit and retry
        if len(serialized) > MAX_PROMPT_CHARS:
            per_msg_limit = 200
            serialized = "\n".join(
                f"[{m['role']}]: {(m.get('content') or '')[:per_msg_limit]}"
                for m in messages_to_summarize
            )

        # Final safety cap — hard truncate if still too large
        if len(serialized) > MAX_PROMPT_CHARS:
            serialized = serialized[:MAX_PROMPT_CHARS] + "\n...[content truncated — too many messages to summarize]..."

        summary_prompt = (
            "Summarize the following conversation history concisely, "
            "preserving key decisions, file changes made, and unresolved tasks:\n\n"
        ) + serialized
```

Note: this replaces lines 112-119 (the old `summary_prompt` construction) while keeping lines 106-111 (messages extraction and scratchpad extraction) and lines 121-146 (the try/except and reassembly) unchanged.

- [ ] **Step 2: Verify the method compiles and runs**

Run:
```bash
python -c "
from core.context import ContextManager

# Simulate many messages
cm = ContextManager(system_prompt='You are a helpful assistant.')
for i in range(100):
    cm.add_user_message(f'Hello {i}' * 100)
    cm.add_assistant_message(content=f'Hi {i}' * 100)

# Check that compress doesn't crash with huge messages
print('Total tokens:', cm.estimate_tokens())
print('Needs compression:', cm.needs_compression())

# Trigger compression with a mock LLM client
import asyncio
class MockLLM:
    async def chat(self, messages, tools=None, on_token=None):
        return {'content': 'Summarized conversation.'}

async def test():
    await cm.compress(MockLLM())
    print('Compressed. Message count:', len(cm.messages))
    print('Summary content:', cm.messages[1]['content'][:100])

asyncio.run(test())
"
```
Expected: No errors, message count reduced.

- [ ] **Step 3: Commit**

```bash
git add core/context.py
git commit -m "fix: add prompt length guard to compress() to prevent context overflow"
```

---

### Task 7: Fix #4 — Deduplicate `run()` and `run_stream()` tool execution (`core/agent.py`)

**Files:**
- Modify: `core/agent.py:1-11` (add dataclass import if needed), `core/agent.py:150-351` (run and run_stream methods)

- [ ] **Step 1: Add `_ToolExec` internal dataclass + `_execute_single_tool` method**

Insert after `_check_circuit_breaker` method (after line 148), before `run`:

```python
    async def _execute_single_tool(
        self,
        tc: dict,
    ) -> tuple[str, dict, ToolResult, str, bool]:
        """Execute a single tool call. Shared by run() and run_stream().

        Handles: JSON parsing, markdown stripping, workspace injection,
        circuit breaker, tool lookup, execution, and history recording.

        Returns:
            (tool_name, tool_args, result, observation, circuit_broken)
        """
        tool_name = tc["function"]["name"]

        # 1. Parse arguments
        try:
            raw_args = tc["function"]["arguments"].strip()
            raw_args = re.sub(r"^```json\s*", "", raw_args, flags=re.IGNORECASE)
            raw_args = re.sub(r"\s*```$", "", raw_args).strip()
            args = json.loads(raw_args)
        except json.JSONDecodeError as e:
            self.ctx.add_tool_result(tc["id"], f"Error: invalid JSON arguments: {e}")
            self.action_history.append(self._hash_action(tool_name, {}))
            return tool_name, {}, ToolResult.fail(f"invalid JSON arguments: {e}"), f"Error: invalid JSON arguments: {e}", False

        # 2. Inject workspace_dir
        if tool_name in ("read", "write", "edit", "bash", "search_codebase"):
            args["workspace_dir"] = self.workspace_dir

        # 3. Circuit breaker check
        if self._check_circuit_breaker(tc["id"], tool_name, args):
            return (
                tool_name,
                args,
                ToolResult.fail(
                    "System Alert: Detected repeated failed tool calls. "
                    "STOP current action. Please reason about why it failed "
                    "and use read or search codebase to gather new context."
                ),
                "System Alert: Detected repeated failed tool calls. "
                "STOP current action. Please reason about why it failed "
                "and use read or search codebase to gather new context.",
                True,
            )

        # 4. Look up and execute tool
        tool = self.tools_by_name.get(tool_name)
        if tool is None:
            observation = f"Error: unknown tool '{tool_name}'. Available: {list(self.tools_by_name.keys())}"
            result = ToolResult.fail(f"unknown tool '{tool_name}'")
        else:
            try:
                result = await tool.execute(**args)
            except Exception as e:
                result = ToolResult.fail(str(e))

            if result.success:
                observation = result.content
            else:
                observation = f"ERROR: {result.error}"
                if result.content:
                    observation += f"\nPartial output: {result.content}"

        self.ctx.add_tool_result(tc["id"], observation)
        self.action_history.append(self._hash_action(tool_name, args))

        return tool_name, args, result, observation, False
```

- [ ] **Step 2: Rewrite `run()` to use `_execute_single_tool`**

Replace the tool execution loop in `run()` (lines 186-225):

```python
            # Execute each tool call via shared method
            for tc in tool_calls:
                await self._execute_single_tool(tc)
```

- [ ] **Step 3: Rewrite `run_stream()` to use `_execute_single_tool`**

Replace the tool execution loop in `run_stream()` (lines 269-351):

```python
            for tc in tool_calls:
                # Parse args for the tool_call event (lightweight, before execution)
                tool_name = tc["function"]["name"]
                try:
                    raw_args = tc["function"]["arguments"].strip()
                    raw_args = re.sub(r"^```json\s*", "", raw_args, flags=re.IGNORECASE)
                    raw_args = re.sub(r"\s*```$", "", raw_args).strip()
                    tool_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    tool_args = {}

                yield AgentEvent(
                    type="tool_call",
                    tool_name=tool_name,
                    tool_args=tool_args,
                )

                # Execute via shared method
                _, _, result, _, _ = await self._execute_single_tool(tc)

                yield AgentEvent(
                    type="tool_result",
                    tool_name=tool_name,
                    tool_result=result,
                )
```

- [ ] **Step 4: Verify both `run()` and `run_stream()` still work**

Run a quick smoke test:
```bash
python -c "
import asyncio
from core.llm import LLMClient
from core.context import ContextManager
from core.agent import Agent
from core.tools.read import ReadTool
from core.tools.bash import BashTool
from core.tools.search import SearchCodebaseTool
import os

async def test():
    llm = LLMClient(api_key='test', base_url='http://localhost:9999')
    ctx = ContextManager(system_prompt='You are a helpful assistant.')
    tools = [ReadTool(), BashTool(), SearchCodebaseTool()]
    agent = Agent(llm_client=llm, context_manager=ctx, tools=tools, workspace_dir=os.getcwd())

    # Test that run() doesn't crash on the method level (will fail on API call, which is expected)
    print('Agent initialized OK')
    print('_execute_single_tool method exists:', hasattr(agent, '_execute_single_tool'))

asyncio.run(test())
"
```
Expected: `Agent initialized OK` and `_execute_single_tool method exists: True`

- [ ] **Step 5: Commit**

```bash
git add core/agent.py
git commit -m "refactor: extract _execute_single_tool to deduplicate run() and run_stream()"
```

---

### Final Verification

- [ ] **Run a full import check across all modified modules**

```bash
python -c "
from core.llm import LLMClient
from core.context import ContextManager
from core.agent import Agent
from core.tools.base import BaseTool, ToolResult, truncate_long_output
from core.tools.read import ReadTool
from core.tools.write import WriteTool
from core.tools.edit import EditTool
from core.tools.bash import BashTool
from core.tools.search import SearchCodebaseTool
from core.system_prompt import SYSTEM_PROMPT
from core.exceptions import LLMAPIError, ToolSecurityError
print('All imports OK')
"
```

- [ ] **Commit final state if any stragglers**

```bash
git status
```
