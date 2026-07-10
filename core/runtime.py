from __future__ import annotations

import json
import re
import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .context import ContextManager
from .events import AgentEvent
from .exceptions import LLMAPIError
from .llm import LLMClient
from .tools.base import BaseTool, ToolResult


WORKSPACE_AWARE_TOOLS = {
    "apply_patch",
    "bash",
    "delegate",
    "edit",
    "list_dir",
    "read",
    "read_outline",
    "search_codebase",
    "update_state",
    "write",
}


@dataclass
class ParsedToolCall:
    tool_name: str
    args: dict
    error: str | None = None


def parse_tool_call(tc: dict) -> ParsedToolCall:
    """Parse an OpenAI-style tool call into a tool name and object args."""
    function = tc.get("function", {})
    tool_name = function.get("name", "")
    raw_args = str(function.get("arguments") or "").strip()
    raw_args = re.sub(r"^\s*```(?:json\s*)?", "", raw_args, flags=re.IGNORECASE)
    raw_args = re.sub(r"\s*```$", "", raw_args).strip()

    if not raw_args:
        return ParsedToolCall(tool_name=tool_name, args={})

    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError as e:
        return ParsedToolCall(
            tool_name=tool_name,
            args={},
            error=(
                f"Invalid JSON format in arguments: {e}. "
                "Escape newlines as \\n, escape double quotes as \\\" and remove trailing commas."
            ),
        )

    if not isinstance(args, dict):
        args = {}

    return ParsedToolCall(tool_name=tool_name, args=args)


