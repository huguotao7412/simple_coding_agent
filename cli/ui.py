from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.live import Live
from rich.status import Status
from rich.table import Table

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
        self._actor_table: Live | None = None

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

    def clear_actor_status(self) -> None:
        """停止并清除并发任务状态表"""
        if self._actor_table:
            self._actor_table.stop()
            self._actor_table = None

    def render_actor_status(self, task_tree: dict) -> None:
        """渲染并发 Actor 执行状态表。

        Bridge 收到 actor_update 事件时调用。
        同一批 delegate 内的多次调用会原地更新表格。
        """
        if not task_tree:
            return

        status_styles = {
            "pending":  ("..", "dim yellow"),
            "running":  (">>", "bold cyan"),
            "done":     ("OK", "bold green"),
            "failed":   ("!!", "bold red"),
        }

        table = Table(
            title="并发任务状态",
            title_style="bold blue",
            show_header=True,
            header_style="bold",
        )
        table.add_column("Task ID", style="dim", width=14)
        table.add_column("任务描述", width=40)
        table.add_column("状态", width=12)

        for tid, task in task_tree.items():
            icon, style = status_styles.get(task.get("status", ""), ("❓", ""))
            status_text = f"{icon} {task['status']}"
            desc = (task.get("description", "") or "")[:38]
            table.add_row(tid, desc, f"[{style}]{status_text}[/]")

        if self._actor_table:
            self._actor_table.update(table)
        else:
            self._actor_table = Live(
                table,
                console=self.console,
                refresh_per_second=4,
                transient=False,
            )
            self._actor_table.start()

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

    def render_token_stats(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Render a compact token consumption summary line."""
        total = prompt_tokens + completion_tokens
        self.console.print(
            f"\n[dim]💡 Token: prompt={prompt_tokens:,} "
            f"completion={completion_tokens:,} "
            f"total={total:,}[/]"
        )

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
    """使用 Rich Live 真正实现流式 Markdown 渲染。"""
    def __init__(self, console: Console):
        self.console = console
        self._buffer = ""
        # refresh_per_second 适度调高保证流畅，但不要太高以防止终端卡顿
        self._live = Live(console=self.console, refresh_per_second=12, transient=False)

    def __enter__(self):
        self._buffer = ""
        self._live.start()
        return self

    def __exit__(self, *args):
        self._live.stop()

    def add_token(self, token: str) -> None:
        self._buffer += token
        # 每次接到新 token，就更新 Live 面板中的 Markdown 渲染
        self._live.update(Markdown(self._buffer))