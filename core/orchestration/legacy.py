from __future__ import annotations

from collections.abc import AsyncIterator

from ..events import AgentEvent
from ..planner import Planner
from .protocol import OrchestrationRequest


class LegacyOrchestrator:
    """Compatibility adapter around the pre-LangGraph Planner lifecycle."""

    def __init__(self, planner: Planner) -> None:
        self._planner = planner

    async def run_stream(
        self,
        request: OrchestrationRequest,
    ) -> AsyncIterator[AgentEvent]:
        if request.approval:
            await self._planner.run_context.grant_human_approval()
            self._planner.high_risk_approved = True
        async for event in self._planner.run_stream(
            request.user_request,
            resume=request.resume,
        ):
            yield event


__all__ = ["LegacyOrchestrator"]
