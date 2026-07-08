# Eval System Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a fixture-based end-to-end eval runner for Simple Coding Agent.

**Architecture:** Eval cases live under `evals/cases/<name>/` with `eval.json` and a `repo/` fixture. The runner copies each fixture into a temporary workspace, optionally runs an agent command template, executes verification commands, and writes JSON and Markdown reports.

**Tech Stack:** Python standard library, pytest for runner tests, JSON case configuration, subprocess-based command execution.

---

### Task 1: Package and Case Model

**Files:**
- Create: `evals/__init__.py`
- Create: `evals/run_evals.py`
- Modify: `pyproject.toml`

**Steps:**
1. Add `evals/__init__.py`.
2. Define dataclasses for `EvalCase`, `CommandResult`, and `CaseResult`.
3. Add JSON loading and validation for `evals/cases/<name>/eval.json`.
4. Include `evals*` in setuptools package discovery.

**Validation:**
- `python -m evals.run_evals --help` exits 0.

### Task 2: Runner Execution

**Files:**
- Modify: `evals/run_evals.py`

**Steps:**
1. Discover all cases or one selected case with `--case`.
2. Copy each `repo/` directory into a temporary workspace.
3. If `--agent-command` is provided and not `--dry-run`, format it with `{workspace}` and `{prompt}` and run it.
4. Run each verification command in the temporary workspace.
5. Record changed files by comparing the fixture source and temp workspace.

**Validation:**
- Dry run writes reports without executing commands.
- Verification failures mark the case failed.

### Task 3: Reports and Sample Cases

**Files:**
- Modify: `evals/run_evals.py`
- Create: `evals/cases/fix_failing_pytest/eval.json`
- Create: `evals/cases/fix_failing_pytest/repo/math_utils.py`
- Create: `evals/cases/fix_failing_pytest/repo/test_math_utils.py`
- Create: `evals/cases/add_cli_argument/eval.json`
- Create: `evals/cases/add_cli_argument/repo/greeter.py`
- Create: `evals/cases/add_cli_argument/repo/test_greeter.py`

**Steps:**
1. Write `evals/reports/latest.json`.
2. Write `evals/reports/latest.md`.
3. Add two minimal eval cases.

**Validation:**
- `python -m evals.run_evals --dry-run` creates both reports.

### Task 4: Tests

**Files:**
- Create: `tests/test_evals_runner.py`

**Steps:**
1. Test case loading.
2. Test dry-run report generation.
3. Test verification-only success and failure behavior with temporary cases.

**Validation:**
- `.venv\Scripts\python.exe -m pytest tests/test_evals_runner.py -v`
