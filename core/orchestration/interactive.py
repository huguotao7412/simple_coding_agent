from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..events import AgentEvent
from ..planner import Planner
from ..runs.context import RunContext
from ..runtime.conversation import ContextManager
from .factory import create_orchestrator
from .protocol import OrchestrationRequest, Orchestrator


ContextFactory = Callable[[list[dict[str, Any]]], Awaitable[RunContext]]
PlannerFactory = Callable[[ContextManager, RunContext], Planner]


@dataclass
class InteractiveRun:
    user_request: str
    planner: Planner
    orchestrator: Orchestrator
    preapproved: bool = False
    final_output: str = ""
    interrupted: bool = False

    def start_stream(self) -> AsyncIterator[AgentEvent]:
        return self.orchestrator.run_stream(OrchestrationRequest(
            user_request=self.user_request,
            approval=True if self.preapproved else None,
        ))

    def resume_stream(self, approved: bool) -> AsyncIterator[AgentEvent]:
        return self.orchestrator.run_stream(OrchestrationRequest(
            user_request="",
            resume=True,
            approval=approved,
        ))


class InteractiveOrchestrationSession:
    """Create one durable graph thread per interactive user task."""

    def __init__(
        self,
        *,
        system_prompt: str,
        context_factory: ContextFactory,
        planner_factory: PlannerFactory,
        orchestrator_name: str | None = None,
        preapprove_high_risk: bool = False,
        max_history_messages: int = 20,
    ) -> None:
        self._system_prompt = system_prompt
        self._context_factory = context_factory
        self._planner_factory = planner_factory
        self._orchestrator_name = orchestrator_name
        self._preapprove_high_risk = preapprove_high_risk
        self._max_history_messages = max_history_messages
        self._history: list[dict[str, str]] = []

    @property
    def history(self) -> tuple[dict[str, str], ...]:
        return tuple(dict(message) for message in self._history)

    async def start(self, user_request: str) -> InteractiveRun:
        context = ContextManager(system_prompt=self._system_prompt)
        context.messages.extend(dict(message) for message in self._history)
        run_context = await self._context_factory(context.messages)
        planner = self._planner_factory(context, run_context)
        return InteractiveRun(
            user_request=user_request,
            planner=planner,
            orchestrator=create_orchestrator(
                planner,
                name=self._orchestrator_name,
            ),
            preapproved=self._preapprove_high_risk,
        )

    def complete(self, run: InteractiveRun, final_output: str) -> None:
        if not final_output:
            return
        run.final_output = final_output
        self._history.extend((
            {"role": "user", "content": run.user_request},
            {"role": "assistant", "content": final_output},
        ))
        if len(self._history) > self._max_history_messages:
            self._history = self._history[-self._max_history_messages :]

    def reset_history(self) -> None:
        self._history.clear()


__all__ = [
    "ContextFactory",
    "InteractiveOrchestrationSession",
    "InteractiveRun",
    "PlannerFactory",
]
