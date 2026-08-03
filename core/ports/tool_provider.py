from __future__ import annotations

from typing import Any, Protocol

from ..domain.tools import ProviderHealth
from ..policy import ToolPolicy
from ..tools.base import ToolResult


class ToolProvider(Protocol):
    async def start(
        self,
        worktree_path: str,
        tool_allowlist: set[str] | None = None,
        tool_policy: ToolPolicy | None = None,
    ) -> None: ...

    def health(self) -> ProviderHealth: ...

    async def list_tools(self) -> list[dict[str, Any]]: ...

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolResult: ...

    async def shutdown(self) -> None: ...


__all__ = ["ToolProvider"]
