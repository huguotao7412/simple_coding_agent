from __future__ import annotations

import json
import re
import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from .context import ContextManager
from .events import AgentEvent
from .exceptions import LLMAPIError
from .llm import LLMClient
from .run_context import RunContext
from .run_state import RunStatus
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
    args: dict[str, Any]
    error: str | None = None


def parse_tool_call(tc: dict[str, Any]) -> ParsedToolCall:
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

    return ParsedToolCall(tool_name=tool_name, args=cast(dict[str, Any], args))


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
        dynamic_context_builder: Callable[[], dict[str, Any]] | None = None,
        after_tool_call: Callable[[str, ToolResult], Awaitable[list[AgentEvent]]] | None = None,
        emit_token_stats: bool = False,
        run_context: RunContext | None = None,
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
        self.run_context = run_context or RunContext.create()
        self.tools_by_name = {t.name: t for t in tools} if tools else {}
        self._recent_actions: deque[int] = deque(maxlen=10)
        self.last_result_success = True

    async def _list_tool_schemas(self) -> list[dict[str, Any]]:
        if self.tool_provider is not None:
            return cast(list[dict[str, Any]], await self.tool_provider.list_tools())
        return [t.schema for t in self.tools_by_name.values()]

    def _payload_messages(self) -> list[dict[str, Any]]:
        if self.dynamic_context_builder is None:
            return self.ctx.messages
        return self.ctx.messages + [self.dynamic_context_builder()]

    async def _execute_single_tool(
        self,
        tc: dict[str, Any],
    ) -> tuple[str, dict[str, Any], ToolResult, bool]:
        parsed = parse_tool_call(tc)
        tool_name = parsed.tool_name
        args = parsed.args
        tool_call_id = str(tc.get("id", ""))

        cached = (
            self.run_context.completed_tool_calls.get(tool_call_id)
            if self.run_context.store is not None and not self.actor_id
            else None
        )
        if cached is not None:
            result, observation = self._decode_cached_tool_result(cached)
            self.ctx.add_tool_result(tool_call_id, observation)
            return tool_name, args, result, False

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

    @staticmethod
    def _decode_cached_tool_result(cached: str) -> tuple[ToolResult, str]:
        try:
            payload = json.loads(cached)
        except json.JSONDecodeError:
            return ToolResult.ok(cached), cached
        if not isinstance(payload, dict):
            return ToolResult.ok(cached), cached
        observation = str(payload.get("observation", ""))
        success = bool(payload.get("success", True))
        if success:
            return ToolResult.ok(str(payload.get("content", observation))), observation
        return ToolResult.fail(
            str(payload.get("error", "cached tool call failed")),
            content=str(payload.get("content", "")),
        ), observation

    def _remember_tool_result(
        self,
        tool_call_id: str,
        result: ToolResult,
    ) -> None:
        if self.run_context.store is None or self.actor_id:
            return
        last_message = self.ctx.messages[-1] if self.ctx.messages else {}
        observation = str(last_message.get("content", ""))
        self.run_context.completed_tool_calls[tool_call_id] = json.dumps(
            {
                "success": result.success,
                "content": result.content,
                "error": result.error,
                "observation": observation,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    async def _persist_root(
        self,
        event_type: str,
        *,
        status: RunStatus | None = None,
        error: str = "",
    ) -> None:
        if self.actor_id:
            return
        await self.run_context.persist_checkpoint(
            self.ctx.messages,
            event_type=event_type,
            status=status,
            error=error,
        )

    def _pending_tool_calls(self) -> list[dict[str, Any]]:
        """Return tool calls missing observations at the last assistant boundary."""
        assistant_index: int | None = None
        tool_calls: list[dict[str, Any]] = []
        for index in range(len(self.ctx.messages) - 1, -1, -1):
            message = self.ctx.messages[index]
            if message.get("role") != "assistant":
                continue
            raw_calls = message.get("tool_calls")
            if isinstance(raw_calls, list):
                assistant_index = index
                tool_calls = [
                    cast(dict[str, Any], call)
                    for call in raw_calls
                    if isinstance(call, dict)
                ]
            break
        if assistant_index is None:
            return []
        observed_ids = {
            str(message.get("tool_call_id", ""))
            for message in self.ctx.messages[assistant_index + 1:]
            if message.get("role") == "tool"
        }
        return [
            call for call in tool_calls
            if str(call.get("id", "")) not in observed_ids
        ]

    async def _process_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
    ) -> AsyncGenerator[AgentEvent, None]:
        for tc in tool_calls:
            parsed = parse_tool_call(tc)
            yield await self._emit(AgentEvent(
                type="tool_call",
                tool_name=parsed.tool_name,
                tool_args=parsed.args,
                actor_id=self.actor_id,
            ))
            tool_name, _, result, _ = await self._execute_single_tool(tc)
            self._remember_tool_result(str(tc.get("id", "")), result)
            await self._persist_root("tool_result")
            yield await self._emit(AgentEvent(
                type="tool_result",
                tool_name=tool_name,
                tool_result=result,
                actor_id=self.actor_id,
            ))
            for event in await self._run_after_tool_hook(tool_name, result):
                yield await self._emit(event)

    async def _run_after_tool_hook(self, tool_name: str, result: ToolResult) -> list[AgentEvent]:
        if self.after_tool_call is None:
            return []
        return await self.after_tool_call(tool_name, result)

    async def _emit(self, event: AgentEvent) -> AgentEvent:
        event.actor_id = event.actor_id or self.actor_id
        event.task_id = event.task_id or self.actor_id
        await self.run_context.emit(event)
        return event

    async def _token_stats_event(self) -> AgentEvent:
        usage = await self.run_context.usage_snapshot()
        return await self._emit(AgentEvent(
            type="token_stats",
            content=json.dumps({
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "estimated": usage.estimated,
            }),
            actor_id=self.actor_id,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            usage_estimated=usage.estimated,
        ))

    async def run(
        self,
        user_input: str,
        on_token: Callable[[str], None] | None = None,
        *,
        resume: bool = False,
    ) -> str:
        final_content = ""
        async for event in self.run_stream(user_input, resume=resume):
            if event.type == "thought" and on_token is not None:
                on_token(event.token)
            elif event.type in {"done", "error"}:
                final_content = event.content
        return final_content

    async def run_stream(
        self,
        user_input: str,
        *,
        resume: bool = False,
    ) -> AsyncGenerator[AgentEvent, None]:
        if not resume:
            self.ctx.add_user_message(user_input)
        await self._persist_root("running", status=RunStatus.RUNNING)
        try:
            if resume:
                async for event in self._process_tool_calls(
                    self._pending_tool_calls()
                ):
                    yield event
            async for event in self._run_stream_loop():
                yield event
        except asyncio.CancelledError:
            await asyncio.shield(
                self._persist_root("paused", status=RunStatus.PAUSED)
            )
            raise
        except Exception as error:
            await asyncio.shield(
                self._persist_root(
                    "failed",
                    status=RunStatus.FAILED,
                    error=str(error),
                )
            )
            raise

    async def _run_stream_loop(self) -> AsyncGenerator[AgentEvent, None]:
        tool_schemas = await self._list_tool_schemas()

        step_count = 0
        while True:
            step_count += 1
            if step_count > self.max_steps:
                error_msg = "Safety stop: agent reached the maximum step limit."
                self.last_result_success = False
                self.ctx.add_assistant_message(content=error_msg)
                await self._persist_root(
                    "failed",
                    status=RunStatus.FAILED,
                    error=error_msg,
                )
                if self.emit_token_stats:
                    yield await self._token_stats_event()
                yield await self._emit(AgentEvent(type="error", content=error_msg, actor_id=self.actor_id))
                return

            if self.ctx.needs_compression(self.llm):
                await self.ctx.compress(self.llm)
                await self._persist_root("compaction")
                yield await self._emit(AgentEvent(type="compaction", actor_id=self.actor_id))
            elif self.ctx.needs_proactive_compression(self.llm):
                self.ctx._lightweight_compress()
                await self._persist_root("compaction")
                yield await self._emit(AgentEvent(type="compaction", content="lightweight", actor_id=self.actor_id))

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
                    yield await self._emit(AgentEvent(
                        type="thought",
                        token=token,
                        content=token,
                        actor_id=self.actor_id,
                    ))
            finally:
                if not chat_task.done():
                    chat_task.cancel()

            try:
                response = await chat_task
                usage = response.get("_usage", {})
                prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
                completion_tokens = int(usage.get("completion_tokens", 0) or 0)
                usage_estimated = bool(usage.get("estimated", True))
                await self.run_context.record_usage(
                    prompt_tokens,
                    completion_tokens,
                    usage_estimated,
                )
                yield await self._emit(AgentEvent(
                    type="model_usage",
                    content=json.dumps({
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "estimated": usage_estimated,
                    }),
                    actor_id=self.actor_id,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    usage_estimated=usage_estimated,
                ))
            except LLMAPIError as e:
                error_msg = str(e)
                self.last_result_success = False
                self.ctx.add_assistant_message(content=error_msg)
                await self._persist_root(
                    "failed",
                    status=RunStatus.FAILED,
                    error=error_msg,
                )
                if self.emit_token_stats:
                    yield await self._token_stats_event()
                yield await self._emit(AgentEvent(type="error", content=error_msg, actor_id=self.actor_id))
                return

            tool_calls = response.get("tool_calls")
            if not tool_calls:
                content = response.get("content") or ""
                self.ctx.add_assistant_message(
                    content=content,
                    reasoning_content=response.get("reasoning_content"),
                )
                self.last_result_success = True
                await self._persist_root(
                    "completed",
                    status=RunStatus.COMPLETED,
                )
                if self.emit_token_stats:
                    yield await self._token_stats_event()
                yield await self._emit(AgentEvent(type="done", content=content, actor_id=self.actor_id))
                return

            self.ctx.add_assistant_message(
                content=response.get("content"),
                tool_calls=tool_calls,
                reasoning_content=response.get("reasoning_content"),
            )
            await self._persist_root("assistant_tool_calls")

            async for event in self._process_tool_calls(tool_calls):
                yield event
