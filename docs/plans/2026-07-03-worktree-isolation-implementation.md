# Concurrent Actor Git Worktree Isolation — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give each concurrent Actor agent a real `git worktree` (physically isolated working directory) and provide the Planner with an `apply_patch` tool to merge results back to the main workspace.

**Architecture:** A new `core/git_utils.py` module manages worktree create/destroy/diff. `delegate.py` wraps each Actor in a worktree lifecycle (setup → execute → extract diff → teardown). A new `apply_patch.py` Planner tool applies Actor diffs to main workspace via `git apply`. Bash blacklist is extended to block Actor git mutation commands.

**Tech Stack:** Python 3.13 asyncio, git CLI, no new dependencies

**Design doc:** `docs/plans/2026-07-03-worktree-isolation-design.md`

---

### Task 1: Create Git Worktree Manager (`core/git_utils.py`)

**Files:**
- Create: `core/git_utils.py`

**Step 1: Create the module with all functions**

```python
"""Git worktree lifecycle manager for Actor isolation.

Each Actor gets a dedicated worktree on a throwaway branch.
File changes are collected as unified diffs for Planner merge.
"""

from __future__ import annotations

import os
import random
import subprocess
import time


WORKTREES_DIR = ".worktrees"


def _run_git(*args: str, cwd: str | None = None, timeout: int = 30) -> tuple[int, str, str]:
    """Run a git command. Returns (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def setup_worktree(base_dir: str, task_id: str) -> str:
    """Create an isolated git worktree for a single Actor.

    Branch: actor-{task_id}-{timestamp}-{4hex_random}
    Path:   {base_dir}/.worktrees/{branch_name}

    Returns the absolute path to the new worktree directory.

    Raises RuntimeError if git worktree add fails.
    """
    ts = int(time.time())
    suffix = f"{random.randint(0, 0xFFFF):04x}"
    branch = f"actor-{task_id}-{ts}-{suffix}"
    worktrees_root = os.path.join(base_dir, WORKTREES_DIR)
    worktree_path = os.path.join(worktrees_root, branch)

    os.makedirs(worktrees_root, exist_ok=True)

    # Retry up to 3 times if branch name collision (extremely unlikely)
    for attempt in range(3):
        if attempt > 0:
            suffix = f"{random.randint(0, 0xFFFF):04x}"
            branch = f"actor-{task_id}-{ts}-{suffix}"
            worktree_path = os.path.join(worktrees_root, branch)

        rc, stdout, stderr = _run_git(
            "worktree", "add", "-b", branch, worktree_path,
            cwd=base_dir, timeout=60,
        )
        if rc == 0:
            return worktree_path
        # If it's not a branch-exists error, fail immediately
        if "already exists" not in stderr and "already exists" not in stdout:
            raise RuntimeError(f"git worktree add failed: {stderr}")

    raise RuntimeError(f"git worktree add failed after 3 attempts: {stderr}")


def teardown_worktree(worktree_path: str) -> None:
    """Remove a worktree and its associated branch.

    Best-effort: logs warnings but never raises.
    """
    if not os.path.isdir(worktree_path):
        return

    base_dir = _get_main_workspace(worktree_path)

    # 1. Remove the worktree (--force skips safety checks)
    rc, stdout, stderr = _run_git(
        "worktree", "remove", "--force", worktree_path,
        cwd=base_dir, timeout=30,
    )
    if rc != 0:
        # If the worktree is already gone, that's fine
        if "not a working tree" not in stderr:
            import logging
            logging.warning(f"teardown_worktree: git worktree remove failed: {stderr}")

    # 2. Clean up stale worktree metadata
    _run_git("worktree", "prune", cwd=base_dir, timeout=10)

    # 3. Delete the branch (attempt; branch may have been auto-deleted)
    branch_name = os.path.basename(worktree_path)
    _run_git("branch", "-D", branch_name, cwd=base_dir, timeout=10)


def extract_diff(worktree_path: str) -> str:
    """Extract all uncommitted changes from a worktree as a unified diff.

    Includes:
    - Modified tracked files (git diff HEAD)
    - Untracked files (git diff --no-index /dev/null <file> or git add --all + git diff --staged)

    Returns a unified diff string suitable for `git apply`.
    """
    parts: list[str] = []

    # 1. Diff for modified tracked files
    rc, stdout, stderr = _run_git("diff", "HEAD", "--binary", cwd=worktree_path, timeout=30)
    if rc == 0 and stdout:
        parts.append(stdout)

    # 2. Capture untracked files by staging them temporarily, then diffing
    #    Use --no-index approach instead to avoid mutating git state
    untracked = _list_untracked(worktree_path)
    for filepath in untracked:
        diff = _diff_untracked_file(worktree_path, filepath)
        if diff:
            parts.append(diff)

    return "\n".join(parts)


def _list_untracked(worktree_path: str) -> list[str]:
    """List untracked files in the worktree (excluding .git directory entries)."""
    rc, stdout, stderr = _run_git(
        "ls-files", "--others", "--exclude-standard",
        cwd=worktree_path, timeout=10,
    )
    if rc != 0:
        return []
    return [f for f in stdout.split("\n") if f]


def _diff_untracked_file(worktree_path: str, filepath: str) -> str:
    """Generate a diff for a single untracked (new) file by diffing against /dev/null."""
    full_path = os.path.join(worktree_path, filepath)
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        # Binary file — skip diff, just note it
        return f"# Binary/Unreadable new file: {filepath}\n"

    lines = content.splitlines(keepends=True)
    diff_header = f"diff --git a/{filepath} b/{filepath}\nnew file mode 100644\n--- /dev/null\n+++ b/{filepath}\n"
    hunks = [f"+{line}" if not line.endswith("\n") else f"+{line.rstrip('\n')}\n" for line in lines]
    # Re-add final newline if original had none
    return diff_header + "@@ -0,0 +1,{n} @@\n".format(n=len(lines)) + "".join(hunks)


def cleanup_orphans(base_dir: str) -> list[str]:
    """Remove worktree directories not tracked by git worktree list.

    Call on startup or before each delegate batch to recover from crashes.
    Returns list of removed directory paths.
    """
    worktrees_root = os.path.join(base_dir, WORKTREES_DIR)
    if not os.path.isdir(worktrees_root):
        return []

    # Get currently tracked worktree paths
    rc, stdout, stderr = _run_git("worktree", "list", "--porcelain", cwd=base_dir, timeout=10)
    tracked_paths: set[str] = set()
    if rc == 0:
        for line in stdout.split("\n"):
            if line.startswith("worktree "):
                tracked_paths.add(os.path.abspath(line[len("worktree "):]))

    # Scan .worktrees/ for orphaned directories
    removed: list[str] = []
    for entry in os.listdir(worktrees_root):
        entry_path = os.path.abspath(os.path.join(worktrees_root, entry))
        if os.path.isdir(entry_path) and entry_path not in tracked_paths:
            _force_remove_dir(entry_path)
            removed.append(entry_path)

    # Also prune stale git worktree metadata
    _run_git("worktree", "prune", cwd=base_dir, timeout=10)

    return removed


def is_clean(workspace_dir: str) -> bool:
    """Check if the git workspace has no uncommitted changes."""
    rc, stdout, stderr = _run_git("status", "--porcelain", cwd=workspace_dir, timeout=10)
    return rc == 0 and stdout == ""


def _get_main_workspace(worktree_path: str) -> str:
    """Given a worktree path, find the main workspace (the repo root).

    The worktree's .git file points back to the main repo.
    """
    git_file = os.path.join(worktree_path, ".git")
    if os.path.isfile(git_file):
        with open(git_file, "r") as f:
            content = f.read().strip()
        # Format: "gitdir: <path-to-main-repo>/.git/worktrees/<name>"
        if content.startswith("gitdir:"):
            gitdir = content[len("gitdir:"):].strip()
            # Walk up from .git/worktrees/<name> to repo root
            # .git/worktrees/<name> → .git → repo root
            worktrees_dir = os.path.dirname(os.path.dirname(gitdir))  # .git dir
            return os.path.dirname(worktrees_dir)  # repo root
    # Fallback: assume worktree_path's parent structure
    return os.path.dirname(os.path.dirname(worktree_path))


def _force_remove_dir(path: str) -> None:
    """Recursively remove a directory, handling permission issues on Windows."""
    import shutil
    import stat

    def _on_error(func, p, exc_info):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onerror=_on_error)
```

