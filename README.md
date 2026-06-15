# Simple Coding Agent (SCA) 🧠💻

Simple Coding Agent (SCA) 是一个基于纯净 **ReAct (Reasoning and Acting)** 架构的本地、轻量级终端及 Web 端编码辅助智能体。项目拒绝引入臃肿复杂的重型 Agent 框架（如 LangChain、LangGraph 的图状态流转），完全由原生 Python 构建。通过极其精简且高度可靠的底层工具链，让大语言模型（完美适配 DeepSeek-V4-Pro、GPT-4o 等）能够直接在本地安全地编写代码、运行终端命令并利用错误反刍（Error Feeding）实现自主纠错与代码修复。

---

## ✨ 核心特性与架构升级

### 1. 🌐 动态环境感知注入 (Dynamic Environment Awareness)
Agent 启动时会自动采集当前工作区的物理目录结构和完整的运行时环境信息：
- **智能工作区目录树**：优先调用系统 `tree` 命令，若不可用则静默降级为纯 Python 原生 `os.scandir()` 递归，自动过滤 `.git`, `.venv`, `__pycache__`, `node_modules` 等无关噪点目录，生成两层深度的直观树状图。
- **环境上下文采集**：精准采集操作系统类型、系统版本号、硬件架构以及当前激活的 Python 准确版本号。
- 以上数据在每轮对话开始前作为 `<workspace_context>` 与 `<environment_context>` 动态注入到 System Prompt 中，赋予 LLM 完美的全局上帝视角。

### 2. 🛠️ 五大原子底层工具 (The Atomic Tools)
项目不堆砌上层业务逻辑，仅提供五个经过精细打磨、边界清晰的原子级能力：
- **`read` (文件读取)**：带有标准行号前缀的文件读取工具，支持 `offset` (起始行) 与 `limit` (最大行数) 分片读取，防止大文件撑爆上下文。
- **`write` (全量写入)**：自动创建中间目录，原子化地创建或覆盖目标文件。
- **`edit` (局部精准修改)**：支持**精确匹配**与**行规范化模糊Fallback匹配**的双级替换引擎。修改完成后利用 `difflib` 自动计算并向模型返回语义化的 **Unified Diff**（包含 `@@`、`+`、`-` 标记），而非盲目提示，极大地提升了模型对修改结果的感知。
- **`bash` (终端执行)**：在工作区目录执行 Shell 命令，捕获 `stdout` 和 `stderr`。内置高危命令黑名单过滤（如 `rm -rf /`、`sudo`、`chmod 777 /` 等），并设置 120 秒强超时保护。
- **`search_codebase` (代码库全局搜索)**：攻克陌生大型项目时的首选利器。支持 `symbol` 模式（通过 Python AST 解析类/函数的签名与 Docstring）与 `text` 模式（通用正则搜索，自动附带命中行前后各 2 行的上下文视窗）。

### 3. 🛡️ 智能输出截断 (Output Truncation)
为了防止 `bash` 执行大量日志（如安装依赖）或 `read` 超长文件导致上下文溢出（Context Overflow），工具层内置了 `truncate_long_output()` 压缩器。当输出字符超过 8000 字阈值时，自动保留前 20%（头部）和后 30%（尾部）的核心内容，并在中间插入省略标识，保护模型上下文的同时留存最关键的报错或结尾信息。

### 4. ⚡ 死循环熔断器 (Loop Detection & Circuit Breaker)
针对 Agent 常见的“死循环旋转”陷阱（如反复以相同参数调用同一失败工具），内置基于行列式 Action Hashing 的状态监控器：
- 记录最近 5 次的工具调用记录。
- 一旦检测到完全相同的 `Tool + Arguments` 组合在历史中出现 $\ge 2$ 次，立即熔断拦截，跳过真实工具执行。
- 强制向模型反向注入系统干预警告（System Alert），迫使其停止机械重复，进入反思模式并转换解题策略。

### 5. 🧠 工作台账无损保留与有损记忆分层 (Hierarchical Memory & Scratchpad)
当总 Token 逼近模型上下文极限阈值（80%）时，触发长文本记忆压缩机制：
- **工作台账提取**：逆序扫描历史消息，通过正则精准提取 LLM 在 `<scratchpad>` 标签中实时维护的工程日志（包含已完成任务、当前 Bug、聚焦的关键文件路径）。
- **无损分层重组**：将这份最新的“工作台账”作为高优先级系统提示词，与静态的 System Prompt 一起**无损保留**在消息列表最前端。
- **有损摘要压缩**：仅将中间积压的旧对话交由 LLM 进行提炼摘要。这种分层设计确保了 Agent 的核心工程记忆绝对不丢失。

---

## 🖥️ 双端皮肤：物理隔离设计

核心大脑逻辑（`core/`）保持纯净，零 `print` 耦合，通过流式生成器 `run_stream()` 对外输出标准事件流（`thought` / `tool_call` / `tool_result` / `compaction` / `done`）。项目原生提供了两套完全解耦的交互皮肤：

### 1. 终端 CLI 交互皮肤 (`sca`)
基于 `Rich` 库打造的沉浸式交互终端：
- 完美的流式 Markdown 渲染。
- **DeepSeek 思考模型原生支持**：流式展示大模型的 `reasoning_content`（思考链），让 Agent 的心智模型完全透明。
- 工具执行状态卡片实时着色（运行中：黄色，成功：绿色，失败：红色）。