class AgentRuntime:
    """Shared ReAct runtime for Planner and Actor-style agents."""

    def __init__(
        self,
        llm_client: LLMClient,
        context_manager: ContextManager,
        tools: list[BaseTool] | None = None,
        workspace_dir: str = "",
        max_steps: int = 30,
        tool_provider: Any | None = None,
        actor_id: str = "",
        dynamic_context_builder: Callable[[], dict] | None = None,
        after_tool_call: Callable[[str, ToolResult], Awaitable[list[AgentEvent]]] | None = None,
        emit_token_stats: bool = False,
    ) -> None:
        self.llm = llm_client
        self.ctx = context_manager
        self.workspace_dir = workspace_dir
        self.max_steps = max_steps
        self.tool_provider = tool_provider
        self.actor_id = actor_id
        self.dynamic_context_builder = dynamic_context_builder
        self.after_tool_call = after_tool_call
        self.emit_token_stats = emit_token_stats
        self.tools_by_name = {t.name: t for t in tools} if tools else {}
        self._recent_actions: deque[int] = deque(maxlen=10)
        self.last_result_success = True

    async def _list_tool_schemas(self) -> list[dict]:
        if self.tool_provider is not None:
            return await self.tool_provider.list_tools()
        return [t.schema for t in self.tools_by_name.values()]

    def _payload_messages(self) -> list[dict]:
        if self.dynamic_context_builder is None:
            return self.ctx.messages
        return self.ctx.messages + [self.dynamic_context_builder()]

    async def _execute_single_tool(self, tc: dict) -> tuple[str, dict, ToolResult, bool]:
        parsed = parse_tool_call(tc)
        tool_name = parsed.tool_name
        args = parsed.args

        if parsed.error is not None:
            result = ToolResult.fail(parsed.error)
            self.ctx.add_tool_result(tc["id"], f"Error: {parsed.error}")
            return tool_name, args, result, False

        if tool_name in WORKSPACE_AWARE_TOOLS and self.workspace_dir:
            args["workspace_dir"] = self.workspace_dir

        action_hash = hash(tool_name + json.dumps(args, sort_keys=True))
        if self._recent_actions.count(action_hash) >= 2:
            intervention = "System Alert: Repeated tool call detected. Please try a different approach."
            result = ToolResult.fail(intervention)
            self.ctx.add_tool_result(tc["id"], intervention)
            return tool_name, args, result, True
        self._recent_actions.append(action_hash)

        if self.tool_provider is not None:
            result = await self.tool_provider.call_tool(tool_name, args)
            if result.success:
                observation = result.content
            else:
                observation = f"ERROR: {result.error}"
                if result.content:
                    observation += f"\nPartial output: {result.content}"
            self.ctx.add_tool_result(tc["id"], observation)
            return tool_name, args, result, False

        tool = self.tools_by_name.get(tool_name)
        if tool is None:
            result = ToolResult.fail(f"unknown tool '{tool_name}'")
            observation = f"Error: unknown tool '{tool_name}'. Available: {list(self.tools_by_name.keys())}"
            self.ctx.add_tool_result(tc["id"], observation)
            return tool_name, args, result, False

        try:
            result = await tool.execute(**args)
        except Exception as e:
            result = ToolResult.fail(f"Internal Tool Error: {str(e)}")

        if result.success:
            observation = result.content
        else:
            observation = f"ERROR: {result.error}"
            if result.content:
                observation += f"\nPartial output: {result.content}"

        self.ctx.add_tool_result(tc["id"], observation)
        return tool_name, args, result, False

    async def _run_after_tool_hook(self, tool_name: str, result: ToolResult) -> list[AgentEvent]:
        if self.after_tool_call is None:
            return []
        return await self.after_tool_call(tool_name, result)

    async def run(self, user_input: str, on_token=None) -> str:
        self.ctx.add_user_message(user_input)
        tool_schemas = await self._list_tool_schemas()

        step_count = 0
        while True:
            step_count += 1
            if step_count > self.max_steps:
                error_msg = "Safety stop: agent reached the maximum step limit."
                self.last_result_success = False
                self.ctx.add_assistant_message(content=error_msg)
                return error_msg

            if self.ctx.needs_compression(self.llm):
                await self.ctx.compress(self.llm)
            elif self.ctx.needs_proactive_compression(self.llm):
                self.ctx._lightweight_compress()

            try:
                response = await self.llm.chat(
                    messages=self._payload_messages(),
                    tools=tool_schemas if tool_schemas else None,
                    on_token=on_token,
                )
            except LLMAPIError as e:
                error_msg = str(e)
                self.last_result_success = False
                self.ctx.add_assistant_message(content=error_msg)
                return error_msg

            tool_calls = response.get("tool_calls")
            if not tool_calls:
                content = response.get("content") or ""
                self.ctx.add_assistant_message(
                    content=content,
                    reasoning_content=response.get("reasoning_content"),
                )
                self.last_result_success = True
                return content

            self.ctx.add_assistant_message(
                content=response.get("content"),
                tool_calls=tool_calls,
                reasoning_content=response.get("reasoning_content"),
            )

            for tc in tool_calls:
                await self._execute_single_tool(tc)

    async def run_stream(self, user_input: str) -> AsyncGenerator[AgentEvent, None]:
        self.ctx.add_user_message(user_input)
        tool_schemas = await self._list_tool_schemas()

        step_count = 0
        total_prompt_tokens = 0
        total_completion_tokens = 0
        while True:
            step_count += 1
            if step_count > self.max_steps:
                error_msg = "Safety stop: agent reached the maximum step limit."
                self.last_result_success = False
                self.ctx.add_assistant_message(content=error_msg)
                if self.emit_token_stats:
                    yield AgentEvent(
                        type="token_stats",
                        content=json.dumps({
                            "prompt_tokens": total_prompt_tokens,
                            "completion_tokens": total_completion_tokens,
                        }),
                        actor_id=self.actor_id,
                    )
                yield AgentEvent(type="error", content=error_msg, actor_id=self.actor_id)
                return

            if self.ctx.needs_compression(self.llm):
                await self.ctx.compress(self.llm)
                yield AgentEvent(type="compaction", actor_id=self.actor_id)
            elif self.ctx.needs_proactive_compression(self.llm):
                self.ctx._lightweight_compress()
                yield AgentEvent(type="compaction", content="lightweight", actor_id=self.actor_id)

            queue: asyncio.Queue[str] = asyncio.Queue()

            def on_token(token: str) -> None:
                queue.put_nowait(token)

            chat_task = asyncio.create_task(
                self.llm.chat(
                    messages=self._payload_messages(),
                    tools=tool_schemas if tool_schemas else None,
                    on_token=on_token,
                )
            )

            try:
                while not chat_task.done() or not queue.empty():
                    try:
                        token = await asyncio.wait_for(queue.get(), timeout=0.05)
                    except asyncio.TimeoutError:
                        continue
                    yield AgentEvent(
                        type="thought",
                        token=token,
                        content=token,
                        actor_id=self.actor_id,
                    )
            finally:
                if not chat_task.done():
                    chat_task.cancel()

            try:
                response = await chat_task
                usage = response.get("_usage", {})
                total_prompt_tokens += usage.get("prompt_tokens", 0)
                total_completion_tokens += usage.get("completion_tokens", 0)
            except LLMAPIError as e:
                error_msg = str(e)
                self.last_result_success = False
                self.ctx.add_assistant_message(content=error_msg)
                if self.emit_token_stats:
                    yield AgentEvent(
                        type="token_stats",
                        content=json.dumps({
                            "prompt_tokens": total_prompt_tokens,
                            "completion_tokens": total_completion_tokens,
                        }),
                        actor_id=self.actor_id,
                    )
                yield AgentEvent(type="error", content=error_msg, actor_id=self.actor_id)
                return

            tool_calls = response.get("tool_calls")
            if not tool_calls:
                content = response.get("content") or ""
                self.ctx.add_assistant_message(
                    content=content,
                    reasoning_content=response.get("reasoning_content"),
                )
                self.last_result_success = True
                if self.emit_token_stats:
                    yield AgentEvent(
                        type="token_stats",
                        content=json.dumps({
                            "prompt_tokens": total_prompt_tokens,
                            "completion_tokens": total_completion_tokens,
                        }),
                        actor_id=self.actor_id,
                    )
                yield AgentEvent(type="done", content=content, actor_id=self.actor_id)
                return

            self.ctx.add_assistant_message(
                content=response.get("content"),
                tool_calls=tool_calls,
                reasoning_content=response.get("reasoning_content"),
            )

            for tc in tool_calls:
                parsed = parse_tool_call(tc)
                yield AgentEvent(
                    type="tool_call",
                    tool_name=parsed.tool_name,
                    tool_args=parsed.args,
                    actor_id=self.actor_id,
                )
                tool_name, _, result, _ = await self._execute_single_tool(tc)
                yield AgentEvent(
                    type="tool_result",
                    tool_name=tool_name,
                    tool_result=result,
                    actor_id=self.actor_id,
                )
                for event in await self._run_after_tool_hook(tool_name, result):
                    yield event
