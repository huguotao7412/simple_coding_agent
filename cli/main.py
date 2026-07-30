from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from cli.encoding import configure_stdio_encoding
from core.model_names import normalize_model_name

if TYPE_CHECKING:
    from core.runtime.conversation import ContextManager
    from core.execution.assessment import TaskAssessor
    from core.planner import Planner
    from core.runs.context import RunContext

configure_stdio_encoding()

def _resolved_model(model: str | None) -> str:
    return normalize_model_name(model or os.getenv("SCA_MODEL"))


def build_planner(
    workspace_dir: str,
    model: str | None = None,
    *,
    run_context: RunContext | None = None,
    context_manager: ContextManager | None = None,
    task_assessor: TaskAssessor | None = None,
    high_risk_approved: bool = False,
) -> Planner:
    from core.config import load_runtime_environment

    load_runtime_environment(workspace_dir)
    api_key = os.getenv("SCA_API_KEY")
    if not api_key or api_key.strip() == "your-api-key":
        from core.config import user_config_path

        raise RuntimeError(
            "SCA_API_KEY is not configured. Run 'sca config init', then edit "
            f"{user_config_path()}"
        )

    from core.logging_config import setup_logging

    setup_logging()

    from core.runtime.conversation import ContextManager
    from core.llm import LLMClient
    from core.planner import Planner
    from core.runs.context import RunContext
    from core.system_prompt import PLANNER_SYSTEM_PROMPT
    from core.tools import PLANNER_TOOLS

    llm = LLMClient(
        api_key=api_key,
        base_url=os.getenv("SCA_API_BASE", "https://api.deepseek.com"),
        model=_resolved_model(model),
        max_tokens=int(os.getenv("SCA_MAX_TOKENS", "128000")),
        max_output_tokens=int(os.getenv("SCA_MAX_OUTPUT_TOKENS", "8192")),
        read_timeout_seconds=float(os.getenv("SCA_LLM_READ_TIMEOUT", "120")),
    )
    ctx = context_manager or ContextManager(system_prompt=PLANNER_SYSTEM_PROMPT)
    tools = [tool_cls() for tool_cls in PLANNER_TOOLS]
    return Planner(
        llm_client=llm,
        context_manager=ctx,
        tools=tools,
        workspace_dir=workspace_dir,
        run_context=run_context or RunContext.create(),
        task_assessor=task_assessor,
        high_risk_approved=high_risk_approved,
    )


async def run_once(
    prompt: str,
    workspace_dir: str,
    model: str | None = None,
    *,
    high_risk_approved: bool = False,
) -> str:
    from core.config import load_runtime_environment
    from cli.report import RunReport
    from cli.runs import create_durable_run_context
    from core.runtime.conversation import ContextManager
    from core.system_prompt import PLANNER_SYSTEM_PROMPT

    load_runtime_environment(workspace_dir)
    context_manager = ContextManager(system_prompt=PLANNER_SYSTEM_PROMPT)
    run_context = await create_durable_run_context(
        workspace_dir=workspace_dir,
        model=_resolved_model(model),
        messages=context_manager.messages,
    )
    planner = build_planner(
        workspace_dir=workspace_dir,
        model=model,
        run_context=run_context,
        context_manager=context_manager,
        high_risk_approved=high_risk_approved,
    )
    from core.orchestration.factory import create_application_service
    from core.orchestration.protocol import OrchestrationRequest

    orchestrator = create_application_service(planner)
    report = RunReport()
    final_output = ""
    async for event in orchestrator.run_stream(OrchestrationRequest(
        user_request=prompt,
        approval=True if high_risk_approved else None,
    )):
        report.observe(event)
        if event.type == "done":
            final_output = event.content
        elif event.type == "error" and not final_output:
            final_output = event.content
    report.write_final_report(workspace_dir, run_context.run_id)
    return final_output or report.final_output


