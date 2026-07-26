from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from ..events import AgentEvent


@dataclass(frozen=True)
class OrchestrationRequest:
    user_request: str
    resume: bool = False
    approval: bool | None = None


class Orchestrator(Protocol):
    def run_stream(
        self,
        request: OrchestrationRequest,
    ) -> AsyncIterator[AgentEvent]: ...


__all__ = ["OrchestrationRequest", "Orchestrator"]