**Step 2: Verify the module imports cleanly**

```bash
python -c "from core.git_utils import setup_worktree, teardown_worktree, extract_diff, cleanup_orphans, is_clean; print('All imports OK')"
```

Expected: `All imports OK`

**Step 3: Test setup_worktree and teardown_worktree manually**

```bash
python -c "
from core.git_utils import setup_worktree, teardown_worktree, is_clean
import os
base = os.getcwd()
print('Creating worktree...')
wt = setup_worktree(base, 'test_task_1')
print(f'Worktree at: {wt}')
print(f'Exists: {os.path.isdir(wt)}')
print(f'Has .git: {os.path.exists(os.path.join(wt, \".git\"))}')
print('Tearing down...')
teardown_worktree(wt)
print(f'Removed: {not os.path.isdir(wt)}')
print('SUCCESS')
"
```

Expected: `SUCCESS` at the end.

**Step 4: Test extract_diff with a simple change**

```bash
python -c "
from core.git_utils import setup_worktree, teardown_worktree, extract_diff
import os
base = os.getcwd()
wt = setup_worktree(base, 'test_diff_task')
# Create a new file
with open(os.path.join(wt, 'hello.txt'), 'w') as f:
    f.write('hello world\n')
diff = extract_diff(wt)
print('--- DIFF ---')
print(diff)
print('--- END ---')
teardown_worktree(wt)
print('Has new file diff:', 'hello.txt' in diff)
"
```

Expected: Diff contains `hello.txt` and the content `hello world`.

