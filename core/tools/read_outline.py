from __future__ import annotations

import ast
import os
import re

from .base import BaseTool, ToolResult


class ReadOutlineTool(BaseTool):
    name = "read_outline"
    description = (
        "Read a file's skeleton: returns only class/function signatures with "
        "line numbers (AST-based). Use this FIRST on large files to understand "
        "the structure, then use 'read' with specific offset/limit to view "
        "implementation details. Dramatically reduces token usage on large files."
    )
    parameters = {
        "file_path": {
            "type": "string",
            "description": "Absolute or workspace-relative path to the file.",
        },
    }
    required_params = ["file_path"]

    async def execute(self, file_path: str, workspace_dir: str = "") -> ToolResult:
        if not os.path.isabs(file_path):
            file_path = os.path.join(workspace_dir, file_path)

        try:
            self.validate_path(file_path, workspace_dir)
        except Exception as e:
            return ToolResult.fail(str(e))

        if not os.path.isfile(file_path):
            return ToolResult.fail(f"File not found: {file_path}")

        rel_path = os.path.relpath(file_path, workspace_dir) if workspace_dir else file_path

        # --- Non-Python files: regex-based outline ---
        if not file_path.endswith(".py"):
            return await self._regex_outline(file_path, rel_path)

        # --- Python files: AST-based outline ---
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                source = f.read()
            tree = ast.parse(source)
        except SyntaxError as e:
            # Fall back to regex for syntactically invalid Python
            return await self._regex_outline(file_path, rel_path, note=f"SyntaxError: {e}")
        except Exception as e:
            return ToolResult.fail(f"Cannot parse {rel_path}: {e}")

        lines_out: list[str] = [f"Outline of {rel_path}:"]
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # Build signature line
                try:
                    sig = self._build_signature(node, source)
                except Exception:
                    sig = node.name

                # Class vs function indicator
                if isinstance(node, ast.ClassDef):
                    prefix = "[Class]"
                elif isinstance(node, ast.AsyncFunctionDef):
                    prefix = "[AsyncFunc]"
                else:
                    prefix = "[Func]"

                # Extract docstring summary
                doc = ast.get_docstring(node)
                doc_summary = ""
                if doc:
                    first_line = doc.splitlines()[0].strip()
                    doc_summary = f" — {first_line[:80]}"

                lines_out.append(f"  L{node.lineno:>5d}  {prefix:>11s}  {sig}{doc_summary}")

        return ToolResult.ok("\n".join(lines_out))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _build_signature(self, node: ast.AST, source: str) -> str:
        """Build a single-line signature for a function or class definition."""
        try:
            lines = source.splitlines()
            start_line = node.lineno - 1
            if hasattr(node, "body") and node.body:
                body_start = node.body[0].lineno
                end_line = body_start - 1 if body_start > node.lineno else node.lineno
            elif hasattr(node, "end_lineno"):
                end_line = node.end_lineno
            else:
                end_line = node.lineno
            sig_lines = lines[start_line:end_line]
            return " ".join(line.strip() for line in sig_lines)[:200]
        except Exception:
            return str(node.name)

    async def _regex_outline(
        self,
        file_path: str,
        rel_path: str,
        note: str | None = None,
    ) -> ToolResult:
        """Fallback: regex-based outline for non-Python or broken Python files."""
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                source_lines = f.readlines()
        except Exception as e:
            return ToolResult.fail(f"Cannot read {rel_path}: {e}")

        lines_out: list[str] = [f"Outline of {rel_path} (regex, no AST):"]
        if note:
            lines_out.append(f"  (note: {note})")

        # Patterns for common languages
        patterns = [
            (r"^\s*(def|class|async\s+def)\s+", "[Py]"),
            (r"^\s*(function|class|const|let|var|export\s+(default\s+)?(function|class|const))\s+", "[JS/TS]"),
            (r"^\s*(pub\s+)?(fn|struct|enum|trait|impl|mod)\s+", "[Rust]"),
            (r"^\s*(func|type|interface)\s+", "[Go]"),
        ]

        for i, line in enumerate(source_lines):
            stripped = line.rstrip()[:200]
            for pattern, tag in patterns:
                if re.search(pattern, stripped, re.IGNORECASE):
                    lines_out.append(f"  L{i + 1:>5d}  {tag:>11s}  {stripped.strip()}")
                    break

        if len(lines_out) <= (2 if note else 1):
            lines_out.append("  (no recognizable symbols found)")

        return ToolResult.ok("\n".join(lines_out[:200]))  # cap at 200 lines
