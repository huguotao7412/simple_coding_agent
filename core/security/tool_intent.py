from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from .models import Capability


_SAFE_ARGUMENT_KEYS = frozenset({
    "attempt", "cwd", "mode", "strategy", "task_id", "timeout",
})
_PATH_ARGUMENT_KEYS = frozenset({
    "path", "paths", "file_path", "dir_path", "source", "destination",
    "workspace_dir",
})


@dataclass(frozen=True)
class ToolIntentSummarizer:
    """Build the only payload eligible for TOOL_INTENT external inspection."""

    def summarize(
        self,
        tool_name: str,
        capabilities: frozenset[Capability],
        arguments: dict[str, Any],
    ) -> str:
        return json.dumps(
            {
                "tool": tool_name,
                "capabilities": sorted(item.value for item in capabilities),
                "arguments": self._value(arguments),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def _value(self, value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {
                str(item_key): self._value(item, str(item_key))
                for item_key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return {
                "type": "array",
                "items": len(value),
                "sample": [self._value(item, key) for item in value[:5]],
            }
        if isinstance(value, str):
            if key in _PATH_ARGUMENT_KEYS:
                return {
                    "type": "path",
                    "basename": os.path.basename(value.rstrip("/\\")),
                    "absolute": os.path.isabs(value),
                }
            if key in {"command", "cmd", "script"}:
                executable = value.strip().split(maxsplit=1)[0] if value.strip() else ""
                return {
                    "type": "command",
                    "executable": os.path.basename(executable),
                    "characters": len(value),
                }
            if key in _SAFE_ARGUMENT_KEYS and len(value) <= 128:
                return value
            return {"type": "string", "characters": len(value)}
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return {"type": type(value).__name__}


__all__ = ["ToolIntentSummarizer"]
