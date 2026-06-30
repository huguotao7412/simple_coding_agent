# Depth Research Agent — 核心架构重构设计文档

**版本**: 2.0
**日期**: 2026-06-30
**状态**: 设计完成，待用户审批

---

## 1. 背景与目标

### 1.1 当前状态

Simple Coding Agent (SCA) 是一个单体 ReAct agent，运行于 `core/agent.py`。核心问题：

- **上下文截断粗糙**：`truncate_long_output` 和 `_truncate_large_messages` 使用掐头去尾的暴力切片，可能破坏代码语法结构
- **Token 估算不准**：`len(text.encode('utf-8')) // 3` 是启发式算法，与实际 token 数偏差大
- **长周期任务状态丢失**：依赖 XML scratchpad + 正则提取在压缩时保留工程台账，但 XML 解析脆弱且非结构化
- **单 Agent 瓶颈**：所有任务串行执行，无法利用并发加速

### 1.2 改造目标

1. 状态管理从 Prompt 约束的 XML 迁移到强类型 Tool 调用
2. 引入精确 Token 计量 + 语义感知降级截断
3. 将单体 Agent 重构为 Planner-Actor 多智能体并发架构
4. 为全中文 UI 提供稳定的底层状态支持

### 1.3 关键决议（已审批）

| # | 决议 | 选项 |
|---|------|------|
| 1 | 架构替换程度 | **完全替换**：所有请求走 Planner → Actors 路径 |
| 2 | Token 计量引擎 | **DeepSeek 官方 tokenizer** (`deepseek-tokenizer`) |
| 3 | 并发隔离策略 | **Git worktree 隔离**：每 Actor 独立 worktree |
| 4 | State 对象定位 | **任务树 + Actor 回传摘要**：State 管调度状态，摘要管知识传递 |
| 5 | System Prompt 语言 | **全部英文** |

---

## 2. 总体架构

### 2.1 三层结构

```
                          ┌──────────────────────────┐
                          │     Planner Agent          │
                          │  (core/planner.py)         │
                          │                            │
                          │  Tools:                    │
                          │  • update_state            │
                          │  • list_dir                │
                          │  • search_codebase         │
                          │  • delegate (dispatch)      │
                          │  • read_outline            │
                          └──────────┬─────────────────┘
                                     │
                          ┌──────────▼─────────────────┐
                          │     GlobalState (Singleton)  │
                          │  core/state.py              │
                          │                             │
                          │  • task_tree                │
                          │  • actor_summaries          │
                          │  • change_log → UI bridge   │
                          └──────────┬─────────────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              │                      │                      │
     ┌────────▼──────┐     ┌────────▼──────┐     ┌────────▼──────┐
     │  ActorAgent   │     │  ActorAgent   │     │  ActorAgent   │
     │  (worktree-1) │     │  (worktree-2) │     │  (worktree-3) │
     │               │     │               │     │               │
     │ Tools:        │     │ Tools:        │     │ Tools:        │
     │ read/write/   │     │ read/write/   │     │ read/write/   │
     │ edit/bash/    │     │ edit/bash/    │     │ edit/bash/    │
     │ search        │     │ search        │     │ search        │
     └───────────────┘     └───────────────┘     └───────────────┘
```

### 2.2 核心数据流

```
User Input
  → Planner.plan() — decompose into task tree
  → Planner calls delegate tool for each subtask batch
  → DelegateTool for each subtask:
      1. Create git worktree (isolated environment)
      2. Construct ActorAgent (inject target context + relevant summaries)
      3. Actor.run() → return structured ActorSummary
      4. Destroy Actor + cleanup worktree
  → Planner collects ActorSummary list, updates GlobalState via update_state tool
  → Planner decides if next round of delegation is needed
  → Planner synthesizes final output for user
```

### 2.3 文件变更总览

