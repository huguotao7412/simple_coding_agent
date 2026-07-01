# Depth Research Agent 核心架构重构 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将单体 ReAct Agent 彻底重构为 Planner-Actor 多智能体并发架构，引入精确 Token 计量与语义截断，并将工程台账从 XML 迁移到强类型 Tool 调用。

**Architecture:** Planner (调度层) 接收用户输入、拆解为任务树、通过 GlobalState 管理进度、使用 DelegateTool 在 git worktree 中并发派发 ActorAgent (执行层)。Actor 执行完毕后回传结构化摘要并销毁。

**Tech Stack:** Python 3.12+, asyncio, deepseek-tokenizer, git worktree, Streamlit (Web UI), Rich (CLI UI)

## Global Constraints

- 所有 System Prompt 使用英文
- `deepseek-tokenizer` 作为精确 Token 计量引擎；若官方包不可用，备选 HuggingFace `tokenizers` + DeepSeek tokenizer.json
- Actor 并发隔离策略：git worktree
- GlobalState 定位：任务树 + Actor 回传摘要（Planner 唯一写入者）
- 完全替换现有 Agent：所有请求走 Planner → Actors 路径
- 并发上限：MAX_CONCURRENT_ACTORS = 4

---

## 文件结构总览

| 文件 | 动作 | 职责 |
|------|------|------|
| `core/state.py` | **新增** | GlobalState 单例：任务树 + 变更日志 + UI 消费接口 |
| `core/tools/update_state.py` | **新增** | UpdateStateTool：Planner 写入全局状态的唯一入口 |
| `core/planner.py` | **新增** | Planner Agent：任务拆解、Actor 调度、结果汇总 |
| `core/tools/delegate.py` | **新增** | DelegateTool：asyncio.gather + worktree 并发调度 |
| `core/agent.py` | 修改 | Agent → ActorAgent：无状态纯执行单元 |
| `core/tools/__init__.py` | 修改 | 注册新工具 + 拆离 ACTOR_TOOLS / PLANNER_TOOLS |
| `core/context.py` | 修改 | 删除 scratchpad 逻辑 + 替换 token 估算 |
| `core/system_prompt.py` | 修改 | 删除 XML scratchpad → 拆为 PLANNER / ACTOR 双 Prompt |
| `core/llm.py` | 修改 | 引入 deepseek-tokenizer + count_tokens/count_messages_tokens |
| `core/tools/base.py` | 修改 | truncate_long_output → semantic_truncate |
| `core/tools/bash.py` | 修改 | 适配 semantic_truncate |
| `core/tools/read.py` | 修改 | 适配 semantic_truncate + 传入 file_path |
| `cli/main.py` | 修改 | Agent → Planner + GlobalState 初始化 |
| `cli/bridge.py` | 修改 | 事件类型适配 |
| `web/main.py` | 修改 | Agent → Planner + GlobalState 初始化 |
| `web/bridge.py` | 修改 | Planner + GlobalState 轮询 |
| `web/components/sidebar.py` | 修改 | 新增任务看板 |
| `requirements.txt` | 修改 | 新增 deepseek-tokenizer |

---

### Task 1: 新增 `core/state.py` — GlobalState 单例

**Files:**
- Create: `core/state.py`

**Interfaces:**
- Produces: `GlobalState.get() -> GlobalState`, `GlobalState.reset() -> None`, `add_task(description, dependencies) -> str`, `update_task(task_id, **kwargs) -> None`, `add_summary(task_id, summary) -> None`, `consume_changes() -> list[ChangeRecord]`, `snapshot() -> dict`, `TaskNode`, `ChangeRecord`

- [ ] **Step 1: Write the file**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, ClassVar


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
    timestamp: float
    payload: dict


class GlobalState:
    _instance: ClassVar[GlobalState | None] = None

    def __init__(self):
        self.task_tree: dict[str, TaskNode] = {}
        self.change_log: list[ChangeRecord] = []
        self._last_consumed: int = 0

    @classmethod
    def get(cls) -> GlobalState:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def add_task(self, description: str, dependencies: list[str] | None = None) -> str:
        import uuid
        import time
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
        new_changes = self.change_log[self._last_consumed:]
        self._last_consumed = len(self.change_log)
        return new_changes

    def snapshot(self) -> dict:
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

- [ ] **Step 2: Verify import works**

Run: `python -c "from core.state import GlobalState, TaskNode, ChangeRecord; s = GlobalState.get(); tid = s.add_task('test task'); print(tid); s.update_task(tid, status='done'); print(s.snapshot())"`

Expected: Prints task_id and snapshot dict with done status.

- [ ] **Step 3: Commit**

```bash
git add core/state.py
git commit -m "feat(state): add GlobalState singleton with task tree and change log"
```

---

### Task 2: 新增 `core/tools/update_state.py` — UpdateStateTool

**Files:**
- Create: `core/tools/update_state.py`

**Interfaces:**
- Consumes: `BaseTool`, `ToolResult` from `core.tools.base`; `GlobalState` from `core.state`
- Produces: `UpdateStateTool(name="update_state")` with 3 actions: `add_task`, `update_task`, `add_summary`

- [ ] **Step 1: Write the file**

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
            return ToolResult.ok(f"Task {task_id} -> {new_status}")

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

- [ ] **Step 2: Verify tool schema generates correctly**

Run: `python -c "from core.tools.update_state import UpdateStateTool; import json; t = UpdateStateTool(); print(json.dumps(t.schema, indent=2, ensure_ascii=False))"`

Expected: Prints valid JSON Schema with `action` enum and all parameters.

- [ ] **Step 3: Verify execute — happy paths**

Run: `python -c "
import asyncio
from core.tools.update_state import UpdateStateTool
from core.state import GlobalState
GlobalState.reset()

async def test():
    t = UpdateStateTool()
    r1 = await t.execute(action='add_task', description='Test task')
    print('add_task:', r1.success, r1.content)
    tid = r1.content.split(': ')[1]
    r2 = await t.execute(action='update_task', task_id=tid, status='running')
    print('update_task:', r2.success, r2.content)
    r3 = await t.execute(action='add_summary', task_id=tid, summary='All done')
    print('add_summary:', r3.success, r3.content)

asyncio.run(test())
"`

Expected: All three actions succeed. verify task tree reflects changes.

- [ ] **Step 4: Verify execute — error paths**

Run: `python -c "
import asyncio
from core.tools.update_state import UpdateStateTool
from core.state import GlobalState
GlobalState.reset()

async def test():
    t = UpdateStateTool()
    r1 = await t.execute(action='add_task')  # missing description
    print('missing desc:', r1.success, r1.error)
    r2 = await t.execute(action='update_task', task_id='nonexistent', status='done')
    print('bad task_id:', r2.success, r2.error)
    r3 = await t.execute(action='update_task')
    print('missing task_id:', r3.success, r3.error)
    r4 = await t.execute(action='add_summary')
    print('missing args:', r4.success, r4.error)
    r5 = await t.execute(action='unknown_action')
    print('bad action:', r5.success, r5.error)

asyncio.run(test())
"`

Expected: All return `success=False` with appropriate error messages.

- [ ] **Step 5: Commit**

```bash
git add core/tools/update_state.py
git commit -m "feat(tools): add UpdateStateTool for structured state management"
```

---

### Task 3: 修改 `core/tools/__init__.py` — 注册 update_state

**Files:**
- Modify: `core/tools/__init__.py`

**Interfaces:**
- Produces: Exports `UpdateStateTool` alongside existing tools

- [ ] **Step 1: Read current file and replace**

Current `core/tools/__init__.py` is empty. Replace with:

