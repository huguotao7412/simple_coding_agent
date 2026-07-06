# CLI Transparent Runtime Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Refactor the CLI-first coding agent around a shared transparent ReAct runtime with deterministic tests and stable event output.

**Architecture:** Add a shared `AgentRuntime` that owns the ReAct loop currently duplicated in `Planner` and `ActorAgent`. Keep `Planner` and `ActorAgent` as compatibility wrappers while moving parsing, tool execution, loop control, compaction, and event emission into reusable runtime helpers.

**Tech Stack:** Python 3.12+, asyncio, pytest, pytest-asyncio, existing `core.llm`, `core.context`, `core.tools`, Rich CLI bridge.

---

## Task 1: Add Runtime-Focused Test Doubles

**Files:**
- Create: `tests/test_runtime.py`
- Read: `core/tools/base.py`
- Read: `core/context.py`

**Step 1: Write the failing test scaffolding**

Create `tests/test_runtime.py` with a fake LLM and fake tool:

```python
from __future__ import annotations

import pytest

from core.context import ContextManager
from core.tools.base import BaseTool, ToolResult


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.max_tokens = 128000

    def count_messages_tokens(self, messages):
        return 1

    def count_tokens(self, text):
        return max(1, len(text) // 3)

    async def chat(self, messages, tools=None, on_token=None):
        if on_token:
            on_token("thinking")
        if not self.responses:
            return {"role": "assistant", "content": "done"}
        return self.responses.pop(0)


class EchoTool(BaseTool):
    name = "echo"
    description = "Echo a value."
    parameters = {"value": {"type": "string"}}
    required_params = ["value"]

    async def execute(self, **kwargs):
        return ToolResult.ok(f"echo:{kwargs.get('value')}")
```

Add placeholder imports for the runtime that does not exist yet:

```python
from core.runtime import AgentRuntime
```

**Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'core.runtime'`.

**Step 3: Commit**

Do not commit yet if implementation starts immediately in the same working tree. Commit after Task 2 passes.

## Task 2: Implement Minimal AgentRuntime Final Answer Path

**Files:**
- Create: `core/runtime.py`
- Modify: `tests/test_runtime.py`

**Step 1: Add final-answer test**

Add:

```python
@pytest.mark.asyncio
async def test_runtime_returns_final_answer_without_tools():
    llm = FakeLLM([{"role": "assistant", "content": "final answer"}])
    ctx = ContextManager(system_prompt="system")
    runtime = AgentRuntime(
        llm_client=llm,
        context_manager=ctx,
        tools=[],
        workspace_dir=".",
        max_steps=3,
    )

    result = await runtime.run("hello")

    assert result == "final answer"
    assert ctx.messages[-1]["role"] == "assistant"
    assert ctx.messages[-1]["content"] == "final answer"
```

**Step 2: Implement minimal runtime**

Create `core/runtime.py` with:

- `AgentRuntime.__init__`
- `run(user_input, on_token=None) -> str`
- LLM call using `ctx.messages`
- final answer storage through `ctx.add_assistant_message`
- `LLMAPIError` handling returning error string

**Step 3: Run test**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py::test_runtime_returns_final_answer_without_tools -v`

Expected: PASS.

**Step 4: Commit**

```bash
git add core/runtime.py tests/test_runtime.py
git commit -m "test: add minimal agent runtime"
```

## Task 3: Centralize Tool Call Parsing

**Files:**
- Modify: `core/runtime.py`
- Modify: `tests/test_runtime.py`

**Step 1: Add parser tests**

Add tests for:

- valid JSON arguments
- empty arguments
- fenced JSON arguments
- invalid JSON arguments
- non-object JSON arguments

Expected behavior: invalid JSON returns a parse error object; non-object JSON returns `{}` without raising.

**Step 2: Implement parser helper**

In `core/runtime.py`, add a small dataclass:

```python
@dataclass
class ParsedToolCall:
    tool_name: str
    args: dict
    error: str | None = None
```

Add `parse_tool_call(tc: dict) -> ParsedToolCall`.

**Step 3: Run parser tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py -v`

Expected: all current runtime tests PASS.

**Step 4: Commit**

```bash
git add core/runtime.py tests/test_runtime.py
git commit -m "feat: centralize tool call parsing"
```

## Task 4: Centralize Local Tool Execution

**Files:**
- Modify: `core/runtime.py`
- Modify: `tests/test_runtime.py`

**Step 1: Add valid tool call test**

Mock LLM responses:

1. assistant with tool call `echo({"value": "abc"})`
2. assistant final content `"finished"`

Assert:

- runtime returns `"finished"`
- tool result message is appended
- tool result content contains `echo:abc`

**Step 2: Add invalid JSON test**

Mock a tool call with malformed arguments. Assert:

- no exception is raised
- a tool result is appended with invalid JSON guidance
- runtime can continue to final answer

**Step 3: Implement execution helper**

Add runtime method `_execute_single_tool(tc: dict)` that:

- calls `parse_tool_call`
- injects `workspace_dir` for local tools that need it
- handles unknown tools
- catches tool exceptions
- appends `ctx.add_tool_result`

**Step 4: Run tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add core/runtime.py tests/test_runtime.py
git commit -m "feat: centralize runtime tool execution"
```

## Task 5: Add Runtime Streaming Events

**Files:**
- Modify: `core/runtime.py`
- Modify: `tests/test_runtime.py`

**Step 1: Add stream event test**

Test `run_stream("hello")` with one tool call and final answer. Assert event order:

```text
thought
tool_call
tool_result
thought
done
```

