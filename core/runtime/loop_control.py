from __future__ import annotations

import json
import re
from collections import deque
from typing import Any

from ..execution.models import ExecutionStrategy
from ..execution.policy import ExecutionPolicy
from ..tools.base import ToolResult
from .tool_calls import (
    ACTOR_EXPLORATION_LIMIT,
    EXPLORATION_TOOLS,
    MUTATION_TOOLS,
    PLANNER_EXPLORATION_LIMIT,
    PLANNER_ORCHESTRATION_TOOLS,
)


class RuntimeLoopControl:
    """Stateful anti-loop policy, separate from execution and persistence."""

    def __init__(self, actor_id: str) -> None:
        self.actor_id = actor_id
        self.available_tool_names: set[str] = set()
        self._recent_actions: deque[int] = deque(maxlen=10)
        self._outline_reads_by_file: dict[str, int] = {}
        self._exploration_calls_without_mutation = 0
        self._actor_exploration_locked = False
        self._planner_exploration_calls = 0
        self._planner_exploration_locked = False
        self._delegation_calls = 0
        self._successful_mutations = 0
        self._blocked_final_without_mutation = 0

    def set_available_tools(self, names: set[str]) -> None:
        self.available_tool_names = names

    def repeated_action(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        action_hash = hash(tool_name + json.dumps(arguments, sort_keys=True))
        repeated = self._recent_actions.count(action_hash) >= 2
        if not repeated:
            self._recent_actions.append(action_hash)
        return repeated

    def record_tool_result(self, tool_name: str, result: ToolResult) -> None:
        if result.success and tool_name == "delegate":
            self._delegation_calls += 1
            self._planner_exploration_calls = 0
            self._planner_exploration_locked = False
            self._exploration_calls_without_mutation = 0
        if not result.success or tool_name not in MUTATION_TOOLS:
            return
        if tool_name == "apply_patch" and "No changes to apply" in result.content:
            return
        self._successful_mutations += 1
        self._exploration_calls_without_mutation = 0
        self._actor_exploration_locked = False

    def actor_intervention(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        policy: ExecutionPolicy | None,
    ) -> str:
        if (
            not self.actor_id
            or self._successful_mutations
            or not self._is_code_change_runtime(policy)
            or not self._has_mutation_capability()
        ):
            return ""
        if self._actor_exploration_locked and tool_name not in MUTATION_TOOLS:
            return _actor_mutation_required_message()

        is_exploration = tool_name in EXPLORATION_TOOLS
        if tool_name in {"bash", "run"}:
            is_exploration = _is_source_reading_command(
                arguments.get("command")
            )
        if not is_exploration:
            return ""
        if not self._actor_exploration_locked:
            self._exploration_calls_without_mutation += 1
            if (
                self._exploration_calls_without_mutation
                <= ACTOR_EXPLORATION_LIMIT
            ):
                if (
                    self._exploration_calls_without_mutation
                    == ACTOR_EXPLORATION_LIMIT
                ):
                    self._actor_exploration_locked = True
                return ""
        return _actor_mutation_required_message()

    def planner_intervention(
        self,
        tool_name: str,
        policy: ExecutionPolicy | None,
    ) -> str:
        if (
            not self.actor_id
            and self._planner_exploration_locked
            and not self._delegation_calls
            and tool_name not in PLANNER_ORCHESTRATION_TOOLS
        ):
            return _planner_delegation_required_message()
        if (
            self.actor_id
            or self._delegation_calls
            or tool_name not in EXPLORATION_TOOLS
            or not self._is_code_change_runtime(policy)
            or "delegate" not in self.available_tool_names
        ):
            return ""
        self._planner_exploration_calls += 1
        if self._planner_exploration_calls <= PLANNER_EXPLORATION_LIMIT:
            if self._planner_exploration_calls == PLANNER_EXPLORATION_LIMIT:
                self._planner_exploration_locked = True
            return ""
        return _planner_delegation_required_message()

    def outline_intervention(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        raw_path = arguments.get("file_path") or arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return ""
        path_key = raw_path.replace("\\", "/").lower()
        source_tools = {
            "read",
            "read_file",
            "read_text_file",
            "edit",
            "edit_file",
            "write",
            "write_file",
        }
        if tool_name in source_tools:
            self._outline_reads_by_file.pop(path_key, None)
            return ""
        if tool_name != "read_outline":
            return ""
        previous_reads = self._outline_reads_by_file.get(path_key, 0)
        if previous_reads >= 2:
            offset = arguments.get("offset") or 1
            limit = arguments.get("limit") or 200
            return (
                "System Alert: read_outline only returns symbol signatures and "
                "this file has already been outlined twice. Call read with "
                f"file_path='{raw_path}', offset={offset}, limit={limit} to "
                "inspect the actual source. Do not call read_outline again for "
                "implementation details."
            )
        self._outline_reads_by_file[path_key] = previous_reads + 1
        return ""

    def schemas_for_step(
        self,
        schemas: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        allowed_names: frozenset[str] | None = None
        if (
            self.actor_id
            and self._actor_exploration_locked
            and not self._successful_mutations
        ):
            allowed_names = MUTATION_TOOLS
        elif (
            not self.actor_id
            and self._planner_exploration_locked
            and not self._delegation_calls
        ):
            allowed_names = PLANNER_ORCHESTRATION_TOOLS
        if allowed_names is None:
            return schemas
        return [
            schema
            for schema in schemas
            if str(
                schema.get("function", {}).get("name")
                or schema.get("name")
                or ""
            )
            in allowed_names
        ]

    def should_continue_for_missing_mutation(
        self,
        policy: ExecutionPolicy | None,
    ) -> bool:
        if (
            self._successful_mutations
            or not self._is_code_change_runtime(policy)
            or not self._has_mutation_capability()
            or self._blocked_final_without_mutation >= 2
        ):
            return False
        self._blocked_final_without_mutation += 1
        return True

    def _has_mutation_capability(self) -> bool:
        return bool(self.available_tool_names & MUTATION_TOOLS)

    def _is_code_change_runtime(
        self,
        policy: ExecutionPolicy | None,
    ) -> bool:
        if policy is None:
            return bool(self.actor_id) and self._has_mutation_capability()
        return policy.strategy is not ExecutionStrategy.PLANNER_DIRECT


def _is_source_reading_command(command: Any) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    source_readers = re.compile(
        r"(?:^|[;&|]\s*)(?:"
        r"cat|head|tail|less|more|sed\s+-n|grep|rg|find|ls|dir|tree|"
        r"get-content|select-string|get-childitem"
        r")\b",
        flags=re.IGNORECASE,
    )
    return bool(source_readers.search(command.strip()))


def _actor_mutation_required_message() -> str:
    return (
        "System Alert: The source-exploration allowance for this code-change "
        "task is exhausted. Non-mutation tools are temporarily unavailable. "
        "Use the context already collected to make the smallest complete change "
        "with edit_file/write_file/apply_patch. After a successful edit, source "
        "inspection, tests, and diagnostics are available again."
    )


def _planner_delegation_required_message() -> str:
    return (
        "System Alert: Planner has enough repository context for this code-change "
        "task. Only orchestration tools are temporarily available. Register one "
        "focused Coder task with update_state, then delegate it with the essential "
        "target files and concise findings."
    )


__all__ = ["RuntimeLoopControl"]
