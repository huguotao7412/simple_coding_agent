from __future__ import annotations

import json

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.status import Status
from rich.table import Table

from cli.report import RunReport

SCA_LOGO = r"""
  [cyan]  ____    ____      _     [/]
  [cyan] / ___|  / ___|    / \    [/]
  [cyan] \___ \ | |       / _ \   [/]
  [cyan]  ___) || |___   / ___ \  [/]
  [cyan] |____/  \____| /_/   \_\ [/]
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
        """Stop and clear the transient tool spinner."""
        if self._tool_status:
            self._tool_status.stop()
            self._tool_status = None

    def clear_actor_status(self) -> None:
        """Stop and clear the concurrent task table."""
        if self._actor_table:
            self._actor_table.stop()
            self._actor_table = None

    def render_actor_status(self, task_tree: dict) -> None:
        """Render concurrent Actor task status."""
        if not task_tree:
            return

        status_styles = {
            "pending": ("..", "dim yellow"),
            "running": (">>", "bold cyan"),
            "verifying": ("??", "bold magenta"),
            "done": ("OK", "bold green"),
            "failed": ("!!", "bold red"),
            "blocked": ("--", "dim"),
        }

        table = Table(
            title="Actor Tasks",
            title_style="bold blue",
            show_header=True,
            header_style="bold",
        )
        table.add_column("Task ID", style="dim", width=14)
        table.add_column("Description", width=40)
        table.add_column("Status", width=12)

        for tid, task in task_tree.items():
            icon, style = status_styles.get(task.get("status", ""), ("??", ""))
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
        """Show a transient one-line tool execution status."""
        if status == "running":
            msg = f"[dim cyan]running tool:[/] [bold cyan]{name}[/]"
            if not self._tool_status:
                self._tool_status = self.console.status(msg, spinner="dots")
                self._tool_status.start()
            else:
                self._tool_status.update(msg)
        elif status == "failed" and self._tool_status:
            self._tool_status.update(f"[red]tool {name} failed[/]")

    def render_tool_call(self, name: str, args: dict | None) -> None:
        """Render an auditable tool-call line before execution."""
        args = args or {}
        arg_bits = []
        for key, value in list(args.items())[:3]:
            text = repr(value)
            if len(text) > 80:
                text = text[:77] + "..."
            arg_bits.append(f"{key}={text}")
        suffix = f" ({', '.join(arg_bits)})" if arg_bits else ""
        self.console.print(f"[dim]tool call[/] [bold cyan]{name}[/]{suffix}")

    def render_tool_result(self, name: str, success: bool, detail: str = "") -> None:
        """Render a compact, persistent tool result."""
        style = "green" if success else "red"
        label = "ok" if success else "failed"
        if detail:
            detail = detail.replace("\n", " ")
            if len(detail) > 160:
                detail = detail[:157] + "..."
            detail = f" - {detail}"
        self.console.print(f"[{style}]tool {label}[/] [bold]{name}[/]{detail}")

    def render_compaction(self, mode: str = "") -> None:
        text = "Context compressed"
        if mode:
            text += f" ({mode})"
        self.console.print(f"[dim]{text}[/dim]")

    def render_error(self, msg: str) -> None:
        self.console.print(f"[red]ERROR: {msg}[/red]")

    def render_info(self, msg: str) -> None:
        self.console.print(f"[dim]{msg}[/dim]")

    def render_token_stats(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Render a compact token consumption summary line."""
        total = prompt_tokens + completion_tokens
        self.console.print(
            f"\n[dim]Tokens: prompt={prompt_tokens:,} "
            f"completion={completion_tokens:,} "
            f"total={total:,}[/]"
        )

    def render_run_report(self, report: RunReport) -> None:
        """Render a final per-run audit summary."""
        table = Table(
            title="Run Report",
            title_style="bold blue",
            show_header=False,
            box=None,
            padding=(0, 1),
        )
        table.add_column("Key", style="dim", width=18)
        table.add_column("Value")

        tool_count = len(report.tool_calls)
        failed_tools = report.failed_tool_count
        table.add_row("Tools", f"{tool_count} call(s), {failed_tools} failed")

        if report.task_assessment:
            table.add_row(
                "Task strategy",
                str(report.task_assessment.get("strategy", "unknown")),
            )

        if report.sandbox_backends:
            table.add_row(
                "Command sandbox",
                f"{', '.join(sorted(report.sandbox_backends))}; "
                f"isolated={'yes' if report.isolated_execution_observed else 'no'}",
            )

        if report.files_referenced:
            files = sorted(report.files_referenced)
            rendered = ", ".join(files[:5])
            if len(files) > 5:
                rendered += f", +{len(files) - 5} more"
            table.add_row("Files referenced", rendered)
        else:
            table.add_row("Files referenced", "none observed")

        if report.actor_status_counts:
            actor_summary = ", ".join(
                f"{status}={count}"
                for status, count in sorted(report.actor_status_counts.items())
            )
            table.add_row("Actor tasks", actor_summary)

        if report.total_tokens:
            table.add_row(
                "Tokens",
                f"prompt={report.prompt_tokens:,}, completion={report.completion_tokens:,}, total={report.total_tokens:,}",
            )

        if report.compactions:
            table.add_row("Compactions", str(report.compactions))

        if report.errors:
            table.add_row("Errors", str(len(report.errors)))

        table.add_row("Outcome", report.outcome)

        self.console.print()
        self.console.print(table)

    def render_user_prompt(self) -> str:
        """Display the prompt and read user input."""
        return input("\n> ")

    def render_approval_prompt(self, payload: dict) -> bool:
        """Render a structured LangGraph interrupt and request a decision."""
        risk = payload.get("risk_level", "high")
        reasons = payload.get("risk_reasons", [])
        capabilities = payload.get("requested_capabilities", [])
        scope = payload.get("target_scope", [])
        self.console.print(Panel(
            "\n".join((
                f"Risk: {risk}",
                f"Reasons: {json.dumps(reasons, ensure_ascii=False)}",
                f"Capabilities: {json.dumps(capabilities, ensure_ascii=False)}",
                f"Scope: {json.dumps(scope, ensure_ascii=False)}",
            )),
            title="Human approval required",
            border_style="yellow",
        ))
        answer = input("Approve this run? [y/N] ").strip().lower()
        return answer in {"y", "yes"}

    def render_welcome(self) -> None:
        self.console.print()
        self.console.print(SCA_LOGO)
        self.console.print(
            Panel.fit(
                "Simple Coding Agent - type your request or [bold]exit[/bold] to quit",
                border_style="blue",
            )
        )


class LiveMarkdownStream:
    """Rich-backed live Markdown stream."""

    def __init__(self, console: Console):
        self.console = console
        self._buffer = ""
        self._live = Live(console=self.console, refresh_per_second=12, transient=False)

    def __enter__(self):
        self._buffer = ""
        self._live.start()
        return self

    def __exit__(self, *args):
        self._live.stop()

    def add_token(self, token: str) -> None:
        self._buffer += token
        self._live.update(Markdown(self._buffer))
