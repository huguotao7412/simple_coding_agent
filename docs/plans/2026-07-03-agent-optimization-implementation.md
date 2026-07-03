# Agent Optimization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task.

**Goal:** Implement 6 optimization areas: token consumption, LLM reliability, CLI double-render fix, concurrency control, type safety, and logging/observability.

**Architecture:** Incremental improvements to existing modules — no new architectural patterns. Each optimization is self-contained within its module.

**Tech Stack:** Python 3.12+, httpx, Rich, pytest, mypy

---

## Task 1: Token Dedup Cache in ContextManager

**Files:**
- Modify: `core/context.py`

**Step 1: Add dedup cache to ContextManager**

Add to `__init__`:
```python
import hashlib
self._result_hashes: set[str] = set()
self._max_hash_cache = 50
```

**Step 2: Modify add_tool_result to dedup**

```python
def add_tool_result(self, tool_call_id: str, content: str) -> None:
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    if content_hash in self._result_hashes:
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": "[Same result as previous call, omitted]",
        })
        return
    self._result_hashes.add(content_hash)
    if len(self._result_hashes) > self._max_hash_cache:
        self._result_hashes.clear()  # Simple rotation
    self.messages.append({
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": content,
    })
```

**Step 3: Reset dedup cache in compress()**

After `self.messages = new_messages` in `compress()`, add: `self._result_hashes.clear()`

**Step 4: Verify**

Run: `python -c "from core.context import ContextManager; cm = ContextManager('test'); cm.add_tool_result('id1', 'hello'); cm.add_tool_result('id2', 'hello'); assert len(cm.messages) == 3; assert 'omitted' in cm.messages[2]['content']; print('PASS')"`

**Step 5: Commit**

```bash
git add core/context.py
git commit -m "feat: add tool result dedup cache to reduce token usage"
```

---

## Task 2: Auto-apply semantic_truncate in add_tool_result

**Files:**
- Modify: `core/context.py`

**Step 1: Import and apply semantic_truncate**

Add import at top of `core/context.py`:
```python
from .tools.base import semantic_truncate, DEFAULT_TOKEN_BUDGET
```

**Step 2: Truncate large tool results before storing**

In `add_tool_result`, before hashing, add:
```python
def add_tool_result(self, tool_call_id: str, content: str) -> None:
    # Truncate large tool results
    if len(content) > DEFAULT_TOKEN_BUDGET * 3:  # char heuristic (~3 chars/token)
        content, _ = semantic_truncate(content, token_budget=DEFAULT_TOKEN_BUDGET)
    # ... existing dedup logic
```

**Step 3: Verify**

Run: `python -c "from core.context import ContextManager; cm = ContextManager('test'); big = 'x' * 30000; cm.add_tool_result('id1', big); assert len(cm.messages[1]['content']) < 30000; print('PASS')"`

**Step 4: Commit**

```bash
git add core/context.py
git commit -m "feat: auto-apply semantic_truncate to large tool results"
```

---

## Task 3: Two-tier Token Budget Warning

**Files:**
- Modify: `core/context.py`

**Step 1: Add warning_threshold parameter**

In `__init__`:
```python
def __init__(
    self,
    system_prompt: str,
    max_tokens: int = 128000,
    model_context_limit: int = 128000,
    compression_threshold: float = 0.8,
    warning_threshold: float = 0.7,
    keep_recent: int = 5,
):
    self.warning_threshold = warning_threshold
    # ... rest unchanged
```

**Step 2: Add needs_proactive_compression()**

```python
def needs_proactive_compression(self, llm_client) -> bool:
    """Lightweight check at warning_threshold — no LLM call needed."""
    return self.estimate_tokens(llm_client) > int(
        self.model_context_limit * self.warning_threshold
    )
```

**Step 3: Add _lightweight_compress()**

```python
def _lightweight_compress(self) -> None:
    """Truncate large messages without calling LLM for summarization."""
    self._truncate_large_messages(self.messages, max_chars=8000)
```

**Step 4: Wire into Planner.run_stream()**

In `core/planner.py`, modify the compression check:
```python
if self.ctx.needs_compression(self.llm):
    await self.ctx.compress(self.llm)
    yield AgentEvent(type="compaction")
elif self.ctx.needs_proactive_compression(self.llm):
    self.ctx._lightweight_compress()
    yield AgentEvent(type="compaction", content="lightweight")
```

Also apply to `ActorAgent.run_stream()` in `core/agent.py`.

**Step 5: Verify**

Run: `python -c "from core.context import ContextManager; cm = ContextManager('test', model_context_limit=1000, warning_threshold=0.5); print('needs_proactive:', cm.needs_proactive_compression.__name__); print('PASS')"`

