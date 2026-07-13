# Durable Runs P1 Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Persist coding-agent runs at safe checkpoints so interrupted runs can be inspected and resumed without replaying completed tool calls.

**Architecture:** Add an explicit run state machine and a `RunStore` port with a standard-library SQLite adapter. `RunContext` remains the run-scoped composition object, while `AgentRuntime` saves a complete conversation/task/usage checkpoint after state-changing boundaries. CLI run-management commands load the checkpoint and reconstruct the existing Planner rather than introducing a second execution path.

**Tech Stack:** Python 3.12, asyncio, sqlite3, dataclasses, argparse, pytest, mypy

---

### Task 1: Durable run domain model

**Files:**
- Create: `core/run_state.py`
- Test: `tests/test_run_state.py`

**Step 1: Write failing transition tests**

Cover the legal lifecycle `created -> running -> paused/completed/failed`, resumption from `paused` or `failed`, terminal-state rejection, and optimistic version increments.

**Step 2: Run the focused test**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_run_state.py -q`

Expected: FAIL because `core.run_state` does not exist.

**Step 3: Implement immutable run records and transitions**

Add `RunStatus`, `RunRecord`, `RunCheckpoint`, `RunTransitionError`, and a pure `transition_run()` function. Store messages, task snapshot, usage, workspace/model metadata, the last completed tool-call IDs, timestamps, error text, and an integer version.

**Step 4: Run the focused test**

Expected: all tests in `tests/test_run_state.py` pass.

### Task 2: RunStore port and SQLite adapter

**Files:**
- Create: `core/run_store.py`
- Create: `core/sqlite_run_store.py`
- Test: `tests/test_sqlite_run_store.py`

**Step 1: Write failing persistence tests**

Cover create/load, ordered listing, checkpoint replacement, optimistic-version conflict, event append ordering, separate database instances, and corrupt checkpoint JSON.

**Step 2: Run the focused test**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_sqlite_run_store.py -q`

Expected: FAIL because the persistence modules do not exist.

**Step 3: Define the port and implement SQLite**

Define an async `RunStore` protocol. Implement SQLite calls behind `asyncio.to_thread`, enable WAL and foreign keys, use explicit transactions, version-checked updates, JSON payloads, and schema creation that is safe to call repeatedly. Keep large patch artifacts outside the database.

**Step 4: Run the focused test**

Expected: all tests in `tests/test_sqlite_run_store.py` pass.

**Step 5: Commit the first independently useful slice**

Commit message: `feat: add durable run store`

### Task 3: Checkpoint and restore run-scoped state

**Files:**
- Modify: `core/context.py`
- Modify: `core/state.py`
- Modify: `core/run_context.py`
- Test: `tests/test_run_context.py`

**Step 1: Write failing round-trip tests**

Cover restoring conversation messages, result-deduplication hashes, task DAG/status/diff references, usage totals, run metadata, and completed tool-call IDs.

**Step 2: Run the focused test**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_run_context.py -q`

Expected: new round-trip tests fail.

**Step 3: Add serialization boundaries**

Add `ContextManager.restore_messages()`, `GlobalState.from_snapshot()`, and `RunContext.from_checkpoint()`. Add `RunContext.checkpoint()` and version-checked persistence methods without making SQLite a dependency of domain modules.

**Step 4: Run the focused test**

Expected: all run-context tests pass.

### Task 4: Runtime safe checkpoints and tool-call idempotency

**Files:**
- Modify: `core/runtime.py`
- Modify: `core/planner.py`
- Test: `tests/test_runtime.py`

**Step 1: Write failing interruption tests**

Simulate cancellation after a tool result is persisted, reconstruct the runtime, and assert the same provider tool-call ID is not executed again. Verify checkpoints after user input, assistant tool-call messages, tool results, compaction, terminal completion, and error.

**Step 2: Run the focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_runtime.py -q`

Expected: new checkpoint/idempotency tests fail.

**Step 3: Add safe checkpoint hooks**

Persist only complete message boundaries. Before executing a tool, consult completed tool-call IDs; when already completed, reuse its persisted tool observation. After execution, persist the observation and completed ID atomically in one checkpoint. Mark interrupted nonterminal runs as paused.

**Step 4: Run the focused tests**

Expected: runtime tests pass with exactly-once behavior at the checkpoint boundary.

### Task 5: CLI run lifecycle

**Files:**
- Modify: `cli/main.py`
- Create: `cli/runs.py`
- Test: `tests/test_cli_runs.py`

**Step 1: Write failing parser and command tests**

Cover `sca runs`, `sca inspect <run-id>`, `sca resume <run-id>`, unknown IDs, terminal runs, workspace mismatch, and operation without `SCA_API_KEY` for read-only commands.

**Step 2: Run the focused test**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_cli_runs.py -q`

Expected: FAIL because run-management commands are absent.

**Step 3: Implement CLI composition**

Default the database to `<workspace>/.sca/runs.db`. Create the run before the first model call, load persisted context for resume, append a short recovery instruction rather than duplicating the original user message, and render deterministic text/JSON-friendly summaries.

**Step 4: Run the focused test**

Expected: CLI run tests pass.

**Step 5: Commit the executable recovery slice**

Commit message: `feat: resume interrupted agent runs`

### Task 6: Failure hardening, architecture record, and quality gates

**Files:**
- Create: `docs/adr/0002-durable-run-store.md`
- Modify: `architecture.md`
- Modify: `architecture_CN.md`
- Modify: `README.md`
- Modify: `README_CN.md`
- Modify: `pyproject.toml`
- Test: `tests/test_project_quality_gates.py`

**Step 1: Add recovery edge-case tests**

Cover cancellation, corrupt checkpoint payloads, stale versions, repeated resume, missing workspace, and SQLite initialization under concurrent access.

**Step 2: Document the decision**

Record alternatives: memory plus JSONL, SQLite plus file artifacts, and an external workflow/database service. Document transaction boundaries and the remaining limitation that external side effects cannot be made globally exactly-once.

**Step 3: Extend strict type-check scope**

Add the new trusted runtime modules to `pyproject.toml` and CI's mypy command.

**Step 4: Run complete verification**

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Expected: all tests pass.

Run: `.\.venv\Scripts\python.exe -m mypy core/actor_execution.py core/policy.py core/events.py core/run_state.py core/run_store.py core/sqlite_run_store.py core/run_context.py core/runtime.py core/worktree_actor_executor.py core/planner.py core/agent.py core/mcp/client.py core/tools/update_state.py core/tools/delegate.py cli/report.py cli/runs.py evals/run_evals.py`

Expected: success with no issues.

Run: `.\.venv\Scripts\python.exe -m compileall -q core cli web evals tests`

Expected: exit code 0.

**Step 5: Commit and push**

Commit message: `docs: define durable run recovery boundary`

Push branch: `git push -u origin codex/durable-runs-p1`
