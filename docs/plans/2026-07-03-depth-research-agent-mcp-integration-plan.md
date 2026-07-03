# Depth Research Agent MCP 集成实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 Actor Agent 的工具层从本地 Python 对象解耦为社区 MCP Server（filesystem + bash），通过官方 `mcp` Python SDK 进行通信。

**Architecture:** 每个 Actor 启动时，`MCPToolProvider` 在 worktree 内拉起两个 Node.js MCP Server 子进程（`@modelcontextprotocol/server-filesystem` + `bash-mcp`），Actor 的工具调用通过 MCP 协议透明路由。保留本地工具 fallback 以支持渐进式迁移。

**Tech Stack:** Python 3.12, `mcp` Python SDK, Node.js, `@modelcontextprotocol/server-filesystem`, `bash-mcp`

**设计文档:** `docs/plans/2026-07-03-depth-research-agent-mcp-integration-design.md`

---

## Task 1: 环境依赖准备

**Files:**
- Modify: `pyproject.toml` 或 `requirements.txt`
- Create: `package.json` (项目根目录，仅用于声明 Node.js MCP Server 依赖)

### Step 1: 安装 Python MCP SDK

确认当前 Python 环境，安装官方 `mcp` 包：

```bash
pip install mcp
```

验证安装：
```bash
python -c "from mcp import ClientSession; from mcp.client.stdio import stdio_client; print('MCP SDK OK')"
```
Expected: `MCP SDK OK` 无 ImportError

### Step 2: 安装 Node.js MCP Server

确认 Node.js >= 18 已安装：
```bash
node --version
```
Expected: `v18.x.x` 或更高

全局安装两个 MCP Server：
```bash
npm install -g @modelcontextprotocol/server-filesystem bash-mcp
```

验证 filesystem server 可启动：
```bash
npx @modelcontextprotocol/server-filesystem --help
```
Expected: 显示帮助信息（或至少不报 module not found）

验证 bash-mcp 可启动：
```bash
npx bash-mcp --help
```
Expected: 显示帮助信息

### Step 3: 记录依赖到项目文件

更新 `requirements.txt`（如有）添加 `mcp`。创建 `package.json` 记录 Node.js 依赖。

### Step 4: Commit

```bash
git add requirements.txt package.json  # 或其他依赖文件
git commit -m "chore: add MCP Python SDK and Node.js MCP server dependencies"
```

---

## Task 2: 实现 MCPToolProvider (`core/mcp_client.py`)

**Files:**
- Create: `core/mcp_client.py`

### Step 1: 创建文件骨架

创建 `core/mcp_client.py`，包含类定义和所有方法签名：

```python
"""MCP Tool Provider — manages MCP Server lifecycles for Actor agents.

Each Actor gets its own MCPToolProvider bound to its worktree.
Two MCP Servers are spawned per Actor:
  - @modelcontextprotocol/server-filesystem  → file read/write/edit/search/list
  - bash-mcp                                  → shell command execution
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .tools.base import ToolResult

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

    Usage::

        provider = MCPToolProvider()
        await provider.start("/path/to/worktree")
        schemas = await provider.list_tools()
        result = await provider.call_tool("read_file", {"path": "src/main.py"})
        await provider.shutdown()
    """

    def __init__(self) -> None:
        self._sessions: dict[str, ClientSession] = {}
        self._server_params: list[tuple[str, StdioServerParameters]] = []
        self._tool_routing: dict[str, str] = {}   # tool_name → server_name
        self._tool_schemas: list[dict] = []        # cached OpenAI-format schemas
        self._read_streams: list[Any] = []          # for cleanup
        self._write_streams: list[Any] = []         # for cleanup
        self._processes: list[Any] = []             # subprocess references
        self._failure_count: int = 0
        self._circuit_open: bool = False
        self._worktree_path: str = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, worktree_path: str) -> None:
        """Launch both MCP Servers bound to the given worktree directory."""
        ...

    async def list_tools(self) -> list[dict]:
        """Return tool schemas in OpenAI function-calling format."""
        ...

    async def call_tool(self, name: str, args: dict) -> ToolResult:
        """Route a tool call to the correct MCP Server and return the result."""
        ...

    async def shutdown(self) -> None:
        """Gracefully terminate all MCP Server processes."""
        ...

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _translate_error(self, error_msg: str) -> str:
        """Map MCP protocol errors to Chinese UI messages."""
        ...

    def _validate_path(self, file_path: str) -> bool:
        """Check that a file path is within the bound worktree (defense in depth)."""
        ...
```

