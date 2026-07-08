from __future__ import annotations

import argparse
import filecmp
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


DEFAULT_CASES_DIR = Path(__file__).resolve().parent / "cases"
DEFAULT_REPORTS_DIR = Path(__file__).resolve().parent / "reports"
DEFAULT_TIMEOUT_SECONDS = 120
IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".git"}


@dataclass(frozen=True)
class EvalCase:
    name: str
    prompt: str
    repo_dir: Path
    verification_commands: list[str]
    expected_files: list[str] = field(default_factory=list)
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS


@dataclass
class CommandResult:
    command: str
    returncode: int
    duration_seconds: float
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@dataclass
class CaseResult:
    name: str
    passed: bool
    verification_passed: bool
    dry_run: bool
    duration_seconds: float
    workspace: str
    prompt: str
    changed_files: list[str] = field(default_factory=list)
    missing_expected_files: list[str] = field(default_factory=list)
    agent_result: CommandResult | None = None
    verification_results: list[CommandResult] = field(default_factory=list)
    error: str | None = None


def load_case(case_dir: Path) -> EvalCase:
    config_path = case_dir / "eval.json"
    repo_dir = case_dir / "repo"
    if not config_path.is_file():
        raise ValueError(f"Missing eval.json in {case_dir}")
    if not repo_dir.is_dir():
        raise ValueError(f"Missing repo/ fixture in {case_dir}")

    with config_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    name = raw.get("name") or case_dir.name
    prompt = raw.get("prompt")
    verification_commands = raw.get("verification_commands", [])
    expected_files = raw.get("expected_files", [])
    timeout_seconds = raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)

    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"Invalid name in {config_path}")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"Invalid prompt in {config_path}")
    if not isinstance(verification_commands, list) or not all(
        isinstance(cmd, str) and cmd.strip() for cmd in verification_commands
    ):
        raise ValueError(f"Invalid verification_commands in {config_path}")
    if not isinstance(expected_files, list) or not all(
        isinstance(path, str) and path.strip() for path in expected_files
    ):
        raise ValueError(f"Invalid expected_files in {config_path}")
    if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
        raise ValueError(f"Invalid timeout_seconds in {config_path}")

    return EvalCase(
        name=name,
        prompt=prompt,
        repo_dir=repo_dir,
        verification_commands=verification_commands,
        expected_files=expected_files,
        timeout_seconds=timeout_seconds,
    )


def discover_cases(cases_dir: Path, selected_case: str | None = None) -> list[EvalCase]:
    if not cases_dir.is_dir():
        raise ValueError(f"Cases directory not found: {cases_dir}")

    case_dirs = sorted(path for path in cases_dir.iterdir() if path.is_dir())
    if selected_case:
        case_dirs = [path for path in case_dirs if path.name == selected_case]
        if not case_dirs:
            raise ValueError(f"Eval case not found: {selected_case}")

    return [load_case(case_dir) for case_dir in case_dirs]


def run_command(command: str, cwd: Path, timeout_seconds: int) -> CommandResult:
    start = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
        return CommandResult(
            command=command,
            returncode=completed.returncode,
            duration_seconds=round(time.monotonic() - start, 3),
            stdout=_trim(completed.stdout),
            stderr=_trim(completed.stderr),
        )
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout.decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else e.stdout
        stderr = e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else e.stderr
        return CommandResult(
            command=command,
            returncode=124,
            duration_seconds=round(time.monotonic() - start, 3),
            stdout=_trim(stdout or ""),
            stderr=_trim(stderr or f"Command timed out after {timeout_seconds}s"),
            timed_out=True,
        )


def run_case(
    case: EvalCase,
    reports_dir: Path,
    agent_command: str | None = None,
    dry_run: bool = False,
    keep_workspace: bool = False,
) -> CaseResult:
    start = time.monotonic()
    temp_root = Path(tempfile.mkdtemp(prefix=f"sca-eval-{case.name}-"))
    workspace = temp_root / "workspace"
    shutil.copytree(case.repo_dir, workspace)

    agent_result: CommandResult | None = None
    verification_results: list[CommandResult] = []
    error: str | None = None

    try:
        if not dry_run:
            git_setup_results = prepare_git_workspace(workspace, case.timeout_seconds)
            failed_git_setup = next(
                (result for result in git_setup_results if result.returncode != 0),
                None,
            )
            if failed_git_setup is not None:
                error = f"Git workspace setup failed: {failed_git_setup.stderr}"
                return CaseResult(
                    name=case.name,
                    passed=False,
                    verification_passed=False,
                    dry_run=dry_run,
                    duration_seconds=round(time.monotonic() - start, 3),
                    workspace=str(workspace),
                    prompt=case.prompt,
                    error=error,
                )

        if not dry_run and agent_command:
            command = agent_command.format(
                workspace=str(workspace),
                prompt=case.prompt,
            )
            agent_result = run_command(command, workspace, case.timeout_seconds)

        if not dry_run:
            for command in case.verification_commands:
                result = run_command(command, workspace, case.timeout_seconds)
                verification_results.append(result)
                if result.returncode != 0:
                    break

        changed_files = compare_trees(case.repo_dir, workspace)
        missing_expected_files = [
            path for path in case.expected_files
            if not (workspace / path).exists()
        ]
        verification_passed = dry_run or all(
            result.returncode == 0 for result in verification_results
        )
        agent_passed = agent_result is None or agent_result.returncode == 0
        passed = agent_passed and verification_passed and not missing_expected_files

        return CaseResult(
            name=case.name,
            passed=passed,
            verification_passed=verification_passed,
            dry_run=dry_run,
            duration_seconds=round(time.monotonic() - start, 3),
            workspace=str(workspace),
            prompt=case.prompt,
            changed_files=changed_files,
            missing_expected_files=missing_expected_files,
            agent_result=agent_result,
            verification_results=verification_results,
            error=error,
        )
    except Exception as exc:
        error = str(exc)
        return CaseResult(
            name=case.name,
            passed=False,
            verification_passed=False,
            dry_run=dry_run,
            duration_seconds=round(time.monotonic() - start, 3),
            workspace=str(workspace),
            prompt=case.prompt,
            error=error,
        )
    finally:
        if keep_workspace:
            reports_dir.mkdir(parents=True, exist_ok=True)
            kept_path = reports_dir / f"workspace-{case.name}"
            if kept_path.exists():
                shutil.rmtree(kept_path)
            shutil.copytree(workspace, kept_path)
        shutil.rmtree(temp_root, ignore_errors=True)


