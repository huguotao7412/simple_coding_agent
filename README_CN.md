# Simple Coding Agent

Simple Coding Agent 是一个 **CLI 优先的本地 coding agent runtime**，重点是透明工具调用、隔离执行、可验证代码修改和可衡量 eval。

项目面向习惯使用终端、Git 仓库和测试套件的开发者。Web UI 仍然保留，但目前是实验入口，不是核心产品面。

## 项目展示重点

- Planner 和 Actor 共享同一套 ReAct runtime。
- Planner 与嵌套 Actor 共用关联事件流：模型输出、工具调用、工具结果、错误、token 使用和任务状态更新。
- 集中式 tool-call JSON 解析，并能把格式错误作为可恢复反馈。
- 运行时安全控制：最大步数、重复动作熔断、上下文压缩。
- Run 级任务状态和关联 ID，不再依赖进程级 Planner 全局状态。
- Planner/Actor 编排，以及在执行入口强制校验的角色工具 allowlist。
- MCP 文件和 shell 工具集成，并绑定到 Actor worktree。
- 使用 git worktree 隔离委派给 Actor 的子任务。
- 使用确定性项目质量门禁与有界自动修复验证 Coder 产出。
- 依赖任务的 diff 会作为下游 Actor 的 baseline，Verifier 能验证真实 Coder 改动。
- eval/debug 运行会持久化 JSONL trace。
- 非交互任务会持久化 Run checkpoint，可列出、检查并恢复中断任务。
- 本地 eval runner 会输出聚合的 `eval_results.json` 指标。
- 版本化的确定性任务评估会记录意图、复杂度、风险和推荐执行策略。
- 版本化执行策略会在运行时强制 Actor 拓扑、模型调用、token、失败工具、修复和活跃时长预算。
- 高风险任务在首次模型调用前 fail-closed，需要 CLI 显式批准；策略和消费账本随 checkpoint 恢复。
- Actor shell 与 verification 共享可替换的本地/E2B 沙箱协议。
- 单元测试覆盖 runtime、隔离、报告和 eval 行为。

当前目标不是宣称它已经是完全自治的生产级 coding system，而是打磨一个可靠、可审计、可持续评测的本地 agent 核心。

## A2A_lite

Actor 完成或失败后会生成版本化的 `A2A_lite` 消息。结构化 handoff 分别记录
发现、决策、约束、未解决问题和 artifact 引用；DAG 调度器会自动把依赖任务的
handoff 注入下游 Actor。完整 patch 与验证日志保留在 `.sca/artifacts/`，prompt
只传递引用和结构化元数据，从而减少隐式共享状态和手工摘要传递。

当前实现保持进程内运行，不引入消息 broker 或网络服务。

## 当前状态

主要入口：

- `sca`：CLI coding agent REPL。

实验入口：

- `sca-web`：Streamlit 可视化面板，适合未来做 trace 展示。

## 架构

```text
用户请求
  -> TaskAssessor（意图 / 复杂度 / 风险 / 策略）
  -> ExecutionPolicy / RunBudgetLedger
  -> Planner
  -> AgentRuntime
  -> LLM response
  -> tool-call parser
  -> tool executor
  -> context observation
  -> transparent event stream
  -> CLI renderer / eval trace writer
```

代码保持 Planner/Actor 分层：

- `Planner` 负责拆解任务、委派子任务、接收 Actor summary/diff，并合成最终回复。
- `ActorAgent` 在隔离 worktree 中执行单个子任务。
- `AgentRuntime` 负责共享执行循环：LLM 调用、工具解析、工具执行、上下文压缩、步数限制、重复动作检测和事件输出。
- `ExecutionPolicy` 将评估结果编译为不可由工具调用覆盖的拓扑与资源约束；`RunBudgetLedger` 由 Planner 和全部 Actor 共享。
- `GlobalState` 记录任务树、任务状态和 Actor 更新。

更完整的架构说明见 [architecture.md](architecture.md) 和 [architecture_CN.md](architecture_CN.md)。

## 项目结构

```text
core/
  runtime/          执行循环和对话上下文
  runs/             Run 生命周期、任务状态和持久化适配器
  actors/           Actor 行为、执行契约、角色和 worktree 适配器
  verification/     质量门禁配置、执行证据与修复提示
  execution/        确定性任务评估与执行策略契约
  sandbox/          命令沙箱协议、配置、E2B 适配器与工作区传输
  planner.py        Planner 对 runtime engine 的封装
  events.py         跨域 AgentEvent 协议
  llm.py            OpenAI-compatible 异步流式客户端
  mcp/              MCP tool provider
  tools/            本地 Planner/Actor 工具

cli/
  main.py           CLI 入口
  bridge.py         runtime event -> terminal UI bridge
  ui.py             Rich 终端渲染

evals/
  cli.py            sca-eval 命令
  run_evals.py      fixture 复制、agent 运行、结果检查和 JSON 汇总

web/
  experimental Streamlit UI

tests/
  test_runtime.py          runtime 行为测试
  test_cli_report.py       final report 审计测试
  test_delegate_baseline.py 依赖 diff baseline 测试
  test_evals.py            eval runner 测试
  test_mcp_provider.py     MCP provider 隔离测试
```

