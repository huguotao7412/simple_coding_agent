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
from dataclasses import dataclass

from .llm import LLMClient
from .context import ContextManager
from .tools.base import BaseTool, ToolResult
from .system_prompt import SYSTEM_PROMPT
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
    if platform.system() == "Windows":
        return _walk_tree_pure_python(workspace_dir)
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

        # Circuit breaker: track recent tool calls to detect loops
        self.action_history: deque[int] = deque(maxlen=5)

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

    def _hash_action(self, tool_name: str, args: dict) -> int:
        """Create a deterministic hash for a tool_name + args combination."""
        return hash(tool_name + json.dumps(args, sort_keys=True))

    def detect_loop(self, action_hash: int) -> bool:
        """Return True if action_hash appears >= 2 times in recent history."""
        return sum(1 for h in self.action_history if h == action_hash) >= 2

    def _check_circuit_breaker(
        self, tool_call_id: str, tool_name: str, args: dict
    ) -> bool:
        """Check for repeated tool calls and intervene if a loop is detected.

        Returns True if the circuit breaker fired (caller should skip execution),
        False if safe to proceed.
        """
        action_hash = self._hash_action(tool_name, args)
        if self.detect_loop(action_hash):
            intervention = (
                "System Alert: Detected repeated failed tool calls. "
                "STOP current action. Please reason about why it failed "
                "and use read or search codebase to gather new context."
            )
            self.ctx.add_tool_result(tool_call_id, intervention)
            self.action_history.append(action_hash)
            return True
        return False

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
            raw_args = re.sub(r"^```json\s*", "", raw_args, flags=re.IGNORECASE)
            raw_args = re.sub(r"\s*```$", "", raw_args).strip()
            args = json.loads(raw_args)
        except json.JSONDecodeError as e:
            self.ctx.add_tool_result(tc["id"], f"Error: invalid JSON arguments: {e}")
            self.action_history.append(self._hash_action(tool_name, {}))
            return (
                tool_name, {},
                ToolResult.fail(f"invalid JSON arguments: {e}"),
                f"Error: invalid JSON arguments: {e}",
                False,
            )

        # 2. Inject workspace_dir
        if tool_name in ("read", "write", "edit", "bash", "search_codebase"):
            args["workspace_dir"] = self.workspace_dir

        # 3. Circuit breaker check
        if self._check_circuit_breaker(tc["id"], tool_name, args):
            intervention = (
                "System Alert: Detected repeated failed tool calls. "
                "STOP current action. Please reason about why it failed "
                "and use read or search codebase to gather new context."
            )
            return (
                tool_name, args,
                ToolResult.fail(intervention),
                intervention,
                True,
            )

        # 4. Look up and execute tool
        tool = self.tools_by_name.get(tool_name)
        if tool is None:
            observation = f"Error: unknown tool '{tool_name}'. Available: {list(self.tools_by_name.keys())}"
            result = ToolResult.fail(f"unknown tool '{tool_name}'")
        else:
            try:
                result = await tool.execute(**args)
            except Exception as e:
                result = ToolResult.fail(str(e))

            if result.success:
                observation = result.content
            else:
                observation = f"ERROR: {result.error}"
                if result.content:
                    observation += f"\nPartial output: {result.content}"

        self.ctx.add_tool_result(tc["id"], observation)
        self.action_history.append(self._hash_action(tool_name, args))

        return tool_name, args, result, observation, False

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
                self.ctx.compress()

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
                return error_msg

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

            # Execute each tool call via shared method
            for tc in tool_calls:
                await self._execute_single_tool(tc)

    async def run_stream(
        self,
        user_input: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        self.ctx.add_user_message(user_input)
        tool_schemas = [t.schema for t in self.tools_by_name.values()]

        step_count = 0
        MAX_STEPS = 5

        while True:
            step_count += 1
            if step_count > MAX_STEPS:
                error_msg = "安全熔断：Agent 单轮工具调用次数已达上限(5次)。系统已强制暂停以防止死循环和网络崩溃。请根据当前线索直接提问。"
                self.ctx.add_assistant_message(content=error_msg)
                yield AgentEvent(type="error", content=error_msg)
                return

            if self.ctx.needs_compression():
                self.ctx.compress()
                yield AgentEvent(type="compaction")

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
                        yield AgentEvent(type="thought", token=token, content=token)
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
                yield AgentEvent(type="error", content=error_msg)
                return
            # === 流式接收修改结束 ===

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
                # Parse args for the tool_call event (lightweight, before execution)
                tool_name = tc["function"]["name"]
                try:
                    raw_args = tc["function"]["arguments"].strip()
                    raw_args = re.sub(r"^```json\s*", "", raw_args, flags=re.IGNORECASE)
                    raw_args = re.sub(r"\s*```$", "", raw_args).strip()
                    tool_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    tool_args = {}

                yield AgentEvent(
                    type="tool_call",
                    tool_name=tool_name,
                    tool_args=tool_args,
                )

                # Execute via shared method
                _, _, result, _, _ = await self._execute_single_tool(tc)

                yield AgentEvent(
                    type="tool_result",
                    tool_name=tool_name,
                    tool_result=result,
                )

