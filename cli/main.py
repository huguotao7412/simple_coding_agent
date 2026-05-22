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

    # Lazy imports so --help is fast
    from core.llm import LLMClient
    from core.context import ContextManager
    from core.agent import Agent
    from core.tools.read import ReadTool
    from core.tools.write import WriteTool
    from core.tools.edit import EditTool
    from core.tools.bash import BashTool
    from core.system_prompt import SYSTEM_PROMPT
    from cli.ui import UI
    from cli.bridge import Bridge

    llm = LLMClient(
        api_key=api_key,
        base_url=os.getenv("SCA_API_BASE", "https://api.deepseek.com"),
        model=args.model or os.getenv("SCA_MODEL", "deepseek-v4-pro"),
        max_tokens=int(os.getenv("SCA_MAX_TOKENS", "128000")),
    )

    ctx = ContextManager(system_prompt=SYSTEM_PROMPT)
    tools = [ReadTool(), WriteTool(), EditTool(), BashTool()]
    agent = Agent(llm_client=llm, context_manager=ctx, tools=tools, workspace_dir=workspace_dir)

    ui = UI()
    bridge = Bridge(agent=agent, ui=ui)

    asyncio.run(bridge.run())


if __name__ == "__main__":
    main()
