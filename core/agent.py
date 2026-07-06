from __future__ import annotations

import os
import platform
import subprocess
import sys
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from .context import ContextManager
from .llm import LLMClient
from .runtime import AgentEvent, AgentRuntime
from .tools.base import BaseTool


@dataclass
class ActorSummary:
    task_id: str
    status: Literal["done", "failed"]
    files_modified: list[str] = field(default_factory=list)
    bugs_found: list[str] = field(default_factory=list)
    key_findings: str = ""
    suggested_next_steps: str = ""
    raw_output: str = ""


def _walk_tree_pure_python(workspace_dir: str, max_depth: int = 2) -> str:
    """Generate a compact tree-like directory listing using stdlib only."""
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
            connector = "`-- " if is_last else "|-- "
            next_prefix = prefix + ("    " if is_last else "|   ")
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


def get_workspace_tree(workspace_dir: str, max_lines: int = 100) -> str:
    """Return a compact workspace tree, truncated for prompt safety."""
    if platform.system() == "Windows":
        raw = _walk_tree_pure_python(workspace_dir)
    else:
        try:
            result = subprocess.run(
                [
                    "tree",
                    "-L",
                    "2",
                    "-I",
                    ".git|__pycache__|.venv|node_modules|.mypy_cache|.pytest_cache",
                    workspace_dir,
                ],
                capture_output=True,
                timeout=10,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            raw = result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else _walk_tree_pure_python(workspace_dir)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            raw = _walk_tree_pure_python(workspace_dir)

    lines = raw.split("\n")
    if len(lines) > max_lines:
        raw = (
            "\n".join(lines[:max_lines])
            + "\n... [Workspace tree truncated. Use list_dir to explore specific directories.] ..."
        )
    return raw


def get_runtime_env() -> str:
    """Return OS and Python runtime details for agent context."""
    return "\n".join([
        f"Operating System: {platform.system()} {platform.release()} ({platform.machine()})",
        f"Python Version: {sys.version}",
    ])


class ActorAgent:
    """Actor wrapper around the shared transparent ReAct runtime."""

    def __init__(
        self,
        llm_client: LLMClient,
        context_manager: ContextManager,
        tools: list[BaseTool] | None = None,
        workspace_dir: str = "",
        actor_id: str = "",
        task_context: str = "",
        tool_provider: Any | None = None,
        max_steps: int = 30,
    ):
        self.actor_id = actor_id
        self.task_context = task_context
        self.llm = llm_client
        self.workspace_dir = workspace_dir
        self._tool_provider = tool_provider
        self.max_steps = max_steps
        self.tools_by_name = {t.name: t for t in tools} if tools else {}
        self.ctx = context_manager

    def _build_dynamic_context_msg(self) -> dict:
        content = (
            f"<workspace_context>\n{get_workspace_tree(self.workspace_dir)}\n</workspace_context>\n"
            f"<environment_context>\n{get_runtime_env()}\n</environment_context>"
        )
        return {"role": "system", "content": content}

    def _runtime(self) -> AgentRuntime:
        return AgentRuntime(
            llm_client=self.llm,
            context_manager=self.ctx,
            tools=list(self.tools_by_name.values()),
            workspace_dir=self.workspace_dir,
            max_steps=self.max_steps,
            tool_provider=self._tool_provider,
            actor_id=self.actor_id,
            dynamic_context_builder=self._build_dynamic_context_msg,
        )

    async def run(
        self,
        user_input: str,
        on_token: Callable[[str], None] | None = None,
    ) -> ActorSummary:
        runtime = self._runtime()
        content = await runtime.run(user_input, on_token=on_token)
        return ActorSummary(
            task_id=self.actor_id,
            status="done" if runtime.last_result_success else "failed",
            key_findings=content,
            raw_output=content,
        )

    async def run_stream(
        self,
        user_input: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        async for event in self._runtime().run_stream(user_input):
            yield event


# Backward compatibility alias; callers should migrate to ActorAgent.
Agent = ActorAgent
