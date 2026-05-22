from __future__ import annotations
import os
from .base import BaseTool, ToolResult


class WriteTool(BaseTool):
    name = "write"
    description = "Create or overwrite a file in the workspace with the given content."
    parameters = {
        "file_path": {"type": "string", "description": "Absolute path to the file."},
        "content": {"type": "string", "description": "Full file content to write."},
    }
    required_params = ["file_path", "content"]

    async def execute(self, file_path: str, content: str, workspace_dir: str = "") -> ToolResult:
        try:
            self.validate_path(file_path, workspace_dir)
        except Exception as e:
            return ToolResult.fail(str(e))
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            lines = content.count("\n") + 1
            return ToolResult.ok(f"Wrote {lines} lines to {file_path}")
        except Exception as e:
            return ToolResult.fail(str(e))
