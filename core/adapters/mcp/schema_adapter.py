from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .models import ToolDefinition, ToolSchema


EMPTY_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


class SerializableMCPTool(Protocol):
    def model_dump(self, *, by_alias: bool = False) -> dict[str, Any]: ...


class SchemaNormalizationError(ValueError):
    """A single advertised MCP tool violated the protocol contract."""


def normalize_mcp_tool(tool: SerializableMCPTool, *, source: str) -> ToolDefinition:
    """Convert an SDK Tool to the stable domain model using its public API."""
    try:
        payload = tool.model_dump(by_alias=True)
    except (AttributeError, TypeError, ValueError) as error:
        raise SchemaNormalizationError(
            f"MCP Tool serialization failed: {type(error).__name__}"
        ) from error
    if not isinstance(payload, Mapping):
        raise SchemaNormalizationError("MCP Tool serialization must return an object")
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SchemaNormalizationError("MCP Tool name must be a non-empty string")
    description = payload.get("description")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise SchemaNormalizationError("MCP Tool description must be a string or null")
    raw_schema = payload.get("inputSchema")
    if raw_schema is None:
        schema = dict(EMPTY_INPUT_SCHEMA)
    elif isinstance(raw_schema, Mapping):
        schema = dict(raw_schema)
    else:
        raise SchemaNormalizationError("MCP Tool inputSchema must be an object")
    return ToolDefinition(
        name=name.strip(),
        description=description,
        input_schema=ToolSchema(schema),
        source=source,
    )


__all__ = ["EMPTY_INPUT_SCHEMA", "SchemaNormalizationError", "normalize_mcp_tool"]
