"""Compatibility exports for MCP adapter consumers.

The stable tool and provider-health models live in the domain layer so ports
never depend on an adapter. Third-party SDK objects are still normalized only
inside this adapter package.
"""

from ...domain.tools import (
    ProviderDiagnostic,
    ProviderHealth,
    ProviderPhase,
    ToolDefinition,
    ToolSchema,
)

__all__ = ["ProviderDiagnostic", "ProviderHealth", "ProviderPhase", "ToolDefinition", "ToolSchema"]
