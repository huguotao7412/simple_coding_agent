from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .base import BaseTool, ToolResult


ToolDispatcher = Callable[[dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolRegistration:
    name: str
    schema: dict[str, Any]
    dispatch: ToolDispatcher
    workspace_aware: bool = False
    adapter_kind: str = "local"


class ToolCatalog:
    """Application catalog; schema exposure and execution remain distinct."""

    def __init__(self) -> None:
        self._registrations: dict[str, ToolRegistration] = {}

    def register(self, registration: ToolRegistration) -> None:
        name = registration.name.strip()
        if not name:
            raise ValueError("tool registration name must not be empty")
        self._registrations[name] = registration

    def register_local(self, tool: BaseTool, *, workspace_aware: bool) -> None:
        async def dispatch(arguments: dict[str, Any]) -> ToolResult:
            return await tool.execute(**arguments)

        self.register(ToolRegistration(
            name=tool.name,
            schema=tool.schema,
            dispatch=dispatch,
            workspace_aware=workspace_aware,
            adapter_kind="local",
        ))

    def resolve(self, name: str) -> ToolRegistration | None:
        return self._registrations.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [registration.schema for registration in self._registrations.values()]


__all__ = ["ToolCatalog", "ToolDispatcher", "ToolRegistration"]
