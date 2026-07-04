# Explore → Execute → Verify: Three-Phase Agent Workflow Design

**Date:** 2026-07-04
**Status:** Approved
**Author:** huguotao7412 (with Claude Code brainstorming)

---

## 1. Problem Statement

The current Planner + multi-Actor concurrent architecture has proven effective — decomposing
tasks and running Actors in isolated worktrees is a solid foundation. However, three
critical gaps emerge when scaling to large projects:

| Gap | Root Cause | Symptom |
|-----|-----------|---------|
| **Premature circuit-breaker** | `MAX_STEPS=30` hardcoded in ActorAgent, `MAX_STEPS=50` in Planner | Actors exploring large codebases hit step limits before finding target files |
| **No quality gate** | Tasks go coder→done with no verification | LLM hallucinations and syntax errors go undetected until user notices |
| **Blind coding** | Coders do their own exploration | Actors waste steps navigating unfamiliar code instead of focusing on edits |

## 2. Solution: Three-Phase Agent Workflow

Introduce an **Explore → Execute → Verify** pipeline, inspired by SWE-agent and Devin's
proven workflow. Each phase uses a specialized Actor role with tuned tool sets, step
budgets, and system prompts.

### Architecture Overview

```
User Request
    │
    ▼
┌─────────────────────────────────────────────┐
│  Planner (MAX_STEPS=60, configurable)        │
│                                              │
│  1. Analyze: large/unfamiliar project?       │
│     → delegate Scout Actor (phase 1)         │
│  2. Create coder + verifier task pairs       │
│     (DAG: verifier depends on coder)         │
│  3. delegate coder Actors (phase 2, concurrent)│
│  4. delegate verifier Actors (phase 3)       │
│  5. Verification failed?                     │
│     → Analyze traceback → fix Actor → re-verify│
│     → Max 2 retry rounds                     │
│  6. apply_patch all successful diffs         │
│  7. Synthesize final response                │
└─────────────────────────────────────────────┘
    │ delegate(subtasks=[{role, ...}, ...])
    ▼
┌─────────────────────────────────────────────┐
│  Delegate Tool (DAG + TaskGroup)             │
│                                              │
│  For each dependency level:                  │
│    run_one(st) →                             │
│      1. Lookup ROLE_CONFIG[role]             │
│      2. Select system prompt                 │
│      3. Start MCP with tool_allowlist        │
│      4. Create ActorAgent(max_steps=N)       │
│      5. Execute → collect diff + summary     │
│      6. Return result (with traceback)       │
└─────────────────────────────────────────────┘
```

## 3. Actor Roles

### 3.1 Role Configuration (`core/role_config.py` — NEW)

```python
class ActorRole(Enum):
    SCOUT     = "scout"      # Read-only exploration
    CODER     = "coder"      # Read-write implementation
    VERIFIER  = "verifier"   # Test & verification

@dataclass
class RoleConfig:
    system_prompt: str
    tool_allowlist: set[str] | None   # None = all tools
    default_max_steps: int = 30
```

| Role | Default Steps | Tool Allowlist | System Prompt |
|------|--------------|----------------|---------------|
| Scout | 60 | `list_dir`, `read_outline`, `search_codebase`, `read` | SCOUT_SYSTEM_PROMPT |
| Coder | 30 | All (None) | ACTOR_SYSTEM_PROMPT |
| Verifier | 25 | `read`, `write`, `edit`, `bash`, `list_dir` | VERIFIER_SYSTEM_PROMPT |

### 3.2 Scout — Read-Only Explorer

- Hard constraint: tool allowlist excludes `write`, `edit`, and `bash`
- Output: target file list, call graph, key class/function inventory in `result_summary`
- Subsequent Coders receive Scout's `context_summaries` to jump directly to target files

### 3.3 Coder — Implementation (unchanged from current Actor)

- Full tool access in isolated worktree
- Existing `ACTOR_SYSTEM_PROMPT` with minor addition: "If exploration-only, do not write code"

### 3.4 Verifier — Quality Gate

- **Adaptive testing strategy:**
  - Pure functions / library code → write `pytest` unit tests, run via `bash pytest test_*.py -v`
  - CLI entry points / scripts / config → run directly via `bash python -c "..."` or execute the script
  - Actor decides based on task context; Planner can override via task description
- **Failure reporting:** Must include full traceback and failed assert details in `key_findings`
- **Hard constraint:** No access to `search_codebase` — verifier only needs to read the code under test

## 4. Planner Prompt Rewrite

The `PLANNER_SYSTEM_PROMPT` is extended with a new section:

### Three-Phase Workflow Rules

**Phase 1 — Explore** (mandatory for large/unfamiliar projects):
- If the project has >10 files or the user hasn't specified exact target files, delegate a
  `role="scout"` Actor first
- Feed Scout's `context_summaries` into subsequent Coder Actors' `context_summaries` field

**Phase 2 — Execute** (concurrent coding):
- Decompose into independent subtasks, each tagged `role="coder"`
- For EVERY coder task, create a paired verifier task with `dependencies: [coder_task_id]`

**Phase 3 — Verify** (quality gate with closed loop):
- After coders complete, delegate verifier Actors
- If verifier returns `failed`:
  1. Parse `key_findings` for traceback
  2. Create a fix task (`role="coder"`) with error context injected
  3. After fix, re-delegate verifier
  4. Max 2 retry rounds; if still failing, report to user with full context

