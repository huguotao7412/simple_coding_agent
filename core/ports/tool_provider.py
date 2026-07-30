from __future__ import annotations

from typing import Any, Protocol

from ..tools.base import ToolResult


class ToolProvider(Protocol):
    async def list_tools(self) -> list[dict[str, Any]]: ...

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolResult: ...


__all__ = ["ToolProvider"]
