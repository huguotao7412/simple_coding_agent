from __future__ import annotations

from core.agent import Agent
from cli.ui import UI


class Bridge:
    """Connects the core Agent to the terminal UI. Runs the REPL loop."""

    def __init__(self, agent: Agent, ui: UI):
        self.agent = agent
        self.ui = ui

    async def run(self) -> None:
        self.ui.render_welcome()

        while True:
            try:
                user_input = self.ui.render_user_prompt()
            except (EOFError, KeyboardInterrupt):
                self.ui.render_info("\nGoodbye.")
                break

            user_input = user_input.strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                self.ui.render_info("Goodbye.")
                break

            stream = None
            try:
                async for event in self.agent.run_stream(user_input):
                    if event.type == "thought":
                        if stream is None:
                            stream = self.ui.stream_markdown()
                            stream.__enter__()  # 开启 Markdown 流式渲染
                        stream.add_token(event.token)
                    elif event.type == "tool_call":
                        if stream:
                            stream.__exit__(None, None, None)
                            stream = None
                        # 触发黄色 running 状态
                        self.ui.render_tool_status(event.tool_name or "tool", "running")
                    elif event.type == "tool_result":
                        status = "done" if event.tool_result and event.tool_result.success else "failed"
                        # 触发绿色/红色完成状态
                        self.ui.render_tool_status(event.tool_name or "tool", status)
                    elif event.type == "compaction":
                        self.ui.render_info("\n[System: Context compressed]")
            finally:
                if stream:
                    stream.__exit__(None, None, None)
