"""MCP Tool Provider: manages MCP Server lifecycles for Actor agents.

Each Actor gets its own MCPToolProvider bound to its worktree. The provider
always exposes Python local baseline tools for read/search/list/edit/write/run.
MCP servers are optional enhancements when their Node binaries are available.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..adapters.mcp import (
    MCPMode,
    ProviderDiagnostic,
    ProviderHealth,
    SchemaNormalizationError,
    ToolDefinition,
    mcp_sdk_version,
    normalize_mcp_tool,
    resolve_mcp_mode,
)
from ..tools import (
    EditFileTool,
    ListDirTool,
    ReadOutlineTool,
    ReadTool,
    SearchCodebaseTool,
    WriteFileTool,
)
from ..tools.base import BaseTool, ToolResult
from ..policy import ToolPolicy
from ..events import AgentEvent
from ..runs.context import RunContext
from ..sandbox.contracts import SandboxBackend
from ..sandbox.factory import create_sandbox_backend
from ..tools.sandbox_run import SandboxRunTool
from ..security.redaction import redact_text, sanitized_subprocess_environment
from .managed_runtime import managed_binary

logger = logging.getLogger(__name__)

MCP_ERROR_MAP: dict[str, str] = {
    "Method not found": "Tool is not registered",
    "Invalid params": "Tool parameters are invalid",
    "Internal error": "Underlying tool service failed",
    "Connection closed": "Tool service connection closed",
    "timed out": "Tool service timed out; simplify the operation and retry",
    "directory not allowed": "Path access denied; operation escapes the workspace",
}

DEFAULT_TOOL_TIMEOUT = 120
MAX_CONSECUTIVE_FAILURES = 3
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
MCP_SERVER_FILESYSTEM_VERSION = "2026.1.14"
BASH_MCP_VERSION = "1.1.0"
DESTRUCTIVE_COMMAND_PATTERNS = [
    r"\brm\s+(?:-[^\s]*[rf][^\s]*|-[^\s]*[fr][^\s]*)\b",
    r"\brmdir\s+(?:/s|-[^\s]*p[^\s]*)\b",
    r"\bdel\s+(?:/s|/q|/f)\b",
    r"\bremove-item\b.*(?:\s-recurse\b|\s-r\b)",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\b.*(?:-[^\s]*f[^\s]*d|-[^\s]*d[^\s]*f)\b",
    r"\bformat\s+[a-z]:",
    r"\bshutdown\b",
]


class MCPToolProvider:
    """Manage MCP server lifecycles and route tool calls.

    Each Actor instance gets its own provider. Local baseline tools are always
    available; MCP subprocesses are launched only when their commands exist.
    """

    def __init__(
        self,
        run_context: RunContext | None = None,
        actor_id: str = "",
        sandbox_backend: SandboxBackend | None = None,
        mcp_mode: MCPMode | str | None = None,
    ) -> None:
        self._exit_stack: AsyncExitStack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._stderr_handles: list[Any] = []
        self._tool_routing: dict[str, str] = {}
        self._remote_tools: dict[str, ToolDefinition] = {}
        self._tool_schemas: list[dict[str, Any]] = []
        local_tools: list[BaseTool] = [
            SearchCodebaseTool(),
            ReadTool(),
            ReadOutlineTool(),
            ListDirTool(),
            EditFileTool(),
            WriteFileTool(),
        ]
        self._local_tools = {tool.name: tool for tool in local_tools}
        self._failure_count: int = 0
        self._circuit_open: bool = False
        self._worktree_path: str = ""
        self._policy = ToolPolicy.for_role("legacy", None)
        self._run_context = run_context
        self._actor_id = actor_id
        self._sandbox_backend = sandbox_backend or create_sandbox_backend()
        self._install_run_tool()
        self._mode = MCPMode.OPTIONAL
        self._mode_override = mcp_mode
        self._started = False
        self._diagnostics: list[ProviderDiagnostic] = []

    def configure_sandbox(self, backend: SandboxBackend) -> None:
        if self._sessions:
            raise RuntimeError("sandbox backend must be configured before MCP startup")
        self._sandbox_backend = backend
        self._install_run_tool()

    def _install_run_tool(self) -> None:
        self._local_tools["run"] = SandboxRunTool(
            self._sandbox_backend,
            run_context=self._run_context,
            actor_id=self._actor_id,
        )

    def set_policy(self, policy: ToolPolicy) -> None:
        """Set the execution policy used for both schemas and dispatch."""
        self._policy = policy

    async def start(
        self,
        worktree_path: str,
        tool_allowlist: set[str] | None = None,
        tool_policy: ToolPolicy | None = None,
    ) -> None:
        """Launch MCP servers bound to the given worktree directory."""
        self._worktree_path = os.path.abspath(worktree_path)
        self._mode = resolve_mcp_mode(
            self._mode_override.value
            if isinstance(self._mode_override, MCPMode)
            else self._mode_override
        )
        self.set_policy(
            tool_policy or ToolPolicy.for_role("actor", tool_allowlist)
        )

        await self._sandbox_backend.ensure_available()
        self._commit_local_catalog()
        self._assert_baseline_tools_available()
        self._started = True
        if self._mode is MCPMode.OFF:
            return

        filesystem_command = _node_bin_command(
            "mcp-server-filesystem",
            f"@modelcontextprotocol/server-filesystem@{MCP_SERVER_FILESYSTEM_VERSION}",
        )
        servers: list[tuple[str, list[str]]] = [(
            "filesystem",
            [*filesystem_command, self._worktree_path] if filesystem_command else [],
        )]
        if self._sandbox_backend is None or not self._sandbox_backend.isolated:
            servers.append((
                "bash",
                _node_bin_command("bash-mcp", f"bash-mcp@{BASH_MCP_VERSION}"),
            ))

        for server_name, cmd_and_args in servers:
            if not cmd_and_args or not _command_available(cmd_and_args[0]):
                await self._record_provider_diagnostic(
                    server_name,
                    "spawn",
                    FileNotFoundError("MCP command is not installed or runtime download is disabled"),
                )
                continue
            server_params = StdioServerParameters(
                command=cmd_and_args[0],
                args=cmd_and_args[1:],
                env=sanitized_subprocess_environment(),
                cwd=self._worktree_path,
            )

            try:
                if logger.isEnabledFor(logging.DEBUG):
                    stdio_ctx = stdio_client(server_params)
                else:
                    errlog = open(os.devnull, "w", encoding="utf-8")
                    self._stderr_handles.append(errlog)
                    stdio_ctx = stdio_client(server_params, errlog=errlog)
                read_stream, write_stream = await self._exit_stack.enter_async_context(
                    stdio_ctx
                )

                session = await self._exit_stack.enter_async_context(
                    ClientSession(read_stream, write_stream)
                )
                await session.initialize()

                self._sessions[server_name] = session
                logger.info(
                    "MCP server '%s' started for worktree %s",
                    server_name,
                    self._worktree_path,
                )
            except Exception as error:
                await self._record_provider_diagnostic(server_name, "initialize", error)

        await self._build_routing_table()
        if self._mode is MCPMode.REQUIRED and not self._sessions:
            raise RuntimeError("MCP mode is required but no MCP server became healthy")

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return cached tool schemas in OpenAI function-calling format."""
        if not self._tool_schemas:
            await self._build_routing_table()
        return [
            tool
            for tool in self._tool_schemas
            if tool.get("function", {}).get("name") in self._policy.allowed_tools
        ]

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Route a tool call to the correct MCP server and return the result."""
        args = dict(args)
        local_tool = self._local_tools.get(name)
        if local_tool is not None:
            args["workspace_dir"] = self._worktree_path
            try:
                return await local_tool.execute(**args)
            except Exception as e:
                return ToolResult.fail(f"Internal local tool error: {e}")

        if self._circuit_open:
            return ToolResult.fail("Remote MCP circuit breaker is open")

        server_name = self._tool_routing.get(name)
        if server_name is None:
            return ToolResult.fail(
                f"Tool is not registered: '{name}'. "
                f"Available tools: {list(self._tool_routing.keys())}"
            )

        session = self._sessions.get(server_name)
        if session is None:
            return ToolResult.fail(f"Tool service '{server_name}' is not connected")

        definition = self._remote_tools.get(name)
        if definition is None:
            return ToolResult.fail(f"Tool schema is unavailable: '{name}'")
        args = self._filter_remote_arguments(definition, args)

        try:
            result = await asyncio.wait_for(
                session.call_tool(name, args),
                timeout=DEFAULT_TOOL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            self._record_failure()
            await self._record_provider_diagnostic(
                server_name, "call", asyncio.TimeoutError("timed out")
            )
            return ToolResult.fail(self._translate_error("timed out"))
        except Exception as e:
            self._record_failure()
            await self._record_provider_diagnostic(server_name, "call", e)
            return ToolResult.fail(self._translate_error(str(e)))

        self._failure_count = 0

        text_parts: list[str] = []
        for item in result.content or []:
            if hasattr(item, "text"):
                text_parts.append(item.text)
        return ToolResult.ok("\n".join(text_parts))

    async def shutdown(self) -> None:
        """Gracefully terminate all MCP server sessions and transports."""
        logger.info("Shutting down MCP servers for worktree %s", self._worktree_path)

        try:
            await asyncio.wait_for(self._exit_stack.aclose(), timeout=3.0)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._record_provider_diagnostic("all", "shutdown", error)
            logger.debug("MCP session close failed", exc_info=True)

        for handle in self._stderr_handles:
            try:
                handle.close()
            except OSError:
                pass

        self._stderr_handles.clear()
        self._sessions.clear()
        self._tool_routing.clear()
        self._remote_tools.clear()
        self._tool_schemas.clear()
        self._started = False
        logger.info("MCP shutdown complete for worktree %s", self._worktree_path)

    async def _build_routing_table(self) -> None:
        """Fetch tools from connected servers and build routing plus schema cache."""
        all_schemas = list(self._tool_schemas) or [
            tool.schema for tool in self._local_tools.values()
        ]

        for server_name, session in self._sessions.items():
            try:
                response = await session.list_tools()
            except Exception as e:
                await self._record_provider_diagnostic(server_name, "list_tools", e)
                continue

            for tool in response.tools:
                try:
                    definition = normalize_mcp_tool(tool, source=server_name)
                except SchemaNormalizationError as error:
                    await self._record_provider_diagnostic(
                        server_name, "normalize_schema", error
                    )
                    continue
                if definition.name in self._local_tools:
                    logger.debug(
                        "MCP tool '%s' from '%s' skipped; local adapter owns the name",
                        definition.name,
                        server_name,
                    )
                    continue
                if definition.name not in self._tool_routing:
                    self._tool_routing[definition.name] = server_name
                    self._remote_tools[definition.name] = definition
                else:
                    logger.debug(
                        "Tool '%s' from '%s' skipped; already registered by another server",
                        definition.name,
                        server_name,
                    )
                    continue

                all_schemas.append(definition.openai_schema())

        self._tool_schemas = all_schemas
        logger.info(
            "MCP routing table built: %d tools from %d servers",
            len(all_schemas),
            len(self._sessions),
        )

    def _record_failure(self) -> None:
        self._failure_count += 1
        if self._failure_count >= MAX_CONSECUTIVE_FAILURES:
            self._circuit_open = True

    def _commit_local_catalog(self) -> None:
        self._tool_schemas = [tool.schema for tool in self._local_tools.values()]

    async def _record_provider_diagnostic(
        self,
        server_name: str,
        phase: str,
        error: BaseException,
    ) -> None:
        summary = str(redact_text(str(error)).value)
        diagnostic = ProviderDiagnostic(
            provider=server_name,
            phase=phase,  # type: ignore[arg-type]
            exception_type=type(error).__name__,
            error_summary=summary,
            mcp_sdk_version=mcp_sdk_version(),
            degraded_to_local=self._mode is not MCPMode.REQUIRED,
            affects_core_capability=False,
        )
        self._diagnostics.append(diagnostic)
        message = (
            f"Optional MCP server '{server_name}' unavailable; continuing with "
            f"local baseline tools. Phase={phase}; cause={summary}"
        )
        logger.warning(message)
        if self._run_context is not None:
            await self._run_context.emit(AgentEvent(
                type="tool_provider_warning",
                content=message,
                actor_id=self._actor_id,
                task_id=self._actor_id,
            ))

    def health(self) -> ProviderHealth:
        local_ok = not self._started or self._baseline_tools_available()
        if not self._started:
            status = "not_started"
        elif not local_ok:
            status = "unhealthy"
        elif self._diagnostics:
            status = "healthy_degraded"
        else:
            status = "healthy"
        return ProviderHealth(
            status=status,  # type: ignore[arg-type]
            local_tools_available=local_ok,
            connected_servers=tuple(sorted(self._sessions)),
            diagnostics=tuple(self._diagnostics),
        )

    def _baseline_tools_available(self) -> bool:
        available = {
            schema.get("function", {}).get("name") for schema in self._tool_schemas
        }
        return self._required_baseline_tools() <= available

    def _assert_baseline_tools_available(self) -> None:
        required = self._required_baseline_tools()
        available = {schema["function"]["name"] for schema in self._tool_schemas}
        missing = sorted(required - available)
        if missing:
            raise RuntimeError(
                "Critical local tool provider failure; missing baseline tools: "
                + ", ".join(missing)
            )

    @staticmethod
    def _required_baseline_tools() -> set[str]:
        return {
            "list_dir",
            "search_codebase",
            "read",
            "read_outline",
            "edit_file",
            "write_file",
            "run",
        }

    @staticmethod
    def _filter_remote_arguments(
        definition: ToolDefinition,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        properties = definition.input_schema.value.get("properties", {})
        if not isinstance(properties, dict):
            return {}
        return {key: value for key, value in arguments.items() if key in properties}

    def _translate_error(self, error_msg: str) -> str:
        for eng_key, readable_msg in MCP_ERROR_MAP.items():
            if eng_key.lower() in error_msg.lower():
                return f"{readable_msg}: {error_msg}"
        return f"Underlying tool service failed: {error_msg}"

    def _validate_path(self, file_path: str) -> bool:
        """Check that an absolute file path stays within the bound worktree."""
        if not os.path.isabs(file_path):
            return True
        try:
            resolved = os.path.realpath(file_path)
            worktree_real = os.path.realpath(self._worktree_path)
            return resolved.startswith(worktree_real + os.sep) or resolved == worktree_real
        except (ValueError, OSError):
            return False


def _node_bin_command(binary_name: str, package_spec: str) -> list[str]:
    managed = managed_binary(binary_name)
    if managed is not None:
        return [str(managed)]
    extension = ".cmd" if sys.platform == "win32" else ""
    local_binary = PACKAGE_ROOT / "node_modules" / ".bin" / f"{binary_name}{extension}"
    if local_binary.exists():
        return [str(local_binary)]
    # Wheel/pipx installs must not perform an implicit network install on every
    # Actor start. A pinned runtime download is available only when opted in.
    if os.getenv("SCA_MCP_ALLOW_RUNTIME_INSTALL", "").strip().lower() in {
        "1", "true", "yes"
    }:
        return ["npx", "--yes", package_spec]
    return []


def _command_available(command: str) -> bool:
    if os.path.isabs(command) or os.sep in command or (os.altsep and os.altsep in command):
        return os.path.exists(command)
    return shutil.which(command) is not None


def _extract_shell_command(args: dict[str, Any]) -> str:
    for key in ("command", "cmd", "script"):
        value = args.get(key)
        if isinstance(value, str):
            return value
    return ""


def is_destructive_shell_command(command: str) -> bool:
    normalized = command.lower().strip()
    return any(re.search(pattern, normalized) for pattern in DESTRUCTIVE_COMMAND_PATTERNS)