### Step 2: 实现 `start()` 方法

```python
async def start(self, worktree_path: str) -> None:
    """Launch both MCP Servers bound to the given worktree directory."""
    self._worktree_path = os.path.abspath(worktree_path)

    # Define the two MCP servers
    servers: list[tuple[str, list[str]]] = [
        (
            "filesystem",
            ["npx", "-y", "@modelcontextprotocol/server-filesystem",
             "--directory", self._worktree_path],
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

        # Open stdio transport
        transport = await stdio_client(server_params).__aenter__()
        read_stream, write_stream = transport

        # Create and initialize session
        session = ClientSession(read_stream, write_stream)
        await session.__aenter__()
        await session.initialize()

        self._sessions[server_name] = session
        logger.info("MCP Server '%s' started for worktree %s", server_name, self._worktree_path)

    # Build tool routing table
    await self._build_routing_table()
```

**注意**: `stdio_client` 返回的是一个 context manager，需要正确地管理其生命周期。在实际实现中可能需要用 `AsyncExitStack` 模式。详见 Step 2b。

### Step 2b: 使用 AsyncExitStack 管理生命周期

由于需要管理多个异步 context manager，推荐使用 `contextlib.AsyncExitStack`：

```python
from contextlib import AsyncExitStack

class MCPToolProvider:
    def __init__(self) -> None:
        self._exit_stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        ...

    async def start(self, worktree_path: str) -> None:
        self._worktree_path = os.path.abspath(worktree_path)

        servers: list[tuple[str, list[str]]] = [
            ("filesystem", ["npx", "-y", "@modelcontextprotocol/server-filesystem",
                            "--directory", self._worktree_path]),
            ("bash", ["npx", "-y", "bash-mcp"]),
        ]

        for server_name, cmd_and_args in servers:
            command = cmd_and_args[0]
            args = cmd_and_args[1:]

            server_params = StdioServerParameters(
                command=command, args=args, env=None,
            )

            # Enter the stdio_client context via exit stack
            read_stream, write_stream = await self._exit_stack.enter_async_context(
                stdio_client(server_params)
            )

            # Enter the ClientSession context via exit stack
            session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await session.initialize()

            self._sessions[server_name] = session

        await self._build_routing_table()
```

### Step 3: 实现 `_build_routing_table()` 和 `list_tools()`

```python
async def _build_routing_table(self) -> None:
    """Fetch tools from each server and build routing + schema cache."""
    all_schemas: list[dict] = []

    for server_name, session in self._sessions.items():
        try:
            response = await session.list_tools()
        except Exception as e:
            logger.error("Failed to list tools from '%s': %s", server_name, e)
            continue

        for tool in response.tools:
            # Build routing: tool_name → server_name
            self._tool_routing[tool.name] = server_name

            # Convert MCP schema → OpenAI function-calling format
            openai_schema = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema if tool.inputSchema else {
                        "type": "object",
                        "properties": {},
                    },
                },
            }
            all_schemas.append(openai_schema)

    self._tool_schemas = all_schemas
    logger.info(
        "MCP routing table built: %d tools from %d servers",
        len(all_schemas), len(self._sessions),
    )

async def list_tools(self) -> list[dict]:
    """Return cached tool schemas in OpenAI function-calling format."""
    if not self._tool_schemas:
        await self._build_routing_table()
    return self._tool_schemas
```

### Step 4: 实现 `call_tool()` 方法

```python
async def call_tool(self, name: str, args: dict) -> ToolResult:
    """Route a tool call to the correct MCP Server and return result."""
    # Circuit breaker check
    if self._circuit_open:
        return ToolResult.fail(
            "工具服务已熔断，请向 Planner 报告 (CRITICAL: MCP circuit breaker open)"
        )

    # Route lookup
    server_name = self._tool_routing.get(name)
    if server_name is None:
        return ToolResult.fail(
            f"工具不存在或未注册: '{name}'. 可用工具: {list(self._tool_routing.keys())}"
        )

    session = self._sessions.get(server_name)
    if session is None:
        return ToolResult.fail(f"工具服务 '{server_name}' 未连接")

    # Defense in depth: validate file paths for filesystem server
    if server_name == "filesystem":
        for key, value in args.items():
            if key in ("path", "source", "destination") and isinstance(value, str):
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
        text_parts = []
        for item in result.content:
            if hasattr(item, 'text'):
                text_parts.append(item.text)
        content = "\n".join(text_parts)
    else:
        content = ""

    return ToolResult.ok(content)
```

