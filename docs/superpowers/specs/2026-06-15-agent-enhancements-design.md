# Agent 四大增强能力设计文档

**版本**: 1.0  
**日期**: 2026-06-15  
**状态**: 设计阶段

---

## 概述

对 Simple Coding Agent (SCA) 的 ReAct 循环进行四项独立增强：

| 阶段 | 能力 | 目标文件 |
|---|---|---|
| Phase 1 | 动态环境感知注入 | `core/agent.py`, `core/system_prompt.py` |
| Phase 2 | 工具输出截断与 Diff 反馈 | `core/tools/base.py`, `core/tools/bash.py`, `core/tools/read.py`, `core/tools/edit.py` |
| Phase 3 | 死循环熔断 | `core/agent.py` |
| Phase 4 | 工作台账与无损记忆 | `core/context.py`, `core/system_prompt.py` |

四个阶段互相独立，可分别实现和测试。

---

## Phase 1: 动态环境感知注入

### 目标

Agent 启动时自动采集工作区目录结构和运行时环境信息，以 XML 标签注入 System Prompt，让 LLM 在对话开始时即拥有环境上下文。

### 函数设计

#### `get_workspace_tree(workspace_dir: str) -> str`

- **位置**: `core/agent.py` 模块级函数
- **实现策略**: 混合方案
  1. 优先尝试 `subprocess.run(["tree", "-L", "2", "-I", ".git|__pycache__|.venv|node_modules", workspace_dir])`，`capture_output=True, timeout=10`
  2. 命令不可用或失败 → 静默降级到 `_walk_tree_pure_python(workspace_dir, max_depth=2)`，使用 `os.scandir()` 手动生成类 tree 格式输出
  3. Python 兜底会自动忽略 `.git`, `__pycache__`, `.venv`, `node_modules` 目录
- **返回值**: 文本形式的目录树，任何异常都不抛出，保证 Agent 启动不中断

#### `get_runtime_env() -> str`

- **位置**: `core/agent.py` 模块级函数
- **实现**: 纯标准库，无 shell 调用
  - `platform.system()` → 操作系统名称
  - `platform.release()` → 系统版本号
  - `platform.machine()` → 架构 (AMD64 / ARM64)
  - `sys.version` → Python 精确版本 (如 `3.13.2 (tags/v3.13.2:...)`)
- **返回值**: 多行格式化的环境信息字符串
- **异常处理**: 所有 API 调用均不抛异常，无需 try/catch

### System Prompt 组装

在 `Agent.__init__()` 中动态构建 system prompt：

```python
from .system_prompt import SYSTEM_PROMPT

workspace_tree = get_workspace_tree(workspace_dir)
runtime_env = get_runtime_env()
dynamic_prompt = (
    SYSTEM_PROMPT
    + f"\n\n<workspace_context>\n{workspace_tree}\n</workspace_context>"
    + f"\n\n<environment_context>\n{runtime_env}\n</environment_context>"
)
self.ctx = ContextManager(system_prompt=dynamic_prompt, ...)
```

- `core/system_prompt.py` 保持不变的静态文本
- agent.py 负责组装，职责清晰：system_prompt.py 提供"骨架"，agent.py 注入"血肉"

### 变更文件

| 文件 | 改动 |
|---|---|
| `core/agent.py` | 新增 `get_workspace_tree()`, `_walk_tree_pure_python()`, `get_runtime_env()`；修改 `__init__` 组装逻辑 |
| `core/system_prompt.py` | 无改动 |

---

## Phase 2: 工具输出截断与 Diff 观测反馈

### 目标

防止超大输出撑爆 LLM 上下文窗口；让 edit 工具返回语义化的 Unified Diff 而非简单成功提示。

### 通用截断函数

- **位置**: `core/tools/base.py` 模块级函数
- **常量**: `TRUNCATION_THRESHOLD = 8000` (字符)

```python
def truncate_long_output(text: str, threshold: int = TRUNCATION_THRESHOLD) -> str:
    if len(text) <= threshold:
        return text
    keep_head = int(threshold * 0.2)   # 前 20%
    keep_tail = int(threshold * 0.3)   # 后 30%
    omitted = len(text) - keep_head - keep_tail
    return (
        text[:keep_head]
        + f"\n... [Output truncated: {omitted} chars omitted for brevity] ...\n"
        + text[-keep_tail:]
    )
```

### bash.py 改动

- 仅在成功路径截断 stdout（`ToolResult.ok` 之前调用 `truncate_long_output`）
- stderr 不截断（报错通常短且关键）

### read.py 改动

- 在 `ToolResult.ok` 之前对输出调用 `truncate_long_output`

### edit.py 改动

- 引入 `difflib` 标准库
- 执行替换后，对比 `content`(原始) 和 `new_content`(修改后) 生成 Unified Diff
- 返回值改为 diff 字符串，替代 `"Exact match replaced in {file_path}"` / `"Fuzzy match replaced lines..."` 等纯文本提示
- 如果 diff 为空（无实际变化），返回 `"No changes made."`

```python
import difflib

diff = difflib.unified_diff(
    content.splitlines(keepends=True),
    new_content.splitlines(keepends=True),
    fromfile=file_path,
    tofile=file_path,
)
diff_text = "".join(diff)
return ToolResult.ok(diff_text if diff_text else "No changes made.")
```

### 变更文件

| 文件 | 改动 |
|---|---|
| `core/tools/base.py` | 新增 `truncate_long_output()` 函数 + `TRUNCATION_THRESHOLD` 常量 |
| `core/tools/bash.py` | stdout 成功路径调用截断 |
| `core/tools/read.py` | 输出调用截断 |
| `core/tools/edit.py` | 引入 `difflib`，返回 Unified Diff |