| 文件 | 动作 | 阶段 |
|------|------|------|
| `core/state.py` | **新增** | 1 |
| `core/tools/update_state.py` | **新增** | 1 |
| `core/tools/__init__.py` | 修改 — 注册新工具 + 拆离 Actor/Planner 工具集 | 1, 3 |
| `core/context.py` | 修改 — 删除 scratchpad 逻辑，替换 token 估算 | 1, 2 |
| `core/system_prompt.py` | 修改 — 删除 XML scratchpad，拆为 Planner/Actor 双 Prompt | 1, 3 |
| `core/llm.py` | 修改 — 引入 deepseek-tokenizer | 2 |
| `core/tools/base.py` | 修改 — semantic_truncate 替换 truncate_long_output | 2 |
| `core/tools/bash.py` | 修改 — 适配新截断函数 | 2 |
| `core/tools/read.py` | 修改 — 适配新截断函数 | 2 |
| `core/agent.py` | 修改 — 降级为 ActorAgent | 3 |
| `core/planner.py` | **新增** | 3 |
| `core/tools/delegate.py` | **新增** | 3 |
| `cli/bridge.py` | 修改 — Agent → Planner | 3 |
| `cli/main.py` | 修改 — 初始化 GlobalState | 3 |
| `web/bridge.py` | 修改 — Agent → Planner + State 轮询 | 3 |
| `web/components/sidebar.py` | 修改 — 新增任务看板区域 | 1 |
| `requirements.txt` | 修改 — 新增 deepseek-tokenizer | 2 |

---

## 3. 阶段一：状态管理工具化

### 3.1 `core/state.py` — GlobalState 单例

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, ClassVar
from collections import deque


@dataclass
class TaskNode:
    task_id: str
    description: str
    status: Literal["pending", "running", "done", "failed"] = "pending"
    assigned_actor: str | None = None
    dependencies: list[str] = field(default_factory=list)
    result_summary: str | None = None


@dataclass
class ChangeRecord:
    type: str  # "task_added" | "task_updated" | "summary_added"
    task_id: str
    timestamp: float  # time.time()
    payload: dict


class GlobalState:
    _instance: ClassVar[GlobalState | None] = None

    def __init__(self):
        self.task_tree: dict[str, TaskNode] = {}
        self.change_log: list[ChangeRecord] = []
        self._last_consumed: int = 0  # track UI consumption position

    @classmethod
    def get(cls) -> GlobalState:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """For testing only."""
        cls._instance = None

    def add_task(self, description: str, dependencies: list[str] | None = None) -> str:
        import uuid, time
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        self.task_tree[task_id] = TaskNode(
            task_id=task_id,
            description=description,
            dependencies=dependencies or [],
        )
        self.change_log.append(ChangeRecord(
            type="task_added", task_id=task_id,
            timestamp=time.time(), payload={"description": description},
        ))
        return task_id

    def update_task(self, task_id: str, **kwargs) -> None:
        import time
        node = self.task_tree[task_id]
        for key, value in kwargs.items():
            if hasattr(node, key):
                setattr(node, key, value)
        self.change_log.append(ChangeRecord(
            type="task_updated", task_id=task_id,
            timestamp=time.time(), payload=kwargs,
        ))

    def add_summary(self, task_id: str, summary: str) -> None:
        import time
        self.task_tree[task_id].result_summary = summary
        self.change_log.append(ChangeRecord(
            type="summary_added", task_id=task_id,
            timestamp=time.time(), payload={"summary": summary},
        ))

    def consume_changes(self) -> list[ChangeRecord]:
        """Return new changes since last consumption (for UI polling)."""
        new_changes = self.change_log[self._last_consumed:]
        self._last_consumed = len(self.change_log)
        return new_changes

    def snapshot(self) -> dict:
        """Return full serializable snapshot for UI initialization."""
        return {
            "task_tree": {
                tid: {
                    "task_id": t.task_id,
                    "description": t.description,
                    "status": t.status,
                    "assigned_actor": t.assigned_actor,
                    "dependencies": t.dependencies,
                    "result_summary": t.result_summary,
                }
                for tid, t in self.task_tree.items()
            },
            "change_count": len(self.change_log),
        }