### Step 5: 实现 `_translate_error()`, `_validate_path()`, `shutdown()`

```python
def _translate_error(self, error_msg: str) -> str:
    """Map MCP protocol errors to Chinese UI messages."""
    for eng_key, chinese_msg in MCP_ERROR_MAP.items():
        if eng_key.lower() in error_msg.lower():
            return f"{chinese_msg}: {error_msg}"
    return f"底层工具服务异常: {error_msg}"

def _validate_path(self, file_path: str) -> bool:
    """Check that a file path is within the bound worktree (defense in depth).

    The MCP filesystem server already enforces --directory, this is an
    additional safety layer.
    """
    if not os.path.isabs(file_path):
        # Relative paths are resolved relative to worktree by the server
        return True
    try:
        resolved = os.path.realpath(file_path)
        wt_real = os.path.realpath(self._worktree_path)
        return resolved.startswith(wt_real + os.sep) or resolved == wt_real
    except (ValueError, OSError):
        return False

async def shutdown(self) -> None:
    """Gracefully terminate all MCP Server processes.

    Uses the AsyncExitStack to unwind contexts in reverse order:
    sessions first, then transports.
    """
    logger.info("Shutting down MCP servers for worktree %s", self._worktree_path)
    try:
        await asyncio.wait_for(self._exit_stack.aclose(), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning("MCP shutdown timed out after 5s — forcing close")
    except Exception as e:
        logger.warning("MCP shutdown error (non-fatal): %s", e)
    finally:
        self._sessions.clear()
        self._tool_routing.clear()
        self._tool_schemas.clear()
```

### Step 6: Commit

```bash
git add core/mcp_client.py
git commit -m "feat: add MCPToolProvider for dual MCP Server lifecycle management"
```

---

## Task 3: 改造 ActorAgent 支持 MCP 模式 (`core/agent.py`)

**Files:**
- Modify: `core/agent.py`

### Step 1: 修改 `__init__` 参数

修改 `ActorAgent.__init__`，新增 `tool_provider` 参数并保留 `tools` 作为 fallback：

找到现有签名（约第 125-138 行），替换为：

```python
def __init__(
    self,
    llm_client: LLMClient,
    context_manager: ContextManager,
    tools: list[BaseTool] | None = None,
    workspace_dir: str = "",
    actor_id: str = "",
    task_context: str = "",
    tool_provider: Any | None = None,  # MCPToolProvider, lazy import
):
    self.actor_id = actor_id
    self.task_context = task_context
    self.llm = llm_client
    self.workspace_dir = workspace_dir
    self._tool_provider = tool_provider

    # Local tool fallback (used when tool_provider is None)
    self.tools_by_name = {t.name: t for t in tools} if tools else {}

    self._recent_actions: deque[int] = deque(maxlen=10)
    self.ctx = context_manager
```

### Step 2: 修改 `run()` 中的 schema 获取

找到 `run()` 方法中 `tool_schemas = [t.schema for t in self.tools_by_name.values()]`（约第 257 行），替换为：

```python
# In run(), before the while loop:
if self._tool_provider is not None:
    tool_schemas = await self._tool_provider.list_tools()
else:
    tool_schemas = [t.schema for t in self.tools_by_name.values()]
```

### Step 3: 修改 `run_stream()` 中的 schema 获取

同样替换 `run_stream()` 中的 schema 获取（约第 330 行）：

```python
# In run_stream(), before the while loop:
if self._tool_provider is not None:
    tool_schemas = await self._tool_provider.list_tools()
else:
    tool_schemas = [t.schema for t in self.tools_by_name.values()]
```

### Step 4: 修改 `_execute_single_tool()` 增加 MCP 路由

找到 `_execute_single_tool()` 方法（约第 162-249 行），在工具查找部分（约第 222-246 行）之前插入 MCP 路径：

