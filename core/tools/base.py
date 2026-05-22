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
        resolved = os.path.realpath(file_path)
        workspace_real = os.path.realpath(workspace_dir)
        if not resolved.startswith(workspace_real + os.sep) and resolved != workspace_real:
            from core.exceptions import ToolSecurityError
            raise ToolSecurityError(
                self.name,
                f"Path '{file_path}' escapes workspace '{workspace_dir}'",
            )
        return resolved