**Step 5: Test cleanup_orphans and is_clean**

```bash
python -c "
from core.git_utils import cleanup_orphans, is_clean
import os
base = os.getcwd()
removed = cleanup_orphans(base)
print(f'Orphans cleaned: {removed}')
clean = is_clean(base)
print(f'Workspace clean: {clean}')
print('SUCCESS')
"
```

Expected: `SUCCESS`.

**Step 6: Commit**

```bash
git add core/git_utils.py
git commit -m "feat: add Git Worktree Manager for Actor isolation"
```

---

### Task 2: Expand Bash Blacklist (`core/tools/bash.py`)

**Files:**
- Modify: `core/tools/bash.py:16-35`

**Step 1: Add git-related patterns to BLACKLIST**

The current BLACKLIST on lines 16-35 of `bash.py`:
```python
BLACKLIST = [
    r"rm\s+-r\S*\s+[/~]",
    r"rm\s+--force\S*\s+[/~]",
    r"\bsudo\b",
    r"chmod\s+[-R]*\s*777\s+[/~]",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r":\(\)\s*\{\s*:\|\:&\s*\}\s*;",
    r">\s*/dev/sd[a-z]",
    r"\bformat\s+[A-Za-z]:",
]
```

Add these patterns after the Windows format rule (line 34) and before the closing bracket:

```python
    # --- Git history / remote mutation ---
    r"\bgit\s+merge\b",
    r"\bgit\s+push\b",
    r"\bgit\s+rebase\b",
    r"\bgit\s+pull\b",
    r"\bgit\s+fetch\b",
    # --- Git worktree manipulation ---
    r"\bgit\s+worktree\b",
    # --- Git destructive operations ---
    r"\bgit\s+branch\s+-D\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-fd\b",
    # --- Git remote manipulation ---
    r"\bgit\s+remote\b",
    # --- Git stash (can hide Actor changes from diff extraction) ---
    r"\bgit\s+stash\b",
```

The final BLACKLIST should look like:

```python
BLACKLIST = [
    # Recursive force delete: rm -r /, rm -rf /, rm -rfa /, rm -rf ~, etc.
    r"rm\s+-r\S*\s+[/~]",
    # Force delete with long flag: rm --force /
    r"rm\s+--force\S*\s+[/~]",
    # Privilege escalation
    r"\bsudo\b",
    # Permissive chmod on root/home
    r"chmod\s+[-R]*\s*777\s+[/~]",
    # Filesystem formatting
    r"\bmkfs\b",
    # Raw disk writes
    r"\bdd\s+if=",
    # Fork bomb
    r":\(\)\s*\{\s*:\|\:&\s*\}\s*;",
    # Overwrite block devices
    r">\s*/dev/sd[a-z]",
    # Windows: format drive
    r"\bformat\s+[A-Za-z]:",
    # --- Git history / remote mutation ---
    r"\bgit\s+merge\b",
    r"\bgit\s+push\b",
    r"\bgit\s+rebase\b",
    r"\bgit\s+pull\b",
    r"\bgit\s+fetch\b",
    # --- Git worktree manipulation ---
    r"\bgit\s+worktree\b",
    # --- Git destructive operations ---
    r"\bgit\s+branch\s+-D\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-fd\b",
    # --- Git remote manipulation ---
    r"\bgit\s+remote\b",
    # --- Git stash (can hide Actor changes from diff extraction) ---
    r"\bgit\s+stash\b",
]
```

**Step 2: Verify the blacklist blocks git commands**

```bash
python -c "
from core.tools.bash import BashTool, BLACKLIST
import re

# Test each new pattern
test_commands = [
    'git merge main',
    'git push origin main',
    'git rebase HEAD~2',
    'git pull origin main',
    'git fetch origin',
    'git worktree add /tmp/wt',
    'git branch -D feature-x',
    'git reset --hard HEAD',
    'git clean -fd',
    'git remote add origin url',
    'git stash',
]
for cmd in test_commands:
    blocked = any(re.search(p, cmd) for p in BLACKLIST)
    print(f'  {\"BLOCKED\" if blocked else \"PASSED\"} : {cmd}')
    assert blocked, f'Command should be blocked: {cmd}'

# Verify legitimate git commands still work
safe_commands = [
    'git status',
    'git diff',
    'git log --oneline',
    'git add file.py',
    'git commit -m \"msg\"',
    'git branch',
    'git checkout -b new-feature',
]
for cmd in safe_commands:
    blocked = any(re.search(p, cmd) for p in BLACKLIST)
    print(f'  {\"BLOCKED\" if blocked else \"OK\"} : {cmd}')
    assert not blocked, f'Command should NOT be blocked: {cmd}'

print('All blacklist tests passed')
"
```

Expected: All blocked commands show `BLOCKED`, all safe commands show `OK`, final message: `All blacklist tests passed`.

**Step 3: Commit**

```bash
git add core/tools/bash.py
git commit -m "feat: expand bash blacklist to block Actor git mutation commands"
```

---

