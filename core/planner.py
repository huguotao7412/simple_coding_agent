from __future__ import annotations

import re
import json
import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Callable

from .llm import LLMClient
from .context import ContextManager
from .tools.base import BaseTool, ToolResult
from .agent import AgentEvent
from .exceptions import LLMAPIError
from .state import GlobalState


class Planner:
    """Orchestration agent — decomposes tasks, dispatches Actors, synthesizes results."""

    def __init__(
        self,
        llm_client: LLMClient,
        context_manager: ContextManager,
        tools: list[BaseTool],
        workspace_dir: str,
    ):
        self.llm = llm_client
        self.workspace_dir = workspace_dir
        self.ctx = context_manager
        self.state = GlobalState.get()
        self._recent_actions: deque[int] = deque(maxlen=10)

        for t in tools:
            if t.name == "delegate":
                t._llm = self.llm
                t._workspace_dir = self.workspace_dir

        self.tools_by_name = {t.name: t for t in tools}

    async def run(
        self,
        user_input: str,
        on_token: Callable[[str], None] | None = None,
    ) -> str:
        self.ctx.add_user_message(user_input)
        tool_schemas = [t.schema for t in self.tools_by_name.values()]

        step_count = 0
        MAX_STEPS = 50

        while True:
            step_count += 1
            if step_count > MAX_STEPS:
                error_msg = "Safety limit: Planner reached max steps. Please retry with a simpler request."
                self.ctx.add_assistant_message(content=error_msg)
                return error_msg

            if self.ctx.needs_compression(self.llm):
                await self.ctx.compress(self.llm)

            payload_messages = self.ctx.messages

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
                tool_name = tc["function"]["name"]
                try:
                    raw_args = tc["function"]["arguments"].strip()
                    raw_args = re.sub(r"^\s*```(?:json\s*)?", "", raw_args, flags=re.IGNORECASE)
                    raw_args = re.sub(r"\s*```$", "", raw_args).strip()
                    args = json.loads(raw_args) if raw_args else {}
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError as e:
                    error_hint = f"Error: Invalid JSON: {e}"
                    self.ctx.add_tool_result(tc["id"], error_hint)
                    continue

                action_hash = hash(tool_name + json.dumps(args, sort_keys=True))
                if self._recent_actions.count(action_hash) >= 2:
                    intervention = "System Alert: Repeated tool call detected. Please try a different approach."
                    self.ctx.add_tool_result(tc["id"], intervention)
                    continue
                self._recent_actions.append(action_hash)

                tool = self.tools_by_name.get(tool_name)
                if tool is None:
                    observation = f"Error: unknown tool '{tool_name}'"
                    result = ToolResult.fail(f"unknown tool '{tool_name}'")
                else:
                    try:
                        args["workspace_dir"] = self.workspace_dir
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

    async def run_stream(
        self,
        user_input: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        self.ctx.add_user_message(user_input)
        tool_schemas = [t.schema for t in self.tools_by_name.values()]

        step_count = 0
        MAX_STEPS = 30

        while True:
            step_count += 1
            if step_count > MAX_STEPS:
                error_msg = "Safety limit: Planner reached max steps. Please retry with a simpler request."
                self.ctx.add_assistant_message(content=error_msg)
                yield AgentEvent(type="error", content=error_msg)
                return

            if self.ctx.needs_compression(self.llm):
                await self.ctx.compress(self.llm)
                yield AgentEvent(type="compaction")

            queue = asyncio.Queue()

            def on_token(t: str) -> None:
                queue.put_nowait(t)

            payload_messages = self.ctx.messages

            chat_task = asyncio.create_task(
                self.llm.chat(
                    messages=payload_messages,
                    tools=tool_schemas if tool_schemas else None,
                    on_token=on_token,
                )
            )

            try:
                while not chat_task.done() or not queue.empty():
                    try:
                        token = await asyncio.wait_for(queue.get(), timeout=0.05)
                        yield AgentEvent(type="thought", token=token, content=token)
                    except asyncio.TimeoutError:
                        continue
            finally:
                if not chat_task.done():
                    chat_task.cancel()

            try:
                response = await chat_task
            except LLMAPIError as e:
                error_msg = str(e)
                self.ctx.add_assistant_message(content=error_msg)
                yield AgentEvent(type="error", content=error_msg)
                return

            tool_calls = response.get("tool_calls")

            if not tool_calls:
                content = response.get("content") or ""
                self.ctx.add_assistant_message(
                    content=content,
                    reasoning_content=response.get("reasoning_content"),
                )
                yield AgentEvent(type="done", content=content)
                return

            self.ctx.add_assistant_message(
                content=response.get("content"),
                tool_calls=tool_calls,
                reasoning_content=response.get("reasoning_content"),
            )

            for tc in tool_calls:
                tool_name = tc["function"]["name"]
                try:
                    raw_args = tc["function"]["arguments"].strip()
                    raw_args = re.sub(r"^\s*```(?:json\s*)?", "", raw_args, flags=re.IGNORECASE)
                    raw_args = re.sub(r"\s*```$", "", raw_args).strip()
                    tool_args = json.loads(raw_args) if raw_args else {}
                    if not isinstance(tool_args, dict):
                        tool_args = {}
                except json.JSONDecodeError as e:
                        error_hint = f"Error: Invalid JSON format in arguments: {e}"
                        self.ctx.add_tool_result(tc["id"], error_hint)

                        yield AgentEvent(type="tool_call", tool_name=tool_name, tool_args={})
                        yield AgentEvent(
                            type="tool_result",
                            tool_name=tool_name,
                            tool_result=ToolResult.fail(error_hint)
                        )
                        continue

                action_hash = hash(tool_name + json.dumps(tool_args, sort_keys=True))
                if self._recent_actions.count(action_hash) >= 2:
                    intervention = "System Alert: Repeated tool call detected. Please try a different approach."
                    self.ctx.add_tool_result(tc["id"], intervention)

                    yield AgentEvent(
                        type="tool_call",
                        tool_name=tool_name,
                        tool_args=tool_args,
                    )
                    yield AgentEvent(
                        type="tool_result",
                        tool_name=tool_name,
                        tool_result=ToolResult.fail(intervention),
                    )
                    continue

                self._recent_actions.append(action_hash)

                yield AgentEvent(
                    type="tool_call",
                    tool_name=tool_name,
                    tool_args=tool_args,
                )

                tool = self.tools_by_name.get(tool_name)
                if tool is None:
                    observation = f"Error: unknown tool '{tool_name}'"
                    result = ToolResult.fail(f"unknown tool '{tool_name}'")
                else:
                    try:
                        tool_args["workspace_dir"] = self.workspace_dir
                        result = await tool.execute(**tool_args)
                    except Exception as e:
                        result = ToolResult.fail(f"Internal Tool Error: {str(e)}")

                    if result.success:
                        observation = result.content
                    else:
                        observation = f"ERROR: {result.error}"
                        if result.content:
                            observation += f"\nPartial output: {result.content}"

                self.ctx.add_tool_result(tc["id"], observation)

                yield AgentEvent(
                    type="tool_result",
                    tool_name=tool_name,
                    tool_result=result,
                )

                # If delegate was called, check State for task updates
                if tool_name == "delegate":
                    snapshot = self.state.snapshot()
                    yield AgentEvent(
                        type="actor_update",
                        content=json.dumps(snapshot, ensure_ascii=False),
                    )
