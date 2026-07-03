"""MCP (Model Context Protocol) integration package.

Provides the MCPToolProvider class for managing MCP Server lifecycles
and routing tool calls from Actor agents to community MCP Servers.

Future expansion:
  - Additional MCP Server connectors (databases, APIs, browsers, etc.)
  - MCP resource/prompt support
  - Server health monitoring and auto-restart
"""

from .client import MCPToolProvider, MCP_ERROR_MAP

__all__ = ["MCPToolProvider", "MCP_ERROR_MAP"]