## 用户安装

推荐使用 `pipx`。它会为 SCA 管理独立 Python 环境，同时把 `sca` 命令加入用户级 `PATH`，因此不需要激活源码仓库或目标项目的 `.venv`。

Windows 首次准备 `pipx`：

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
```

重启终端后，从 GitHub 安装当前版本：

```powershell
pipx install git+https://github.com/huguotao7412/simple_coding_agent.git
sca config init
sca config path
```

编辑 `sca config path` 输出的用户配置文件，填入 `SCA_API_KEY`。之后可以在任意 Git 工作区直接启动，当前目录就是默认 workspace：

```powershell
cd C:\path\to\any-project
sca
```

升级和卸载：

```powershell
pipx upgrade simple-coding-agent
pipx uninstall simple-coding-agent
```

仍需安装 Node.js 18+。开发 checkout 优先使用仓库 `node_modules`；用户级安装首次启动 Actor 时，`npx` 会下载并缓存代码中固定版本的 MCP 文件/shell 服务。

## 开发安装

需要 Python 3.12+。如果使用 MCP Actor 工具，还需要 Node.js 18+。

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"
npm install
```

实验 Web UI：

```powershell
pip install -e ".[web]"
```

## 配置

用户级默认配置由 `sca config init` 创建。也可以在某个目标工作区根目录放置 `.env` 作为该项目的覆盖配置：

```bash
SCA_API_KEY=your-api-key
SCA_API_BASE=https://api.deepseek.com
SCA_MODEL=deepseek-v4-pro
SCA_MAX_TOKENS=128000
SCA_MAX_ACTORS=4
SCA_SANDBOX_BACKEND=e2b
E2B_API_KEY=e2b_your_key_here
SCA_E2B_TEMPLATE=base
SCA_E2B_ALLOW_INTERNET=false
SCA_SANDBOX_MAX_TIMEOUT=300
SCA_SANDBOX_MAX_TRANSFER=50000000
```

配置优先级为：进程环境变量 > 当前工作区 `.env` > 用户级配置。SCA 只读取当前工作区的 `.env`，不会向父目录搜索，避免从启动终端的位置意外继承配置。

客户端使用 OpenAI-compatible chat completions API。

### 命令沙箱

`local` 是兼容性默认值，**不提供 OS 隔离**。无需 API key 即可检查：

```powershell
sca sandbox-check
```

启用 E2B 后端前，请在 <https://e2b.dev/dashboard> 注册并创建 API Key：

```powershell
$env:SCA_SANDBOX_BACKEND = "e2b"
$env:E2B_API_KEY = "e2b_your_key"
sca sandbox-check
```

E2B 模式仍由可信宿主机管理 Git/worktree，命令与确定性验证在远程 Linux 沙箱中
运行。受限 archive transport 会在每条命令前后同步 Actor worktree，并拒绝路径
穿越；`.env`、Git 元数据、虚拟环境、依赖缓存和常见凭证文件不会上传。默认禁止
远程出站网络，需要安装依赖时必须显式设置 `SCA_E2B_ALLOW_INTERNET=true`。
SDK、API Key 或远程服务不可用时会明确失败，绝不会静默退回宿主机执行。使用者
也必须理解：符合传输规则的仓库源代码会发送至 E2B 云端。

项目可选地在 `.sca/quality-gates.toml` 中声明确定性 Coder 质量门禁：

```toml
max_repair_attempts = 2

[[gates]]
name = "unit"
command = ["{python}", "-m", "pytest", "-q"]
timeout_seconds = 120

[[gates]]
name = "types"
command = ["{python}", "-m", "mypy", "core"]
required = false
```

命令必须使用参数数组，并以无 shell 的方式在 Coder 隔离 worktree 中运行。必选门禁失败会把证据回灌给同一个 Actor，进行次数受限的自动修复；只有全部必选门禁通过后才导出 diff。完整输出保存在 `.sca/artifacts/verification/`。这些项目自有命令仍以当前用户权限执行，因此只应启用可信仓库中的配置。

## 使用

在当前仓库运行 CLI：

```powershell
sca
```

指定其他工作区：

