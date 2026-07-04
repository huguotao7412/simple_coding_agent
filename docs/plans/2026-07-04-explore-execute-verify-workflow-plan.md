# Explore → Execute → Verify: Three-Phase Workflow Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Evolve the Simple Coding Agent from "dispatch → execute" to "Explore → Execute → Verify" with role-specialized Actors, dynamic step budgets, and closed-loop verification.

**Architecture:** Introduce an `ActorRole` enum with per-role configuration (system prompt, tool allowlist, step budget). The Delegate tool dispatches Actors by role. The Planner enforces a three-phase workflow (Scout → Coder → Verifier) with automatic retry on verification failure. Concurrency migrates from `asyncio.gather` to `asyncio.TaskGroup`.

**Tech Stack:** Python 3.12, asyncio, MCP (Model Context Protocol), Rich (CLI), Streamlit (Web)

**Design Doc:** `docs/plans/2026-07-04-explore-execute-verify-workflow-design.md`

---

## Dependency Graph

```
Task 1 (role_config.py) ─────────────────────────┐
                                                  ├──→ Task 7 (delegate.py)
Task 2 (state.py) ────────────────────────────────┤
                                                  │
Task 3 (agent.py) ────────────────────────────────┤
                                                  │
Task 4 (planner.py) ──────────────────────────────┤
                                                  │
Task 5 (mcp/client.py) ───────────────────────────┤
                                                  │
Task 6 (system_prompt.py) ────────────────────────┘

Task 8 (cli/ui.py) — independent, after Task 2
Task 9 (web/sidebar.py) — independent, after Task 2
Task 10 (validation) — after all Tasks 1-9
```

Tasks 1-6 can be done in parallel. Task 7 depends on 1-6. Tasks 8-9 are independent.

---

### Task 1: Create `core/role_config.py` — Role Configuration Table

**Files:**
- Create: `core/role_config.py`

**Step 1: Write the module**

```python
"""Actor role configuration — system prompt, tool allowlist, and step budget per role.

Used by DelegateTool.run_one() to dispatch Actors with the correct configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ActorRole(Enum):
    SCOUT = "scout"          # Read-only exploration
    CODER = "coder"          # Read-write implementation
    VERIFIER = "verifier"    # Test & verification


@dataclass
class RoleConfig:
    system_prompt: str
    tool_allowlist: set[str] | None   # None = all tools available
    default_max_steps: int = 30


# Actual prompt strings are imported lazily to avoid circular imports
# with system_prompt.py. See _build_role_config() below.

ROLE_CONFIG: dict[ActorRole, RoleConfig] = {}


def _build_role_config() -> dict[ActorRole, RoleConfig]:
    """Build the role configuration table.

    Lazy import of system_prompt constants to avoid circular dependency
    (system_prompt.py may import from role_config in the future).
    """
    from .system_prompt import (
        SCOUT_SYSTEM_PROMPT,
        ACTOR_SYSTEM_PROMPT,
        VERIFIER_SYSTEM_PROMPT,
    )

    return {
        ActorRole.SCOUT: RoleConfig(
            system_prompt=SCOUT_SYSTEM_PROMPT,
            tool_allowlist={"list_dir", "read_outline", "search_codebase", "read"},
            default_max_steps=60,
        ),
        ActorRole.CODER: RoleConfig(
            system_prompt=ACTOR_SYSTEM_PROMPT,
            tool_allowlist=None,  # Full tool access
            default_max_steps=30,
        ),
        ActorRole.VERIFIER: RoleConfig(
            system_prompt=VERIFIER_SYSTEM_PROMPT,
            tool_allowlist={"read", "write", "edit", "bash", "list_dir"},
            default_max_steps=25,
        ),
    }


def get_role_config(role: ActorRole) -> RoleConfig:
    """Return the RoleConfig for the given role, initializing lazily if needed."""
    if not ROLE_CONFIG:
        ROLE_CONFIG.update(_build_role_config())
    return ROLE_CONFIG[role]
```

**Step 2: Verify syntax**

Run: `cd E:/huguotao7412/simple_coding_agent && python -c "from core.role_config import ActorRole, get_role_config; print('OK')"`
Expected: `OK` (import succeeds, no circular dependency)

**Step 3: Commit**

```bash
git add core/role_config.py
git commit -m "feat: add ActorRole enum and role configuration table"
```

---

