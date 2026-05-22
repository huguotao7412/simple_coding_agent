from __future__ import annotations

import json
from collections.abc import Callable

from .llm import LLMClient
from .context import ContextManager
from .tools.base import BaseTool, ToolResult


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
        self.ctx = context_manager
        self.tools_by_name = {t.name: t for t in tools}
        self.workspace_dir = workspace_dir

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
                self.ctx.add_assistant_message(content=response.get("content"))
                return response.get("content") or ""

            # Record assistant message with tool calls
            self.ctx.add_assistant_message(
                content=response.get("content"),
                tool_calls=tool_calls,
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
                if tool_name in ("read", "write", "edit", "bash"):
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
