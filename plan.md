## 📄 Simple Coding Agent (SCA) 项目书

### 1. 项目定位与哲学 (Philosophy)

**Simple Coding Agent** 是一个基于纯净 ReAct 架构的本地编码辅助智能体。

* **拒绝臃肿：** 不依赖任何重型 Agent 框架，完全由原生 Python 构建核心引擎。
* **极简工具：** 系统提示词控制在千字以内，底层仅暴露 `read`、`write`、`edit`、`bash` 四个原子操作。让大模型通过写代码和执行脚本来解决复杂问题。
* **核心即逻辑：** 严格遵循“大脑（Core）”与“皮肤（CLI）”物理隔离的设计，确保核心代码可以无缝迁移到任何终端或前端界面。

### 2. 核心架构设计 (Architecture)

项目分为两个绝对独立的模块：

* **大脑 (Core Layer)：** 纯粹的逻辑引擎。负责维护 `思考 -> 行动 -> 观察` 循环，处理 API 请求，管理上下文窗口，并在沙盒中安全执行四大基础工具。该层不包含任何终端打印（`print`）或 UI 渲染代码。
* **皮肤 (CLI Layer)：** 终端交互界面。负责读取用户键盘输入，解析命令行参数，并将 Core 层的状态变化转化为漂亮的终端 UI（比如进度条、代码高亮）。

### 3. 关键特性 (Key Features)

1. **原生错误反刍 (Error Feeding)：** 当 `bash` 工具执行报错时，框架不会崩溃，而是将 `stderr` 的报错信息作为 `Observation` 直接喂回给模型，实现模型的**自我纠错**。
2. **局部热修改 (Precision Edit)：** 提供专门的 `edit` 工具，让模型能够通过定位代码块进行精准替换，而不是每次都重写整个数百行的文件。
3. **内存截断防御 (Context Compaction)：** 针对长轮次对话，当 Token 逼近上下文上限时，触发记忆压缩机制，保留 System Prompt 和最近轮次，将历史对话摘要化。

### 4. 技术栈选型 (Tech Stack)

* **运行环境：** 锁定在 **Python 3.13.11**，利用现代 Python 的类型提示（Type Hints）保证代码健壮性。
* **大语言模型：** OpenAI 兼容接口（使用deepseek v4 pro 以保证代码能力）。
* **终端渲染 (CLI)：** `Rich` 库（用于在终端输出漂亮的 Markdown 和控制台颜色）。
* **依赖管理：** 现代 `pyproject.toml` 标准。

---

## 📂 源码文件骨架蓝图

参照 Pi 的 Monorepo 物理隔离思想，我们在 Python 中通过清晰的包（Package）划分来实现类似的效果。

你可以直接在本地创建这样一个目录结构：

```text
simple_coding_agent/
├── pyproject.toml              # 依赖和环境管理
├── .env                        # 存放 API 密钥等敏感配置 (不提交Git)
├── .gitignore
│
├── core/                       # 🧠 【大脑层】核心包 (不可包含任何终端 print)
│   ├── __init__.py
│   ├── agent.py                # Agent 核心执行大循环 (The Loop)
│   ├── context.py              # 对话历史管理与 Token 截断逻辑
│   ├── llm.py                  # 大模型 API 的极简封装层
│   ├── system_prompt.py        # 纯文本：极简的 Agent 初始系统设定
│   ├── exceptions.py           # 自定义异常 (如 ToolExecutionError)
│   │
│   └── tools/                  # 🛠️ 四大基础工具实现
│       ├── __init__.py
│       ├── base.py             # 工具基类，定义 tool_schema 的 JSON 结构
│       ├── bash.py             # 基于 subprocess 的终端命令执行与输出捕获
│       ├── read.py             # 安全的文件读取
│       ├── write.py            # 全量文件创建与写入
│       └── edit.py             # 基于搜索与替换的局部代码修改
│
└── cli/                        # 🖥️ 【皮肤层】终端交互包
    ├── __init__.py
    ├── main.py                 # CLI 启动入口 (解析命令行参数，如 --model)
    ├── ui.py                   # 基于 Rich 库的终端渲染逻辑 (进度条、颜色)
    └── bridge.py               # 桥接层：实例化 core.Agent 并接管用户输入/输出流

```

### 骨架设计解读：

1. **高内聚低耦合：** `core/` 目录下的代码完全不知道自己是在终端跑还是在网页跑，它只负责接收文本、调用模型、执行工具并返回结果。
2. **易于测试：** 你可以非常轻易地在测试脚本中直接 `import core.agent` 来进行自动化测试，而不会被弹出的终端输入框卡住。
3. **扩展性预留：** 如果未来你想给 Agent 增加网页搜索功能，只需要在 `core/tools/` 下新建一个 `search.py`，然后继承 `base.py` 即可。