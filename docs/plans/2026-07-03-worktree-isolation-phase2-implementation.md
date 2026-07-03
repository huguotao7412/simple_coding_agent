# Worktree Isolation Phase 2 — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix three functional gaps in the worktree isolation system: (1) real-time Actor status rendering in both CLI and Web UIs, (2) orphaned worktree cleanup at startup, (3) concrete conflict resolution guidance for the Planner.

**Architecture:** CLI bridge intercepts `actor_update` events and renders a Rich `Live` table via a new `UI.render_actor_status()` method. Web chat pushes actor snapshots into `st.session_state` for the sidebar to consume in real time. Both `cli/main.py` and `web/main.py` call `cleanup_orphans()` before Planner initialization. `apply_patch.py` returns a step-by-step resolution protocol on conflict. `PLANNER_SYSTEM_PROMPT` adds a "Conflict Resolution" SOP section.

**Tech Stack:** Python 3.13, Rich (terminal), Streamlit (web), git CLI, no new dependencies

**Design doc:** `docs/plans/2026-07-03-worktree-isolation-phase2-design.md`

---

### Task 1: Add Actor Status Rendering to CLI UI (`cli/ui.py`)

**Files:**
- Modify: `cli/ui.py:20-62` (UI class)

**Step 1: Add imports for Rich Table and Live**

Read the current imports (lines 3-7):
```python
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.live import Live
from rich.status import Status
```

`Live` is already imported. Add `Table`:

```python
from rich.table import Table
```

**Step 2: Add `_actor_table` field to `__init__`**

Replace the `__init__` method (lines 23-25):

```python
def __init__(self):
    self.console = Console(force_terminal=True)
    self._tool_status: Status | None = None
    self._actor_table: Live | None = None
```

With:
```python
def __init__(self):
    self.console = Console(force_terminal=True)
    self._tool_status: Status | None = None
    self._actor_table: Live | None = None
```

Wait — it's already there with `_tool_status`. I just need to add `_actor_table`. Let me provide the exact edit.

**Actual edit — add after `self._tool_status` line:**

```python
self._actor_table: Live | None = None
```

**Step 3: Add `clear_actor_status()` method**

Add after `clear_tool_status()` (after line 38):

```python
def clear_actor_status(self) -> None:
    """Stop and clear the concurrent Actor status table."""
    if self._actor_table:
        self._actor_table.stop()
        self._actor_table = None
```

**Step 4: Add `render_actor_status()` method**

Add after `clear_actor_status()`:

```python
def render_actor_status(self, task_tree: dict) -> None:
    """Render concurrent Actor execution status as a dynamic table.

    Called by Bridge when it receives an actor_update event.
    Updates in-place on subsequent calls for the same delegate batch.
    """
    if not task_tree:
        return

    status_styles = {
        "pending":  ("⏳", "dim yellow"),
        "running":  ("🔄", "bold cyan"),
        "done":     ("✅", "bold green"),
        "failed":   ("❌", "bold red"),
    }

    table = Table(
        title="并发任务状态",
        title_style="bold blue",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Task ID", style="dim", width=14)
    table.add_column("任务描述", width=40)
    table.add_column("状态", width=12)

    for tid, task in task_tree.items():
        icon, style = status_styles.get(task.get("status", ""), ("❓", ""))
        status_text = f"{icon} {task['status']}"
        desc = (task.get("description", "") or "")[:38]
        table.add_row(tid, desc, f"[{style}]{status_text}[/]")

    if self._actor_table:
        self._actor_table.update(table)
    else:
        self._actor_table = Live(
            table,
            console=self.console,
            refresh_per_second=4,
            transient=False,
        )
        self._actor_table.start()
```

**Step 5: Verify the module imports and parses**

```bash
python -c "
from cli.ui import UI
ui = UI()
assert hasattr(ui, 'render_actor_status')
assert hasattr(ui, 'clear_actor_status')
assert hasattr(ui, '_actor_table')
print('SUCCESS')
"
```

Expected: `SUCCESS`

**Step 6: Commit**

```bash
git add cli/ui.py
git commit -m "feat: add actor status table rendering to CLI UI"
```

---

### Task 2: Handle `actor_update` in CLI Bridge (`cli/bridge.py`)

**Files:**
- Modify: `cli/bridge.py:33-61` (event loop and finally block)

**Step 1: Add `actor_update` handler in the event loop**

Insert after the `tool_result` handler (after line 47, before `elif event.type == "compaction"`):

```python
                    elif event.type == "actor_update":
                        if stream:
                            stream.__exit__(None, None, None)
                            stream = None
                        import json
                        try:
                            snapshot = json.loads(event.content)
                            self.ui.render_actor_status(
                                snapshot.get("task_tree", {})
                            )
                        except Exception:
                            pass  # best-effort rendering
```