### Task 3: Add Diff Storage to GlobalState (`core/state.py`)

**Files:**
- Modify: `core/state.py:7-14` (TaskNode dataclass)
- Modify: `core/state.py:69-75` (add_summary method)
- Modify: `core/state.py:82-96` (snapshot method)

**Step 1: Add `diff` field to TaskNode**

Change the TaskNode dataclass (line 8-14) from:

```python
@dataclass
class TaskNode:
    task_id: str
    description: str
    status: Literal["pending", "running", "done", "failed"] = "pending"
    assigned_actor: str | None = None
    dependencies: list[str] = field(default_factory=list)
    result_summary: str | None = None
```

To:

```python
@dataclass
class TaskNode:
    task_id: str
    description: str
    status: Literal["pending", "running", "done", "failed"] = "pending"
    assigned_actor: str | None = None
    dependencies: list[str] = field(default_factory=list)
    result_summary: str | None = None
    diff: str | None = None  # unified diff from Actor's worktree changes
```

**Step 2: Update `add_summary()` to accept optional diff**

Change the `add_summary` method (lines 69-75) from:

```python
def add_summary(self, task_id: str, summary: str) -> None:
    import time
    self.task_tree[task_id].result_summary = summary
    self.change_log.append(ChangeRecord(
        type="summary_added", task_id=task_id,
        timestamp=time.time(), payload={"summary": summary},
    ))
```

To:

```python
def add_summary(self, task_id: str, summary: str, diff: str = "") -> None:
    import time
    self.task_tree[task_id].result_summary = summary
    self.task_tree[task_id].diff = diff or None
    self.change_log.append(ChangeRecord(
        type="summary_added", task_id=task_id,
        timestamp=time.time(), payload={"summary": summary, "diff": diff},
    ))
```

**Step 3: Update `snapshot()` to include diff**

Change the `snapshot` method (lines 82-96), adding `"diff": t.diff` to the task dict:

```python
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
                "diff": (t.diff or "")[:500],  # truncate for context window
            }
            for tid, t in self.task_tree.items()
        },
        "change_count": len(self.change_log),
    }
```

**Step 4: Verify the changes**

```bash
python -c "
from core.state import GlobalState, TaskNode
state = GlobalState.get()
tid = state.add_task('Test task with diff')
state.add_summary(tid, 'Done', diff='diff --git a/test b/test\n+hello')
node = state.task_tree[tid]
print(f'Summary: {node.result_summary}')
print(f'Diff: {node.diff}')
snap = state.snapshot()
print(f'Snapshot diff: {snap[\"task_tree\"][tid][\"diff\"]}')
GlobalState.reset()
print('SUCCESS')
"
```

Expected: Diff is stored and appears in snapshot. `SUCCESS`.

**Step 5: Commit**

```bash
git add core/state.py
git commit -m "feat: add diff field to TaskNode and add_summary for Actor change tracking"
```

---

### Task 4: Refactor Delegate for Worktree Lifecycle (`core/tools/delegate.py`)

**Files:**
- Modify: `core/tools/delegate.py:78-148` (run_one function)
- Modify: `core/tools/delegate.py:1-9` (imports)

This is the core change — wrapping each Actor in a worktree lifecycle.

**Step 1: Add imports for git_utils**

Add `from ..git_utils import setup_worktree, teardown_worktree, extract_diff, cleanup_orphans` to the imports at the top of `delegate.py`:

Current imports (lines 1-8):
```python
from __future__ import annotations

import asyncio
import os

from .base import BaseTool, ToolResult
from ..state import GlobalState

MAX_CONCURRENT_ACTORS = 4
```

Change to:
```python
from __future__ import annotations

import asyncio
import logging
import os
import shutil

from .base import BaseTool, ToolResult
from ..state import GlobalState
from ..git_utils import setup_worktree, teardown_worktree, extract_diff, cleanup_orphans

MAX_CONCURRENT_ACTORS = 4

logger = logging.getLogger(__name__)
```

**Step 2: Add cleanup_orphans call at the start of execute()**

In the `execute` method (line 55), add orphan cleanup as the first action. Insert after line 62 (`state = GlobalState.get()`):

```python
# Clean up any orphaned worktrees from previous crashes
try:
    removed = cleanup_orphans(self._workspace_dir)
    if removed:
        logger.warning(f"Cleaned up orphaned worktrees: {removed}")
except Exception:
    pass  # cleanup is best-effort
```

**Step 3: Rewrite run_one() with worktree lifecycle**

Replace the entire `run_one` coroutine (lines 78-148). The new version wraps the Actor in worktree create/destroy:

