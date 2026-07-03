# Design: Concurrent Actor Git Worktree Isolation

**Date**: 2026-07-03
**Status**: Approved — ready for implementation
**Scope**: `core/tools/delegate.py`, `core/tools/bash.py`, `core/state.py`, `core/system_prompt.py`, new: `core/git_utils.py`, `core/tools/apply_patch.py`

---

## Problem Statement

The project uses a Planner → Actors pattern for concurrent task execution. The Planner decomposes tasks and delegates them to up to 4 concurrent Actor agents. However, **all Actors share the same physical workspace directory**, so concurrent file writes cause overwrite conflicts and corruption. The Actor system prompt falsely claims "You operate in an isolated git worktree" — but no isolation exists in code.

## Solution Overview

Give each Actor a real `git worktree` — a physically independent working directory on a dedicated branch. The Planner collects diffs from completed Actors and applies them back to the main workspace via a new `apply_patch` tool.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Merge strategy | Sequential `git apply` patches, Planner-driven | Planner retains full control; conflicts are explicit; simple to debug |
| Worktree lifecycle | Created before Actor.run, destroyed in `finally` | Guarantees cleanup regardless of Actor outcome |
| Diff source | `git diff HEAD` (uncommitted changes + untracked files) | Actors edit files but don't commit; HEAD is the baseline |
| Conflict resolution | Planner spawns a dedicated conflict-resolution Actor | Keeps merge logic in the Planner's decision loop |
| Branch naming | `actor-{task_id}-{timestamp}-{4hex}` | Handles re-runs of same task; timestamp ensures uniqueness |

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│ Planner                                             │
│  ┌──────────┐  ┌──────────────┐  ┌───────────────┐ │
│  │ delegate │  │ apply_patch  │  │ update_state  │ │
│  └────┬─────┘  └──────┬───────┘  └───────────────┘ │
│       │               │                             │
└───────┼───────────────┼─────────────────────────────┘
        │               │
        │  ┌────────────▼──────────┐
        │  │  git apply <patch>    │
        │  │  (to main workspace)  │
        │  └───────────────────────┘
        │
   ┌────▼──────────────────────────────────┐
   │  delegate (semaphore: max 4)          │
   │                                       │
   │  for each subtask:                    │
   │    1. git_utils.setup_worktree()      │
   │    2. copy context_files into wt      │
   │    3. ActorAgent(workspace_dir=wt)    │
   │    4. try: actor.run()                │
   │       finally: teardown_worktree()    │
   │    5. extract_diff() → return         │
   └───────────────────────────────────────┘
```

---

## Files Changed / Created

### NEW: `core/git_utils.py` — Git Worktree Manager

```python
# Key functions:
def setup_worktree(base_dir: str, task_id: str) -> str:
    """git worktree add -b actor-{task_id}-{ts}-{rand} <base_dir>/.worktrees/<name>
    Returns: path to new worktree directory
    """

def teardown_worktree(worktree_path: str) -> None:
    """git worktree remove --force <path> ; git branch -D <branch>"""

def extract_diff(worktree_path: str) -> str:
    """git -C <path> diff HEAD --binary ; also list untracked files"""

def cleanup_orphans(base_dir: str) -> list[str]:
    """Scan .worktrees/ vs git worktree list; prune stale dirs"""

def is_clean(workspace_dir: str) -> bool:
    """Check git status --porcelain is empty (for pre-merge safety)"""
```

### MODIFY: `core/tools/delegate.py` — Worktree Lifecycle

Changes to `run_one()`:
1. Read `context_files` from `self._workspace_dir` (main) BEFORE worktree creation
2. `wt_path = setup_worktree(self._workspace_dir, tid)` inside semaphore block
3. Copy context files into `wt_path`
4. `ActorAgent(..., workspace_dir=wt_path)`
5. `try: summary = await actor.run(...)` / `finally: teardown_worktree(wt_path)`
6. On success: `diff = extract_diff(wt_path)` → include in return dict
7. Store diff in GlobalState via `state.add_summary()` extension

### MODIFY: `core/tools/bash.py` — Extended Blacklist

Add to `BLACKLIST`:
- `git merge`, `git push`, `git rebase`, `git pull`, `git fetch`
- `git worktree` (any subcommand)
- `git branch -D`, `git reset --hard`, `git clean -fd`
- `git remote` (any subcommand)
- `git stash`

### NEW: `core/tools/apply_patch.py` — Planner Merge Tool

```python
class ApplyPatchTool(BaseTool):
    name = "apply_patch"
    # Parameters: diff, task_id, strategy ("strict" | "fuzz")
    # 1. Write diff to temp .patch file
    # 2. git apply --check (dry-run)
    # 3. If pass: git apply
    # 4. If fail + strategy=fuzz: git apply --reject, return conflict info
    # Pre-check: workspace must be clean (git status --porcelain empty)
```

### MODIFY: `core/state.py` — Diff Storage

- `TaskNode` gains `diff: str | None = None` field
- `add_summary()` gains optional `diff: str = ""` parameter
- `snapshot()` includes diff for each task

### MODIFY: `core/system_prompt.py` — Truthful Prompts

- `ACTOR_SYSTEM_PROMPT`: Update to reflect real isolation. Add rule: "Do NOT run git merge, push, rebase, or any remote operations. Your file changes will be collected automatically."
- `PLANNER_SYSTEM_PROMPT`: Add `apply_patch` to available tools. Add merge workflow: "After delegate completes, use apply_patch to merge each Actor's diff into the main workspace. If a patch conflicts, spawn a dedicated Actor to resolve it."

### MODIFY: `core/tools/__init__.py` — Tool Registration

- Add `ApplyPatchTool` to `PLANNER_TOOLS` list

### MODIFY: `.gitignore`

- Add `.worktrees/`

---

## Error Handling Matrix

| Failure Point | Behavior |
|---------------|----------|
| `git worktree add` fails (dirty main workspace) | Return `{status: "failed", error: "worktree setup: ..."}`; Actor never starts |
| `git worktree add` fails (branch already exists) | Branch name includes timestamp+random, collisions near-impossible; if it happens, retry with new random suffix (max 3 attempts) |
| Actor throws exception mid-execution | `finally` block still runs `teardown_worktree`; error surfaced in delegate result |
| `teardown_worktree` fails | Log warning via `log`; mark for `cleanup_orphans` on next call |
| `git apply` conflicts | Return `{applied: false, conflicts: [file_paths], hint: "..."}`; Planner decides next step |
| `git apply` on dirty workspace | `apply_patch` refuses; tells Planner "workspace is dirty, commit or stash first" |
| Process crash mid-delegate | On next startup, `cleanup_orphans()` scans `.worktrees/` and prunes unlisted directories |

---

## Implementation Order

1. **`core/git_utils.py`** — foundation layer, no dependencies on other changes
2. **`core/tools/bash.py`** — blacklist expansion (independent change)
3. **`core/state.py`** — diff storage field (minor change)
4. **`core/tools/delegate.py`** — worktree lifecycle integration (core change)
5. **`core/tools/apply_patch.py`** — new Planner tool
6. **`core/tools/__init__.py`** — register new tool
7. **`core/system_prompt.py`** — prompt updates
8. **`.gitignore`** — final cleanup