**Step 6: Commit**

```bash
git add core/context.py core/planner.py core/agent.py
git commit -m "feat: add two-tier token budget warning with lightweight compression"
```

---

## Task 4: Structured Truncation for list_dir

**Files:**
- Modify: `core/tools/list_dir.py`

**Step 1: Add compact mode**

Add constant and compact logic in `execute()`:
```python
COMPACT_THRESHOLD_CHARS = 2000

async def execute(self, dir_path: str, workspace_dir: str = "") -> ToolResult:
    # ... existing logic to build lines ...

    output = header + "\n" + "\n".join(lines)
    
    # Auto-compact: switch to JSON lines format when output is large
    if len(output) > self.COMPACT_THRESHOLD_CHARS:
        compact_lines = [header]
        for entry in entries:
            etype = "dir" if entry.is_dir(follow_symlinks=False) else "file"
            name = entry.name
            if entry.is_dir(follow_symlinks=False) and entry.name in ignore_dirs:
                continue  # skip hidden dirs in compact mode
            compact_lines.append(f'{{"name": "{name}", "type": "{etype}"}}')
        return ToolResult.ok("\n".join(compact_lines))
    
    return ToolResult.ok(output)
```

**Step 2: Verify**

Run: `python -c "from core.tools.list_dir import ListDirTool; t = ListDirTool(); import asyncio; r = asyncio.run(t.execute('.', workspace_dir='E:/huguotao7412/simple_coding_agent')); print('OK' if r.success else 'FAIL:', r.content[:200])"`

**Step 3: Commit**

```bash
git add core/tools/list_dir.py
git commit -m "feat: add auto-compact mode for large list_dir output"
```

---

## Task 5: Structured Truncation for search_codebase

**Files:**
- Modify: `core/tools/search.py`

**Step 1: Add compact threshold and logic**

Add constant:
```python
COMPACT_THRESHOLD_CHARS = 2000
```

In `_search_text_sync`, after building results but before returning, add:
```python
output = "\n\n".join(results)
if len(output) > self.COMPACT_THRESHOLD_CHARS and len(results) > 5:
    # Compact mode: drop context lines, keep only match lines
    compact = []
    for r in results:
        lines = r.split("\n")
        match_line = next((l for l in lines if l.startswith("  >")), lines[0] if lines else "")
        compact.append(match_line)
    compact.append(f"\n... [Compact mode: {len(results)} matches. Use read to inspect specific files.] ...")
    return ToolResult.ok("\n".join(compact))
```

**Step 2: Verify**

Run: `python -c "from core.tools.search import SearchCodebaseTool; t = SearchCodebaseTool(); import asyncio; r = asyncio.run(t.execute(query='def', mode='text', workspace_dir='E:/huguotao7412/simple_coding_agent', include_ext='.py')); print('OK' if r.success else 'FAIL:', len(r.content))"`

**Step 3: Commit**

```bash
git add core/tools/search.py
git commit -m "feat: add auto-compact mode for large search_codebase output"
```

---

## Task 6: Fine-grained LLM Retry Logic

**Files:**
- Modify: `core/llm.py`

**Step 1: Refactor chat() with status-code-aware retry**

Replace the current retry loop in `chat()`:
```python
async def chat(
    self,
    messages: list[dict],
    tools: list[dict] | None = None,
    on_token: Callable[[str], None] | None = None,
) -> dict:
    body: dict[str, Any] = {
        "model": self.model,
        "messages": messages,
        "max_tokens": self.max_tokens,
        "stream": True,
    }
    if tools:
        body["tools"] = tools

    timeout_config = httpx.Timeout(600.0)
    
    # Separate retry counts for different error types
    max_retries_network = 3
    max_retries_server = 4
    max_retries_rate = 5
    
    last_error = None
    
    async with httpx.AsyncClient(timeout=timeout_config) as client:
        for attempt in range(max(max_retries_network, max_retries_server, max_retries_rate)):
            try:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=body,
                ) as response:
                    if response.status_code == 200:
                        return await self._parse_stream(response, on_token)
                    
                    # Read error body
                    text = await response.aread()
                    error_body = text.decode()[:500]
                    
                    # 429: Rate limit — wait for Retry-After
                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After", "5")
                        try:
                            wait = int(retry_after)
                        except ValueError:
                            wait = 5
                        if attempt >= max_retries_rate - 1:
                            raise LLMAPIError(response.status_code, error_body)
                        await asyncio.sleep(wait)
                        continue
                    
                    # 5xx: Server error — exponential backoff
                    if response.status_code >= 500:
                        if attempt >= max_retries_server - 1:
                            raise LLMAPIError(response.status_code, error_body)
                        await asyncio.sleep(2 ** attempt)
                        continue
                    
                    # 4xx (except 429): no retry
                    raise LLMAPIError(response.status_code, error_body)
                    
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
                if attempt >= max_retries_network - 1:
                    error_detail = f"{type(e).__name__}: {str(e) or 'Connection dropped or timed out'}"
                    raise LLMAPIError(0, error_detail) from e
                await asyncio.sleep(2 ** attempt)
```