### Task 2: Extend `core/state.py` — Add `verifying` Status

**Files:**
- Modify: `core/state.py:13`

**Step 1: Add `verifying` to the Literal type**

Edit `core/state.py`, line 13. Change:

```python
status: Literal["pending", "running", "done", "failed", "blocked"] = "pending"
```

To:

```python
status: Literal["pending", "running", "verifying", "done", "failed", "blocked"] = "pending"
```

**Step 2: Verify**

Run: `cd E:/huguotao7412/simple_coding_agent && python -c "from core.state import TaskNode; t = TaskNode(task_id='test', description='test', status='verifying'); print(t.status)"`
Expected: `verifying`

**Step 3: Commit**

```bash
git add core/state.py
git commit -m "feat: add 'verifying' status to TaskNode for verification phase"
```

---

### Task 3: Parameterize `core/agent.py` — Configurable `max_steps`

**Files:**
- Modify: `core/agent.py:127-136` (ActorAgent.__init__)
- Modify: `core/agent.py:282` (MAX_STEPS in run)
- Modify: `core/agent.py:359` (MAX_STEPS in run_stream)

**Step 1: Add `max_steps` parameter to `__init__`**

In `ActorAgent.__init__` (line 127-136), add `max_steps` parameter:

```python
def __init__(
    self,
    llm_client: LLMClient,
    context_manager: ContextManager,
    tools: list[BaseTool] | None = None,
    workspace_dir: str = "",
    actor_id: str = "",
    task_context: str = "",
    tool_provider: Any | None = None,
    max_steps: int = 30,  # NEW: configurable step budget
):
    self.actor_id = actor_id
    self.task_context = task_context
    self.llm = llm_client
    self.workspace_dir = workspace_dir
    self._tool_provider = tool_provider
    self.max_steps = max_steps  # NEW
    # ... rest unchanged ...
```

**Step 2: Replace hardcoded `MAX_STEPS` in `run()`**

Change line 282 from:
```python
MAX_STEPS = 30
```
To:
```python
MAX_STEPS = self.max_steps
```

**Step 3: Replace hardcoded `MAX_STEPS` in `run_stream()`**

Change line 359 from:
```python
MAX_STEPS = 30
```
To:
```python
MAX_STEPS = self.max_steps
```

**Step 4: Verify**

Run: `cd E:/huguotao7412/simple_coding_agent && python -c "from core.agent import ActorAgent; print('OK')"`
Expected: `OK`

**Step 5: Commit**

```bash
git add core/agent.py
git commit -m "feat: make ActorAgent max_steps configurable via constructor"
```

---

### Task 4: Parameterize `core/planner.py` — Configurable `max_steps`

**Files:**
- Modify: `core/planner.py:21-28` (Planner.__init__)
- Modify: `core/planner.py:50` (MAX_STEPS in run)
- Modify: `core/planner.py:140` (MAX_STEPS in run_stream)

**Step 1: Add `max_steps` parameter to `__init__`**

```python
def __init__(
    self,
    llm_client: LLMClient,
    context_manager: ContextManager,
    tools: list[BaseTool],
    workspace_dir: str,
    max_steps: int = 50,  # NEW
):
    self.llm = llm_client
    self.workspace_dir = workspace_dir
    self.ctx = context_manager
    self.state = GlobalState.get()
    self.max_steps = max_steps  # NEW
    # ... rest unchanged ...
```

**Step 2: Replace hardcoded `MAX_STEPS` in `run()`**

Change line 50 from:
```python
MAX_STEPS = 50
```
To:
```python
MAX_STEPS = self.max_steps
```

**Step 3: Replace hardcoded `MAX_STEPS` in `run_stream()`**

Change line 140 from:
```python
MAX_STEPS = 30
```
To:
```python
MAX_STEPS = self.max_steps
```

**Step 4: Verify**

Run: `cd E:/huguotao7412/simple_coding_agent && python -c "from core.planner import Planner; print('OK')"`
Expected: `OK`

**Step 5: Commit**

```bash
git add core/planner.py
git commit -m "feat: make Planner max_steps configurable via constructor"
```

---

### Task 5: Add Tool Allowlist to `core/mcp/client.py`

**Files:**
- Modify: `core/mcp/client.py:80` (start method signature)
- Modify: `core/mcp/client.py:135-151` (list_tools method)

**Step 1: Extend `start()` to accept `tool_allowlist`**

