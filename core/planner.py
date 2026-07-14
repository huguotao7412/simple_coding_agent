from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable

from .runtime.conversation import ContextManager
from .execution.assessment import TaskAssessor
from .execution.models import TaskAssessment
from .llm import LLMClient
from .events import AgentEvent
from .runtime.engine import AgentRuntime
from .runs.context import RunContext
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
        task_assessor: TaskAssessor | None = None,
    ):
        self.llm = llm_client
        self.workspace_dir = workspace_dir
        self.max_steps = max_steps
        self.ctx = context_manager
        self.run_context = run_context or RunContext.create()
        self.state = self.run_context.state
        self.task_assessor = task_assessor or TaskAssessor(workspace_dir)
        self.current_task_assessment: TaskAssessment | None = None

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
            assessment = self.task_assessor.assess(user_input)
            self.current_task_assessment = assessment
            self.ctx.add_system_message(assessment.to_system_message())
            await self.run_context.emit(AgentEvent(
                type="task_assessment",
                content=assessment.to_json(),
            ))
        else:
            restored_assessment = self._restored_assessment_json()
            if restored_assessment:
                await self.run_context.emit(AgentEvent(
                    type="task_assessment",
                    content=restored_assessment,
                ))

        async def produce() -> None:
            async for _ in self._runtime(emit_token_stats=True).run_stream(
                user_input,
                resume=resume,
            ):
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
                try:
                    await producer
                except asyncio.CancelledError:
                    pass

    def _restored_assessment_json(self) -> str:
        """Recover the latest durable assessment for resume-time observability."""
        prefix = "<task_assessment>\n"
        suffix = "\n</task_assessment>"
        for message in reversed(self.ctx.messages):
            content = message.get("content")
            if message.get("role") != "system" or not isinstance(content, str):
                continue
            if not content.startswith(prefix):
                continue
            end = content.find(suffix, len(prefix))
            if end == -1:
                continue
            raw = content[len(prefix):end]
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("schema_version") == 1:
                return json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return ""
