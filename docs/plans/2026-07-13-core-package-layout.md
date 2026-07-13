# Core Package Layout Refactor Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the flat `core/` module list with cohesive `runtime`, `runs`, and `actors` packages while preserving behavior and enforcing canonical dependency directions.

**Architecture:** `core.runtime` owns the model/tool execution loop and conversation context. `core.runs` owns durable-run and task-state models plus persistence. `core.actors` owns Actor behavior, role configuration, executor contracts, and the worktree adapter. The cross-domain event protocol, application orchestration, and infrastructure remain at the `core` root.

**Tech Stack:** Python 3.12 packages, AST/import inspection, pytest, mypy, compileall, Git

---

### Task 1: Lock the target package boundaries

**Files:**
- Create: `tests/test_core_package_layout.py`
- Create: `core/runtime/__init__.py`
- Create: `core/runs/__init__.py`
- Create: `core/actors/__init__.py`

**Step 1: Add a failing layout regression test**

Require the three package directories and reject the legacy flat modules after migration. Parse project Python imports and reject imports from the removed legacy module paths.

**Step 2: Run the focused test**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_core_package_layout.py -q`

Expected: FAIL because the target packages do not exist and flat modules remain.

**Step 3: Add package initializers**

Keep `__init__.py` files intentionally small. Do not re-export whole subpackages or create compatibility modules that hide the canonical dependency graph.

### Task 2: Move runtime and run-lifecycle modules

**Files:**
- Move: `core/runtime.py` -> `core/runtime/engine.py`
- Move: `core/context.py` -> `core/runtime/conversation.py`
- Keep: `core/events.py` as a cross-domain event contract
- Move: `core/run_context.py` -> `core/runs/context.py`
- Move: `core/run_state.py` -> `core/runs/models.py`
- Move: `core/run_store.py` -> `core/runs/store.py`
- Move: `core/sqlite_run_store.py` -> `core/runs/sqlite_store.py`
- Move: `core/state.py` -> `core/runs/task_state.py`
- Modify: all Python consumers under `core/`, `cli/`, `web/`, `evals/`, and `tests/`

**Step 1: Move files without changing behavior**

Use canonical imports such as `core.runtime.engine`, `core.runtime.events`, `core.runs.context`, and `core.runs.store`. Relative imports inside packages may only point to a sibling or explicit parent package; no wildcard re-export layer.

**Step 2: Run focused runtime/run tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py tests/test_run_context.py tests/test_run_state.py tests/test_sqlite_run_store.py tests/test_cli_runs.py -q`

Expected: all focused tests pass.

### Task 3: Move Actor modules and validate dependency direction

**Files:**
- Move: `core/agent.py` -> `core/actors/agent.py`
- Move: `core/actor_execution.py` -> `core/actors/contracts.py`
- Move: `core/role_config.py` -> `core/actors/roles.py`
- Move: `core/worktree_actor_executor.py` -> `core/actors/worktree.py`
- Modify: `core/tools/delegate.py`
- Modify: Actor imports in tests and UI components

**Step 1: Move the Actor implementation and update imports**

`core.actors.contracts` must depend only on `core.runs.context`. `core.actors.worktree` may depend on contracts, roles, runs, policy, Git utilities, and lazily on `core.actors.agent`. Tools may depend on Actor contracts/adapters, but Actor modules must not import Planner tools as a package-level orchestration dependency.

**Step 2: Run Actor, delegate, MCP, and runtime tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_actor_execution.py tests/test_worktree_actor_executor.py tests/test_delegate_scheduler.py tests/test_delegate_baseline.py tests/test_mcp_provider.py tests/test_runtime.py -q`

Expected: all focused tests pass.

### Task 4: Update trusted boundaries and current documentation

**Files:**
- Modify: `pyproject.toml`
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`
- Modify: `README_CN.md`
- Modify: `architecture.md`
- Modify: `architecture_CN.md`
- Modify: `tests/test_project_quality_gates.py`

**Step 1: Replace trusted mypy paths**

Point mypy and CI at the canonical package paths. Historical implementation plans remain unchanged because their paths describe the repository at the time they were executed.

**Step 2: Update current layout and architecture documents**

Document package ownership and dependency direction. Remove current references to legacy flat paths.

**Step 3: Run import and quality checks**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_core_package_layout.py tests/test_project_quality_gates.py -q`

Expected: all layout and repository-boundary tests pass.

### Task 5: Final verification, review, merge, and cleanup

**Step 1: Run the complete test suite**

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Expected: all tests pass.

**Step 2: Run strict type checking**

Run: `.\.venv\Scripts\python.exe -m mypy`

Expected: success with no issues.

**Step 3: Compile and smoke-check imports**

Run: `.\.venv\Scripts\python.exe -m compileall -q core cli web evals tests`

Run: `.\.venv\Scripts\python.exe -c "from core.runtime.engine import AgentRuntime; from core.runs.context import RunContext; from core.actors.contracts import ActorExecutor"`

Expected: exit code 0.

**Step 4: Review for legacy imports and whitespace errors**

Run: `rg -n "core\.(runtime|context|events|run_context|run_state|run_store|sqlite_run_store|state|agent|actor_execution|role_config|worktree_actor_executor)" core cli web evals tests pyproject.toml .github`

Expected: no legacy module imports or trusted-path references.

Run: `git diff --check`

Expected: exit code 0.

**Step 5: Commit, push, fast-forward merge to master, and delete the merged feature branch**

Commit message: `refactor: organize core into cohesive packages`
