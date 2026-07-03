from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time

from .base import BaseTool, ToolResult
from ..state import GlobalState
from ..git_utils import setup_worktree, teardown_worktree, extract_diff, cleanup_orphans

MAX_CONCURRENT_ACTORS = int(os.getenv("SCA_MAX_ACTORS", "4"))

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

        # 动态提取最新的 workspace_dir，回退兜底为初始缓存
        current_workspace = kwargs.get("workspace_dir", self._workspace_dir)

        # Clean up any orphaned worktrees from previous crashes
        try:
            removed = cleanup_orphans(current_workspace)
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
            await state.update_task(st["task_id"], status="running")

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_ACTORS)

        async def run_one(subtask: dict) -> dict:
            tid = subtask.get("task_id", "")
            if not tid:
                return {"task_id": "unknown", "status": "failed", "error": "LLM failed to provide task_id"}

            description = subtask.get("description", "")
            if not description:
                await state.update_task(tid, status="failed")
                await state.add_summary(tid, "ERROR: LLM failed to provide description")
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
                            abs_path = os.path.join(current_workspace, fp)
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
                start_time = time.monotonic()
                try:
                    wt_path = setup_worktree(current_workspace, tid)
                    logger.info("actor_start task_id=%s worktree=%s", tid, wt_path)
                except Exception as e:
                    await state.update_task(tid, status="failed")
                    await state.add_summary(tid, f"ERROR: worktree setup failed: {e}")
                    return {
                        "task_id": tid,
                        "status": "failed",
                        "error": f"worktree setup: {str(e)}",
                    }

                # --- 3. Start MCP Servers for this Actor ---
                from ..mcp import MCPToolProvider  # lazy import to avoid circular dep
                tool_provider = MCPToolProvider()
                try:
                    await tool_provider.start(wt_path)
                except Exception as e:
                    logger.error("MCP startup failed for %s: %s", tid, e)
                    await state.update_task(tid, status="failed")
                    await state.add_summary(tid, f"ERROR: MCP Server 启动失败: {e}")
                    teardown_worktree(wt_path)
                    return {
                        "task_id": tid,
                        "status": "failed",
                        "error": f"MCP startup: {str(e)}",
                    }

                try:
                    # --- 4. Copy context files into worktree ---
                    for fp in context_files:
                        src = os.path.join(current_workspace, fp)
                        dst = os.path.join(wt_path, fp)
                        if os.path.isfile(src):
                            try:
                                os.makedirs(os.path.dirname(dst), exist_ok=True)
                                shutil.copy2(src, dst)
                            except Exception:
                                pass  # best-effort copy

                    # --- 5. Build ActorAgent with MCP tool provider ---
                    actor_ctx = ContextManager(
                        system_prompt=ACTOR_SYSTEM_PROMPT,
                        max_tokens=self._llm.max_tokens,
                    )
                    actor_ctx.add_user_message(injected_context)

                    actor = ActorAgent(
                        llm_client=self._llm,
                        context_manager=actor_ctx,
                        tools=None,                      # 不使用本地工具
                        tool_provider=tool_provider,     # MCP 模式
                        workspace_dir=wt_path,
                        actor_id=tid,
                        task_context=description,
                    )

                    # --- 6. Execute Actor ---
                    trigger_prompt = "请基于上述提供的上下文和目标，开始执行你负责的子任务。"
                    summary = await actor.run(trigger_prompt)

                    # Extract diff from worktree changes
                    diff = ""
                    try:
                        diff = await extract_diff(wt_path)
                    except Exception:
                        logger.warning(f"Failed to extract diff for {tid}")

                    await state.add_summary(tid, summary.key_findings or "Task completed.", diff=diff)
                    await state.update_task(tid, status=summary.status)
                    duration_ms = int((time.monotonic() - start_time) * 1000)
                    logger.info(
                        "actor_end task_id=%s duration_ms=%d outcome=%s files_modified=%d",
                        tid, duration_ms, summary.status, len(summary.files_modified),
                    )
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
                    await state.update_task(tid, status="failed")
                    await state.add_summary(tid, f"ERROR: {e}")
                    duration_ms = int((time.monotonic() - start_time) * 1000)
                    logger.error(
                        "actor_end task_id=%s duration_ms=%d outcome=failed error=%s",
                        tid, duration_ms, str(e),
                    )
                    return {
                        "task_id": tid,
                        "status": "failed",
                        "error": str(e),
                    }
                finally:
                    # --- 7. Cleanup: MCP first, then worktree ---
                    try:
                        await tool_provider.shutdown()
                    except Exception:
                        logger.warning("MCP shutdown error for %s", tid, exc_info=True)
                    try:
                        teardown_worktree(wt_path)
                    except Exception:
                        logger.warning(f"Failed to teardown worktree for {tid}: {wt_path}")

        # DAG-aware execution: topological sort into dependency levels
        # Tasks with no pending dependencies run concurrently; dependent tasks wait.
        # Failed tasks DO NOT unlock their dependents — they are cascaded as "blocked".
        completed: set[str] = set()       # task_ids that succeeded (status == "done")
        failed: set[str] = set()          # task_ids that failed (status != "done")
        all_results: list[dict] = []
        remaining = {st["task_id"]: st for st in subtasks}

        while remaining:
            # Find tasks whose dependencies are all completed (not just done OR failed,
            # but only "done" — a failed dependency blocks downstream tasks)
            ready: dict[str, dict] = {}
            for tid, st in remaining.items():
                node = state.task_tree.get(tid)
                deps = set(node.dependencies) if node else set()

                # A task is blocked if any of its dependencies failed
                if deps & failed:
                    all_results.append({
                        "task_id": tid,
                        "status": "blocked",
                        "error": f"Blocked: dependency {sorted(deps & failed)} failed",
                    })
                    await state.update_task(tid, status="blocked")
                    continue

                unresolved = {d for d in deps if d not in completed and d in remaining}
                if not unresolved:
                    ready[tid] = st

            # Remove blocked tasks from remaining
            remaining = {tid: st for tid, st in remaining.items()
                         if tid not in {r["task_id"] for r in all_results if r["status"] == "blocked"}}

            if not ready and remaining:
                # Circular dependency detected — break the cycle by running all remaining
                ready = dict(remaining)

            if not ready:
                break

            # Execute ready tasks concurrently
            batch_results = await asyncio.gather(
                *[run_one(st) for st in ready.values()],
                return_exceptions=True,
            )

            for r in batch_results:
                # Guard against unhandled exceptions from run_one
                if isinstance(r, BaseException):
                    logger.error(f"run_one raised unhandled exception: {r}")
                    continue

                all_results.append(r)
                if r["status"] == "done":
                    completed.add(r["task_id"])
                else:
                    failed.add(r["task_id"])
                if r["task_id"] in remaining:
                    del remaining[r["task_id"]]

        # Build return message
        done_count = sum(1 for r in all_results if r["status"] == "done")
        blocked_count = sum(1 for r in all_results if r.get("status") == "blocked")
        failed_count = sum(1 for r in all_results if r.get("status") == "failed")
        lines = [
            f"Delegate complete: {done_count} done, {failed_count} failed, "
            f"{blocked_count} blocked (total {len(all_results)} subtask(s)).\n"
        ]
        for r in all_results:
            status = r.get("status", "unknown")
            if status == "done":
                status_icon = "OK"
            elif status == "blocked":
                status_icon = "BLOCKED"
            else:
                status_icon = "FAIL"
            detail = r.get("key_findings", r.get("error", ""))[:200]
            lines.append(f"  [{status_icon}] {r['task_id']}: {detail}")
        return ToolResult.ok("\n".join(lines))
