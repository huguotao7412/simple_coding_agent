# Depth Research Agent MCP 集成改造设计

**日期**: 2026-07-03
**状态**: 已审核，待实现
**涉及文件**: `core/mcp_client.py` (新建), `core/agent.py`, `core/tools/delegate.py`, `core/tools/__init__.py`, `cli/bridge.py`

---

## 背景

Depth Research Agent 采用 Planner Agent 拆解任务 + 多个 Actor Agent 并发执行的架构。当前 Actor 的工具（read/write/edit/bash/search/list_dir/read_outline）作为本地 Python 对象直接绑定在 `ActorAgent` 中。引入 MCP（Model Context Protocol）可以：

1. **解耦工具链维护**：文件操作和 Shell 执行由社区维护的 MCP Server 提供
2. **拥抱开源生态**：未来可接入更多 MCP Server（数据库、API、浏览器等）
3. **进程级隔离**：每个 Actor 的工具执行跑在独立子进程中，故障不蔓延

## 关键决策

| 决策项 | 选择 |
|---|---|
| MCP Server 来源 | **纯开源生态** — 直接使用社区 MCP Server，不自研 |
| 运行时依赖 | **接受 Node.js** — 使用最成熟的 `@modelcontextprotocol/server-filesystem` + `server-commands` |
| 集成架构 | **方案 A: 双 Server 直连** — 每个 Actor 同时拉起 filesystem + commands 两个 MCP Server |
| MCP 客户端 | **官方 `mcp` Python SDK** — 开箱即用的 stdio 传输层和 JSON-RPC 协议处理 |

---

## 架构概览

```
Planner
  └── DelegateTool.run_one() (per Actor)
        ├── setup_worktree() → worktree_path
        ├── NEW: MCPToolProvider.start(worktree_path)
        │     ├── spawn: npx @modelcontextprotocol/server-filesystem --directory <wt>
        │     └── spawn: npx @modelcontextprotocol/server-commands
        ├── ActorAgent(tool_provider=MCPToolProvider, workspace_dir=wt)
        │     └── _execute_single_tool()
        │           └── MCPToolProvider.call_tool(name, args)  ← 不再查 self.tools_by_name
        └── finally:
              ├── NEW: MCPToolProvider.shutdown()  ← 优雅退出
              └── teardown_worktree()
```

**关键设计原则：**
- `MCPToolProvider` 与 worktree **同生命周期** — 一个 Actor 一个 Provider
- `ActorAgent` 对工具来源**无感知** — 只看到 schemas，不关心来自本地还是 MCP
- `Planner` 层**不变** — Planner 继续使用本地 `PLANNER_TOOLS`

---

## 一、MCPToolProvider 设计 (`core/mcp_client.py`)

### 1.1 类结构

```python
class MCPToolProvider:
    """管理与 MCP Server 的通信生命周期。

    每个 Actor 实例化一个 Provider，绑定到其专属 worktree。
    内部管理两个 MCP Server 子进程：
      - filesystem: 文件读写/编辑/搜索/目录列表
      - commands:   Shell 命令执行
    """

    async def start(self, worktree_path: str) -> None:
        """启动两个 MCP Server 子进程，建立 stdio 连接"""

    async def list_tools(self) -> list[dict]:
        """分别调用 tools/list，合并后转换为 OpenAI function calling 格式"""

    async def call_tool(self, name: str, args: dict) -> ToolResult:
        """路由工具调用到正确的 MCP Server，含超时熔断"""

    async def shutdown(self) -> None:
        """先发 shutdown 通知，超时则强杀进程"""
```

### 1.2 双 Server 管理

- 使用 `mcp` SDK 的 `stdio_client` 分别连接两个 Server
- 内部维护 `{server_name: ClientSession}` 映射
- 维护工具名到 Server 的路由表：`{tool_name: server_name}`

### 1.3 Schema 转换

MCP `tools/list` 返回格式：
```json
{"name": "read_file", "description": "...", "inputSchema": {"type": "object", "properties": {...}}}
```

转换为 OpenAI function calling 格式：
```json
{"type": "function", "function": {"name": "read_file", "description": "...", "parameters": {...}}}
```

两个 Server 的工具列表合并后统一返回，重复工具名以 filesystem 优先。

### 1.4 路径安全

- `server-filesystem` 启动时传入 `--directory <worktree_path>`，MCP Server 自身强制执行目录隔离
- `MCPToolProvider` 在 `call_tool` 中对路径参数做二次校验（深度防御）：绝对路径必须落在 worktree 内

### 1.5 超时熔断

- 每个 `call_tool` 调用设置 120s 超时
- 超时返回 `ToolResult.fail("底层工具服务超时")`
- 连续 3 次超时/连接错误触发熔断，后续调用直接快速失败
- 熔断状态在 Provider 生命周期内不可恢复

### 1.6 优雅退出

- `shutdown()` 先发 MCP 标准 shutdown 信号，等待 5 秒
- 超时则 `process.kill()` + `process.wait()`
- 清理流程在 `finally` 块中执行，确保不产生僵尸进程

---

## 二、ActorAgent 改造 (`core/agent.py`)

