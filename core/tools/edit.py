from __future__ import annotations
import os
from .base import BaseTool, ToolResult


class EditTool(BaseTool):
    name = "edit"
    description = (
        "Make precise edits to a file. Two modes: "
        "(1) search/replace: provide old_string and new_string. "
        "Use replace_all=true to replace all occurrences. "
        "(2) line-range: provide start_line, end_line, new_string to replace a line range."
    )
    parameters = {
        "file_path": {"type": "string", "description": "Absolute path to the file."},
        "old_string": {"type": "string", "description": "Text to search for (search/replace mode)."},
        "new_string": {"type": "string", "description": "Replacement text."},
        "start_line": {"type": "integer", "description": "Start line for line-range mode (0-indexed)."},
        "end_line": {"type": "integer", "description": "End line for line-range mode (inclusive)."},
        "replace_all": {"type": "boolean", "description": "Replace all matches. Default false."},
    }
    required_params = ["file_path"]

    async def execute(
        self,
        file_path: str,
        workspace_dir: str = "",
        old_string: str | None = None,
        new_string: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        replace_all: bool = False,
    ) -> ToolResult:
        try:
            self.validate_path(file_path, workspace_dir)
        except Exception as e:
            return ToolResult.fail(str(e))
        if not os.path.isfile(file_path):
            return ToolResult.fail(f"File not found: {file_path}")
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return ToolResult.fail(str(e))

        # Line-range mode
        if start_line is not None and end_line is not None and new_string is not None:
            lines = content.splitlines(keepends=True)
            if start_line < 0 or end_line >= len(lines):
                return ToolResult.fail(f"Line range [{start_line}:{end_line}] out of bounds (file has {len(lines)} lines)")
            new_lines = lines[:start_line] + [new_string if new_string.endswith("\n") else new_string + "\n"] + lines[end_line + 1:]
            new_content = "".join(new_lines)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            return ToolResult.ok(f"Replaced lines [{start_line}:{end_line}] in {file_path}")

        # Search/replace mode
        if old_string is not None and new_string is not None:
            count = content.count(old_string)
            if count == 0:
                return ToolResult.fail(f"old_string not found in {file_path}")
            if count > 1 and not replace_all:
                return ToolResult.fail(
                    f"Found {count} occurrences of old_string in {file_path}. "
                    "Use replace_all=true to replace all, or provide a more specific old_string."
                )
            new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            replaced = count if replace_all else 1
            return ToolResult.ok(f"Replaced {replaced} occurrence(s) in {file_path}")

        return ToolResult.fail("Must provide either (old_string + new_string) or (start_line + end_line + new_string)")
