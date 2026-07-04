from __future__ import annotations

import re
import json
import os
import asyncio
import platform
import subprocess
import sys
from collections import deque
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Literal

from .llm import LLMClient
from .context import ContextManager
from .tools.base import BaseTool, ToolResult

from .exceptions import LLMAPIError


@dataclass
class AgentEvent:
    type: str
    # "thought" / "tool_call" / "tool_result" / "compaction" / "error" / "done"
    content: str = ""
    tool_name: str | None = None
    tool_args: dict | None = None
    tool_result: ToolResult | None = None
    token: str = ""
    actor_id: str = ""


@dataclass
class ActorSummary:
    task_id: str
    status: Literal["done", "failed"]  # noqa: F821
    files_modified: list[str] = field(default_factory=list)
    bugs_found: list[str] = field(default_factory=list)
    key_findings: str = ""
    suggested_next_steps: str = ""
    raw_output: str = ""


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


def get_workspace_tree(workspace_dir: str, max_lines: int = 100) -> str:
    """Get directory structure of workspace. Tries `tree` command first, falls back to pure Python.

    Truncates output to max_lines to prevent context overflow in large projects.
    """
    if platform.system() == "Windows":
        raw = _walk_tree_pure_python(workspace_dir)
    else:
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
                encoding="utf-8", 
                errors="replace",
            )
            if result.returncode == 0 and result.stdout.strip():
                raw = result.stdout.strip()
            else:
                raw = _walk_tree_pure_python(workspace_dir)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            raw = _walk_tree_pure_python(workspace_dir)

    lines = raw.split("\n")
    if len(lines) > max_lines:
        raw = (
            "\n".join(lines[:max_lines])
            + "\n... [Workspace tree truncated due to size. "
            + "Use 'list_dir' tool to explore specific directories] ..."
        )
    return raw


def get_runtime_env() -> str:
    """Get OS info and exact Python version using stdlib only."""
    lines = [
        f"Operating System: {platform.system()} {platform.release()} ({platform.machine()})",
        f"Python Version: {sys.version}",
    ]
    return "\n".join(lines)


