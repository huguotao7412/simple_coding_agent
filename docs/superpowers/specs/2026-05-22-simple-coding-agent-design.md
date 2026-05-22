# Simple Coding Agent (SCA) — 设计规格书

> 日期：2026-05-22 | 状态：待审核

## 1. 项目概述

Simple Coding Agent 是一个基于纯净 ReAct 架构的本地编码辅助智能体，运行在终端中。不依赖任何重型 Agent 框架，完全由原生 Python 构建。

### 核心哲学

- **拒绝臃肿**：系统提示词控制在千字以内，底层仅暴露 `read`、`write`、`edit`、`bash` 四个原子操作，让模型通过写代码和执行脚本来解决复杂问题。
- **核心即逻辑**：严格遵循"大脑（Core）"与"皮肤（CLI）"物理隔离，核心代码不包含任何 `print` 或 UI 渲染，可无缝迁移到任何终端或前端界面。

---

## 2. 运行模式

**仅支持交互式 REPL 模式**。启动后进入持续对话，用户输入消息 → Agent 在内部循环中思考、调用工具、观察结果，直到产出最终文本回复 → 等待用户下一轮输入。用户输入 `exit` 或 `quit` 退出。

不提供单轮 oneshot 模式。

---

## 3. 架构概览

```
simple_coding_agent/
├── pyproject.toml
├── .env                          # API 密钥等敏感配置（不提交 Git）
├── .gitignore
│
├── core/                         # 🧠 大脑层 — 纯逻辑，无终端 I/O
│   ├── __init__.py
│   ├── agent.py                  # Agent 核心执行大循环
│   ├── context.py                # 对话历史管理与 Token 截断逻辑
│   ├── llm.py                    # 大模型 API 极简封装（OpenAI 兼容接口）
│   ├── system_prompt.py          # 极简系统提示词（<1000 字）
│   ├── exceptions.py             # 自定义异常
│   └── tools/
│       ├── __init__.py
│       ├── base.py               # 工具基类 + ToolResult
│       ├── bash.py               # subprocess 执行，路径限制 + 黑名单
│       ├── read.py               # 安全文件读取
│       ├── write.py              # 全量文件创建/覆写
│       └── edit.py               # search/replace + 行号定位双模式
│
├── cli/                          # 🖥️ 皮肤层 — 终端交互
│   ├── __init__.py
│   ├── main.py                   # CLI 入口
│   ├── ui.py                     # Rich 渲染函数
│   └── bridge.py                 # 桥接层，连接 core.Agent 与终端 I/O
│
└── docs/
    └── superpowers/
        └── specs/
            └── 2026-05-22-simple-coding-agent-design.md
```

### 模块职责

| 模块 | 职责 | 依赖 |
|------|------|------|
| `core/agent.py` | ReAct 主循环：思考→行动→观察 | llm, tools, context |
| `core/llm.py` | OpenAI 兼容 API 调用，流式返回 | 无 |
| `core/context.py` | 消息列表管理、token 估算、历史压缩 | llm（压缩时调用） |
| `core/tools/base.py` | 工具抽象基类 + ToolResult 数据结构 | 无 |
| `core/tools/{read,write,edit,bash}.py` | 四大工具实现 | base |
| `cli/bridge.py` | 持有 Agent 实例，管理输入/输出循环 | core.agent, cli.ui |
| `cli/ui.py` | Rich 终端渲染（Markdown、状态、错误） | Rich 库 |
| `cli/main.py` | 解析参数、加载配置、启动 bridge | bridge |

---

## 4. 核心引擎 — Agent Loop

```
用户输入 → bridge → agent.run(prompt)
                        │
                        ▼
            ┌──────────────────────────┐
            │  while not finished:     │
            │                          │
            │  ① llm.chat(messages)    │  ← 流式返回
            │        │                 │
            │   有 tool_calls?          │
            │   /          \           │
            │ YES           NO         │
            │   │             │         │
            │   ▼             ▼         │
            │  ② 执行工具     产出文本   │
            │  ③ 观察结果 → finished    │
            │  ④ messages.append       │
            │     (含错误反刍)          │
            │  ⑤ 检查 token → 压缩?    │
            │                          │
            └──────────────────────────┘
                        │
                        ▼
            返回最终文本 → bridge → Rich 渲染
```

