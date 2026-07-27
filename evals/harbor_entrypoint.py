from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from cli.main import build_planner
from cli.report import RunReport
from core.execution.assessment import TaskAssessor
from core.model_names import normalize_model_name
from core.orchestration.factory import create_application_service
from core.orchestration.protocol import OrchestrationRequest
from evals.harbor_support import SUMMARY_FILENAME, TRACE_FILENAME
from evals.run_evals import _event_to_trace_record


DEFAULT_WORKSPACE = Path("/app")
DEFAULT_AGENT_LOG_DIR = Path("/logs/agent")
DEFAULT_ARTIFACT_DIR = Path("/logs/artifacts/sca")


def build_harbor_task_prompt(instruction: str) -> str:
    """Frame benchmark issues as repository-changing coding tasks for SCA."""
    stripped = instruction.strip()
    return (
        "You are running inside an automated coding benchmark task repository.\n\n"
        "Implement the necessary code changes in the current repository to resolve "
        "the issue below. Do not stop at explaining the cause. Modify the source "
        "files, preserve unrelated behavior, and run the most relevant tests or "
        "checks you can before finishing. Leave a concise final summary of the "
        "files changed and the verification result.\n\n"
        "Benchmark issue:\n"
        f"{stripped}\n"
    )


async def run_harbor_agent(
    instruction: str,
    *,
    workspace: Path = DEFAULT_WORKSPACE,
    agent_log_dir: Path = DEFAULT_AGENT_LOG_DIR,
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
    model: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run SCA in a Harbor task container and persist portable run metadata."""
    agent_log_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    trace_path = agent_log_dir / TRACE_FILENAME
    summary_path = agent_log_dir / SUMMARY_FILENAME
    report = RunReport()
    final_output = ""
    runtime_error: str | None = None
    started = time.perf_counter()

    with trace_path.open("w", encoding="utf-8") as trace_file:
        try:
            planner = build_planner(
                str(workspace),
                model=model,
                task_assessor=TaskAssessor(workspace),
            )
            task_prompt = build_harbor_task_prompt(instruction)
            orchestrator = create_application_service(planner)
            async for event in orchestrator.run_stream(OrchestrationRequest(
                user_request=task_prompt,
            )):
                report.observe(event)
                record = _event_to_trace_record(event)
                record["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
                trace_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                trace_file.flush()
                if event.type == "done":
                    final_output = event.content
                elif event.type == "error" and not final_output:
                    final_output = event.content
        except Exception as error:
            runtime_error = f"{type(error).__name__}: {error}"
            trace_file.write(json.dumps({
                "type": "runner_error",
                "content": runtime_error,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            }, ensure_ascii=False) + "\n")

    duration_ms = int((time.perf_counter() - started) * 1000)
    report_path = report.write_final_report(
        workspace,
        "harbor",
        state_dir=artifact_dir,
    )
    summary: dict[str, Any] = {
        "schema_version": 1,
        "model": normalize_model_name(model or os.getenv("SCA_MODEL")),
        "duration_ms": duration_ms,
        "tool_calls": len(report.tool_calls),
        "failed_tool_calls": report.failed_tool_count,
        "prompt_tokens": report.prompt_tokens,
        "completion_tokens": report.completion_tokens,
        "total_tokens": report.total_tokens,
        "usage_estimated": report.usage_estimated,
        "trace_path": str(trace_path),
        "report_path": str(report_path),
        "final_output": final_output or report.final_output,
        "runtime_error": runtime_error,
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return (1 if runtime_error else 0), summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sca-harbor-agent",
        description="Headless Simple Coding Agent entrypoint for Harbor task containers.",
    )
    instruction = parser.add_mutually_exclusive_group(required=True)
    instruction.add_argument("--prompt")
    instruction.add_argument("--instruction-file", type=Path)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--agent-log-dir", type=Path, default=DEFAULT_AGENT_LOG_DIR)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--model", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    instruction = args.prompt
    if args.instruction_file is not None:
        instruction = args.instruction_file.read_text(encoding="utf-8")
    if not isinstance(instruction, str):  # argparse enforces the exclusive group
        raise RuntimeError("Harbor instruction was not provided")
    exit_code, summary = asyncio.run(run_harbor_agent(
        instruction,
        workspace=args.workspace,
        agent_log_dir=args.agent_log_dir,
        artifact_dir=args.artifact_dir,
        model=args.model,
    ))
    if summary.get("final_output"):
        print(summary["final_output"])
    if summary.get("runtime_error"):
        print(f"Harbor agent error: {summary['runtime_error']}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
