# Verification and Repair Loop Implementation Plan

> **For Codex:** Implement task-by-task with focused tests before production code.

**Goal:** Make coder tasks prove their changes with deterministic project-defined quality gates and automatically repair bounded failures before a diff is accepted.

**Architecture:** Add a dependency-light `core.verification` package containing immutable gate models, strict TOML configuration loading, and an async subprocess runner. `WorktreeActorExecutor` owns the orchestration boundary: the coder edits only inside its isolated worktree, gates execute there without a shell, failures are returned to the same actor as structured repair context, and the final diff is exported only after gates pass. Missing configuration preserves current behavior.

**Tech Stack:** Python 3.12, asyncio subprocesses, tomllib, dataclasses, pytest, mypy

---

### Task 1: Quality-gate domain model and configuration

**Files:**
- Create: `core/verification/__init__.py`
- Create: `core/verification/models.py`
- Create: `core/verification/config.py`
- Test: `tests/test_verification_config.py`

**Step 1: Write failing configuration tests**

Cover a missing `.sca/quality-gates.toml`, valid ordered gates, repair-attempt configuration, duplicate names, string commands, empty arguments, invalid timeouts, and unknown top-level fields.

**Step 2: Run the focused test**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_verification_config.py -q`

Expected: FAIL because `core.verification` does not exist.

**Step 3: Implement strict immutable models and loader**

Define `GateSpec`, `GateResult`, `VerificationReport`, and `VerificationConfig`. Parse with `tomllib`, require argv arrays rather than shell strings, preserve gate order, reject malformed or ambiguous input, and return a disabled configuration when the file is absent.

**Step 4: Run the focused test**

Expected: all configuration tests pass.

### Task 2: Deterministic gate runner and evidence artifacts

**Files:**
- Create: `core/verification/runner.py`
- Test: `tests/test_verification_runner.py`

**Step 1: Write failing runner tests**

Cover success, nonzero exit, timeout, execution in the supplied worktree, `{python}` expansion, ordered execution, output truncation, and complete log artifact creation.

**Step 2: Run the focused test**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_verification_runner.py -q`

Expected: FAIL because the runner does not exist.

**Step 3: Implement shell-free execution**

Use `asyncio.create_subprocess_exec` with the worktree as `cwd`, bounded timeouts, process termination on timeout, captured stdout/stderr, and durable logs under `.sca/artifacts/verification/<task>/<attempt>/`. Never interpolate a command string or invoke a shell.

**Step 4: Run the focused test**

Expected: all runner tests pass.

**Step 5: Commit the independently useful verification slice**

Commit message: `feat: add deterministic quality gate runner`

### Task 3: Worktree verification boundary

**Files:**
- Modify: `core/actors/contracts.py`
- Modify: `core/actors/worktree.py`
- Modify: `core/tools/delegate.py`
- Test: `tests/test_worktree_actor_executor.py`
- Test: `tests/test_delegate_scheduler.py`

**Step 1: Write failing integration tests**

Cover coder success, gate failure, absent configuration, non-coder behavior, evidence propagation, and the invariant that a failing diff is not applied to the primary workspace.

**Step 2: Run focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_worktree_actor_executor.py tests/test_delegate_scheduler.py -q`

Expected: new integration tests fail.

**Step 3: Inject and execute verification**

Load project gates once per task, run them after a successful coder turn, attach the report to `ActorExecutionResult`, expose compact evidence through delegation, and fail closed when configured required gates do not pass.

**Step 4: Run focused tests**

Expected: actor and delegation tests pass.

### Task 4: Bounded repair loop and no-progress detection

**Files:**
- Create: `core/verification/repair.py`
- Modify: `core/actors/worktree.py`
- Test: `tests/test_verification_repair.py`
- Test: `tests/test_worktree_actor_executor.py`

**Step 1: Write failing repair tests**

Cover failure followed by success, attempt exhaustion, repeated failure fingerprints, actor failure during repair, compact prompts, and exact attempt accounting.

**Step 2: Run focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_verification_repair.py tests/test_worktree_actor_executor.py -q`

Expected: new repair tests fail.

**Step 3: Implement the loop**

Build a structured repair prompt from failed gate evidence, call the same actor context, rerun gates deterministically, stop at the configured budget, and terminate early when the normalized failure fingerprint repeats. Export a diff only after a passing report.

**Step 4: Run focused tests**

Expected: repair-loop tests pass.

### Task 5: Documentation, typing boundary, and full regression

**Files:**
- Create: `docs/adr/0003-deterministic-verification-boundary.md`
- Modify: `architecture.md`
- Modify: `architecture_CN.md`
- Modify: `README.md`
- Modify: `README_CN.md`
- Modify: `pyproject.toml`
- Test: `tests/test_project_quality_gates.py`

**Step 1: Document configuration and architectural decision**

Explain the semantic-actor versus deterministic-gate split, isolated execution, artifact paths, bounded retries, no-progress detection, and the trust boundary around project-owned commands.

**Step 2: Extend strict type checking**

Add the verification package and changed actor boundary modules to the strict mypy scope.

**Step 3: Run complete verification**

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Run: `.\.venv\Scripts\python.exe -m mypy core/verification core/actors/contracts.py core/actors/worktree.py core/tools/delegate.py`

Run: `.\.venv\Scripts\python.exe -m compileall -q core cli web evals tests`

Expected: every command exits successfully.

**Step 4: Review and publish the feature branch**

Review the complete diff for package boundaries, unsafe subprocess usage, accidental workspace mutation, and backward compatibility before pushing `codex/verification-pipeline`.