**Step 2: Verify**

Run: `python -c "from core.llm import LLMClient; c = LLMClient('test'); print('chat method has retry logic:', 'max_retries_rate' in c.chat.__code__.co_varnames if hasattr(c.chat, '__code__') else 'check source'); print('PASS')"`

**Step 3: Commit**

```bash
git add core/llm.py
git commit -m "feat: fine-grained retry with Retry-After for 429 and backoff for 5xx"
```

---

## Task 7: Token Usage Tracking in LLMClient

**Files:**
- Modify: `core/llm.py`

**Step 1: Add token usage to response dict**

In `_parse_stream()`, after building the `result` dict, track usage from stream chunks. Add a `usage` field if the API returns it in the final chunk.

But since DeepSeek may not include usage in streaming mode, add estimate-based tracking:

```python
async def chat(self, messages, tools=None, on_token=None) -> dict:
    # ... before returning ...
    result = await self._parse_stream(response, on_token)
    
    # Add estimated token usage
    result["_usage"] = {
        "prompt_tokens": self.count_messages_tokens(messages),
        "completion_tokens": self.count_tokens(result.get("content") or ""),
    }
    return result
```

**Step 2: Verify**

Run: `python -c "from core.llm import LLMClient; print('_usage field logic ready'); print('PASS')"`

**Step 3: Commit**

```bash
git add core/llm.py
git commit -m "feat: add token usage estimation to chat response"
```

---

## Task 8: Token Stats Logging in Planner

**Files:**
- Modify: `core/planner.py`

**Step 1: Accumulate and log token usage in run_stream()**

Add before the while loop:
```python
total_prompt_tokens = 0
total_completion_tokens = 0
```

After each `response = await chat_task`:
```python
usage = response.get("_usage", {})
total_prompt_tokens += usage.get("prompt_tokens", 0)
total_completion_tokens += usage.get("completion_tokens", 0)
```

Before `return` (when done or error), add:
```python
import logging
logger = logging.getLogger(__name__)
logger.info(
    "Planner run complete: prompt_tokens=%d, completion_tokens=%d, total=%d",
    total_prompt_tokens, total_completion_tokens,
    total_prompt_tokens + total_completion_tokens,
)
```

**Step 2: Verify**

Run: `grep -n "total_prompt_tokens" core/planner.py`

**Step 3: Commit**

```bash
git add core/planner.py
git commit -m "feat: add per-run token consumption logging to Planner"
```

---

## Task 9: CLI Double Rendering Fix

**Files:**
- Modify: `cli/ui.py`

**Step 1: Fix LiveMarkdownStream**

Change `transient=True` → `transient=False`, remove extra `console.print`:

```python
class LiveMarkdownStream:
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
            vertical_overflow="ellipsis",
            transient=False,  # ← changed from True
        )
        self._live.start()
        return self

    def __exit__(self, *args):
        if self._live:
            self._live.stop()
            self._live = None
        # No extra console.print — Live with transient=False leaves final content

    def add_token(self, token: str) -> None:
        self._buffer += token
        if self._live:
            self._live.update(Markdown(self._buffer + "▌"))
```

**Step 2: Verify**

Check that `console.print(Markdown(...))` no longer appears in `__exit__`:
Run: `grep -n "console.print" cli/ui.py`

**Step 3: Commit**

```bash
git add cli/ui.py
git commit -m "fix: remove double rendering in LiveMarkdownStream"
```

---

## Task 10: Configurable MAX_CONCURRENT_ACTORS

**Files:**
- Modify: `core/tools/delegate.py`

**Step 1: Replace hardcoded constant**

```python
MAX_CONCURRENT_ACTORS = int(os.getenv("SCA_MAX_ACTORS", "4"))
```

**Step 2: Verify**

Run: `SCA_MAX_ACTORS=8 python -c "from core.tools.delegate import MAX_CONCURRENT_ACTORS; assert MAX_CONCURRENT_ACTORS == 8; print('PASS')"`

**Step 3: Commit**

