# Design: Worktree Isolation Phase 2 — Gap Remediation

**Date**: 2026-07-03
**Status**: Approved — ready for implementation
**Depends on**: Phase 1 worktree isolation (completed — commits `af4dfe9` through `2546fbf`)
**Scope**: `cli/bridge.py`, `cli/ui.py`, `cli/main.py`, `web/main.py`, `web/components/chat.py`, `core/tools/apply_patch.py`, `core/system_prompt.py`

---

## Problem Statement

Phase 1 established the core worktree isolation infrastructure (git_utils, delegate lifecycle, apply_patch, bash blacklist, state diff tracking). However, three functional gaps remain:

1. **UI dead zone**: The Planner yields `actor_update` events with concurrent task status, but neither the CLI bridge nor the Web chat component handles this event type. Users cannot see which Actors are running/done/failed during concurrent execution.

2. **Orphan accumulation**: `delegate.py` cleans up worktrees per-batch, but if the process crashes between sessions, stale worktrees persist on disk indefinitely. No startup cleanup exists in either `cli/main.py` or `web/main.py`.

3. **Conflict resolution ambiguous**: `apply_patch.py` returns error text suggesting the Planner "spawn a resolution Actor", but the guidance is too vague. The Planner can get stuck in retry loops without a concrete SOP.

## Solution Overview

Three targeted fixes that build on Phase 1 infrastructure:
- **Section 1**: Handle `actor_update` events in both CLI and Web UIs
- **Section 2**: Inject `cleanup_orphans()` at startup in both entry points
- **Section 3**: Strengthen `apply_patch.py` error messages and `PLANNER_SYSTEM_PROMPT` with a concrete conflict resolution SOP

---

## Section 1: Actor Update → UI Pipeline

### 1a. CLI (`cli/bridge.py`)

Add a new `elif event.type == "actor_update"` branch in the event loop (after line 47, before the `compaction` handler):

```python
elif event.type == "actor_update":
    if stream:
        stream.__exit__(None, None, None)
        stream = None
    import json
    try:
        snapshot = json.loads(event.content)
        self.ui.render_actor_status(snapshot.get("task_tree", {}))
    except Exception:
        pass  # best-effort rendering
```

### 1b. CLI (`cli/ui.py`)

New method `render_actor_status()` renders a compact Rich table:

```python
from rich.table import Table
from rich.live import Live

class UI:
    def __init__(self):
        ...
        self._actor_table: Live | None = None

    def clear_actor_status(self) -> None:
        if self._actor_table:
            self._actor_table.stop()
            self._actor_table = None

    def render_actor_status(self, task_tree: dict) -> None:
        if not task_tree:
            return

        status_styles = {
            "pending":  ("⏳", "dim yellow"),
            "running":  ("🔄", "bold cyan"),
            "done":     ("✅", "bold green"),
            "failed":   ("❌", "bold red"),
        }

        table = Table(title="并发任务状态", title_style="bold blue",
                      show_header=True, header_style="bold")
        table.add_column("Task ID", style="dim", width=12)
        table.add_column("任务描述", width=40)
        table.add_column("状态", width=10)

        for tid, task in task_tree.items():
            icon, style = status_styles.get(task.get("status", ""), ("❓", ""))
            status_text = f"{icon} {task['status']}"
            desc = (task.get("description", "") or "")[:38]
            table.add_row(tid, desc, f"[{style}]{status_text}[/]")

        if self._actor_table:
            self._actor_table.update(table)
        else:
            self._actor_table = Live(table, console=self.console,
                                     refresh_per_second=4, transient=False)
            self._actor_table.start()
```

Add `self.ui.clear_actor_status()` call in bridge.py's `finally` block alongside the existing `clear_tool_status()`.

### 1c. Web (`web/components/chat.py`)

Add handler in `render_current_events()` (after line 57, before `elif event.type == "error"`):

```python
elif event.type == "actor_update":
    import json
    try:
        snapshot = json.loads(event.content)
        st.session_state["actor_snapshot"] = snapshot.get("task_tree", {})
    except Exception:
        pass
    i += 1
```

The Web sidebar (`web/components/sidebar.py:38-59`) already reads from `GlobalState.get().snapshot()`. Update it to prefer `st.session_state.get("actor_snapshot")` when available (real-time during streaming), falling back to the static `GlobalState` read:

```python
# In sidebar.py, inside the Task Board section:
actor_snapshot = st.session_state.get("actor_snapshot", {})
tasks = actor_snapshot if actor_snapshot else state.snapshot().get("task_tree", {})
```

### 1d. Clear state on new conversation