```python
        async def run_one(subtask: dict) -> dict:
            tid = subtask.get("task_id", "")
            if not tid:
                return {"task_id": "unknown", "status": "failed", "error": "LLM failed to provide task_id"}

            description = subtask.get("description", "")
            if not description:
                state.update_task(tid, status="failed")
                state.add_summary(tid, "ERROR: LLM failed to provide description")
                return {"task_id": tid, "status": "failed", "error": "Missing description"}

            context_files = subtask.get("context_files", [])
            context_summaries = subtask.get("context_summaries", [])

            async with semaphore:
                # --- 1. Read context from MAIN workspace before worktree creation ---
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

                # --- 2. Create worktree ---
                wt_path: str | None = None
                try:
                    wt_path = setup_worktree(self._workspace_dir, tid)
                except Exception as e:
                    state.update_task(tid, status="failed")
                    state.add_summary(tid, f"ERROR: worktree setup failed: {e}")
                    return {
                        "task_id": tid,
                        "status": "failed",
                        "error": f"worktree setup: {str(e)}",
                    }

                # --- 3. Copy context files into worktree so Actor sees current state ---
                for fp in context_files:
                    src = os.path.join(self._workspace_dir, fp)
                    dst = os.path.join(wt_path, fp)
                    if os.path.isfile(src):
                        try:
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            shutil.copy2(src, dst)
                        except Exception:
                            pass  # best-effort copy

                # --- 4. Build ActorAgent pointing at worktree ---
                actor_ctx = ContextManager(
                    system_prompt=ACTOR_SYSTEM_PROMPT,
                    max_tokens=self._llm.max_tokens,
                )
                actor_ctx.add_user_message(injected_context)

                actor = ActorAgent(
                    llm_client=self._llm,
                    context_manager=actor_ctx,
                    tools=[t() for t in ACTOR_TOOLS],
                    workspace_dir=wt_path,
                    actor_id=tid,
                    task_context=description,
                )

                # --- 5. Execute Actor; always teardown worktree ---
                try:
                    trigger_prompt = "请基于上述提供的上下文和目标，开始执行你负责的子任务。"
                    summary = await actor.run(trigger_prompt)

                    # Extract diff from worktree changes
                    diff = ""
                    try:
                        diff = extract_diff(wt_path)
                    except Exception:
                        logger.warning(f"Failed to extract diff for {tid}")

                    state.add_summary(tid, summary.key_findings or "Task completed.", diff=diff)
                    state.update_task(tid, status=summary.status)
                    return {
                        "task_id": tid,
                        "status": summary.status,
                        "files_modified": summary.files_modified,
                        "bugs_found": summary.bugs_found,
                        "key_findings": (summary.key_findings or "")[:500],
                        "suggested_next_steps": summary.suggested_next_steps,
                        "diff": diff[:8000],  # truncate for Planner context
                    }
                except Exception as e:
                    state.update_task(tid, status="failed")
                    state.add_summary(tid, f"ERROR: {e}")
                    return {
                        "task_id": tid,
                        "status": "failed",
                        "error": str(e),
                    }
                finally:
                    # --- 6. Teardown worktree (always, even on exception) ---
                    try:
                        teardown_worktree(wt_path)
                    except Exception:
                        logger.warning(f"Failed to teardown worktree for {tid}: {wt_path}")
```

**Step 4: Verify the delegate module imports and parses**

```bash
python -c "
from core.tools.delegate import DelegateTool
print(f'Tool name: {DelegateTool.name}')
print(f'Parameters: {list(DelegateTool.parameters.keys())}')
print('SUCCESS')
"
```

Expected: `Tool name: delegate`, parameters listed, `SUCCESS`.

**Step 5: Commit**

```bash
git add core/tools/delegate.py
git commit -m "feat: integrate worktree lifecycle into delegate for Actor isolation"
```

---

### Task 5: Create ApplyPatch Tool (`core/tools/apply_patch.py`)

**Files:**
- Create: `core/tools/apply_patch.py`

**Step 1: Create the ApplyPatchTool**

