# Agent Optimization Design

**Date**: 2026-07-03
**Status**: Approved

## Overview

Comprehensive optimization of the Simple Coding Agent across six dimensions: token consumption, LLM reliability, concurrency control, CLI rendering, type safety, and observability.

---

## 1. Token Consumption Optimization (🔴 High Priority)

### 1.1 Layered Dedup Cache

**Problem**: Repeated tool calls with identical results (e.g., reading the same file) consume tokens unnecessarily.

**Solution**: Add SHA256-based dedup in `ContextManager.add_tool_result()`.

- Maintain a `set` of recent 50 content hashes
- If a tool result's hash matches a previous entry, store `"[Same result as previous call, omitted]"`
- Resets when `compress()` runs

**Files**: `core/context.py`

### 1.2 Structured Truncation for List/Search

**Problem**: `list_dir` and `search_codebase` can return large outputs with redundant formatting (emojis, context lines).

**Solution**: Auto-compact mode when output exceeds 2000 chars.

- `list_dir`: Drop emojis, output as `{"name": "...", "type": "file"|"dir"}` JSON lines
- `search_codebase`: In text mode, keep only `[file] L{line}: {match_line}` (drop context window). In symbol mode, already compact.
- Triggered automatically; no API change needed.

**Files**: `core/tools/list_dir.py`, `core/tools/search.py`

### 1.3 Consistent semantic_truncate Application

**Problem**: `semantic_truncate` exists but is only used in `ReadTool` and `BashTool` — tool results from `search_codebase`, `list_dir` can still be large.

**Solution**: Call `semantic_truncate` in `ContextManager.add_tool_result()` for all results exceeding `DEFAULT_TOKEN_BUDGET` (8000 tokens).

**Files**: `core/context.py`

---

## 2. LLM Call Reliability (🔴 High Priority)

### 2.1 Fine-grained Retry Logic

**Problem**: Current retry treats all HTTP errors the same. 429 (rate limit) should wait for `Retry-After`; 5xx should use exponential backoff.

**Solution**: Refactor `LLMClient.chat()`:

| Status | Strategy | Max Retries |
|--------|----------|-------------|
| 429 | Read `Retry-After` header, wait, retry | 5 |
| 5xx | Exponential backoff: 1s, 2s, 4s, 8s | 4 |
| Network error | Existing exponential backoff (1s, 2s, 4s) | 3 |

Move retry into a dedicated `_chat_with_retry()` method.

**Files**: `core/llm.py`

### 2.2 Two-tier Token Budget Warning

**Problem**: Compression only triggers at 80% threshold; no earlier warning or lightweight intervention.

**Solution**: Two-tier system:

| Threshold | Action |
|-----------|--------|
| 70% (`warning_threshold`) | `needs_proactive_compression()` → lightweight truncation only (no LLM call) |
| 80% (`compression_threshold`) | `needs_compression()` → full LLM summarization via `compress()` |

Add `_lightweight_compress()` method that only calls `_truncate_large_messages()` on all messages without LLM summarization.

**Files**: `core/context.py`

---

## 3. CLI Double Rendering Fix (🔴 High Priority)

**Problem**: `LiveMarkdownStream.__exit__()` (cli/ui.py:150) calls `console.print(Markdown(self._buffer))` after `Live.stop()`. The `Live` widget uses `transient=True`, which on Windows terminals may not properly clear the transient content before the new print, causing double display.

**Solution**:
- Change `transient=True` → `transient=False` in `Live` constructor
- Remove the extra `console.print(Markdown(self._buffer))` from `__exit__()`
- The `Live` with `transient=False` leaves its final rendered state on screen

**Files**: `cli/ui.py`

---

## 4. Concurrency Control (🟡 Medium Priority)

### 4.1 Configurable Actor Limit

**Problem**: `MAX_CONCURRENT_ACTORS = 4` is hardcoded.

**Solution**: Read from `SCA_MAX_ACTORS` environment variable, default 4.

**Files**: `core/tools/delegate.py`

### 4.2 Task DAG Awareness

**Problem**: All subtasks run concurrently; dependencies are ignored.

**Solution**: In `DelegateTool.execute()`:
- Parse `dependencies` from each task in `GlobalState.task_tree`
- Group tasks into dependency levels (topological sort)
- Execute each level concurrently, wait for all tasks in a level before starting the next
- Tasks with no dependencies run in level 0

**Files**: `core/tools/delegate.py`

---

## 5. Type Safety (🟡 Medium Priority)

### 5.1 mypy Configuration

Add `[tool.mypy]` section to `pyproject.toml` with strict mode, excluding `.venv` and generated files.

### 5.2 Complete Type Annotations

Add missing type annotations to:
- `core/state.py`: `TaskNode`, `GlobalState` methods
- `core/context.py`: verify all public methods have complete annotations
- `core/git_utils.py`: add return types to all functions

**Files**: `pyproject.toml`, `core/state.py`, `core/context.py`, `core/git_utils.py`

---

## 6. Logging & Observability (🟡 Medium Priority)

### 6.1 Unified Logging Config

New file `core/logging_config.py`:
```python
def setup_logging(level: str = "INFO", json_format: bool = False) -> None:
    ...
```

- JSON format: structured logs for production/collection
- Console format: colored, human-readable for development
- Controlled via `SCA_LOG_LEVEL` and `SCA_LOG_JSON` env vars

**Files**: `core/logging_config.py` (new), `cli/main.py`, `web/cli.py`

### 6.2 Actor Full-chain Trace

Add structured logging in `run_one()`:
```
{"event": "actor_start", "task_id": "...", "worktree": "..."}
{"event": "actor_end", "task_id": "...", "duration_ms": 1234, "outcome": "done"}
```

**Files**: `core/tools/delegate.py`

### 6.3 Token Consumption Stats

Accumulate token counts in `Planner.run_stream()`:
- Track `prompt_tokens` and `completion_tokens` per LLM call
- Log summary at end of each Planner run

**Files**: `core/planner.py`, `core/llm.py` (add token usage to response dict)

---

## Impact Summary

| File | Change | Lines |
|------|--------|-------|
| `core/context.py` | Modify — dedup, auto-truncate, two-tier compression | ~80 |
| `core/llm.py` | Modify — fine-grained retry, token usage in response | ~50 |
| `core/tools/delegate.py` | Modify — configurable concurrency, DAG, trace logs | ~80 |
| `core/tools/list_dir.py` | Modify — compact mode | ~15 |
| `core/tools/search.py` | Modify — compact mode | ~20 |
| `cli/ui.py` | Modify — fix double render | ~5 |
| `cli/main.py` | Modify — logging setup | ~5 |
| `core/planner.py` | Modify — token stats | ~25 |
| `core/state.py` | Modify — type annotations | ~20 |
| `core/git_utils.py` | Modify — type annotations | ~15 |
| `pyproject.toml` | Modify — mypy config | ~15 |
| `core/logging_config.py` | **New** — unified logging | ~60 |
| **Total** | | **~390 lines** |

No breaking changes. Backward compatible.