```

**设计要点：**
- `change_log` 是 append-only 队列，UI 通过 `consume_changes()` 实现增量更新
- 无并发锁：Planner 是唯一写入者，Actor 不直接写 State
- `reset()` 仅用于测试

### 3.2 `core/tools/update_state.py` — UpdateStateTool

```python
from __future__ import annotations

from .base import BaseTool, ToolResult
from ..state import GlobalState


class UpdateStateTool(BaseTool):
    name = "update_state"
    description = (
        "Update the global engineering ledger. "
        "Use this to add tasks, update task status, or record Actor summaries. "
        "This is the Planner's working memory — keep it current."
    )
    parameters = {
        "action": {
            "type": "string",
            "enum": ["add_task", "update_task", "add_summary"],
            "description": "What kind of state update to perform.",
        },
        "task_id": {
            "type": "string",
            "description": "Target task ID. Required for update_task and add_summary actions.",
        },
        "description": {
            "type": "string",
            "description": "Task description. Required for add_task action.",
        },
        "dependencies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of task_id this task depends on. Optional for add_task.",
        },
        "status": {
            "type": "string",
            "enum": ["pending", "running", "done", "failed"],
            "description": "New status. Required for update_task action.",
        },
        "summary": {
            "type": "string",
            "description": "Actor summary text. Required for add_summary action.",
        },
    }
    required_params = ["action"]

    async def execute(self, action: str, **kwargs) -> ToolResult:
        state = GlobalState.get()

        if action == "add_task":
            description = kwargs.get("description", "")
            if not description:
                return ToolResult.fail("'description' is required for add_task action")
            dependencies = kwargs.get("dependencies", [])
            task_id = state.add_task(description, dependencies)
            return ToolResult.ok(f"Task registered: {task_id}")

        elif action == "update_task":
            task_id = kwargs.get("task_id", "")
            if not task_id:
                return ToolResult.fail("'task_id' is required for update_task action")
            if task_id not in state.task_tree:
                return ToolResult.fail(f"Unknown task_id: {task_id}")
            new_status = kwargs.get("status", "")
            if new_status not in ("pending", "running", "done", "failed"):
                return ToolResult.fail(f"Invalid status: {new_status}")
            state.update_task(task_id, status=new_status)
            return ToolResult.ok(f"Task {task_id} → {new_status}")

        elif action == "add_summary":
            task_id = kwargs.get("task_id", "")
            if not task_id:
                return ToolResult.fail("'task_id' is required for add_summary action")
            if task_id not in state.task_tree:
                return ToolResult.fail(f"Unknown task_id: {task_id}")
            summary = kwargs.get("summary", "")
            if not summary:
                return ToolResult.fail("'summary' is required for add_summary action")
            state.add_summary(task_id, summary)
            state.update_task(task_id, status="done")
            return ToolResult.ok(f"Summary recorded for {task_id}")

        else:
            return ToolResult.fail(f"Unknown action: {action}")
```

### 3.3 `core/context.py` — 瘦身

| 方法/属性 | 改动 |
|-----------|------|
| `_SCRATCHPAD_RE` | **删除** |
| `_extract_last_scratchpad()` | **删除** |
| `compress()` | 删除 scratchpad 提取 + 插入逻辑，仅保留摘要生成和重组 |
| `add_user_message()` | 保留 |
| `add_assistant_message()` | 保留 |
| `add_tool_result()` | 保留 |
| `estimate_tokens()` | 保留但阶段二会替换内部实现 |
| `needs_compression()` | 保留 |
| `get_compressible_range()` | 保留 |
| `_truncate_large_messages()` | 保留但阶段二会替换内部实现 |
| `_close_open_fences()` | 保留 |
| `_reopen_closed_fences()` | 保留 |

### 3.4 `core/system_prompt.py` — 移除 XML scratchpad 指令

删除 SYSTEM_PROMPT 尾部整段 `## Scratchpad (Engineering Ledger)` 及 XML 示例。

