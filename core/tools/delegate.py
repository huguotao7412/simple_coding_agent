from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace
from typing import Any, cast

from ..a2a_lite.models import AgentHandoff, AgentMessage, ArtifactRef
from ..events import AgentEvent
from .base import BaseTool, ToolResult
from ..actors.contracts import ActorExecutionResult, ActorExecutor, ActorTaskSpec
from ..runs.context import RunContext
from ..runs.task_state import GlobalState
from ..actors.worktree import WorktreeActorExecutor


MAX_CONCURRENT_ACTORS = int(os.getenv("SCA_MAX_ACTORS", "4"))
logger = logging.getLogger(__name__)


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
        llm_client: Any | None = None,
        workspace_dir: str = "",
        state: GlobalState | None = None,
        run_context: RunContext | None = None,
        actor_executor: ActorExecutor | None = None,
    ) -> None:
        super().__init__()
        self._llm = llm_client
        self._workspace_dir = workspace_dir
        self._state = state
        self._run_context = run_context
        self._actor_executor = actor_executor

    def _resolve_run_context(self, state: GlobalState) -> RunContext:
        if self._run_context is not None:
            return self._run_context
        run_context = RunContext.create()
        run_context.state = state
        self._run_context = run_context
        return run_context

    def _resolve_executor(self, workspace_dir: str) -> ActorExecutor | None:
        if self._actor_executor is not None:
            return self._actor_executor
        if self._llm is None:
            return None
        self._actor_executor = WorktreeActorExecutor(
            llm_client=self._llm,
            workspace_dir=workspace_dir,
        )
        return self._actor_executor

    @staticmethod
    def _result_payload(
        result: ActorExecutionResult,
        message: AgentMessage | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": result.task_id,
            "status": result.status,
            "error": result.error,
            "files_modified": list(result.files_modified),
            "bugs_found": list(result.bugs_found),
            "key_findings": result.key_findings[:2000],
            "suggested_next_steps": result.suggested_next_steps,
            "diff_artifact": result.diff_artifact,
            "diff": result.diff[:8000],
            "artifacts": [artifact.to_dict() for artifact in result.artifacts],
        }
        if message is not None:
            payload["artifacts"] = [
                artifact.to_dict() for artifact in message.handoff.artifacts
            ]
            payload["a2a_lite_message"] = message.to_dict()
            payload["a2a_lite_prompt"] = message.to_prompt_json()
        if result.verification_reports:
            final_report = result.verification_reports[-1]
            payload["verification"] = {
                "attempts": len(result.verification_reports),
                "passed": final_report.passed,
                "failure_fingerprint": final_report.failure_fingerprint,
                "reports": [
                    {
                        "attempt": report.attempt,
                        "passed": report.passed,
                        "gates": [
                            {
                                "name": gate.gate_name,
                                "required": gate.required,
                                "passed": gate.passed,
                                "exit_code": gate.exit_code,
                                "timed_out": gate.timed_out,
                                "duration_ms": gate.duration_ms,
                                "execution_backend": gate.execution_backend,
                                "isolated": gate.isolated,
                                "output_artifact": gate.output_artifact,
                                "output_excerpt": gate.output_excerpt[:2000],
                            }
                            for gate in report.results
                        ],
                    }
                    for report in result.verification_reports
                ],
            }
        return payload

    @staticmethod
    async def _record_result(
        run_context: RunContext,
        result: ActorExecutionResult,
    ) -> AgentMessage:
        state = run_context.state
        summary = result.key_findings or result.error or "Task completed."
        artifacts = result.artifacts
        if not artifacts and result.diff_artifact:
            artifacts = (ArtifactRef.create(
                kind="patch",
                uri=result.diff_artifact,
                media_type="text/x-diff",
                producer_task_id=result.task_id,
                content=result.diff,
                description="Full unified diff produced by the Actor.",
            ),)
        handoff = result.handoff or AgentHandoff(
            findings=(summary,),
            unresolved_questions=(
                (result.suggested_next_steps,)
                if result.suggested_next_steps
                else ()
            ),
            artifacts=artifacts,
        )
        message = AgentMessage.handoff_message(
            run_id=run_context.run_id,
            task_id=result.task_id,
            sender_id=f"actor:{result.task_id}",
            recipient_id="planner",
            handoff=handoff,
            failed=result.status == "failed",
        )
        await state.add_summary(
            result.task_id,
            summary,
            diff=result.diff,
            files_modified=list(result.files_modified),
            diff_artifact=result.diff_artifact or None,
            handoff_message=message,
        )
        await state.update_task(result.task_id, status=result.status)
        await run_context.emit(AgentEvent(
            type="a2a_lite_message",
            content=message.to_json(),
            actor_id=result.task_id,
            task_id=result.task_id,
            parent_id="planner",
        ))
        return message

    async def execute(self, **kwargs: Any) -> ToolResult:
        """Schedule ready tasks and delegate single-task execution to an executor."""
        state = self._state or GlobalState.get()
        raw_subtasks = kwargs.pop("subtasks", [])
        if not isinstance(raw_subtasks, list):
            return ToolResult.fail("'subtasks' must be a list")
        subtasks = cast(list[dict[str, Any]], raw_subtasks)
        workspace_dir = str(kwargs.get("workspace_dir", self._workspace_dir))
        executor = self._resolve_executor(workspace_dir)
        if executor is None:
            return ToolResult.fail("Delegate tool is not configured with an LLM client")
        run_context = self._resolve_run_context(state)

        for subtask in subtasks:
            task_id = str(subtask.get("task_id", ""))
            if task_id not in state.task_tree:
                return ToolResult.fail(
                    f"Unknown task_id: {task_id}. Register via update_state first."
                )

        specs: dict[str, ActorTaskSpec] = {}
        for subtask in subtasks:
            task_id = str(subtask["task_id"])
            node = state.task_tree[task_id]
            specs[task_id] = ActorTaskSpec.from_mapping(
                subtask,
                dependencies=tuple(node.dependencies),
            )
            await state.update_task(task_id, status="running")

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_ACTORS)

        async def execute_one(spec: ActorTaskSpec) -> ActorExecutionResult:
            if not spec.description:
                return ActorExecutionResult(
                    task_id=spec.task_id,
                    status="failed",
                    error="Missing description",
                    key_findings="ERROR: LLM failed to provide description",
                )
            async with semaphore:
                dependency_handoffs = tuple(
                    node.handoff_message
                    for dependency_id in spec.dependencies
                    if (
                        (node := state.task_tree.get(dependency_id)) is not None
                        and node.handoff_message is not None
                    )
                )
                resolved_spec = replace(
                    spec,
                    dependency_handoffs=dependency_handoffs,
                )
                return await executor.execute(resolved_spec, run_context)

        completed: set[str] = set()
        failed: set[str] = set()
        all_results: list[dict[str, Any]] = []
        remaining = dict(specs)

        while remaining:
            ready: dict[str, ActorTaskSpec] = {}
            blocked_ids: set[str] = set()
            for task_id, spec in remaining.items():
                dependencies = set(spec.dependencies)
                failed_dependencies = dependencies & failed
                if failed_dependencies:
                    all_results.append({
                        "task_id": task_id,
                        "status": "blocked",
                        "error": (
                            "Blocked: dependency "
                            f"{sorted(failed_dependencies)} failed"
                        ),
                    })
                    await state.update_task(task_id, status="blocked")
                    blocked_ids.add(task_id)
                    continue

                unresolved = {
                    dependency
                    for dependency in dependencies
                    if dependency not in completed and dependency in remaining
                }
                if not unresolved:
                    ready[task_id] = spec

            for task_id in blocked_ids:
                del remaining[task_id]

            if not ready and remaining:
                ready = dict(remaining)
            if not ready:
                break

            ready_specs = list(ready.values())
            raw_results = await asyncio.gather(
                *(execute_one(spec) for spec in ready_specs),
                return_exceptions=True,
            )
            batch_results: list[ActorExecutionResult] = []
            for spec, raw_result in zip(ready_specs, raw_results):
                if isinstance(raw_result, BaseException):
                    logger.error(
                        "Actor executor crashed for %s: %s",
                        spec.task_id,
                        raw_result,
                    )
                    result = ActorExecutionResult(
                        task_id=spec.task_id,
                        status="failed",
                        error=f"Fatal actor error: {raw_result}",
                        key_findings=f"ERROR: Fatal actor error: {raw_result}",
                    )
                else:
                    result = raw_result
                batch_results.append(result)

            for result in batch_results:
                message = await self._record_result(run_context, result)
                all_results.append(self._result_payload(result, message))
                if result.status == "done":
                    completed.add(result.task_id)
                else:
                    failed.add(result.task_id)
                remaining.pop(result.task_id, None)

        done_count = sum(1 for result in all_results if result["status"] == "done")
        blocked_count = sum(
            1 for result in all_results if result.get("status") == "blocked"
        )
        failed_count = sum(
            1 for result in all_results if result.get("status") == "failed"
        )
        lines = [
            f"Delegate complete: {done_count} done, {failed_count} failed, "
            f"{blocked_count} blocked (total {len(all_results)} subtask(s)).\n"
        ]
        for payload in all_results:
            status = payload.get("status", "unknown")
            status_icon = "OK" if status == "done" else (
                "BLOCKED" if status == "blocked" else "FAIL"
            )
            detail = str(
                payload.get("key_findings") or payload.get("error") or ""
            )[:200]
            artifact = payload.get("diff_artifact")
            files = ", ".join(
                cast(list[str], payload.get("files_modified", []))[:5]
            )
            suffix = f" files={files}" if files else ""
            if artifact:
                suffix += f" artifact={artifact}"
            lines.append(
                f"  [{status_icon}] {payload['task_id']}: {detail}{suffix}"
            )
        message_observations = [
            str(payload["a2a_lite_prompt"])
            for payload in all_results
            if payload.get("a2a_lite_prompt")
        ]
        if message_observations:
            lines.append("\n<a2a_lite_messages>")
            lines.extend(message_observations)
            lines.append("</a2a_lite_messages>")
        return ToolResult.ok("\n".join(lines))
