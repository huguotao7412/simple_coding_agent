# Simple Coding Agent

Simple Coding Agent 是一个 **CLI 优先的本地 Coding Agent Runtime**。它的重点不是做一个花哨的聊天界面，而是让程序员能在本地仓库里用一个可审计、可追踪、可验证的 agent 辅助完成工程任务。

Web 端仍然保留，但目前定位为实验性可视化入口，不是核心产品面。

## 项目定位

这个项目适合作为简历项目展示以下能力：

- 共享 ReAct Runtime，而不是在 Planner 和 Actor 里复制两套循环。
- 透明事件流：思考输出、工具调用、工具结果、错误、token 统计、任务状态更新。
- 集中化 tool-call JSON 解析，并能从格式错误中恢复。
- 最大步数限制、重复动作熔断等运行时安全控制。
- Planner/Actor 架构与角色化工具权限。
- MCP 文件与 Shell 工具集成。
- 使用 git worktree 隔离 Actor 子任务。
- 通过 deterministic tests 验证 runtime 行为。

当前目标是打磨一个可靠的本地 agent 核心，而不是宣称它已经是生产级全自动软件工程系统。

## 当前入口

主要入口：

```bash
sca
```

实验入口：

```bash
sca-web
```

`sca-web` 可以保留给未来做 trace、任务树、diff、token 统计等可视化，但现阶段不作为主线。

## 架构

```text
用户请求
  -> Planner
  -> AgentRuntime
  -> LLM 响应
  -> 工具调用解析
  -> 工具执行
  -> 上下文记录
  -> 透明事件流
  -> CLI 渲染
```

核心模块：

- `core/runtime.py`：共享 ReAct runtime 和事件协议。
- `core/planner.py`：Planner 包装层，负责规划和委派。
- `core/agent.py`：ActorAgent 包装层，负责隔离子任务。
- `core/context.py`：上下文管理和压缩。
- `core/llm.py`：OpenAI-compatible 异步流式客户端。
- `core/state.py`：任务状态账本。
- `core/mcp/`：MCP 工具服务集成。
- `cli/`：CLI 入口、事件桥接和 Rich 渲染。
- `web/`：实验性 Streamlit UI。

## 安装

需要 Python 3.12+。如果使用 Actor 的 MCP 工具，还需要 Node.js 18+。

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"
npm install
```

如果要试验 Web UI：

```bash
pip install -e ".[web]"
```

## 配置

在项目根目录创建 `.env`：

```bash
SCA_API_KEY=your-api-key
SCA_API_BASE=https://api.deepseek.com
SCA_MODEL=deepseek-v4-pro
SCA_MAX_TOKENS=128000
SCA_MAX_ACTORS=4
```

## 使用

在当前仓库启动：

```bash
sca
```

指定工作区：

```bash
sca --dir C:\path\to\project
```

## CLI 透明事件

CLI 会展示：

- 模型流式输出
- 工具调用名称和关键参数
- 工具执行成功/失败摘要
- Actor 任务状态
- 上下文压缩提示
- token 使用统计
- 错误和最终输出

这样一次 agent run 可以被观察、复盘和调试，而不是黑盒调用。

## 开发验证

运行测试：

```bash
.\.venv\Scripts\python.exe -m pytest
```

编译检查：

```bash
.\.venv\Scripts\python.exe -m compileall core cli web tests
```

CLI smoke check：

```bash
.\.venv\Scripts\python.exe -m cli.main --help
```

准备本地 eval 任务工作区：

```bash
sca-eval prepare
```

然后对每个任务运行 `sca --dir tmp/eval-runs/<task_id>`。完成后检查结果：

```bash
sca-eval check
```

## 下一步路线

- 改进 test/diff/verification workflow。
- Web 端继续保持 experimental，除非它对 trace 可视化有明显价值。
