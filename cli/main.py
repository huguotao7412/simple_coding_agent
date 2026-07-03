from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

load_dotenv()


def main():
    parser = argparse.ArgumentParser(description="Simple Coding Agent")
    parser.add_argument("--model", default=None, help="Model name (overrides .env)")
    parser.add_argument("--dir", default=None, help="Workspace directory (default: cwd)")
    args = parser.parse_args()

    api_key = os.getenv("SCA_API_KEY")
    if not api_key:
        print("Error: SCA_API_KEY not set in .env file", file=sys.stderr)
        sys.exit(1)

    workspace_dir = args.dir or os.getcwd()
    workspace_dir = os.path.abspath(workspace_dir)

    # 清理上次异常退出遗留的临时 Worktree
    from core.git_utils import cleanup_orphans
    try:
        removed = cleanup_orphans(workspace_dir)
        if removed:
            print(
                f"[init] Cleaned up {len(removed)} orphaned worktree(s)",
                file=sys.stderr,
            )
    except Exception as e:
        print(
            f"[init] Warning: worktree cleanup failed: {e}",
            file=sys.stderr,
        )

    # Lazy imports so --help is fast
    from core.llm import LLMClient
    from core.context import ContextManager
    from core.planner import Planner
    from core.tools import PLANNER_TOOLS
    from core.system_prompt import PLANNER_SYSTEM_PROMPT
    from core.state import GlobalState
    from cli.ui import UI
    from cli.bridge import Bridge

    llm = LLMClient(
        api_key=api_key,
        base_url=os.getenv("SCA_API_BASE", "https://api.deepseek.com"),
        model=args.model or os.getenv("SCA_MODEL", "deepseek-v4-pro"),
        max_tokens=int(os.getenv("SCA_MAX_TOKENS", "128000")),
    )

    ctx = ContextManager(system_prompt=PLANNER_SYSTEM_PROMPT)
    tools = [t() for t in PLANNER_TOOLS]
    state = GlobalState.get()
    planner = Planner(llm_client=llm, context_manager=ctx, tools=tools, workspace_dir=workspace_dir)

    ui = UI()
    bridge = Bridge(agent=planner, ui=ui)

    asyncio.run(bridge.run())


if __name__ == "__main__":
    main()