Change line 80 from:
```python
async def start(self, worktree_path: str) -> None:
```
To:
```python
async def start(self, worktree_path: str, tool_allowlist: set[str] | None = None) -> None:
```

Add after `self._worktree_path = os.path.abspath(worktree_path)` (after line 90):
```python
self._tool_allowlist = tool_allowlist
```

Also add to `__init__` (after line 74):
```python
self._tool_allowlist: set[str] | None = None
```

**Step 2: Filter tools in `list_tools()`**

Change `list_tools()` (line 135-151) from:
```python
async def list_tools(self) -> list[dict]:
    """..."""
    if not self._tool_schemas:
        await self._build_routing_table()
    return self._tool_schemas
```

To:
```python
async def list_tools(self) -> list[dict]:
    """Return cached tool schemas, filtered by allowlist if set."""
    if not self._tool_schemas:
        await self._build_routing_table()
    if self._tool_allowlist is None:
        return self._tool_schemas
    return [
        t for t in self._tool_schemas
        if t.get("function", {}).get("name") in self._tool_allowlist
    ]
```

**Step 3: Verify**

Run: `cd E:/huguotao7412/simple_coding_agent && python -c "from core.mcp.client import MCPToolProvider; print('OK')"`
Expected: `OK`

**Step 4: Commit**

```bash
git add core/mcp/client.py
git commit -m "feat: add tool_allowlist filtering to MCPToolProvider"
```

---

### Task 6: Rewrite `core/system_prompt.py` — Three Actor Prompts + Planner Rewrite

**Files:**
- Modify: `core/system_prompt.py` (entire file)

**Step 1: Add `SCOUT_SYSTEM_PROMPT`**

Add after the existing `PLANNER_SYSTEM_PROMPT` definition (after line 50):

```python
SCOUT_SYSTEM_PROMPT = """You are Simple Coding Agent (Scout mode), a read-only exploration agent.

Your sole purpose is to explore an unfamiliar codebase and produce a structured map
for other Agents (Coders) to use. You do NOT write code, edit files, or run shell
commands.

## Your Tools (READ-ONLY)
- **list_dir**: Explore directory structure
- **read_outline**: View skeleton structure of large files (classes, functions, signatures)
- **search_codebase**: Locate symbols, classes, functions, or text patterns
- **read**: Read the contents of specific files

## Your Task
1. **Map the project structure** — which directories contain what
2. **Identify target files** — files most relevant to the task described in your context
3. **Trace call relationships** — which functions/classes call which, key imports
4. **Note patterns and conventions** — coding style, naming conventions, test patterns

## Output Format
When done, produce a structured summary with these sections:
- **Project Layout**: Top-level directory purpose and key files
- **Target Files**: Full paths of files that need modification, with brief notes on what's in each
- **Call Graph**: Key call relationships (e.g., "main() → parse_args() → run_command()")
- **Conventions**: Naming style, test framework (if any), config patterns
- **Gotchas**: Circular imports, unusual patterns, deprecated code

## Rules
- NEVER write, edit, or delete any file. You have NO write/edit/bash tools.
- NEVER run shell commands. You have no bash tool.
- Focus on producing high-density, actionable context. Other Agents will read your summary.
- Prefer read_outline over read for large files — then deep-read only the most relevant ones.
- If the project is very large, focus on the subset most relevant to the task description.
"""
```

**Step 2: Add `VERIFIER_SYSTEM_PROMPT`**

Add after `SCOUT_SYSTEM_PROMPT`:

```python
VERIFIER_SYSTEM_PROMPT = """You are Simple Coding Agent (Verifier mode), a quality-assurance agent.

Your job is to verify that another Agent's code changes are correct by writing and
running tests. You operate in an isolated git worktree that already contains the
changes made by the Coder Agent.

## Your Tools
- **read**: Read files to understand the code changes
- **write / edit**: Create or modify test files
- **bash**: Run test commands (pytest, python -c, etc.)
- **list_dir**: Check directory structure

## Verification Strategy (Adaptive)

Choose your approach based on the code under test:

### Strategy A: Pytest (for libraries, pure functions, modules with clear interfaces)
1. Read the changed files to understand what was modified
2. Create `test_<module>.py` with focused unit tests covering:
   - Happy path (expected inputs → correct outputs)
   - Edge cases (empty, None, boundary values)
   - Error handling (invalid inputs → proper exceptions)
3. Run: `bash pytest test_<module>.py -v --tb=short`
4. If tests pass → report success
5. If tests fail → include the FULL traceback in your key_findings

### Strategy B: Direct Execution (for CLI tools, scripts, configuration changes)
1. Read the changed files
2. Run the script/module directly: `bash python -c "from module import func; ..."`
3. Or run the CLI entry point with test inputs
4. Verify output matches expectations
5. If it crashes → include the FULL traceback in your key_findings

## Output Format
When done, your key_findings MUST include:
- **Verdict**: PASS or FAIL
- **Strategy used**: pytest / direct execution / mixed
- **Test summary**: What was tested, how many tests, results
- **On FAILURE**: Complete traceback, failed assertion details, and your analysis of what went wrong

## Rules
- NEVER modify the Coder's original files. Only create new test files.
- ALWAYS include full traceback on failure — the Planner needs it to dispatch a fix.
- If pytest is not installed, fall back to `python -c` direct assertions.
- Do NOT delete test files after running — they become part of the project.
- If the code changes are trivial (typo fix, comment change), a simple syntax check
  (`python -m py_compile <file>`) is sufficient.
"""
```

**Step 3: Update `ACTOR_SYSTEM_PROMPT`**

Add this line at the end of the Rules section (before the Git Restrictions section, after line 74):

```
- If your task is exploration-only, do NOT write code — only analyze and report findings.
```

**Step 4: Rewrite `PLANNER_SYSTEM_PROMPT`**

Replace the existing `PLANNER_SYSTEM_PROMPT` (lines 1-50) with the expanded version below. The key addition is the "Three-Phase Workflow" section inserted as step 2.5 in the workflow:

```python
PLANNER_SYSTEM_PROMPT = """You are Simple Coding Agent (Planner mode), a task orchestration agent.

You solve complex programming tasks by decomposing them into subtasks and delegating
execution to isolated Actor agents. You do NOT author code, edit files, or run shell
commands yourself — you orchestrate.

## Your Workflow

### 1. Analyze the user's request
Understand the full scope. Determine if this is a greenfield project, a modification
to existing code, or a bug fix in a large codebase.

### 2. Optional: Explore (MANDATORY for large/unfamiliar projects)
If ANY of these conditions are true:
- The project has >10 files
- The user has not specified exact target file paths
- You are unfamiliar with the codebase structure

Then BEFORE creating any coder tasks:
a) Register a scout task via `update_state` (add_task)
b) Delegate it with `role="scout"` to a single Actor
c) Use the Scout's context_summaries as input when creating coder tasks

### 3. Decompose into subtask PAIRS
For every code-modification task you create:
a) Register a **coder task** via `update_state` (add_task) with `role="coder"`
b) Register a **verifier task** via `update_state` (add_task) with:
   - `role="verifier"`
   - `dependencies: [<coder_task_id>]` — verifier waits for coder to complete

Example:
```json
[
  {"action": "add_task", "description": "Implement calculator.py with add/subtract/multiply/divide"},
  {"action": "add_task", "description": "Verify calculator.py: write pytest and run", "dependencies": ["task_abc123"]}
]
```

### 4. Delegate in phases
- Group independent **coder** subtasks into one `delegate` call for maximum concurrency
- After coders complete, delegate **verifier** subtasks
- Inject only essential context into each Actor — less noise = better results
- For coders, inject Scout's context_summaries so they can jump directly to target files

### 5. Verify and close the loop
After verifier Actors complete:
- If all pass → proceed to merge (step 6)
- If any verifier returns `failed`:
  a) Read the verifier's key_findings — it contains the full traceback
  b) Analyze: is it a code bug or a test bug?
  c) Create a **fix task** (role="coder") with the error context injected via context_summaries
  d) Create a new verifier task dependent on the fix task
  e) Delegate both
  f) Maximum 2 retry rounds — if still failing, report the traceback to the user and ask for guidance

### 6. Merge successful changes
- Review each successful Actor's diff
- Apply patches with `apply_patch`
- Follow the Conflict Resolution SOP for any merge conflicts

### 7. Synthesize final response
Summarize what was done, which files were changed, and test results.

## Tools
- **update_state**: Maintain the task tree and record Actor summaries.
- **delegate**: Dispatch subtasks to Actors for concurrent execution in isolated worktrees.
  Supports `role` field: "scout" (explore), "coder" (implement), "verifier" (test).
- **apply_patch**: Apply an Actor's diff back to the main workspace. Use after delegate.
- **list_dir**: Explore project structure.
- **search_codebase**: Locate symbols, classes, functions, or text patterns.
- **read_outline**: View skeleton structure of large files.

## Rules
- Register tasks via `update_state` BEFORE delegating them.
- Group independent subtasks into a single `delegate` call for maximum concurrency.
- Always create verifier tasks paired with coder tasks (DAG: verifier depends on coder).
- Inject only essential context into each Actor — less noise = better results.
- When delegate completes, review each Actor's diff and apply patches with apply_patch.
- When a verifier fails, analyze the traceback before spawning a fix Actor.
- Prefer reading outlines before reading full files when scoping a task.
- For large projects, ALWAYS start with a Scout Actor.

## Conflict Resolution SOP
When apply_patch reports a conflict, follow this exact procedure:

1. **Do NOT retry the same diff** — read the error to understand what conflicted.
2. Create a new task via update_state: "Resolve merge conflict in <filename>"
3. Delegate this task to a single Actor. Inject context:
   - The conflicting file paths as context_files
   - The original diff and git error details as context_summaries
   - Instruction: "Read the files, understand both sides, manually merge, produce a clean diff."
4. Apply the resolution Actor's diff with apply_patch.
5. If resolution also fails, retry ONCE with strategy='fuzz'.
6. **After 2 failed resolution attempts for the same original task, STOP** —
   explain the conflict to the user and ask for guidance.
"""
```