async def resume_once(
    run_id: str,
    workspace_dir: str,
    model: str | None = None,
    *,
    high_risk_approved: bool = False,
) -> str:
    from cli.report import RunReport
    from cli.runs import load_resumable_run, open_run_store
    from core.runtime.conversation import ContextManager
    from core.runs.context import RunContext
    from core.system_prompt import PLANNER_SYSTEM_PROMPT

    store = await open_run_store(workspace_dir)
    stored = await load_resumable_run(store, run_id, workspace_dir)
    checkpoint = stored.checkpoint
    if checkpoint is None:  # narrowed by load_resumable_run
        raise RuntimeError(f"durable run {run_id} has no checkpoint")
    context_manager = ContextManager(system_prompt=PLANNER_SYSTEM_PROMPT)
    context_manager.restore_messages(list(checkpoint.messages))
    run_context = RunContext.from_checkpoint(
        stored.record,
        checkpoint,
        store=store,
    )
    from core.orchestration.factory import (
        create_application_service,
        has_langgraph_checkpoint,
    )
    from core.orchestration.protocol import OrchestrationRequest

    if not has_langgraph_checkpoint(workspace_dir, run_id):
        raise RuntimeError(
            f"durable run {run_id} predates the LangGraph checkpoint format "
            "and remains inspectable, but cannot be resumed safely. SCA will "
            "not invent a graph program counter; start a new Run instead."
        )
    planner = build_planner(
        workspace_dir=workspace_dir,
        model=model or stored.record.model,
        run_context=run_context,
        context_manager=context_manager,
    )
    orchestrator = create_application_service(planner)
    report = RunReport()
    final_output = ""
    async for event in orchestrator.run_stream(OrchestrationRequest(
        user_request="",
        resume=True,
        approval=True if high_risk_approved else None,
    )):
        report.observe(event)
        if event.type == "done":
            final_output = event.content
        elif event.type == "error" and not final_output:
            final_output = event.content
    report.write_final_report(workspace_dir, run_context.run_id)
    return final_output or report.final_output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Simple Coding Agent")
    parser.add_argument("--model", default=None, help="Model name (overrides .env)")
    parser.add_argument("--dir", default=None, help="Workspace directory (default: cwd)")
    parser.add_argument("--workspace", default=None, help="Alias for --dir")
    parser.add_argument(
        "--prompt",
        default=None,
        help="Run one non-interactive task and print the final response.",
    )
    parser.add_argument(
        "--approve-high-risk",
        action="store_true",
        help=(
            "Explicitly approve execution of a task classified as high risk. "
            "Only use after reviewing the requested side effects."
        ),
    )
    commands = parser.add_subparsers(dest="command")
    runs_parser = commands.add_parser("runs", help="List durable runs")
    runs_parser.add_argument("--limit", type=int, default=50)
    runs_commands = runs_parser.add_subparsers(dest="runs_command")
    runs_delete = runs_commands.add_parser("delete", help="Delete one durable run")
    runs_delete.add_argument("run_id")
    inspect_parser = commands.add_parser("inspect", help="Inspect one durable run")
    inspect_parser.add_argument("run_id")
    resume_parser = commands.add_parser("resume", help="Resume an interrupted run")
    resume_parser.add_argument("run_id")
    gc_parser = commands.add_parser("gc", help="Garbage-collect user-level SCA state")
    gc_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned lifecycle actions without changing state",
    )
    gc_parser.add_argument("--retention-days", type=int, default=None)
    gc_parser.add_argument("--retain-runs", type=int, default=None)
    gc_parser.add_argument("--artifact-max-bytes", type=int, default=None)
    commands.add_parser(
        "sandbox-check",
        help="Validate the configured command-execution sandbox",
    )
    config_parser = commands.add_parser(
        "config",
        help="Manage user-level configuration",
    )
    config_commands = config_parser.add_subparsers(dest="config_command")
    config_commands.add_parser("path", help="Print the user config path")
    config_init = config_commands.add_parser(
        "init",
        help="Create a user config template",
    )
    config_init.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing user config template",
    )
    return parser


def _config_command(args: argparse.Namespace) -> int:
    from core.config import initialize_user_config, user_config_path

    if args.config_command in {None, "path"}:
        print(user_config_path())
        return 0
    try:
        path = initialize_user_config(force=bool(args.force))
    except FileExistsError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Created user configuration: {path}")
    print("Edit SCA_API_KEY before starting sca.")
    return 0


async def _read_run_command(args: argparse.Namespace, workspace_dir: str) -> int:
    from cli.runs import render_run_detail, render_run_list, open_run_store

    store = await open_run_store(workspace_dir)
    if args.command == "runs":
        if args.runs_command == "delete":
            deleted = await store.delete_run(args.run_id)
            if not deleted:
                print(
                    f"Error: durable run {args.run_id} was not found",
                    file=sys.stderr,
                )
                return 1
            from core.lifecycle import delete_run_artifacts

            delete_run_artifacts(workspace_dir, args.run_id)
            print(f"Deleted durable run {args.run_id}")
            return 0
        print(render_run_list(await store.list_runs(limit=args.limit)))
        return 0
    stored = await store.load_run(args.run_id)
    if stored is None:
        print(f"Error: durable run {args.run_id} was not found", file=sys.stderr)
        return 1
    events = await store.list_events(args.run_id)
    message_count = len(stored.checkpoint.messages) if stored.checkpoint else 0
    print(render_run_detail(
        stored.record,
        event_count=len(events),
        message_count=message_count,
    ))
    return 0


def _cleanup_workspace(workspace_dir: str) -> None:
    from core.git_utils import cleanup_orphans

    try:
        removed = cleanup_orphans(workspace_dir)
        if removed:
            print(
                f"[init] Cleaned up {len(removed)} orphaned worktree(s)",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"[init] Warning: worktree cleanup failed: {e}", file=sys.stderr)