### 2.1 `__init__` 参数变更

```python
# 新签名
def __init__(self, llm_client, context_manager,
             tool_provider: MCPToolProvider | None = None,  # MCP 模式
             tools: list[BaseTool] | None = None,           # 保留作为 fallback
             workspace_dir, actor_id="", task_context="")
```

- 优先使用 `tool_provider`（MCP 模式），若为 None 则回退到 `tools`（本地模式）
- 保证渐进式迁移：可先在部分 Actor 启用 MCP，验证稳定后再全量切换

### 2.2 工具 Schema 获取

```python
# run() / run_stream() 中
if self._tool_provider:
    tool_schemas = await self._tool_provider.list_tools()
else:
    tool_schemas = [t.schema for t in self.tools_by_name.values()]
```

### 2.3 `_execute_single_tool` 路由改造

```python
async def _execute_single_tool(self, tc: dict) -> tuple[...]:
    tool_name = tc["function"]["name"]
    args = ...  # JSON 解析（不变）

    # --- MCP 路径 ---
    if self._tool_provider:
        result = await self._tool_provider.call_tool(tool_name, args)
        observation = result.content if result.success else f"ERROR: {result.error}"
        self.ctx.add_tool_result(tc["id"], observation)
        return tool_name, args, result, observation, False

    # --- 本地路径（fallback）---
    # ... 原有逻辑不变 ...
```

### 2.4 不再注入 workspace_dir

- MCP 模式下，worktree 路径已在启动 Server 时通过 `--directory` 绑定
- 工具调用不需要也不应该传 `workspace_dir` 参数

---

## 三、DelegateTool 生命周期改造 (`core/tools/delegate.py`)

### 3.1 `run_one` MCP 生命周期

```
async with semaphore:
    1. setup_worktree()
    2. MCPToolProvider.start(worktree_path)    ← NEW
    3. ActorAgent(tool_provider=..., tools=None) ← 改为 MCP 模式
    4. actor.run()
    finally:
       5. MCPToolProvider.shutdown()  ← NEW（先关 MCP）
       6. teardown_worktree()        ← 后拆 worktree
```

**时序保证：**
- `finally` 中先关 MCP 再拆 worktree — 避免 Windows 上 MCP 持有的文件句柄导致目录删除失败
- MCP 启动失败视为 Actor 失败，不进入执行循环
- shutdown 失败只记日志，不阻塞 worktree 清理

---

## 四、错误处理与中文 UI 映射

### 4.1 MCP 协议层错误映射 (`core/mcp_client.py`)

```python
MCP_ERROR_MAP = {
    "Method not found":       "工具不存在或未注册",
    "Invalid params":         "工具参数格式错误",
    "Internal error":         "底层工具服务异常",
    "Connection closed":      "工具服务连接已断开",
    "timed out":              "工具服务响应超时，请简化操作后重试",
    "directory not allowed":  "路径访问被拒绝：操作超出工作区范围",
}
```

### 4.2 熔断机制

```
连续超时/连接错误计数
  ├── 1-2 次：返回错误 Observation，Actor 可重试
  ├── 3 次：触发熔断，后续 call_tool 直接返回
  │         "工具服务已熔断，请向 Planner 报告 (CRITICAL)"
  └── 熔断状态在 Provider 生命周期内不可恢复
```

### 4.3 UI 层 (`cli/bridge.py`)

- MCP 错误已通过 `MCP_ERROR_MAP` 在 Provider 层转为中文
- `bridge.py` 无需额外改动，现有中文 UI 渲染逻辑直接生效

---

## 实施顺序

建议在独立分支上按以下顺序实施：

1. **依赖准备** — 安装 `mcp` Python SDK，安装 Node.js MCP Server
2. **`core/mcp_client.py`** — 实现 `MCPToolProvider` 类（含双 Server 管理、Schema 转换、超时熔断、优雅退出）
3. **`core/agent.py`** — 改造 `ActorAgent` 支持 MCP 模式（保留本地 fallback）
4. **`core/tools/delegate.py`** — 改造 `run_one` 管理 MCP 生命周期
5. **集成测试** — 端到端验证单个 Actor + MCP 的完整流程
6. **全量切换** — 验证稳定后，将 `ACTOR_TOOLS` 标记为废弃

---

## 测试验证要点

- [ ] 单 Actor + MCP：创建 Actor 执行简单文件读写任务，验证 MCP 通路正常
- [ ] 双 Server 并发：验证 filesystem 和 commands 两个 Server 同时工作无冲突
- [ ] 超时熔断：模拟 MCP Server 卡死，验证熔断触发和 Observation 返回
- [ ] 优雅退出：验证 Actor 完成后 MCP Server 进程被清理，无僵尸进程
- [ ] 并发 Actor：同时运行 4 个 Actor，每个带独立的 MCP Server 对，验证进程隔离
- [ ] 错误翻译：触发 `Method not found` 等 MCP 错误，验证中文错误信息输出
- [ ] 路径隔离：Actor 尝试用绝对路径访问 worktree 外的文件，验证被拒绝
- [ ] 本地 fallback：在未传入 `tool_provider` 时，验证本地工具仍然正常工作