**Step 5: Verify all prompts are syntactically valid**

Run: `cd E:/huguotao7412/simple_coding_agent && python -c "from core.system_prompt import PLANNER_SYSTEM_PROMPT, ACTOR_SYSTEM_PROMPT, SCOUT_SYSTEM_PROMPT, VERIFIER_SYSTEM_PROMPT; print('PLANNER:', len(PLANNER_SYSTEM_PROMPT), 'chars'); print('ACTOR:', len(ACTOR_SYSTEM_PROMPT), 'chars'); print('SCOUT:', len(SCOUT_SYSTEM_PROMPT), 'chars'); print('VERIFIER:', len(VERIFIER_SYSTEM_PROMPT), 'chars')"`
Expected: All four lengths printed with no errors.

**Step 6: Commit**

```bash
git add core/system_prompt.py
git commit -m "feat: add Scout/Verifier system prompts, rewrite Planner for three-phase workflow"
```

---

### Task 7: Refactor `core/tools/delegate.py` — Role Dispatch, TaskGroup, Failure Propagation

**Files:**
- Modify: `core/tools/delegate.py` (multiple sections)

This is the most complex task. Break into sub-steps.

**Step 1: Add imports**

At the top of the file, add after existing imports (after line 11):

```python
from ..role_config import ActorRole, get_role_config
```

**Step 2: Extend subtask schema with `role` and `max_steps` fields**

In `DelegateTool.parameters["subtasks"]["items"]["properties"]` (around line 31-49), add two new fields after `"context_summaries"`:

```python
"role": {
    "type": "string",
    "enum": ["scout", "coder", "verifier"],
    "description": "Actor role: scout (read-only explore), coder (implement), verifier (test).",
    "default": "coder",
},
"max_steps": {
    "type": "integer",
    "description": "Override the role's default max_steps. Use for complex tasks needing more steps.",
},
```

**Step 3: Modify `run_one()` to dispatch by role**

Replace the section that creates `ActorAgent` (lines 179-187) with role-aware dispatch:

```python
# --- 5. Build ActorAgent with role-based configuration ---
role_str = subtask.get("role", "coder")
try:
    role = ActorRole(role_str)
except ValueError:
    role = ActorRole.CODER
role_cfg = get_role_config(role)
max_steps = subtask.get("max_steps", role_cfg.default_max_steps)

actor_ctx = ContextManager(
    system_prompt=role_cfg.system_prompt,
    max_tokens=self._llm.max_tokens,
)
actor_ctx.add_user_message(injected_context)

actor = ActorAgent(
    llm_client=self._llm,
    context_manager=actor_ctx,
    tools=None,      # MCP mode — no local tools
    tool_provider=tool_provider,
    workspace_dir=wt_path,
    actor_id=tid,
    task_context=description,
    max_steps=max_steps,
)
```

