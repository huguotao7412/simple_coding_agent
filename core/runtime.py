from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .context import ContextManager
from .exceptions import LLMAPIError
from .llm import LLMClient
from .tools.base import BaseTool


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
        dynamic_context_builder: Any | None = None,
    ) -> None:
        self.llm = llm_client
        self.ctx = context_manager
        self.workspace_dir = workspace_dir
        self.max_steps = max_steps
        self.tool_provider = tool_provider
        self.actor_id = actor_id
        self.dynamic_context_builder = dynamic_context_builder
        self.tools_by_name = {t.name: t for t in tools} if tools else {}

    async def _list_tool_schemas(self) -> list[dict]:
        if self.tool_provider is not None:
            return await self.tool_provider.list_tools()
        return [t.schema for t in self.tools_by_name.values()]

    def _payload_messages(self) -> list[dict]:
        if self.dynamic_context_builder is None:
            return self.ctx.messages
        return self.ctx.messages + [self.dynamic_context_builder()]

    async def run(self, user_input: str, on_token=None) -> str:
        self.ctx.add_user_message(user_input)
        tool_schemas = await self._list_tool_schemas()

        step_count = 0
        while True:
            step_count += 1
            if step_count > self.max_steps:
                error_msg = "Safety stop: agent reached the maximum step limit."
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
                self.ctx.add_assistant_message(content=error_msg)
                return error_msg

            tool_calls = response.get("tool_calls")
            if not tool_calls:
                content = response.get("content") or ""
                self.ctx.add_assistant_message(
                    content=content,
                    reasoning_content=response.get("reasoning_content"),
                )
                return content

            self.ctx.add_assistant_message(
                content=response.get("content"),
                tool_calls=tool_calls,
                reasoning_content=response.get("reasoning_content"),
            )

            for tc in tool_calls:
                parsed = parse_tool_call(tc)
                if parsed.error is not None:
                    self.ctx.add_tool_result(tc["id"], f"Error: {parsed.error}")
                else:
                    self.ctx.add_tool_result(tc["id"], "Tool execution is not implemented yet.")
