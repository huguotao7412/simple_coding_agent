<p align="center">
  <a href="README.md"><img src="https://img.shields.io/badge/English-🇬🇧-white?style=for-the-badge" alt="English"></a>
  <a href="README_CN.md"><img src="https://img.shields.io/badge/中文-🇨🇳-red?style=for-the-badge" alt="中文"></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/架构-Planner--Actor-8A2BE2?style=for-the-badge" alt="Planner-Actor">
  <img src="https://img.shields.io/badge/开源协议-MIT-yellow?style=for-the-badge" alt="License MIT">
  <img src="https://img.shields.io/badge/模型-DeepSeek%20V4%20Pro-4B8BF5?style=for-the-badge" alt="DeepSeek V4 Pro">
</p>

<h1 align="center">🧠 Simple Coding Agent</h1>

<p align="center">
  <b>基于 Plan-and-Execute 范式的生产级自主软件工程智能体</b>
</p>

<p align="center">
  <i>"不要告诉 Agent 敲什么代码 —— 告诉它你想要什么，然后看它自己搞定。"</i>
</p>

---

> **🎯 太长不看** — SCA 是一个 AI 编码智能体，能将复杂工程任务拆解为并发子任务，分发到隔离的工作 Agent 并行执行，最终汇总结果 —— 全程维护一个实时全局状态台账。你可以把它理解为一个 **由 LLM 大脑驱动的微型 CI/CD 流水线**。

---

## 📖 目录

