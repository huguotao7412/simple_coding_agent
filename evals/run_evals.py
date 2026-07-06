from __future__ import annotations

import argparse
import filecmp
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = Path(__file__).with_name("tasks.json")
REPORT_PATH = Path(".sca") / "final_report.md"
IGNORED_DIRS = {"__pycache__", ".git", ".mypy_cache", ".pytest_cache", ".worktrees"}


@dataclass
class EvalResult:
    task_id: str
    passed: bool
    failures: list[str] = field(default_factory=list)


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


def copy_fixtures(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for task in load_tasks():
        src = REPO_ROOT / "evals" / task["fixture"]
        dst = destination / task["id"]
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        init_candidate_repo(dst)


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
    args = parser.parse_args(argv)

    if args.copy_fixtures_to is not None:
        copy_fixtures(args.copy_fixtures_to)
        print(f"Copied fixtures to {args.copy_fixtures_to}")
        return 0

    results = evaluate_all(args.candidate_root)
    print_results(results)
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