In bridge.py's `finally` block and web's streaming completion, clear `actor_snapshot` so stale task trees don't persist into the next conversation turn.

---

## Section 2: Global Startup Cleanup

### 2a. CLI (`cli/main.py`)

After `workspace_dir` resolution (line 27) and before Planner init (line 49), insert:

```python
# Clean up orphaned worktrees from previous crashes
from core.git_utils import cleanup_orphans
try:
    removed = cleanup_orphans(workspace_dir)
    if removed:
        print(f"[init] Cleaned up {len(removed)} orphaned worktree(s)", file=sys.stderr)
except Exception as e:
    print(f"[init] Warning: worktree cleanup failed: {e}", file=sys.stderr)
```

### 2b. Web (`web/main.py`)

Inside `init_planner()`, after workspace path resolution (line 39), before `Planner(...)` construction:

```python
from core.git_utils import cleanup_orphans
try:
    removed = cleanup_orphans(workspace)
    if removed:
        import logging
        logging.warning(f"Cleaned up {len(removed)} orphaned worktree(s)")
except Exception:
    pass  # best-effort on web startup
```

This complements the existing `delegate.py` per-batch cleanup (commit `c068fb2`). The delegate cleanup handles intra-session orphans; the startup cleanup handles inter-session orphans from crashes.

---

## Section 3: Planner Conflict Resolution Hardening

### 3a. `core/tools/apply_patch.py` error message

Replace the current conflict error return with an actionable resolution protocol:

```python
return ToolResult.fail(
    f"Patch for {task_id} conflicts with current workspace state.\n"
    f"Conflicting files: {', '.join(conflict_files) if conflict_files else 'unknown'}.\n"
    f"Git error details:\n{stderr}\n\n"
    f"=== RESOLUTION PROTOCOL ===\n"
    f"1. Create a new task via update_state: 'Resolve merge conflict for {task_id}'\n"
    f"2. Delegate this task to a single Actor. Inject as context_files the conflicting\n"
    f"   file paths listed above, plus the original diff (see below).\n"
    f"3. The resolution Actor should read the conflicting files, understand both the\n"
    f"   current state AND the intended changes in the diff, manually merge, and\n"
    f"   produce a clean unified diff as output.\n"
    f"4. Apply the resolution Actor's diff with apply_patch.\n"
    f"5. If resolution also fails, retry ONCE with strategy='fuzz'.\n"
    f"6. After 2 failed resolution attempts, report the conflict to the user.\n"
    f"=== ORIGINAL DIFF (first 2000 chars) ===\n"
    f"{diff[:2000]}"
)
```

### 3b. `core/system_prompt.py` — add Conflict Resolution SOP

Add this section to `PLANNER_SYSTEM_PROMPT` between the "Merge" workflow step and the "Evaluate" step:

```
## Conflict Resolution
When apply_patch reports a conflict:
1. **Do NOT retry the same diff** — read the error to understand what conflicted.
2. Create a new task via update_state: "Resolve merge conflict in <filename>"
3. Delegate this task to a single Actor. Inject context:
   - The conflicting file paths as context_files
   - The original diff and git error as context_summaries
   - Clear instruction to manually merge and produce a clean diff
4. Apply the resolution Actor's diff with apply_patch.
5. If resolution also fails, try strategy='fuzz' as a last resort.
6. **After 2 failed resolution attempts for the same original task, STOP** —
   explain the conflict to the user and ask for guidance.
```

---

## Error Handling Summary

| Failure Point | Behavior |
|---------------|----------|
| `json.loads(event.content)` fails in actor_update handler | Silently skip — best-effort rendering |
| `cleanup_orphans` raises at startup | Log warning, continue — cleanup is non-critical |
| apply_patch conflict → resolution Actor also fails | Planner stops after 2 attempts, reports to user |
| `actor_snapshot` key missing in session_state | Fall back to static `GlobalState.snapshot()` |
| Actor table rendering fails mid-stream | `clear_actor_status()` in finally block resets state |

---

## Implementation Order

1. **`cli/ui.py`** — new `render_actor_status()` + `clear_actor_status()` methods (foundation)
2. **`cli/bridge.py`** — handle `actor_update` event, call UI methods
3. **`web/components/chat.py`** — handle `actor_update` event, push to session_state
4. **`web/components/sidebar.py`** — prefer actor_snapshot for real-time rendering
5. **`cli/main.py`** — startup cleanup_orphans
6. **`web/main.py`** — startup cleanup_orphans
7. **`core/tools/apply_patch.py`** — enhanced conflict error message
8. **`core/system_prompt.py`** — conflict resolution SOP
