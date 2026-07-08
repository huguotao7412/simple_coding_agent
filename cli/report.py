from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from core.runtime import AgentEvent


FILE_ARG_KEYS = ("path", "file_path", "filepath", "source", "destination")


@dataclass
class ToolCallRecord:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    success: bool | None = None
    detail: str = ""


@dataclass
class RunReport:
    """Compact audit summary for one CLI agent run."""

    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    files_referenced: set[str] = field(default_factory=set)
    actor_status_counts: dict[str, int] = field(default_factory=dict)
    compactions: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    errors: list[str] = field(default_factory=list)
    final_output: str = ""

    def observe(self, event: AgentEvent) -> None:
        if event.type == "tool_call":
            args = dict(event.tool_args or {})
            self.tool_calls.append(ToolCallRecord(name=event.tool_name or "tool", args=args))
            self._record_file_args(args)

        elif event.type == "tool_result":
            if not self.tool_calls:
                self.tool_calls.append(ToolCallRecord(name=event.tool_name or "tool"))
            record = self.tool_calls[-1]
            record.name = event.tool_name or record.name
            if event.tool_result is None:
                record.success = False
                return
            record.success = event.tool_result.success
            record.detail = event.tool_result.content if event.tool_result.success else (event.tool_result.error or "")

        elif event.type == "actor_update":
            self._record_actor_update(event.content)

        elif event.type == "compaction":
            self.compactions += 1

        elif event.type == "token_stats":
            self._record_token_stats(event.content)

        elif event.type == "error":
            if event.content:
                self.errors.append(event.content)

        elif event.type == "done":
            self.final_output = event.content or ""

    @property
    def failed_tool_count(self) -> int:
        return sum(1 for call in self.tool_calls if call.success is False)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def _record_file_args(self, args: dict[str, Any]) -> None:
        for key in FILE_ARG_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value:
                self.files_referenced.add(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item:
                        self.files_referenced.add(item)

    def _record_actor_update(self, content: str) -> None:
        try:
            snapshot = json.loads(content)
        except json.JSONDecodeError:
            return
        counts: dict[str, int] = {}
        for task in snapshot.get("task_tree", {}).values():
            status = task.get("status", "unknown")
            counts[status] = counts.get(status, 0) + 1
        self.actor_status_counts = counts

    def _record_token_stats(self, content: str) -> None:
        try:
            stats = json.loads(content)
        except json.JSONDecodeError:
            return
        self.prompt_tokens = int(stats.get("prompt_tokens", 0) or 0)
        self.completion_tokens = int(stats.get("completion_tokens", 0) or 0)