替换为（阶段一暂时，阶段三会完全拆分为 Planner/Actor 双 Prompt）：

```
## Engineering Ledger

The Planner maintains the engineering ledger via the `update_state` tool.
Actors track their own temporary bug lists and file focus internally,
and return a structured summary when their subtask completes.
```

### 3.5 `web/components/sidebar.py` — 任务看板

在现有侧边栏文件树下方新增"Task Board"区域：

```
st.sidebar.divider()
st.sidebar.subheader("Tasks")

state = GlobalState.get()
snapshot = state.snapshot()

for task_id, task in snapshot["task_tree"].items():
    status_icon = {"pending": "⏳", "running": "🔄", "done": "✅", "failed": "❌"}
    icon = status_icon.get(task["status"], "❓")

    with st.sidebar.expander(f"{icon} {task['description'][:40]}...", expanded=False):
        st.text(f"Status: {task['status']}")
        if task["result_summary"]:
            st.markdown(task["result_summary"])
```

轮询机制：每 2 秒调用 `state.consume_changes()` 获取增量，触发 Streamlit `st.rerun()` 刷新。

---

## 4. 阶段二：语义级防爆与精准 Token 控制

### 4.1 新增依赖

```
# requirements.txt 追加
deepseek-tokenizer
```

> **实现备注**: 实施时需验证 `deepseek-tokenizer` 的实际 PyPI 包名和 API。
> 若官方包不可用，备选方案为 HuggingFace `tokenizers` + DeepSeek 发布的 tokenizer.json 文件。
> 当前设计假定 `from deepseek_tokenizer import Tokenizer` 接口可用。

### 4.2 `core/llm.py` — 精确 Token 计量

```python
from deepseek_tokenizer import Tokenizer


class LLMClient:
    def __init__(self, ...):
        ...
        self._tokenizer = Tokenizer()

    def count_tokens(self, text: str) -> int:
        """Count tokens in a single string using DeepSeek's tokenizer."""
        return len(self._tokenizer.encode(text))

    def count_messages_tokens(self, messages: list[dict]) -> int:
        """Count tokens across a full messages array.
        Includes per-message format overhead (~4 tokens per message).
        """
        total = 0
        for msg in messages:
            total += 4  # role + formatting overhead
            for key, value in msg.items():
                if isinstance(value, str):
                    total += self.count_tokens(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            total += self.count_tokens(str(item))
        return max(1, total)
```

### 4.3 `core/context.py` — 替换 token 估算

```python
# estimate_tokens 改为接收 llm_client 参数
def estimate_tokens(self, llm_client) -> int:
    return llm_client.count_messages_tokens(self.messages)

# needs_compression 同样需要 llm_client
def needs_compression(self, llm_client) -> bool:
    return self.estimate_tokens(llm_client) > int(
        self.model_context_limit * self.compression_threshold
    )

# compress 调用处传递 self.llm
```

### 4.4 `core/tools/base.py` — 语义感知降级

**删除** `truncate_long_output()` 及 `TRUNCATION_THRESHOLD` 常量。

**新增** `semantic_truncate()`：

