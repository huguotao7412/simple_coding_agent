from __future__ import annotations

import argparse
import asyncio
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

load_dotenv()


def build_planner(workspace_dir: str, model: str | None = None):
    api_key = os.getenv("SCA_API_KEY")
    if not api_key:
        raise RuntimeError("SCA_API_KEY not set in .env file")

    from core.logging_config import setup_logging

    setup_logging()

    from core.context import ContextManager
    from core.llm import LLMClient
    from core.planner import Planner
    from core.state import GlobalState
    from core.system_prompt import PLANNER_SYSTEM_PROMPT
    from core.tools import PLANNER_TOOLS

    GlobalState.reset()

    llm = LLMClient(
        api_key=api_key,
        base_url=os.getenv("SCA_API_BASE", "https://api.deepseek.com"),
        model=model or os.getenv("SCA_MODEL", "deepseek-v4-pro"),
        max_tokens=int(os.getenv("SCA_MAX_TOKENS", "128000")),
    )
    ctx = ContextManager(system_prompt=PLANNER_SYSTEM_PROMPT)
    tools = [tool_cls() for tool_cls in PLANNER_TOOLS]
    return Planner(
        llm_client=llm,
        context_manager=ctx,
        tools=tools,
        workspace_dir=workspace_dir,
    )


async def run_once(prompt: str, workspace_dir: str, model: str | None = None) -> str:
    from cli.report import RunReport

    planner = build_planner(workspace_dir=workspace_dir, model=model)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Simple Coding Agent")
    parser.add_argument("--model", default=None, help="Model name (overrides .env)")
    parser.add_argument("--dir", default=None, help="Workspace directory (default: cwd)")
    parser.add_argument("--workspace", default=None, help="Alias for --dir")
    parser.add_argument(
        "--prompt",
        default=None,
        help="Run one non-interactive task and print the final response.",
    )
    args = parser.parse_args()

    workspace_dir = os.path.abspath(args.workspace or args.dir or os.getcwd())

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

    if args.prompt:
        try:
            result = asyncio.run(run_once(args.prompt, workspace_dir, args.model))
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        print(result)
        return

    from cli.bridge import Bridge
    from cli.ui import UI

    try:
        planner = build_planner(workspace_dir=workspace_dir, model=args.model)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    ui = UI()
    bridge = Bridge(agent=planner, ui=ui)
    asyncio.run(bridge.run())


if __name__ == "__main__":
    main()
