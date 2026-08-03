from __future__ import annotations

import os
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version


class MCPMode(StrEnum):
    OFF = "off"
    OPTIONAL = "optional"
    REQUIRED = "required"


def resolve_mcp_mode(value: str | None = None) -> MCPMode:
    raw = (value if value is not None else os.getenv("SCA_MCP_MODE", "optional"))
    try:
        return MCPMode(raw.strip().lower())
    except ValueError as error:
        raise ValueError("SCA_MCP_MODE must be one of: off, optional, required") from error


def mcp_sdk_version() -> str:
    try:
        return version("mcp")
    except PackageNotFoundError:
        return "not-installed"


__all__ = ["MCPMode", "mcp_sdk_version", "resolve_mcp_mode"]
