from __future__ import annotations
import os
from .base import BaseTool, ToolResult, truncate_long_output


class ReadTool(BaseTool):
    name = "read"
    description = (
        "Read a file from the workspace. Returns content with line number prefixes. "
        "For large files, read in chunks of 500 lines using offset."
    )
    parameters = {
        "file_path": {"type": "string", "description": "Absolute path to the file."},
        "offset": {"type": "integer", "description": "Starting line (0-indexed). Default 0."},
        "limit": {"type": "integer", "description": "Max lines to read. Default 500 (chunked reading recommended for large files)."},
    }
    required_params = ["file_path"]

    async def execute(self, file_path: str, workspace_dir: str = "", offset: int = 0, limit: int = 500) -> ToolResult:
        try:
            self.validate_path(file_path, workspace_dir)
        except Exception as e:
            return ToolResult.fail(str(e))
        if not os.path.isfile(file_path):
            return ToolResult.fail(f"File not found: {file_path}")
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            if offset:
                lines = lines[offset:]
            if limit:
                lines = lines[:limit]
            output = "".join(f"{i + offset + 1}\t{line}" for i, line in enumerate(lines))
            return ToolResult.ok(truncate_long_output(output))
        except Exception as e:
            return ToolResult.fail(str(e))
