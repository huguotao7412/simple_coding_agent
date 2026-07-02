from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.live import Live
from rich.status import Status

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
        self._tool_status: Status | None = None

    def render_markdown(self, text: str) -> None:
        if text.strip():
            self.console.print(Markdown(text))

    def stream_markdown(self) -> "LiveMarkdownStream":
        return LiveMarkdownStream(self.console)

    def clear_tool_status(self) -> None:
        """停止并清除工具执行的临时状态"""
        if self._tool_status:
            self._tool_status.stop()
            self._tool_status = None

    def render_tool_status(self, name: str, status: str) -> None:
        """Show a one-line tool execution status."""
        if status == "running":
            msg = f"[dim cyan]⚡ 正在执行工具:[/] [bold cyan]{name}[/]"
            if not self._tool_status:
                self._tool_status = self.console.status(msg, spinner="dots")
                self._tool_status.start()
            else:
                self._tool_status.update(msg)
        elif status == "failed":
            if self._tool_status:
                self._tool_status.update(f"[red]❌ 工具 {name} 执行失败，等待修正...[/]")

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
            vertical_overflow="ellipsis",
            transient=True,
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
