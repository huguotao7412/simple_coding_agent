from __future__ import annotations

import ast
import os
import re
import asyncio

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
            return await asyncio.to_thread(self._search_symbols_sync, workspace_dir, query, include_ext)
        elif mode == "text":
            return await asyncio.to_thread(self._search_text_sync, workspace_dir, query, include_ext)
        else:
            return ToolResult.fail(
                f"Unknown mode '{mode}'. Supported modes: 'symbol', 'text'."
            )

    # ------------------------------------------------------------------
    # Symbol mode — AST-based
    # ------------------------------------------------------------------
    def _search_symbols_sync(
        self, root: str, query: str, include_ext: str | None
    ) -> ToolResult:
        results: list[str] = []
        query_lower = query.lower()

        for dirpath, dirnames, filenames in os.walk(root):
            # Prune ignored directories in-place
            dirnames[:] = [d for d in dirnames if d not in self._IGNORED_DIRS]

            for fname in filenames:
                if include_ext:
                    if not fname.endswith(include_ext):
                        continue
                elif not fname.endswith(".py"):
                    continue  # symbol mode defaults to .py

                full_path = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(full_path, root)

                # 1. 初始预判：是否尝试使用 AST
                use_ast = fname.endswith(".py")
                tree = None
                source = ""

                if use_ast:
                    try:
                        with open(full_path, "r", encoding="utf-8") as f:
                            source = f.read()
                        tree = ast.parse(source)
                    except Exception:
                        # 核心修复点：遇到 SyntaxError 或解码失败，不抛出也不 continue
                        # 而是将 use_ast 置为 False，平滑降级到下方的正则搜索
                        use_ast = False

                        # 2. 根据预判结果分流
                if use_ast and tree is not None:
                    # --- .py files (语法正确): 走原生 AST 解析 ---
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
                else:
                    # --- Non-.py files (或存在语法错误的 .py 文件): 走纯正则匹配 ---
                    try:
                        with open(full_path, "r", encoding="utf-8") as f:
                            source_lines = f.readlines()
                    except Exception:
                        continue

                    symbol_pattern = re.compile(
                        r"^\s*(def|class|function|fn|async\s+def|async\s+function)\s+"
                        + re.escape(query),
                        re.IGNORECASE,
                    )
                    for i, line in enumerate(source_lines):
                        if symbol_pattern.search(line):
                            results.append(
                                f"[{rel_path}] L{i + 1}: {line.strip()[:120]}"
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
    def _search_text_sync(
        self, root: str, query: str, include_ext: str | None
    ) -> ToolResult:
        results: list[str] = []
        try:
            pattern = re.compile(query)
        except re.error as e:
            return ToolResult.fail(f"Invalid regex query '{query}': {e}. Please fix your regex pattern.")

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