```python
"""apply_patch tool — Planner-only tool for merging Actor diffs into the main workspace."""

from __future__ import annotations

import os
import subprocess
import tempfile

from .base import BaseTool, ToolResult
from ..git_utils import is_clean


def _run_git(*args: str, cwd: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run a git command. Returns (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


class ApplyPatchTool(BaseTool):
    name = "apply_patch"
    description = (
        "Apply a unified diff (produced by an Actor) to the main workspace. "
        "Use this to merge Actor changes back after a delegate call completes. "
        "If the patch conflicts, you will receive the conflict details and can "
        "spawn a dedicated Actor to resolve them.\n\n"
        "Strategy: 'strict' (default) fails on any conflict. "
        "'fuzz' applies what it can and writes .rej files for rejected hunks."
    )
    parameters = {
        "diff": {
            "type": "string",
            "description": "The unified diff string to apply. Copy from an Actor's returned diff field.",
        },
        "task_id": {
            "type": "string",
            "description": "The task_id that produced this diff (for tracking).",
        },
        "strategy": {
            "type": "string",
            "enum": ["strict", "fuzz"],
            "description": "Merge strategy: 'strict' (fail on any conflict) or 'fuzz' (apply partial, create .rej files). Default: 'strict'.",
        },
    }
    required_params = ["diff", "task_id"]

    async def execute(
        self,
        diff: str,
        task_id: str,
        strategy: str = "strict",
        workspace_dir: str = "",
    ) -> ToolResult:
        if not diff or not diff.strip():
            return ToolResult.ok(f"No changes to apply for task {task_id} (empty diff).")

        base_dir = workspace_dir or os.getcwd()

        # --- Pre-check: workspace must be clean ---
        if not is_clean(base_dir):
            return ToolResult.fail(
                "Main workspace is dirty (uncommitted changes exist). "
                "Please commit or stash your changes before applying patches. "
                "This ensures patches apply cleanly and conflicts are traceable."
            )

        # --- Write diff to temp file ---
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".patch",
                delete=False,
                encoding="utf-8",
            ) as f:
                f.write(diff)
                patch_path = f.name
        except OSError as e:
            return ToolResult.fail(f"Failed to write patch file: {e}")

        try:
            # --- Dry-run: check if patch applies cleanly ---
            rc, stdout, stderr = _run_git(
                "apply", "--check", patch_path,
                cwd=base_dir, timeout=30,
            )
            if rc != 0:
                # Dry-run failed — try to identify conflicting files
                conflict_files = _parse_conflict_files(stderr)
                if strategy == "fuzz":
                    # Try with --reject to apply what we can
                    rc2, stdout2, stderr2 = _run_git(
                        "apply", "--reject", patch_path,
                        cwd=base_dir, timeout=30,
                    )
                    result_parts = [
                        f"Patch for {task_id} partially applied (fuzz mode).",
                        f"Conflicts in: {', '.join(conflict_files) if conflict_files else 'unknown files'}.",
                    ]
                    if rc2 != 0:
                        result_parts.append(f"Partial application output: {stderr2}")
                    result_parts.append(
                        "Review .rej files in the workspace to resolve rejected hunks."
                    )
                    return ToolResult.ok("\n".join(result_parts))
                else:
                    return ToolResult.fail(
                        f"Patch for {task_id} conflicts with current workspace state.\n"
                        f"Conflicting files: {', '.join(conflict_files) if conflict_files else 'unknown'}.\n"
                        f"Git output: {stderr}\n\n"
                        f"Consider: spawn a resolution Actor to manually merge the changes, "
                        f"or use strategy='fuzz' to apply partial changes."
                    )

            # --- Apply the patch ---
            rc, stdout, stderr = _run_git(
                "apply", patch_path,
                cwd=base_dir, timeout=30,
            )
            if rc == 0:
                return ToolResult.ok(
                    f"Patch for {task_id} applied successfully to main workspace."
                )
            else:
                return ToolResult.fail(f"git apply failed for {task_id}: {stderr}")

        finally:
            # Clean up temp patch file
            try:
                os.unlink(patch_path)
            except OSError:
                pass


def _parse_conflict_files(git_stderr: str) -> list[str]:
    """Extract conflicting file paths from git apply error output."""
    files: list[str] = []
    for line in git_stderr.split("\n"):
        line = line.strip()
        if line.startswith("error: patch failed:") or line.startswith("error:"):
            # Try to extract filename after the colon
            parts = line.split(":", 2)
            if len(parts) >= 3:
                fname = parts[2].strip()
                if fname and fname != "patch failed":
                    files.append(fname)
    return files if files else []
```

**Step 2: Verify the tool module**

```bash
python -c "
from core.tools.apply_patch import ApplyPatchTool
t = ApplyPatchTool()
print(f'Name: {t.name}')
print(f'Params: {list(t.parameters.keys())}')
print(f'Schema valid: {\"function\" in t.schema}')

# Test _parse_conflict_files
from core.tools.apply_patch import _parse_conflict_files
result = _parse_conflict_files('error: patch failed: src/main.py: patch does not apply')
print(f'Parsed conflicts: {result}')
assert 'src/main.py' in result
print('SUCCESS')
"
```

Expected: `SUCCESS`.

**Step 3: Commit**

```bash
git add core/tools/apply_patch.py
git commit -m "feat: add apply_patch tool for Planner merge of Actor diffs"
```

---

### Task 6: Register ApplyPatchTool (`core/tools/__init__.py`)

**Files:**
- Modify: `core/tools/__init__.py:1-9` (imports)
- Modify: `core/tools/__init__.py:22-29` (PLANNER_TOOLS)

**Step 1: Add import and registration**

Current file:
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