```python
import re

# Fallback thresholds in characters (used when tokenizer unavailable)
DEFAULT_TOKEN_BUDGET = 8000
ERROR_PATTERNS = [
    r"(?i)\b(error|exception|traceback|failed|failure|fatal|critical)\b",
    r"(?i)\b(warning|warn|deprecated)\b",
    r"^\s*File\s+\".+?\",\s+line\s+\d+",
    r"^\s*\^+$",
]


def semantic_truncate(
    text: str,
    file_path: str | None = None,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    token_counter=None,
) -> tuple[str, bool]:
    """
    Semantically truncate text to fit within token_budget.

    Returns (truncated_text, was_degraded: bool).

    Degradation levels:
      L0: within budget → return as-is
      L1: code file + over budget → return ReadOutline skeleton view
      L2: non-code file or skeleton still over budget → hybrid smart truncation
    """
    # If no token counter available, estimate via character heuristic (temporary fallback)
    estimated_tokens = token_counter(text) if token_counter else len(text) // 3

    if estimated_tokens <= token_budget:
        return text, False

    # L1: Code file → redirect to outline
    if file_path and file_path.endswith((".py", ".js", ".ts", ".rs", ".go", ".java")):
        # Caller should have already used read_outline; this is a safety net.
        # Return a hint instructing the LLM that the content was too large.
        hint = (
            f"[Content degraded: file {file_path} exceeds token budget. "
            f"Use read_outline to view the skeleton structure, "
            f"or read with offset/limit for specific sections.]"
        )
        return hint, True

    # L2: Non-code or fallback — smart truncation preserving key lines
    lines = text.splitlines()
    head_count = max(1, int(len(lines) * 0.15))
    tail_count = max(1, int(len(lines) * 0.15))

    # Extract error/critical lines from the middle
    error_line_indices = set()
    for i, line in enumerate(lines):
        for pattern in ERROR_PATTERNS:
            if re.search(pattern, line):
                error_line_indices.add(i)
                break

    # Build result: head + error lines + tail
    head = lines[:head_count]
    tail = lines[-tail_count:]

    middle_errors = []
    for i in sorted(error_line_indices):
        if i >= head_count and i < len(lines) - tail_count:
            middle_errors.append(lines[i])

    omitted = len(lines) - len(head) - len(tail) - len(middle_errors)
    marker = f"\n... [{omitted} lines omitted — use read with offset/limit for full content] ...\n"

    result_lines = head
    if middle_errors:
        result_lines.append(f"\n... [key lines from omitted section] ...")
        result_lines.extend(middle_errors)
    result_lines.append(marker)
    result_lines.extend(tail)

    return "\n".join(result_lines), True
```

### 4.5 波及文件修改

| 文件 | 改动 |
|------|------|
| `core/tools/bash.py` | `truncate_long_output(result.content)` → `semantic_truncate(result.content, token_counter=...)` |
| `core/tools/read.py` | 同 bash，传入 `file_path` 以启用 L1 骨架降级 |
| `core/context.py` `_truncate_large_messages()` | 用 `semantic_truncate` 替换当前 fence-aware 掐头去尾逻辑 |

---

## 5. 阶段三：Planner-Actor 并发架构

### 5.1 `core/agent.py` — 降级为 ActorAgent

`Agent` 类重命名为 `ActorAgent`，职责大幅收窄：

| 属性/方法 | 改动 |
|-----------|------|
| `__init__` | 新增 `actor_id: str`, `task_context: str`（Planner 注入），移除 `action_history`（可选保留熔断作为 Actor 内部防护） |
| `run()` | 返回值改为 `ActorSummary`（Dataclass），不再裸返字符串 |
| `run_stream()` | AgentEvent 新增 `actor_id` 字段 |
| 工具集 | 仅接收执行层工具：`read`, `write`, `edit`, `bash`, `search_codebase`, `read_outline`, `list_dir` |
| 生命周期 | 构造 → `run(task_description)` 单次执行 → 返回摘要 → 实例丢弃 |

**ActorSummary 结构：**

```python
@dataclass
class ActorSummary:
    task_id: str
    status: Literal["done", "failed"]
    files_modified: list[str]
    bugs_found: list[str]
    key_findings: str           # structured prose
    suggested_next_steps: str   # hints for Planner
    raw_output: str             # full output for user inspection (not stored in State)
```

### 5.2 `core/planner.py` — Planner Agent

Planner 是新的顶层入口，也是一个 ReAct agent，但拥有的是调度层工具而非执行层工具。

