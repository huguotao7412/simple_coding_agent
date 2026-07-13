from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.runtime.conversation import ContextManager
    from core.planner import Planner
    from core.runs.context import RunContext

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

load_dotenv()


def _resolved_model(model: str | None) -> str:
    return model or os.getenv("SCA_MODEL") or "deepseek-v4-pro"


def build_planner(
    workspace_dir: str,
    model: str | None = None,
    *,
    run_context: RunContext | None = None,
    context_manager: ContextManager | None = None,
) -> Planner:
    api_key = os.getenv("SCA_API_KEY")
    if not api_key:
        raise RuntimeError("SCA_API_KEY not set in .env file")

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
    )
    ctx = context_manager or ContextManager(system_prompt=PLANNER_SYSTEM_PROMPT)
    tools = [tool_cls() for tool_cls in PLANNER_TOOLS]
    return Planner(
        llm_client=llm,
        context_manager=ctx,
        tools=tools,
        workspace_dir=workspace_dir,
        run_context=run_context or RunContext.create(),
    )


async def run_once(prompt: str, workspace_dir: str, model: str | None = None) -> str:
    from cli.report import RunReport
    from cli.runs import create_durable_run_context
    from core.runtime.conversation import ContextManager
    from core.system_prompt import PLANNER_SYSTEM_PROMPT

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
    )
    report = RunReport()
    final_output = ""
    async for event in planner.run_stream(prompt):
        report.observe(event)
        if event.type == "done":
            final_output = event.content
        elif event.type == "error" and not final_output:
            final_output = event.content
    report.write_final_report(workspace_dir)
    return final_output or report.final_output


async def resume_once(
    run_id: str,
    workspace_dir: str,
    model: str | None = None,
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
    planner = build_planner(
        workspace_dir=workspace_dir,
        model=model or stored.record.model,
        run_context=run_context,
        context_manager=context_manager,
    )
    report = RunReport()
    final_output = ""
    async for event in planner.run_stream("", resume=True):
        report.observe(event)
        if event.type == "done":
            final_output = event.content
        elif event.type == "error" and not final_output:
            final_output = event.content
    report.write_final_report(workspace_dir)
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
    commands = parser.add_subparsers(dest="command")
    runs_parser = commands.add_parser("runs", help="List durable runs")
    runs_parser.add_argument("--limit", type=int, default=50)
    inspect_parser = commands.add_parser("inspect", help="Inspect one durable run")
    inspect_parser.add_argument("run_id")
    resume_parser = commands.add_parser("resume", help="Resume an interrupted run")
    resume_parser.add_argument("run_id")
    return parser


async def _read_run_command(args: argparse.Namespace, workspace_dir: str) -> int:
    from cli.runs import render_run_detail, render_run_list, open_run_store

    store = await open_run_store(workspace_dir)
    if args.command == "runs":
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    workspace_dir = os.path.abspath(args.workspace or args.dir or os.getcwd())

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
                resume_once(args.run_id, workspace_dir, args.model)
            )
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(result)
        return 0

    if args.prompt:
        try:
            result = asyncio.run(run_once(args.prompt, workspace_dir, args.model))
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(result)
        return 0

    from cli.bridge import Bridge
    from cli.ui import UI

    try:
        planner = build_planner(workspace_dir=workspace_dir, model=args.model)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    ui = UI()  # type: ignore[no-untyped-call]
    bridge = Bridge(agent=planner, ui=ui)
    asyncio.run(bridge.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