---

## Phase 3: 死循环熔断与强制反思

### 目标

当 LLM 反复调用相同的工具+参数组合时，自动熔断跳过执行，注入反思提示，迫使 LLM 改变策略。

### 数据结构

- 在 `Agent.__init__` 中新增 `action_history: deque[int]`，`maxlen=5`

### 核心方法

#### `_hash_action(tool_name: str, args: dict) -> int`

```python
def _hash_action(self, tool_name: str, args: dict) -> int:
    return hash(tool_name + json.dumps(args, sort_keys=True))
```

#### `detect_loop(action_hash: int) -> bool`

```python
def detect_loop(self, action_hash: int) -> bool:
    """当前 hash 在最近 5 次历史中出现 ≥2 次 → 判定为死循环"""
    return sum(1 for h in self.action_history if h == action_hash) >= 2
```

### 注入位置与逻辑

在 `run()` 和 `run_stream()` 的工具执行循环中，**执行前**进行检测：

1. 计算当前 `action_hash`
2. `detect_loop()` 返回 `True` → 跳过 `tool.execute()`，直接向上下文注入系统干预消息
3. 系统干预消息内容：
   ```
   System Alert: Detected repeated failed tool calls. STOP current action.
   Please reason about why it failed and use read or search codebase to gather new context.
   ```
4. 无论熔断还是正常执行，都将 `action_hash` 加入 `action_history`，防止"跳过→LLM重试→再跳过"的二次死循环

### 变更文件

| 文件 | 改动 |
|---|---|
| `core/agent.py` | `__init__` 加 `action_history`；新增 `_hash_action()`, `detect_loop()`；在 `run()` 和 `run_stream()` 工具执行循环中插入熔断逻辑 |

---

## Phase 4: 工作台账与无损记忆分层

### 目标

在上下文压缩时，从旧消息中提取 LLM 生成的工作台账（scratchpad），无损保留到压缩后的消息列表最前端，确保核心工程状态不因有损压缩而丢失。

### Scratchpad 结构

通过 system prompt 指令要求 LLM 维护以下 XML 块（纯提示词层面，无代码改动）：

```xml
<scratchpad>
  <completed_tasks>
    - 已完成事项
  </completed_tasks>
  <current_bugs>
    - 当前遇到的 bug 及排查方向
  </current_bugs>
  <key_files_in_focus>
    - 当前任务涉及的关键文件路径
  </key_files_in_focus>
</scratchpad>
```

### 压缩算法重构

#### 新增方法：`_extract_last_scratchpad(messages) -> str | None`

- 从 `messages[start:end]` 中逆序扫描
- 使用正则 `<scratchpad>.*?</scratchpad>`（`re.DOTALL`）提取**最后一个**出现的 scratchpad 内容
- 未找到返回 `None`

#### 修改方法：`compress()`

现有流程 → 改造后流程：

```
现有:                                             改造后:
messages[start:end] → LLM 摘要 → 插入 system     messages[start:end] → 提取台账 → LLM 摘要
                                                      ↓
                                                  重组:
                                                  [0] system (原始 prompt)
                                                  [1] system (台账, 如有)
                                                  [2] system (摘要)
                                                  [3..] 尾部最新对话
```

### 压缩后记忆分层

```
┌──────────────────────────────────────┐
│ [0] system: SYSTEM_PROMPT            │ ← 静态指令，永不丢失
├──────────────────────────────────────┤
│ [1] system: [Scratchpad]             │ ← 最新台账，无损保留
├──────────────────────────────────────┤
│ [2] system: [Conversation summary]   │ ← 旧对话摘要，有损压缩
├──────────────────────────────────────┤
│ [3..] user/assistant/tool ...        │ ← 最近 N 轮，完整保留
└──────────────────────────────────────┘
```

### 变更文件

| 文件 | 改动 |
|---|---|
| `core/system_prompt.py` | 追加 scratchpad 指令到 SYSTEM_PROMPT |
| `core/context.py` | 新增 `_extract_last_scratchpad()`；修改 `compress()` 重组逻辑 |

---

## 依赖关系

```
Phase 1 ──→ 独立，无依赖
Phase 2 ──→ 独立，无依赖
Phase 3 ──→ 独立，无依赖（虽与 Phase 1 同文件，但改动区域不重叠）
Phase 4 ──→ 独立，无依赖
```

四个阶段可并行或任意顺序实现。

---

## 测试要点

### Phase 1
- `get_workspace_tree()`: 有 `tree` 命令时返回正确结构；无 `tree` 时静默降级不抛异常
- `get_runtime_env()`: 返回字符串包含 OS 名称和 Python 版本号
- System prompt 组装后包含 `<workspace_context>` 和 `<environment_context>` 标签

### Phase 2
- `truncate_long_output()`: 短文本原样返回；长文本截断且插入提示标识
- `bash.py`: 大量 stdout 输出被截断；stderr 不截断
- `read.py`: 大文件读取被截断
- `edit.py`: 返回值是 Unified Diff 格式，包含 `@@` 行号和 `+`/`-` 标记

### Phase 3
- `detect_loop()`: 同一工具+参数连续 2 次触发熔断；不同参数不误判
- 熔断后 LLM 收到系统干预消息而非工具执行结果

### Phase 4
- `_extract_last_scratchpad()`: 正确提取嵌套在长文本中的 scratchpad
- `compress()`: 压缩后台账在摘要之前
- 无 scratchpad 时不插入空消息