## 5. Delegate Tool Changes (`core/tools/delegate.py`)

### 5.1 Subtask Schema Extension

```json
{
  "role": {
    "type": "string",
    "enum": ["scout", "coder", "verifier"],
    "default": "coder"
  },
  "max_steps": {
    "type": "integer",
    "description": "Override role default. Planner sets higher for complex tasks."
  }
}
```

### 5.2 Role Dispatch in `run_one()`

- Read `role` from subtask, resolve `RoleConfig`, select system prompt
- Pass `tool_allowlist` to `MCPToolProvider.start()`
- Pass `max_steps` to `ActorAgent()`
- On verifier failure, include full traceback in `key_findings` (truncated to 2000 chars vs current 500)

### 5.3 `asyncio.gather` → `asyncio.TaskGroup`

Replace (current line 282):
```python
batch_results = await asyncio.gather(
    *[run_one(st) for st in ready.values()],
    return_exceptions=True,
)
```

With:
```python
batch_results = []
async with asyncio.TaskGroup() as tg:
    tasks = {st["task_id"]: tg.create_task(run_one(st)) for st in ready.values()}
for tid, task in tasks.items():
    try:
        batch_results.append(task.result())
    except Exception as e:
        batch_results.append({"task_id": tid, "status": "failed", "error": str(e)})
```

Rationale: Python 3.12's `TaskGroup` provides cleaner exception tracing and safe cancellation
propagation. If an MCP-level or worktree-level fatal error escapes `run_one()`, the entire
batch should stop rather than silently proceeding with corrupted state.

### 5.4 Verification Failure Propagation

Failed verifier results include a `traceback` field extracted from `key_findings`. Planner
receives this directly in the delegate return message, enabling immediate fix Actor dispatch
without extra exploration steps.

## 6. MCP Tool Filtering (`core/mcp/client.py`)

`MCPToolProvider.start()` gains an optional `tool_allowlist` parameter:

```python
async def start(self, workspace_dir: str, tool_allowlist: set[str] | None = None):
    ...
    self._tool_allowlist = tool_allowlist

async def list_tools(self) -> list[dict]:
    all_tools = [...]  # from MCP servers
    if self._tool_allowlist is None:
        return all_tools
    return [t for t in all_tools if t.get("name") in self._tool_allowlist]
```

Filtering occurs at the schema level — the Actor never sees tools it shouldn't use.

## 7. Agent Parameterization (`core/agent.py`, `core/planner.py`)

### ActorAgent

- `__init__` gains `max_steps: int = 30` parameter
- Both `run()` and `run_stream()` use `self.max_steps` instead of hardcoded `30`

### Planner

- `__init__` gains `max_steps: int = 50` parameter
- Both `run()` and `run_stream()` use `self.max_steps` instead of hardcoded values

## 8. UI Enhancements

### CLI (`cli/ui.py`)

```python
status_styles = {
    "pending":    ("..", "dim yellow"),
    "running":    (">>", "bold cyan"),
    "verifying":  ("\U0001F50D", "bold magenta"),  # NEW
    "done":       ("OK", "bold green"),
    "failed":     ("!!", "bold red"),
    "blocked":    ("\U0001F6AB", "dim"),            # NEW (was missing)
}
```

### Web (`web/components/sidebar.py`)

```python
status_icon = {
    "pending":   "⏳",
    "running":   "\U0001F504",
    "verifying": "\U0001F50D",   # NEW
    "done":      "✅",
    "failed":    "❌",
    "blocked":   "\U0001F6AB",   # NEW (was missing)
}
```

## 9. State Model (`core/state.py`)

`TaskNode.status` literal extended:

```python
# Before
status: Literal["pending", "running", "done", "failed", "blocked"]

# After
status: Literal["pending", "running", "verifying", "done", "failed", "blocked"]
```

Planner sets coder task to `verifying` when dispatching its dependent verifier.
On verifier success → coder marked `done`; on failure → coder reverted to `running` for fix round.

## 10. File Change Summary

| File | Type | Delta |
|------|------|-------|
| `core/role_config.py` | **NEW** | ~40 lines |
| `core/state.py` | Modify | +1 literal |
| `core/agent.py` | Modify | ~6 lines |
| `core/planner.py` | Modify | ~4 lines |
| `core/system_prompt.py` | Modify | ~80 lines |
| `core/tools/delegate.py` | Modify | ~40 lines |
| `core/mcp/client.py` | Modify | ~8 lines |
| `cli/ui.py` | Modify | ~4 lines |
| `web/components/sidebar.py` | Modify | ~2 lines |

**Total: ~185 net new lines, no breaking changes.**

## 11. Design Decisions Record

| Decision | Option Selected | Rationale |
|----------|----------------|-----------|
| Verification strategy | Mixed (pytest + bash, adaptive) | Pytest for library code, bash for CLI/scripts; Actor auto-selects |
| Retry loop driver | Planner-driven | Planner has global context to judge fix vs test-bug |
| Scout design | Independent prompt + tool allowlist | Hard constraint against accidental writes during exploration |
| Architecture approach | Role config table (Approach B) | Centralized role definitions, easy to extend, ActorAgent stays generic |
| Concurrency primitive | `asyncio.TaskGroup` | Python 3.12 native, cleaner exception propagation |
