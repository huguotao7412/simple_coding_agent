from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, cast


EXPLORATION_TOOLS = frozenset({
    "list_dir",
    "read",
    "read_outline",
    "search_codebase",
})
ACTOR_EXPLORATION_LIMIT = 8
PLANNER_EXPLORATION_LIMIT = 5
PLANNER_ORCHESTRATION_TOOLS = frozenset({"delegate", "update_state"})
MUTATION_TOOLS = frozenset({
    "apply_patch",
    "edit",
    "edit_file",
    "write",
    "write_file",
})
WORKSPACE_AWARE_TOOLS = frozenset({
    "apply_patch",
    "bash",
    "delegate",
    "edit",
    "list_dir",
    "read",
    "read_outline",
    "search_codebase",
    "update_state",
    "write",
})


@dataclass(frozen=True)
class ParsedToolCall:
    tool_name: str
    args: dict[str, Any]
    error: str | None = None


def parse_tool_call(tool_call: dict[str, Any]) -> ParsedToolCall:
    """Parse an OpenAI-style tool call into a name and object arguments."""
    function = tool_call.get("function", {})
    tool_name = function.get("name", "")
    raw_args = str(function.get("arguments") or "").strip()
    raw_args = re.sub(
        r"^\s*```(?:json\s*)?",
        "",
        raw_args,
        flags=re.IGNORECASE,
    )
    raw_args = re.sub(r"\s*```$", "", raw_args).strip()

    if not raw_args:
        return ParsedToolCall(tool_name=tool_name, args={})
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError as error:
        return ParsedToolCall(
            tool_name=tool_name,
            args={},
            error=(
                f"Invalid JSON format in arguments: {error}. "
                "Escape newlines as \\n, escape double quotes as \\\" "
                "and remove trailing commas."
            ),
        )
    if not isinstance(args, dict):
        args = {}
    return ParsedToolCall(
        tool_name=tool_name,
        args=cast(dict[str, Any], args),
    )


__all__ = [
    "ACTOR_EXPLORATION_LIMIT",
    "EXPLORATION_TOOLS",
    "MUTATION_TOOLS",
    "PLANNER_EXPLORATION_LIMIT",
    "PLANNER_ORCHESTRATION_TOOLS",
    "ParsedToolCall",
    "WORKSPACE_AWARE_TOOLS",
    "parse_tool_call",
]