async def _gc_command(args: argparse.Namespace, workspace_dir: str) -> int:
    from core.lifecycle import (
        DEFAULT_ARTIFACT_MAX_BYTES,
        DEFAULT_RETAIN_RUNS,
        DEFAULT_RETENTION_DAYS,
        garbage_collect,
    )
    from core.paths import touch_workspace_state

    if not args.dry_run:
        touch_workspace_state(workspace_dir)

    retention_days = (
        args.retention_days
        if args.retention_days is not None
        else int(os.getenv("SCA_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS)))
    )
    retain_runs = (
        args.retain_runs
        if args.retain_runs is not None
        else int(os.getenv("SCA_RETAIN_RUNS", str(DEFAULT_RETAIN_RUNS)))
    )
    artifact_max_bytes = (
        args.artifact_max_bytes
        if args.artifact_max_bytes is not None
        else int(
            os.getenv(
                "SCA_ARTIFACT_MAX_BYTES",
                str(DEFAULT_ARTIFACT_MAX_BYTES),
            )
        )
    )
    report = await garbage_collect(
        dry_run=bool(args.dry_run),
        retention_days=retention_days,
        retain_runs=retain_runs,
        artifact_max_bytes=artifact_max_bytes,
    )
    prefix = "Would" if report.dry_run else "Did"
    if not report.actions:
        print("No lifecycle actions needed.")
    for action in report.actions:
        print(
            f"{prefix} {action.action}: {action.target} "
            f"({action.reason}; bytes={action.bytes_reclaimed})"
        )
    for warning in report.warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    print(
        f"GC summary: actions={len(report.actions)}, "
        f"bytes={report.bytes_reclaimed}, dry_run={report.dry_run}"
    )
    return 0


async def _sandbox_check() -> int:
    from core.sandbox.config import load_sandbox_config
    from core.sandbox.factory import create_sandbox_backend

    config = load_sandbox_config()
    backend = create_sandbox_backend(config)
    print(f"Backend: {backend.name}")
    print(f"OS isolated: {'yes' if backend.isolated else 'no'}")
    if not backend.isolated:
        print(
            "Warning: local mode executes Actor shell and verification commands "
            "with the current host user's permissions."
        )
    try:
        await backend.ensure_available()
    except Exception as error:
        print(f"Unavailable: {error}", file=sys.stderr)
        return 1
    print("Status: available")
    if config.mode.value == "e2b":
        print(f"Template: {config.e2b_template}")
        print(
            "Outbound internet: "
            f"{'enabled' if config.e2b_allow_internet else 'blocked'}"
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    workspace_dir = os.path.abspath(args.workspace or args.dir or os.getcwd())

    if args.command == "config":
        return _config_command(args)

    from core.config import load_runtime_environment

    load_runtime_environment(workspace_dir)

    if args.command == "sandbox-check":
        return asyncio.run(_sandbox_check())

    if args.command == "gc":
        try:
            return asyncio.run(_gc_command(args, workspace_dir))
        except (OSError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    if args.command in {"runs", "inspect"}:
        try:
            return asyncio.run(_read_run_command(args, workspace_dir))
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    _cleanup_workspace(workspace_dir)

    if args.command == "resume":
        try:
            result = asyncio.run(
                resume_once(
                    args.run_id,
                    workspace_dir,
                    args.model,
                    high_risk_approved=args.approve_high_risk,
                )
            )
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(result)
        return 0

    if args.prompt:
        try:
            result = asyncio.run(run_once(
                args.prompt,
                workspace_dir,
                args.model,
                high_risk_approved=args.approve_high_risk,
            ))
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(result)
        return 0

    from cli.bridge import Bridge
    from cli.ui import UI

    try:
        # Validate configuration before entering the REPL. Each turn below gets
        # its own durable RunContext and LangGraph thread.
        build_planner(
            workspace_dir=workspace_dir,
            model=args.model,
            high_risk_approved=args.approve_high_risk,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    from core.orchestration.interactive import InteractiveOrchestrationSession
    from core.system_prompt import PLANNER_SYSTEM_PROMPT

    async def interactive_context_factory(
        messages: list[dict[str, Any]],
    ) -> RunContext:
        from cli.runs import create_durable_run_context

        return await create_durable_run_context(
            workspace_dir=workspace_dir,
            model=_resolved_model(args.model),
            messages=messages,
        )

    def interactive_planner_factory(
        context_manager: ContextManager,
        run_context: RunContext,
    ) -> Planner:
        return build_planner(
            workspace_dir=workspace_dir,
            model=args.model,
            run_context=run_context,
            context_manager=context_manager,
            high_risk_approved=args.approve_high_risk,
        )

    session = InteractiveOrchestrationSession(
        system_prompt=PLANNER_SYSTEM_PROMPT,
        context_factory=interactive_context_factory,
        planner_factory=interactive_planner_factory,
        preapprove_high_risk=bool(args.approve_high_risk),
    )
    ui = UI()  # type: ignore[no-untyped-call]
    bridge = Bridge(session=session, ui=ui)
    asyncio.run(bridge.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
