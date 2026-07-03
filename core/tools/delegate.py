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

class DelegateTool(BaseTool):
    name = "delegate"
    description = (
        "Dispatch multiple subtasks to independent Actor agents for concurrent execution. "
        "Each Actor runs with full tool access. "
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

    def __init__(self, llm_client=None, workspace_dir: str = ""):
        super().__init__()
        self._llm = llm_client
        self._workspace_dir = workspace_dir

    async def execute(self, subtasks: list[dict], **kwargs) -> ToolResult:
        """Dispatch subtasks to Actors concurrently with asyncio gate."""
        from ..agent import ActorAgent
        from ..context import ContextManager
        from ..tools import ACTOR_TOOLS
        from ..system_prompt import ACTOR_SYSTEM_PROMPT

        state = GlobalState.get()

        # Clean up any orphaned worktrees from previous crashes
        try:
            removed = cleanup_orphans(self._workspace_dir)
            if removed:
                logger.warning(f"Cleaned up orphaned worktrees: {removed}")
        except Exception:
            pass  # cleanup is best-effort

        # Validate all task_ids
        for st in subtasks:
            tid = st.get("task_id", "")
            if tid not in state.task_tree:
                return ToolResult.fail(
                    f"Unknown task_id: {tid}. Register via update_state first."
                )

        # Mark all as running
        for st in subtasks:
            state.update_task(st["task_id"], status="running")

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_ACTORS)

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

        # Concurrent execution
        results = await asyncio.gather(*[run_one(st) for st in subtasks])

        # Build return message
        lines = [f"Delegate complete: {len(results)} subtask(s) executed.\n"]
        for r in results:
            status_icon = "OK" if r["status"] == "done" else "FAIL"
            detail = r.get("key_findings", r.get("error", ""))[:200]
            lines.append(f"  [{status_icon}] {r['task_id']}: {detail}")
        return ToolResult.ok("\n".join(lines))
