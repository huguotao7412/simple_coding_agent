from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.live import Live

SCA_LOGO = r"""
  [cyan] ▄██████▄    ▄██████▄     ▄██▄    [/]
  [cyan]██▀    ▀██  ██▀    ▀██   ██▀▀██   [/]
  [cyan]██          ██          ██▄  ▄██  [/]
  [cyan] ▀██████▄   ██          ████████  [/]
  [cyan]       ▀██  ██          ██▀  ▀██  [/]
  [cyan]██▄    ▄██  ██▄    ▄██  ██    ██  [/]
  [cyan] ▀██████▀    ▀██████▀   ██    ██  [/]
"""


class UI:
    """Terminal rendering using Rich."""

    def __init__(self):
        self.console = Console(force_terminal=True)

    def render_markdown(self, text: str) -> None:
        if text.strip():
            self.console.print(Markdown(text))

    def stream_markdown(self) -> "LiveMarkdownStream":
        return LiveMarkdownStream(self.console)

    def render_tool_status(self, name: str, status: str) -> None:
        """Show a one-line tool execution status."""
        color = "yellow" if status == "running" else "green" if status == "done" else "red"
        self.console.print(f"  [{color}]{name}[/{color}]: {status}")

    def render_error(self, msg: str) -> None:
        self.console.print(f"[red]✗ {msg}[/red]")

    def render_info(self, msg: str) -> None:
        self.console.print(f"[dim]{msg}[/dim]")

    def render_user_prompt(self) -> str:
        """Display the prompt and read user input."""
        return input("\n> ")

    def render_welcome(self) -> None:
        self.console.print()
        self.console.print(SCA_LOGO)
        self.console.print(
            Panel.fit(
                "Simple Coding Agent — type your request or [bold]exit[/bold] to quit",
                border_style="blue",
            )
        )


class LiveMarkdownStream:
    """Context manager for streaming markdown to the terminal."""

    def __init__(self, console: Console):
        self.console = console
        self._buffer = ""
        self._live: Live | None = None

    def __enter__(self):
        self._buffer = ""
        self._live = Live(
            Markdown(""),
            console=self.console,
            refresh_per_second=10,
            vertical_overflow="visible",
        )
        self._live.start()
        return self

    def __exit__(self, *args):
        if self._live:
            self._live.stop()
            self._live = None
        # Print final rendered version
        if self._buffer.strip():
            self.console.print(Markdown(self._buffer))

    def add_token(self, token: str) -> None:
        self._buffer += token
        if self._live:
            self._live.update(Markdown(self._buffer + "▌"))