### 4.1 循环终止条件

模型单次返回的 message 中 `tool_calls` 为空（null/[]）时，循环结束，该 message 的文本内容作为最终回复返回。

### 4.2 错误反刍 (Error Feeding)

工具执行失败时（如 bash 非零 exit_code、文件不存在），不抛异常中断循环。而是将错误信息构造为 `role: "tool"` 的 OpenAI 标准消息 append 到消息列表，让模型看到错误后自行修正。具体格式：

```python
{
    "role": "tool",
    "tool_call_id": "<id>",
    "content": "<stderr 或错误描述>"
}
```

### 4.3 流式回调

LLM 层流式接收 API 响应时，通过 `on_token: Callable[[str], None] | None` 回调将增量文本传给调用方。Core 层不关心回调做什么，CLI 层通过 `bridge.py` 注入 Rich 渲染。思考过程（如 DeepSeek 的 reasoning_content）默认折叠为灰色可展开区块，仅最终可见文本流式打印。

### 4.4 上下文压缩

- **触发条件**：每次工具执行后估算总 token 数，超过模型上下文上限的 80% 时触发。
- **压缩方式**：取消息列表中最早的对话轮次（保留 System Prompt 不动），调用模型生成一段摘要，用摘要替换被压缩的历史消息。
- **保留策略**：System Prompt + 最近 5 轮完整对话始终保持不压缩。

---

## 5. 四大工具

### 5.1 工具基类

```python
class ToolResult:
    success: bool
    content: str          # 成功时的输出
    error: str | None     # 失败时的错误信息

class BaseTool:
    name: str
    description: str
    parameters: dict      # JSON Schema
    async def execute(self, **kwargs) -> ToolResult
```

工具的 `name`、`description`、`parameters` 汇总后注入到 System Prompt 中，作为模型的 function calling 定义。

### 5.2 Read

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file_path` | string | 是 | 绝对路径，内部校验在工作目录内 |
| `offset` | int | 否 | 起始行号，0-indexed |
| `limit` | int | 否 | 读取行数，默认 2000 |

- 返回：带行号前缀的文本（`1\tcode...`）
- 安全：拒绝读取工作目录外的路径

### 5.3 Write

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file_path` | string | 是 | 绝对路径 |
| `content` | string | 是 | 完整文件内容 |

- 直接覆写，不检查文件是否存在
- 返回：文件路径 + 写入行数

### 5.4 Edit

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file_path` | string | 是 | 绝对路径 |
| `old_string` | string | 否 | search/replace 模式的目标字符串 |
| `new_string` | string | 否 | 替换后的字符串 |
| `start_line` | int | 否 | 行号模式起始行 |
| `end_line` | int | 否 | 行号模式结束行 |
| `replace_all` | bool | 否 | 是否替换所有匹配，默认 false |

两种模式：
- **search/replace**：传入 `old_string` + `new_string`。在文件中搜索 `old_string`，替换为 `new_string`。多个匹配时若 `replace_all=false` 则报错并列出匹配位置，提示模型精确化或改用 replace_all。
- **行号定位**：传入 `start_line` + `end_line` + `new_string`。替换指定行范围。
- 两者都传时优先 search/replace。

### 5.5 Bash

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `command` | string | 是 | 待执行的 shell 命令 |

- 通过 `subprocess` 执行，`cwd` 固定为项目工作目录
- 返回：`stdout` + `stderr` + `exit_code`
- 安全机制：
  - **路径限制**：`cwd` 锁定在工作目录
  - **命令黑名单**（正则匹配后拒绝执行）：`rm -rf /`、`sudo`、`chmod 777 /`、fork bomb（`:(){ :|:& };:`）、`mkfs`、`dd if=` 写设备等
  - **超时**：120 秒自动 kill
- 非零 exit_code 不抛异常，通过 ToolResult.error 返回，触发上层错误反刍

---

## 6. CLI 层

### 6.1 main.py

```
sca [--model deepseek-v4-pro] [--dir /path/to/project]
```

- `--model`：覆盖 .env 中的默认模型
- `--dir`：指定 Agent 的工作目录（默认当前目录）
- 启动后进入 REPL 循环

### 6.2 bridge.py

```python
class Bridge:
    def __init__(self, agent: Agent, ui: UI):
        ...
    async def run(self):
        while True:
            user_input = await self.read_input()
            if user_input in ('exit', 'quit'):
                break
            final_text = await self.agent.run(user_input)
            self.ui.render_markdown(final_text)
