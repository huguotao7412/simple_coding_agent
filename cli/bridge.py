from __future__ import annotations

from core.planner import Planner
from cli.ui import UI


class Bridge:
    """Connects the core Agent to the terminal UI. Runs the REPL loop."""

    def __init__(self, agent: Planner, ui: UI):
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
                        self.ui.clear_tool_status()  # 💡收到大模型思考时，清除工具的动画
                        if stream is None:
                            stream = self.ui.stream_markdown()
                            stream.__enter__()  # Markdown
                        stream.add_token(event.token)
                    elif event.type == "tool_call":
                        if stream:
                            stream.__exit__(None, None, None)
                            stream = None
                        self.ui.render_tool_status(event.tool_name or "tool", "running")
                    elif event.type == "tool_result":
                        status = "done" if event.tool_result and event.tool_result.success else "failed"
                        self.ui.render_tool_status(event.tool_name or "tool", status)
                    elif event.type == "compaction":
                        self.ui.clear_tool_status()
                        self.ui.render_info("\n[System: Context compressed]")
                    elif event.type == "error":
                        self.ui.clear_tool_status()
                        if stream:
                            stream.__exit__(None, None, None)
                            stream = None
                        self.ui.render_error(f"Agent Error: {event.content}")
            finally:
                self.ui.clear_tool_status()  # 确保一轮对话结束时，不残留工具动画
                if stream:
                    stream.__exit__(None, None, None)