**Step 2: Add `clear_actor_status()` to the finally block**

Update the `finally` block (lines 57-61) to also clear actor status:

Current:
```python
            finally:
                self.ui.clear_tool_status()  # 确保一轮对话结束时，不残留工具动画
                if stream:
                    stream.__exit__(None, None, None)
```

Change to:
```python
            finally:
                self.ui.clear_tool_status()  # 确保一轮对话结束时，不残留工具动画
                self.ui.clear_actor_status()  # 清理并发任务状态表
                if stream:
                    stream.__exit__(None, None, None)
```

**Step 3: Verify the bridge module parses**

```bash
python -c "
from cli.bridge import Bridge
print('Bridge imported OK')
print('SUCCESS')
"
```

Expected: `SUCCESS`

**Step 4: Commit**

```bash
git add cli/bridge.py
git commit -m "feat: handle actor_update events in CLI bridge"
```

---

### Task 3: Handle `actor_update` in Web Chat Component (`web/components/chat.py`)

**Files:**
- Modify: `web/components/chat.py:29-71` (render_current_events)

**Step 1: Add `actor_update` handler**

Insert after the `compaction` handler (after line 57, before `elif event.type == "error"`):

```python
        elif event.type == "actor_update":
            import json
            try:
                snapshot = json.loads(event.content)
                st.session_state["actor_snapshot"] = snapshot.get(
                    "task_tree", {}
                )
            except Exception:
                pass
            i += 1
```

**Step 2: Verify the module parses**

```bash
python -c "
from web.components.chat import render_current_events, render_chat_history
print('Chat component imports OK')
print('SUCCESS')
"
```

Expected: `SUCCESS`

**Step 3: Commit**

```bash
git add web/components/chat.py
git commit -m "feat: handle actor_update events in web chat component"
```

---

### Task 4: Update Web Sidebar for Real-time Actor Snapshots (`web/components/sidebar.py`)

**Files:**
- Modify: `web/components/sidebar.py:38-59` (Task Board section)

**Step 1: Prefer actor_snapshot for real-time task display**

Replace the task reading logic (lines 44-47):

Current:
```python
    state = GlobalState.get()
    snapshot = state.snapshot()
    tasks = snapshot.get("task_tree", {})
```

Change to:
```python
    # Prefer actor_snapshot (real-time from actor_update events during streaming),
    # fall back to GlobalState for static reads between turns
    actor_snapshot = st.session_state.get("actor_snapshot", {})
    if actor_snapshot:
        tasks = actor_snapshot
    else:
        state = GlobalState.get()
        tasks = state.snapshot().get("task_tree", {})
```

**Step 2: Verify the sidebar module parses**

```bash
python -c "
from web.components.sidebar import render_sidebar
print('Sidebar component imports OK')
print('SUCCESS')
"
```

Expected: `SUCCESS`

**Step 3: Commit**

```bash
git add web/components/sidebar.py
git commit -m "feat: use actor_snapshot for real-time task status in web sidebar"
```

---

### Task 5: Add Startup Cleanup to CLI Entry (`cli/main.py`)

**Files:**
- Modify: `cli/main.py:15-54` (main function)

**Step 1: Insert cleanup_orphans after workspace_dir resolution**

After line 27 (`workspace_dir = os.path.abspath(workspace_dir)`) and before line 29 (`# Lazy imports`), add:

```python
    # Clean up orphaned worktrees from previous crashes
    from core.git_utils import cleanup_orphans
    try:
        removed = cleanup_orphans(workspace_dir)
        if removed:
            print(
                f"[init] Cleaned up {len(removed)} orphaned worktree(s)",
                file=sys.stderr,
            )
    except Exception as e:
        print(
            f"[init] Warning: worktree cleanup failed: {e}",
            file=sys.stderr,
        )
```

**Step 2: Verify the module parses and the cleanup doesn't break imports**

```bash
python -c "
import sys
sys.argv = ['sca', '--help']
# Quick parse test — main() would run the agent, so just import the module
import importlib
spec = importlib.util.spec_from_file_location('main', 'cli/main.py')
print('main.py spec loaded OK')
print('SUCCESS')
"
```

Expected: `SUCCESS`

**Step 3: Commit**

```bash
git add cli/main.py
git commit -m "feat: add startup worktree cleanup to CLI entry point"
```

---

### Task 6: Add Startup Cleanup to Web Entry (`web/main.py`)

**Files:**
- Modify: `web/main.py:30-50` (init_planner function)

**Step 1: Insert cleanup_orphans in init_planner()**

