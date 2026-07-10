# Runtime Trust Boundary and Observability Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make tool authorization enforceable and make every Planner/Actor action observable within an isolated run context.

**Architecture:** Add a small policy object at the MCP execution boundary and a run-scoped context containing task state, event transport, and usage totals. Refactor the existing runtime so `run()` consumes the same streaming implementation used by `run_stream()`, then route root and nested Actor events through one queue without changing public CLI commands.

**Tech Stack:** Python 3.12, asyncio, dataclasses, typing Protocol, pytest, pytest-asyncio, mypy, existing MCP and Git worktree adapters.

---

### Task 1: Enforce Actor tool policy at execution time

**Files:**
- Create: `core/policy.py`
- Modify: `core/mcp/client.py:54-194`
- Modify: `core/tools/delegate.py:266-319`
- Test: `tests/test_mcp_provider.py`

**Step 1: Write the failing authorization tests**

Add tests that construct a provider with an allowlist and call a hidden tool directly:

```python
@pytest.mark.asyncio
async def test_mcp_provider_denies_tool_not_in_allowlist(tmp_path):
    provider = MCPToolProvider()
    provider._worktree_path = str(tmp_path)
    provider._tool_routing["run"] = "bash"
    provider._sessions["bash"] = object()
    provider.set_policy(ToolPolicy.for_role("scout", {"read_file"}))

    result = await provider.call_tool("run", {"command": "git status"})

    assert not result.success
    assert "not permitted for role 'scout'" in (result.error or "")
```

Also test that an allowed tool reaches the existing dispatch path and that `list_tools()` continues filtering schemas.

**Step 2: Run the tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_mcp_provider.py -q`

Expected: FAIL because `ToolPolicy` and `set_policy()` do not exist.

**Step 3: Implement the policy value objects**

Create `core/policy.py` with a small, dependency-free API:

```python
from dataclasses import dataclass
from enum import StrEnum

class PolicyOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"

@dataclass(frozen=True)
class PolicyDecision:
    outcome: PolicyOutcome
    tool_name: str
    role: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.outcome is PolicyOutcome.ALLOW

@dataclass(frozen=True)
class ToolPolicy:
    role: str
    allowed_tools: frozenset[str] | None = None

    @classmethod
    def for_role(cls, role: str, allowed_tools: set[str] | None) -> "ToolPolicy":
        return cls(role=role, allowed_tools=None if allowed_tools is None else frozenset(allowed_tools))

    def authorize(self, tool_name: str) -> PolicyDecision:
        allowed = self.allowed_tools is None or tool_name in self.allowed_tools
        outcome = PolicyOutcome.ALLOW if allowed else PolicyOutcome.DENY
        reason = "allowed" if allowed else f"tool '{tool_name}' is not permitted for role '{self.role}'"
        return PolicyDecision(outcome, tool_name, self.role, reason)
```

**Step 4: Enforce the policy before local or MCP dispatch**

Give `MCPToolProvider` a default permissive policy for backward compatibility, add `set_policy()`, and call `authorize(name)` at the start of `call_tool()`. Reuse the same policy allowlist in `list_tools()` so guidance and enforcement cannot drift.

In `DelegateTool`, build the policy from `ActorRole` and `RoleConfig` before starting the provider.

**Step 5: Run focused and full tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_mcp_provider.py tests/test_runtime.py -q`

Expected: all selected tests pass.

**Step 6: Commit**

```powershell
git add core/policy.py core/mcp/client.py core/tools/delegate.py tests/test_mcp_provider.py
git commit -m "feat: enforce actor tool policy"
```

### Task 2: Introduce run-scoped events, state, and usage

**Files:**
- Create: `core/events.py`
- Create: `core/run_context.py`
- Modify: `core/runtime.py:35-49`
- Modify: `core/planner.py:13-44`
- Modify: `core/tools/update_state.py:7-79`
- Modify: `core/tools/delegate.py:168-218`
- Modify: `cli/main.py:16-47`
- Test: `tests/test_run_context.py`
- Test: `tests/test_runtime.py`

