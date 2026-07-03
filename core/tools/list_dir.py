from __future__ import annotations

import os

from .base import BaseTool, ToolResult


class ListDirTool(BaseTool):
    name = "list_dir"
    description = (
        "List files and subdirectories in a given directory (depth=1 only). "
        "Use this when the workspace tree is truncated and you need to "
        "explore a specific subdirectory. Returns a tree-like listing."
    )
    parameters = {
        "dir_path": {
            "type": "string",
            "description": "Absolute or workspace-relative path to the directory to list.",
        },
    }
    required_params = ["dir_path"]

    COMPACT_THRESHOLD_CHARS = 2000

    async def execute(self, dir_path: str, workspace_dir: str = "") -> ToolResult:
        # Resolve path
        if not os.path.isabs(dir_path):
            dir_path = os.path.join(workspace_dir, dir_path)

        try:
            self.validate_path(dir_path, workspace_dir)
        except Exception as e:
            return ToolResult.fail(str(e))

        if not os.path.isdir(dir_path):
            return ToolResult.fail(f"Directory not found: {dir_path}")

        try:
            entries = sorted(os.scandir(dir_path), key=lambda e: e.name.lower())
        except PermissionError:
            return ToolResult.fail(f"Permission denied: {dir_path}")
        except OSError as e:
            return ToolResult.fail(f"Cannot read directory {dir_path}: {e}")

        if not entries:
            return ToolResult.ok(f"[Empty directory] {dir_path}")

        lines: list[str] = []
        ignore_dirs = {".git", "__pycache__", ".venv", "node_modules",
                       ".mypy_cache", ".pytest_cache", ".idea", "dist"}
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                if entry.name in ignore_dirs:
                    lines.append(f"  📁 {entry.name}/ (hidden)")
                else:
                    lines.append(f"  📁 {entry.name}/")
            else:
                lines.append(f"  📄 {entry.name}")

        rel = os.path.relpath(dir_path, workspace_dir) if workspace_dir else dir_path
        header = f"Contents of {rel}/:"
        output = header + "\n" + "\n".join(lines)

        # Auto-compact: switch to JSON lines when output is large
        if len(output) > self.COMPACT_THRESHOLD_CHARS:
            compact_lines = [header]
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in ignore_dirs:
                        continue
                    compact_lines.append(f'{{"name": "{entry.name}", "type": "dir"}}')
                else:
                    compact_lines.append(f'{{"name": "{entry.name}", "type": "file"}}')
            return ToolResult.ok("\n".join(compact_lines))

        return ToolResult.ok(output)