```

唯一连接 core 的桥接点。持有 Agent 实例，负责：
1. 读取用户键盘输入（支持多行输入）
2. 调用 `agent.run()`
3. 渲染最终结果

### 6.3 ui.py

纯渲染函数，全部基于 Rich 库：

| 函数 | 用途 |
|------|------|
| `render_markdown(text)` | 渲染 Markdown 文本 |
| `render_streaming(text, on_token)` | 流式接收 token，实时渲染 |
| `render_tool_status(name, status)` | 显示工具执行状态（如 "[...] 正在执行 bash..."） |
| `render_error(msg)` | 红色错误提示 |
| `render_thinking(tokens)` | 折叠/展开的思考过程区块 |

### 6.4 流式输出流程

1. `agent.run()` 调用 `llm.chat(messages, on_token=callback)`
2. `llm.chat()` 流式接收 API 响应，对每个 delta token 调用 `on_token`
3. CLI 注入的 callback 将 token 交给 `ui.py` 实时打印
4. 如果 delta 包含 `reasoning_content`（DeepSeek 思考 token），折叠显示；`content` 则正常流式打印

---

## 7. 配置管理

**纯 .env 文件**，不额外引入 yaml/toml 配置。

```env
# .env
SCA_API_KEY=sk-xxx
SCA_API_BASE=https://api.deepseek.com
SCA_MODEL=deepseek-v4-pro
SCA_MAX_TOKENS=128000
SCA_COMPRESSION_THRESHOLD=0.8   # 触发压缩的上下文使用率
```

CLI 层通过 `python-dotenv` 加载，core 层通过构造参数接收具体值（不直接读 .env）。

---

## 8. 技术栈

| 组件 | 选型 | 说明 |
|------|------|------|
| 运行环境 | Python 3.13+ | 利用现代类型提示 |
| LLM API | OpenAI 兼容接口 | 使用 deepseek-v4-pro |
| 终端渲染 | Rich | Markdown + 颜色 + 布局 |
| 依赖管理 | pyproject.toml | PEP 621 标准 |
| 异步 | asyncio | 支撑流式 API 调用 |
| 配置 | python-dotenv | .env 文件解析 |
| HTTP | httpx | 异步 HTTP 客户端 |

---

## 9. 源码文件清单

| 文件 | 预估行数 | 说明 |
|------|----------|------|
| `core/tools/base.py` | ~30 | ToolResult + BaseTool 抽象类 |
| `core/tools/read.py` | ~40 | 文件读取 |
| `core/tools/write.py` | ~30 | 文件写入 |
| `core/tools/edit.py` | ~80 | 双模式精准编辑 |
| `core/tools/bash.py` | ~80 | 安全沙箱命令执行 |
| `core/llm.py` | ~100 | OpenAI 兼容 API 封装 + 流式 |
| `core/context.py` | ~120 | 消息管理 + Token 估算 + 摘要压缩 |
| `core/agent.py` | ~100 | ReAct 主循环 |
| `core/system_prompt.py` | ~50 | 系统提示词纯文本 |
| `core/exceptions.py` | ~15 | 自定义异常 |
| `cli/ui.py` | ~100 | Rich 渲染函数 |
| `cli/bridge.py` | ~80 | 输入/输出桥接 |
| `cli/main.py` | ~40 | CLI 入口 |
| `pyproject.toml` | ~20 | 依赖声明 |

---

## 10. 不在范围内

以下特性明确不做：

- 多模型并行/投票
- 插件系统
- Web/GUI 前端
- 会话持久化（历史保存到磁盘）
- MCP 协议支持
- 自动 git 操作
- 后台 Agent / Cron 任务
- oneshot 单轮模式
