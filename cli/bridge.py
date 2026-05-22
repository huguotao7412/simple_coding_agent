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

            # Stream the response
            with self.ui.stream_markdown() as stream:
                result = await self.agent.run(
                    user_input,
                    on_token=stream.add_token,
                )

            # If result came back with content but streaming didn't fire
            # (e.g., model returned full content without streaming deltas),
            # render it now.
            if result and not stream._buffer.strip():
                self.ui.render_markdown(result)
