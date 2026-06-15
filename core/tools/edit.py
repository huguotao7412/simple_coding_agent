from __future__ import annotations
import difflib
import os
from .base import BaseTool, ToolResult


class EditTool(BaseTool):
    name = "edit"
    description = (
        "Make precise edits to a file using context-aware search/replace. "
        "Provide a search_block (the exact code to find, including surrounding "
        "context for uniqueness) and a replace_block (the new code to substitute). "
        "The tool matches exactly first, then falls back to line-normalized fuzzy matching."
    )
    parameters = {
        "file_path": {"type": "string", "description": "Absolute path to the file."},
        "search_block": {
            "type": "string",
            "description": "The exact block of code to search for, including context. Must be unique in the file.",
        },
        "replace_block": {
            "type": "string",
            "description": "The new block of code that will replace the search_block. Must include proper indentation.",
        },
    }
    required_params = ["file_path", "search_block", "replace_block"]

    async def execute(
        self,
        file_path: str,
        search_block: str,
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
        # Step 1 — Exact match
        # ================================================================
        count = content.count(search_block)
        if count == 1:
            new_content = content.replace(search_block, replace_block, 1)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            diff = difflib.unified_diff(
                content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=file_path,
                tofile=file_path,
            )
            diff_text = "".join(diff)
            return ToolResult.ok(diff_text if diff_text else "No changes made.")

        if count > 1:
            return ToolResult.fail(
                f"Found {count} occurrences of search_block in {file_path}. "
                "Please include more surrounding context (unchanged lines above and below) "
                "to make the match unique."
            )

        # ================================================================
        # Step 2 — Line-normalized fuzzy fallback
        # ================================================================
        # Use keepends=True for reconstruction so newlines are preserved.
        file_lines = content.splitlines(keepends=True)
        # For normalisation we strip line-endings and whitespace.
        search_lines = search_block.splitlines()

        def normalise(lines: list[str]) -> list[str]:
            """Strip whitespace and discard blank lines."""
            return [ln.strip() for ln in lines if ln.strip()]

        # Build a mapping: normalised-index → original-index (only for non-blank lines)
        norm_to_orig: list[int] = []
        norm_file: list[str] = []
        for idx, line in enumerate(file_lines):
            stripped = line.strip()
            if stripped:
                norm_to_orig.append(idx)
                norm_file.append(stripped)

        norm_search = normalise(search_lines)

        if not norm_search:
            return ToolResult.fail("search_block is empty after normalisation.")

        # Sliding window over normalised file lines
        matches: list[tuple[int, int]] = []  # (start_idx, end_idx) in original file_lines
        w = len(norm_search)
        for i in range(len(norm_file) - w + 1):
            if norm_file[i : i + w] == norm_search:
                start_orig = norm_to_orig[i]
                end_orig = norm_to_orig[i + w - 1]
                matches.append((start_orig, end_orig))

        if len(matches) == 0:
            return ToolResult.fail(
                "search_block not found in the file (neither exact nor fuzzy match). "
                "Please verify the content and try again with the exact code from the file, "
                "including at least one line of unchanged context above and below."
            )
        if len(matches) > 1:
            locs = ", ".join(f"[{s}:{e}]" for s, e in matches)
            return ToolResult.fail(
                f"search_block matched {len(matches)} locations (lines {locs}). "
                "Please include more surrounding context to make the match unique."
            )

        start_idx, end_idx = matches[0]
        if replace_block and not replace_block.endswith("\n"):
            replace_block += "\n"
        replace_lines = replace_block.splitlines(keepends=True)

        new_lines = (
            file_lines[:start_idx]
            + replace_lines
            + file_lines[end_idx + 1 :]
        )
        new_content = "".join(new_lines)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        diff = difflib.unified_diff(
            content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=file_path,
            tofile=file_path,
        )
        diff_text = "".join(diff)
        return ToolResult.ok(diff_text if diff_text else "No changes made.")
