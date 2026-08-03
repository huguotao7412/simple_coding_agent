"""Stable application boundary for the optional Python MCP SDK."""

from .compatibility import MCPMode, mcp_sdk_version, resolve_mcp_mode
from .models import ProviderDiagnostic, ProviderHealth, ToolDefinition, ToolSchema
from .schema_adapter import SchemaNormalizationError, normalize_mcp_tool

__all__ = [
    "MCPMode",
    "ProviderDiagnostic",
    "ProviderHealth",
    "SchemaNormalizationError",
    "ToolDefinition",
    "ToolSchema",
    "mcp_sdk_version",
    "normalize_mcp_tool",
    "resolve_mcp_mode",
]
