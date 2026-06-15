from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass

from .llm import LLMClient
from .context import ContextManager
from .tools.base import BaseTool, ToolResult
from .system_prompt import SYSTEM_PROMPT


@dataclass
class AgentEvent:
    type: str
    # "thought" / "tool_call" / "tool_result" / "compaction" / "done"
    content: str = ""
    tool_name: str | None = None
    tool_args: dict | None = None
    tool_result: ToolResult | None = None
    token: str = ""


def _walk_tree_pure_python(workspace_dir: str, max_depth: int = 2) -> str:
    """Fallback: generate tree-like directory listing using os.scandir()."""
    ignore_dirs = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"}

    def _walk(dirpath: str, prefix: str = "", depth: int = 0) -> list[str]:
        if depth >= max_depth:
            return []
        lines: list[str] = []
        try:
            entries = sorted(os.scandir(dirpath), key=lambda e: e.name.lower())
        except (PermissionError, OSError):
            return lines
        dirs = [e for e in entries if e.is_dir(follow_symlinks=False) and e.name not in ignore_dirs]
        files = [e for e in entries if e.is_file(follow_symlinks=False)]
        items = dirs + files
        for i, entry in enumerate(items):
            is_last = i == len(items) - 1
            connector = "└── " if is_last else "├── "
            next_prefix = prefix + ("    " if is_last else "│   ")
            if entry.is_dir(follow_symlinks=False):
                lines.append(f"{prefix}{connector}{entry.name}/")
                lines.extend(_walk(entry.path, next_prefix, depth + 1))
            else:
                lines.append(f"{prefix}{connector}{entry.name}")
        return lines

    root_name = os.path.basename(workspace_dir) or workspace_dir
    lines = [root_name + "/"]
    lines.extend(_walk(workspace_dir))
    return "\n".join(lines)


def get_workspace_tree(workspace_dir: str) -> str:
    """Get directory structure of workspace. Tries `tree` command first, falls back to pure Python."""
    try:
        result = subprocess.run(
            [
                "tree", "-L", "2", "-I",
                ".git|__pycache__|.venv|node_modules|.mypy_cache|.pytest_cache",
                workspace_dir,
            ],
            capture_output=True,
            timeout=10,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return _walk_tree_pure_python(workspace_dir)


def get_runtime_env() -> str:
    """Get OS info and exact Python version using stdlib only."""
    lines = [
        f"Operating System: {platform.system()} {platform.release()} ({platform.machine()})",
        f"Python Version: {sys.version}",
    ]
    return "\n".join(lines)


class Agent:
    """Core ReAct agent. Runs the think->act->observe loop."""

    def __init__(
        self,
        llm_client: LLMClient,
        context_manager: ContextManager,
        tools: list[BaseTool],
        workspace_dir: str,
    ):
        self.llm = llm_client
        self.tools_by_name = {t.name: t for t in tools}
        self.workspace_dir = workspace_dir

        # Build dynamic system prompt with environment context
        workspace_tree = get_workspace_tree(workspace_dir)
        runtime_env = get_runtime_env()
        dynamic_prompt = (
            SYSTEM_PROMPT
            + f"\n\n<workspace_context>\n{workspace_tree}\n</workspace_context>"
            + f"\n\n<environment_context>\n{runtime_env}\n</environment_context>"
        )
        # Override context_manager's system prompt with our dynamic version
        context_manager.messages[0] = {"role": "system", "content": dynamic_prompt}
        self.ctx = context_manager

    async def run(
        self,
        user_input: str,
        on_token: Callable[[str], None] | None = None,
    ) -> str:
        self.ctx.add_user_message(user_input)
        tool_schemas = [t.schema for t in self.tools_by_name.values()]

        while True:
            # Check context and compress if needed
            if self.ctx.needs_compression():
                await self.ctx.compress(self.llm)

            response = await self.llm.chat(
                messages=self.ctx.messages,
                tools=tool_schemas if tool_schemas else None,
                on_token=on_token,
            )

            tool_calls = response.get("tool_calls")

            if not tool_calls:
                # Final response -- no more tool calls
                self.ctx.add_assistant_message(
                    content=response.get("content"),
                    reasoning_content=response.get("reasoning_content"),
                )
                return response.get("content") or ""

            # Record assistant message with tool calls
            self.ctx.add_assistant_message(
                content=response.get("content"),
                tool_calls=tool_calls,
                reasoning_content=response.get("reasoning_content"),
            )

            # Execute each tool call
            for tc in tool_calls:
                tool_name = tc["function"]["name"]
                tool = self.tools_by_name.get(tool_name)

                if tool is None:
                    self.ctx.add_tool_result(
                        tc["id"],
                        f"Error: unknown tool '{tool_name}'. Available: {list(self.tools_by_name.keys())}",
                    )
                    continue

                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError as e:
                    self.ctx.add_tool_result(tc["id"], f"Error: invalid JSON arguments: {e}")
                    continue

                # Inject workspace_dir into all tools
                if tool_name in ("read", "write", "edit", "bash", "search_codebase"):
                    args["workspace_dir"] = self.workspace_dir

                result: ToolResult = await tool.execute(**args)

                # Build observation for the model
                if result.success:
                    observation = result.content
                else:
                    observation = f"ERROR: {result.error}"
                    if result.content:
                        observation += f"\nPartial output: {result.content}"

                self.ctx.add_tool_result(tc["id"], observation)

    async def run_stream(
        self,
        user_input: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        self.ctx.add_user_message(user_input)
        tool_schemas = [t.schema for t in self.tools_by_name.values()]

        while True:
            if self.ctx.needs_compression():
                await self.ctx.compress(self.llm)
                yield AgentEvent(type="compaction")

            tokens: list[str] = []

            def on_token(t: str) -> None:
                tokens.append(t)

            response = await self.llm.chat(
                messages=self.ctx.messages,
                tools=tool_schemas if tool_schemas else None,
                on_token=on_token,
            )

            for token in tokens:
                yield AgentEvent(type="thought", token=token, content=token)

            tool_calls = response.get("tool_calls")

            if not tool_calls:
                self.ctx.add_assistant_message(
                    content=response.get("content"),
                    reasoning_content=response.get("reasoning_content"),
                )
                yield AgentEvent(type="done", content=response.get("content") or "")
                return

            self.ctx.add_assistant_message(
                content=response.get("content"),
                tool_calls=tool_calls,
                reasoning_content=response.get("reasoning_content"),
            )

            for tc in tool_calls:
                tool_name = tc["function"]["name"]
                yield AgentEvent(
                    type="tool_call",
                    tool_name=tool_name,
                    tool_args=json.loads(tc["function"]["arguments"]),
                )

                tool = self.tools_by_name.get(tool_name)

                if tool is None:
                    result = ToolResult.fail(
                        f"unknown tool '{tool_name}'. Available: {list(self.tools_by_name.keys())}"
                    )
                else:
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError as e:
                        result = ToolResult.fail(f"invalid JSON arguments: {e}")
                    else:
                        if tool_name in ("read", "write", "edit", "bash", "search_codebase"):
                            args["workspace_dir"] = self.workspace_dir
                        try:
                            result = await tool.execute(**args)
                        except Exception as e:
                            result = ToolResult.fail(str(e))

                observation = (
                    result.content
                    if result.success
                    else f"ERROR: {result.error}\nPartial output: {result.content}" if result.content
                    else f"ERROR: {result.error}"
                )
                self.ctx.add_tool_result(tc["id"], observation)

                yield AgentEvent(
                    type="tool_result",
                    tool_name=tool_name,
                    tool_result=result,
                )
