# Simple Coding Agent

Simple Coding Agent 是一个 **CLI 优先的本地 Coding Agent Runtime**，重点是透明工具调用、隔离执行和可验证代码修改。

这个项目面向习惯使用终端、Git 仓库和测试套件的开发者。Web UI 仍然保留，但目前定位为实验性入口，不是核心产品面。

## 项目展示重点

- Planner 和 Actor 共享一套 ReAct Runtime，而不是复制两套循环。
- 透明事件流：模型输出、工具调用、工具结果、错误、token 统计和任务状态更新。
- 集中化 tool-call JSON 解析，并能从格式错误中恢复。
- 最大步数限制、重复动作熔断等运行时安全控制。
- Planner/Actor 编排和角色化工具权限。
- MCP 文件与 shell 工具集成。
- 使用 git worktree 隔离委派给 Actor 的子任务。
- 用 deterministic tests 验证 runtime 行为，并保留 MCP 集成 smoke tests。

当前目标是打磨一个可靠、可审计、可持续评测的本地 agent 核心，而不是宣称它已经是全自动生产级软件工程系统。

## 当前状态

主要入口：

- `sca`：CLI coding agent REPL。

实验入口：

- `sca-web`：Streamlit 可视化面板。它适合未来做 trace 可视化，但不是当前核心里程碑。

## 架构

```text
用户请求
  -> Planner
  -> AgentRuntime
  -> LLM response
  -> tool-call parser
  -> tool executor
  -> context observation
  -> transparent event stream
  -> CLI renderer
```

代码仍然保留 Planner/Actor 分层：

- `Planner` 负责拆解任务，并可以委派子任务。
- `ActorAgent` 使用角色化权限执行隔离子任务。
- `AgentRuntime` 负责共享执行循环：LLM 调用、工具解析、工具执行、上下文压缩、步数限制、重复动作检测和事件发射。
- `GlobalState` 记录任务状态和 Actor 更新。

## 项目结构

```text
core/
  runtime.py        共享 ReAct runtime 和 AgentEvent 协议
  planner.py        Planner 对 AgentRuntime 的封装
  agent.py          ActorAgent 对 AgentRuntime 的封装
  context.py        对话上下文和上下文压缩
  llm.py            OpenAI-compatible 异步流式客户端
  state.py          任务账本和状态快照
  mcp/              MCP 工具 provider
  tools/            本地 Planner/Actor 工具

cli/
  main.py           CLI 入口
  bridge.py         runtime event -> terminal UI bridge
  ui.py             Rich 终端渲染

web/
  experimental Streamlit UI

tests/
  test_runtime.py          deterministic runtime tests
  test_role_config.py      角色和工具权限测试
  test_mcp_integration.py  MCP smoke test
```

## 安装

需要 Python 3.12+。如果要使用 MCP Actor 工具，还需要 Node.js 18+。

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"
npm install
```

实验性 Web UI：

```powershell
pip install -e ".[web]"
```

## 配置

在仓库根目录创建 `.env`：

```bash
SCA_API_KEY=your-api-key
SCA_API_BASE=https://api.deepseek.com
SCA_MODEL=deepseek-v4-pro
SCA_MAX_TOKENS=128000
SCA_MAX_ACTORS=4
```

客户端使用 OpenAI-compatible chat completions API。

## 使用

在当前仓库运行 CLI：

```powershell
sca
```

指定其他工作区：

```powershell
sca --dir C:\path\to\project
```

实验性 Web UI：

```powershell
sca-web
```

## CLI 事件透明性

CLI 会渲染 runtime 事件流：

- 流式模型输出
- 工具调用名称和精简参数
- 工具成功/失败摘要
- Actor 任务更新
- 上下文压缩提示
- token 使用统计
- 错误和最终输出

目标是让每次运行都可检查，而不是把 agent 当成黑盒。

## 安全模型

当前安全模型是务实的工程防护：

- 工具调用集中解析，格式错误会作为可恢复反馈返回给模型。
- 重复的相同工具调用会触发熔断。
- 最大步数限制防止无限循环。
- 本地工具和 MCP provider 都会校验文件访问边界。
- 委派给 Actor 的任务使用独立 git worktree。
- Planner 和 Actor 可以使用不同工具 allowlist。

这些机制能降低风险，但不是完整沙箱。请只在你愿意让 agent 修改的仓库和环境中运行。

## 开发

运行全部测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

编译检查：

```powershell
.\.venv\Scripts\python.exe -m compileall core cli web evals tests
```

CLI smoke check：

```powershell
.\.venv\Scripts\python.exe -m cli.main --help
```

准备本地 eval 任务工作区：

```powershell
sca-eval prepare
```

然后对每个任务运行：

```powershell
sca --dir tmp/eval-runs/<task_id>
```

完成后检查结果：

```powershell
sca-eval check
```

## 路线图

近期：

- 增加结构化 final report：修改文件、使用工具、验证命令、残余风险。
- 改进测试命令和 diff summary 周围的验证工作流。
- Web UI 继续保持实验状态，除非它能真正服务 trace 可视化。

长期：

- 更好的 Actor diff 合并和冲突处理流程。
- 更稳健的模型/provider 路由。
- 持久化 run traces，用于调试和 eval 对比。
- 对破坏性或高风险操作增加人工审批。

## License

MIT。详见 `LICENSE`。