```python
from .read import ReadTool
from .write import WriteTool
from .edit import EditTool
from .bash import BashTool
from .search import SearchCodebaseTool
from .list_dir import ListDirTool
from .read_outline import ReadOutlineTool
from .update_state import UpdateStateTool

__all__ = [
    "ReadTool",
    "WriteTool",
    "EditTool",
    "BashTool",
    "SearchCodebaseTool",
    "ListDirTool",
    "ReadOutlineTool",
    "UpdateStateTool",
]
```

- [ ] **Step 2: Verify imports**

Run: `python -c "from core.tools import ReadTool, UpdateStateTool; print('OK')"`

Expected: Prints `OK`.

- [ ] **Step 3: Commit**

```bash
git add core/tools/__init__.py
git commit -m "feat(tools): register UpdateStateTool in tools __init__"
```

---

### Task 4: 修改 `core/context.py` — 删除 scratchpad 逻辑

**Files:**
- Modify: `core/context.py`

**Interfaces:**
- Removes: `_SCRATCHPAD_RE`, `_extract_last_scratchpad()`
- Modifies: `compress()` — removes scratchpad extraction and re-insertion

- [ ] **Step 1: Remove `_SCRATCHPAD_RE` class attribute**

Delete line 25:
```python
# DELETE:
_SCRATCHPAD_RE = re.compile(r"<scratchpad>.*?</scratchpad>", re.DOTALL)
```

Also remove `import re` at line 3 if no longer needed (check: `_XML_TAG_RE`, `_FENCE_RE` still use `re` — keep the import).

- [ ] **Step 2: Remove `_extract_last_scratchpad` classmethod**

Delete lines 27-41 (the entire method).

- [ ] **Step 3: Simplify `compress()` method**

Replace the current `compress()` method (lines 110-158) with a version that removes all scratchpad extraction and re-insertion:

