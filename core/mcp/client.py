"""MCP Tool Provider — manages MCP Server lifecycles for Actor agents.

Each Actor gets its own MCPToolProvider bound to its worktree.
Two MCP Servers are spawned per Actor:
  - @modelcontextprotocol/server-filesystem  → file read/write/edit/search/list
  - bash-mcp                                  → shell command execution

Usage::

    from core.mcp import MCPToolProvider

    provider = MCPToolProvider()
    await provider.start("/path/to/worktree")
    schemas = await provider.list_tools()
    result = await provider.call_tool("read_file", {"path": "src/main.py"})
    await provider.shutdown()
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..tools.base import ToolResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP protocol-level error → Chinese UI mapping
# ---------------------------------------------------------------------------
MCP_ERROR_MAP: dict[str, str] = {
    "Method not found":       "工具不存在或未注册",
    "Invalid params":         "工具参数格式错误",
    "Internal error":         "底层工具服务异常",
    "Connection closed":      "工具服务连接已断开",
    "timed out":              "工具服务响应超时，请简化操作后重试",
    "directory not allowed":  "路径访问被拒绝：操作超出工作区范围",
}

# Per-tool timeout in seconds
DEFAULT_TOOL_TIMEOUT = 120

# Circuit breaker: max consecutive failures before fast-fail
MAX_CONSECUTIVE_FAILURES = 3


class MCPToolProvider:
    """Manages MCP Server lifecycles and routes tool calls.

    Each Actor instance gets its own MCPToolProvider. The provider launches
    two MCP Server subprocesses bound to the Actor's worktree, fetches tool
    schemas, routes tool calls to the correct server, and handles graceful
    shutdown.

    Circuit breaker: after MAX_CONSECUTIVE_FAILURES consecutive timeouts or
    connection errors, the provider enters "open" state and all subsequent
    calls fast-fail until the provider is shut down.
    """

    def __init__(self) -> None:
        self._exit_stack: AsyncExitStack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._stdio_ctxs: list[Any] = []          # stdio_client async generators for manual cleanup
        self._tool_routing: dict[str, str] = {}   # tool_name → server_name
        self._tool_schemas: list[dict] = []        # cached OpenAI-format schemas
        self._failure_count: int = 0
        self._circuit_open: bool = False
        self._worktree_path: str = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, worktree_path: str) -> None:
        """Launch both MCP Servers bound to the given worktree directory.

        Spawns two Node.js subprocesses via npx:
          - @modelcontextprotocol/server-filesystem <worktree_path>
          - bash-mcp

        Each server is connected via stdio transport and initialized with
        the MCP handshake. Tool schemas are fetched and cached.
        """
        self._worktree_path = os.path.abspath(worktree_path)

        # filesystem server takes the allowed directory as a positional arg
        servers: list[tuple[str, list[str]]] = [
            (
                "filesystem",
                ["npx", "-y", "@modelcontextprotocol/server-filesystem",
                 self._worktree_path],
            ),
            (
                "bash",
                ["npx", "-y", "bash-mcp"],
            ),
        ]

        for server_name, cmd_and_args in servers:
            command = cmd_and_args[0]
            args = cmd_and_args[1:]

            server_params = StdioServerParameters(
                command=command,
                args=args,
                env=None,
            )

            # Manually enter stdio_client to avoid AsyncExitStack task-scope issue
            # (stdio_client uses anyio task groups internally)
            stdio_ctx = stdio_client(server_params)
            read_stream, write_stream = await stdio_ctx.__aenter__()
            self._stdio_ctxs.append(stdio_ctx)

            # Enter session via exit stack (safe – ClientSession doesn't use task groups)
            session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()

            self._sessions[server_name] = session
            logger.info(
                "MCP Server '%s' started for worktree %s", server_name, self._worktree_path
            )

        # Build routing table and cache tool schemas
        await self._build_routing_table()

    async def list_tools(self) -> list[dict]:
        """Return cached tool schemas in OpenAI function-calling format.

        Each schema looks like::

            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read the complete contents of a file...",
                    "parameters": { ... JSON Schema ... },
                },
            }
        """
        if not self._tool_schemas:
            await self._build_routing_table()
        return self._tool_schemas

    async def call_tool(self, name: str, args: dict) -> ToolResult:
        """Route a tool call to the correct MCP Server and return the result.

        Features:
          - Circuit breaker: fast-fails after consecutive failures exceed threshold
          - Per-call timeout (DEFAULT_TOOL_TIMEOUT seconds)
          - Defense-in-depth path validation for filesystem tools
          - Chinese error message translation
        """
        # Circuit breaker check
        if self._circuit_open:
            return ToolResult.fail(
                "工具服务已熔断，请向 Planner 报告 "
                "(CRITICAL: MCP circuit breaker open)"
            )

        # Route lookup
        server_name = self._tool_routing.get(name)
        if server_name is None:
            return ToolResult.fail(
                f"工具不存在或未注册: '{name}'. "
                f"可用工具: {list(self._tool_routing.keys())}"
            )

        session = self._sessions.get(server_name)
        if session is None:
            return ToolResult.fail(f"工具服务 '{server_name}' 未连接")

        # Defense in depth: validate file paths for filesystem server
        if server_name == "filesystem":
            for key, value in args.items():
                if key in ("path", "paths", "source", "destination") and isinstance(value, str):
                    if not self._validate_path(value):
                        return ToolResult.fail(
                            f"路径访问被拒绝：'{value}' 超出工作区范围"
                        )

        # Execute with timeout
        try:
            result = await asyncio.wait_for(
                session.call_tool(name, args),
                timeout=DEFAULT_TOOL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            self._failure_count += 1
            if self._failure_count >= MAX_CONSECUTIVE_FAILURES:
                self._circuit_open = True
            return ToolResult.fail(self._translate_error("timed out"))
        except Exception as e:
            self._failure_count += 1
            error_str = str(e)
            if self._failure_count >= MAX_CONSECUTIVE_FAILURES:
                self._circuit_open = True
            return ToolResult.fail(self._translate_error(error_str))

        # Reset failure count on success
        self._failure_count = 0

        # Extract text content from MCP result
        if result.content and len(result.content) > 0:
            text_parts: list[str] = []
            for item in result.content:
                if hasattr(item, 'text'):
                    text_parts.append(item.text)
            content = "\n".join(text_parts)
        else:
            content = ""

        return ToolResult.ok(content)

    async def shutdown(self) -> None:
        """Gracefully terminate all MCP Server processes.

        Attempts clean shutdown via context managers first. Falls back to
        forceful cancellation if anyio task-scope errors occur (a known
        limitation of the MCP Python SDK's anyio usage). Subprocesses are
        terminated via process.kill() as a final safety net.
        """
        logger.info("Shutting down MCP servers for worktree %s", self._worktree_path)

        # Close sessions (via exit stack)
        try:
            await asyncio.wait_for(self._exit_stack.aclose(), timeout=3.0)
        except BaseException:
            # anyio task-scope RuntimeError / CancelledError are expected here
            # (MCP Python SDK issues — sessions use anyio task groups internally)
            logger.debug("MCP session close: swallowed expected cleanup error", exc_info=True)

        # Close stdio transports (manual — they use anyio task groups)
        for stdio_ctx in reversed(self._stdio_ctxs):
            try:
                await asyncio.wait_for(stdio_ctx.__aexit__(None, None, None), timeout=3.0)
            except BaseException:
                logger.debug("MCP stdio close: swallowed expected cleanup error", exc_info=True)

        self._stdio_ctxs.clear()
        self._sessions.clear()
        self._tool_routing.clear()
        self._tool_schemas.clear()
        logger.info("MCP shutdown complete for worktree %s", self._worktree_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _build_routing_table(self) -> None:
        """Fetch tools from each connected server and build routing + schema cache."""
        all_schemas: list[dict] = []

        for server_name, session in self._sessions.items():
            try:
                response = await session.list_tools()
            except Exception as e:
                logger.error("Failed to list tools from '%s': %s", server_name, e)
                continue

            for tool in response.tools:
                # Build routing: tool_name → server_name
                # If a tool name appears in both servers, the first one wins
                if tool.name not in self._tool_routing:
                    self._tool_routing[tool.name] = server_name
                else:
                    logger.debug(
                        "Tool '%s' from '%s' skipped — already registered by another server",
                        tool.name, server_name,
                    )
                    continue

                # Convert MCP schema → OpenAI function-calling format
                openai_schema = {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": (
                            tool.inputSchema
                            if tool.inputSchema
                            else {"type": "object", "properties": {}}
                        ),
                    },
                }
                all_schemas.append(openai_schema)

        self._tool_schemas = all_schemas
        logger.info(
            "MCP routing table built: %d tools from %d servers",
            len(all_schemas), len(self._sessions),
        )

    def _translate_error(self, error_msg: str) -> str:
        """Map MCP protocol errors to Chinese UI messages.

        Iterates over MCP_ERROR_MAP entries. If any English key is found as a
        substring of the error message, prepends the Chinese translation.
        Falls back to a generic Chinese prefix if no match.
        """
        for eng_key, chinese_msg in MCP_ERROR_MAP.items():
            if eng_key.lower() in error_msg.lower():
                return f"{chinese_msg}: {error_msg}"
        return f"底层工具服务异常: {error_msg}"

    def _validate_path(self, file_path: str) -> bool:
        """Check that a file path is within the bound worktree.

        The MCP filesystem server already enforces --directory restrictions.
        This is an additional defense-in-depth layer.
        """
        if not os.path.isabs(file_path):
            # Relative paths are resolved relative to worktree by the MCP server
            return True
        try:
            resolved = os.path.realpath(file_path)
            wt_real = os.path.realpath(self._worktree_path)
            return resolved.startswith(wt_real + os.sep) or resolved == wt_real
        except (ValueError, OSError):
            return False
