from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ToolResult:
    success: bool
    content: str = ""
    error: str | None = None

    @classmethod
    def ok(cls, content: str) -> ToolResult:
        return cls(success=True, content=content)

    @classmethod
    def fail(cls, error: str, content: str = "") -> ToolResult:
        return cls(success=False, content=content, error=error)


class BaseTool(ABC):
    name: str
    description: str
    parameters: dict = {}
    required_params: list[str] = []

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required_params,
                },
            },
        }

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult: ...

    def validate_path(self, file_path: str, workspace_dir: str) -> str:
        import os
        if not os.path.isabs(file_path):
            file_path = os.path.join(workspace_dir, file_path)

        resolved = os.path.realpath(file_path)
        workspace_real = os.path.realpath(workspace_dir)

        if not resolved.startswith(workspace_real + os.sep) and resolved != workspace_real:
            from core.exceptions import ToolSecurityError
            raise ToolSecurityError(
                self.name,
                f"Path '{file_path}' escapes workspace '{workspace_dir}'. Please use paths relative to the workspace."
            )
        return resolved

import re

DEFAULT_TOKEN_BUDGET = 8000
ERROR_PATTERNS = [
    r"(?i)\b(error|exception|traceback|failed|failure|fatal|critical)\b",
    r"(?i)\b(warning|warn|deprecated)\b",
    r"^\s*File\s+\".+?\",\s+line\s+\d+",
    r"^\s*\^+$",
]

CODE_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx", ".rs", ".go", ".java", ".c", ".cpp", ".h"}


def semantic_truncate(
    text: str,
    file_path: str | None = None,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    token_counter=None,
) -> tuple[str, bool]:
    """Semantically truncate text to fit within token_budget.

    Returns (truncated_text, was_degraded: bool).

    Degradation levels:
      L0: within budget -> return as-is, was_degraded=False
      L1: code file + over budget -> hint to use read_outline, was_degraded=True
      L2: non-code or fallback -> smart truncation preserving error lines, was_degraded=True
    """
    estimated_tokens = token_counter(text) if token_counter else len(text) // 3

    if estimated_tokens <= token_budget:
        return text, False

    # L1: Code file -> hint to use outline
    if file_path:
        ext = file_path[file_path.rfind("."):].lower() if "." in file_path else ""
        if ext in CODE_EXTENSIONS:
            hint = (
                f"[Content degraded: file {file_path} exceeds token budget. "
                f"Use read_outline to view the skeleton structure, "
                f"or read with offset/limit for specific sections.]"
            )
            return hint, True

    # L2: Smart truncation preserving key lines
    lines = text.splitlines()
    head_count = max(1, int(len(lines) * 0.15))
    tail_count = max(1, int(len(lines) * 0.15))

    error_line_indices: set[int] = set()
    for i, line in enumerate(lines):
        for pattern in ERROR_PATTERNS:
            if re.search(pattern, line):
                error_line_indices.add(i)
                break

    head = lines[:head_count]
    tail = lines[-tail_count:]

    middle_errors = []
    for i in sorted(error_line_indices):
        if head_count <= i < len(lines) - tail_count:
            middle_errors.append(lines[i])

    omitted = len(lines) - len(head) - len(tail) - len(middle_errors)
    marker = f"\n... [{omitted} lines omitted - use read with offset/limit for full content] ...\n"

    result_lines = list(head)
    if middle_errors:
        result_lines.append("\n... [key lines from omitted section] ...")
        result_lines.extend(middle_errors)
    result_lines.append(marker)
    result_lines.extend(tail)

    return "\n".join(result_lines), True