After workspace path resolution (line 39: `workspace = os.path.abspath(...)`) and before Planner construction (line 50: `return Planner(...)`), add:

```python
    # Clean up orphaned worktrees from previous sessions
    from core.git_utils import cleanup_orphans
    try:
        removed = cleanup_orphans(workspace)
        if removed:
            import logging
            logging.warning(
                f"Cleaned up {len(removed)} orphaned worktree(s)"
            )
    except Exception:
        pass  # best-effort on web startup
```

Insert this right before the `return Planner(...)` line.

**Step 2: Verify the module parses**

```bash
python -c "
from web.main import init_planner
print('web.main imports OK')
print('SUCCESS')
"
```

Expected: `SUCCESS`

**Step 3: Commit**

```bash
git add web/main.py
git commit -m "feat: add startup worktree cleanup to web entry point"
```

---

### Task 7: Strengthen apply_patch Conflict Error Message (`core/tools/apply_patch.py`)

**Files:**
- Modify: `core/tools/apply_patch.py:94-106` (conflict error return in strict mode)

**Step 1: Replace the conflict error return with actionable protocol**

Current (approximately lines 94-106 in the strict branch):
```python
                else:
                    return ToolResult.fail(
                        f"Patch for {task_id} conflicts with current workspace state.\n"
                        f"Conflicting files: {', '.join(conflict_files) if conflict_files else 'unknown'}.\n"
                        f"Git output: {stderr}\n\n"
                        f"Consider: spawn a resolution Actor to manually merge the changes, "
                        f"or use strategy='fuzz' to apply partial changes."
                    )
```

Replace with:
```python
                else:
                    return ToolResult.fail(
                        f"Patch for {task_id} conflicts with current workspace state.\n"
                        f"Conflicting files: {', '.join(conflict_files) if conflict_files else 'unknown'}.\n"
                        f"Git error details:\n{stderr}\n\n"
                        f"=== RESOLUTION PROTOCOL ===\n"
                        f"1. Create a new task via update_state: 'Resolve merge conflict for {task_id}'\n"
                        f"2. Delegate this task to a single Actor. Inject as context_files the\n"
                        f"   conflicting file paths listed above, plus the original diff below.\n"
                        f"3. The resolution Actor should read the conflicting files, understand\n"
                        f"   both the current state AND the intended changes, manually merge,\n"
                        f"   and produce a clean unified diff as output.\n"
                        f"4. Apply the resolution Actor's clean diff with apply_patch.\n"
                        f"5. If resolution also fails, retry ONCE with strategy='fuzz'.\n"
                        f"6. After 2 failed resolution attempts, report to the user.\n"
                        f"=== ORIGINAL DIFF (first 2000 chars) ===\n"
                        f"{diff[:2000]}"
                    )
```

**Step 2: Also update the fuzz-mode result to be more actionable**

Find the fuzz-mode return (in the `if strategy == "fuzz"` branch) and enhance its `result_parts` to include a hint about reviewing `.rej` files. The current code already does this — verify it's still correct:

```bash
python -c "
# Verify the fuzz-mode return includes .rej hint
import inspect
from core.tools.apply_patch import ApplyPatchTool
source = inspect.getsource(ApplyPatchTool.execute)
assert '.rej' in source, 'Fuzz mode should mention .rej files'
print('Fuzz mode .rej hint present')
print('SUCCESS')
"
```

Expected: `SUCCESS`

**Step 3: Verify the module still parses**

```bash
python -c "
from core.tools.apply_patch import ApplyPatchTool
t = ApplyPatchTool()
print(f'Tool: {t.name}')
print('SUCCESS')
"
```

Expected: `SUCCESS`

**Step 4: Commit**

```bash
git add core/tools/apply_patch.py
git commit -m "feat: enhance apply_patch conflict error with resolution protocol"
```

---

### Task 8: Add Conflict Resolution SOP to Planner Prompt (`core/system_prompt.py`)

**Files:**
- Modify: `core/system_prompt.py:1-30` (PLANNER_SYSTEM_PROMPT)

**Step 1: Add "Conflict Resolution" section to PLANNER_SYSTEM_PROMPT**

Insert the **Conflict Resolution** section between the existing **Merge** step (step 4) and the **Evaluate** step (step 5) in the Workflow. Also add it as a standalone `## Conflict Resolution` section after the Tools list.

Read the current PLANNER_SYSTEM_PROMPT to find the exact insertion point. Add after the "Merge" workflow bullet and before the "Evaluate" bullet:

Find:
```
4. **Merge** Actor results back into the main workspace:
   ...
   - If a patch conflicts, analyze the conflict and spawn a dedicated Actor to resolve it.
5. **Evaluate** Actor summaries.
```