**Step 1: Write failing run-isolation tests**

Create `tests/test_run_context.py`:

```python
@pytest.mark.asyncio
async def test_run_contexts_do_not_share_task_state():
    left = RunContext.create()
    right = RunContext.create()

    task_id = await left.state.add_task("left task")

    assert task_id in left.state.task_tree
    assert right.state.task_tree == {}
    assert left.run_id != right.run_id

@pytest.mark.asyncio
async def test_event_bus_adds_run_metadata():
    context = RunContext.create(run_id="run_test")
    await context.emit(AgentEvent(type="done", content="ok"))
    event = await context.events.get()
    assert event.run_id == "run_test"
```

Add a test proving two Planner instances receive distinct states without calling `GlobalState.reset()`.

**Step 2: Verify the new tests fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_run_context.py -q`

Expected: FAIL because the modules do not exist.

**Step 3: Create the event contract and queue sink**

Move `AgentEvent` to `core/events.py`, preserving all existing fields and adding defaults for:

```python
run_id: str = ""
task_id: str = ""
parent_id: str = ""
prompt_tokens: int = 0
completion_tokens: int = 0
usage_estimated: bool = True
```

Add an `EventSink` Protocol and `QueueEventSink` with async `emit()` and `get()` methods. Re-export `AgentEvent` from `core/runtime.py` so existing imports stay valid.

**Step 4: Create RunContext**

Implement a factory that creates a fresh `GlobalState()` instance, queue sink, UUID run ID, and lock-protected usage totals:

```python
@dataclass
class RunContext:
    run_id: str
    state: GlobalState
    events: QueueEventSink
    usage: UsageTotals = field(default_factory=UsageTotals)

    @classmethod
    def create(cls, run_id: str | None = None) -> "RunContext": ...

    async def emit(self, event: AgentEvent) -> None: ...
    async def record_usage(self, prompt: int, completion: int, estimated: bool) -> UsageTotals: ...
```

`emit()` fills an empty event `run_id` before queueing it.

**Step 5: Inject state into Planner tools**

Add optional `state` constructor arguments to `UpdateStateTool` and `DelegateTool`. Planner creates or accepts a `RunContext`, injects `run_context.state` into those tools, and stops reading `GlobalState.get()` on the new path. Keep singleton fallback only for direct legacy construction.

Remove `GlobalState.reset()` from `build_planner()` and create a fresh `RunContext` instead.

**Step 6: Run the isolation suite**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_run_context.py tests/test_runtime.py tests/test_delegate_baseline.py -q`

Expected: all selected tests pass and no test requires singleton reset for Planner isolation.

**Step 7: Commit**

```powershell
git add core/events.py core/run_context.py core/runtime.py core/planner.py core/tools/update_state.py core/tools/delegate.py cli/main.py tests/test_run_context.py tests/test_runtime.py
git commit -m "refactor: scope state and events to each run"
```

### Task 3: Use one event-producing runtime path

**Files:**
- Modify: `core/runtime.py:78-349`
- Modify: `core/agent.py:103-164`
- Test: `tests/test_runtime.py`

**Step 1: Write failing parity and metadata tests**

Add tests proving that:

- `run()` and `run_stream()` produce the same final result and state transitions;
- events emitted by ActorAgent contain its `actor_id` and `task_id`;
- model errors and max-step errors reach the shared sink;
- tool calls are emitted once, not duplicated.

Example assertion:

```python
events = [event async for event in actor.run_stream("hello")]
assert {event.run_id for event in events} == {run_context.run_id}
assert {event.actor_id for event in events} == {"task_1"}
assert {event.task_id for event in events} == {"task_1"}
```

**Step 2: Run focused tests and confirm failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py -q`

Expected: new metadata/sink assertions fail.

**Step 3: Add one event emission helper**

Give AgentRuntime a `RunContext` and add:

```python
async def _emit(self, event: AgentEvent) -> AgentEvent:
    event.actor_id = event.actor_id or self.actor_id
    event.task_id = event.task_id or self.actor_id
    await self.run_context.emit(event)
    return event