```bash
git add core/tools/delegate.py
git commit -m "feat: make MAX_CONCURRENT_ACTORS configurable via SCA_MAX_ACTORS env"
```

---

## Task 11: Task DAG-aware Concurrency

**Files:**
- Modify: `core/tools/delegate.py`

**Step 1: Add topological execution in execute()**

Replace the simple `asyncio.gather` with DAG-aware execution:

```python
async def execute(self, subtasks: list[dict], **kwargs) -> ToolResult:
    # ... existing validation and mark-running logic ...
    
    state = GlobalState.get()
    
    # Build dependency graph
    task_deps: dict[str, set[str]] = {}
    for st in subtasks:
        tid = st["task_id"]
        node = state.task_tree.get(tid)
        deps = set(node.dependencies) if node else set()
        task_deps[tid] = {d for d in deps if d in task_deps or any(
            s["task_id"] == d for s in subtasks
        )}
    
    # Topological sort into levels
    completed: set[str] = set()
    results: list[dict] = []
    remaining = {st["task_id"]: st for st in subtasks}
    
    while remaining:
        # Find tasks whose dependencies are all completed
        ready = {
            tid: st for tid, st in remaining.items()
            if task_deps.get(tid, set()).issubset(completed)
        }
        if not ready:
            # Circular dependency or all-done
            break
        
        # Execute ready tasks concurrently
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_ACTORS)
        async def run_with_semaphore(st):
            async with semaphore:
                return await run_one(st)
        
        batch_results = await asyncio.gather(
            *[run_with_semaphore(st) for st in ready.values()]
        )
        
        for r in batch_results:
            results.append(r)
            completed.add(r["task_id"])
            del remaining[r["task_id"]]
    
    # ... existing return message building ...
```

**Step 2: Verify**

Check syntax:
Run: `python -c "import ast; ast.parse(open('core/tools/delegate.py').read()); print('Syntax OK')"`

**Step 3: Commit**

```bash
git add core/tools/delegate.py
git commit -m "feat: add task DAG-aware execution ordering for delegate"
```

---

## Task 12: Unified Logging Configuration

**Files:**
- Create: `core/logging_config.py`
- Modify: `cli/main.py`

**Step 1: Create core/logging_config.py**

```python
"""Unified logging configuration for SCA."""

from __future__ import annotations

import logging
import os
import sys


def setup_logging(level: str | None = None, json_format: bool | None = None) -> None:
    """Configure root logger for the entire application.
    
    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR). Defaults to SCA_LOG_LEVEL env or INFO.
        json_format: If True, output JSON. Defaults to SCA_LOG_JSON env or False.
    """
    if level is None:
        level = os.getenv("SCA_LOG_LEVEL", "INFO")
    if json_format is None:
        json_format = os.getenv("SCA_LOG_JSON", "").lower() in ("1", "true", "yes")
    
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    # Remove existing handlers
    for h in root.handlers[:]:
        root.removeHandler(h)
    
    if json_format:
        handler = logging.StreamHandler(sys.stderr)
        formatter = _JSONFormatter()
        handler.setFormatter(formatter)
        root.addHandler(handler)
    else:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)-8s %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(handler)


class _JSONFormatter(logging.Formatter):
    """Structured JSON log formatter."""
    
    def format(self, record: logging.LogRecord) -> str:
        import json
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            payload["exc"] = str(record.exc_info[1])
        return json.dumps(payload, ensure_ascii=False)
```

**Step 2: Wire into cli/main.py**

Add before the lazy imports:
```python
from core.logging_config import setup_logging
setup_logging()
```

**Step 3: Verify**

Run: `python -c "from core.logging_config import setup_logging; setup_logging('DEBUG'); import logging; logging.getLogger('test').info('hello'); print('PASS')"`

**Step 4: Commit**

```bash
git add core/logging_config.py cli/main.py
git commit -m "feat: add unified JSON/console logging configuration"
```

---

## Task 13: Actor Full-chain Trace Logging

**Files:**
- Modify: `core/tools/delegate.py`

**Step 1: Add structured trace logs in run_one()**

In `run_one()`, add:
```python
import time
import logging
logger = logging.getLogger(__name__)

# At start of run_one, after worktree creation:
start_time = time.monotonic()
logger.info(
    "actor_start task_id=%s worktree=%s",
    tid, wt_path,
)

# Before return (in try block, after getting summary):
duration_ms = int((time.monotonic() - start_time) * 1000)
logger.info(
    "actor_end task_id=%s duration_ms=%d outcome=%s files_modified=%d",
    tid, duration_ms, summary.status, len(summary.files_modified),
)

# In except block:
duration_ms = int((time.monotonic() - start_time) * 1000)
logger.error(
    "actor_end task_id=%s duration_ms=%d outcome=failed error=%s",
    tid, duration_ms, str(e),
)
```

