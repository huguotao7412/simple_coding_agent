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
- Actor 具备不依赖 Node 的基础文件、搜索、编辑和运行工具，MCP 作为可选增强绑定到 Actor worktree。
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
- 唯一的 LangGraph 1.x 持久控制平面，使用异步 SQLite checkpoint、结构化 Actor DAG fan-out 和可恢复人工审批，同时复用原安全执行内核。

当前目标不是宣称它已经是完全自治的生产级 coding system，而是打磨一个可靠、可审计、可持续评测的本地 agent 核心。

## A2A_lite

Actor 完成或失败后会生成版本化的 `A2A_lite` 消息。结构化 handoff 分别记录
发现、决策、约束、未解决问题和 artifact 引用；DAG 调度器会自动把依赖任务的
handoff 注入下游 Actor。完整 patch 与验证日志保留在用户级 SCA state 目录，prompt
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

编辑 `sca config path` 输出的用户配置文件，填入 `SCA_API_KEY`。之后可以在任意项目目录直接启动，当前目录就是默认 workspace。干净的 Git 仓库使用原生 worktree；非 Git 目录或包含未提交改动的 Git 工作区使用临时影子仓库，不会修改项目的 Git 元数据：

```powershell
cd C:\path\to\any-project
sca
```

升级和卸载：

```powershell
pipx upgrade simple-coding-agent
pipx uninstall simple-coding-agent
```

Node.js 18+ 只在需要增强型 MCP Actor 工具时才需要。wheel 自带 Python 基础工具：`list_dir`、`search_codebase`、`read`、`edit_file`、`write_file` 和 `run`，因此基本编码能力不依赖 `npm`、`npx` 或 `node_modules`。

## 开发安装

需要 Python 3.12+。只有在开发时使用可选 MCP Actor 工具服务，才需要 Node.js 18+。

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

### 持久化编排

交互 CLI、非交互 CLI、Web Live Agent、local eval 和 Harbor 只使用
LangGraph，不再存在控制平面选择参数：

```powershell
sca
sca --prompt "解释这个仓库"
sca --prompt "修复已审查的问题"
```

LangGraph 在 workspace 对应的用户级 state 目录保存紧凑流程状态，并保持
`run_id == thread_id`。高风险任务以结构化 interrupt 暂停，继续复用
`--approve-high-risk` 恢复。LangGraph 提供 durable orchestration，不提供安全
sandbox；checkpoint 重放也不等于 shell、文件系统或网络副作用 exactly-once。
完整边界见 [ADR-0006](docs/adr/0006-langgraph-control-plane.md)。

交互 CLI 中，每次用户输入都会建立独立的 durable Run 和 LangGraph thread。
下一轮只继承精简的用户/助手对话历史；execution policy、工具结果和 task DAG
保持 Run 级隔离。高风险 interrupt 会显示审批提示，并在同一 thread 上恢复。

升级前没有 LangGraph checkpoint 的旧 Run 仍可通过 `runs` 和 `inspect`
查看，但 `resume` 会给出明确迁移原因并拒绝恢复；系统不会猜测 graph program
counter。需要继续工作时应创建新 Run。

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

命令必须使用参数数组，并以无 shell 的方式在 Coder 隔离 worktree 中运行。必选门禁失败会把证据回灌给同一个 Actor，进行次数受限的自动修复；只有全部必选门禁通过后才导出 diff。完整输出保存在用户级 SCA state 目录。这些项目自有命令仍以当前用户权限执行，因此只应启用可信仓库中的配置。

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

Run checkpoint、最终报告、Actor patch 和 verification 日志默认按工作区分别保存在 Windows 的 `%LOCALAPPDATA%\\sca\\workspaces` 或 Unix 的 `$XDG_STATE_HOME/sca/workspaces`。可通过 `SCA_STATE_HOME` 覆盖根目录。目标工作区不再写入运行报告和数据库；`.sca/quality-gates.toml` 仍是可选的项目配置。`runs` 和 `inspect` 是只读命令，不需要模型 API key。

每个 workspace state 目录包含 `workspace.json`，记录原始 workspace 路径、
创建时间、最后访问时间和可选的 orphan 时间。`final_report.md` 始终表示最新
报告，历史报告保存在 `reports/<run-id>.md`。默认保留最近 30 天内的所有 Run，
并至少保留最新 50 个 Run；Actor 与 verification artifact 全局总容量上限为
1 GiB。可通过 `SCA_RETENTION_DAYS`、`SCA_RETAIN_RUNS` 和
`SCA_ARTIFACT_MAX_BYTES` 调整。

