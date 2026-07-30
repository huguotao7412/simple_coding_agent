"""Compatibility facade for the pre-refactor runtime import path."""

from __future__ import annotations

from .model_loop import ModelLoop
from .tool_calls import parse_tool_call


class AgentRuntime(ModelLoop):
    """Deprecated name retained for Planner/Actor/API compatibility."""


__all__ = ["AgentRuntime", "ModelLoop", "parse_tool_call"]