The exact number of `thought` events may vary if token streaming changes; assert key relative ordering instead of brittle full equality.

**Step 2: Reuse existing `AgentEvent`**

Import `AgentEvent` from `core.agent` for compatibility, or move it later only after wrappers are stable. Avoid circular imports by importing only the dataclass.

**Step 3: Implement `run_stream`**

Mirror `run`, but:

- emit `thought` tokens from `on_token`
- emit `tool_call` before execution
- emit `tool_result` after execution
- emit `compaction`, `error`, and `done` where relevant

**Step 4: Run tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add core/runtime.py tests/test_runtime.py
git commit -m "feat: stream runtime events"
```

## Task 6: Add Repeat Detection And Max-Step Tests

**Files:**
- Modify: `core/runtime.py`
- Modify: `tests/test_runtime.py`

**Step 1: Add repeat detection test**

Mock three identical tool calls. Assert the third repeated call produces a failed tool result containing `Repeated tool call detected`.

**Step 2: Add max steps test**

Mock an LLM that always returns a tool call. Create runtime with `max_steps=1`. Assert returned content includes max-step exhaustion text and no infinite loop occurs.

**Step 3: Implement behavior**

Move repeat detection from `Planner`/`ActorAgent` into runtime using a bounded `deque`.

**Step 4: Run tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add core/runtime.py tests/test_runtime.py
git commit -m "feat: add runtime safety controls"
```

## Task 7: Migrate ActorAgent To AgentRuntime

**Files:**
- Modify: `core/agent.py`
- Modify: `tests/test_runtime.py`
- Run: existing tests

**Step 1: Replace duplicated loop**

In `ActorAgent.run`, instantiate/use `AgentRuntime` with:

- actor `llm`
- actor `ctx`
- `tools_by_name` or local tool list
- `_tool_provider`
- `workspace_dir`
- `actor_id`
- `max_steps`
- `_build_dynamic_context_msg`

Return `ActorSummary` based on runtime final string.

**Step 2: Replace `run_stream` loop**

Delegate to `AgentRuntime.run_stream` and re-yield events. Preserve `actor_id`.

**Step 3: Keep backward compatibility**

Do not remove `AgentEvent`, `ActorSummary`, `Agent = ActorAgent`, or helper functions yet.

**Step 4: Run tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py tests/test_role_config.py -v`

Expected: PASS.

**Step 5: Run full tests**

Run: `.\.venv\Scripts\python.exe -m pytest`

Expected: PASS.

**Step 6: Commit**

```bash
git add core/agent.py core/runtime.py tests/test_runtime.py
git commit -m "refactor: migrate actor agent to shared runtime"
```

## Task 8: Migrate Planner To AgentRuntime

**Files:**
- Modify: `core/planner.py`
- Modify: `core/runtime.py` if Planner-specific state update hook is needed
- Run: existing tests

**Step 1: Replace duplicated loop**

In `Planner.run`, delegate to `AgentRuntime.run` using Planner tools and context.

**Step 2: Preserve delegate state update event**

In streaming mode, after a `delegate` tool result, preserve existing behavior of emitting `actor_update` with `GlobalState.snapshot()`. Prefer a small callback/hook in `AgentRuntime` rather than Planner reimplementing the loop.

**Step 3: Run tests**

Run: `.\.venv\Scripts\python.exe -m pytest`

Expected: PASS.

**Step 4: Commit**

```bash
git add core/planner.py core/runtime.py
git commit -m "refactor: migrate planner to shared runtime"
```

## Task 9: Improve CLI Event Rendering Without Changing Core Behavior

**Files:**
- Read: `cli/bridge.py`
- Read: `cli/ui.py`
- Modify: `cli/bridge.py`
- Modify: `cli/ui.py` if needed

**Step 1: Inspect current event rendering**

Read `cli/bridge.py` and `cli/ui.py`. Identify how `thought`, `tool_call`, `tool_result`, `error`, and `done` are displayed.

**Step 2: Add rendering for stable event types**

Ensure CLI visibly handles:

- `tool_call`
- `tool_result`
- `compaction`
- `token_stats`
- `error`
- `done`

Keep output concise. Do not add Web-specific behavior.

**Step 3: Run tests**

Run: `.\.venv\Scripts\python.exe -m pytest`

Expected: PASS.

**Step 4: Manual smoke check**

Run: `sca --help`

Expected: help output prints without importing API-dependent runtime.

**Step 5: Commit**

```bash
git add cli/bridge.py cli/ui.py
git commit -m "feat: render transparent runtime events in cli"
```

## Task 10: Final Verification

**Files:**
- Modify: no files unless verification exposes issues

**Step 1: Compile all Python modules**

Run: `.\.venv\Scripts\python.exe -m compileall core cli web tests`

Expected: no syntax errors.

**Step 2: Run full test suite**

Run: `.\.venv\Scripts\python.exe -m pytest`

Expected: all tests PASS.

**Step 3: Inspect git diff**

Run: `git diff --stat`

Expected: changes are limited to runtime, tests, wrappers, CLI rendering, and docs.

**Step 4: Final commit if needed**

If verification fixes were required:

```bash
git add <changed files>
git commit -m "test: verify transparent runtime migration"
```

## Execution Notes

- Keep commits small and reversible.
- Do not remove the multi-Actor architecture.
- Do not redesign Web UI in this milestone.
- Do not expand tool permissions.
- Prefer adding runtime helpers over changing prompt behavior.
- Preserve public script entry points `sca` and `sca-web`.
