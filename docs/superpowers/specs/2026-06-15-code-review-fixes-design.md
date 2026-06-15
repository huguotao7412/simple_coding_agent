# Code Review Fixes — Design Document

**Date:** 2026-06-15
**Status:** Approved

## Overview

Based on a comprehensive code review of the Simple Coding Agent (SCA) project, this
document captures the design for 8 fixes (one critical bug was already addressed in
the working tree).

## Fixes

### #2 — EditTool Fuzzy Fallback Matching (`core/tools/edit.py`)

**Problem:** EditTool uses pure line-number-based replacement. If prior edits change
file length, subsequent edits target wrong lines. The README claims "fuzzy fallback
matching" but it is not implemented.

**Approach:** Two-phase matching:
1. Try exact line-number replacement (existing logic, kept for performance).
2. If line numbers are out of range (file changed), fall back to
   `difflib.SequenceMatcher` to find the most similar block within a ±20-line
   window around the target position.
3. If similarity >= 0.7, use matched line numbers for replacement.
4. Otherwise, return an error advising the LLM to re-read the file first.

### #3 — compress() Prompt Truncation Protection (`core/context.py`)

**Problem:** `compress()` builds a summary prompt by concatenating truncated message
content (500 chars each). If many messages accumulate, the prompt itself may exceed
the model's context window, causing a second failure.

**Approach:** Add a total-length guard before building the prompt. If serialized
content exceeds 32,000 characters, reduce per-message truncation from 500 to 200
chars and add an explicit truncation marker. Additionally, cap the total prompt
length and truncate with a warning if it exceeds 64,000 characters.

### #4 — run()/run_stream() Deduplication (`core/agent.py`)

**Problem:** `run()` and `run_stream()` share ~60 lines of nearly identical tool
execution loop code. Changes to tool execution logic must be made in two places.

**Approach:** Extract a private `_execute_tool_calls()` method that:
- Parses arguments (JSON + markdown stripping + workspace injection)
- Runs circuit breaker check
- Executes the tool
- Records the result in context and action history
- Returns structured results (list of result objects)

`run()` calls it directly; `run_stream()` calls it and yields AgentEvents.

### #5 — _build_signature() Empty Slice (`core/tools/search.py`)

**Problem:** For single-line function bodies (e.g., `def foo(): pass`),
`node.body[0].lineno` equals `node.lineno`, so `end_line <= start_line` and
the signature slice is empty.

**Approach:** Guard: when `node.body[0].lineno <= node.lineno`, set `end_line`
to `node.body[0].lineno` (not `node.body[0].lineno - 1`) to ensure at least
one line is included.

### #6 — BashTool Blacklist Hardening (`core/tools/bash.py`)

**Problem:** The blacklist uses `re.search(pattern, command)` which is correct,
but some patterns (like `rm\s+-rf\s+/`) require specific whitespace between tokens.
Commands like `rm -rf /` with tabs, or `rm -rf  /` with double spaces, could bypass.

**Approach:** Keep the regex approach but make critical patterns more robust:
- `rm\s+-rf\s+/` → `rm\s+-r[fa]\S*\s+/` (looser whitespace, covers -rf, -r, --force)
- Add explicit check for `rm` followed by `-rf` or `-r` with a path argument
- Keep other patterns as-is

### #7 — JSONDecodeError Logging (`core/llm.py`)

**Problem:** In `_parse_stream()`, `json.JSONDecodeError` is silently caught with
`continue`. If SSE data is malformed, there is no trace for debugging.

**Approach:** Add a `logging.getLogger(__name__).debug()` call before `continue`
to log the skipped line at DEBUG level. Normal operation is unaffected.

### #8 — Symbol Search Multi-Language Support (`core/tools/search.py`)

**Problem:** Symbol mode only handles `.py` files even when `include_ext` is
explicitly specified.

**Approach:** When `include_ext` is explicitly provided (not `.py`), symbol mode
falls back to regex-based class/function name matching instead of AST parsing.
The AST path remains unchanged for `.py` files.

### #9 — import json Placement (`core/llm.py`)

**Problem:** `import json` is inside `_parse_stream()` method body (line 85) rather
than at the top of the file.

**Approach:** Move `import json` to the module-level imports at the top of `llm.py`.
