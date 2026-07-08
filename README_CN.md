# Simple Coding Agent

一个本地 Coding Agent 原型，核心围绕 Planner-Actor 编排、git worktree 隔离、MCP 工具接入、上下文管理和 fixture-based eval 反馈闭环构建。

这个项目的目标不是做一个花哨聊天界面，而是探索一个可解释、可验证、可演进的自主编程系统：如何拆任务、如何隔离执行、如何管理工具权限、如何合并补丁、如何用评测证明能力没有退化。

## 当前状态

这是一个工程 MVP，不是最终产品。项目已经具备核心架构和第一版 eval runner。下一阶段重点是结构化 Actor 输出、Planner 自动合并与验证闭环，以及更完整的 eval suite。

## 架构概览

```text
用户请求
  -> Planner
      - 拆解任务
      - 更新 GlobalState
      - 分发独立子任务
      - 汇总结果
  -> Actor workers
      - 在独立 git worktree 中执行
      - 使用角色化工具权限
      - 通过 MCP 调用文件系统和 shell 工具
  -> Planner
      - 接收 summary 和 diff
      - 应用 patch
      - 返回最终结果
```

核心模块：

- `core/planner.py`：顶层编排循环。
- `core/agent.py`：Actor ReAct 执行循环。
- `core/state.py`：任务状态账本和变更日志。
- `core/tools/delegate.py`：并发 Actor 分发和依赖处理。
- `core/git_utils.py`：worktree 创建、diff 提取和清理。
- `core/mcp/client.py`：MCP server 生命周期、工具路由、超时和熔断。
- `evals/run_evals.py`：fixture-based eval runner。
- `cli/main.py`：命令行入口。
- `web/main.py`：Streamlit Web UI 入口。

## 主要能力

- Planner-Actor 任务编排。
- 可配置的并发 Actor 执行。
- 每个 Actor 使用独立 git worktree 隔离修改。
- 通过 MCP 接入文件系统和 shell 工具。
- scout、coder、verifier 三类角色化工具权限。
- GlobalState 维护任务树、快照和变更记录。
- 上下文压缩和大工具输出截断。
- 支持 Actor diff 提取和 patch 应用。
- 提供 CLI、非交互 CLI 和 Streamlit UI。
- 提供 fixture-based eval runner，输出 JSON 和 Markdown 报告。

## 快速开始

要求：

- Python 3.12+
- Node.js 和 npm，用于 MCP server 依赖
- Git

安装 Python 依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .[dev]
```

安装 MCP server 依赖：

```powershell
npm install
```

创建本地配置：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，填入 `SCA_API_KEY`。

运行 CLI：

```powershell
.\.venv\Scripts\python.exe -m cli.main --workspace . --prompt "Inspect this repository and summarize the architecture."
```

运行 Streamlit UI：

```powershell
.\.venv\Scripts\sca-web
```

## 配置项

- `SCA_API_KEY`：OpenAI-compatible 模型服务 API key。
- `SCA_API_BASE`：API base URL，默认 `https://api.deepseek.com`。
- `SCA_MODEL`：模型名称，默认 `deepseek-v4-pro`。
- `SCA_MAX_TOKENS`：模型上下文/token 预算，默认 `128000`。
- `SCA_WORKSPACE`：Web UI 使用的工作区路径，默认 `./workspaces`。
- `SCA_MAX_ACTORS`：最大并发 Actor 数，默认 `4`。

## 测试

运行单元测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

`evals/cases/*/repo` 下的样例仓库故意包含失败或未完成代码，不参与普通 pytest 收集；它们应该通过 eval runner 执行。

只做 eval 发现和报告生成，不调用 agent：

```powershell
.\.venv\Scripts\python.exe -m evals.run_evals --dry-run
```

运行单个 eval：

```powershell
.\.venv\Scripts\python.exe -m evals.run_evals --case fix_failing_pytest --agent-command ".\.venv\Scripts\python.exe -m cli.main --workspace {workspace} --prompt ""{prompt}"""
```

报告输出到：

- `evals/reports/latest.json`
- `evals/reports/latest.md`

## 安全边界

项目目前包含这些防护：

- Actor 修改先发生在临时 git worktree 中。
- MCP filesystem server 绑定到 Actor worktree。
- 额外路径校验会拒绝访问 worktree 外部的绝对路径。
- 工具有超时和 provider 级熔断。
- 上下文管理会截断过大的工具输出，减少 token 失控。

这些机制可以降低风险，但不是完整沙箱。请只在你愿意让 agent 修改的仓库和环境中运行。

## 路线图

- 结构化 Actor JSON summary。
- 确定性的 Planner 合并、验证和重试协议。
- 更多 eval cases 和通过率追踪。
- Trace 持久化与回放。
- CI 发布 eval 报告。
- 更强的命令策略和审计日志。

## License

MIT。详见 `LICENSE`。
