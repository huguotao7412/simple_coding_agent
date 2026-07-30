from __future__ import annotations

from dataclasses import dataclass

from ..tools.gateway import ToolGateway


@dataclass(frozen=True)
class ToolCycle:
    """Explicit ModelLoop dependency for all final tool execution."""

    gateway: ToolGateway


__all__ = ["ToolCycle"]