class ActorAgent:
    """Core ReAct agent. Runs the think->act->observe loop."""

    def __init__(
        self,
        llm_client: LLMClient,
        context_manager: ContextManager,
        tools: list[BaseTool] | None = None,
        workspace_dir: str = "",
        actor_id: str = "",
        task_context: str = "",
        tool_provider: Any | None = None,
    ):
        self.actor_id = actor_id
        self.task_context = task_context
        self.llm = llm_client
        self.workspace_dir = workspace_dir
        self._tool_provider = tool_provider

        # Local tool fallback (used when tool_provider is None)
        self.tools_by_name = {t.name: t for t in tools} if tools else {}

        # Lightweight repeat detection (Actor-level only)
        self._recent_actions: deque[int] = deque(maxlen=10)

        # Keep context_manager.messages[0] as the pure static SYSTEM_PROMPT.
        # Dynamic workspace/environment context is injected per-request to
        # preserve prompt cache hits on messages[0].
        self.ctx = context_manager

    def _build_dynamic_context_msg(self) -> dict:
        """Build a system-level message with current workspace tree and runtime env.

        This is appended to messages at API-call time rather than baked into
        messages[0], so the prefix (static SYSTEM_PROMPT) always hits the cache.
        """
        workspace_tree = get_workspace_tree(self.workspace_dir)
        runtime_env = get_runtime_env()
        content = (
            f"<workspace_context>\n{workspace_tree}\n</workspace_context>\n"
            f"<environment_context>\n{runtime_env}\n</environment_context>"
        )
        return {"role": "system", "content": content}

    async def _execute_single_tool(
        self,
        tc: dict,
    ) -> tuple[str, dict, ToolResult, str, bool]:
        """Execute a single tool call. Shared by run() and run_stream().

        Handles: JSON parsing, markdown stripping, workspace injection,
        circuit breaker, tool lookup, execution, and history recording.

        Returns:
            (tool_name, tool_args, result, observation, circuit_broken)
        """
        tool_name = tc["function"]["name"]

        # 1. Parse arguments
        try:
            raw_args = tc["function"]["arguments"].strip()
            # 防线1: 放宽 markdown 剔除正则，让 "json" 关键字变为可选
            raw_args = re.sub(r"^\s*```(?:json\s*)?", "", raw_args, flags=re.IGNORECASE)
            raw_args = re.sub(r"\s*```$", "", raw_args).strip()
            # 防线2: 空字符串拦截，避免将空字符串传给 json.loads()
            if not raw_args:
                args = {}
            else:
                args = json.loads(raw_args)
            # 防线3: 类型守卫，防止解析出列表/字符串/None 等非字典类型导致后续崩溃
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError as e:
            error_hint = (
                f"Error: Invalid JSON format in arguments: {e}\n"
                "CRITICAL HINT: \n"
                "1. If you are writing multi-line code, you MUST escape newlines as \\n and double quotes as \\\".\n"
                "2. Ensure there are NO trailing commas.\n"
                "3. Do NOT wrap the arguments in Markdown blockticks like ```json ... ``` inside the tool call.\n"
                "Please fix the format and call the tool again."
            )
            self.ctx.add_tool_result(tc["id"], error_hint)
            return (
                tool_name, {},
                ToolResult.fail(error_hint),
                error_hint,
                False,
            )

        # 2. Inject workspace_dir
        if tool_name in ("read", "write", "edit", "bash", "search_codebase"):
            args["workspace_dir"] = self.workspace_dir

        # 3. Simple repeat detection (lightweight, Actor-level only)
        action_hash = hash(tool_name + json.dumps(args, sort_keys=True))
        if self._recent_actions.count(action_hash) >= 2:
            intervention = (
                "System Alert: Repeated tool call detected. "
                "Please try a different approach."
            )
            self.ctx.add_tool_result(tc["id"], intervention)
            return (tool_name, args, ToolResult.fail(intervention), intervention, True)
        self._recent_actions.append(action_hash)

        # 4. Route through MCP or local tool
        if self._tool_provider is not None:
            # MCP path: workspace_dir is already bound to the MCP server at startup
            result = await self._tool_provider.call_tool(tool_name, args)
            if result.success:
                observation = result.content
            else:
                observation = f"ERROR: {result.error}"
                if result.content:
                    observation += f"\nPartial output: {result.content}"
        else:
            # Local tool path (fallback)
            tool = self.tools_by_name.get(tool_name)
            if tool is None:
                observation = f"Error: unknown tool '{tool_name}'. Available: {list(self.tools_by_name.keys())}"
                result = ToolResult.fail(f"unknown tool '{tool_name}'")
            else:
                try:
                    result = await tool.execute(**args)
                except Exception as e:
                    result = ToolResult.fail(f"Internal Tool Error: {str(e)}")

                # If the failure is caused by an internal code bug (e.g. AttributeError),
                # give the model a stronger stop signal to prevent retry loops
                if not result.success and "AttributeError" in str(result.error):
                    result.error += (
                        " (CRITICAL: 此工具当前存在内部故障，请立即停止调用并向用户报告)"
                    )

                if result.success:
                    observation = result.content
                else:
                    observation = f"ERROR: {result.error}"
                    if result.content:
                        observation += f"\nPartial output: {result.content}"

        self.ctx.add_tool_result(tc["id"], observation)

        return tool_name, args, result, observation, False

    async def run(
        self,
        user_input: str,
        on_token: Callable[[str], None] | None = None,
    ) -> ActorSummary:
        self.ctx.add_user_message(user_input)

        # Get tool schemas from MCP provider or local tools
        if self._tool_provider is not None:
            tool_schemas = await self._tool_provider.list_tools()
        else:
            tool_schemas = [t.schema for t in self.tools_by_name.values()]

        step_count = 0
        MAX_STEPS = 30

        while True:
            step_count += 1
            if step_count > MAX_STEPS:
                error_msg = "安全熔断：Agent 已达到最大步数限制。请尝试简化请求后重试。"
                self.ctx.add_assistant_message(content=error_msg)
                return ActorSummary(
                    task_id=self.actor_id,
                    status="failed",
                    key_findings=error_msg,
                    raw_output=error_msg,
                )

            # Check context and compress if needed
            if self.ctx.needs_compression(self.llm):
                await self.ctx.compress(self.llm)
            elif self.ctx.needs_proactive_compression(self.llm):
                self.ctx._lightweight_compress()

            # Build payload: static prefix (cacheable) + dynamic context tail
            payload_messages = self.ctx.messages + [self._build_dynamic_context_msg()]

            try:
                response = await self.llm.chat(
                    messages=payload_messages,
                    tools=tool_schemas if tool_schemas else None,
                    on_token=on_token,
                )
            except LLMAPIError as e:
                error_msg = str(e)
                self.ctx.add_assistant_message(content=error_msg)
                return ActorSummary(
                    task_id=self.actor_id,
                    status="failed",
                    key_findings=error_msg,
                    raw_output=error_msg,
                )

            tool_calls = response.get("tool_calls")

            if not tool_calls:
                # Final response -- no more tool calls
                self.ctx.add_assistant_message(
                    content=response.get("content"),
                    reasoning_content=response.get("reasoning_content"),
                )
                return ActorSummary(
                    task_id=self.actor_id,
                    status="done",
                    key_findings=response.get("content") or "",
                    raw_output=response.get("content") or "",
                )

            # Record assistant message with tool calls
            self.ctx.add_assistant_message(
                content=response.get("content"),
                tool_calls=tool_calls,
                reasoning_content=response.get("reasoning_content"),
            )

            # Execute each tool call via shared method
            for tc in tool_calls:
                await self._execute_single_tool(tc)

    async def run_stream(
        self,
        user_input: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        self.ctx.add_user_message(user_input)
        # Get tool schemas from MCP provider or local tools
        if self._tool_provider is not None:
            tool_schemas = await self._tool_provider.list_tools()
        else:
            tool_schemas = [t.schema for t in self.tools_by_name.values()]

        step_count = 0
        MAX_STEPS = 30

        while True:
            step_count += 1
            if step_count > MAX_STEPS:
                error_msg = "安全熔断：Agent 单轮工具调用次数已达上限(5次)。系统已强制暂停以防止死循环和网络崩溃。请根据当前线索直接提问。"
                self.ctx.add_assistant_message(content=error_msg)
                yield AgentEvent(type="error", content=error_msg, actor_id=self.actor_id)
                return

            if self.ctx.needs_compression(self.llm):
                await self.ctx.compress(self.llm)
                yield AgentEvent(type="compaction", actor_id=self.actor_id)
            elif self.ctx.needs_proactive_compression(self.llm):
                self.ctx._lightweight_compress()
                yield AgentEvent(type="compaction", content="lightweight", actor_id=self.actor_id)

            queue = asyncio.Queue()

            def on_token(t: str) -> None:
                # 使用 put_nowait 将字塞入队列，不阻塞回调
                queue.put_nowait(t)

            # Build payload: static prefix (cacheable) + dynamic context tail
            payload_messages = self.ctx.messages + [self._build_dynamic_context_msg()]

            # 将 LLM 请求作为后台任务启动，不要用 await 在这里死等
            chat_task = asyncio.create_task(
                self.llm.chat(
                    messages=payload_messages,
                    tools=tool_schemas if tool_schemas else None,
                    on_token=on_token,
                )
            )

            try:
                # 只要后台任务没结束，或者队列里还有字，就一直循环取字
                while not chat_task.done() or not queue.empty():
                    try:
                        token = await asyncio.wait_for(queue.get(), timeout=0.05)
                        yield AgentEvent(type="thought", token=token, content=token, actor_id=self.actor_id)
                    except asyncio.TimeoutError:
                        continue
            finally:
                # 极简防御：一旦生成器意外销毁或中断，立刻掐断 LLM 后台请求
                if not chat_task.done():
                    chat_task.cancel()

            try:
                # 任务彻底结束后，获取最终的完整 response
                response = await chat_task
            except LLMAPIError as e:
                error_msg = str(e)
                self.ctx.add_assistant_message(content=error_msg)
                yield AgentEvent(type="error", content=error_msg, actor_id=self.actor_id)
                return
            # === 流式接收修改结束 ===

            tool_calls = response.get("tool_calls")

            if not tool_calls:
                self.ctx.add_assistant_message(
                    content=response.get("content"),
                    reasoning_content=response.get("reasoning_content"),
                )
                yield AgentEvent(type="done", content=response.get("content") or "", actor_id=self.actor_id)
                return

            self.ctx.add_assistant_message(
                content=response.get("content"),
                tool_calls=tool_calls,
                reasoning_content=response.get("reasoning_content"),
            )

            for tc in tool_calls:
                # Parse args for the tool_call event (lightweight, before execution)
                tool_name = tc["function"]["name"]
                try:
                    raw_args = tc["function"]["arguments"].strip()
                    # 防线1: 放宽 markdown 剔除正则，让 "json" 关键字变为可选
                    raw_args = re.sub(r"^\s*```(?:json\s*)?", "", raw_args, flags=re.IGNORECASE)
                    raw_args = re.sub(r"\s*```$", "", raw_args).strip()
                    # 防线2: 空字符串拦截，避免将空字符串传给 json.loads()
                    if not raw_args:
                        tool_args = {}
                    else:
                        tool_args = json.loads(raw_args)
                    # 防线3: 类型守卫，防止解析出列表/字符串/None 等非字典类型
                    if not isinstance(tool_args, dict):
                        tool_args = {}
                except json.JSONDecodeError:
                    tool_args = {}

                yield AgentEvent(
                    type="tool_call",
                    tool_name=tool_name,
                    tool_args=tool_args,
                    actor_id=self.actor_id,
                )

                # Execute via shared method
                _, _, result, _, _ = await self._execute_single_tool(tc)

                yield AgentEvent(
                    type="tool_result",
                    tool_name=tool_name,
                    tool_result=result,
                    actor_id=self.actor_id,
                )


# Backward compatibility alias — will be removed after Planner migration
Agent = ActorAgent