```

Every event created in `run_stream()` must pass through `_emit()` before being yielded.

**Step 4: Replace the duplicate non-streaming loop**

Implement `run()` as a consumer of `run_stream()` that forwards thought tokens to the optional callback and returns the last `done` or `error` content. Delete the duplicate LLM/tool loop only after parity tests pass.

**Step 5: Pass RunContext through ActorAgent**

Add an optional `run_context` constructor argument and pass it into AgentRuntime. ActorAgent `run()` continues returning `ActorSummary`, but now consumes the shared event-producing runtime path.

**Step 6: Verify runtime behavior**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py -q`

Expected: all runtime tests pass with no duplicate tool events.

**Step 7: Commit**

```powershell
git add core/runtime.py core/agent.py tests/test_runtime.py
git commit -m "refactor: unify runtime event execution"
```

### Task 4: Stream nested Actor events through Planner

**Files:**
- Modify: `core/planner.py:39-71`
- Modify: `core/tools/delegate.py:274-325`
- Modify: `core/mcp/client.py:54-194`
- Test: `tests/test_runtime.py`
- Test: `tests/test_delegate_baseline.py`

**Step 1: Write a failing nested-event test**

Create a fake Planner tool that emits an Actor event into the injected RunContext before returning. Assert that `Planner.run_stream()` yields it while the root tool call is still in progress:

```python
assert [event.actor_id for event in events if event.type == "tool_call"] == ["", "task_child"]
```

Add assertions that Planner injects the same RunContext into DelegateTool and DelegateTool injects it into ActorAgent/MCPToolProvider.

**Step 2: Verify failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py tests/test_delegate_baseline.py -q`

Expected: nested child events are absent from Planner output.

**Step 3: Pump root runtime into the shared queue**

Change `Planner.run_stream()` to start the root runtime as a producer task. Drain `run_context.events` until the producer is done and the queue is empty, yielding every queued event. The root runtime's direct iterator is drained by the producer and is not yielded separately, preventing duplicates.

**Step 4: Share context with child services**

DelegateTool passes the same RunContext to each ActorAgent and to MCPToolProvider. Policy denials emitted by MCP receive the Actor's IDs and enter the same queue.

**Step 5: Verify ordering and isolation**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py tests/test_delegate_baseline.py tests/test_mcp_provider.py -q`

Expected: root and child events appear once, carry one run ID, and keep distinct actor IDs.

**Step 6: Commit**

```powershell
git add core/planner.py core/tools/delegate.py core/mcp/client.py tests/test_runtime.py tests/test_delegate_baseline.py
git commit -m "feat: stream nested actor events"
```

### Task 5: Aggregate trustworthy usage and persist complete traces

**Files:**
- Modify: `core/llm.py:61-220`
- Modify: `core/runtime.py:225-349`
- Modify: `core/run_context.py`
- Modify: `cli/report.py:23-192`
- Modify: `evals/run_evals.py:204-266,369-384`
- Modify: `cli/bridge.py:108-115`
- Test: `tests/test_runtime.py`
- Test: `tests/test_cli_report.py`
- Test: `tests/test_evals.py`

**Step 1: Write failing usage tests**

Extend FakeLLM responses with exact and estimated usage. Assert that two model calls from different actor IDs aggregate into one final `token_stats` event and that trace records retain per-call metadata.

Add a RunReport test:

```python
report.observe(AgentEvent(type="token_stats", prompt_tokens=15, completion_tokens=7))
assert report.total_tokens == 22
```

Add an eval test that makes trace opening/writing raise `OSError` and asserts the task fails with `trace persistence failed`.

