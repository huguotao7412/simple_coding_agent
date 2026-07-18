from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .base import BaseTool, ToolResult


def _path_arg(kwargs: dict[str, Any]) -> str:
    raw = kwargs.get("file_path", kwargs.get("path", ""))
    return raw if isinstance(raw, str) else ""


class WriteFileTool(BaseTool):
    name = "write_file"
    description = (
        "Create or replace a text file inside the Actor workspace. The path may "
        "be workspace-relative or absolute, but it must stay inside the workspace."
    )
    parameters = {
        "file_path": {
            "type": "string",
            "description": "Workspace-relative path to create or replace.",
        },
        "path": {
            "type": "string",
            "description": "Alias for file_path.",
        },
        "content": {
            "type": "string",
            "description": "Complete UTF-8 text content to write.",
        },
    }
    required_params: list[str] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        workspace_dir = str(kwargs.get("workspace_dir", ""))
        if not workspace_dir:
            return ToolResult.fail("workspace_dir is required")
        file_path = _path_arg(kwargs)
        if not file_path:
            return ToolResult.fail("'file_path' is required")
        content = kwargs.get("content")
        if not isinstance(content, str):
            return ToolResult.fail("'content' must be a string")
        try:
            resolved = self.validate_path(file_path, workspace_dir)
            Path(resolved).parent.mkdir(parents=True, exist_ok=True)
            with open(resolved, "w", encoding="utf-8", newline="\n") as target:
                target.write(content)
        except Exception as error:
            return ToolResult.fail(str(error))
        rel_path = os.path.relpath(resolved, workspace_dir)
        return ToolResult.ok(f"Wrote {rel_path} ({len(content)} characters).")


class EditFileTool(BaseTool):
    name = "edit_file"
    description = (
        "Edit a text file inside the Actor workspace by replacing exact text. "
        "Use old_text/new_text for one replacement, or edits=[{old_text,new_text}] "
        "for multiple ordered replacements."
    )
    parameters = {
        "file_path": {
            "type": "string",
            "description": "Workspace-relative path to edit.",
        },
        "path": {
            "type": "string",
            "description": "Alias for file_path.",
        },
        "old_text": {
            "type": "string",
            "description": "Exact text to replace.",
        },
        "new_text": {
            "type": "string",
            "description": "Replacement text.",
        },
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["old_text", "new_text"],
            },
            "description": "Optional ordered list of exact replacements.",
        },
    }
    required_params: list[str] = []

    async def execute(self, **kwargs: Any) -> ToolResult:
        workspace_dir = str(kwargs.get("workspace_dir", ""))
        if not workspace_dir:
            return ToolResult.fail("workspace_dir is required")
        file_path = _path_arg(kwargs)
        if not file_path:
            return ToolResult.fail("'file_path' is required")
        try:
            resolved = self.validate_path(file_path, workspace_dir)
        except Exception as error:
            return ToolResult.fail(str(error))
        if not os.path.isfile(resolved):
            return ToolResult.fail(f"File not found: {file_path}")

        edits = self._normalize_edits(kwargs)
        if isinstance(edits, str):
            return ToolResult.fail(edits)
        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as source:
                content = source.read()
            replacements = 0
            for old_text, new_text in edits:
                if old_text == "":
                    return ToolResult.fail("old_text must not be empty")
                count = content.count(old_text)
                if count == 0:
                    return ToolResult.fail("old_text was not found in the file")
                if count > 1:
                    return ToolResult.fail(
                        "old_text matched multiple locations; provide a larger unique snippet"
                    )
                content = content.replace(old_text, new_text, 1)
                replacements += 1
            with open(resolved, "w", encoding="utf-8", newline="\n") as target:
                target.write(content)
        except OSError as error:
            return ToolResult.fail(f"Cannot edit {file_path}: {error}")

        rel_path = os.path.relpath(resolved, workspace_dir)
        return ToolResult.ok(f"Edited {rel_path} ({replacements} replacement(s)).")

    @staticmethod
    def _normalize_edits(kwargs: dict[str, Any]) -> list[tuple[str, str]] | str:
        raw_edits = kwargs.get("edits")
        if raw_edits is not None:
            if not isinstance(raw_edits, list) or not raw_edits:
                return "'edits' must be a non-empty list"
            edits: list[tuple[str, str]] = []
            for item in raw_edits:
                if not isinstance(item, dict):
                    return "each edit must be an object"
                old_text = item.get("old_text")
                new_text = item.get("new_text")
                if not isinstance(old_text, str) or not isinstance(new_text, str):
                    return "each edit requires string old_text and new_text"
                edits.append((old_text, new_text))
            return edits

        old_text = kwargs.get("old_text")
        new_text = kwargs.get("new_text")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            return "old_text and new_text must be strings"
        return [(old_text, new_text)]


__all__ = ["EditFileTool", "WriteFileTool"]
