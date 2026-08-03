from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


ProviderPhase = Literal[
    "spawn", "initialize", "list_tools", "normalize_schema", "call", "shutdown"
]


@dataclass(frozen=True)
class ToolSchema:
    value: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.value, dict):
            raise TypeError("tool input schema must be an object")


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: ToolSchema
    source: str
    adapter_kind: str = "mcp"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool name must be a non-empty string")

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema.value,
            },
        }


@dataclass(frozen=True)
class ProviderDiagnostic:
    provider: str
    phase: ProviderPhase
    exception_type: str
    error_summary: str
    mcp_sdk_version: str
    degraded_to_local: bool
    affects_core_capability: bool

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "provider": self.provider,
            "phase": self.phase,
            "exception_type": self.exception_type,
            "error_summary": self.error_summary,
            "mcp_sdk_version": self.mcp_sdk_version,
            "degraded_to_local": self.degraded_to_local,
            "affects_core_capability": self.affects_core_capability,
        }


@dataclass(frozen=True)
class ProviderHealth:
    status: Literal["not_started", "healthy", "healthy_degraded", "unhealthy"]
    local_tools_available: bool
    connected_servers: tuple[str, ...]
    diagnostics: tuple[ProviderDiagnostic, ...]


__all__ = ["ProviderDiagnostic", "ProviderHealth", "ProviderPhase", "ToolDefinition", "ToolSchema"]