Insert a new step 4.5 between them:
```
   - **On conflict**: follow the Conflict Resolution SOP below.
```

Then add this new section after the `## Rules` section (before the closing `"""`):

```python

## Conflict Resolution SOP
When apply_patch reports a conflict, follow this procedure:

1. **Do NOT retry the same diff** — read the error to understand what conflicted.
2. Create a new task via update_state: "Resolve merge conflict in <filename>"
3. Delegate this task to a single Actor. Inject context:
   - The conflicting file paths as context_files
   - The original diff and git error details as context_summaries
   - Instruction: "Read the files, understand both sides, manually merge, produce a clean diff."
4. Apply the resolution Actor's diff with apply_patch.
5. If resolution also fails, retry ONCE with strategy='fuzz'.
6. **After 2 failed resolution attempts for the same original task, STOP** —
   explain the conflict to the user and ask for guidance.
```

**Step 2: Verify the prompt contains the new SOP**

```bash
python -c "
from core.system_prompt import PLANNER_SYSTEM_PROMPT
assert 'Conflict Resolution SOP' in PLANNER_SYSTEM_PROMPT
assert 'Do NOT retry the same diff' in PLANNER_SYSTEM_PROMPT
assert 'After 2 failed resolution attempts' in PLANNER_SYSTEM_PROMPT
print('PLANNER prompt length:', len(PLANNER_SYSTEM_PROMPT))
print('SUCCESS')
"
```

Expected: `SUCCESS`

**Step 3: Commit**

```bash
git add core/system_prompt.py
git commit -m "feat: add conflict resolution SOP to Planner system prompt"
```

---

### Task 9: End-to-End Verification

**Files:**
- No new files — manual verification of all three phases

**Step 1: Full import chain — verify no broken imports**

```bash
python -c "
# CLI chain
from cli.ui import UI
from cli.bridge import Bridge
from core.planner import Planner
from core.state import GlobalState
from core.git_utils import cleanup_orphans
from core.tools.apply_patch import ApplyPatchTool
from core.system_prompt import PLANNER_SYSTEM_PROMPT, ACTOR_SYSTEM_PROMPT

# Web chain
from web.components.chat import render_chat_history, render_current_events
from web.components.sidebar import render_sidebar

print('All imports OK')
print('SUCCESS')
"
```

Expected: `SUCCESS`

**Step 2: Test UI.render_actor_status with mock task tree**

```bash
python -c "
from cli.ui import UI
ui = UI()

# Simulate a task tree snapshot
mock_tree = {
    'task_a1b2': {'description': 'Fix bug in auth module', 'status': 'done'},
    'task_c3d4': {'description': 'Add unit tests for API', 'status': 'running'},
    'task_e5f6': {'description': 'Refactor database layer', 'status': 'pending'},
    'task_g7h8': {'description': 'Update documentation', 'status': 'failed'},
}

# Render — should not raise
ui.render_actor_status(mock_tree)
print('Rendered OK')

# Clear — should not raise
ui.clear_actor_status()
print('Cleared OK')

# Verify empty tree is a no-op
ui.render_actor_status({})
print('Empty tree no-op OK')

print('SUCCESS')
"
```

Expected: `SUCCESS` (the Rich table renders to terminal but shouldn't raise)

**Step 3: Simulate cleanup_orphans call (no-op when clean)**

```bash
python -c "
import os
from core.git_utils import cleanup_orphans
base = os.getcwd()
removed = cleanup_orphans(base)
print(f'Cleaned: {removed}')  # Should be empty list (no orphans)
print('Cleanup OK')
print('SUCCESS')
"
```

Expected: `SUCCESS`

**Step 4: Check git status clean**

```bash
git status --porcelain
```

Expected: No output (clean workspace, all changes committed).

---

## Summary

| Task | Files | Key Change |
|------|-------|-----------|
| 1 | `cli/ui.py` | `render_actor_status()` + `clear_actor_status()` |
| 2 | `cli/bridge.py` | Handle `actor_update` event, call UI methods |
| 3 | `web/components/chat.py` | Push `actor_update` snapshot to `session_state` |
| 4 | `web/components/sidebar.py` | Prefer `actor_snapshot` for real-time display |
| 5 | `cli/main.py` | Startup `cleanup_orphans()` call |
| 6 | `web/main.py` | Startup `cleanup_orphans()` call |
| 7 | `core/tools/apply_patch.py` | Enhanced conflict error with resolution protocol |
| 8 | `core/system_prompt.py` | Conflict Resolution SOP in Planner prompt |
| 9 | E2E verification | Full import chain + UI rendering + cleanup test |

**Total commits: 8** (one per implementation task, excluding verification)
