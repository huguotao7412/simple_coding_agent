# Actor Executor Boundary Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Separate DAG scheduling from the infrastructure-heavy lifecycle of executing one Actor task.

**Architecture:** `DelegateTool` remains the application-level scheduler and owns task-state transitions, dependency blocking, concurrency, and result aggregation. A new `ActorExecutor` port accepts an immutable `ActorTaskSpec` and returns an `ActorExecutionResult`; `WorktreeActorExecutor` is the default adapter that owns context injection, worktree setup, dependency baselines, MCP startup/shutdown, Actor execution, diff extraction, artifact persistence, and cleanup.

**Tech Stack:** Python 3.12, asyncio, dataclasses, typing Protocol, pytest, Git worktrees, MCP

---

### Task 1: Define the execution port and value objects

**Files:**
- Create: `core/actor_execution.py`
- Create: `tests/test_actor_execution.py`

**Step 1: Write failing tests for immutable `ActorTaskSpec`, structured `ActorExecutionResult`, and runtime-checkable `ActorExecutor`.**

The spec must contain `task_id`, `description`, `context_files`, `context_summaries`, `role`, `max_steps`, and `dependencies`. The result must contain status, error, findings, modified files, diff artifact, and full diff.

**Step 2: Run the focused tests.**

Run: `python -m pytest tests/test_actor_execution.py -q`

Expected: import failure because `core.actor_execution` does not exist.

**Step 3: Implement the dataclasses, status literal, and protocol without importing Git, MCP, or concrete Actor classes.**

**Step 4: Run the focused tests again.**

Expected: all tests pass.

### Task 2: Extract the worktree execution adapter

**Files:**
- Create: `core/worktree_actor_executor.py`
- Create: `tests/test_worktree_actor_executor.py`
- Modify: `tests/test_delegate_baseline.py`
- Modify: `core/tools/delegate.py`

**Step 1: Move dependency baseline application and diff-artifact persistence into `core/worktree_actor_executor.py`; update existing helper tests to import from the new module.**

**Step 2: Write a failing lifecycle test using fake provider and Actor factories.**

The test must prove startup order, structured result mapping, diff extraction, and MCP/worktree cleanup without starting real MCP processes.

Add a path-containment regression proving that `context_files` cannot read or copy files outside the main workspace or Actor worktree before MCP policy enforcement.

**Step 3: Implement `WorktreeActorExecutor.execute(spec, run_context)` with cleanup in `finally`.**

Worktree setup or MCP startup failures must return `ActorExecutionResult(status="failed")`; cleanup failures must be logged without replacing the primary result.

**Step 4: Run focused tests.**

Run: `python -m pytest tests/test_actor_execution.py tests/test_worktree_actor_executor.py tests/test_delegate_baseline.py -q`

Expected: all focused tests pass.

### Task 3: Make DelegateTool an executor-driven scheduler

**Files:**
- Modify: `core/tools/delegate.py`
- Create: `tests/test_delegate_scheduler.py`

**Step 1: Write scheduler tests with a fake `ActorExecutor`.**

Cover dependency order, failed-dependency blocking, sibling-result preservation after an executor exception, and state/result-summary updates. Tests must not create Git worktrees, MCP processes, or real LLM calls.

**Step 2: Add optional `actor_executor` injection to `DelegateTool`.**

If no executor is injected, lazily construct `WorktreeActorExecutor` from the Planner-injected LLM, workspace, and run context. Preserve current construction by `PLANNER_TOOLS`.

**Step 3: Replace nested `run_one` lifecycle logic with semaphore-guarded calls to the executor.**

Keep input validation, DAG readiness, dependency failure propagation, concurrent gather semantics, task-state transitions, summary recording, and user-facing output in `DelegateTool`.

**Step 4: Run scheduler and regression tests.**

Run: `python -m pytest tests/test_delegate_scheduler.py tests/test_delegate_baseline.py tests/test_runtime.py -q`

Expected: all tests pass.

### Task 4: Record the architecture decision and trusted boundary

**Files:**
- Create: `docs/adr/0001-actor-executor-boundary.md`
- Modify: `architecture.md`
- Modify: `architecture_CN.md`
- Modify: `pyproject.toml`

**Step 1: Add an ADR with Context, Alternatives, Decision, and Consequences.**

Alternatives must include keeping `DelegateTool` monolithic, extracting only helper functions, and selecting the executor port/adapter boundary.

**Step 2: Update architecture documents with the new scheduler/executor ownership split.**

**Step 3: Add the new trusted modules to the documented and CI mypy boundary.**

**Step 4: Run mypy over the complete trusted boundary.**

Expected: success with no issues.

### Task 5: Complete P1 verification and integration

**Files:**
- Verify: `core/`, `tests/`, `.github/workflows/ci.yml`, architecture documents

**Step 1: Run the full unit-test suite.**

Run: `python -m pytest -q`

Expected: all tests pass.

**Step 2: Run strict mypy and compileall checks.**

Expected: both succeed.

**Step 3: Review `git diff --check` and confirm `DelegateTool` no longer imports worktree, MCP, Actor, context, shutil, tempfile, or subprocess infrastructure.**

**Step 4: Commit the P1 branch and stop for review before merging to `master`.**
