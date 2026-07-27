from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .tools.base import ToolResult


@dataclass
class AgentEvent:
    type: str
    content: str = ""
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: ToolResult | None = None
    token: str = ""
    actor_id: str = ""
    run_id: str = ""
    task_id: str = ""
    parent_id: str = ""
    node_name: str = ""
    route: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usage_estimated: bool = True


class EventSink(Protocol):
    async def emit(self, event: AgentEvent) -> None: ...


class QueueEventSink:
    """Unbounded in-process event transport shared by one agent run."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[AgentEvent] = asyncio.Queue()

    async def emit(self, event: AgentEvent) -> None:
        await self._queue.put(event)

    async def get(self) -> AgentEvent:
        return await self._queue.get()

    def empty(self) -> bool:
        return self._queue.empty()


__all__ = ["AgentEvent", "EventSink", "QueueEventSink"]
