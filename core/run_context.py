from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field, replace

from .events import AgentEvent, QueueEventSink
from .state import GlobalState


@dataclass
class UsageTotals:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class RunContext:
    run_id: str
    state: GlobalState
    events: QueueEventSink
    usage: UsageTotals = field(default_factory=UsageTotals)
    _usage_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @classmethod
    def create(cls, run_id: str | None = None) -> RunContext:
        return cls(
            run_id=run_id or f"run_{uuid.uuid4().hex[:12]}",
            state=GlobalState(),
            events=QueueEventSink(),
        )

    async def emit(self, event: AgentEvent) -> None:
        if not event.run_id:
            event.run_id = self.run_id
        await self.events.emit(event)

    async def record_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        estimated: bool,
    ) -> UsageTotals:
        async with self._usage_lock:
            self.usage.prompt_tokens += prompt_tokens
            self.usage.completion_tokens += completion_tokens
            self.usage.estimated = self.usage.estimated or estimated
            return replace(self.usage)

    async def usage_snapshot(self) -> UsageTotals:
        async with self._usage_lock:
            return replace(self.usage)


__all__ = ["RunContext", "UsageTotals"]
