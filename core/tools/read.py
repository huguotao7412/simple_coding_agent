from __future__ import annotations
import os
from .base import BaseTool, ToolResult


class ReadTool(BaseTool):
    name = "read"
    description = "Read a file from the workspace. Returns content with line number prefixes."
    parameters = {
        "file_path": {"type": "string", "description": "Absolute path to the file."},
        "offset": {"type": "integer", "description": "Starting line (0-indexed). Default 0."},
        "limit": {"type": "integer", "description": "Max lines to read. Default 2000."},
    }
    required_params = ["file_path"]

    async def execute(self, file_path: str, workspace_dir: str = "", offset: int = 0, limit: int = 2000) -> ToolResult:
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
            return ToolResult.ok(output)
        except Exception as e:
            return ToolResult.fail(str(e))
