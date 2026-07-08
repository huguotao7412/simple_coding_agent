from __future__ import annotations

import argparse
import asyncio
import filecmp
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = Path(__file__).with_name("tasks.json")
REPORT_PATH = Path(".sca") / "final_report.md"
TRACE_PATH = Path(".sca") / "traces" / "run_trace.jsonl"
IGNORED_DIRS = {"__pycache__", ".git", ".mypy_cache", ".pytest_cache", ".worktrees"}


@dataclass
class EvalResult:
    task_id: str
    passed: bool
    failures: list[str] = field(default_factory=list)


@dataclass
class EvalRunResult:
    task_id: str
    title: str
    model: str | None
    passed: bool
    duration_ms: int
    tool_calls: int
    failed_tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    trace_path: str
    report_path: str
    failures: list[str] = field(default_factory=list)
    final_output: str = ""
    runtime_error: str | None = None


@dataclass
class EvalComparisonRun:
    label: str
    path: str
    total: int
    passed: int
    failed: int
    pass_rate: float
    total_duration_ms: int
    total_tool_calls: int
    failed_tool_calls: int
    total_tokens: int


@dataclass
class EvalComparison:
    baseline: EvalComparisonRun
    runs: list[EvalComparisonRun]
    task_regressions: dict[str, list[str]] = field(default_factory=dict)
    task_improvements: dict[str, list[str]] = field(default_factory=dict)


def load_tasks(path: Path = TASKS_PATH) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate_task(task: dict[str, Any], candidate_root: Path) -> EvalResult:
    task_id = task["id"]
    fixture_dir = REPO_ROOT / "evals" / task["fixture"]
    candidate_dir = candidate_root / task_id
    failures: list[str] = []

    if not candidate_dir.is_dir():
        return EvalResult(task_id, False, [f"missing candidate directory: {candidate_dir}"])

    changed_files = _changed_files(fixture_dir, candidate_dir)
    allowed_files = set(task.get("allowed_files", []))
    extra_files = sorted(changed_files - allowed_files)
    if extra_files:
        failures.append(f"changed files outside allowlist: {', '.join(extra_files)}")

    for requirement in task.get("required_file_contains", []):
        rel_path = Path(requirement["path"])
        text = requirement["text"]
        target = candidate_dir / rel_path
        if not target.is_file():
            failures.append(f"required file missing: {rel_path.as_posix()}")
            continue
        content = target.read_text(encoding="utf-8", errors="replace")
        if text not in content:
            failures.append(f"{rel_path.as_posix()} does not contain required text: {text}")

    report = candidate_dir / REPORT_PATH
    if not report.is_file():
        failures.append(f"missing final report: {REPORT_PATH.as_posix()}")
    else:
        report_text = report.read_text(encoding="utf-8", errors="replace").lower()
        for term in task.get("report_terms", []):
            if term.lower() not in report_text:
                failures.append(f"final report missing term: {term}")

    test_result = _run_test_command(task.get("test_command", []), candidate_dir)
    if test_result.returncode != 0:
        output = (test_result.stdout + "\n" + test_result.stderr).strip()
        failures.append(f"test command failed with exit {test_result.returncode}: {output[:600]}")

    return EvalResult(task_id, not failures, failures)


def evaluate_all(candidate_root: Path, tasks: list[dict[str, Any]] | None = None) -> list[EvalResult]:
    return [evaluate_task(task, candidate_root) for task in (tasks or load_tasks())]


def print_results(results: list[EvalResult]) -> None:
    passed = sum(1 for result in results if result.passed)
    total = len(results)
    print(f"Eval results: {passed}/{total} passed")
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.task_id}")
        for failure in result.failures:
            print(f"  - {failure}")


