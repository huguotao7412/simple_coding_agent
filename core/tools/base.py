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


TRUNCATION_THRESHOLD = 3000


def truncate_long_output(text: str, threshold: int = TRUNCATION_THRESHOLD) -> str:
    """Truncate long text, keeping first 20% and last 30% of threshold chars.

    Inserts a visible marker so the LLM knows content was omitted.
    """
    if len(text) <= threshold:
        return text

    keep_head = int(threshold * 0.2)
    keep_tail = int(threshold * 0.3)
    omitted = len(text) - keep_head - keep_tail

    head = text[:keep_head]
    tail = text[-keep_tail:]
    return (
        head
        + f"\n... [Output truncated: {omitted} chars omitted for brevity] ...\n"
        + tail
    )