### 2. Web 可视化看板 (`sca-web`)
基于 `Streamlit` 构建的 IDE 级单页应用：
- **多项目多开切换**：自动扫描 `SCA_WORKSPACE` 下的子目录，一键无缝切换项目。
- **侧边栏互动文件树**：实时展示项目文件结构，点击任意文件可一键在右侧面板唤起带有行号和语法高亮的文件预览。
- **可视化状态卡片**：流式对话框中优雅折叠展示模型的思考流、工具调用参数、及执行结果（Edit 工具的 Diff 结果将以标准补丁着色高亮展示）。

---

## 🚀 快速开始

### 环境要求
- **Python**: $\ge 3.12$ (推荐 3.12.10)
- **API Key**: 标准 OpenAI 兼容格式的 API Key（默认且强烈推荐使用 DeepSeek API）。

### 安装配置

1. **克隆仓库并进入目录**
   ```bash
   git clone [https://github.com/huguotao7412/simple_coding_agent.git](https://github.com/huguotao7412/simple_coding_agent.git)
   cd simple_coding_agent
   ```
2. **创建虚拟环境并安装项目 (推荐)**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scripts\activate
   pip install -e .
    ```
3. **设置环境变量**
   - 创建 `.env` 文件，添加你的模型 API Key：
   ```bash
   SCA_API_KEY=sk-your-api-key-here
   SCA_API_BASE=https://api.deepseek.com
   SCA_MODEL=deepseek-v4-pro
   SCA_MAX_TOKENS=128000
   SCA_WORKSPACE=./workspaces
   ```
4. **运行 SCA**
   ```bash
   # 在当前目录下启动 Agent
   sca
   
   # 指定工作目录启动
   sca --dir /path/to/your/project
   
   # 临时覆盖使用其他模型
   sca --model gpt-4o
    ```
   启动后，SCA 会进入交互式 REPL 终端。你可以直接用自然语言向它下达开发指令：

   示例指令：

   "帮我在当前目录下初始化一个 React 项目。"
   
   "读取 main.py，帮我把里面的冒泡排序改成快速排序。"
   
   "运行一下 pytest，如果报错了你就自己看报错信息修复它，直到测试全绿为止。"
   
   输入 exit 或 quit 即可退出。

4. **启动 Web 可视化界面**
   ```bash
   # 启动 Streamlit Web 端
   sca-web
   ```
   Web 端提供：
   - 侧边栏项目切换器（自动扫描 SCA_WORKSPACE 下子目录）
   - 文件树浏览器（点击预览文件内容）
   - 流式对话面板（实时展示 Agent 思考过程）
   - 工具调用可视化（执行状态卡片 + 成功/失败着色）
   - 页面刷新后自动恢复会话

   CLI 和 Web 端可以随时切换使用，共享同一套 core 引擎。

5. **目录结构说明**
   ```bash
   simple_coding_agent/
   ├── pyproject.toml              # 项目打包、入口脚本(sca/sca-web)及依赖配置
   ├── .env.example                # 环境变量配置模板
   ├── core/                       # 🧠 【大脑层】纯逻辑处理中心 (零 UI 耦合)
   │   ├── agent.py                # ReAct 核心执行大循环、环境注入与循环熔断控制
   │   ├── context.py              # 对话上下文管理器、Token估算与工作台账硬保留压缩
   │   ├── llm.py                  # 异步 OpenAI 兼容流式 API 客户端（含思考流拆分解析）
   │   ├── system_prompt.py        # 静态系统提示词基座
   │   ├── exceptions.py           # 异常基类定义
   │   └── tools/                  # 🛠️ 原子核心工具集
   │       ├── base.py             # 工具基类与长输出自动截断器
   │       ├── read.py             # 分片带行号读取
   │       ├── write.py            # 物理全量写入
   │       ├── edit.py             # 差异替换与 Unified Diff 发生器
   │       ├── bash.py             # 终端命令安全执行引擎
   │       └── search.py           # AST 符号/正则文本双模检索器
   ├── cli/                        # 🖥️ 【皮肤层】终端交互端
   │   ├── main.py                 # CLI 命令行添置与延迟加载入口
   │   ├── ui.py                   # 基于 Rich 的动态 REPL 界面渲染
   │   └── bridge.py               # 连接大脑 run() 与终端的桥接循环
   └── web/                        # 🌐 【皮肤层】Streamlit Web 交互端
       ├── main.py                 # Streamlit 多栏大看板主页面入口
       ├── cli.py                  # sca-web 脚本触发入口
       ├── bridge.py               # 异步事件流向 Streamlit 状态机的非阻塞桥接
       └── components/             # 前端功能组件
           ├── sidebar.py          # 侧边栏：项目动态扫视 + 联动文件树
           ├── chat.py             # 对话主区：思考链瀑布 + 工具卡片折叠
           └── diff.py             # Diff 补丁红绿变色渲染器
   ```

6. **安全警告**
   SCA 在执行用户指令时拥有高权限的本地 Shell 执行（bash）与文件覆写（write/edit）能力。虽然我们在代码中内置了高危命令黑名单过滤并严禁目录逃逸，但仍强烈建议遵守以下安全策略：

   1.禁止赋予 Sudo 权限：切勿以 root 或 sudo sca 权限运行本程序。
   
   2.版本控制沙盒：务必在初始化了 git 的干净项目根目录下运行 SCA，以便通过 git diff 随时审查 Agent 做出的每一行修改，或者一键 git reset --hard 回滚异常代码。
   
   3.隔离环境（可选）：对于高敏或未知任务，建议将工作目录挂载至 Docker 容器内运行。