ACTOR_TOOLS = [...]
PLANNER_TOOLS = [
    UpdateStateTool,
    DelegateTool,
    ListDirTool,
    SearchCodebaseTool,
    ReadOutlineTool,
]
```

Change imports to add:
```python
from .apply_patch import ApplyPatchTool
```

Change PLANNER_TOOLS to:
```python
PLANNER_TOOLS = [
    UpdateStateTool,
    DelegateTool,
    ApplyPatchTool,
    ListDirTool,
    SearchCodebaseTool,
    ReadOutlineTool,
]
```

Change `__all__` to add `"ApplyPatchTool"`.

**Step 2: Verify imports**

```bash
python -c "
from core.tools import ACTOR_TOOLS, PLANNER_TOOLS, ApplyPatchTool
print(f'Actor tools ({len(ACTOR_TOOLS)}): {[t.name for t in ACTOR_TOOLS]}')
print(f'Planner tools ({len(PLANNER_TOOLS)}): {[t.name for t in PLANNER_TOOLS]}')
assert ApplyPatchTool in PLANNER_TOOLS
assert ApplyPatchTool not in ACTOR_TOOLS
print('SUCCESS')
"
```

Expected: 7 actor tools, 6 planner tools (including apply_patch), `SUCCESS`.

**Step 3: Commit**

```bash
git add core/tools/__init__.py
git commit -m "feat: register ApplyPatchTool in Planner tools"
```

---

### Task 7: Update System Prompts (`core/system_prompt.py`)

**Files:**
- Modify: `core/system_prompt.py:1-56` (both prompts)

**Step 1: Update ACTOR_SYSTEM_PROMPT**

Current (lines 32-56):
```python
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
```

Change to:
```python
ACTOR_SYSTEM_PROMPT = """You are Depth Research Agent (Actor mode), a task execution agent.

You execute a SINGLE, specific subtask assigned by the Planner. You operate in an
isolated git worktree — your file changes will be automatically collected as a diff
and merged back to the main workspace by the Planner. Do NOT plan next steps — the
Planner handles that.

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
- Return a structured summary when done — do NOT chain into unrelated work.
- Before making edits, maintain a mental note of bugs found and files modified.

## Git Restrictions
- Do NOT run git merge, push, rebase, pull, fetch, stash, or any remote operations.
- Do NOT run git worktree, git branch -D, git reset --hard, or git clean -fd.
- Your file changes will be collected automatically — just edit files as needed.
- You may use: git status, git diff, git log, git add, git commit.
"""
```

**Step 2: Update PLANNER_SYSTEM_PROMPT**

Current (lines 1-30):
```python
PLANNER_SYSTEM_PROMPT = """You are Depth Research Agent (Planner mode), a task orchestration agent.

You solve complex programming tasks by decomposing them into subtasks and delegating
execution to isolated Actor agents. You do NOT author code, edit files, or run shell
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
```

Change to:
```python
PLANNER_SYSTEM_PROMPT = """You are Depth Research Agent (Planner mode), a task orchestration agent.

You solve complex programming tasks by decomposing them into subtasks and delegating
execution to isolated Actor agents. You do NOT author code, edit files, or run shell
commands yourself — you orchestrate.

## Your Workflow
1. **Analyze** the user's request and understand the full scope.
2. **Decompose** into independent subtasks. Register each via `update_state` (add_task).
3. **Delegate** batches of subtasks to Actors via the `delegate` tool.
   - Actors run in isolated git worktrees — no file conflicts possible.
   - Inject only the specific context each Actor needs (relevant files, prior summaries).
4. **Merge** Actor results back into the main workspace:
   - Each Actor's return includes a `diff` field with their changes.
   - Use `apply_patch` to apply each diff to the main workspace.
   - If a patch conflicts, analyze the conflict and spawn a dedicated Actor to resolve it.
5. **Evaluate** Actor summaries. If new issues or follow-ups are needed, create and
   delegate additional rounds of subtasks.
6. **Synthesize** a final answer for the user once all subtasks are resolved.

## Tools
- **update_state**: Maintain the task tree and record Actor summaries.
- **delegate**: Dispatch subtasks to Actors for concurrent execution in isolated worktrees.
- **apply_patch**: Apply an Actor's diff back to the main workspace. Use after delegate.
- **list_dir**: Explore project structure.
- **search_codebase**: Locate symbols, classes, functions, or text patterns.
- **read_outline**: View skeleton structure of large files.

