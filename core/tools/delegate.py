from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time

from .base import BaseTool, ToolResult
from ..state import GlobalState
from ..git_utils import (
    setup_worktree,
    teardown_worktree,
    extract_diff,
    cleanup_orphans,
    parse_diff_file_paths,
)
from ..role_config import ActorRole, get_role_config
from ..policy import ToolPolicy

MAX_CONCURRENT_ACTORS = int(os.getenv("SCA_MAX_ACTORS", "4"))

logger = logging.getLogger(__name__)

ARTIFACT_DIR = os.path.join(".sca", "artifacts", "actor-diffs")


def _run_git(*args: str, cwd: str, timeout: int = 30) -> tuple[int, str, str]:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        timeout=timeout,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _apply_dependency_diffs_to_worktree(
    worktree_path: str,
    dependency_ids: list[str],
    state: GlobalState,
) -> list[str]:
    """Apply completed dependency diffs as the baseline for this Actor worktree."""
    applied: list[str] = []
    for dependency_id in dependency_ids:
        dependency = state.task_tree.get(dependency_id)
        if dependency is None or not dependency.diff:
            continue

        patch_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".patch",
                delete=False,
                encoding="utf-8",
                newline="",
            ) as patch_file:
                patch_file.write(dependency.diff)
                patch_path = patch_file.name

            rc, _, stderr = _run_git("apply", "--check", patch_path, cwd=worktree_path)
            if rc != 0:
                raise RuntimeError(
                    f"dependency {dependency_id} patch does not apply: {stderr}"
                )

            rc, _, stderr = _run_git("apply", patch_path, cwd=worktree_path)
            if rc != 0:
                raise RuntimeError(f"dependency {dependency_id} apply failed: {stderr}")

            applied.append(dependency_id)
        finally:
            if patch_path:
                try:
                    os.unlink(patch_path)
                except OSError:
                    pass

    if applied:
        rc, _, stderr = _run_git("add", "-A", cwd=worktree_path)
        if rc != 0:
            raise RuntimeError(f"failed to stage dependency baseline: {stderr}")

        rc, _, stderr = _run_git(
            "commit",
            "-q",
            "-m",
            "Apply dependency diffs for actor baseline",
            cwd=worktree_path,
        )
        if rc != 0:
            raise RuntimeError(f"failed to commit dependency baseline: {stderr}")

    return applied


def _write_diff_artifact(workspace_dir: str, task_id: str, diff: str) -> str:
    """Persist the full Actor diff and return a workspace-relative path."""
    if not diff.strip():
        return ""

    safe_task_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_id).strip("._") or "task"
    artifact_rel = os.path.join(ARTIFACT_DIR, f"{safe_task_id}.patch")
    artifact_abs = os.path.join(workspace_dir, artifact_rel)
    os.makedirs(os.path.dirname(artifact_abs), exist_ok=True)
    with open(artifact_abs, "w", encoding="utf-8", newline="\n") as artifact_file:
        artifact_file.write(diff)
        if not diff.endswith("\n"):
            artifact_file.write("\n")
    return artifact_rel.replace(os.sep, "/")


