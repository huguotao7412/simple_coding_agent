# Streamlit Web 端架构设计

> 为基于 ReAct 主循环的极简 Coding Agent 增加 Web 交互端，与现有 CLI 并行共存、共用 core。

**目标：** 在不动 CLI 和 core 核心逻辑的前提下，新增 Streamlit Web 端，提供 IDE 级的交互体验（侧边栏文件树 + 对话流 + 工作区管理）。

**核心策略：** 将 `agent.run()` 改造为 async generator（原方法保留），WebBridge 逐 step 消费事件流，注入 `st.session_state` 驱动 UI 渲染。

**技术栈：** Python >=3.13, Streamlit >=1.40, httpx, 现有 core 模块

---

## 一、关键设计决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| Web vs CLI 关系 | 并行共存，`sca` 和 `sca-web` 两条命令 | core 不变，各带各的 bridge |
| Agent 循环改造 | `run_stream()` async generator，逐 step yield `AgentEvent` | 对 core 改动最小，天然适配流式渲染 |
| 工作区管理 | 全局 `SCA_WORKSPACE` 根目录 + 侧边栏子项目切换器 | 与 CLI `--dir` 行为对应，单进程覆盖多项目 |
| 会话持久化 | `st.session_state` 完整保存 messages，刷新后恢复 | 最简单，满足"继续之前对话"需求 |
| 历史会话管理 | 不做历史会话列表/切换 | YAGNI，第一版不需要 |

---

## 二、文件组织

```
web/                        # 新增
├── __init__.py
├── main.py                 # Streamlit 入口 + 页面骨架 + Agent 初始化
├── bridge.py               # WebBridge: agent generator → st.session_state 事件流
├── components/
│   ├── __init__.py
│   ├── sidebar.py          # 文件树 + 项目切换
│   ├── chat.py             # 对话流渲染（思考/tool_call/tool_result）
│   └── diff.py             # unified diff 可视化（difflib 标准库）

core/                       # 微调
├── agent.py                # 新增 run_stream() async generator 方法
└── (其他文件不变)

cli/                        # 不变
```

---

## 三、数据流

```
用户输入 → st.chat_input → st.session_state.messages 追加 user msg
  → WebBridge.handle_user_input(user_input)
    → for await event in agent.run_stream(user_input):
        → 解析 event 类型 (thought / tool_call / tool_result / compaction / done)
        → 压入 st.session_state.events
        → 关键节点 (tool_call / tool_result / done) 触发 st.rerun()
  → chat.py 读取 events 渲染
  → sidebar.py 读取文件系统渲染文件树（与 agent 无关）
  → 右侧 Preview Panel 渲染选中文件内容（与 agent 无关）
```

Agent 生命周期：
- `st.session_state.agent` 缓存，首次加载时 `init_agent()` 创建
- 项目切换时：新的 `workspace_dir` + 重置 `ctx.messages` + 清空 `session_state.messages`

---

## 四、组件详设

### 4.1 AgentEvent（core/agent.py 新增）

```python
@dataclass
class AgentEvent:
    type: str       # "thought" | "tool_call" | "tool_result" | "compaction" | "done"
    content: str = ""
    tool_name: str | None = None
    tool_args: dict | None = None
    tool_result: ToolResult | None = None
    token: str = "" # thought 类型的流式 token
```

### 4.2 Agent.run_stream()（core/agent.py 新增）

- 与 `run()` 逻辑完全一致，差异仅在于每步 `yield AgentEvent` 而非只在最后返回
- 原 `run()` 方法一行不改
- streaming token 通过 `on_token` 回调收集，逐个 yield `thought` 事件
- 压缩时 yield `compaction` 事件
- 工具调用时 yield `tool_call` 事件（执行前）、`tool_result` 事件（执行后）
- 最终回复时 yield `done` 事件

### 4.3 WebBridge（web/bridge.py）

- `init_session(st)`: 初始化 st.session_state 默认值（messages, events, streaming, workspace_root, current_project）
- `handle_user_input(user_input, st)`: 标记 streaming=True → 调用 agent.run_stream() → 压入 events → 在 tool_result/done 时 st.rerun()
- `switch_project(project_name, st)`: 重置 agent workspace_dir + ctx.messages + session_state
- 不 import streamlit，只接收 st 对象作为参数（方便测试）

### 4.4 侧边栏（web/components/sidebar.py）

- 顶部：项目下拉选择器，自动扫描 workspace_root 下的子目录
- 中部：文件树，按顶层目录分组，st.expander 折叠
- 排除：.git .venv __pycache__ node_modules .idea .pytest_cache
- .py 文件优先排在前面
- 返回用户选中（点击）的文件路径供右侧预览

### 4.5 对话面板（web/components/chat.py）

- `render_chat_history()`: 遍历 st.session_state.messages，用 `st.chat_message` 渲染历史
- `render_current_events()`: 遍历当前轮次 events
  - `thought`: 累积连续 token，streaming 中用 st.empty() 占位符实时刷新，非 streaming 直接渲染
  - `tool_call`: 用 `st.status` 包裹，等待对应 tool_result，完成后显示"完成"/"失败"
  - `tool_result`: 根据工具类型选择渲染样式（edit → diff 格式，bash → bash 高亮，read/write → 纯文本）
  - `compaction`: `st.toast` 提示"上下文已压缩"
  - `done`: 最终内容追加到 st.session_state.messages

### 4.6 Diff（web/components/diff.py）

- 依赖：仅标准库 `difflib`
- 输入：old_text, new_text, file_path
- 输出：inline CSS 着色（绿底 + 行 / 红底 - 行 / 蓝字 @@ 行），max 200 行截断

### 4.7 主入口（web/main.py）

- `st.set_page_config` 在最前面
- `init_agent()`: 从 .env 读配置，实例化 LLMClient + ContextManager + 四个 Tool + Agent + WebBridge
- 页面布局：`sidebar | col_main(3) | col_preview(2)`
- `col_main`: 项目标题 + chat_history + current_events + chat_input
- `col_preview`: 选中文件内容，st.code 带语法高亮和行号
- 检测 `project_selector` 变化 → `bridge.switch_project()`

---

## 五、pyproject.toml 变更

```toml
[project]
dependencies = [
    "httpx>=0.28",
    "rich>=13",
    "python-dotenv>=1.0",
    "streamlit>=1.40",          # 新增
]

[project.scripts]
sca = "cli.main:main"
sca-web = "web.main:main"       # 新增

[tool.setuptools.packages.find]
include = ["cli*", "core*", "web*"]  # web* 新增
```

---

## 六、不做什么（显式排除）

- 不做历史会话列表/切换/删除
- 不做拖拽文件上传（工作区就是本地目录）
- 不做实时文件监控（不挂 watchdog）
- 不做 WebSocket/SSE（Streamlit 的 st.rerun() 足够）
- 不做用户认证/多租户
- 不做黑暗模式切换（默认跟随系统）