```python
# --- MCP 路径 ---
if self._tool_provider is not None:
    # workspace_dir injection NOT needed — MCP Server is already bound
    result = await self._tool_provider.call_tool(tool_name, args)
    if result.success:
        observation = result.content
    else:
        observation = f"ERROR: {result.error}"
        if result.content:
            observation += f"\nPartial output: {result.content}"

    self.ctx.add_tool_result(tc["id"], observation)
    return tool_name, args, result, observation, False

# --- 本地路径（fallback）---
```

注意：原有的 `workspace_dir` 注入逻辑（第 208-209 行）在 MCP 路径中**不需要**，因为 worktree 路径已通过 `--directory` 绑定到 filesystem server。但当 MCP 路径执行时，这段代码仍会在函数开头附近的旧位置被执行（`if tool_name in (...)`）。为了干净，在 MCP 路径 return 之前，不需要调整——因为 MCP 路径在到达原工具查找逻辑之前就已经 return 了。

**但需注意**：重复检测逻辑（约第 212-220 行）在 MCP 模式下仍然适用，因此保持在 MCP 分支之前。

### Step 5: Commit

```bash
git add core/agent.py
git commit -m "feat: add MCP tool provider support to ActorAgent with local fallback"
```

---

## Task 4: 改造 DelegateTool 管理 MCP 生命周期 (`core/tools/delegate.py`)

**Files:**
- Modify: `core/tools/delegate.py`

### Step 1: 添加 import

在文件顶部的 import 区添加：

```python
from ..mcp_client import MCPToolProvider
```

### Step 2: 修改 `run_one` 添加 MCP 生命周期

找到 `run_one` 协程中 worktree 创建后的代码（约第 130-218 行），在 `wt_path = setup_worktree(...)` 之后插入 MCP Server 启动，并修改 Actor 创建和 finally 块。

具体改动（将现有第 133-218 行替换为以下结构）：

```python
# --- 2. Create worktree ---
wt_path: str | None = None
start_time = time.monotonic()
try:
    wt_path = setup_worktree(current_workspace, tid)
    logger.info("actor_start task_id=%s worktree=%s", tid, wt_path)
except Exception as e:
    await state.update_task(tid, status="failed")
    await state.add_summary(tid, f"ERROR: worktree setup failed: {e}")
    return {
        "task_id": tid,
        "status": "failed",
        "error": f"worktree setup: {str(e)}",
    }

# --- 3. Start MCP Servers ---
tool_provider = MCPToolProvider()
try:
    await tool_provider.start(wt_path)
except Exception as e:
    logger.error("MCP startup failed for %s: %s", tid, e)
    await state.update_task(tid, status="failed")
    await state.add_summary(tid, f"ERROR: MCP Server 启动失败: {e}")
    teardown_worktree(wt_path)
    return {
        "task_id": tid,
        "status": "failed",
        "error": f"MCP startup: {str(e)}",
    }

try:
    # --- 4. Copy context files (unchanged) ---
    for fp in context_files:
        src = os.path.join(current_workspace, fp)
        dst = os.path.join(wt_path, fp)
        if os.path.isfile(src):
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
            except Exception:
                pass

    # --- 5. Build ActorAgent with MCP tool provider ---
    actor_ctx = ContextManager(
        system_prompt=ACTOR_SYSTEM_PROMPT,
        max_tokens=self._llm.max_tokens,
    )
    actor_ctx.add_user_message(injected_context)

    actor = ActorAgent(
        llm_client=self._llm,
        context_manager=actor_ctx,
        tools=None,                      # ← 不使用本地工具
        tool_provider=tool_provider,     # ← MCP 模式
        workspace_dir=wt_path,
        actor_id=tid,
        task_context=description,
    )

    # --- 6. Execute Actor ---
    trigger_prompt = "请基于上述提供的上下文和目标，开始执行你负责的子任务。"
    summary = await actor.run(trigger_prompt)
    # ... extract diff, update state (UNCHANGED) ...

finally:
    # --- 7. Cleanup: MCP first, then worktree ---
    try:
        await tool_provider.shutdown()
    except Exception:
        logger.warning("MCP shutdown error for %s", tid, exc_info=True)
    try:
        teardown_worktree(wt_path)
    except Exception:
        logger.warning("Worktree teardown error for %s: %s", tid, wt_path)
```

