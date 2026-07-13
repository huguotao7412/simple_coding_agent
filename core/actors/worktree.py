from __future__ import annotations

import os
import re
import asyncio
import logging
import shutil
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .contracts import ActorExecutionResult, ActorExecutionStatus, ActorTaskSpec
from ..git_utils import (
    cleanup_orphans,
    extract_diff,
    parse_diff_file_paths,
    setup_worktree,
    teardown_worktree,
)
from ..policy import ToolPolicy
from .roles import ActorRole, get_role_config
from ..runs.context import RunContext
from ..runs.task_state import GlobalState


ARTIFACT_DIR = os.path.join(".sca", "artifacts", "actor-diffs")
logger = logging.getLogger(__name__)

WorktreeFactory = Callable[[str, str], str]
WorktreeCleanup = Callable[[str], None]
DiffExtractor = Callable[[str], Awaitable[str]]
ArtifactWriter = Callable[[str, str, str], str]
ToolProviderFactory = Callable[[RunContext, str], Any]
ActorFactory = Callable[..., Any]


def _resolve_within(root_dir: str, relative_path: str) -> str:
    """Resolve a relative path and reject absolute paths or workspace escape."""
    if os.path.isabs(relative_path):
        raise ValueError("absolute context paths are not allowed")
    root = os.path.realpath(root_dir)
    candidate = os.path.realpath(os.path.join(root, relative_path))
    try:
        contained = os.path.commonpath([root, candidate]) == root
    except ValueError as error:
        raise ValueError("context path is outside the workspace") from error
    if not contained:
        raise ValueError("context path is outside the workspace")
    return candidate


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
    dependency_ids: tuple[str, ...] | list[str],
    state: GlobalState,
) -> list[str]:
    """Apply completed dependency diffs as the baseline for an Actor worktree."""
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


def _default_tool_provider_factory(run_context: RunContext, actor_id: str) -> Any:
    from ..mcp import MCPToolProvider

    return MCPToolProvider(run_context=run_context, actor_id=actor_id)


def _default_actor_factory(**kwargs: Any) -> Any:
    from .agent import ActorAgent

    return ActorAgent(**kwargs)