Also update the MCP startup call (line 148) to pass tool_allowlist:

```python
await tool_provider.start(wt_path, tool_allowlist=role_cfg.tool_allowlist)
```

**Step 4: Enrich failure result for verifier traceback propagation**

In `run_one()`, update the failure return (around line 208-214) to include more context. Change `key_findings` truncation from 500 to 2000 chars:

```python
return {
    "task_id": tid,
    "status": summary.status,
    "files_modified": summary.files_modified,
    "bugs_found": summary.bugs_found,
    "key_findings": (summary.key_findings or "")[:2000],  # was 500
    "suggested_next_steps": summary.suggested_next_steps,
    "diff": diff[:8000],
}
```

**Step 5: Replace `asyncio.gather` with `asyncio.TaskGroup`**

Replace lines 282-285:
```python
batch_results = await asyncio.gather(
    *[run_one(st) for st in ready.values()],
    return_exceptions=True,
)
```

With:
```python
batch_results: list[dict] = []
async with asyncio.TaskGroup() as tg:
    tasks_map = {
        st["task_id"]: tg.create_task(run_one(st))
        for st in ready.values()
    }
for tid, task in tasks_map.items():
    try:
        batch_results.append(task.result())
    except Exception as e:
        logger.error("run_one crashed for %s: %s", tid, e)
        batch_results.append({
            "task_id": tid,
            "status": "failed",
            "error": f"Fatal actor error: {str(e)}",
        })
```

**Step 6: Update result processing to handle the new task map**

The existing loop (lines 287-299) iterates over `batch_results`. With TaskGroup, the loop needs to handle the exception guard that's now in the `except` clause above. The existing guard:

```python
if isinstance(r, BaseException):
    logger.error(...)
    continue
```

Can now be removed since TaskGroup's `try/except` already catches exceptions. Simplify to:

```python
for r in batch_results:
    all_results.append(r)
    if r["status"] == "done":
        completed.add(r["task_id"])
    else:
        failed.add(r["task_id"])
    if r["task_id"] in remaining:
        del remaining[r["task_id"]]
```

**Step 7: Verify syntax and imports**

Run: `cd E:/huguotao7412/simple_coding_agent && python -c "from core.tools.delegate import DelegateTool; print('OK')"`
Expected: `OK`

**Step 8: Commit**

```bash
git add core/tools/delegate.py
git commit -m "feat: add role dispatch, TaskGroup concurrency, and verifier traceback propagation to delegate"
```

---

### Task 8: Update `cli/ui.py` — New Status Styles

**Files:**
- Modify: `cli/ui.py:57-62` (status_styles dict)

**Step 1: Add `verifying` and `blocked` to status_styles**

Change the `status_styles` dict from:
```python
status_styles = {
    "pending":  ("..", "dim yellow"),
    "running":  (">>", "bold cyan"),
    "done":     ("OK", "bold green"),
    "failed":   ("!!", "bold red"),
}
```

To:
```python
status_styles = {
    "pending":    ("..", "dim yellow"),
    "running":    (">>", "bold cyan"),
    "verifying":  ("\U0001F50D", "bold magenta"),  # 🔍
    "done":       ("OK", "bold green"),
    "failed":     ("!!", "bold red"),
    "blocked":    ("\U0001F6AB", "dim"),            # 🚫
}
```

**Step 2: Verify**

Run: `cd E:/huguotao7412/simple_coding_agent && python -c "from cli.ui import UI; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add cli/ui.py
git commit -m "feat: add verifying and blocked status styles to CLI task table"
```

---

### Task 9: Update `web/components/sidebar.py` — New Status Icons

**Files:**
- Modify: `web/components/sidebar.py:56` (status_icon dict)

**Step 1: Add `verifying` and `blocked` to status_icon**

Change the `status_icon` dict from:
```python
status_icon = {"pending": "⏳", "running": "🔄", "done": "✅", "failed": "❌"}
```

To:
```python
status_icon = {
    "pending": "⏳",
    "running": "🔄",
    "verifying": "🔍",
    "done": "✅",
    "failed": "❌",
    "blocked": "🚫",
}
```

**Step 2: Verify**

Run: `cd E:/huguotao7412/simple_coding_agent && python -c "from web.components.sidebar import render_sidebar; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add web/components/sidebar.py
git commit -m "feat: add verifying and blocked status icons to web sidebar"
```