**关键结构变化：**
- 原有 `try → worktree + actor → finally: teardown` 的单层结构
- 变为 `try → worktree → try → MCP.start → try → actor → finally: MCP.shutdown + teardown` 的嵌套结构
- 外层 try/finally 保证 MCP shutdown 和 worktree teardown 必然执行

### Step 3: Commit

```bash
git add core/tools/delegate.py
git commit -m "feat: integrate MCP Server lifecycle into DelegateTool.run_one"
```

---

## Task 5: 集成验证

**Files:**
- Create: `tests/test_mcp_integration.py` (临时验证脚本)

### Step 1: 编写最小验证脚本

创建 `tests/test_mcp_integration.py`：

```python
"""Minimal integration test — verify MCPToolProvider can start, list tools,
and execute a call end-to-end within a temp directory.
"""

import asyncio
import os
import shutil
import tempfile

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.mcp_client import MCPToolProvider


async def test_mcp_filesystem_read_write():
    """Start MCP servers, create a file, read it back."""
    tmpdir = tempfile.mkdtemp(prefix="mcp_test_")
    try:
        # 1. Start provider
        provider = MCPToolProvider()
        await provider.start(tmpdir)

        # 2. List tools
        schemas = await provider.list_tools()
        tool_names = {s["function"]["name"] for s in schemas}
        print(f"[OK] Listed {len(schemas)} tools: {tool_names}")

        # Verify essential tools exist
        assert "read_file" in tool_names, f"read_file missing from {tool_names}"
        assert "write_file" in tool_names, f"write_file missing from {tool_names}"
        assert "run" in tool_names or "execute-bash-command" in tool_names, \
            f"bash tool missing from {tool_names}"
        print("[OK] Essential tools verified")

        # 3. Write a test file via MCP
        test_content = "Hello from MCP integration test!"
        test_path = os.path.join(tmpdir, "test.txt")
        result = await provider.call_tool("write_file", {
            "path": test_path,
            "content": test_content,
        })
        assert result.success, f"write_file failed: {result.error}"
        print(f"[OK] write_file succeeded")

        # 4. Read it back via MCP
        result = await provider.call_tool("read_file", {"path": test_path})
        assert result.success, f"read_file failed: {result.error}"
        assert test_content in result.content, \
            f"Expected '{test_content}' in read result, got: {result.content[:200]}"
        print(f"[OK] read_file returned expected content")

        # 5. Run a bash command
        result = await provider.call_tool("run", {
            "command": "echo hello_from_bash",
            "options": {"cwd": tmpdir},
        })
        assert result.success, f"bash run failed: {result.error}"
        assert "hello_from_bash" in result.content, \
            f"Expected 'hello_from_bash' in output, got: {result.content[:200]}"
        print(f"[OK] bash run succeeded")

        # 6. Shutdown
        await provider.shutdown()
        print("[OK] Shutdown completed")

        print("\n=== ALL MCP INTEGRATION TESTS PASSED ===")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(test_mcp_filesystem_read_write())
```

### Step 2: 运行验证脚本

```bash
cd E:\huguotao7412\simple_coding_agent
python tests/test_mcp_integration.py
```

Expected: 所有 6 个步骤输出 `[OK]`，最终显示 `ALL MCP INTEGRATION TESTS PASSED`

如果失败，检查：
- Node.js 和 npx 是否在 PATH 中
- MCP Server 是否成功全局安装
- 网络是否能访问 npm registry

### Step 3: 验证本地 fallback 未被破坏

运行现有的 Actor 测试（如有）：
```bash
python -c "
import asyncio
from core.agent import ActorAgent
# Create a minimal ActorAgent without tool_provider
# Verify it still works with local tools
print('Local fallback path imports OK')
"
```

### Step 4: Commit

```bash
git add tests/test_mcp_integration.py
git commit -m "test: add MCP integration verification script"
```

---

## 实施顺序总结

```
Task 1 (环境准备)
  └── Task 2 (MCPToolProvider)
        └── Task 3 (ActorAgent 改造)
              └── Task 4 (DelegateTool 改造)
                    └── Task 5 (集成验证)
```

每个 Task 独立可验证，Task 3 和 Task 4 可并行（都依赖 Task 2）。
