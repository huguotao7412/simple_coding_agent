from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, cast

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from ..events import AgentEvent
from ..application.workflow import (
    AgentWorkflow,
    WorkflowCursor,
    WorkflowStage,
)
from ..execution.models import TaskAssessment
from ..execution.policy import ExecutionPolicy
from ..paths import workspace_state_dir
from ..planner import Planner
from ..runs.models import RunStatus
from .protocol import OrchestrationRequest
from .plan import (
    TaskPlan,
    compile_task_plan,
    validate_task_plan,
)
from .security import validate_artifact_uri
from .state import (
    GRAPH_STATE_SCHEMA_VERSION,
    GraphState,
    validate_graph_state,
)


class LangGraphOrchestrator:
    """LangGraph adapter delegating workflow transitions to AgentWorkflow."""

    def __init__(
        self,
        planner: Planner,
        *,
        checkpointer: Any | None = None,
        checkpoint_path: str | Path | None = None,
    ) -> None:
        self._planner = planner
        self._planner.external_lifecycle = True
        self._checkpointer = checkpointer
        self._checkpoint_path = Path(
            checkpoint_path
            or workspace_state_dir(planner.workspace_dir)
            / "langgraph-checkpoints.sqlite"
        )
        self._events: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._invocation_guard = threading.Lock()
        self._audit_keys: set[str] = set()
        self._workflow = AgentWorkflow()
        # Official SQLite checkpointer guidance: reject arbitrary msgpack modules.
        os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

    @classmethod
    def in_memory(cls, planner: Planner) -> LangGraphOrchestrator:
        return cls(planner, checkpointer=InMemorySaver())

    async def _audit(
        self,
        event_type: str,
        *,
        node: str,
        phase: str,
        payload: dict[str, Any] | None = None,
        route: str = "",
    ) -> None:
        body = dict(payload or {})
        correlation = str(
            body.get("actor_id")
            or body.get("task_id")
            or ""
        )
        event_key = f"{node}:{phase}:{route}:{correlation}"
        body.update({
            "schema_version": GRAPH_STATE_SCHEMA_VERSION,
            "run_id": self._planner.run_context.run_id,
            "thread_id": self._planner.run_context.run_id,
            "node": node,
            "phase": phase,
            "route": route,
            "event_key": event_key,
        })
        context = self._planner.run_context
        duplicate = event_key in self._audit_keys
        if context.store is not None and not duplicate:
            existing = await context.store.list_events(context.run_id)
            duplicate = any(
                item.event_type == event_type
                and item.payload.get("event_key") == event_key
                for item in existing
            )
            if not duplicate:
                import time

                await context.store.append_event(
                    context.run_id,
                    event_type,
                    body,
                    time.time(),
                )
        if duplicate:
            return
        self._audit_keys.add(event_key)
        await self._events.put(AgentEvent(
            type=event_type,
            content=json.dumps(body, ensure_ascii=False, sort_keys=True),
            node_name=node,
            route=route,
            run_id=context.run_id,
            actor_id=str(body.get("actor_id", "")),
            task_id=str(body.get("task_id", "")),
        ))

    def _instrument(
        self,
        name: str,
        handler: Callable[[GraphState], Awaitable[GraphState]],
    ) -> Callable[[GraphState], Awaitable[GraphState]]:
        async def instrumented(state: GraphState) -> GraphState:
            validate_graph_state(state)
            stage = self._workflow_stage(name)
            raw_current = state.get(
                "current_stage",
                WorkflowStage.INGRESS_SECURITY.value,
            )
            cursor = WorkflowCursor(
                schema_version=int(state.get("workflow_schema_version", 1)),
                run_id=self._planner.run_context.run_id,
                stage=WorkflowStage(raw_current),
            )
            cursor = self._workflow.transition(cursor, stage)
            correlation = (
                {"actor_id": state["dispatch_actor_id"]}
                if state.get("dispatch_actor_id")
                else None
            )
            await self._audit(
                "graph_node_started",
                node=name,
                phase="start",
                payload=correlation,
            )
            try:
                result = await handler(state)
            except Exception as error:
                await self._audit(
                    "graph_node_failed",
                    node=name,
                    phase="failure",
                    payload={**(correlation or {}), "error": str(error)},
                )
                raise
            await self._audit(
                "graph_node_completed",
                node=name,
                phase="complete",
                payload=correlation,
            )
            result["workflow_schema_version"] = cursor.schema_version
            result["current_stage"] = cursor.stage.value
            result["domain_state_ref"] = self._planner.run_context.run_id
            return result

        return instrumented

    @staticmethod
    def _workflow_stage(node: str) -> WorkflowStage:
        return {
            "assess_task": WorkflowStage.TASK_ASSESSMENT,
            "compile_policy": WorkflowStage.EXECUTION_POLICY,
            "request_human_approval": WorkflowStage.INPUT_APPROVAL,
            "plan": WorkflowStage.PLANNING,
            "validate_plan": WorkflowStage.PLAN_VALIDATION,
            "schedule_ready_actors": WorkflowStage.ACTOR_SCHEDULING,
            "execute_planner": WorkflowStage.ACTOR_EXECUTION,
            "execute_actor": WorkflowStage.ACTOR_EXECUTION,
            "collect_actor_results": WorkflowStage.ACTOR_EXECUTION,
            "verify": WorkflowStage.VERIFICATION,
            "repair_router": WorkflowStage.VERIFICATION,
            "bounded_repair": WorkflowStage.BOUNDED_REPAIR,
            "finalize_success": WorkflowStage.FINALIZATION,
            "finalize_failure": WorkflowStage.FINALIZATION,
        }[node]

    async def _assess_task(self, state: GraphState) -> GraphState:
        validate_graph_state(state)
        await self._audit("graph_node_started", node="assess_task", phase="start")
        assessment = self._planner.task_assessor.assess(state["user_request"])
        self._planner.current_task_assessment = assessment
        self._planner.ctx.add_system_message(assessment.to_system_message())
        await self._events.put(AgentEvent(
            type="task_assessment",
            content=assessment.to_json(),
            node_name="assess_task",
            run_id=self._planner.run_context.run_id,
        ))
        await self._audit("graph_node_completed", node="assess_task", phase="complete")
        return {"task_assessment": assessment.to_dict()}

    async def _compile_policy(self, state: GraphState) -> GraphState:
        await self._audit("graph_node_started", node="compile_policy", phase="start")
        assessment = TaskAssessment.from_dict(state["task_assessment"])
        preapproved = bool(state.get("approval_result", {}).get("approved", False))
        policy = ExecutionPolicy.from_assessment(
            assessment,
            human_approved=preapproved,
        )
        self._planner.run_context.install_execution_policy(policy)
        self._planner.ctx.add_system_message(policy.to_system_message())
        await self._events.put(AgentEvent(
            type="execution_policy",
            content=policy.to_json(),
            node_name="compile_policy",
            run_id=self._planner.run_context.run_id,
        ))
        await self._planner.run_context.persist_checkpoint(
            self._planner.ctx.messages,
            event_type="graph_policy_compiled",
            status=RunStatus.RUNNING,
        )
        await self._audit(
            "graph_node_completed",
            node="compile_policy",
            phase="complete",
        )
        return {
            "execution_policy": policy.to_dict(),
            "max_repair_attempts": policy.budget.max_repair_attempts,
        }

    def _approval_route(self, state: GraphState) -> str:
        policy = ExecutionPolicy.from_dict(state["execution_policy"])
        trusted = self._planner.run_context.execution_policy
        if trusted is None or policy != trusted:
            raise ValueError("graph policy does not match immutable RunStore policy")
        if not policy.requires_human_approval:
            return "plan"
        if state.get("approval_result", {}).get("approved") is True:
            return "plan"
        if state.get("approval_result", {}).get("approved") is False:
            return "failure"
        return "approval"

    async def _request_approval(self, state: GraphState) -> GraphState:
        policy = ExecutionPolicy.from_dict(state["execution_policy"])
        assessment = TaskAssessment.from_dict(state["task_assessment"])
        request = {
            "schema_version": GRAPH_STATE_SCHEMA_VERSION,
            "run_id": state["run_id"],
            "thread_id": state["thread_id"],
            "assessment_schema_version": assessment.schema_version,
            "risk_level": assessment.risk.value,
            "risk_reasons": list(assessment.reasons),
            "requested_capabilities": [
                "planner_model",
                *[f"actor:{role}" for role in policy.allowed_actor_roles],
            ],
            "target_scope": (
                list(assessment.explicit_paths)
                or [str(Path(self._planner.workspace_dir).resolve())]
            ),
            "policy_summary": {
                "schema_version": policy.schema_version,
                "strategy": policy.strategy.value,
                "max_actors": policy.max_actors,
                "allowed_actor_roles": list(policy.allowed_actor_roles),
                "max_repair_attempts": policy.budget.max_repair_attempts,
                "requires_human_approval": True,
            },
            "plan_summary": "Planning occurs after approval and before model execution.",
        }
        context = self._planner.run_context
        if context.record is None or context.record.status is not RunStatus.PAUSED:
            await context.persist_checkpoint(
                self._planner.ctx.messages,
                event_type="graph_approval_paused",
                status=RunStatus.PAUSED,
            )
            await self._audit(
                "graph_interrupted",
                node="request_human_approval",
                phase="paused",
                payload=request,
            )
        raw_result = interrupt(request)
        result = (
            dict(raw_result)
            if isinstance(raw_result, dict)
            else {"approved": bool(raw_result)}
        )
        approved = bool(result.get("approved", False))
        result = {
            "approved": approved,
            "decided_by": str(result.get("decided_by", "cli")),
            "reason": str(result.get("reason", "")),
        }
        if approved:
            await context.grant_human_approval()
            policy = cast(ExecutionPolicy, context.execution_policy)
            self._planner.ctx.messages = [
                message
                for message in self._planner.ctx.messages
                if not (
                    message.get("role") == "system"
                    and isinstance(message.get("content"), str)
                    and str(message["content"]).startswith("<execution_policy>\n")
                )
            ]
            self._planner.ctx.add_system_message(policy.to_system_message())
            await context.persist_checkpoint(
                self._planner.ctx.messages,
                event_type="graph_approval_granted",
                status=RunStatus.RUNNING,
            )
        else:
            await context.persist_checkpoint(
                self._planner.ctx.messages,
                event_type="graph_approval_rejected",
                status=RunStatus.FAILED,
                error="high-risk execution was not approved",
            )
        await self._audit(
            "graph_resumed",
            node="request_human_approval",
            phase="resumed",
            payload=result,
            route="plan" if approved else "failure",
        )
        return {
            "approval_request": request,
            "approval_result": result,
            "execution_policy": policy.to_dict(),
            "final_status": "" if approved else "rejected",
            "failure_category": "" if approved else "approval_rejected",
            "final_output": (
                "" if approved else "High-risk execution was rejected; no Actor ran."
            ),
        }

    async def _plan(self, state: GraphState) -> GraphState:
        await self._audit(
            "graph_node_started",
            node="plan",
            phase="start",
        )
        policy = self._planner.run_context.execution_policy
        if policy is None or not policy.approval_satisfied:
            raise RuntimeError("execution policy approval is not satisfied")
        assessment = TaskAssessment.from_dict(state["task_assessment"])
        plan = compile_task_plan(state["user_request"], assessment, policy)
        for actor in plan.actors:
            await self._planner.run_context.state.register_task(
                actor.actor_id,
                actor.description,
                list(actor.dependencies),
            )
        await self._planner.run_context.persist_checkpoint(
            self._planner.ctx.messages,
            event_type="graph_plan_persisted",
            status=RunStatus.RUNNING,
        )
        await self._audit(
            "graph_node_completed",
            node="plan",
            phase="complete",
            payload={"plan_digest": plan.to_dict()["digest"]},
        )
        return {"validated_plan": plan.to_dict()}

    async def _validate_plan(self, state: GraphState) -> GraphState:
        await self._audit("graph_node_started", node="validate_plan", phase="start")
        state_policy = ExecutionPolicy.from_dict(state["execution_policy"])
        policy = self._planner.run_context.execution_policy
        if policy is None or state_policy != policy:
            raise ValueError("graph policy does not match immutable RunStore policy")
        plan = TaskPlan.from_dict(state["validated_plan"])
        validate_task_plan(
            plan,
            policy,
            workspace_dir=self._planner.workspace_dir,
        )
        await self._audit(
            "graph_node_completed",
            node="validate_plan",
            phase="complete",
        )
        return {}

    async def _schedule_ready_actors(self, state: GraphState) -> GraphState:
        await self._audit(
            "graph_node_started",
            node="schedule_ready_actors",
            phase="start",
        )
        plan = TaskPlan.from_dict(state["validated_plan"])
        snapshot = await self._planner.run_context.state.snapshot()
        tree = snapshot.get("task_tree", {})
        completed = {
            actor_id
            for actor_id, node in tree.items()
            if isinstance(node, dict) and node.get("status") == "done"
        }
        failed = {
            actor_id
            for actor_id, node in tree.items()
            if isinstance(node, dict) and node.get("status") == "failed"
        }
        policy = self._planner.run_context.execution_policy
        stop_after_failure = bool(
            failed
            and policy is not None
            and not policy.continue_independent_branches
        )
        blocked = {
            actor.actor_id
            for actor in plan.actors
            if (
                stop_after_failure
                and actor.actor_id not in failed | completed
            )
            or set(actor.dependencies)
            & (failed | set(state.get("blocked_actor_ids", [])))
        }
        for actor_id in blocked:
            node = self._planner.run_context.state.task_tree.get(actor_id)
            if node is not None and node.status == "pending":
                await self._planner.run_context.state.update_task(
                    actor_id, status="blocked"
                )
        ready = [
            actor.actor_id
            for actor in plan.actors
            if actor.actor_id not in completed | failed | blocked
            and set(actor.dependencies) <= completed
        ]
        await self._audit(
            "graph_node_completed",
            node="schedule_ready_actors",
            phase="complete",
            payload={"ready_actor_ids": ready},
            route="fan_out" if ready else "collect",
        )
        return {
            "ready_actor_ids": ready,
            "active_actor_ids": ready,
            "blocked_actor_ids": sorted(blocked),
        }

    def _dispatch_route(self, state: GraphState) -> Any:
        plan = TaskPlan.from_dict(state["validated_plan"])
        if plan.direct_planner:
            return "execute_planner"
        ready = state.get("ready_actor_ids", [])
        if not ready:
            return "collect_actor_results"
        return [
            Send(
                "execute_actor",
                {
                    **state,
                    "dispatch_actor_id": actor_id,
                    "actor_results": [],
                },
            )
            for actor_id in ready
        ]

    async def _execute_planner(self, state: GraphState) -> GraphState:
        await self._audit(
            "graph_node_started", node="execute_actor", phase="start", route="planner"
        )
        terminal = ""
        failed = False
        runtime_resume = any(
            message.get("role") == "user"
            for message in self._planner.ctx.messages
        )
        async for event in self._planner.run_prepared_stream(
            "" if runtime_resume else state["user_request"],
            resume=runtime_resume,
        ):
            if event.type in {"done", "error"}:
                terminal = event.content
                failed = event.type == "error"
            await self._events.put(event)
        await self._audit(
            "graph_node_completed",
            node="execute_actor",
            phase="complete",
            route="planner",
        )
        return {
            "final_output": terminal,
            "final_status": "failed" if failed else "executed",
            "failure_category": "runtime_failure" if failed else "",
        }

    async def _execute_actor(self, state: GraphState) -> GraphState:
        actor_id = state["dispatch_actor_id"]
        plan = TaskPlan.from_dict(state["validated_plan"])
        planned = next(
            (actor for actor in plan.actors if actor.actor_id == actor_id),
            None,
        )
        if planned is None:
            raise ValueError(f"unknown dispatched Actor: {actor_id}")
        task_node = self._planner.run_context.state.task_tree[actor_id]
        if task_node.status == "done":
            return {"actor_results": [{
                "actor_id": actor_id,
                "status": "done",
                "replayed": True,
            }]}
        delegate = self._planner.tools_by_name.get("delegate")
        if delegate is None:
            raise RuntimeError("structured Actor execution requires delegate tool")
        attempts = dict(state.get("actor_attempts", {}))
        attempts[actor_id] = attempts.get(actor_id, 0) + 1
        await self._audit(
            "actor_status",
            node="execute_actor",
            phase="start",
            payload={"actor_id": actor_id, "attempt": attempts[actor_id]},
        )
        result = await delegate.execute(subtasks=[{
            "task_id": planned.actor_id,
            "description": planned.description,
            "context_files": list(planned.target_paths),
            "role": planned.role,
        }])
        node = self._planner.run_context.state.task_tree[actor_id]
        await self._audit(
            "actor_status",
            node="execute_actor",
            phase="complete",
            payload={"actor_id": actor_id, "status": node.status},
            route=node.status,
        )
        return {
            "actor_attempts": attempts,
            "actor_results": [{
                "actor_id": actor_id,
                "status": node.status,
                "role": planned.role,
                "error": "" if result.success else result.error,
            }],
        }

    async def _collect_actor_results(self, state: GraphState) -> GraphState:
        await self._audit(
            "graph_node_started", node="collect_actor_results", phase="start"
        )
        snapshot = await self._planner.run_context.state.snapshot()
        tree = snapshot.get("task_tree", {})
        completed = [
            str(task_id)
            for task_id, node in tree.items()
            if isinstance(node, dict) and node.get("status") == "done"
        ]
        failed_ids = [
            str(task_id)
            for task_id, node in tree.items()
            if isinstance(node, dict)
            and node.get("status") == "failed"
        ]
        blocked_ids = [
            str(task_id)
            for task_id, node in tree.items()
            if isinstance(node, dict) and node.get("status") == "blocked"
        ]
        artifact_refs: list[dict[str, Any]] = []
        handoff_refs: list[dict[str, Any]] = []
        for task_id, node in tree.items():
            if not isinstance(node, dict):
                continue
            artifact = node.get("diff_artifact")
            if artifact:
                resolved_artifact = validate_artifact_uri(
                    str(artifact),
                    workspace_dir=self._planner.workspace_dir,
                )
                artifact_refs.append({
                    "task_id": str(task_id),
                    "uri": str(resolved_artifact),
                    "digest": hashlib.sha256(
                        resolved_artifact.read_bytes()
                    ).hexdigest(),
                })
            handoff = node.get("handoff_message")
            if isinstance(handoff, dict):
                handoff_refs.append({
                    "task_id": str(task_id),
                    "message_id": str(handoff.get("message_id", "")),
                })
        apply_tool = self._planner.tools_by_name.get("apply_patch")
        apply_failures: list[str] = []
        if apply_tool is not None:
            for task_id, node in self._planner.run_context.state.task_tree.items():
                if (
                    node.status != "done"
                    or node.actor_role != "coder"
                    or not node.diff
                    or node.patch_applied
                ):
                    continue
                applied = await apply_tool.execute(
                    task_id=task_id,
                    workspace_dir=self._planner.workspace_dir,
                )
                if applied.success:
                    await self._planner.run_context.state.update_task(
                        task_id, patch_applied=True
                    )
                else:
                    apply_failures.append(task_id)
                    await self._events.put(AgentEvent(
                        type="error",
                        content=applied.error or applied.content,
                        actor_id=task_id,
                        task_id=task_id,
                        node_name="collect_actor_results",
                        run_id=self._planner.run_context.run_id,
                    ))
        failed_ids.extend(apply_failures)
        actor_output = "\n".join(
            str(node.get("result_summary", "")).strip()
            for node in tree.values()
            if isinstance(node, dict) and node.get("result_summary")
        )
        await self._audit(
            "graph_node_completed",
            node="collect_actor_results",
            phase="complete",
            route="schedule" if any(
                isinstance(node, dict) and node.get("status") == "pending"
                for node in tree.values()
            ) else "verify",
        )
        return {
            "active_actor_ids": [],
            "completed_actor_ids": completed,
            "failed_actor_ids": sorted(set(failed_ids)),
            "blocked_actor_ids": blocked_ids,
            "artifact_refs": artifact_refs,
            "handoff_refs": handoff_refs,
            "final_output": state.get("final_output", "") or actor_output,
            "final_status": "failed" if failed_ids else "executed",
            "failure_category": "actor_failure" if failed_ids else "",
        }

    def _collect_route(self, state: GraphState) -> str:
        plan = TaskPlan.from_dict(state["validated_plan"])
        terminal = (
            set(state.get("completed_actor_ids", []))
            | set(state.get("failed_actor_ids", []))
            | set(state.get("blocked_actor_ids", []))
        )
        return "verify" if terminal >= {actor.actor_id for actor in plan.actors} else "schedule"

    async def _verify(self, state: GraphState) -> GraphState:
        await self._audit("graph_node_started", node="verify", phase="start")
        failed_ids = state.get("failed_actor_ids", [])
        blocked_ids = state.get("blocked_actor_ids", [])
        summary = {
            "passed": (
                not failed_ids
                and not blocked_ids
                and state.get("final_status") != "failed"
            ),
            "completed_actor_count": len(state.get("completed_actor_ids", [])),
            "failed_actor_count": len(failed_ids),
            "blocked_actor_count": len(blocked_ids),
            "repairable": False,
            "repair_is_data_plane_bounded": True,
        }
        usage = await self._planner.run_context.usage_snapshot()
        route = "success" if summary["passed"] else "failure"
        await self._audit(
            "graph_route_selected",
            node="repair_router",
            phase="route",
            payload=summary,
            route=route,
        )
        return {
            "verification_summary": summary,
            "usage_summary": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "estimated": usage.estimated,
            },
            "final_status": "verified" if summary["passed"] else "failed",
            "failure_category": (
                state.get("failure_category", "")
                if summary["passed"]
                else state.get("failure_category", "") or "actor_failure"
            ),
        }

    async def _repair_router(self, state: GraphState) -> GraphState:
        route = (
            "success"
            if state.get("verification_summary", {}).get("passed")
            else "failure"
        )
        await self._audit(
            "graph_route_selected",
            node="repair_router",
            phase="route",
            route=route,
        )
        return {}

    def _repair_route(self, state: GraphState) -> str:
        if state.get("verification_summary", {}).get("passed"):
            return "success"
        if (
            state.get("verification_summary", {}).get("repairable")
            and state.get("repair_attempts", 0)
            < state.get("max_repair_attempts", 0)
        ):
            return "repair"
        return "failure"

    async def _bounded_repair(self, state: GraphState) -> GraphState:
        attempts = state.get("repair_attempts", 0) + 1
        ledger = self._planner.run_context.budget_ledger
        if ledger is not None:
            await ledger.claim_repair_attempt()
        await self._audit(
            "graph_node_completed",
            node="bounded_repair",
            phase="complete",
            payload={"repair_attempts": attempts},
            route="verify",
        )
        return {"repair_attempts": attempts}

    async def _finalize_success(self, state: GraphState) -> GraphState:
        validate_graph_state(state)
        for artifact in state.get("artifact_refs", []):
            validate_artifact_uri(
                str(artifact.get("uri", "")),
                workspace_dir=self._planner.workspace_dir,
                expected_digest=str(artifact.get("digest", "")),
            )
        context = self._planner.run_context
        await context.persist_checkpoint(
            self._planner.ctx.messages,
            event_type="graph_verification_persisted",
            status=RunStatus.RUNNING,
        )
        await self._audit(
            "graph_node_completed",
            node="finalize_success",
            phase="complete",
        )
        return {"final_status": "graph_committed"}

    async def _finalize_failure(self, state: GraphState) -> GraphState:
        context = self._planner.run_context
        if context.record is not None and context.record.status in {
            RunStatus.CREATED,
            RunStatus.RUNNING,
            RunStatus.PAUSED,
        }:
            await context.persist_checkpoint(
                self._planner.ctx.messages,
                event_type="graph_finalized_failure",
                status=RunStatus.FAILED,
                error=state.get("failure_category", "orchestration failure"),
            )
        await self._audit(
            "graph_node_completed",
            node="finalize_failure",
            phase="complete",
        )
        return {
            "final_status": (
                str(state.get("final_status") or "failed")
                if state.get("final_status") == "rejected"
                else "failed"
            )
        }

    def _build_graph(self, checkpointer: Any) -> Any:
        from .graph_builder import build_graph

        return build_graph(self, checkpointer)

    async def _invoke(
        self,
        request: OrchestrationRequest,
        checkpointer: Any,
    ) -> GraphState:
        graph = self._build_graph(checkpointer)
        config = {
            "configurable": {"thread_id": self._planner.run_context.run_id},
            "recursion_limit": 50,
        }
        if request.resume:
            if (
                self._planner.run_context.record is not None
                and self._planner.run_context.record.status is RunStatus.COMPLETED
            ):
                raise RuntimeError("duplicate resume rejected: Run is already completed")
            snapshot = await graph.aget_state(config)
            waiting_for_approval = "request_human_approval" in snapshot.next
            if waiting_for_approval:
                if request.approval is None:
                    raise RuntimeError(
                        "This LangGraph run is waiting for high-risk approval; "
                        "resume with --approve-high-risk."
                    )
                graph_input: Any = Command(resume={
                    "approved": request.approval,
                    "decided_by": "cli",
                })
            else:
                graph_input = None
        else:
            secured_request, rejection, _ = (
                await self._planner.inspect_user_input(request.user_request)
            )
            if rejection:
                await self._events.put(AgentEvent(
                    type="security_decision",
                    content=rejection,
                    node_name="input_guard",
                    run_id=self._planner.run_context.run_id,
                ))
                raise PermissionError(rejection)
            graph_input = GraphState(
                schema_version=GRAPH_STATE_SCHEMA_VERSION,
                workflow_schema_version=AgentWorkflow.schema_version,
                current_stage=WorkflowStage.INGRESS_SECURITY.value,
                domain_state_ref=self._planner.run_context.run_id,
                run_id=self._planner.run_context.run_id,
                thread_id=self._planner.run_context.run_id,
                user_request=secured_request,
                approval_result=(
                    {"approved": True, "decided_by": "cli"}
                    if request.approval is True
                    else {}
                ),
                active_actor_ids=[],
                ready_actor_ids=[],
                completed_actor_ids=[],
                failed_actor_ids=[],
                blocked_actor_ids=[],
                actor_attempts={},
                actor_results=[],
                handoff_refs=[],
                artifact_refs=[],
                repair_attempts=0,
                max_repair_attempts=0,
                final_status="",
                final_output="",
                final_output_ref="",
                failure_category="",
            )
        result = cast(GraphState, await graph.ainvoke(graph_input, config=config))
        if result.get("final_status") == "graph_committed":
            cursor = WorkflowCursor(
                schema_version=int(result.get("workflow_schema_version", 1)),
                run_id=self._planner.run_context.run_id,
                stage=WorkflowStage(result["current_stage"]),
            )
            for stage in (
                WorkflowStage.FINAL_OUTPUT_SECURITY,
                WorkflowStage.PERSISTENCE_REPORT,
                WorkflowStage.COMPLETED,
            ):
                cursor = self._workflow.transition(cursor, stage)
            result["current_stage"] = cursor.stage.value
            await self._planner.run_context.persist_checkpoint(
                self._planner.ctx.messages,
                event_type="graph_finalized_success",
                status=RunStatus.COMPLETED,
            )
            result["final_status"] = "completed"
        elif result.get("final_status") in {"failed", "rejected"}:
            cursor = WorkflowCursor(
                schema_version=int(result.get("workflow_schema_version", 1)),
                run_id=self._planner.run_context.run_id,
                stage=WorkflowStage(result["current_stage"]),
            )
            result["current_stage"] = self._workflow.transition(
                cursor,
                WorkflowStage.FAILED,
            ).stage.value
        return result

    async def _run(self, request: OrchestrationRequest) -> GraphState:
        if self._checkpointer is not None:
            return await self._invoke(request, self._checkpointer)
        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        manager: AbstractAsyncContextManager[AsyncSqliteSaver] = (
            AsyncSqliteSaver.from_conn_string(str(self._checkpoint_path))
        )
        async with manager as saver:
            await saver.setup()
            return await self._invoke(request, saver)

    async def run_stream(
        self,
        request: OrchestrationRequest,
    ) -> AsyncIterator[AgentEvent]:
        if not self._invocation_guard.acquire(blocking=False):
            yield AgentEvent(
                type="error",
                content="Concurrent resume rejected for this Run.",
                node_name="control_plane",
                run_id=self._planner.run_context.run_id,
            )
            return
        # One orchestrator may be resumed from a different UI event loop.
        # A fresh transport queue keeps the durable graph state loop-independent.
        task: asyncio.Task[GraphState] | None = None
        try:
            self._events = asyncio.Queue()
            task = asyncio.create_task(self._run(request))
            terminal: list[AgentEvent] = []
            while not task.done() or not self._events.empty():
                try:
                    event = await asyncio.wait_for(self._events.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                if event.type in {"done", "error"}:
                    terminal.append(event)
                else:
                    yield event
            try:
                state = await task
            except Exception as error:
                context = self._planner.run_context
                persistence_error: Exception | None = None
                if context.record is not None and context.record.status in {
                    RunStatus.CREATED,
                    RunStatus.RUNNING,
                    RunStatus.PAUSED,
                }:
                    try:
                        await context.persist_checkpoint(
                            self._planner.ctx.messages,
                            event_type="graph_checkpoint_failure",
                            status=RunStatus.FAILED,
                            error=str(error),
                        )
                    except Exception as checkpoint_error:
                        persistence_error = checkpoint_error
                detail = str(error)
                if persistence_error is not None:
                    detail += f"; RunStore failure: {persistence_error}"
                yield AgentEvent(
                    type="error",
                    content=f"LangGraph orchestration failed: {detail}",
                    node_name="control_plane",
                    run_id=context.run_id,
                )
                return
            for event in terminal:
                yield event
            if state.get("final_status") == "rejected" and not terminal:
                yield AgentEvent(
                    type="error",
                    content=state.get(
                        "final_output", "High-risk execution rejected."
                    ),
                    node_name="finalize_failure",
                    run_id=self._planner.run_context.run_id,
                )
            elif state.get("final_status") == "completed" and not terminal:
                yield AgentEvent(
                    type="done",
                    content=state.get("final_output", ""),
                    node_name="finalize_success",
                    run_id=self._planner.run_context.run_id,
                )
            elif state.get("final_status") == "failed" and not terminal:
                yield AgentEvent(
                    type="error",
                    content=state.get("final_output", "")
                    or state.get("failure_category", "orchestration failed"),
                    node_name="finalize_failure",
                    run_id=self._planner.run_context.run_id,
                )
        except asyncio.CancelledError:
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            context = self._planner.run_context
            if (
                context.record is not None
                and context.record.status is RunStatus.RUNNING
            ):
                await asyncio.shield(context.persist_checkpoint(
                    self._planner.ctx.messages,
                    event_type="graph_cancelled",
                    status=RunStatus.PAUSED,
                ))
            raise
        finally:
            self._invocation_guard.release()


__all__ = ["LangGraphOrchestrator"]
