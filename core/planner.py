from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable

from .context import ContextManager
from .llm import LLMClient
from .runtime import AgentEvent, AgentRuntime
from .run_context import RunContext
from .tools.base import BaseTool, ToolResult


class Planner:
    """Planner wrapper around the shared transparent ReAct runtime."""

    def __init__(
        self,
        llm_client: LLMClient,
        context_manager: ContextManager,
        tools: list[BaseTool],
        workspace_dir: str,
        max_steps: int = 50,
        run_context: RunContext | None = None,
    ):
        self.llm = llm_client
        self.workspace_dir = workspace_dir
        self.max_steps = max_steps
        self.ctx = context_manager
        self.run_context = run_context or RunContext.create()
        self.state = self.run_context.state

        for tool in tools:
            if tool.name == "delegate":
                tool._llm = self.llm
                tool._workspace_dir = self.workspace_dir
                tool._state = self.state
                tool._run_context = self.run_context
            elif tool.name == "update_state":
                tool._state = self.state

        self.tools_by_name = {tool.name: tool for tool in tools}

    async def _after_tool_call(self, tool_name: str, result: ToolResult) -> list[AgentEvent]:
        if tool_name != "delegate":
            return []
        snapshot = await self.state.snapshot()
        return [
            AgentEvent(
                type="actor_update",
                content=json.dumps(snapshot, ensure_ascii=False),
            )
        ]

    def _runtime(self, *, emit_token_stats: bool = False) -> AgentRuntime:
        return AgentRuntime(
            llm_client=self.llm,
            context_manager=self.ctx,
            tools=list(self.tools_by_name.values()),
            workspace_dir=self.workspace_dir,
            max_steps=self.max_steps,
            after_tool_call=self._after_tool_call,
            emit_token_stats=emit_token_stats,
            run_context=self.run_context,
        )

    async def run(
        self,
        user_input: str,
        on_token: Callable[[str], None] | None = None,
    ) -> str:
        return await self._runtime().run(user_input, on_token=on_token)

    async def run_stream(
        self,
        user_input: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        async for event in self._runtime(emit_token_stats=True).run_stream(user_input):
            yield event
