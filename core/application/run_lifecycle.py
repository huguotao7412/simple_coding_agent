from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..runs.context import RunContext
from ..runs.models import RunStatus


@dataclass
class RunLifecycleService:
    """Application owner for durable lifecycle transitions and checkpoints."""

    context: RunContext
    messages: list[dict[str, Any]]
    enabled: bool = True

    async def persist(
        self,
        event_type: str,
        *,
        terminal_status: str | None = None,
        error: str = "",
    ) -> None:
        status = (
            RunStatus(terminal_status)
            if self.enabled and terminal_status is not None
            else None
        )
        await self.context.persist_checkpoint(
            self.messages,
            event_type=event_type,
            status=status,
            error=error,
        )


__all__ = ["RunLifecycleService"]