## Rules
- Register tasks via `update_state` BEFORE delegating them.
- Group independent subtasks into a single `delegate` call for maximum concurrency.
- Inject only essential context into each Actor — less noise = better results.
- After delegate completes, review each Actor's diff and apply patches with apply_patch.
- If a patch conflicts, spawn a dedicated conflict-resolution Actor to manually merge.
- When an Actor reports bugs or blockers, analyze them before spawning follow-up Actors.
- Prefer reading outlines before reading full files when scoping a task.
"""
```

**Step 3: Verify prompts parse and contain key terms**

```bash
python -c "
from core.system_prompt import ACTOR_SYSTEM_PROMPT, PLANNER_SYSTEM_PROMPT
assert 'worktree' in ACTOR_SYSTEM_PROMPT
assert 'Do NOT run git merge' in ACTOR_SYSTEM_PROMPT
assert 'apply_patch' in PLANNER_SYSTEM_PROMPT
assert 'git worktrees' in PLANNER_SYSTEM_PROMPT
print('ACTOR prompt length:', len(ACTOR_SYSTEM_PROMPT))
print('PLANNER prompt length:', len(PLANNER_SYSTEM_PROMPT))
print('SUCCESS')
"
```

Expected: `SUCCESS`.

**Step 4: Commit**

```bash
git add core/system_prompt.py
git commit -m "feat: update system prompts for real worktree isolation and apply_patch workflow"
```

---

### Task 8: Update .gitignore

**Files:**
- Modify: `.gitignore`

**Step 1: Add `.worktrees/` to .gitignore**

Current `.gitignore`:
```
.env
__pycache__/
*.pyc
.venv/
.pytest_cache/
dist/
*.egg-info/
.idea/
/.claude/
/.superpowers/
```

Add `.worktrees/` after `.venv/`:

```
.env
__pycache__/
*.pyc
.venv/
.worktrees/
.pytest_cache/
dist/
*.egg-info/
.idea/
/.claude/
/.superpowers/
```

**Step 2: Verify git ignores the directory**

```bash
mkdir -p .worktrees/test_verify
git status --porcelain
```

Expected: No output (`.worktrees/` is ignored, not showing in status).

```bash
rmdir .worktrees/test_verify
rmdir .worktrees 2>/dev/null || true
```

**Step 3: Commit**

```bash
git add .gitignore
git commit -m "chore: add .worktrees/ to .gitignore"
```

---

### Task 9: End-to-End Verification

**Files:**
- No new files — manual verification

**Step 1: Run full import chain to confirm no broken references**

```bash
python -c "
# Full import chain: tools → delegate → git_utils → state
from core.tools import ACTOR_TOOLS, PLANNER_TOOLS
from core.tools.delegate import DelegateTool
from core.tools.apply_patch import ApplyPatchTool
from core.git_utils import setup_worktree, teardown_worktree, extract_diff, cleanup_orphans, is_clean
from core.state import GlobalState
from core.system_prompt import ACTOR_SYSTEM_PROMPT, PLANNER_SYSTEM_PROMPT
print('All imports OK')

# Verify tool counts
print(f'ACTOR_TOOLS: {len(ACTOR_TOOLS)} ({[t.__name__ for t in ACTOR_TOOLS]})')
print(f'PLANNER_TOOLS: {len(PLANNER_TOOLS)} ({[t.__name__ for t in PLANNER_TOOLS]})')

# Verify state has diff field
state = GlobalState.get()
tid = state.add_task('e2e test')
state.add_summary(tid, 'Done', diff='test diff')
assert state.task_tree[tid].diff == 'test diff'
GlobalState.reset()
print('State diff field OK')

print('=== ALL CHECKS PASSED ===')
"
```

Expected: `=== ALL CHECKS PASSED ===`.

**Step 2: Run a simulated single-Actor worktree cycle**

```bash
python -c "
import asyncio
import os
from core.git_utils import setup_worktree, teardown_worktree, extract_diff, cleanup_orphans, is_clean

async def simulate():
    base = os.getcwd()
    
    # Clean orphans first
    removed = cleanup_orphans(base)
    print(f'Cleaned orphans: {removed}')
    
    # Verify main workspace is clean
    assert is_clean(base), 'Main workspace should be clean before test'
    print('Main workspace is clean')
    
    # Create worktree
    wt = setup_worktree(base, 'e2e_sim')
    print(f'Worktree created: {wt}')
    assert os.path.isdir(wt)
    
    # Simulate Actor work: create a file
    test_file = os.path.join(wt, 'e2e_test_output.txt')
    with open(test_file, 'w') as f:
        f.write('E2E simulation output\n')
    print(f'Created test file: {test_file}')
    
    # Extract diff
    diff = extract_diff(wt)
    print(f'Diff ({len(diff)} chars):')
    print(diff[:300])
    assert 'e2e_test_output.txt' in diff
    
    # Teardown
    teardown_worktree(wt)
    assert not os.path.isdir(wt)
    print('Worktree removed successfully')
    
    # Main workspace should still be clean (worktree changes don't affect it)
    assert is_clean(base)
    print('Main workspace still clean (no cross-contamination)')
    
    print('=== E2E CYCLE PASSED ===')

asyncio.run(simulate())
"
```

Expected: `=== E2E CYCLE PASSED ===`.

**Step 3: Commit (if any test artifacts left)**

```bash
# Clean up any test artifacts
git status --porcelain
```

Expected: Clean workspace (no output).

---

## Summary

| Task | Files | Key Change |
|------|-------|-----------|
| 1 | `core/git_utils.py` (NEW) | Worktree create/destroy/diff/cleanup |
| 2 | `core/tools/bash.py` | Blacklist: git merge/push/rebase/etc |
| 3 | `core/state.py` | TaskNode.diff field, add_summary(diff=) |
| 4 | `core/tools/delegate.py` | Worktree lifecycle in run_one |
| 5 | `core/tools/apply_patch.py` (NEW) | git apply tool for Planner |
| 6 | `core/tools/__init__.py` | Register ApplyPatchTool |
| 7 | `core/system_prompt.py` | Truthful Actor isolation, Planner merge workflow |
| 8 | `.gitignore` | Ignore .worktrees/ |
| 9 | E2E verification | Full import + simulated Actor cycle |

**Total commits: 8** (one per task, excluding verification)
