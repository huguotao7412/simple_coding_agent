from __future__ import annotations
import difflib
import os
from .base import BaseTool, ToolResult


class EditTool(BaseTool):
    name = "edit"
    description = (
        "Make precise edits to a file using absolute line numbers. "
        "Provide start_line and end_line (inclusive, 1-indexed — must match "
        "the line numbers shown by the read tool) and a replace_block with the "
        "new code. The tool replaces lines [start_line, end_line] with replace_block."
    )
    parameters = {
        "file_path": {"type": "string", "description": "Absolute path to the file."},
        "start_line": {
            "type": "integer",
            "description": (
                "Starting line number of the block to replace (inclusive, 1-indexed). "
                "Must match the line numbers returned by the read tool exactly."
            ),
        },
        "end_line": {
            "type": "integer",
            "description": (
                "Ending line number of the block to replace (inclusive, 1-indexed). "
                "For a pure insertion without deleting any lines, set end_line = start_line - 1 "
                "(i.e. end_line points just before the insertion point). "
                "For a pure deletion, pass an empty replace_block."
            ),
        },
        "replace_block": {
            "type": "string",
            "description": (
                "The new block of code that will replace lines start_line through end_line. "
                "Must include proper indentation. Pass an empty string to delete the target lines."
            ),
        },
    }
    required_params = ["file_path", "start_line", "end_line", "replace_block"]

    async def execute(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
        replace_block: str,
        workspace_dir: str = "",
    ) -> ToolResult:
        # --- Security & existence checks ---
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

        # ================================================================
        # Line-number-based replacement
        # ================================================================
        file_lines = content.splitlines(keepends=True)
        total_lines = len(file_lines)

        # --- Handle pure insertion (end_line = start_line - 1) ---
        if end_line == start_line - 1:
            # Insert replace_block *before* start_line
            if start_line < 1 or start_line > total_lines + 1:
                return ToolResult.fail(
                    f"Invalid insertion point. File has {total_lines} lines, "
                    f"received start_line={start_line} (valid range: 1–{total_lines + 1})."
                )
            start_idx = start_line - 1
            end_idx = start_idx  # nothing to delete
        else:
            # --- Validate line range ---
            if start_line < 1:
                return ToolResult.fail(
                    f"start_line must be >= 1, got {start_line}."
                )
            if end_line > total_lines:
                return ToolResult.fail(
                    f"end_line ({end_line}) exceeds file length ({total_lines} lines)."
                )
            if start_line > end_line:
                return ToolResult.fail(
                    f"start_line ({start_line}) must be <= end_line ({end_line}). "
                    "For pure insertion, use end_line = start_line - 1."
                )

            # Convert 1-indexed to 0-indexed
            start_idx = start_line - 1
            end_idx = end_line  # exclusive upper bound for slicing

        # --- Prepare replacement lines ---
        if replace_block and not replace_block.endswith("\n"):
            replace_block += "\n"
        replace_lines = replace_block.splitlines(keepends=True) if replace_block else []

        # --- Slice & rebuild ---
        new_lines = (
            file_lines[:start_idx]
            + replace_lines
            + file_lines[end_idx:]
        )
        new_content = "".join(new_lines)

        # --- Write back ---
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        # --- Return unified diff ---
        diff = difflib.unified_diff(
            content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=file_path,
            tofile=file_path,
        )
        diff_text = "".join(diff)
        return ToolResult.ok(diff_text if diff_text else "No changes made.")