```python
    async def compress(self, llm_client) -> None:
        start, end = self.get_compressible_range()
        if start >= end:
            self._truncate_large_messages(self.messages)
            return

        messages_to_drop = self.messages[start:end]

        # Build slim summary entries
        slim_entries: list[str] = []
        for m in messages_to_drop:
            role = m["role"]
            raw = (m.get("content") or "")
            stripped = re.sub(r"```[^`]*```", "[code block omitted]", raw, flags=re.DOTALL)
            if role == "tool":
                snippet = stripped[:80].replace("\n", " ")
                slim_entries.append(f"[{role}]: {snippet}...")
            else:
                snippet = stripped[:150].replace("\n", " ")
                slim_entries.append(f"[{role}]: {snippet}")
        summary_prompt = (
            "Summarize the following conversation history concisely, "
            "preserving key file paths, decisions, and bugs:\n\n"
            + "\n".join(slim_entries)
        )

        try:
            result = await llm_client.chat([{"role": "user", "content": summary_prompt[:8000]}])
            summary = result.get("content", "Previous conversation summarized.")
        except Exception:
            summary = "(Conversation compressed but summary failed due to error.)"

        tail = self.messages[end:]
        self._truncate_large_messages(tail)

        new_messages = self.messages[:start]
        new_messages.append({"role": "system", "content": f"[Conversation summary]: {summary}"})
        new_messages.extend(tail)
        self.messages = new_messages
```

Key changes from original:
- Removed `saved_scratchpad = self._extract_last_scratchpad(messages_to_drop)` 
- Removed `has_newer_scratchpad` check
- Removed scratchpad re-insertion block
- Directly appends summary system message after prefix messages

- [ ] **Step 4: Verify file still imports and ContextManager initializes**

Run: `python -c "from core.context import ContextManager; c = ContextManager(system_prompt='test'); print('OK')"`

Expected: Prints `OK`.

- [ ] **Step 5: Commit**

```bash
git add core/context.py
git commit -m "refactor(context): remove scratchpad extraction, state now managed via UpdateStateTool"
```

---

### Task 5: 修改 `core/system_prompt.py` — 删除 XML scratchpad 指令

**Files:**
- Modify: `core/system_prompt.py`

- [ ] **Step 1: Remove scratchpad section from SYSTEM_PROMPT**

Delete lines 38-55 (the entire `## Scratchpad (Engineering Ledger)` section and its XML example).

Replace with:

```python
## Engineering Ledger

The Planner maintains the engineering ledger via the `update_state` tool.
Actors track their own temporary bug lists and file focus internally,
and return a structured summary when their subtask completes.
```

- [ ] **Step 2: Verify file still imports**

Run: `python -c "from core.system_prompt import SYSTEM_PROMPT; assert 'Scratchpad' not in SYSTEM_PROMPT; assert 'update_state' in SYSTEM_PROMPT; print('OK')"`

Expected: Prints `OK`.

- [ ] **Step 3: Commit**

```bash
git add core/system_prompt.py
git commit -m "refactor(prompt): remove XML scratchpad, reference update_state tool instead"
```

---

### Task 6: 修改 `web/components/sidebar.py` — 新增任务看板

**Files:**
- Modify: `web/components/sidebar.py`

**Interfaces:**
- Consumes: `GlobalState` from `core.state`

- [ ] **Step 1: Add Task Board section to sidebar**

Insert after the file tree section (after line 75, before the `return selected_file`):

```python
    # --- Task Board ---
    from core.state import GlobalState

    st.sidebar.divider()
    st.sidebar.subheader("Tasks")

    state = GlobalState.get()
    snapshot = state.snapshot()
    tasks = snapshot.get("task_tree", {})

    if not tasks:
        st.sidebar.caption("(no active task tree)")
    else:
        status_icon = {"pending": "⏳", "running": "🔄", "done": "✅", "failed": "❌"}
        for task_id, task in tasks.items():
            icon = status_icon.get(task["status"], "❓")
            desc_display = task["description"][:40] + ("..." if len(task["description"]) > 40 else "")
            with st.sidebar.expander(f"{icon} {desc_display}", expanded=False):
                st.caption(f"Status: {task['status']}")
                if task.get("result_summary"):
                    st.markdown(task["result_summary"])
```

Note: The `return selected_file` statement at line 75 must become `return selected_file` after the new block — ensure the task board code is inserted BEFORE the return.

- [ ] **Step 2: Verify the file still imports**

Run: `python -c "from web.components.sidebar import render_sidebar; print('OK')"`

Expected: Prints `OK`.

- [ ] **Step 3: Commit**

```bash
git add web/components/sidebar.py
git commit -m "feat(web): add Task Board panel to sidebar"
```

---

### Task 7: 修改 `requirements.txt` — 新增 deepseek-tokenizer

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add dependency**

Append to `requirements.txt`:

```
deepseek-tokenizer
```

- [ ] **Step 2: Install and verify**

Run: `pip install deepseek-tokenizer`

If the package is not available on PyPI, note this and use the fallback approach (HuggingFace `tokenizers` + DeepSeek tokenizer.json). Update the plan accordingly.

Expected: Package installs successfully, OR we identify the correct package name.

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "chore(deps): add deepseek-tokenizer for precise token counting"
```

---

### Task 8: 修改 `core/llm.py` — 引入精确 Token 计量

**Files:**
- Modify: `core/llm.py`

**Interfaces:**
- Produces: `LLMClient.count_tokens(text: str) -> int`, `LLMClient.count_messages_tokens(messages: list[dict]) -> int`

- [ ] **Step 1: Add tokenizer import and initialization**

At the top of `core/llm.py`, add:

```python
from deepseek_tokenizer import Tokenizer
```

If the package name differs, use the correct import. If unavailable, add a fallback:

```python
try:
    from deepseek_tokenizer import Tokenizer
except ImportError:
    Tokenizer = None
```

- [ ] **Step 2: Add `_tokenizer` to `__init__`**

In `LLMClient.__init__`, add after `self.max_tokens = max_tokens`:

```python
        if Tokenizer is not None:
            self._tokenizer = Tokenizer()
        else:
            self._tokenizer = None
```

- [ ] **Step 3: Add `count_tokens` method**

Add after `__init__`:

```python
    def count_tokens(self, text: str) -> int:
        """Count tokens in a single string using DeepSeek's tokenizer.
        Falls back to character heuristic if tokenizer unavailable.
        """
        if self._tokenizer is not None:
            return len(self._tokenizer.encode(text))
        # Fallback heuristic
        return max(1, len(text.encode("utf-8", errors="ignore")) // 3)
```

- [ ] **Step 4: Add `count_messages_tokens` method**

Add after `count_tokens`:

```python
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

- [ ] **Step 5: Verify token counting works**

Run: `python -c "
from core.llm import LLMClient
llm = LLMClient(api_key='test', model='deepseek-v4-pro')
t = llm.count_tokens('Hello world')
print(f'Tokens in \"Hello world\": {t}')
msgs = [{'role': 'user', 'content': 'Hello world'}]
mt = llm.count_messages_tokens(msgs)
print(f'Tokens in messages: {mt}')
print('OK')
"`

Expected: Prints token counts (e.g., 2-4 for "Hello world") and `OK`. No exceptions.

- [ ] **Step 6: Commit**

```bash
git add core/llm.py
git commit -m "feat(llm): add precise token counting via deepseek-tokenizer"
```

---

### Task 9: 修改 `core/context.py` — 替换 estimate_tokens

**Files:**
- Modify: `core/context.py`

**Interfaces:**
- Modifies: `estimate_tokens(self) -> int` → `estimate_tokens(self, llm_client) -> int`
- Modifies: `needs_compression(self) -> bool` → `needs_compression(self, llm_client) -> bool`

- [ ] **Step 1: Replace `estimate_tokens`**

Replace the current `estimate_tokens` (lines 66-77) with:

```python
    def estimate_tokens(self, llm_client) -> int:
        """Precise token count using LLM client's tokenizer."""
        return llm_client.count_messages_tokens(self.messages)
```

- [ ] **Step 2: Replace `needs_compression`**

Replace the current `needs_compression` (lines 79-80) with:

```python
    def needs_compression(self, llm_client) -> bool:
        return self.estimate_tokens(llm_client) > int(
            self.model_context_limit * self.compression_threshold
        )
```

- [ ] **Step 3: Update `run()` and `run_stream()` callers in `core/agent.py`**

The calls `self.ctx.needs_compression()` and `self.ctx.compress(self.llm)` in `agent.py` need updating: `needs_compression()` now requires `llm_client`. Update both call sites.

In `run()` (line 282):
```python
            if self.ctx.needs_compression(self.llm):
```

In `run_stream()` (lines 338):
```python
            if self.ctx.needs_compression(self.llm):
```

- [ ] **Step 4: Verify imports and basic usage**

Run: `python -c "
from core.llm import LLMClient
from core.context import ContextManager
llm = LLMClient(api_key='test', model='deepseek-v4-pro')
ctx = ContextManager(system_prompt='test prompt')
tokens = ctx.estimate_tokens(llm)
print(f'Tokens: {tokens}')
needs = ctx.needs_compression(llm)
print(f'Needs compression: {needs}')
print('OK')
"`

Expected: Prints token count and `OK`.

- [ ] **Step 5: Commit**

```bash
git add core/context.py core/agent.py
git commit -m "refactor(context): replace heuristic token estimation with llm_client.count_messages_tokens"
```

---

### Task 10: 修改 `core/tools/base.py` — semantic_truncate 替换 truncate_long_output

**Files:**
- Modify: `core/tools/base.py`

**Interfaces:**
- Removes: `TRUNCATION_THRESHOLD`, `truncate_long_output()`
- Produces: `DEFAULT_TOKEN_BUDGET`, `ERROR_PATTERNS`, `semantic_truncate(text, file_path, token_budget, token_counter) -> tuple[str, bool]`

- [ ] **Step 1: Delete old truncation code**

Delete lines 63-84 (the `TRUNCATION_THRESHOLD` constant and `truncate_long_output` function).

- [ ] **Step 2: Add new semantic truncation code**

Add at the end of the file:

```python
import re

DEFAULT_TOKEN_BUDGET = 8000
ERROR_PATTERNS = [
    r"(?i)\b(error|exception|traceback|failed|failure|fatal|critical)\b",
    r"(?i)\b(warning|warn|deprecated)\b",
    r"^\s*File\s+\".+?\",\s+line\s+\d+",
    r"^\s*\^+$",
]

CODE_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx", ".rs", ".go", ".java", ".c", ".cpp", ".h"}


def semantic_truncate(
    text: str,
    file_path: str | None = None,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    token_counter=None,
) -> tuple[str, bool]:
    """Semantically truncate text to fit within token_budget.

    Returns (truncated_text, was_degraded: bool).

    Degradation levels:
      L0: within budget -> return as-is, was_degraded=False
      L1: code file + over budget -> hint to use read_outline, was_degraded=True
      L2: non-code or fallback -> smart truncation preserving error lines, was_degraded=True
    """
    estimated_tokens = token_counter(text) if token_counter else len(text) // 3

    if estimated_tokens <= token_budget:
        return text, False

    # L1: Code file -> hint to use outline
    if file_path:
        ext = file_path[file_path.rfind("."):].lower() if "." in file_path else ""
        if ext in CODE_EXTENSIONS:
            hint = (
                f"[Content degraded: file {file_path} exceeds token budget. "
                f"Use read_outline to view the skeleton structure, "
                f"or read with offset/limit for specific sections.]"
            )
            return hint, True

    # L2: Smart truncation preserving key lines
    lines = text.splitlines()
    head_count = max(1, int(len(lines) * 0.15))
    tail_count = max(1, int(len(lines) * 0.15))

    error_line_indices: set[int] = set()
    for i, line in enumerate(lines):
        for pattern in ERROR_PATTERNS:
            if re.search(pattern, line):
                error_line_indices.add(i)
                break

    head = lines[:head_count]
    tail = lines[-tail_count:]

    middle_errors = []
    for i in sorted(error_line_indices):
        if head_count <= i < len(lines) - tail_count:
            middle_errors.append(lines[i])

    omitted = len(lines) - len(head) - len(tail) - len(middle_errors)
    marker = f"\n... [{omitted} lines omitted - use read with offset/limit for full content] ...\n"

    result_lines = list(head)
    if middle_errors:
        result_lines.append("\n... [key lines from omitted section] ...")
        result_lines.extend(middle_errors)
    result_lines.append(marker)
    result_lines.extend(tail)

    return "\n".join(result_lines), True
```

- [ ] **Step 3: Verify module imports**

Run: `python -c "from core.tools.base import BaseTool, ToolResult, semantic_truncate, DEFAULT_TOKEN_BUDGET, ERROR_PATTERNS; print('OK')"`

Expected: Prints `OK`.

- [ ] **Step 4: Test semantic_truncate — L0 (no truncation needed)**

Run: `python -c "
from core.tools.base import semantic_truncate
text = 'short text'
result, degraded = semantic_truncate(text, token_budget=100)
print(f'L0: degraded={degraded}, result=\"{result}\"')
assert not degraded
assert result == text
print('PASS')
"`

Expected: `degraded=False`, result unchanged, `PASS`.

- [ ] **Step 5: Test semantic_truncate — L1 (code file)**

Run: `python -c "
from core.tools.base import semantic_truncate
text = 'x' * 10000
result, degraded = semantic_truncate(text, file_path='/test/main.py', token_budget=100)
print(f'L1: degraded={degraded}')
print(f'Result: {result[:80]}...')
assert degraded
assert 'read_outline' in result
print('PASS')
"`

Expected: `degraded=True`, result contains `read_outline` hint, `PASS`.

- [ ] **Step 6: Test semantic_truncate — L2 (non-code with errors)**

Run: `python -c "
from core.tools.base import semantic_truncate
lines = ['line ' + str(i) for i in range(1000)]
lines[500] = 'ERROR: something went wrong'
lines[600] = 'WARNING: deprecated API'
text = '\n'.join(lines)
result, degraded = semantic_truncate(text, token_budget=500)
print(f'L2: degraded={degraded}')
assert degraded
assert 'ERROR: something went wrong' in result
assert 'WARNING: deprecated API' in result
assert 'lines omitted' in result
print('PASS')
"`

Expected: `degraded=True`, error lines preserved, `PASS`.

- [ ] **Step 7: Commit**

```bash
git add core/tools/base.py
git commit -m "feat(truncate): replace head-tail truncation with semantic_truncate (L0/L1/L2 degradation)"
```

---

### Task 11: 修改 `core/tools/bash.py` 和 `core/tools/read.py` — 适配 semantic_truncate

**Files:**
- Modify: `core/tools/bash.py`
- Modify: `core/tools/read.py`

**Interfaces:**
- Consumes: `semantic_truncate` from `core.tools.base`

- [ ] **Step 1: Update bash.py imports**

Change line 12 from:
```python
from .base import BaseTool, ToolResult, truncate_long_output
```
to:
```python
from .base import BaseTool, ToolResult, semantic_truncate
```

- [ ] **Step 2: Replace all `truncate_long_output(...)` calls in bash.py**

Replace all occurrences (lines 286, 292, 295, 358, 382, 403) of:
```python
truncate_long_output(...)
```
with:
```python
semantic_truncate(...)[0]
```

The `[0]` extracts just the text from the `(text, degraded)` tuple — bash.py does not need the degradation flag since it never triggers L1 (no file_path for bash output).

- [ ] **Step 3: Update read.py imports**

Change line 3 from:
```python
from .base import BaseTool, ToolResult, truncate_long_output
```
to:
```python
from .base import BaseTool, ToolResult, semantic_truncate
```

- [ ] **Step 4: Replace `truncate_long_output` call in read.py**

Change line 34 from:
```python
            return ToolResult.ok(truncate_long_output(output))
```
to:
```python
            truncated, degraded = semantic_truncate(output, file_path=file_path)
            if degraded:
                return ToolResult.ok(truncated)
            return ToolResult.ok(output)
```

Note: only degrade when actually over budget. If not degraded, return the full output.

- [ ] **Step 5: Verify imports work**

Run: `python -c "from core.tools.bash import BashTool; from core.tools.read import ReadTool; print('OK')"`

Expected: Prints `OK`.

- [ ] **Step 6: Commit**

```bash
git add core/tools/bash.py core/tools/read.py
git commit -m "refactor(tools): migrate bash and read to semantic_truncate"
```

---

### Task 12: 修改 `core/agent.py` — 降级为 ActorAgent

**Files:**
- Modify: `core/agent.py`

**Interfaces:**
- Renames: `Agent` -> `ActorAgent`
- Adds: `ActorSummary` dataclass
- Adds: `actor_id: str`, `task_context: str` to `__init__`
- Modifies: `run()` returns `ActorSummary` instead of `str`
- Modifies: `run_stream()` AgentEvent includes `actor_id`
- Removes: `action_history` and circuit breaker (optional: keep as lightweight protection)

- [ ] **Step 1: Add ActorSummary dataclass**

After the `AgentEvent` dataclass (after line 30), add:

```python
@dataclass
class ActorSummary:
    task_id: str
    status: Literal["done", "failed"]  # noqa: F821
    files_modified: list[str] = field(default_factory=list)
    bugs_found: list[str] = field(default_factory=list)
    key_findings: str = ""
    suggested_next_steps: str = ""
    raw_output: str = ""
```

Also add `from dataclasses import field` to the import on line 12 (currently: `from dataclasses import dataclass` — change to `from dataclasses import dataclass, field`).

Also add `from typing import Literal` to line 12 imports.

- [ ] **Step 2: Rename class and update __init__**

Rename `class Agent:` to `class ActorAgent:` (line 109).

Update `__init__` signature:

```python
    def __init__(
        self,
        llm_client: LLMClient,
        context_manager: ContextManager,
        tools: list[BaseTool],
        workspace_dir: str,
        actor_id: str = "",
        task_context: str = "",
    ):
        self.actor_id = actor_id
        self.task_context = task_context
        self.llm = llm_client
        self.tools_by_name = {t.name: t for t in tools}
        self.workspace_dir = workspace_dir
        self.ctx = context_manager
```

Remove `self.action_history: deque[int] = deque(maxlen=5)` from `__init__`.

- [ ] **Step 3: Update `run()` return type**

Change `run()` return type annotation from `-> str` to `-> ActorSummary`.

Replace the final return in `run()` (after the `if not tool_calls` block, line 307):

```python
                return ActorSummary(
                    task_id=self.actor_id,
                    status="done",
                    key_findings=response.get("content") or "",
                    raw_output=response.get("content") or "",
                )
```

And the error return (line 297):
```python
                return ActorSummary(
                    task_id=self.actor_id,
                    status="failed",
                    key_findings=error_msg,
                    raw_output=error_msg,
                )
```

- [ ] **Step 4: Update `run_stream()` — add actor_id to events**

In `run_stream()`, update the `AgentEvent` construction lines to include `actor_id`:

Line 339: `yield AgentEvent(type="error", content=error_msg)` plus add actor_id
Line 390: `yield AgentEvent(type="done", content=response.get("content") or "")` plus add actor_id

The `AgentEvent` dataclass needs an `actor_id` field. Add to the dataclass at line 21:

```python
@dataclass
class AgentEvent:
    type: str
    content: str = ""
    tool_name: str | None = None
    tool_args: dict | None = None
    tool_result: ToolResult | None = None
    token: str = ""
    actor_id: str = ""
```

- [ ] **Step 5: Remove circuit breaker code**

Delete lines 145-174 (methods `_hash_action`, `detect_loop`, `_check_circuit_breaker`).

But keep a simplified circuit breaker for Actor self-protection: in `_execute_single_tool`, add a simple repeat-detection:

```python
        # Simple repeat detection (lightweight, Actor-level only)
        action_hash = hash(tool_name + json.dumps(args, sort_keys=True))
        if hasattr(self, '_recent_actions'):
            if self._recent_actions.count(action_hash) >= 2:
                intervention = (
                    "System Alert: Repeated tool call detected. "
                    "Please try a different approach."
                )
                self.ctx.add_tool_result(tc["id"], intervention)
                return (tool_name, args, ToolResult.fail(intervention), intervention, True)
            self._recent_actions.append(action_hash)
```

Add `self._recent_actions: list[int] = []` to `__init__`.

- [ ] **Step 6: Verify ActorAgent initializes and basic structure is correct**

Run: `python -c "
from core.llm import LLMClient
from core.context import ContextManager
from core.agent import ActorAgent, ActorSummary, AgentEvent
from core.tools.read import ReadTool

llm = LLMClient(api_key='test')
ctx = ContextManager(system_prompt='test')
actor = ActorAgent(llm_client=llm, context_manager=ctx, tools=[ReadTool()],
                   workspace_dir='.', actor_id='test-1', task_context='test task')
print(f'Actor ID: {actor.actor_id}')
print(f'Task context: {actor.task_context}')
print('OK')
"`

Expected: Prints actor_id, task_context, and `OK`.

- [ ] **Step 7: Update all references to `Agent` in the codebase**

Search for all `from core.agent import Agent` or references to `Agent` class:

```
cli/main.py:32      from core.agent import Agent
cli/main.py:53      agent = Agent(...)
cli/bridge.py:3     from core.agent import Agent
cli/bridge.py:10    def __init__(self, agent: Agent, ...):
web/bridge.py:5     from core.agent import Agent
web/bridge.py:11    def __init__(self, agent: Agent):
web/main.py:21      from core.agent import Agent
web/main.py:35      def init_agent() -> Agent:
web/main.py:54      return Agent(...)
```

These will be updated in later tasks (when we introduce Planner). For now, keep backward compatibility by adding an alias in `core/agent.py`:

```python
# Backward compatibility alias — will be removed after Planner migration
Agent = ActorAgent
```

- [ ] **Step 8: Commit**

```bash
git add core/agent.py
git commit -m "refactor(agent): downgrade Agent to stateless ActorAgent with ActorSummary return type"
```

---

### Task 13: 新增 `core/tools/delegate.py` — DelegateTool

**Files:**
- Create: `core/tools/delegate.py`

**Interfaces:**
- Consumes: `BaseTool`, `ToolResult` from `core.tools.base`; `GlobalState` from `core.state`; `ActorAgent`, `ActorSummary` from `core.agent`
- Produces: `DelegateTool(name="delegate")`, `MAX_CONCURRENT_ACTORS = 4`

- [ ] **Step 1: Write the file**

```python
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

from .base import BaseTool, ToolResult
from ..state import GlobalState

MAX_CONCURRENT_ACTORS = 4


class DelegateTool(BaseTool):
    name = "delegate"
    description = (
        "Dispatch multiple subtasks to independent Actor agents for concurrent execution. "
        "Each Actor runs in an isolated git worktree. "
        "Use this after you have decomposed a complex task into independent subtasks "
        "via update_state. Returns structured summaries from each Actor."
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
                        "description": "File paths to pre-read and inject into the Actor's context.",
                    },
                    "context_summaries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Relevant summary snippets from previous Actors to inject as context.",
                    },
                },
                "required": ["task_id", "description"],
            },
        }
    }
    required_params = ["subtasks"]

    def __init__(self, llm_client, workspace_dir: str):
        super().__init__()
        self._llm = llm_client
        self._workspace_dir = workspace_dir

    async def execute(self, subtasks: list[dict], **kwargs) -> ToolResult:
        """Dispatch subtasks to Actors concurrently with git worktree isolation."""
        from ..agent import ActorAgent
        from ..context import ContextManager
        from ..system_prompt import ACTOR_SYSTEM_PROMPT

        state = GlobalState.get()

        # Validate all task_ids
        for st in subtasks:
            tid = st.get("task_id", "")
            if tid not in state.task_tree:
                return ToolResult.fail(f"Unknown task_id: {tid}. Register via update_state first.")

        # Mark all as running
        for st in subtasks:
            state.update_task(st["task_id"], status="running")

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_ACTORS)

        async def run_one(subtask: dict) -> dict:
            tid = subtask["task_id"]
            description = subtask["description"]
            context_files = subtask.get("context_files", [])
            context_summaries = subtask.get("context_summaries", [])

            async with semaphore:
                # Build injected context message
                context_parts = [f"## Task\n{description}"]
                if context_files:
                    context_parts.append("\n## Relevant Files")
                    for fp in context_files:
                        try:
                            abs_path = os.path.join(self._workspace_dir, fp)
                            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                                content = f.read()[:4000]
                            context_parts.append(f"\n### {fp}\n```\n{content}\n```")
                        except Exception:
                            context_parts.append(f"\n### {fp}\n(unable to read)")
                if context_summaries:
                    context_parts.append("\n## Context from Previous Actors")
                    for s in context_summaries:
                        context_parts.append(f"- {s}")

                injected_context = "\n".join(context_parts)

                # Build ContextManager with actor prompt + injected context
                actor_ctx = ContextManager(
                    system_prompt=ACTOR_SYSTEM_PROMPT,
                    max_tokens=self._llm.max_tokens,
                )
                actor_ctx.add_user_message(injected_context)

                # Import ACTOR_TOOLS
                from ..tools import ACTOR_TOOLS

                actor = ActorAgent(
                    llm_client=self._llm,
                    context_manager=actor_ctx,
                    tools=[t() for t in ACTOR_TOOLS],
                    workspace_dir=self._workspace_dir,
                    actor_id=tid,
                    task_context=description,
                )

                try:
                    summary = await actor.run(description)
                    state.add_summary(tid, summary.key_findings or "Task completed.")
                    return {
                        "task_id": tid,
                        "status": summary.status,
                        "files_modified": summary.files_modified,
                        "bugs_found": summary.bugs_found,
                        "key_findings": summary.key_findings[:500],
                        "suggested_next_steps": summary.suggested_next_steps,
                    }
                except Exception as e:
                    state.update_task(tid, status="failed")
                    state.add_summary(tid, f"ERROR: {str(e)}")
                    return {
                        "task_id": tid,
                        "status": "failed",
                        "error": str(e),
                    }

        # Concurrent execution
        results = await asyncio.gather(*[run_one(st) for st in subtasks])

        # Build return message
        lines = [f"Delegate complete: {len(results)} subtask(s) executed.\n"]
        for r in results:
            status_icon = "OK" if r["status"] == "done" else "FAIL"
            lines.append(f"  [{status_icon}] {r['task_id']}: {r.get('key_findings', r.get('error', ''))[:200]}")
        return ToolResult.ok("\n".join(lines))
```

- [ ] **Step 2: Verify tool schema**

Run: `python -c "
from core.llm import LLMClient
from core.tools.delegate import DelegateTool
import json
llm = LLMClient(api_key='test')
dt = DelegateTool(llm_client=llm, workspace_dir='.')
print(json.dumps(dt.schema, indent=2))
print('OK')
"`

Expected: Prints valid JSON Schema with `subtasks` array parameter.

- [ ] **Step 3: Commit**

```bash
git add core/tools/delegate.py
git commit -m "feat(tools): add DelegateTool for concurrent Actor dispatch"
```

---

### Task 14: 新增 `core/planner.py` — Planner Agent

**Files:**
- Create: `core/planner.py`

**Interfaces:**
- Consumes: `LLMClient` from `core.llm`; `ContextManager` from `core.context`; `BaseTool` from `core.tools.base`; `GlobalState` from `core.state`; `AgentEvent` from `core.agent`
- Produces: `Planner` class with `run(user_input, on_token) -> str` and `run_stream(user_input) -> AsyncGenerator[AgentEvent]`

- [ ] **Step 1: Write the file**

```python
from __future__ import annotations

import re
import json
import asyncio
from collections.abc import AsyncGenerator, Callable

from .llm import LLMClient
from .context import ContextManager
from .tools.base import BaseTool, ToolResult
from .agent import AgentEvent
from .exceptions import LLMAPIError
from .state import GlobalState


class Planner:
    """Orchestration agent — decomposes tasks, dispatches Actors, synthesizes results."""

    def __init__(
        self,
        llm_client: LLMClient,
        context_manager: ContextManager,
        tools: list[BaseTool],
        workspace_dir: str,
    ):
        self.llm = llm_client
        self.tools_by_name = {t.name: t for t in tools}
        self.workspace_dir = workspace_dir
        self.ctx = context_manager
        self.state = GlobalState.get()

    async def run(
        self,
        user_input: str,
        on_token: Callable[[str], None] | None = None,
    ) -> str:
        self.ctx.add_user_message(user_input)
        tool_schemas = [t.schema for t in self.tools_by_name.values()]

        while True:
            if self.ctx.needs_compression(self.llm):
                await self.ctx.compress(self.llm)

            payload_messages = self.ctx.messages

            try:
                response = await self.llm.chat(
                    messages=payload_messages,
                    tools=tool_schemas if tool_schemas else None,
                    on_token=on_token,
                )
            except LLMAPIError as e:
                error_msg = str(e)
                self.ctx.add_assistant_message(content=error_msg)
                return error_msg

            tool_calls = response.get("tool_calls")

            if not tool_calls:
                content = response.get("content") or ""
                self.ctx.add_assistant_message(
                    content=content,
                    reasoning_content=response.get("reasoning_content"),
                )
                return content

            self.ctx.add_assistant_message(
                content=response.get("content"),
                tool_calls=tool_calls,
                reasoning_content=response.get("reasoning_content"),
            )

            for tc in tool_calls:
                tool_name = tc["function"]["name"]
                try:
                    raw_args = tc["function"]["arguments"].strip()
                    raw_args = re.sub(r"^```(?:json\s*)?", "", raw_args, flags=re.IGNORECASE)
                    raw_args = re.sub(r"\s*```$", "", raw_args).strip()
                    args = json.loads(raw_args) if raw_args else {}
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError as e:
                    error_hint = f"Error: Invalid JSON: {e}"
                    self.ctx.add_tool_result(tc["id"], error_hint)
                    continue

                tool = self.tools_by_name.get(tool_name)
                if tool is None:
                    observation = f"Error: unknown tool '{tool_name}'"
                    result = ToolResult.fail(f"unknown tool '{tool_name}'")
                else:
                    try:
                        result = await tool.execute(**args)
                    except Exception as e:
                        result = ToolResult.fail(f"Internal Tool Error: {str(e)}")

                    if result.success:
                        observation = result.content
                    else:
                        observation = f"ERROR: {result.error}"
                        if result.content:
                            observation += f"\nPartial output: {result.content}"

                self.ctx.add_tool_result(tc["id"], observation)

    async def run_stream(
        self,
        user_input: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        self.ctx.add_user_message(user_input)
        tool_schemas = [t.schema for t in self.tools_by_name.values()]

        step_count = 0
        MAX_STEPS = 30

        while True:
            step_count += 1
            if step_count > MAX_STEPS:
                error_msg = "Safety limit: Planner reached max steps. Please retry with a simpler request."
                self.ctx.add_assistant_message(content=error_msg)
                yield AgentEvent(type="error", content=error_msg)
                return

            if self.ctx.needs_compression(self.llm):
                await self.ctx.compress(self.llm)
                yield AgentEvent(type="compaction")

            queue = asyncio.Queue()

            def on_token(t: str) -> None:
                queue.put_nowait(t)

            payload_messages = self.ctx.messages

            chat_task = asyncio.create_task(
                self.llm.chat(
                    messages=payload_messages,
                    tools=tool_schemas if tool_schemas else None,
                    on_token=on_token,
                )
            )

            try:
                while not chat_task.done() or not queue.empty():
                    try:
                        token = await asyncio.wait_for(queue.get(), timeout=0.05)
                        yield AgentEvent(type="thought", token=token, content=token)
                    except asyncio.TimeoutError:
                        continue
            finally:
                if not chat_task.done():
                    chat_task.cancel()

            try:
                response = await chat_task
            except LLMAPIError as e:
                error_msg = str(e)
                self.ctx.add_assistant_message(content=error_msg)
                yield AgentEvent(type="error", content=error_msg)
                return

            tool_calls = response.get("tool_calls")

            if not tool_calls:
                content = response.get("content") or ""
                self.ctx.add_assistant_message(
                    content=content,
                    reasoning_content=response.get("reasoning_content"),
                )
                yield AgentEvent(type="done", content=content)
                return

            self.ctx.add_assistant_message(
                content=response.get("content"),
                tool_calls=tool_calls,
                reasoning_content=response.get("reasoning_content"),
            )

            for tc in tool_calls:
                tool_name = tc["function"]["name"]
                try:
                    raw_args = tc["function"]["arguments"].strip()
                    raw_args = re.sub(r"^```(?:json\s*)?", "", raw_args, flags=re.IGNORECASE)
                    raw_args = re.sub(r"\s*```$", "", raw_args).strip()
                    tool_args = json.loads(raw_args) if raw_args else {}
                    if not isinstance(tool_args, dict):
                        tool_args = {}
                except json.JSONDecodeError:
                    tool_args = {}

                yield AgentEvent(
                    type="tool_call",
                    tool_name=tool_name,
                    tool_args=tool_args,
                )

                tool = self.tools_by_name.get(tool_name)
                if tool is None:
                    observation = f"Error: unknown tool '{tool_name}'"
                    result = ToolResult.fail(f"unknown tool '{tool_name}'")
                else:
                    try:
                        result = await tool.execute(**tool_args)
                    except Exception as e:
                        result = ToolResult.fail(f"Internal Tool Error: {str(e)}")

                    if result.success:
                        observation = result.content
                    else:
                        observation = f"ERROR: {result.error}"
                        if result.content:
                            observation += f"\nPartial output: {result.content}"

                self.ctx.add_tool_result(tc["id"], observation)

                yield AgentEvent(
                    type="tool_result",
                    tool_name=tool_name,
                    tool_result=result,
                )

                # If delegate was called, check State for task updates
                if tool_name == "delegate":
                    snapshot = self.state.snapshot()
                    yield AgentEvent(
                        type="actor_update",
                        content=json.dumps(snapshot, ensure_ascii=False),
                    )
```

- [ ] **Step 2: Verify Planner initializes**

Run: `python -c "
from core.llm import LLMClient
from core.context import ContextManager
from core.planner import Planner
from core.tools.list_dir import ListDirTool
from core.system_prompt import PLANNER_SYSTEM_PROMPT

llm = LLMClient(api_key='test')
ctx = ContextManager(system_prompt=PLANNER_SYSTEM_PROMPT)
planner = Planner(llm_client=llm, context_manager=ctx,
                  tools=[ListDirTool()], workspace_dir='.')
print(f'Tools: {list(planner.tools_by_name.keys())}')
print('OK')
"`

Expected: Prints `['list_dir']` and `OK`.

- [ ] **Step 3: Commit**

```bash
git add core/planner.py
git commit -m "feat(planner): add Planner orchestration agent with ReAct loop"
```

---

### Task 15: 修改 `core/system_prompt.py` — 拆分为 Planner/Actor 双 Prompt

**Files:**
- Modify: `core/system_prompt.py`

**Interfaces:**
- Produces: `PLANNER_SYSTEM_PROMPT`, `ACTOR_SYSTEM_PROMPT`
- Removes: `SYSTEM_PROMPT` (old unified prompt)

- [ ] **Step 1: Replace the entire file**

Replace `core/system_prompt.py` content with:

```python
PLANNER_SYSTEM_PROMPT = """You are Depth Research Agent (Planner mode), a task orchestration agent.

You solve complex programming tasks by decomposing them into subtasks and delegating
execution to isolated Actor agents. You do NOT write code, edit files, or run shell
commands yourself - you orchestrate.

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
- Inject only essential context into each Actor - less noise = better results.
- When an Actor reports bugs or blockers, analyze them before spawning follow-up Actors.
- Prefer reading outlines before reading full files when scoping a task.
"""

ACTOR_SYSTEM_PROMPT = """You are Depth Research Agent (Actor mode), a task execution agent.

You execute a SINGLE, specific subtask assigned by the Planner. You operate in an
isolated git worktree. After completing your task, return a structured summary.
Do NOT plan next steps - the Planner handles that.

## Tools
- **read**: Read file contents with line numbers. For large files, read in chunks.
- **write**: Create or overwrite a file.
- **edit**: Make precise edits using search/replace blocks.
- **bash**: Execute shell commands. Use action="background" for servers, action="logs"
  to check output, action="kill" to terminate. Never run interactive commands.
- **search_codebase**: Find symbols (classes/functions) or text patterns.
- **list_dir**: List directory contents.
- **read_outline**: View skeleton structure of large files before reading them fully.

## Rules
- Work only within your assigned worktree directory.
- Read a file before editing it. Use `read` to see exact line numbers.
- Prefer `edit` over `write` for small changes to large files.
- When you encounter errors, read the error message and fix the problem yourself.
- For background servers: start with action="background", verify with curl/tests, then kill.
- Return a structured summary when done - do NOT chain into unrelated work.
- Before making edits, maintain a mental note of bugs found and files modified.
"""

# Backward compatibility alias — remove after all consumers migrate to Planner
SYSTEM_PROMPT = PLANNER_SYSTEM_PROMPT
```

- [ ] **Step 2: Verify both prompts are valid**

Run: `python -c "
from core.system_prompt import PLANNER_SYSTEM_PROMPT, ACTOR_SYSTEM_PROMPT
assert 'Planner mode' in PLANNER_SYSTEM_PROMPT
assert 'Actor mode' in ACTOR_SYSTEM_PROMPT
assert 'delegate' in PLANNER_SYSTEM_PROMPT
assert 'write' not in PLANNER_SYSTEM_PROMPT  # Planner has no write tool
assert 'write' in ACTOR_SYSTEM_PROMPT
print('PLANNER length:', len(PLANNER_SYSTEM_PROMPT))
print('ACTOR length:', len(ACTOR_SYSTEM_PROMPT))
print('OK')
"`

Expected: All assertions pass, prints lengths and `OK`.

- [ ] **Step 3: Commit**

```bash
git add core/system_prompt.py
git commit -m "refactor(prompt): split SYSTEM_PROMPT into PLANNER_SYSTEM_PROMPT and ACTOR_SYSTEM_PROMPT"
```

---

### Task 16: 修改 `core/tools/__init__.py` — 拆离 Actor/Planner 工具集

**Files:**
- Modify: `core/tools/__init__.py`

**Interfaces:**
- Produces: `ACTOR_TOOLS` (list of tool classes), `PLANNER_TOOLS` (list of tool classes)

- [ ] **Step 1: Replace the file with tool set separation**

```python
from .read import ReadTool
from .write import WriteTool
from .edit import EditTool
from .bash import BashTool
from .search import SearchCodebaseTool
from .list_dir import ListDirTool
from .read_outline import ReadOutlineTool
from .update_state import UpdateStateTool
from .delegate import DelegateTool

# Actor tools (execution layer) — tools that modify files/run commands
ACTOR_TOOLS = [
    ReadTool,
    WriteTool,
    EditTool,
    BashTool,
    SearchCodebaseTool,
    ReadOutlineTool,
    ListDirTool,
]

# Planner tools (orchestration layer) — tools that schedule and observe
PLANNER_TOOLS = [
    UpdateStateTool,
    DelegateTool,
    ListDirTool,
    SearchCodebaseTool,
    ReadOutlineTool,
]

__all__ = [
    "ReadTool", "WriteTool", "EditTool", "BashTool",
    "SearchCodebaseTool", "ListDirTool", "ReadOutlineTool",
    "UpdateStateTool", "DelegateTool",
    "ACTOR_TOOLS", "PLANNER_TOOLS",
]
```

- [ ] **Step 2: Verify imports**

Run: `python -c "
from core.tools import ACTOR_TOOLS, PLANNER_TOOLS
print('ACTOR tools:', [t.__name__ for t in ACTOR_TOOLS])
print('PLANNER tools:', [t.__name__ for t in PLANNER_TOOLS])
assert len(ACTOR_TOOLS) == 7
assert len(PLANNER_TOOLS) == 5
print('OK')
"`

Expected: Prints tool lists, 7 Actor tools + 5 Planner tools, `OK`.

- [ ] **Step 3: Commit**

```bash
git add core/tools/__init__.py
git commit -m "refactor(tools): split into ACTOR_TOOLS (execution) and PLANNER_TOOLS (orchestration)"
```

---

### Task 17: 修改 `cli/main.py` — 切换到 Planner

**Files:**
- Modify: `cli/main.py`

- [ ] **Step 1: Update imports and initialization**

Replace lines 30-53 (the lazy imports and tool construction block):

```python
    # Lazy imports so --help is fast
    from core.llm import LLMClient
    from core.context import ContextManager
    from core.planner import Planner
    from core.tools import PLANNER_TOOLS
    from core.system_prompt import PLANNER_SYSTEM_PROMPT
    from core.state import GlobalState
    from cli.ui import UI
    from cli.bridge import Bridge

    llm = LLMClient(
        api_key=api_key,
        base_url=os.getenv("SCA_API_BASE", "https://api.deepseek.com"),
        model=args.model or os.getenv("SCA_MODEL", "deepseek-v4-pro"),
        max_tokens=int(os.getenv("SCA_MAX_TOKENS", "128000")),
    )

    ctx = ContextManager(system_prompt=PLANNER_SYSTEM_PROMPT)
    tools = [t() for t in PLANNER_TOOLS]
    state = GlobalState.get()
    planner = Planner(llm_client=llm, context_manager=ctx, tools=tools, workspace_dir=workspace_dir)

    ui = UI()
    bridge = Bridge(agent=planner, ui=ui)

    asyncio.run(bridge.run())
```

- [ ] **Step 2: Update Bridge to accept Planner**

`Bridge.__init__` currently takes `agent: Agent`. Since Planner has the same `run_stream` interface, it's type-compatible. But update the type hint in `cli/bridge.py`:

Change line 3 from:
```python
from core.agent import Agent
```
to:
```python
from core.planner import Planner
```

Change line 10 from:
```python
    def __init__(self, agent: Agent, ui: UI):
```
to:
```python
    def __init__(self, agent: Planner, ui: UI):
```

- [ ] **Step 3: Verify CLI starts without errors**

Run: `python -c "
from cli.main import main
# Just verify imports resolve — won't start the REPL
print('CLI imports OK')
"`

Expected: Prints `CLI imports OK`.

- [ ] **Step 4: Commit**

```bash
git add cli/main.py cli/bridge.py
git commit -m "refactor(cli): switch from Agent to Planner with PLANNER_TOOLS"
```

---

### Task 18: 修改 `web/main.py` 和 `web/bridge.py` — 切换到 Planner

**Files:**
- Modify: `web/main.py`
- Modify: `web/bridge.py`

- [ ] **Step 1: Update web/main.py — imports and init_agent**

Replace lines 19-54 (imports and `init_agent` function):

```python
from core.llm import LLMClient
from core.context import ContextManager
from core.planner import Planner
from core.tools import PLANNER_TOOLS
from core.system_prompt import PLANNER_SYSTEM_PROMPT
from core.state import GlobalState
from web.bridge import WebBridge
from web.components.sidebar import render_sidebar
from web.components.chat import render_chat_history, render_current_events


def init_planner() -> Planner:
    api_key = os.getenv("SCA_API_KEY", "")
    if not api_key:
        st.error("SCA_API_KEY not set in .env file")
        st.stop()

    base_url = os.getenv("SCA_API_BASE", "https://api.deepseek.com")
    model = os.getenv("SCA_MODEL", "deepseek-v4-pro")
    max_tokens = int(os.getenv("SCA_MAX_TOKENS", "128000"))
    workspace = os.path.abspath(os.getenv("SCA_WORKSPACE", "./workspaces"))

    llm = LLMClient(
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_tokens=max_tokens,
    )
    ctx = ContextManager(system_prompt=PLANNER_SYSTEM_PROMPT)
    tools = [t() for t in PLANNER_TOOLS]
    _ = GlobalState.get()  # initialize singleton
    return Planner(llm_client=llm, context_manager=ctx, tools=tools, workspace_dir=workspace)
```

Also update session state initialization (lines 59-62):

```python
if "planner" not in st.session_state:
    st.session_state.planner = init_planner()
    st.session_state.bridge = WebBridge(st.session_state.planner)
    st.session_state.bridge.init_session(st)

bridge: WebBridge = st.session_state.bridge
```

Update model caption (line 67):
```python
    st.caption(f"Model: {st.session_state.planner.llm.model}")
```

- [ ] **Step 2: Update web/bridge.py — imports and type hints**

Change lines 3-5 from:
```python
from pathlib import Path
from core.agent import Agent
```
to:
```python
from pathlib import Path
from core.planner import Planner
```

Change line 11 from:
```python
    def __init__(self, agent: Agent):
```
to:
```python
    def __init__(self, agent: Planner):
```

Change `__init__` body and `init_session` to use `planner` instead of `agent`:
- `self.agent` stays as the attribute name (for backward compat in bridge methods) or rename to `self.planner` — choose to keep `self.agent` to minimize changes in `switch_project` and `handle_user_input_sync`.

Line 21:
```python
            "workspace_root": str(self.agent.workspace_dir),
            "current_project": Path(self.agent.workspace_dir).name,
```

- [ ] **Step 3: Verify web imports**

Run: `python -c "
from web.main import init_planner
print('Web imports OK')
"`

Expected: Prints `Web imports OK`. (Note: Streamlit may print warnings about no context — that's fine.)

- [ ] **Step 4: Commit**

```bash
git add web/main.py web/bridge.py
git commit -m "refactor(web): switch from Agent to Planner with GlobalState integration"
```

---

### Task 19: End-to-end integration test

**Files:**
- Create (temporary): `tests/test_integration.py`

- [ ] **Step 1: Write integration test for GlobalState + UpdateStateTool + Planner initialization**

```python
"""Minimal integration test — verifies the full chain initializes without errors."""
import asyncio
import pytest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_global_state_singleton():
    from core.state import GlobalState
    GlobalState.reset()
    s1 = GlobalState.get()
    s2 = GlobalState.get()
    assert s1 is s2


def test_state_add_and_update():
    from core.state import GlobalState
    GlobalState.reset()
    state = GlobalState.get()
    tid = state.add_task("test task")
    assert tid.startswith("task_")
    assert state.task_tree[tid].status == "pending"

    state.update_task(tid, status="running")
    assert state.task_tree[tid].status == "running"

    state.add_summary(tid, "all done")
    assert state.task_tree[tid].result_summary == "all done"
    assert state.task_tree[tid].status == "running"  # add_summary does NOT auto-set done

    assert len(state.change_log) == 3


def test_update_state_tool():
    from core.tools.update_state import UpdateStateTool
    from core.state import GlobalState
    GlobalState.reset()

    async def _test():
        tool = UpdateStateTool()

        # add_task
        r = await tool.execute(action="add_task", description="test")
        assert r.success
        tid = r.content.split(": ")[1]

        # update_task
        r = await tool.execute(action="update_task", task_id=tid, status="running")
        assert r.success

        # add_summary
        r = await tool.execute(action="add_summary", task_id=tid, summary="done")
        assert r.success

        # error: unknown task
        r = await tool.execute(action="update_task", task_id="bad", status="done")
        assert not r.success
        assert "Unknown" in r.error

    asyncio.run(_test())


def test_planner_initialization():
    from core.llm import LLMClient
    from core.context import ContextManager
    from core.planner import Planner
    from core.tools import PLANNER_TOOLS
    from core.system_prompt import PLANNER_SYSTEM_PROMPT
    from core.state import GlobalState
    GlobalState.reset()

    llm = LLMClient(api_key="test", model="deepseek-v4-pro")
    ctx = ContextManager(system_prompt=PLANNER_SYSTEM_PROMPT)
    tools = [t() for t in PLANNER_TOOLS]
    planner = Planner(llm_client=llm, context_manager=ctx, tools=tools, workspace_dir=".")

    assert "delegate" in planner.tools_by_name
    assert "update_state" in planner.tools_by_name
    assert "list_dir" in planner.tools_by_name
    # Planner must NOT have write/edit/bash
    assert "write" not in planner.tools_by_name
    assert "edit" not in planner.tools_by_name
    assert "bash" not in planner.tools_by_name


def test_actor_initialization():
    from core.llm import LLMClient
    from core.context import ContextManager
    from core.agent import ActorAgent
    from core.tools import ACTOR_TOOLS
    from core.system_prompt import ACTOR_SYSTEM_PROMPT

    llm = LLMClient(api_key="test", model="deepseek-v4-pro")
    ctx = ContextManager(system_prompt=ACTOR_SYSTEM_PROMPT)
    tools = [t() for t in ACTOR_TOOLS]
    actor = ActorAgent(
        llm_client=llm, context_manager=ctx, tools=tools,
        workspace_dir=".", actor_id="test-1", task_context="test task",
    )

    assert actor.actor_id == "test-1"
    # Actor must have execution tools
    assert "write" in actor.tools_by_name
    assert "bash" in actor.tools_by_name
    # Actor must NOT have delegate/update_state
    assert "delegate" not in actor.tools_by_name
    assert "update_state" not in actor.tools_by_name


def test_semantic_truncate_l0():
    from core.tools.base import semantic_truncate
    text = "hello world"
    result, degraded = semantic_truncate(text, token_budget=100)
    assert not degraded
    assert result == text


def test_semantic_truncate_l1():
    from core.tools.base import semantic_truncate
    text = "x" * 10000
    result, degraded = semantic_truncate(text, file_path="/test/main.py", token_budget=100)
    assert degraded
    assert "read_outline" in result


def test_semantic_truncate_l2():
    from core.tools.base import semantic_truncate
    lines = [f"line {i}" for i in range(1000)]
    lines[500] = "ERROR: critical failure"
    text = "\n".join(lines)
    result, degraded = semantic_truncate(text, token_budget=500)
    assert degraded
    assert "ERROR: critical failure" in result
    assert "lines omitted" in result
```

- [ ] **Step 2: Run the integration tests**

Run: `python -m pytest tests/test_integration.py -v`

Expected: All 8 tests pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: add integration tests for GlobalState, tools, Planner/Actor initialization"
```

---

### Task 20: Final verification — clean import sweep

- [ ] **Step 1: Verify all modules import cleanly**

Run: `python -c "
from core.state import GlobalState, TaskNode, ChangeRecord
from core.tools.base import BaseTool, ToolResult, semantic_truncate
from core.tools.update_state import UpdateStateTool
from core.tools.delegate import DelegateTool
from core.tools import ACTOR_TOOLS, PLANNER_TOOLS
from core.agent import ActorAgent, ActorSummary, AgentEvent
from core.planner import Planner
from core.system_prompt import PLANNER_SYSTEM_PROMPT, ACTOR_SYSTEM_PROMPT
from core.llm import LLMClient
from core.context import ContextManager
from core.exceptions import SCAAgentError, LLMAPIError
print('All imports OK')
"`

Expected: Prints `All imports OK`.

- [ ] **Step 2: Verify no stale references to old `Agent` class (except backward-compat alias)**

Run: `grep -rn "from core.agent import Agent" --include="*.py" cli/ web/ core/`

Expected: No results (all references should now use Planner or ActorAgent).

- [ ] **Step 3: Verify no stale `truncate_long_output` references**

Run: `grep -rn "truncate_long_output" --include="*.py" core/`

Expected: No results.

- [ ] **Step 4: Commit any cleanup**

```bash
git add -A
git diff --cached --stat
git commit -m "chore: final cleanup and import verification"
```

---