```python
class Planner:
    def __init__(
        self,
        llm_client: LLMClient,
        context_manager: ContextManager,
        tools: list[BaseTool],   # PLANNER_TOOLS
        workspace_dir: str,
    ):
        self.llm = llm_client
        self.ctx = context_manager
        self.tools_by_name = {t.name: t for t in tools}
        self.workspace_dir = workspace_dir

    async def run(self, user_input: str, on_token=None) -> str:
        # Standard ReAct loop, but "actions" are:
        #   - update_state (maintain task tree)
        #   - delegate (dispatch Actors)
        #   - list_dir / search_codebase / read_outline (understand context)
        # Planner NEVER calls write/edit/bash directly.
        ...
        # When no more tool calls, return synthesized final answer.
        return final_content

    async def run_stream(self, user_input: str) -> AsyncGenerator[AgentEvent, None]:
        # Streaming variant. New event types:
        #   "planning" — Planner is decomposing the task
        #   "delegating" — Planner is dispatching Actors
        #   "actor_update" — Actor task status changed
        ...
```

**Planner 工具集：**

```python
PLANNER_TOOLS = [
    UpdateStateTool,
    DelegateTool,
    ListDirTool,
    SearchTool,
    ReadOutlineTool,
]
```

### 5.3 `core/tools/delegate.py` — DelegateTool

核心并发调度工具。

```python
MAX_CONCURRENT_ACTORS = 4  # configurable


class DelegateTool(BaseTool):
    name = "delegate"
    description = (
        "Dispatch multiple subtasks to independent Actor agents for concurrent execution. "
        "Each Actor runs in an isolated git worktree. "
        "Use this after you have decomposed a complex task into independent subtasks. "
        "Returns structured summaries from each Actor."
    )
    parameters = {
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID from the global state (register via update_state first).",
                    },
                    "description": {
                        "type": "string",
                        "description": "Specific, actionable task description for the Actor.",
                    },
                    "context_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File paths to inject into the Actor's context.",
                    },
                    "context_summaries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Relevant summary snippets from previous Actors.",
                    },
                },
                "required": ["task_id", "description"],
            },
        }
    }
    required_params = ["subtasks"]
```

**`execute()` 核心逻辑：**

```
1. Validate all task_ids exist in GlobalState
2. For each subtask:
   a. Mark status → "running" via GlobalState
   b. Create git worktree via `git worktree add`
   c. Build ActorAgent with:
      - task_description = subtask.description
      - context_files → pre-read and inject into initial messages
      - context_summaries → inject as system-level context
   d. Wrap ActorAgent.run() in a coroutine
3. semaphore = asyncio.Semaphore(MAX_CONCURRENT_ACTORS)
4. results = await asyncio.gather(*coroutines)  # each wrapped with semaphore
5. For each completed Actor:
   a. Record ActorSummary via GlobalState.add_summary()
   b. If Actor status is "done" and worktree has changes:
      - Keep worktree on disk; note modified files in summary
      - Planner evaluates summaries and decides merge strategy:
        - For independent file edits: `git merge` each worktree branch sequentially
        - For overlapping file edits: report conflict to user, request manual resolution
        - For analysis-only tasks (no file changes): discard worktree immediately
   c. If Actor status is "failed" or worktree has no changes:
      - `git worktree remove --force` + `git branch -D actor-<id>`
6. Return aggregated result: list of {task_id, status, summary_snippet}
```

**Worktree 管理细节：**

- Worktree 路径：`<workspace>/.claude/worktrees/<actor_id>/`
- 创建：`git worktree add -b actor-<id> <path> <base_branch>`
- 清理：`git worktree remove --force <path>` + `git branch -D actor-<id>`
- 异常处理：delegate 工具 finally 块确保 worktree 清理，即使 Actor 崩溃

### 5.4 `core/system_prompt.py` — 双 Prompt 拆分

删除原有单体 `SYSTEM_PROMPT`，拆为两个独立 Prompt（**全部英文**）：

