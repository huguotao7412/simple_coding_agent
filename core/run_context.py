from __future__ import annotations

import asyncio
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any

from .events import AgentEvent, QueueEventSink
from .run_state import RunCheckpoint, RunRecord
from .run_store import RunStore
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
    completed_tool_calls: dict[str, str] = field(default_factory=dict)
    record: RunRecord | None = None
    store: RunStore | None = None
    _usage_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @classmethod
    def create(
        cls,
        run_id: str | None = None,
        *,
        record: RunRecord | None = None,
        store: RunStore | None = None,
    ) -> RunContext:
        resolved_run_id = run_id or (record.run_id if record is not None else None)
        resolved_run_id = resolved_run_id or f"run_{uuid.uuid4().hex[:12]}"
        if record is not None and record.run_id != resolved_run_id:
            raise ValueError("record run_id does not match RunContext run_id")
        return cls(
            run_id=resolved_run_id,
            state=GlobalState(),
            events=QueueEventSink(),
            record=record,
            store=store,
        )

    @classmethod
    def from_checkpoint(
        cls,
        record: RunRecord,
        checkpoint: RunCheckpoint,
        *,
        store: RunStore | None = None,
    ) -> RunContext:
        if record.run_id != checkpoint.run_id:
            raise ValueError("checkpoint run_id does not match record run_id")
        return cls(
            run_id=record.run_id,
            state=GlobalState.from_snapshot(checkpoint.task_snapshot),
            events=QueueEventSink(),
            usage=UsageTotals(
                prompt_tokens=checkpoint.prompt_tokens,
                completion_tokens=checkpoint.completion_tokens,
                estimated=checkpoint.usage_estimated,
            ),
            completed_tool_calls=dict(checkpoint.completed_tool_calls or {}),
            record=record,
            store=store,
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

    async def checkpoint(
        self,
        messages: list[dict[str, Any]],
        *,
        saved_at: float | None = None,
    ) -> RunCheckpoint:
        usage = await self.usage_snapshot()
        task_snapshot = await self.state.snapshot(truncate_diffs=False)
        return RunCheckpoint(
            run_id=self.run_id,
            messages=tuple(deepcopy(messages)),
            task_snapshot=task_snapshot,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            usage_estimated=usage.estimated,
            completed_tool_calls=dict(self.completed_tool_calls),
            saved_at=time.time() if saved_at is None else saved_at,
        )


__all__ = ["RunContext", "UsageTotals"]