**Step 2: Verify tests fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py tests/test_cli_report.py tests/test_evals.py -q`

Expected: structured usage and trace-failure assertions fail.

**Step 3: Capture provider usage with an explicit estimate flag**

In `LLMClient._parse_stream()`, preserve any `usage` object present in stream chunks, including chunks with no choices. In `chat()`, return:

```python
result["_usage"] = {
    "prompt_tokens": prompt_tokens,
    "completion_tokens": completion_tokens,
    "estimated": provider_usage is None,
}
```

Fallback estimates remain available but are never presented as exact.

**Step 4: Record every model call in RunContext**

After each response, AgentRuntime calls `record_usage()` and emits a `model_usage` event containing per-call counts and `usage_estimated`. Root token-stat events use the shared RunContext totals, so they include all child Actors.

**Step 5: Update reports and trace serialization**

RunReport reads structured token fields first and keeps the JSON content fallback for old events. `_event_to_trace_record()` writes run/task/actor IDs, parent ID, token fields, and estimate status.

Wrap eval trace persistence errors so they become deterministic eval failures. Catch report-write `OSError` in interactive Bridge and render a warning instead of crashing the completed run.

**Step 6: Run focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py tests/test_cli_report.py tests/test_evals.py -q`

Expected: all selected tests pass, including exact/estimated usage and trace failure.

**Step 7: Commit**

```powershell
git add core/llm.py core/runtime.py core/run_context.py cli/report.py evals/run_evals.py cli/bridge.py tests/test_runtime.py tests/test_cli_report.py tests/test_evals.py
git commit -m "feat: aggregate full-run usage and traces"
```

### Task 6: Add static checking and update architecture evidence

**Files:**
- Modify: `pyproject.toml:18-38`
- Modify: `.github/workflows/ci.yml:20-30`
- Modify: `README.md`
- Modify: `README_CN.md`
- Modify: `architecture.md`
- Modify: `architecture_CN.md`
- Test: all tests

**Step 1: Add mypy to development dependencies**

Add `mypy>=1.10` under `[project.optional-dependencies].dev`. Do not add broad `disable_error_code` settings or blanket ignores.

**Step 2: Run mypy on touched production modules**

Run:

```powershell
.\.venv\Scripts\python.exe -m mypy core/policy.py core/events.py core/run_context.py core/runtime.py core/planner.py core/agent.py core/mcp/client.py core/tools/update_state.py core/tools/delegate.py cli/report.py evals/run_evals.py
```

Expected: initially report actionable typing errors. Fix those errors with explicit annotations, Protocols, and narrow casts. Do not hide them with file-wide ignores.

**Step 3: Add the same mypy command to CI**

Place it after unit tests. Keep the existing compile and eval preparation checks.

**Step 4: Update documentation**

Document:

- execution-time allowlist enforcement;
- worktree isolation is not an OS sandbox;
- run-scoped state and event correlation;
- full Planner/Actor trace and usage accounting;
- exact versus estimated usage.

**Step 5: Run the complete verification matrix**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q core cli web evals tests
.\.venv\Scripts\python.exe -m cli.main --help
.\.venv\Scripts\python.exe -m evals.cli prepare
.\.venv\Scripts\python.exe -m mypy core/policy.py core/events.py core/run_context.py core/runtime.py core/planner.py core/agent.py core/mcp/client.py core/tools/update_state.py core/tools/delegate.py cli/report.py evals/run_evals.py
```

Expected: all tests pass; compile, CLI help, eval preparation, and mypy exit with code 0.

**Step 6: Inspect the final diff and repository state**

Run: `git diff --check`

Expected: no whitespace errors and no generated eval/worktree artifacts staged.

**Step 7: Commit**

```powershell
git add pyproject.toml .github/workflows/ci.yml README.md README_CN.md architecture.md architecture_CN.md
git commit -m "docs: document trusted runtime guarantees"
```

## Final Acceptance

The implementation is complete only when:

- direct invocation cannot bypass an Actor role allowlist;
- separate Planner instances do not share task state;
- nested Actor events appear in the parent Planner stream with correlation metadata;
- final token statistics include Planner and Actor usage and label estimates;
- trace persistence failure fails evals deterministically;
- the full test, compile, CLI, eval preparation, and mypy matrix passes.