```python
PLANNER_SYSTEM_PROMPT = """You are Depth Research Agent (Planner mode), a task orchestration agent.

You solve complex programming tasks by decomposing them into subtasks and delegating
execution to isolated Actor agents. You do NOT write code, edit files, or run shell
commands yourself — you orchestrate.

## Your Workflow
1. **Analyze** the user's request and understand the full scope.
2. **Decompose** into independent subtasks. Register each via `update_state` (add_task).
3. **Delegate** batches of subtasks to Actors via the `delegate` tool.
   - Actors are stateless and isolated in git worktrees.
   - Inject only the specific context each Actor needs (relevant files, prior summaries).
4. **Evaluate** Actor summaries. If new issues or follow-ups are needed, create and
   delegate additional rounds of subtasks.
5. **Synthesize** a final answer for the user once all subtasks are resolved.

## Tools
- **update_state**: Maintain the task tree and record Actor summaries.
- **delegate**: Dispatch subtasks to Actors for concurrent execution.
- **list_dir**: Explore project structure.
- **search_codebase**: Locate symbols, classes, functions, or text patterns.
- **read_outline**: View skeleton structure of large files.

## Rules
- Register tasks via `update_state` BEFORE delegating them.
- Group independent subtasks into a single `delegate` call for maximum concurrency.
- Inject only essential context into each Actor — less noise = better results.
- When an Actor reports bugs or blockers, analyze them before spawning follow-up Actors.
- Prefer reading outlines before reading full files when scoping a task.
"""

ACTOR_SYSTEM_PROMPT = """You are Depth Research Agent (Actor mode), a task execution agent.

You execute a SINGLE, specific subtask assigned by the Planner. You operate in an
isolated git worktree. After completing your task, return a structured summary.
Do NOT plan next steps — the Planner handles that.

## Tools
- **read**: Read file contents with line numbers.
- **write**: Create or overwrite a file.
- **edit**: Make precise edits using search/replace blocks.
- **bash**: Execute shell commands (background/long-running via action="background").
- **search_codebase**: Find symbols (classes/functions) or text patterns.
- **list_dir**: List directory contents.
- **read_outline**: View skeleton structure of large files.

## Rules
- Work only within your assigned worktree directory.
- Read a file before editing it.
- Prefer `edit` over `write` for small changes.
- When you encounter errors, read the error message and fix the problem yourself.
- For background servers: start with action="background", verify, then kill.
- Return a structured summary when done — do NOT chain into unrelated work.
"""
```

### 5.5 皮肤层适配

#### `cli/main.py`

```python
# Before:
agent = Agent(llm_client, context_manager, tools, workspace_dir)
await agent.run(user_input, on_token=callback)

# After:
state = GlobalState.get()
planner = Planner(llm_client, context_manager, PLANNER_TOOLS, workspace_dir)
await planner.run(user_input, on_token=callback)
```

#### `cli/bridge.py`

`agent.run()` → `planner.run()`；新增 AgentEvent 类型处理（`planning`, `delegating`, `actor_update`）。

#### `web/bridge.py`

同 CLI 的 Planner 切换 + 新增后台协程轮询 `GlobalState.consume_changes()` 推送到前端。

### 5.6 `core/tools/__init__.py` — 工具集拆离

```python
from .read import ReadTool
from .write import WriteTool
from .edit import EditTool
from .bash import BashTool
from .search import SearchTool
from .list_dir import ListDirTool
from .read_outline import ReadOutlineTool
from .update_state import UpdateStateTool
from .delegate import DelegateTool

# Actor tools (execution layer)
ACTOR_TOOLS = [
    ReadTool, WriteTool, EditTool, BashTool,
    SearchTool, ReadOutlineTool, ListDirTool,
]

# Planner tools (orchestration layer)
PLANNER_TOOLS = [
    UpdateStateTool, DelegateTool, ListDirTool,
    SearchTool, ReadOutlineTool,
]
```

