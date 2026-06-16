from __future__ import annotations
import ast
import difflib
import json
import os
import re
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


def _normalize_lines(text: str) -> str:
    """Strip trailing whitespace from each line (preserves leading indent and exact line count)."""
    return re.sub(r'[ \t]+$', '', text, flags=re.MULTILINE)


def _fuzzy_find(content: str, search: str) -> tuple[int, int] | None:
    """Find the best fuzzy match for `search` in `content`.

    Uses difflib.SequenceMatcher on whitespace‑normalized lines. Returns
    (start_char, end_char) in the **original** content, or None if the
    best match is below the similarity threshold.
    """
    content_norm = _normalize_lines(content)
    search_norm = _normalize_lines(search)

    sm = difflib.SequenceMatcher(None, content_norm, search_norm)
    # ratio() measures overall similarity; require ≥ 85 %
    if sm.ratio() < 0.85:
        return None

    # Use get_matching_blocks to locate the best contiguous run
    blocks = sm.get_matching_blocks()
    if not blocks or len(blocks) <= 1:
        return None

    # The matching blocks (except the sentinel) cover the search text
    # Map the first and last real match back to content positions
    real_blocks = [b for b in blocks if b.size > 0]
    if not real_blocks:
        return None

    start = real_blocks[0].a
    end = real_blocks[-1].a + real_blocks[-1].size

    # Guard: don't return tiny/spurious matches
    if end - start < max(len(search_norm) * 0.5, 4):
        return None

    return start, end


class EditTool(BaseTool):
    name = "edit"
    description = (
        "Make precise edits to a file by providing the exact code block to replace. "
        "Provide `search_block` (the existing code to replace — copy‑pasted from the "
        "file) and `replace_block` (the new code). The tool locates the unique match "
        "and replaces it. "
        "If the match is ambiguous (multiple hits) or not found, you must re‑read "
        "the file and provide a more specific search_block."
    )
    parameters = {
        "file_path": {"type": "string", "description": "Absolute path to the file."},
        "search_block": {
            "type": "string",
            "description": (
                "The exact code block to find and replace. Must be unique in the file. "
                "Copy‑paste the exact lines from the file as shown by the read tool. "
                "Include surrounding context lines if needed to make the match unique."
            ),
        },
        "replace_block": {
            "type": "string",
            "description": (
                "The new code that will replace search_block. "
                "Must include proper indentation. Pass an empty string to delete search_block."
            ),
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
        # Level 1 — Exact match
        # ================================================================
        count = content.count(search_block)

        if count == 0:
            # ================================================================
            # Level 2 — Whitespace‑normalized match (trailing ws tolerant)
            # ================================================================
            content_norm = _normalize_lines(content)
            search_norm = _normalize_lines(search_block)
            norm_count = content_norm.count(search_norm)

            if norm_count == 1 and search_norm:
                # Map normalized position back to original content
                norm_start = content_norm.index(search_norm)
                norm_end = norm_start + len(search_norm)
                # Find the corresponding original span by walking lines
                mapped = _map_norm_to_original(content, content_norm, norm_start, norm_end)
                if mapped is not None:
                    old_block, s_idx, e_idx = mapped
                    new_content = content[:s_idx] + replace_block + content[e_idx:]
                    return self._write_and_diff(file_path, content, new_content)

            # ================================================================
            # Level 3 — Fuzzy similarity via SequenceMatcher
            # ================================================================
            span = _fuzzy_find(content, search_block)
            if span is not None:
                mapped = _map_norm_to_original(content, content_norm, span[0], span[1])
                if mapped is not None:
                    old_block, s_idx, e_idx = mapped
                    new_content = content[:s_idx] + replace_block + content[e_idx:]
                    return self._write_and_diff(
                        file_path, content, new_content,
                        note="[Note: fuzzy match applied — verify the diff carefully]",
                    )

            # --- Not found at all ---
            if norm_count > 1:
                return ToolResult.fail(
                    f"search_block is ambiguous: found {norm_count} similar matches "
                    f"after whitespace normalization. Provide more surrounding context "
                    f"lines to make the match unique, then re‑read the file and try again.",
                )
            return ToolResult.fail(
                "search_block not found in file (exact, whitespace‑normalized, "
                "or fuzzy match all failed). Please re‑read the file and try again.",
            )

        if count > 1:
            return ToolResult.fail(
                f"search_block matched {count} identical occurrences. "
                "Provide additional surrounding context lines to make the match unique, "
                "then re‑read the file and try again.",
            )

        # --- Unique exact match — fast path ---
        new_content = content.replace(search_block, replace_block, 1)
        return self._write_and_diff(file_path, content, new_content)

    def _write_and_diff(
        self,
        file_path: str,
        old_content: str,
        new_content: str,
        note: str = "",
    ) -> ToolResult:
        """Validate syntax, write the file, and return a unified diff."""

        # --- Proactive syntax validation before writing ---
        syntax_error = _validate_syntax(file_path, new_content)
        if syntax_error:
            return ToolResult.fail(
                f"Edit would result in SyntaxError: {syntax_error}\nCode change rejected.",
                content="",
            )

        # --- Write back ---
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        # --- Return unified diff ---
        diff = difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=file_path,
            tofile=file_path,
        )
        diff_text = "".join(diff)
        if note:
            diff_text = f"{note}\n{diff_text}"
        return ToolResult.ok(diff_text if diff_text else "No changes made.")


def _map_norm_to_original(
        original: str, normalized: str, norm_start: int, norm_end: int
) -> str | None:
    orig_lines = original.splitlines(keepends=True)
    norm_lines = normalized.splitlines(keepends=True)

    def get_orig_idx(norm_idx: int) -> int:
        o_pos = 0
        n_pos = 0
        for o, n in zip(orig_lines, norm_lines):
            # 判断字符索引是否落在当前行
            if n_pos <= norm_idx < n_pos + len(n):
                offset = norm_idx - n_pos
                # 如果匹配到了去空格后的换行符位置，强行将其映射回原始文本的换行符
                # 这样就能把原本被剔除的尾随空格完整囊括进替换块中
                if n.endswith('\n') and offset == len(n) - 1:
                    return o_pos + len(o) - 1
                elif n.endswith('\r\n') and offset >= len(n) - 2:
                    return o_pos + len(o) - (len(n) - offset)
                return o_pos + offset
            o_pos += len(o)
            n_pos += len(n)
        return o_pos

    start_orig = get_orig_idx(norm_start)
    end_orig = get_orig_idx(norm_end)

    if start_orig >= end_orig:
        return None
    return original[start_orig:end_orig], start_orig, end_orig