class DelegateTool(BaseTool):
    name = "delegate"
    description = (
        "Dispatch multiple subtasks to independent Actor agents for concurrent execution. "
        "Each Actor runs with role-specific tool access. "
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
                    "role": {
                        "type": "string",
                        "enum": ["scout", "coder", "verifier"],
                        "description": "Actor role: scout (read-only explore), coder (implement), verifier (test).",
                        "default": "coder",
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": "Override the role's default max_steps. Use for complex tasks needing more steps.",
                    },
                },
                "required": ["task_id", "description"],
            },
        }
    }
    required_params = ["subtasks"]

    def __init__(
        self,
        llm_client=None,
        workspace_dir: str = "",
        state: GlobalState | None = None,
        run_context=None,
    ):
        super().__init__()
        self._llm = llm_client
        self._workspace_dir = workspace_dir
        self._state = state
        self._run_context = run_context

    async def execute(self, subtasks: list[dict], **kwargs) -> ToolResult:
        """Dispatch subtasks to Actors concurrently with asyncio gate."""
        from ..agent import ActorAgent
        from ..context import ContextManager
        state = self._state or GlobalState.get()

        # Resolve the latest workspace_dir, falling back to the constructor value.
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
            task_node = state.task_tree.get(tid)
            dependencies = list(task_node.dependencies) if task_node else []

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
                    applied_deps = _apply_dependency_diffs_to_worktree(
                        wt_path,
                        dependencies,
                        state,
                    )
                    logger.info("actor_start task_id=%s worktree=%s", tid, wt_path)
                    if applied_deps:
                        logger.info(
                            "actor_baseline task_id=%s dependencies=%s",
                            tid,
                            ",".join(applied_deps),
                        )
                except Exception as e:
                    await state.update_task(tid, status="failed")
                    await state.add_summary(tid, f"ERROR: worktree setup/baseline failed: {e}")
                    return {
                        "task_id": tid,
                        "status": "failed",
                        "error": f"worktree setup/baseline: {str(e)}",
                    }

                # --- 3. Resolve role configuration ---
                role_str = subtask.get("role", "coder")
                try:
                    role = ActorRole(role_str)
                except ValueError:
                    role = ActorRole.CODER
                role_cfg = get_role_config(role)

                # --- 4. Start MCP Servers for this Actor ---
                from ..mcp import MCPToolProvider  # lazy import to avoid circular dep
                tool_provider = MCPToolProvider()
                try:
                    await tool_provider.start(
                        wt_path,
                        tool_policy=ToolPolicy.for_role(
                            role.value,
                            role_cfg.tool_allowlist,
                        ),
                    )
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

                    # --- 6. Build ActorAgent with role-based configuration ---
                    max_steps = subtask.get("max_steps", role_cfg.default_max_steps)
                    actor_ctx = ContextManager(
                        system_prompt=role_cfg.system_prompt,
                        max_tokens=self._llm.max_tokens,
                    )
                    actor_ctx.add_user_message(injected_context)

                    actor = ActorAgent(
                        llm_client=self._llm,
                        context_manager=actor_ctx,
                        tools=None,
                        tool_provider=tool_provider,
                        workspace_dir=wt_path,
                        actor_id=tid,
                        task_context=description,
                        max_steps=max_steps,
                    )

                    # --- 6. Execute Actor ---
                    trigger_prompt = (
                        "Use the provided context and objective to execute your assigned subtask."
                    )
                    summary = await actor.run(trigger_prompt)

                    # Extract diff from worktree changes
                    diff = ""
                    files_modified: list[str] = []
                    diff_artifact = ""
                    try:
                        diff = await extract_diff(wt_path)
                        files_modified = parse_diff_file_paths(diff)
                        diff_artifact = _write_diff_artifact(current_workspace, tid, diff)
                    except Exception:
                        logger.warning(f"Failed to extract diff for {tid}")

                    await state.add_summary(
                        tid,
                        summary.key_findings or "Task completed.",
                        diff=diff,
                        files_modified=files_modified,
                        diff_artifact=diff_artifact or None,
                    )
                    await state.update_task(tid, status=summary.status)
                    duration_ms = int((time.monotonic() - start_time) * 1000)
                    logger.info(
                        "actor_end task_id=%s duration_ms=%d outcome=%s files_modified=%d",
                        tid, duration_ms, summary.status, len(files_modified),
                    )
                    return {
                        "task_id": tid,
                        "status": summary.status,
                        "files_modified": files_modified,
                        "bugs_found": summary.bugs_found,
                        "key_findings": (summary.key_findings or "")[:2000],
                        "suggested_next_steps": summary.suggested_next_steps,
                        "diff_artifact": diff_artifact,
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

            # Execute ready tasks concurrently. Keep return_exceptions=True so one
            # Actor crash does not hide every other Actor's result from Planner.
            ready_items = list(ready.values())
            raw_results = await asyncio.gather(
                *[run_one(st) for st in ready_items],
                return_exceptions=True,
            )
            batch_results: list[dict] = []
            for st, result in zip(ready_items, raw_results):
                tid = st["task_id"]
                if isinstance(result, Exception):
                    logger.error("run_one crashed for %s: %s", tid, result)
                    batch_results.append({
                        "task_id": tid,
                        "status": "failed",
                        "error": f"Fatal actor error: {str(result)}",
                    })
                else:
                    batch_results.append(result)

            for r in batch_results:
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
            artifact = r.get("diff_artifact")
            files = ", ".join(r.get("files_modified", [])[:5])
            suffix = ""
            if files:
                suffix += f" files={files}"
            if artifact:
                suffix += f" artifact={artifact}"
            lines.append(f"  [{status_icon}] {r['task_id']}: {detail}{suffix}")
        return ToolResult.ok("\n".join(lines))
