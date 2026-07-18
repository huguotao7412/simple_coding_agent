from __future__ import annotations

import os

from .base import BaseTool, ToolResult


class ReadTool(BaseTool):
    """Read an exact, line-addressable slice of a text file."""

    name = "read"
    description = (
        "Read source text with line numbers. Use this after search_codebase or "
        "read_outline to inspect implementations. offset is a 1-based source "
        "line and limit is the number of source lines to return. Unlike "
        "read_outline, this tool returns the actual file contents."
    )
    parameters = {
        "file_path": {
            "type": "string",
            "description": "Absolute or workspace-relative path to a text file.",
        },
        "offset": {
            "type": "integer",
            "minimum": 1,
            "description": "First source line to return (1-based; default 1).",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
            "description": "Maximum source lines to return (default 200, max 500).",
        },
    }
    required_params = ["file_path"]
    DEFAULT_LIMIT = 200
    MAX_LIMIT = 500
    MAX_OUTPUT_CHARS = 40_000

    async def execute(  # type: ignore[override]
        self,
        file_path: str,
        workspace_dir: str = "",
        offset: int = 1,
        limit: int = DEFAULT_LIMIT,
    ) -> ToolResult:
        if not workspace_dir:
            return ToolResult.fail("workspace_dir is required")

        try:
            resolved = self.validate_path(file_path, workspace_dir)
        except Exception as error:
            return ToolResult.fail(str(error))

        if not os.path.isfile(resolved):
            return ToolResult.fail(f"File not found: {file_path}")

        try:
            start = int(offset)
            requested_limit = int(limit)
        except (TypeError, ValueError):
            return ToolResult.fail("offset and limit must be integers")
        if start < 1:
            return ToolResult.fail("offset must be at least 1")
        if requested_limit < 1:
            return ToolResult.fail("limit must be at least 1")
        requested_limit = min(requested_limit, self.MAX_LIMIT)

        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as source:
                lines = source.readlines()
        except OSError as error:
            return ToolResult.fail(f"Cannot read {file_path}: {error}")

        total_lines = len(lines)
        rel_path = os.path.relpath(resolved, workspace_dir)
        if start > total_lines and total_lines:
            return ToolResult.fail(
                f"offset {start} is past the end of {rel_path} ({total_lines} lines)"
            )
        if total_lines == 0:
            return ToolResult.ok(f"{rel_path} is empty (0 lines).")

        end = min(total_lines, start + requested_limit - 1)
        rendered: list[str] = []
        rendered_chars = 0
        truncated_for_size = False
        for line_number in range(start, end + 1):
            rendered_line = f"L{line_number}: {lines[line_number - 1].rstrip()}"
            if rendered_chars + len(rendered_line) > self.MAX_OUTPUT_CHARS:
                if not rendered:
                    rendered.append(
                        rendered_line[: self.MAX_OUTPUT_CHARS]
                        + " ... [source line truncated]"
                    )
                truncated_for_size = True
                break
            rendered.append(rendered_line)
            rendered_chars += len(rendered_line) + 1

        actual_end = start + len(rendered) - 1
        header = f"{rel_path} (lines {start}-{actual_end} of {total_lines})"
        output = [header, *rendered]
        if truncated_for_size:
            output.append(
                f"... [output size limit reached; continue with offset={actual_end + 1}] ..."
            )
        elif actual_end < total_lines:
            output.append(f"... [continue with offset={actual_end + 1}] ...")
        return ToolResult.ok("\n".join(output))