- [为什么还需要一个编码 Agent？](#-为什么还需要一个编码-agent)
- [架构全景](#-架构全景)
- [核心特性](#-核心特性)
- [项目结构](#-项目结构)
- [快速开始](#-快速开始)
- [配置参考](#-配置参考)
- [工具库详解](#-工具库详解)
- [双端界面](#-双端界面)
- [安全与防护](#-安全与防护)
- [高级用法](#-高级用法)
- [常见问题](#-常见问题)
- [路线图](#-路线图)

---

## 🤔 为什么还需要一个编码 Agent？

市面上大多数编码 Agent 都是**单线程**的。它们思考→执行→思考→执行，一次只做一件事。修改变量名这种小事还行，但面对真实世界的需求就崩了：

> *"给这个 FastAPI 项目加上 JWT 鉴权、写单元测试、顺便更新 OpenAPI 文档。"*

单线程 Agent 只能串行搞定一切 —— 15 分钟后你还在等。**SCA 不一样。** 它会把这个需求拆成 3 个独立子任务，同时分发给 3 个并发 Actor，**耗时直接砍到三分之一**。

|   | 传统 Agent | **Simple Coding Agent** |
|---|---|---|
| 任务模型 | 线性 ReAct 循环 | **Plan → Delegate → Synthesize** |
| 并发能力 | 🚫 只能串行 | ✅ 最多 4 个 Actor 并发 |
| 状态追踪 | 临时变量（崩溃即丢失） | ✅ 全局状态机 + 变更日志 |
| 上下文管理 | 粗暴截断 | ✅ 分层压缩：台账硬保留 + 旧消息软摘要 |
| 死循环检测 | 没有或简陋 | ✅ 动作哈希熔断器 |
| 工具安全 | 基础 | ✅ 路径沙箱 + 命令黑名单 + 语法预检 |

---

## 🏗️ 架构全景

```
                        ┌─────────────────────────────┐
                        │         用户输入              │
                        └─────────────┬───────────────┘
                                      │
                                      ▼
                        ┌─────────────────────────────┐
                        │        🧠 PLANNER            │
                        │      （编排智能体）           │
                        │                              │
                        │  • 任务拆解                   │
                        │  • GlobalState 全局状态管理   │
                        │  • Actor 分发 & 结果汇总      │
                        │  • 上下文压缩                 │
                        └──────┬──────────────┬────────┘
                               │              │
                    update_state│              │ delegate()
                               │              │
                               ▼              ▼
                    ┌──────────────┐  ┌──────────────────┐
                    │  GlobalState │  │   ⚡ Actor 池      │
                    │  （单例模式） │  │  （最多 4 并发）   │
                    │              │  ├──────────────────┤
                    │ • TaskTree   │  │ Actor-1: 写鉴权   │
                    │ • ChangeLog  │  │ Actor-2: 写测试   │
                    │ • Snapshots  │  │ Actor-3: 写文档   │
                    └──────────────┘  └──────┬───────────┘
                                             │ 返回摘要
                                             ▼
                                    ┌──────────────────┐
                                    │   PLANNER 汇总    │
                                    │   → 综合分析      │
                                    │   → 最终回复      │
                                    └──────────────────┘
```

### 双层模型

**第一层 — Planner（大脑）**
- 运行 ReAct 循环，持有编排级工具（`delegate`、`update_state`、`search_codebase`、`list_dir`、`read_outline`）
- 绝不直接操作文件或执行 Shell 命令
- 维护 `GlobalState` 单例 —— 全部任务的唯一事实来源
- Token 逼近阈值时触发分层上下文压缩

**第二层 — Actor 池（手脚）**
- 无状态、隔离的执行单元 —— 每个 Actor 拥有独立的 `ContextManager` 与完整工具链（`read`、`write`、`edit`、`bash`、`search_codebase`、`list_dir`、`read_outline`）
- 通过 `asyncio.Semaphore(4)` 控制并发上限
- 返回结构化的 `ActorSummary { task_id, status, files_modified, bugs_found, key_findings }`
- 每个 Actor 只看 Planner 显式注入的上下文 —— 杜绝交叉污染

### GlobalState：全局台账

```python
# 每个任务都有 UUID、依赖列表、状态和结果摘要
TaskNode(
    task_id="task_a1b2c3d4",
    description="添加 JWT 鉴权中间件",
    status="running",       # pending → running → done / failed
    dependencies=[],        # 阻塞直到依赖任务完成
    assigned_actor=None,
    result_summary=None,
)
```

`ChangeLog` 记录每一次变更 —— 新增、更新、摘要 —— 并附带时间戳。Planner 通过 `consume_changes()` 增量消费变更日志，确保对 Agent 全局状态的每一丝变化都了如指掌。

---

## ✨ 核心特性

### 1. 🚀 并发任务编排

Planner 将用户需求拆解为**依赖感知的任务树**，然后将互不依赖的子任务分发到最多 **4 个并发 Actor**。每个 Actor 运行在独立的 `ContextManager` 沙箱中 —— 无共享可变状态，无竞态条件。

```text
用户："重构 auth 模块、加限流器、写集成测试"

Planner:
  ├── task_01: 重构 auth.py  → Actor-1 (运行中)
  ├── task_02: 添加限流器    → Actor-2 (运行中)  ← 并发！
  ├── task_03: 编写测试      → Actor-3 (运行中)  ← 并发！
  └── 汇总结果               → 最终回复
```

### 2. 🧠 思维链实时流式展示

DeepSeek V4 Pro 的推理 Token 实时流式展示，视觉上清晰区分：

```
> 🧠 Thinking...
> 先分析任务树...
> 三个任务彼此独立，没有依赖...
> 直接并发分发，等摘要回来再汇总...

（然后是正常的回答内容）
```

CLI 和 Web 两端都能以醒目的样式渲染推理过程 —— 你看到的不只是结论，更是 Agent **怎么想**的。

### 3. ⚡ 死循环熔断器

Agent 卡住了，反复以相同参数调用同一个失败的工具？

```
动作哈希: hash("bash" + json.dumps({"command": "npm run build"}))

[最近动作]: [hash1, hash2, hash1, hash3, hash1]
                              ↑ count(hash1) >= 2 → 熔断触发！

→ 系统干预警告注入对话
→ 强制 Agent 转换策略
```

告别死循环。告别浪费 API 额度。

### 4. 🧠 分层记忆压缩

当上下文达到模型上限的 **80%** 时触发：

| 层级 | 策略 | 保留内容 |
|---|---|---|
| **System Prompt** | 冻结，永不触碰 | Agent 身份、规则、工具 Schema |
| **工作台账** (Scratchpad) | 提取并**原样硬保留** | 已完成任务、当前 Bug、关键文件路径 |
| **中间历史** | LLM 摘要压缩 | 关键决策、文件修改记录 |
| **最近对话** | 完整保留（最近 N 轮） | 即时对话上下文 |

"硬保留 + 软摘要" 的分层设计，让 Agent 在极端上下文压力下也**绝不会忘记自己正在做什么**。

### 5. 🛡️ 默认安全

- **路径沙箱**：每次文件操作经过 `os.path.realpath()` 校验 —— `../../../etc/passwd` 逃逸攻击直接拦截
- **命令黑名单**：`sudo`、`rm -rf /`、`mkfs`、`dd if=`、Fork 炸弹、裸设备写入 —— 正则拦截
- **语法预检**：`write` 和 `edit` 在**落盘前**先验证 Python/JSON 语法 —— 拒绝产出 Broken Code
- **环境硬化**：Shell 会话强制 `DEBIAN_FRONTEND=noninteractive`、`CI=1`、`GIT_TERMINAL_PROMPT=0` —— 杜绝交互式弹窗卡死

---

## 📁 项目结构

```
simple_coding_agent/
│
├── pyproject.toml                # 包配置、入口脚本 (sca/sca-web)、依赖
├── .env.example                  # 环境变量模板
├── .gitignore
│
├── core/                         # 🧠 大脑层 —— 零 UI 耦合
│   ├── planner.py                # Planner 编排：拆解 → 分发 → 汇总
│   ├── agent.py                  # ActorAgent：隔离式 ReAct 执行器
│   ├── state.py                  # GlobalState：任务树 + 变更日志（单例）
│   ├── context.py                # ContextManager：Token 估算、分层压缩
│   ├── llm.py                    # LLMClient：异步 OpenAI 兼容流式客户端（含重试）
│   ├── system_prompt.py          # Planner 与 Actor 系统提示词
│   ├── exceptions.py             # 异常层级体系
│   │
│   └── tools/                    # 🛠️ 工具实现
│       ├── base.py               # BaseTool 抽象基类、ToolResult、语义截断
│       ├── delegate.py           # 并发 Actor 分发（asyncio.Semaphore 门控）
│       ├── update_state.py       # GlobalState CRUD（add/update/summary）
│       ├── read.py               # 分片带行号文件读取
│       ├── write.py              # 全量文件覆写 + 语法预检
│       ├── edit.py               # 三级 Diff 引擎（精确→去空格归一化→模糊匹配）
│       ├── bash.py               # 持久化 Shell + 后台进程管理
│       ├── search_codebase.py    # 双模搜索：AST 符号 / 正则文本
│       ├── list_dir.py           # 目录列表（带 emoji 图标）
│       ├── read_outline.py       # 文件骨架查看器（.py 走 AST，其他走正则）
│       └── __init__.py           # ACTOR_TOOLS 与 PLANNER_TOOLS 注册表
│
├── cli/                          # 🖥️ 终端皮肤
│   ├── main.py                   # CLI 入口（sca 命令），延迟导入
│   ├── ui.py                     # Rich 实时 Markdown 渲染、工具状态卡片
│   └── bridge.py                 # 异步事件流 → Rich UI 桥接
│
└── web/                          # 🌐 Web 皮肤（Streamlit）
    ├── cli.py                    # sca-web 入口（封装 streamlit run）
    ├── main.py                   # 三栏布局：侧边栏 | 对话 | 文件预览
    ├── bridge.py                 # 多线程异步事件 → Streamlit session_state 桥接
    └── components/
        ├── sidebar.py            # 项目切换 + 文件树 + 任务看板
        ├── chat.py               # 对话历史 + 流式事件渲染
        └── diff.py               # HTML Diff 渲染（绿增/红删）
```

### 工具权限矩阵

| 工具 | Planner | Actor | 用途 |
|---|---|---|---|
| `delegate` | ✅ | ❌ | 将子任务并发分发给 Actor |
| `update_state` | ✅ | ❌ | 全局任务树 CRUD |
| `read` | ❌ | ✅ | 分片带行号文件阅读 |
| `write` | ❌ | ✅ | 创建/覆写文件 + 语法检查 |
| `edit` | ❌ | ✅ | 精确搜索替换（三级 Diff 匹配） |
| `bash` | ❌ | ✅ | 持久化 Shell + 后台/logs/kill |
| `search_codebase` | ✅ | ✅ | AST 符号查询 + 正则文本搜索 |
| `list_dir` | ✅ | ✅ | 目录列表（深度=1） |
| `read_outline` | ✅ | ✅ | 文件骨架 —— 只看签名不看实现 |

> **设计原则**：Planner 负责*观察与决策*，Actor 负责*执行与汇报*。权限严格分离，杜绝越权。

---

## 🚀 快速开始

### 环境要求

| 条件 | 版本 | 说明 |
|---|---|---|
| **Python** | ≥ 3.12 | 原生 `asyncio` 改进、AST 新特性 |
| **API Key** | DeepSeek（或 OpenAI 兼容） | LLM 大脑 |
| **Git** | 任意较新版本 | `git diff` 安全网 |

### 1. 克隆与安装

```bash
git clone https://github.com/huguotao7412/simple_coding_agent.git
cd simple_coding_agent

# 创建虚拟环境并安装（推荐）
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .

# 可选：开发依赖（pytest）
pip install -e ".[dev]"
```

### 2. 配置环境变量

在项目根目录创建 `.env` 文件：

```bash
# 必填
SCA_API_KEY=sk-你的-deepseek-api-key

# 选填 —— 以下为默认值
SCA_API_BASE=https://api.deepseek.com
SCA_MODEL=deepseek-v4-pro
SCA_MAX_TOKENS=128000
SCA_WORKSPACE=./workspaces
```

### 3. 启动

```bash
# 终端模式 —— 交互式 REPL
sca

# 指定工作目录
sca --dir /path/to/your/project

# 临时换模型
sca --model gpt-4o
```

```bash
# Web 可视化面板 —— IDE 级体验
sca-web
# → 自动打开 http://localhost:8501
```

### 4. 试试这些指令

进入 REPL 后，直接用自然语言下指令：

```
> 帮我初始化一个 FastAPI 项目，要有 /health 端点和 Dockerfile。

> 读取 main.py，找到里面的冒泡排序，改成快速排序。

> 跑一下 pytest，如果报错了就看报错信息自己修，直到全绿为止。

> 找出项目里所有用 os.path 的地方，全部迁移到 pathlib。

> 给这个 Flask 项目加 JWT 鉴权。写测试。更新 README。
```

输入 `exit` 或 `quit` 退出。按 `Ctrl+C` 可中断正在运行的 Agent。

---

## ⚙️ 配置参考

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `SCA_API_KEY` | ✅ 是 | — | API 密钥（DeepSeek 或 OpenAI 兼容） |
| `SCA_API_BASE` | 否 | `https://api.deepseek.com` | API 端点地址 |
| `SCA_MODEL` | 否 | `deepseek-v4-pro` | 模型标识符 |
| `SCA_MAX_TOKENS` | 否 | `128000` | 每次 API 调用的 Token 预算 |
| `SCA_WORKSPACE` | 否 | `./workspaces` | 项目工作区根目录（Web 模式） |

---

## 🛠️ 工具库详解

### `read` — 分片文件阅读器
```
read(file_path="src/auth.py", offset=0, limit=500)
→ 返回带 1 起始行号的文件内容。大文件自动语义截断，提示先用 read_outline 查看结构。
```

### `write` — 原子化文件写入
```
write(file_path="src/new_module.py", content="...")
→ 自动创建父目录。写入前校验 Python/JSON 语法，拒绝产出 Broken Code。
```

### `edit` — 智能 Diff 引擎
```
edit(file_path="src/auth.py", search_block="...", replace_block="...")
→ 三级匹配策略：
  L1: 精确字符串匹配（快速路径）
  L2: 去尾随空格后匹配（容忍格式化差异）
  L3: 模糊匹配 difflib.SequenceMatcher（≥85% 相似度）
→ 返回 Unified Diff，+/- 清晰标注变更。
```

### `bash` — 持久化 Shell + 进程管理器
```
# 执行命令（状态持久化 —— cd、export、venv activate 效果保留）
bash(command="pytest tests/ -v", action="run")

# 后台启动开发服务器
bash(command="uvicorn app:app --port 8000", action="background")
→ 返回 PID

# 查看后台进程输出
bash(command="", action="logs", pid=12345)

# 终止后台进程
bash(command="", action="kill", pid=12345)
```

### `search_codebase` — 双模搜索引擎
```
# AST 符号搜索（Python 类/函数签名 + docstring）
search_codebase(query="authenticate", mode="symbol")

# 正则文本搜索（带 2 行上下文窗口）
search_codebase(query="TODO|FIXME|HACK", mode="text")

# 按文件后缀过滤
search_codebase(query="def test_", mode="symbol", include_ext=".py")
```

### `read_outline` — 文件骨架查看器
```
read_outline(file_path="core/agent.py")
→ 返回：
  L   25     [Class]  class ActorAgent:
  L  125      [Func]  def run(self, user_input, on_token=None) -> ActorSummary:
  L  251      [Func]  async def run_stream(self, user_input) -> AsyncGenerator:
  ...
→ 大文件建议先用此工具查看结构，再按需读具体段落。
```

### `list_dir` — 目录浏览器
```
list_dir(dir_path="core/tools")
→ 返回树形列表，📁/📄 emoji 标注，自动忽略 .git、.venv 等。
```

### Planner 专属工具

### `delegate` — 并发 Actor 分发
```
delegate(subtasks=[
  {"task_id": "task_01", "description": "添加 JWT 中间件",
   "context_files": ["src/auth.py"], "context_summaries": ["..."]},
  {"task_id": "task_02", "description": "编写测试",
   "context_files": ["tests/test_auth.py"]},
])
→ 通过 asyncio.Semaphore 控制最多 4 个 Actor 并发
→ 返回：每个子任务的结构化执行摘要
```

### `update_state` — 全局台账 CRUD
```
update_state(action="add_task", description="重构 auth 模块")
update_state(action="update_task", task_id="task_01", status="running")
update_state(action="add_summary", task_id="task_01", summary="完成。修改了 3 个文件。")
```

---

## 🖥️ 双端界面

### CLI 终端 (`sca`)
<p>
  <b>Rich 驱动的沉浸式终端体验</b> —— 实时 Markdown 流式渲染、工具执行状态动画、DeepSeek 思维链可视化。
</p>

- `rich.Live` 流式 Markdown，带光标闪烁动画
- 工具状态实时着色：⚡ 执行中（青色）→ ✅ 成功 / ❌ 失败
- DeepSeek 推理 Token 以 `> 🧠 Thinking...` 引用块形式呈现
- 启动时展示 SCA ASCII 艺术 Logo
- `Ctrl+C` 中断，`exit` 退出

### Web 面板 (`sca-web`)
<p>
  <b>Streamlit IDE 级可视化面板</b> —— 项目切换、文件树、任务看板、对话区，一屏尽览。
</p>

| 面板 | 内容 |
|---|---|
| **侧边栏** | 项目下拉切换器、可展开文件树、实时任务状态看板 |
| **主对话区** | 流式 Agent 回复、可折叠工具执行卡片、Diff 着色高亮 |
| **文件预览区** | 点击侧边栏任意文件 → 语法高亮预览（带行号） |
| **任务看板** | 实时 `GlobalState` 快照 —— 哪些任务 pending / running / done / failed |

---

## 🛡️ 安全与防护

SCA 能写文件和执行 Shell 命令，安全绝非儿戏：

### 五层纵深防御

```
第一层 —— 路径沙箱
  所有文件操作经 BaseTool.validate_path() →
  os.path.realpath() 校验工作区根路径。
  ../../../etc/passwd → 拦截

第二层 —— 命令黑名单
  sudo、rm -rf /、mkfs、dd if=、chmod 777 /、
  fork 炸弹、format C:、> /dev/sda → 正则拦截

第三层 —— 语法预检
  write 和 edit 在落盘前解析 Python AST / JSON。
  SyntaxError → 拒绝写入，文件完好无损。

第四层 —— 死循环熔断
  相同工具 + 相同参数在最近历史中重复 ≥2 次 →
  跳过执行，注入 System Alert 强制 Agent 换策略

第五层 —— 环境硬化
  DEBIAN_FRONTEND=noninteractive、CI=1、GIT_TERMINAL_PROMPT=0
  → 防止 apt/npm/git 弹交互式提示卡死
```

### 安全建议

1. **始终使用版本控制。** 在 Git 仓库中运行 SCA。每次改动用 `git diff` 审查。出事了一键 `git reset --hard`。

2. **禁止 Root 运行。** 别用 `sudo sca`。Agent 不需要 root，你也不该给。

3. **高风险操作用 Docker。** 把工作区挂进容器里跑，多加一层隔离。

4. **审查 Diff。** `edit` 工具每次返回 Unified Diff，花几秒扫一眼再继续。

---

## 🔧 高级用法

### CLI 和 Web 随意切换

两端共享同一套 `core/` 引擎：
- 在 `sca-web` 里开启任务，看过任务看板，切到 `sca` 终端继续
- 同时跑多个 `sca` 会话对应不同工作区
- Web 端刷新页面后自动恢复会话状态

### 自定义系统提示词

系统提示词就在 `core/system_prompt.py`，想要不同的 Agent 人格？

```python
# 直接改 PLANNER_SYSTEM_PROMPT 或 ACTOR_SYSTEM_PROMPT
# 不需要配置文件 —— 就是 Python 字符串
```

### 添加自定义工具

```python
# 1. 创建 core/tools/my_tool.py
from .base import BaseTool, ToolResult

class MyTool(BaseTool):
    name = "my_tool"
    description = "做点有用的事。"
    parameters = {...}
    required_params = [...]

    async def execute(self, **kwargs) -> ToolResult:
        ...

# 2. 在 core/tools/__init__.py 里注册
from .my_tool import MyTool

# 按权限需求加入 ACTOR_TOOLS 或 PLANNER_TOOLS
ACTOR_TOOLS = [..., MyTool]
```

### 编程式 API

```python
import asyncio
from core.llm import LLMClient
from core.context import ContextManager
from core.planner import Planner
from core.tools import PLANNER_TOOLS
from core.system_prompt import PLANNER_SYSTEM_PROMPT

async def main():
    llm = LLMClient(api_key="...", model="deepseek-v4-pro")
    ctx = ContextManager(system_prompt=PLANNER_SYSTEM_PROMPT)
    tools = [t() for t in PLANNER_TOOLS]
    planner = Planner(llm, ctx, tools, workspace_dir="./my_project")

    async for event in planner.run_stream("给所有函数加上类型标注"):
        if event.type == "thought":
            print(event.token, end="", flush=True)
        elif event.type == "done":
            print(f"\n\n最终结果: {event.content}")

asyncio.run(main())
```

---

## ❓ 常见问题

<details>
<summary><b>Q: 为什么用 DeepSeek？能用 OpenAI/GPT-4 吗？</b></summary>

SCA 基于 OpenAI 兼容 API 格式构建。任何支持 `tools` 和流式输出的模型都能用：

```bash
SCA_API_BASE=https://api.openai.com/v1
SCA_MODEL=gpt-4o
```

但**强烈推荐 DeepSeek V4 Pro** —— 它的原生推理 Token（`reasoning_content`）赋予了 SCA 透明的思维链能力。其他模型能跑，但看不到思考过程。
</details>

<details>
<summary><b>Q: 和 Claude Code / Aider / Cursor 有什么区别？</b></summary>

- **Claude Code / Cursor** 是交互式结对编程工具。SCA 是**自主智能体** —— 你给目标，它自己想办法。
- **Aider** 是单线程 edit-edit-edit。SCA 先规划，再**并发**执行。
- SCA 的 **Planner-Actor 分离架构**意味着它能同时动多个文件 —— 大多数同类 Agent 严格串行。
</details>

<details>
<summary><b>Q: SCA 能处理多复杂的任务？</b></summary>

实践经验：适宜 3-5 个独立子任务以内的复杂度。Planner 有 50 步限制，每个 Actor 有 30 步限制。Token 用量达 80% 时触发压缩。超大项目建议拆成多轮 SCA 会话。
</details>

<details>
<summary><b>Q: 能在 CI/无头模式下跑吗？</b></summary>

暂不支持 —— CLI 目前需要交互式终端。编程式 API 可用（见上文），但尚未封装为 CI 友好的命令行。已在路线图中。
</details>

<details>
<summary><b>Q: 支持 Windows 吗？</b></summary>

支持！SCA 在 Windows 11 上实测通过。`bash` 工具自动检测平台，Windows 使用 `cmd.exe` 持久会话，Unix 使用 `/bin/bash`。工作区树形目录的回退方案采用纯 Python 实现，不依赖 `tree` 命令。
</details>

---

## 🗺️ 路线图

| 里程碑 | 状态 |
|---|---|
| Planner-Actor 编排架构 | ✅ 已完成 |
| GlobalState 依赖 DAG | ✅ 已完成 |
| 并发 Actor 分发（asyncio 门控） | ✅ 已完成 |
| 分层记忆压缩 | ✅ 已完成 |
| 死循环熔断器 | ✅ 已完成 |
| 双端 UI（CLI + Streamlit Web） | ✅ 已完成 |
| 8 个核心工具 + 语法校验 | ✅ 已完成 |
| Git Worktree 隔离（每个 Actor 独立分支） | 🔨 开发中 |
| CI/CD 无头模式 | 📋 计划中 |
| 破坏性操作人工审批（Human-in-the-loop） | 📋 计划中 |
| 持久化会话历史（SQLite） | 📋 计划中 |
| 多模型路由（简单任务走便宜模型） | 💡 想法阶段 |

---

## 🙏 致谢

基于以下开源项目构建：

- [DeepSeek](https://deepseek.com) — 赋予一切可能的 LLM
- [Rich](https://github.com/Textualize/rich) — 精美的终端渲染
- [Streamlit](https://streamlit.io) — 极速 Web UI 原型
- [httpx](https://www.python-httpx.org/) — 异步 HTTP/2 客户端

---

<p align="center">
  <b>⭐ 如果这个项目帮到你，别忘了点个 Star！</b><br>
  <sub>由 <a href="https://github.com/huguotao7412">huguotao7412</a> 用 ☕ 和无数深夜打造</sub>
</p>
