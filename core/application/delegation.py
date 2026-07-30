from __future__ import annotations

from typing import Any, Protocol

from ..actors.contracts import ActorExecutionResult, ActorTaskSpec
from ..runs.context import RunContext


class ActorExecutionPort(Protocol):
    async def execute(
        self,
        spec: ActorTaskSpec,
        run_context: RunContext,
    ) -> ActorExecutionResult: ...


class DelegationService:
    """Application owner for Actor scheduling and result recording."""

    def __init__(self, executor: ActorExecutionPort) -> None:
        self.executor = executor

    async def execute(
        self,
        spec: ActorTaskSpec,
        run_context: RunContext,
    ) -> ActorExecutionResult:
        return await self.executor.execute(spec, run_context)


__all__ = ["ActorExecutionPort", "DelegationService"]
