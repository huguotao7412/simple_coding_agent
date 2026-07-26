from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, cast

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..events import AgentEvent
from ..execution.models import TaskAssessment
from ..execution.policy import ExecutionPolicy
from ..paths import workspace_state_dir
from ..planner import Planner
from ..runs.models import RunStatus
from .protocol import OrchestrationRequest
from .security import validate_artifact_uri
from .state import (
    GRAPH_STATE_SCHEMA_VERSION,
    GraphState,
    validate_graph_state,
)


class LangGraphOrchestrator:
    """Durable control plane around the existing secure execution data plane."""

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
        event_key = f"{node}:{phase}:{route}"
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
        if context.store is not None:
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
        await self._events.put(AgentEvent(
            type=event_type,
            content=json.dumps(body, ensure_ascii=False, sort_keys=True),
            node_name=node,
            route=route,
            run_id=context.run_id,
        ))

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
            "schema_version": 1,
            "run_id": state["run_id"],
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
                "strategy": policy.strategy.value,
                "max_actors": policy.max_actors,
                "requires_human_approval": True,
            },
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

    async def _plan_and_execute(self, state: GraphState) -> GraphState:
        await self._audit(
            "graph_node_started",
            node="plan_and_execute_actors",
            phase="start",
        )
        policy = self._planner.run_context.execution_policy
        if policy is None or not policy.approval_satisfied:
            raise RuntimeError("execution policy approval is not satisfied")
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
            and node.get("status") in {"failed", "blocked"}
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
                })
            handoff = node.get("handoff_message")
            if isinstance(handoff, dict):
                handoff_refs.append({
                    "task_id": str(task_id),
                    "message_id": str(handoff.get("message_id", "")),
                })
        await self._audit(
            "graph_node_completed",
            node="plan_and_execute_actors",
            phase="complete",
            route="failure" if failed else "verify",
        )
        return {
            "plan_summary": {"task_count": len(tree)},
            "active_actor_ids": [],
            "completed_actor_ids": completed,
            "failed_actor_ids": failed_ids,
            "artifact_refs": artifact_refs,
            "handoff_refs": handoff_refs,
            "final_output": terminal,
            "final_status": "failed" if failed else "executed",
            "failure_category": "runtime_failure" if failed else "",
        }

    async def _verify(self, state: GraphState) -> GraphState:
        await self._audit("graph_node_started", node="verify", phase="start")
        failed_ids = state.get("failed_actor_ids", [])
        summary = {
            "passed": not failed_ids and state.get("final_status") != "failed",
            "completed_actor_count": len(state.get("completed_actor_ids", [])),
            "failed_actor_count": len(failed_ids),
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

    def _verification_route(self, state: GraphState) -> str:
        return (
            "success"
            if state.get("verification_summary", {}).get("passed")
            else "failure"
        )

    async def _finalize_success(self, state: GraphState) -> GraphState:
        validate_graph_state(state)
        context = self._planner.run_context
        await context.persist_checkpoint(
            self._planner.ctx.messages,
            event_type="graph_finalized_success",
            status=RunStatus.COMPLETED,
        )
        await self._audit(
            "graph_node_completed",
            node="finalize_success",
            phase="complete",
        )
        return {"final_status": "completed"}

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
        builder = StateGraph(GraphState)
        builder.add_node("assess_task", self._assess_task)
        builder.add_node("compile_policy", self._compile_policy)
        builder.add_node("request_human_approval", self._request_approval)
        builder.add_node("plan_and_execute_actors", self._plan_and_execute)
        builder.add_node("verify", self._verify)
        builder.add_node("finalize_success", self._finalize_success)
        builder.add_node("finalize_failure", self._finalize_failure)
        builder.add_edge(START, "assess_task")
        builder.add_edge("assess_task", "compile_policy")
        builder.add_conditional_edges(
            "compile_policy",
            self._approval_route,
            {
                "approval": "request_human_approval",
                "plan": "plan_and_execute_actors",
                "failure": "finalize_failure",
            },
        )
        builder.add_conditional_edges(
            "request_human_approval",
            self._approval_route,
            {
                "approval": "request_human_approval",
                "plan": "plan_and_execute_actors",
                "failure": "finalize_failure",
            },
        )
        builder.add_edge("plan_and_execute_actors", "verify")
        builder.add_conditional_edges(
            "verify",
            self._verification_route,
            {
                "success": "finalize_success",
                "failure": "finalize_failure",
            },
        )
        builder.add_edge("finalize_success", END)
        builder.add_edge("finalize_failure", END)
        return builder.compile(checkpointer=checkpointer)

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
            graph_input = GraphState(
                schema_version=GRAPH_STATE_SCHEMA_VERSION,
                run_id=self._planner.run_context.run_id,
                thread_id=self._planner.run_context.run_id,
                user_request=request.user_request,
                approval_result=(
                    {"approved": True, "decided_by": "cli"}
                    if request.approval is True
                    else {}
                ),
                active_actor_ids=[],
                completed_actor_ids=[],
                failed_actor_ids=[],
                handoff_refs=[],
                artifact_refs=[],
                repair_attempts=0,
                final_status="",
                final_output="",
                failure_category="",
            )
        return cast(GraphState, await graph.ainvoke(graph_input, config=config))

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
        # One orchestrator may be resumed from a different UI event loop.
        # A fresh transport queue keeps the durable graph state loop-independent.
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
                except Exception:
                    pass
            yield AgentEvent(
                type="error",
                content=f"LangGraph orchestration failed: {error}",
                node_name="control_plane",
                run_id=context.run_id,
            )
            return
        for event in terminal:
            yield event
        if state.get("final_status") == "rejected" and not terminal:
            yield AgentEvent(
                type="error",
                content=state.get("final_output", "High-risk execution rejected."),
                node_name="finalize_failure",
                run_id=self._planner.run_context.run_id,
            )


__all__ = ["LangGraphOrchestrator"]