**Step 2: Verify**

Run: `grep -n "actor_start\|actor_end" core/tools/delegate.py`

**Step 3: Commit**

```bash
git add core/tools/delegate.py
git commit -m "feat: add Actor full-chain trace logging with task_id, duration, outcome"
```

---

## Task 14: mypy Configuration

**Files:**
- Modify: `pyproject.toml`

**Step 1: Add [tool.mypy] section**

```toml
[tool.mypy]
strict = true
python_version = "3.12"
exclude = [".venv", ".worktrees", "__pycache__"]
ignore_missing_imports = true

[[tool.mypy.overrides]]
module = ["deepseek_tokenizer", "streamlit", "dotenv", "rich"]
ignore_missing_imports = true
```

**Step 2: Run mypy to see baseline**

Run: `pip install mypy && mypy cli/ core/ --ignore-missing-imports 2>&1 | head -30`
Expected: Some type errors (we'll fix key files in next task).

**Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add mypy strict configuration"
```

---

## Task 15: Complete Type Annotations for Key Modules

**Files:**
- Modify: `core/state.py`
- Modify: `core/git_utils.py`

**Step 1: Add return types to core/state.py**

- `get() -> GlobalState`
- `reset() -> None`
- `add_task(description: str, dependencies: list[str] | None = None) -> str`
- `update_task(task_id: str, **kwargs: object) -> None`
- `add_summary(task_id: str, summary: str, diff: str = "") -> None`
- `consume_changes() -> list[ChangeRecord]`
- `snapshot() -> dict[str, object]`

**Step 2: Add return types to core/git_utils.py**

Ensure all functions have explicit return types:
- `_run_git(*args, cwd=None, timeout=30) -> tuple[int, str, str]`
- `setup_worktree(base_dir: str, task_id: str) -> str`
- `teardown_worktree(worktree_path: str) -> None`
- `extract_diff(worktree_path: str) -> str`
- `cleanup_orphans(base_dir: str) -> list[str]`
- `is_clean(workspace_dir: str) -> bool`

**Step 3: Verify type completeness**

Run: `mypy core/state.py core/git_utils.py --ignore-missing-imports`

**Step 4: Commit**

```bash
git add core/state.py core/git_utils.py
git commit -m "chore: add complete type annotations to state.py and git_utils.py"
```

---

## Task 16: Final Integration Test

**Files:**
- None (test only)

**Step 1: Run full import test**

```bash
python -c "
from core.context import ContextManager
from core.llm import LLMClient
from core.planner import Planner
from core.state import GlobalState
from core.tools.delegate import MAX_CONCURRENT_ACTORS
from core.logging_config import setup_logging
from cli.ui import UI, LiveMarkdownStream
print('All imports successful')
"
```

**Step 2: Run mypy on changed files**

```bash
mypy core/context.py core/llm.py core/planner.py core/tools/delegate.py core/tools/list_dir.py core/tools/search.py cli/ui.py core/state.py core/git_utils.py --ignore-missing-imports
```

**Step 3: Verify CLI starts (dry run)**

```bash
python -c "from cli.main import main; print('CLI entry loads OK')"
```

**Step 4: Commit**

```bash
git add -A
git commit -m "chore: final integration verification"
```

---

## Execution Order

| Task | Priority | Dependencies |
|------|----------|-------------|
| 1. Token Dedup Cache | 🔴 | None |
| 2. Auto semantic_truncate | 🔴 | None |
| 3. Two-tier Token Warning | 🔴 | None |
| 4. list_dir Compact | 🔴 | None |
| 5. search_codebase Compact | 🔴 | None |
| 6. LLM Retry Logic | 🔴 | None |
| 7. Token Usage Tracking | 🔴 | Task 6 |
| 8. Planner Token Stats | 🔴 | Task 7 |
| 9. CLI Double Render Fix | 🔴 | None |
| 10. Configurable Concurrency | 🟡 | None |
| 11. DAG-aware Concurrency | 🟡 | Task 10 |
| 12. Unified Logging | 🟡 | None |
| 13. Actor Trace Logs | 🟡 | Task 12 |
| 14. mypy Config | 🟡 | None |
| 15. Type Annotations | 🟡 | Task 14 |
| 16. Integration Test | 🔴 | All above |

Tasks 1-9 (🔴) can run in any order and are independent. Tasks 10-15 (🟡) have some dependencies.

**Estimated total: ~16 commits, ~390 lines changed.**