def compare_trees(before: Path, after: Path) -> list[str]:
    before_files = _file_map(before)
    after_files = _file_map(after)
    changed: list[str] = []

    for rel_path in sorted(before_files | after_files):
        before_path = before_files.get(rel_path)
        after_path = after_files.get(rel_path)
        if before_path is None or after_path is None:
            changed.append(rel_path)
        elif not filecmp.cmp(before_path, after_path, shallow=False):
            changed.append(rel_path)
    return changed


def prepare_git_workspace(workspace: Path, timeout_seconds: int) -> list[CommandResult]:
    commands = [
        "git init",
        "git config user.email sca-eval@example.com",
        "git config user.name sca-eval",
        "git add -A",
        "git commit -m initial-eval-fixture",
    ]
    results: list[CommandResult] = []
    for command in commands:
        result = run_command(command, workspace, timeout_seconds)
        results.append(result)
        if result.returncode != 0:
            break
    return results


def write_reports(results: list[CaseResult], reports_dir: Path) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "latest.json"
    md_path = reports_dir / "latest.md"

    payload = {
        "summary": {
            "total": len(results),
            "passed": sum(1 for result in results if result.passed),
            "failed": sum(1 for result in results if not result.passed),
        },
        "results": [asdict(result) for result in results],
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown_report(payload), encoding="utf-8")
    return json_path, md_path


def render_markdown_report(payload: dict) -> str:
    summary = payload["summary"]
    lines = [
        "# Simple Coding Agent Eval Report",
        "",
        f"- Total: {summary['total']}",
        f"- Passed: {summary['passed']}",
        f"- Failed: {summary['failed']}",
        "",
        "| Case | Result | Verification | Duration | Changed Files |",
        "|---|---:|---:|---:|---:|",
    ]

    for result in payload["results"]:
        status = "PASS" if result["passed"] else "FAIL"
        verification = "PASS" if result["verification_passed"] else "FAIL"
        changed_count = len(result["changed_files"])
        lines.append(
            f"| {result['name']} | {status} | {verification} | "
            f"{result['duration_seconds']}s | {changed_count} |"
        )

    lines.append("")
    for result in payload["results"]:
        lines.extend([
            f"## {result['name']}",
            "",
            f"Prompt: {result['prompt']}",
            "",
            f"Workspace: `{result['workspace']}`",
            "",
        ])
        if result.get("error"):
            lines.extend(["Error:", "", f"```text\n{result['error']}\n```", ""])
        if result.get("agent_result"):
            lines.extend(_command_section("Agent Command", result["agent_result"]))
        for command_result in result.get("verification_results", []):
            lines.extend(_command_section("Verification Command", command_result))
        if result.get("missing_expected_files"):
            lines.append("Missing expected files: " + ", ".join(result["missing_expected_files"]))
            lines.append("")
        if result.get("changed_files"):
            lines.append("Changed files: " + ", ".join(result["changed_files"]))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Simple Coding Agent eval cases.")
    parser.add_argument("--case", help="Run a single eval case by directory name.")
    parser.add_argument("--cases-dir", type=Path, default=DEFAULT_CASES_DIR)
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    parser.add_argument(
        "--agent-command",
        help="Optional command template. Supports {workspace} and {prompt}.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Discover cases and write reports only.")
    parser.add_argument(
        "--keep-workspaces",
        action="store_true",
        help="Copy final workspaces into the reports directory for inspection.",
    )
    args = parser.parse_args(argv)

    try:
        cases = discover_cases(args.cases_dir, args.case)
        results = [
            run_case(
                case,
                reports_dir=args.reports_dir,
                agent_command=args.agent_command,
                dry_run=args.dry_run,
                keep_workspace=args.keep_workspaces,
            )
            for case in cases
        ]
        json_path, md_path = write_reports(results, args.reports_dir)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    passed = sum(1 for result in results if result.passed)
    total = len(results)
    print(f"Eval complete: {passed}/{total} passed")
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")
    return 0 if passed == total else 1


def _file_map(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in IGNORED_DIRS for part in path.relative_to(root).parts):
            continue
        files[path.relative_to(root).as_posix()] = path
    return files


def _trim(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"\n... [{omitted} chars omitted] ..."


def _command_section(title: str, result: dict) -> list[str]:
    lines = [
        f"### {title}",
        "",
        f"Command: `{result['command']}`",
        f"Return code: {result['returncode']}",
        f"Duration: {result['duration_seconds']}s",
        "",
    ]
    if result.get("stdout"):
        lines.extend(["Stdout:", "", f"```text\n{result['stdout']}\n```", ""])
    if result.get("stderr"):
        lines.extend(["Stderr:", "", f"```text\n{result['stderr']}\n```", ""])
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
