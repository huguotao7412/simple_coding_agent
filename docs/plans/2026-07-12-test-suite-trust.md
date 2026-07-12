# Test Suite Trust Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make accidental test-suite deletion fail clearly and prevent generated eval workspaces from being collected as project tests.

**Architecture:** Keep the protection at the repository boundary: pytest owns collection exclusions, a small configuration regression test locks those exclusions in place, and CI checks the test inventory before running pytest. No runtime or agent behavior changes are included.

**Tech Stack:** Python 3.12, pytest, TOML, GitHub Actions

---

### Task 1: Add quality-gate regression tests

**Files:**
- Create: `tests/test_project_quality_gates.py`

**Step 1: Write a test that loads `pyproject.toml` with `tomllib` and requires `tmp` in `tool.pytest.ini_options.norecursedirs`.**

**Step 2: Write a test that reads `.github/workflows/ci.yml` and requires a named test-inventory gate before the unit-test step.**

**Step 3: Run the new test module.**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/test_project_quality_gates.py -q`

Expected: two failures because neither guard exists yet.

### Task 2: Implement repository test guards

**Files:**
- Modify: `pyproject.toml`
- Modify: `.github/workflows/ci.yml`

**Step 1: Add `tmp` to pytest `norecursedirs`.**

**Step 2: Add a CI step named `Verify test suite inventory` before `Run unit tests`.**

The step must use Python's standard library to assert that `tests/` exists and contains at least eight `test_*.py` modules, with an explicit failure message.

**Step 3: Run the quality-gate tests.**

Run: `..\..\.venv\Scripts\python.exe -m pytest tests/test_project_quality_gates.py -q`

Expected: two passing tests.

### Task 3: Verify the complete P0 stage

**Files:**
- Verify: `tests/`
- Verify: `core/`, `cli/`, `web/`, `evals/`

**Step 1: Run all unit tests.**

Run: `..\..\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest_cache/tmp`

Expected: all tests pass and no files under `tmp/eval-runs` are collected.

**Step 2: Run strict type checking for the trusted runtime boundary.**

Run: `..\..\.venv\Scripts\python.exe -m mypy core/policy.py core/events.py core/run_context.py core/runtime.py core/planner.py core/agent.py core/mcp/client.py core/tools/update_state.py core/tools/delegate.py cli/report.py evals/run_evals.py`

Expected: success with no issues.

**Step 3: Compile-check all Python modules.**

Run: `..\..\.venv\Scripts\python.exe -m compileall -q core cli web evals tests`

Expected: exit code 0.

**Step 4: Review the diff and stop for user feedback before committing or starting P1.**
