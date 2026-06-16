from __future__ import annotations
import ast
import json
import os
from .base import BaseTool, ToolResult


def _validate_syntax(file_path: str, content: str) -> str | None:
    """Validate content against file extension. Returns error message or None if valid."""
    _, ext = os.path.splitext(file_path)
    if ext == ".py":
        try:
            ast.parse(content)
        except SyntaxError as e:
            return f"SyntaxError: {e.msg} at line {e.lineno}, column {e.offset}"
    elif ext == ".json":
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            return f"JSONDecodeError: {e.msg} at line {e.lineno}, column {e.colno}"
    return None


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

        # --- Proactive syntax validation before writing ---
        syntax_error = _validate_syntax(file_path, content)
        if syntax_error:
            return ToolResult.fail(
                f"Write rejected to prevent broken syntax: {syntax_error}\nCode change rejected.",
                content="",
            )

        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            lines = content.count("\n") + 1
            return ToolResult.ok(f"Wrote {lines} lines to {file_path}")
        except Exception as e:
            return ToolResult.fail(str(e))