def write_eval_results(results: list[EvalRunResult], output_path: Path) -> Path:
    """Write aggregate eval run results to a deterministic JSON file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    passed = sum(1 for result in results if result.passed)
    payload = {
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "pass_rate": (passed / len(results)) if results else 0.0,
            "total_duration_ms": sum(result.duration_ms for result in results),
            "total_tool_calls": sum(result.tool_calls for result in results),
            "total_prompt_tokens": sum(result.prompt_tokens for result in results),
            "total_completion_tokens": sum(result.completion_tokens for result in results),
            "total_tokens": sum(result.total_tokens for result in results),
        },
        "tasks": [
            {
                "task_id": result.task_id,
                "title": result.title,
                "model": result.model,
                "passed": result.passed,
                "duration_ms": result.duration_ms,
                "tool_calls": result.tool_calls,
                "failed_tool_calls": result.failed_tool_calls,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "total_tokens": result.total_tokens,
                "trace_path": result.trace_path,
                "report_path": result.report_path,
                "failures": result.failures,
                "final_output": result.final_output,
                "runtime_error": result.runtime_error,
            }
            for result in results
        ],
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


async def run_eval_suite(
    candidate_root: Path,
    model: str | None = None,
    results_path: Path = Path("eval_results.json"),
    prepare: bool = True,
    tasks: list[dict[str, Any]] | None = None,
) -> list[EvalRunResult]:
    """Run the agent against each eval fixture and write aggregate results."""
    selected_tasks = tasks or load_tasks()
    if prepare:
        copy_fixtures(candidate_root)

    results: list[EvalRunResult] = []
    for task in selected_tasks:
        results.append(await run_eval_task(task, candidate_root, model=model))

    write_eval_results(results, results_path)
    return results


async def run_eval_task(
    task: dict[str, Any],
    candidate_root: Path,
    model: str | None = None,
) -> EvalRunResult:
    """Run one eval task through the real Planner and persist its trace."""
    from cli.report import RunReport
    from cli.main import build_planner

    task_id = task["id"]
    candidate_dir = candidate_root / task_id
    trace_path = candidate_dir / TRACE_PATH
    trace_path.parent.mkdir(parents=True, exist_ok=True)

    report = RunReport()
    final_output = ""
    runtime_error: str | None = None
    start = time.perf_counter()

    with trace_path.open("w", encoding="utf-8") as trace_file:
        try:
            planner = build_planner(str(candidate_dir), model=model)
            async for event in planner.run_stream(task["prompt"]):
                report.observe(event)
                trace_record = _event_to_trace_record(event)
                trace_record["elapsed_ms"] = int((time.perf_counter() - start) * 1000)
                trace_file.write(json.dumps(trace_record, ensure_ascii=False) + "\n")
                if event.type == "done":
                    final_output = event.content
                elif event.type == "error" and not final_output:
                    final_output = event.content
        except Exception as e:
            runtime_error = str(e)
            trace_file.write(json.dumps({
                "type": "runner_error",
                "content": runtime_error,
                "elapsed_ms": int((time.perf_counter() - start) * 1000),
            }, ensure_ascii=False) + "\n")

    duration_ms = int((time.perf_counter() - start) * 1000)
    report_path = report.write_final_report(candidate_dir)
    eval_result = evaluate_task(task, candidate_root)
    failures = list(eval_result.failures)
    if runtime_error:
        failures.insert(0, f"runtime error: {runtime_error}")

    return EvalRunResult(
        task_id=task_id,
        title=task.get("title", task_id),
        model=model,
        passed=not failures,
        duration_ms=duration_ms,
        tool_calls=len(report.tool_calls),
        failed_tool_calls=report.failed_tool_count,
        prompt_tokens=report.prompt_tokens,
        completion_tokens=report.completion_tokens,
        total_tokens=report.total_tokens,
        trace_path=str(trace_path),
        report_path=str(report_path),
        failures=failures,
        final_output=final_output or report.final_output,
        runtime_error=runtime_error,
    )


def print_run_results(results: list[EvalRunResult], results_path: Path) -> None:
    passed = sum(1 for result in results if result.passed)
    total = len(results)
    print(f"Eval run results: {passed}/{total} passed")
    print(f"Wrote aggregate results to {results_path}")
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(
            f"[{status}] {result.task_id} "
            f"({result.duration_ms} ms, tools={result.tool_calls}, tokens={result.total_tokens})"
        )
        for failure in result.failures:
            print(f"  - {failure}")


def compare_eval_result_files(paths: list[Path]) -> EvalComparison:
    """Compare two or more aggregate eval result JSON files."""
    if len(paths) < 2:
        raise ValueError("compare requires at least two eval result files")

    payloads = [_load_eval_results_payload(path) for path in paths]
    runs = [
        _comparison_run_from_payload(path, payload)
        for path, payload in zip(paths, payloads)
    ]
    _dedupe_comparison_labels(runs)
    baseline = runs[0]
    baseline_tasks = _task_pass_map(payloads[0])

    regressions: dict[str, list[str]] = {}
    improvements: dict[str, list[str]] = {}
    for run, payload in zip(runs[1:], payloads[1:]):
        current_tasks = _task_pass_map(payload)
        for task_id, baseline_passed in baseline_tasks.items():
            if task_id not in current_tasks:
                continue
            current_passed = current_tasks[task_id]
            if baseline_passed and not current_passed:
                regressions.setdefault(run.label, []).append(task_id)
            elif not baseline_passed and current_passed:
                improvements.setdefault(run.label, []).append(task_id)

    return EvalComparison(
        baseline=baseline,
        runs=runs,
        task_regressions=regressions,
        task_improvements=improvements,
    )


def render_eval_comparison_markdown(comparison: EvalComparison) -> str:
    """Render a deterministic Markdown comparison report."""
    lines = [
        "# Simple Coding Agent Eval Comparison",
        "",
        "| Run | Passed | Pass Rate | Duration ms | Tool Calls | Failed Tools | Tokens |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for run in comparison.runs:
        lines.append(
            f"| {run.label} | {run.passed}/{run.total} | {run.pass_rate:.2%} | "
            f"{run.total_duration_ms} | {run.total_tool_calls} | "
            f"{run.failed_tool_calls} | {run.total_tokens} |"
        )

    lines.extend(["", "## Changes vs Baseline", ""])
    any_changes = False
    for run in comparison.runs[1:]:
        regressions = comparison.task_regressions.get(run.label, [])
        improvements = comparison.task_improvements.get(run.label, [])
        lines.append(f"### {run.label}")
        if regressions:
            any_changes = True
            lines.append(f"- Regressions: {', '.join(regressions)}")
        else:
            lines.append("- Regressions: none")
        if improvements:
            any_changes = True
            lines.append(f"- Improvements: {', '.join(improvements)}")
        else:
            lines.append("- Improvements: none")
        lines.append("")

    if not any_changes and len(comparison.runs) > 1:
        lines.append("No task pass/fail changes detected.")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_eval_comparison(paths: list[Path], output_path: Path) -> Path:
    comparison = compare_eval_result_files(paths)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_eval_comparison_markdown(comparison),
        encoding="utf-8",
    )
    return output_path


def _event_to_trace_record(event) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": event.type,
        "content": event.content,
        "token": event.token,
        "actor_id": event.actor_id,
        "tool_name": event.tool_name,
        "tool_args": event.tool_args,
    }
    if event.tool_result is not None:
        record["tool_result"] = {
            "success": event.tool_result.success,
            "content": event.tool_result.content,
            "error": event.tool_result.error,
        }
    return record


def _run_test_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    if not command:
        return subprocess.CompletedProcess(command, 0, "", "")
    resolved = [sys.executable if part == "{python}" else part for part in command]
    return subprocess.run(
        resolved,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
    )


def _changed_files(left: Path, right: Path) -> set[str]:
    paths: set[str] = set()
    _collect_changed(left, right, Path(), paths)
    return paths


def _collect_changed(left: Path, right: Path, rel: Path, paths: set[str]) -> None:
    if rel.name in IGNORED_DIRS:
        return
    if rel.as_posix().startswith(".sca/traces"):
        return
    if rel.as_posix().startswith(".sca/artifacts"):
        return

    left_exists = left.exists()
    right_exists = right.exists()
    if not left_exists and not right_exists:
        return
    if left_exists != right_exists:
        if right.is_dir() or left.is_dir():
            base = right if right_exists else left
            for child in _iter_children(base):
                _collect_changed(left / child.name, right / child.name, rel / child.name, paths)
        else:
            paths.add(rel.as_posix())
        return

    if left.is_dir() and right.is_dir():
        names = {child.name for child in _iter_children(left)} | {child.name for child in _iter_children(right)}
        for name in sorted(names):
            _collect_changed(left / name, right / name, rel / name, paths)
        return

    if left.is_file() and right.is_file() and not filecmp.cmp(left, right, shallow=False):
        paths.add(rel.as_posix())


def _iter_children(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    return [child for child in path.iterdir() if child.name not in IGNORED_DIRS]


def _load_eval_results_payload(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"missing eval results file: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "summary" not in payload or "tasks" not in payload:
        raise ValueError(f"invalid eval results file: {path}")
    return payload


def _comparison_run_from_payload(path: Path, payload: dict[str, Any]) -> EvalComparisonRun:
    summary = payload["summary"]
    tasks = payload.get("tasks", [])
    label = _comparison_label(path, payload)
    return EvalComparisonRun(
        label=label,
        path=str(path),
        total=int(summary.get("total", len(tasks)) or 0),
        passed=int(summary.get("passed", 0) or 0),
        failed=int(summary.get("failed", 0) or 0),
        pass_rate=float(summary.get("pass_rate", 0.0) or 0.0),
        total_duration_ms=int(summary.get("total_duration_ms", 0) or 0),
        total_tool_calls=int(summary.get("total_tool_calls", 0) or 0),
        failed_tool_calls=sum(int(task.get("failed_tool_calls", 0) or 0) for task in tasks),
        total_tokens=int(summary.get("total_tokens", 0) or 0),
    )


def _comparison_label(path: Path, payload: dict[str, Any]) -> str:
    models = sorted({
        str(task.get("model"))
        for task in payload.get("tasks", [])
        if task.get("model")
    })
    if len(models) == 1:
        return models[0]
    return path.stem


def _dedupe_comparison_labels(runs: list[EvalComparisonRun]) -> None:
    seen: dict[str, int] = {}
    for run in runs:
        count = seen.get(run.label, 0)
        seen[run.label] = count + 1
        if count:
            run.label = f"{run.label} ({count + 1})"


def _task_pass_map(payload: dict[str, Any]) -> dict[str, bool]:
    return {
        str(task["task_id"]): bool(task.get("passed"))
        for task in payload.get("tasks", [])
        if "task_id" in task
    }


def copy_fixtures(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for task in load_tasks():
        src = REPO_ROOT / "evals" / task["fixture"]
        dst = destination / task["id"]
        if dst.exists():
            _remove_tree(dst)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*IGNORED_DIRS))
        init_candidate_repo(dst)


def _remove_tree(path: Path) -> None:
    """Remove a tree that may contain read-only git objects on Windows."""

    def on_error(func, failed_path, exc_info):
        try:
            os.chmod(failed_path, stat.S_IWRITE)
            func(failed_path)
        except OSError:
            raise

    shutil.rmtree(path, onerror=on_error)


def init_candidate_repo(path: Path) -> None:
    """Make a copied fixture a standalone git repo for agent worktree tools."""
    commands = [
        ["git", "init", "-q"],
        ["git", "config", "user.email", "sca-eval@example.local"],
        ["git", "config", "user.name", "SCA Eval"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "initial eval fixture"],
    ]
    for command in commands:
        result = subprocess.run(
            command,
            cwd=path,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"failed to initialize eval git repo at {path}: {detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local coding-agent eval checks.")
    parser.add_argument(
        "--candidate-root",
        type=Path,
        required=True,
        help="Directory containing one completed workspace per task id.",
    )
    parser.add_argument(
        "--copy-fixtures-to",
        type=Path,
        default=None,
        help="Copy initial fixtures to this directory and exit.",
    )
    parser.add_argument(
        "--run-agent",
        action="store_true",
        help="Run the agent against each fixture before checking.",
    )
    parser.add_argument("--model", default=None, help="Model name to pass to the agent.")
    parser.add_argument(
        "--results-path",
        type=Path,
        default=Path("eval_results.json"),
        help="Path for aggregate eval run JSON when --run-agent is used.",
    )
    args = parser.parse_args(argv)

    if args.copy_fixtures_to is not None:
        copy_fixtures(args.copy_fixtures_to)
        print(f"Copied fixtures to {args.copy_fixtures_to}")
        return 0

    if args.run_agent:
        results = asyncio.run(
            run_eval_suite(
                candidate_root=args.candidate_root,
                model=args.model,
                results_path=args.results_path,
                prepare=True,
            )
        )
        print_run_results(results, args.results_path)
        return 0 if all(result.passed for result in results) else 1

    results = evaluate_all(args.candidate_root)
    print_results(results)
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