---

### Task 10: Integration Validation

**Files:**
- Test: `tests/test_role_config.py` (new)

**Step 1: Write integration test for role configuration**

Create `tests/test_role_config.py`:

```python
"""Integration tests for the role configuration system."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_all_roles_have_valid_config():
    """Every ActorRole must have a corresponding RoleConfig."""
    from core.role_config import ActorRole, get_role_config

    for role in ActorRole:
        cfg = get_role_config(role)
        assert cfg.system_prompt, f"{role} has empty system_prompt"
        assert cfg.default_max_steps > 0, f"{role} has non-positive max_steps"
        # tool_allowlist can be None (all tools) or a non-empty set
        if cfg.tool_allowlist is not None:
            assert len(cfg.tool_allowlist) > 0, f"{role} has empty tool_allowlist"


def test_scout_is_read_only():
    """Scout must NOT have write/edit/bash in its allowlist."""
    from core.role_config import ActorRole, get_role_config

    scout_cfg = get_role_config(ActorRole.SCOUT)
    assert scout_cfg.tool_allowlist is not None, "Scout must have explicit tool allowlist"
    forbidden = {"write", "edit", "bash"}
    assert not (scout_cfg.tool_allowlist & forbidden), \
        f"Scout allowlist contains forbidden tools: {scout_cfg.tool_allowlist & forbidden}"


def test_verifier_has_bash():
    """Verifier must have bash for running tests."""
    from core.role_config import ActorRole, get_role_config

    verifier_cfg = get_role_config(ActorRole.VERIFIER)
    assert verifier_cfg.tool_allowlist is not None, "Verifier must have explicit tool allowlist"
    assert "bash" in verifier_cfg.tool_allowlist, "Verifier needs bash to run tests"
    assert "read" in verifier_cfg.tool_allowlist, "Verifier needs read to inspect code"


def test_coder_has_full_access():
    """Coder should have no tool restrictions (allowlist=None)."""
    from core.role_config import ActorRole, get_role_config

    coder_cfg = get_role_config(ActorRole.CODER)
    assert coder_cfg.tool_allowlist is None, "Coder must have full tool access"


def test_tasknode_supports_verifying_status():
    """TaskNode must accept 'verifying' as a valid status."""
    from core.state import TaskNode

    task = TaskNode(task_id="test", description="test", status="verifying")
    assert task.status == "verifying"


def test_actor_accepts_max_steps():
    """ActorAgent must accept and store max_steps."""
    from core.agent import ActorAgent
    # We can't fully instantiate without LLM client, but we can check the signature
    import inspect
    sig = inspect.signature(ActorAgent.__init__)
    assert "max_steps" in sig.parameters, "ActorAgent.__init__ missing max_steps parameter"


def test_planner_accepts_max_steps():
    """Planner must accept and store max_steps."""
    from core.planner import Planner
    import inspect
    sig = inspect.signature(Planner.__init__)
    assert "max_steps" in sig.parameters, "Planner.__init__ missing max_steps parameter"
```

**Step 2: Run the integration tests**

Run: `cd E:/huguotao7412/simple_coding_agent && python -m pytest tests/test_role_config.py -v`
Expected: All 7 tests PASS

**Step 3: Commit**

```bash
git add tests/test_role_config.py
git commit -m "test: add integration tests for role configuration and new statuses"
```

---

## Summary

| Task | Files | Estimated Time |
|------|-------|---------------|
| 1 | `core/role_config.py` (new) | 10 min |
| 2 | `core/state.py` (1 line) | 5 min |
| 3 | `core/agent.py` (3 lines) | 5 min |
| 4 | `core/planner.py` (3 lines) | 5 min |
| 5 | `core/mcp/client.py` (8 lines) | 10 min |
| 6 | `core/system_prompt.py` (~80 lines) | 15 min |
| 7 | `core/tools/delegate.py` (~40 lines) | 20 min |
| 8 | `cli/ui.py` (4 lines) | 5 min |
| 9 | `web/components/sidebar.py` (4 lines) | 5 min |
| 10 | `tests/test_role_config.py` (new) + validation | 10 min |

**Total estimated: ~90 minutes for a single engineer.**

Tasks 1-6 can be done in parallel. Task 7 depends on 1-6. Tasks 8-9 are independent. Task 10 is the final gate.
