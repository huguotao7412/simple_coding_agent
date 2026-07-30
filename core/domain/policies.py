from __future__ import annotations

from dataclasses import dataclass

from ..execution.policy import ExecutionPolicy


@dataclass(frozen=True)
class ToolExposurePolicy:
    """Controls model-visible schemas; it never grants execution authority."""

    visible_tools: frozenset[str]

    def exposes(self, tool_name: str) -> bool:
        return tool_name in self.visible_tools


__all__ = ["ExecutionPolicy", "ToolExposurePolicy"]
