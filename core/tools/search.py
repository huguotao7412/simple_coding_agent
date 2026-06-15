from __future__ import annotations

import ast
import os
import re

from .base import BaseTool, ToolResult


class SearchCodebaseTool(BaseTool):
    name = "search_codebase"
    description = (
        "Search the codebase for symbols (classes/functions) or specific text patterns. "
        "Use 'symbol' mode to quickly locate class and function signatures and their docstrings "
        "by parsing Python AST. Use 'text' mode for generic regex search with surrounding context."
    )
    parameters = {
        "query": {"type": "string", "description": "The target text or symbol name to search for."},
        "mode": {
            "type": "string",
            "enum": ["symbol", "text"],
            "description": "'symbol' to parse Python AST for class/function signatures. 'text' for generic regex search.",
        },
        "include_ext": {
            "type": "string",
            "description": "Optional file extension to filter (e.g., '.py', '.md'). Default searches all relevant files.",
        },
    }
    required_params = ["query", "mode"]

    # Directories ignored during traversal
    _IGNORED_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".idea", "dist"}

    async def execute(
        self,
        query: str,
        mode: str,
        workspace_dir: str = "",
        include_ext: str | None = None,
    ) -> ToolResult:
        if not os.path.isdir(workspace_dir):
            return ToolResult.fail(f"Workspace directory not found: {workspace_dir}")

        if mode == "symbol":
            return await self._search_symbols(workspace_dir, query, include_ext)
        elif mode == "text":
            return await self._search_text(workspace_dir, query, include_ext)
        else:
            return ToolResult.fail(
                f"Unknown mode '{mode}'. Supported modes: 'symbol', 'text'."
            )

    # ------------------------------------------------------------------
    # Symbol mode — AST-based
    # ------------------------------------------------------------------
    async def _search_symbols(
        self, root: str, query: str, include_ext: str | None
    ) -> ToolResult:
        results: list[str] = []
        query_lower = query.lower()

        for dirpath, dirnames, filenames in os.walk(root):
            # Prune ignored directories in-place
            dirnames[:] = [d for d in dirnames if d not in self._IGNORED_DIRS]

            for fname in filenames:
                if include_ext and not fname.endswith(include_ext):
                    continue
                if include_ext is None and not fname.endswith(".py"):
                    continue  # symbol mode only handles .py by default

                full_path = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(full_path, root)

                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        source = f.read()
                except Exception:
                    continue

                try:
                    tree = ast.parse(source)
                except SyntaxError:
                    continue

                for node in ast.walk(tree):
                    if not isinstance(
                        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    ):
                        continue

                    if query_lower not in node.name.lower():
                        continue

                    # Build signature
                    signature = self._build_signature(node, source)

                    # Extract docstring
                    doc = ast.get_docstring(node)
                    doc_summary = ""
                    if doc:
                        doc_summary = " - " + doc.splitlines()[0].strip()

                    results.append(
                        f"[{rel_path}] L{node.lineno}-L{node.end_lineno}: {signature}{doc_summary}"
                    )

        if not results:
            return ToolResult.ok(
                f"No symbols matching '{query}' found in the codebase."
            )
        return ToolResult.ok("\n".join(results))

    def _build_signature(self, node: ast.AST, source: str) -> str:
        """Build a human-readable signature for a function or class definition."""
        try:
            lines = source.splitlines()
            start_line = node.lineno - 1
            if hasattr(node, 'body') and node.body:
                body_start = node.body[0].lineno
                if body_start <= node.lineno:
                    # Single-line body: e.g. "def foo(): pass"
                    end_line = body_start
                else:
                    end_line = body_start - 1
            else:
                end_line = node.end_lineno
            sig_lines = lines[start_line:end_line]
            return " ".join(line.strip() for line in sig_lines)
        except Exception:
            return node.name

    # ------------------------------------------------------------------
    # Text mode — regex-based with context
    # ------------------------------------------------------------------
    async def _search_text(
        self, root: str, query: str, include_ext: str | None
    ) -> ToolResult:
        results: list[str] = []
        pattern = re.compile(re.escape(query))

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in self._IGNORED_DIRS]

            for fname in filenames:
                if include_ext and not fname.endswith(include_ext):
                    continue

                full_path = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(full_path, root)

                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                except UnicodeDecodeError:
                    continue
                except Exception:
                    continue

                for i, line in enumerate(lines):
                    if not pattern.search(line):
                        continue

                    # Extract context window: 2 lines before, match line, 2 lines after
                    ctx_start = max(0, i - 2)
                    ctx_end = min(len(lines), i + 3)  # +3 because range end is exclusive

                    snippet_parts: list[str] = []
                    for j in range(ctx_start, ctx_end):
                        prefix = ">" if j == i else " "
                        snippet_parts.append(f"  {prefix} L{j}: {lines[j].rstrip()}")

                    results.append(
                        f"[{rel_path}] L{i}:\n" + "\n".join(snippet_parts)
                    )

        if not results:
            return ToolResult.ok(
                f"No matches for '{query}' found in the codebase."
            )
        return ToolResult.ok("\n\n".join(results))