```powershell
sca gc --dry-run
sca gc
sca runs delete <run-id>
```

GC 对失联 workspace 采用保守的两阶段处理：第一次实际 GC 只标记 orphan，
超过保留期后的后续 GC 才删除；`--dry-run` 不修改 state。artifact 超限时按
最旧文件优先清理。处于 created、running 或 paused 状态的持久化 Run 不会因
时间或数量策略被删除。

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

现有 fixture suite 被定位为项目自身的回归与安全 smoke suite，而不是通用编码能力榜单。
标准 coding-agent 评估交给 Harbor 执行。安装可选依赖并运行持续更新的
SWE-rebench 数据集：

```bash
python -m pip install -e ".[benchmark]"
sca-eval harbor --model deepseek/deepseek-v4-pro
```

请在 Python 3.12 虚拟环境中运行这些命令；项目 CI 与 Harbor 任务容器内的
adapter 也统一使用 Python 3.12。

`sca-eval harbor` 会把当前 checkout 构建成 wheel，在每个 Harbor 任务容器中
安装同一个构建产物，自动发现任务仓库后以 headless 模式运行与 CLI/Web 相同的
SCA 核心，并将 token 统计、JSONL trace、final report 和 Actor artifacts 写入
Harbor job。adapter 不强制 benchmark 专用策略或工具顺序。数据集选择、参数透传
和建议的 nightly/release 运行节奏见 `evals/README.md`。

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
- Actor 任务使用独立 Git worktree；非 Git 和 dirty Git 工作区会先快照到临时影子仓库。
- 影子工作区合并会比较 patch 涉及文件的哈希，拒绝覆盖用户的并发修改。
- 下游 Actor 会接收上游 diff baseline，避免 Verifier 验证空代码。
- MCP 服务不可用时会降级到 Python 基础工具；基础工具缺失则在 Actor 首次模型调用前快速失败。
- Actor shell 非零退出会作为失败 ToolResult 返回。
- Actor 角色 allowlist 会在本地或 MCP 工具真正执行前再次强制校验。
- E2B 模式让 Actor shell 与确定性 verification 共用同一个 fail-closed 远程后端。
- 已落盘的 root tool-call 结果会在恢复时复用，避免 checkpoint 之后重复执行。

Git worktree 和影子仓库提供版本控制及默认工作目录隔离，不是操作系统沙箱。Actor 子进程仍拥有当前用户的系统权限。这些机制能降低风险，但不能替代进程级沙箱；请只在你愿意让 agent 修改的项目和环境中运行。

SQLite checkpoint 也不能与任意 shell、文件系统或网络副作用组成同一个原子事务。如果进程恰好在副作用成功后、tool result 落盘前崩溃，该操作仍可能在恢复时重试。完整取舍见 [ADR-0002](docs/adr/0002-durable-run-store.md)。

### 混合式安全边界

安全链路分成三个不能互相替代的层次：

1. 可选的 OpenAI Guardrails Python 0.2.x 是概率型内容风险信号。
2. `SecurityMiddleware` 是最终确定性 PDP/PEP，负责 capability、最终参数、workspace、审批、破坏性命令、审计与脱敏。
3. `SandboxBackend`、OS、代理、防火墙或 E2B 才负责真实资源和网络隔离。

`ALLOW` 不能覆盖本地 `DENY`；未知 Tool/capability 默认拒绝；输入审批不批准后续 Tool。模式为 `local`（仅本地检测）、`hybrid`（外部不可用时明确降级并保留本地限制）、`strict`（外部异常 fail closed）和 `off`（仅关闭内容检测，确定性控制仍开启）。

Guardrails 是可选依赖：`pip install -e ".[guardrails]"`。配置必须来自用户级可信配置或显式进程/CLI 环境，并使用绝对路径和独立的 `SCA_GUARDRAILS_API_KEY`。目标仓库 `.env` 不能设置安全模式、Guardrails endpoint/model/budget/config/key，仓库内 `.sca/guardrails.json` 也不会自动加载。源码、二进制、secret、credential 和原始 Tool output 默认禁止外发。

成功提交顺序是：校验 required artifact 及 digest；在领域 Run 仍非终态时持久化
verification；让 LangGraph 成功写入最终 checkpoint；最后才把 RunStore 标为
completed。任意持久化错误都会显式返回。本地 `AsyncSqliteSaver` 适合单进程
CLI，不是多进程生产 checkpointer。

## 开发

运行全部测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

对可信运行时边界执行类型检查：

```powershell
.\.venv\Scripts\python.exe -m mypy
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
