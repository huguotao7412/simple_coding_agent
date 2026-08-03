from __future__ import annotations

import os
import shutil
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, metadata, version
from pathlib import Path

from core.adapters.mcp import mcp_sdk_version, resolve_mcp_mode
from core.config import user_config_path
from core.mcp.client import MCPToolProvider
from core.mcp.managed_runtime import managed_runtime_status
from core.sandbox.config import load_sandbox_config


def _distribution_version() -> str:
    try:
        return version("simple-coding-agent")
    except PackageNotFoundError:
        return "source-checkout"


def _mcp_requirement() -> str:
    project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if project_file.is_file():
        try:
            project = tomllib.loads(project_file.read_text(encoding="utf-8"))
            dependencies = project.get("project", {}).get("dependencies", [])
            requirement = next(
                (str(item) for item in dependencies if str(item).lower().startswith("mcp")),
                "",
            )
            if requirement:
                return requirement
        except (OSError, tomllib.TOMLDecodeError, AttributeError):
            pass
    try:
        requirements = metadata("simple-coding-agent").get_all("Requires-Dist") or []
    except PackageNotFoundError:
        requirements = []
    return next((item for item in requirements if item.lower().startswith("mcp")), "mcp>=1.28,<2")


async def run_doctor() -> int:
    """Read-only environment diagnostics; never starts a model or MCP process."""
    executable = shutil.which("sca") or sys.argv[0]
    source_root = Path(__file__).resolve().parents[1]
    install_style = "editable/source" if (source_root / "pyproject.toml").is_file() else "wheel/pipx"
    sandbox = load_sandbox_config()
    provider = MCPToolProvider(mcp_mode="off")
    schemas = await provider.list_tools()
    local_names = sorted(
        str(schema.get("function", {}).get("name", ""))
        for schema in schemas
        if isinstance(schema, dict)
    )
    required = sorted(provider._required_baseline_tools())
    missing = sorted(set(required) - set(local_names))
    node = shutil.which("node")
    npm = shutil.which("npm")
    npx = shutil.which("npx")
    managed = managed_runtime_status()

    print(f"SCA executable: {executable}")
    print(f"SCA version: {_distribution_version()}")
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    print(f"Install: {install_style} ({source_root})")
    print(f"Supported MCP: {_mcp_requirement()}")
    print(f"Installed MCP SDK: {mcp_sdk_version()}")
    print(f"Node: {node or 'unavailable'}")
    print(f"npm: {npm or 'unavailable'}")
    print(f"npx: {npx or 'unavailable'}")
    print(f"Managed MCP root: {managed.root}")
    print(f"Managed MCP status: {'healthy' if managed.healthy else managed.detail}")
    for name in ("mcp-server-filesystem", "bash-mcp"):
        print(f"{name}: {managed.binaries.get(name, 'not installed')}")
    print(f"MCP mode: {resolve_mcp_mode().value}")
    print("Runtime MCP install: " + (
        "enabled" if os.getenv("SCA_MCP_ALLOW_RUNTIME_INSTALL", "").lower() in {"1", "true", "yes"}
        else "disabled"
    ))
    print("Local baseline tools: " + ("healthy" if not missing else "missing " + ", ".join(missing)))
    print(f"Sandbox: {sandbox.mode.value}")
    print(f"User config: {user_config_path()}")
    print("Optional MCP servers: not started (doctor is read-only)")
    return 0 if not missing else 1


__all__ = ["run_doctor"]