---

## 6. 错误处理

### 6.1 Actor 故障隔离

- 单个 Actor 崩溃不影响其他并发 Actor（`asyncio.gather` 默认不取消其他）
- 崩溃 Actor 返回 `ActorSummary(status="failed", ...)` 包含错误信息
- Planner 收到失败摘要后决定：重试 / 创建替代子任务 / 向用户报告

### 6.2 Worktree 泄漏防护

- `delegate` 工具的 `finally` 块确保 worktree 清理
- 异常时记录 worktree 路径，下次启动时清理孤儿 worktree

### 6.3 Token 预算保护

- `semantic_truncate` 确保单次工具返回值不超预算
- `needs_compression` 使用精确计量防止上下文溢出

### 6.4 死循环熔断

- Actor 内部保留轻量熔断（3 次相同调用 → 拦截）
- Planner 层面：连续 3 轮派发无进展 → 终止并向用户说明

---

## 7. 测试策略

### 7.1 单元测试

| 模块 | 测试内容 |
|------|---------|
| `core/state.py` | GlobalState 单例、add_task/update_task/add_summary、change_log 增量消费 |
| `core/tools/update_state.py` | 三种 action 的 Schema 校验、错误路径 |
| `core/llm.py` `count_tokens()` | 与已知 token 数的文本对比验证 |
| `core/tools/base.py` `semantic_truncate()` | L0/L1/L2 三级降级路径 |

### 7.2 集成测试

| 场景 | 验证点 |
|------|--------|
| Planner 单任务派发 | 1 个 Actor 正确执行并回传摘要 |
| Planner 并发派发 | 3 个 Actor 并发执行，worktree 不冲突 |
| Actor 故障恢复 | 1 个 Actor 崩溃，其余继续，Planner 收到失败摘要 |
| Token 压缩 | 长上下文触发 semantic_truncate，不破坏语法结构 |
| Worktree 清理 | delegate 完成后无残留 worktree |

### 7.3 端到端测试

- 完整"深度研究"任务：用户提问 → Planner 拆解 → 3 轮 Actor 派发 → 最终汇总

---

## 8. 实现依赖与顺序

```
Phase 1 (State Tooling)
  ├── core/state.py              [NEW]
  ├── core/tools/update_state.py [NEW]
  ├── core/tools/__init__.py     [MODIFY]
  ├── core/context.py            [MODIFY — remove scratchpad]
  ├── core/system_prompt.py      [MODIFY — remove XML section]
  └── web/components/sidebar.py  [MODIFY — task board]

Phase 2 (Token Precision) — depends on Phase 1
  ├── core/llm.py                [MODIFY — tokenizer]
  ├── core/context.py            [MODIFY — estimate_tokens]
  ├── core/tools/base.py         [MODIFY — semantic_truncate]
  ├── core/tools/bash.py         [MODIFY — adapt]
  ├── core/tools/read.py         [MODIFY — adapt]
  └── requirements.txt           [MODIFY — deepseek-tokenizer]

Phase 3 (Planner-Actor) — depends on Phase 1, can parallel with Phase 2
  ├── core/agent.py              [MODIFY — Agent → ActorAgent]
  ├── core/planner.py            [NEW]
  ├── core/tools/delegate.py     [NEW]
  ├── core/tools/__init__.py     [MODIFY — split tool sets]
  ├── core/system_prompt.py      [MODIFY — split to Planner/Actor]
  ├── cli/bridge.py              [MODIFY]
  ├── cli/main.py                [MODIFY]
  └── web/bridge.py              [MODIFY]
```

Phase 1 必须先完成（State 是 Phase 3 Planner-Actor 通信协议的基础）。
Phase 2 和 Phase 3 可并行开发，但 Phase 2 的部分修改与 Phase 3 同文件（如 `context.py`、`tools/__init__.py`），建议 Phase 2 先行或合并到一个 feature branch 中顺序提交。
