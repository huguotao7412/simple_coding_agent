"""MCP Tool Provider: manages MCP Server lifecycles for Actor agents.

Each Actor gets its own MCPToolProvider bound to its worktree.
Two MCP Servers are spawned per Actor:
  - @modelcontextprotocol/server-filesystem: file read/write/edit/search/list
  - bash-mcp: shell command execution
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..tools import ListDirTool, ReadOutlineTool, SearchCodebaseTool
from ..tools.base import BaseTool, ToolResult
from ..policy import ToolPolicy
from ..events import AgentEvent
from ..runs.context import RunContext

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

    Each Actor instance gets its own provider. The provider launches MCP
    subprocesses with their current working directory bound to the Actor
    worktree, fetches tool schemas, routes calls, and shuts everything down.
    """

    def __init__(
        self,
        run_context: RunContext | None = None,
        actor_id: str = "",
    ) -> None:
        self._exit_stack: AsyncExitStack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._stdio_ctxs: list[Any] = []
        self._tool_routing: dict[str, str] = {}
        self._tool_schemas: list[dict[str, Any]] = []
        local_tools: list[BaseTool] = [
            SearchCodebaseTool(),
            ReadOutlineTool(),
            ListDirTool(),
        ]
        self._local_tools = {tool.name: tool for tool in local_tools}
        self._failure_count: int = 0
        self._circuit_open: bool = False
        self._worktree_path: str = ""
        self._policy = ToolPolicy.for_role("legacy", None)
        self._run_context = run_context
        self._actor_id = actor_id

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
        self.set_policy(
            tool_policy or ToolPolicy.for_role("actor", tool_allowlist)
        )

        servers: list[tuple[str, list[str]]] = [
            (
                "filesystem",
                [
                    *_node_bin_command(
                        "mcp-server-filesystem",
                        f"@modelcontextprotocol/server-filesystem@{MCP_SERVER_FILESYSTEM_VERSION}",
                    ),
                    self._worktree_path,
                ],
            ),
            (
                "bash",
                _node_bin_command("bash-mcp", f"bash-mcp@{BASH_MCP_VERSION}"),
            ),
        ]

        for server_name, cmd_and_args in servers:
            server_params = StdioServerParameters(
                command=cmd_and_args[0],
                args=cmd_and_args[1:],
                env=None,
                cwd=self._worktree_path,
            )

            stdio_ctx = stdio_client(server_params)
            read_stream, write_stream = await stdio_ctx.__aenter__()
            self._stdio_ctxs.append(stdio_ctx)

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

        await self._build_routing_table()

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return cached tool schemas in OpenAI function-calling format."""
        if not self._tool_schemas:
            await self._build_routing_table()
        if self._policy.allowed_tools is None:
            return self._tool_schemas
        return [
            tool
            for tool in self._tool_schemas
            if tool.get("function", {}).get("name") in self._policy.allowed_tools
        ]

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Route a tool call to the correct MCP server and return the result."""
        decision = self._policy.authorize(name)
        if not decision.allowed:
            if self._run_context is not None:
                await self._run_context.emit(AgentEvent(
                    type="policy_denied",
                    content=decision.reason,
                    tool_name=name,
                    actor_id=self._actor_id,
                    task_id=self._actor_id,
                ))
            return ToolResult.fail(decision.reason)

        if self._circuit_open:
            return ToolResult.fail(
                "Tool service circuit breaker is open; report this to the Planner "
                "(CRITICAL: MCP circuit breaker open)"
            )

        local_tool = self._local_tools.get(name)
        if local_tool is not None:
            try:
                args["workspace_dir"] = self._worktree_path
                return await local_tool.execute(**args)
            except Exception as e:
                return ToolResult.fail(f"Internal local tool error: {e}")

        server_name = self._tool_routing.get(name)
        if server_name is None:
            return ToolResult.fail(
                f"Tool is not registered: '{name}'. "
                f"Available tools: {list(self._tool_routing.keys())}"
            )

        session = self._sessions.get(server_name)
        if session is None:
            return ToolResult.fail(f"Tool service '{server_name}' is not connected")

        if server_name == "filesystem":
            for key, value in args.items():
                if key in ("path", "paths", "source", "destination") and isinstance(value, str):
                    if not self._validate_path(value):
                        return ToolResult.fail(
                            f"Path access denied: '{value}' escapes the workspace"
                        )
        elif server_name == "bash":
            command = _extract_shell_command(args)
            if command and is_destructive_shell_command(command):
                return ToolResult.fail(
                    "Destructive shell command blocked by Actor safety policy. "
                    "Ask the Planner/user for an explicit safer workflow instead."
                )

        try:
            result = await asyncio.wait_for(
                session.call_tool(name, args),
                timeout=DEFAULT_TOOL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            self._record_failure()
            return ToolResult.fail(self._translate_error("timed out"))
        except Exception as e:
            self._record_failure()
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
        except BaseException:
            logger.debug("MCP session close: swallowed cleanup error", exc_info=True)

        for stdio_ctx in reversed(self._stdio_ctxs):
            try:
                await asyncio.wait_for(stdio_ctx.__aexit__(None, None, None), timeout=3.0)
            except BaseException:
                logger.debug("MCP stdio close: swallowed cleanup error", exc_info=True)

        self._stdio_ctxs.clear()
        self._sessions.clear()
        self._tool_routing.clear()
        self._tool_schemas.clear()
        logger.info("MCP shutdown complete for worktree %s", self._worktree_path)

    async def _build_routing_table(self) -> None:
        """Fetch tools from connected servers and build routing plus schema cache."""
        all_schemas: list[dict[str, Any]] = [
            tool.schema for tool in self._local_tools.values()
        ]

        for server_name, session in self._sessions.items():
            try:
                response = await session.list_tools()
            except Exception as e:
                logger.error("Failed to list tools from '%s': %s", server_name, e)
                continue

            for tool in response.tools:
                if tool.name not in self._tool_routing:
                    self._tool_routing[tool.name] = server_name
                else:
                    logger.debug(
                        "Tool '%s' from '%s' skipped; already registered by another server",
                        tool.name,
                        server_name,
                    )
                    continue

                all_schemas.append({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                    },
                })

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
    extension = ".cmd" if sys.platform == "win32" else ""
    local_binary = PACKAGE_ROOT / "node_modules" / ".bin" / f"{binary_name}{extension}"
    if local_binary.exists():
        return [str(local_binary)]
    return ["npx", "--no-install", package_spec]


def _extract_shell_command(args: dict[str, Any]) -> str:
    for key in ("command", "cmd", "script"):
        value = args.get(key)
        if isinstance(value, str):
            return value
    return ""


def is_destructive_shell_command(command: str) -> bool:
    normalized = command.lower().strip()
    return any(re.search(pattern, normalized) for pattern in DESTRUCTIVE_COMMAND_PATTERNS)
