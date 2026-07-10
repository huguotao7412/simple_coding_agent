from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable

from .context import ContextManager
from .llm import LLMClient
from .events import AgentEvent
from .runtime import AgentRuntime
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
                setattr(tool, "_llm", self.llm)
                setattr(tool, "_workspace_dir", self.workspace_dir)
                setattr(tool, "_state", self.state)
                setattr(tool, "_run_context", self.run_context)
            elif tool.name == "update_state":
                setattr(tool, "_state", self.state)

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
        final_content = ""
        async for event in self.run_stream(user_input):
            if event.type == "thought" and on_token is not None:
                on_token(event.token)
            elif event.type in {"done", "error"}:
                final_content = event.content
        return final_content

    async def run_stream(
        self,
        user_input: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        async def produce() -> None:
            async for _ in self._runtime(emit_token_stats=True).run_stream(user_input):
                pass

        producer = asyncio.create_task(produce())
        try:
            while not producer.done() or not self.run_context.events.empty():
                try:
                    event = await asyncio.wait_for(
                        self.run_context.events.get(),
                        timeout=0.05,
                    )
                except asyncio.TimeoutError:
                    continue
                yield event
            await producer
        finally:
            if not producer.done():
                producer.cancel()