```powershell
sca --dir C:\path\to\project
```

运行一个可恢复的非交互任务：

```powershell
sca --dir C:\path\to\project --prompt "修复失败的测试"
```

被确定性评估判定为高风险的任务会在第一次模型调用前停止。审查预期副作用后，使用显式批准参数重新运行或恢复：

```powershell
sca --approve-high-risk --dir C:\path\to\project --prompt "执行已审查的数据库迁移"
sca --approve-high-risk --dir C:\path\to\project resume run_abc123
```

该参数只是本地 CLI 的显式授权信号，不是多人审批服务，也不会绕过工具 allowlist、命令防护、workspace 边界或 sandbox。

检查和恢复本地 Run：

```powershell
sca --dir C:\path\to\project runs
sca --dir C:\path\to\project inspect run_abc123
sca --dir C:\path\to\project resume run_abc123
```

Run checkpoint 默认保存在工作区的 `.sca/runs.db`。`runs` 和 `inspect` 是只读命令，不需要模型 API key。P1 的持久化恢复覆盖 `--prompt` 单任务运行；交互式多轮 REPL 暂时仍是内存会话。

实验 Web UI：

```powershell
sca-web
```

## Eval

准备本地 eval 工作区：

```powershell
sca-eval prepare
```

运行完整可衡量 eval：

```powershell
sca-eval run --model deepseek-v4-pro
```

默认输出：

- 根目录 `eval_results.json`
- 每个任务工作区的 `.sca/final_report.md`
- 每个任务工作区的 `.sca/traces/run_trace.jsonl`

也可以手动运行某个任务：

```powershell
sca --dir tmp/eval-runs/<task_id>
```

然后检查结果：

```powershell
sca-eval check
```

## CLI 事件透明性

CLI 会渲染 runtime 事件流：

- 确定性任务评估和推荐执行策略
- 流式模型输出
- 工具调用名称和精简参数
- 工具成功/失败摘要
- Actor 任务更新
- 上下文压缩提示
- 整个 Planner/Actor 执行树的 token 统计，并标注 provider 原始值或本地估算值
- 错误和最终输出

eval runner 会把同一条事件流写成 JSONL trace，方便调试、复盘和展示。

## 安全模型

当前安全模型是务实的工程防护：

- 工具调用集中解析，格式错误会作为可恢复反馈返回给模型。
- 重复的相同工具调用会触发熔断。
- 最大步数限制防止无限循环。
- 本地工具和 MCP provider 都会校验文件访问边界。
- Actor 任务使用独立 git worktree。
- 下游 Actor 会接收上游 diff baseline，避免 Verifier 验证空代码。
- Actor 角色 allowlist 会在本地或 MCP 工具真正执行前再次强制校验。
- E2B 模式让 Actor shell 与确定性 verification 共用同一个 fail-closed 远程后端。
- 已落盘的 root tool-call 结果会在恢复时复用，避免 checkpoint 之后重复执行。

Git worktree 提供版本控制和默认工作目录隔离，不是操作系统沙箱。Actor 子进程仍拥有当前用户的系统权限。这些机制能降低风险，但不能替代进程级沙箱；请只在你愿意让 agent 修改的仓库和环境中运行。

SQLite checkpoint 也不能与任意 shell、文件系统或网络副作用组成同一个原子事务。如果进程恰好在副作用成功后、tool result 落盘前崩溃，该操作仍可能在恢复时重试。完整取舍见 [ADR-0002](docs/adr/0002-durable-run-store.md)。

## 开发

运行全部测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

对可信运行时边界执行类型检查：

```powershell
.\.venv\Scripts\python.exe -m mypy core/execution core/sandbox core/actors/contracts.py core/policy.py core/events.py core/runs/models.py core/runs/store.py core/runs/sqlite_store.py core/runs/context.py core/runtime/engine.py core/actors/worktree.py core/verification core/planner.py core/actors/agent.py core/mcp/client.py core/tools/update_state.py core/tools/delegate.py core/tools/sandbox_run.py cli/report.py cli/runs.py cli/main.py evals/run_evals.py
```

编译检查：

```powershell
.\.venv\Scripts\python.exe -m compileall core cli web evals tests
```

CLI smoke check：

```powershell
.\.venv\Scripts\python.exe -m cli.main --help
```

## 路线图

近期：

- 使用重复的真实模型 eval 基线校准任务评估规则。
- 扩展多文件、恢复、冲突和故障注入 eval，并比较单位成功成本。

长期：

- 更好的 Actor diff 合并和冲突处理流程。
- 更稳健的模型/provider 路由。
- 为破坏性或高风险操作增加可审计的交互式/多人审批工作流。

## License

MIT。详见 `LICENSE`。