class WorktreeActorExecutor:
    """Execute one Actor task inside a disposable Git worktree."""

    def __init__(
        self,
        *,
        llm_client: Any,
        workspace_dir: str,
        worktree_factory: WorktreeFactory = setup_worktree,
        worktree_cleanup: WorktreeCleanup = teardown_worktree,
        diff_extractor: DiffExtractor = extract_diff,
        artifact_writer: ArtifactWriter = _write_diff_artifact,
        tool_provider_factory: ToolProviderFactory = _default_tool_provider_factory,
        actor_factory: ActorFactory = _default_actor_factory,
    ) -> None:
        self.llm = llm_client
        self.workspace_dir = workspace_dir
        self.worktree_factory = worktree_factory
        self.worktree_cleanup = worktree_cleanup
        self.diff_extractor = diff_extractor
        self.artifact_writer = artifact_writer
        self.tool_provider_factory = tool_provider_factory
        self.actor_factory = actor_factory
        self._orphan_cleanup_lock = asyncio.Lock()
        self._orphan_cleanup_done = False

    async def _cleanup_orphans_once(self) -> None:
        async with self._orphan_cleanup_lock:
            if self._orphan_cleanup_done:
                return
            self._orphan_cleanup_done = True
            try:
                removed = cleanup_orphans(self.workspace_dir)
                if removed:
                    logger.warning("Cleaned up orphaned worktrees: %s", removed)
            except Exception:
                logger.warning("Orphaned worktree cleanup failed", exc_info=True)

    def _build_injected_context(self, spec: ActorTaskSpec) -> str:
        context_parts = [f"## Task\n{spec.description}"]
        if spec.context_files:
            context_parts.append("\n## Relevant Files")
            for file_path in spec.context_files:
                try:
                    absolute_path = _resolve_within(self.workspace_dir, file_path)
                    with open(
                        absolute_path,
                        "r",
                        encoding="utf-8",
                        errors="replace",
                    ) as context_file:
                        content = context_file.read()[:4000]
                    context_parts.append(f"\n### {file_path}\n```\n{content}\n```")
                except Exception:
                    context_parts.append(f"\n### {file_path}\n(unable to read)")
        if spec.context_summaries:
            context_parts.append("\n## Context from Previous Actors")
            context_parts.extend(f"- {summary}" for summary in spec.context_summaries)
        return "\n".join(context_parts)

    async def execute(
        self,
        spec: ActorTaskSpec,
        run_context: RunContext,
    ) -> ActorExecutionResult:
        worktree_path: str | None = None
        tool_provider: Any | None = None
        phase = "worktree setup/baseline"
        start_time = time.monotonic()

        try:
            await self._cleanup_orphans_once()
            injected_context = self._build_injected_context(spec)
            worktree_path = self.worktree_factory(self.workspace_dir, spec.task_id)
            applied_dependencies = _apply_dependency_diffs_to_worktree(
                worktree_path,
                spec.dependencies,
                run_context.state,
            )
            logger.info(
                "actor_start task_id=%s worktree=%s",
                spec.task_id,
                worktree_path,
            )
            if applied_dependencies:
                logger.info(
                    "actor_baseline task_id=%s dependencies=%s",
                    spec.task_id,
                    ",".join(applied_dependencies),
                )

            try:
                role = ActorRole(spec.role)
            except ValueError:
                role = ActorRole.CODER
            role_config = get_role_config(role)

            phase = "MCP startup"
            tool_provider = self.tool_provider_factory(run_context, spec.task_id)
            await tool_provider.start(
                worktree_path,
                tool_policy=ToolPolicy.for_role(
                    role.value,
                    role_config.tool_allowlist,
                ),
            )

            phase = "actor execution"
            from ..runtime.conversation import ContextManager

            for file_path in spec.context_files:
                try:
                    source = _resolve_within(self.workspace_dir, file_path)
                    destination = _resolve_within(worktree_path, file_path)
                except ValueError:
                    logger.warning(
                        "Rejected context path outside workspace for %s: %s",
                        spec.task_id,
                        file_path,
                    )
                    continue
                if os.path.isfile(source):
                    try:
                        os.makedirs(os.path.dirname(destination), exist_ok=True)
                        shutil.copy2(source, destination)
                    except Exception:
                        pass

            max_steps = spec.max_steps or role_config.default_max_steps
            actor_context = ContextManager(
                system_prompt=role_config.system_prompt,
                max_tokens=self.llm.max_tokens,
            )
            actor_context.add_user_message(injected_context)
            actor = self.actor_factory(
                llm_client=self.llm,
                context_manager=actor_context,
                tools=None,
                tool_provider=tool_provider,
                workspace_dir=worktree_path,
                actor_id=spec.task_id,
                task_context=spec.description,
                max_steps=max_steps,
                run_context=run_context,
            )
            summary = await actor.run(
                "Use the provided context and objective to execute your assigned subtask."
            )

            diff = ""
            files_modified: tuple[str, ...] = ()
            diff_artifact = ""
            try:
                diff = await self.diff_extractor(worktree_path)
                files_modified = tuple(parse_diff_file_paths(diff))
                diff_artifact = self.artifact_writer(
                    self.workspace_dir,
                    spec.task_id,
                    diff,
                )
            except Exception:
                logger.warning("Failed to extract diff for %s", spec.task_id)

            status: ActorExecutionStatus = (
                "done" if summary.status == "done" else "failed"
            )
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.info(
                "actor_end task_id=%s duration_ms=%d outcome=%s files_modified=%d",
                spec.task_id,
                duration_ms,
                status,
                len(files_modified),
            )
            return ActorExecutionResult(
                task_id=spec.task_id,
                status=status,
                files_modified=files_modified,
                bugs_found=tuple(summary.bugs_found),
                key_findings=summary.key_findings or "",
                suggested_next_steps=summary.suggested_next_steps or "",
                diff_artifact=diff_artifact,
                diff=diff,
            )
        except Exception as error:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.error(
                "actor_end task_id=%s duration_ms=%d outcome=failed error=%s",
                spec.task_id,
                duration_ms,
                str(error),
            )
            return ActorExecutionResult(
                task_id=spec.task_id,
                status="failed",
                error=f"{phase}: {error}",
                key_findings=f"ERROR: {phase}: {error}",
            )
        finally:
            if tool_provider is not None:
                try:
                    await tool_provider.shutdown()
                except Exception:
                    logger.warning(
                        "MCP shutdown error for %s",
                        spec.task_id,
                        exc_info=True,
                    )
            if worktree_path is not None:
                try:
                    self.worktree_cleanup(worktree_path)
                except Exception:
                    logger.warning(
                        "Failed to teardown worktree for %s: %s",
                        spec.task_id,
                        worktree_path,
                    )


__all__ = [
    "WorktreeActorExecutor",
    "_apply_dependency_diffs_to_worktree",
    "_write_diff_artifact",
    "parse_diff_file_paths",
]
