from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from .state import GraphState


def build_graph(adapter: Any, checkpointer: Any) -> Any:
    """Map thin adapter nodes onto the AgentWorkflow stage graph."""
    builder = StateGraph(GraphState)
    nodes = {
        "assess_task": adapter._assess_task,
        "compile_policy": adapter._compile_policy,
        "request_human_approval": adapter._request_approval,
        "plan": adapter._plan,
        "validate_plan": adapter._validate_plan,
        "schedule_ready_actors": adapter._schedule_ready_actors,
        "execute_planner": adapter._execute_planner,
        "execute_actor": adapter._execute_actor,
        "collect_actor_results": adapter._collect_actor_results,
        "verify": adapter._verify,
        "repair_router": adapter._repair_router,
        "bounded_repair": adapter._bounded_repair,
        "finalize_success": adapter._finalize_success,
        "finalize_failure": adapter._finalize_failure,
    }
    for name, handler in nodes.items():
        builder.add_node(name, adapter._instrument(name, handler))
    builder.add_edge(START, "assess_task")
    builder.add_edge("assess_task", "compile_policy")
    builder.add_conditional_edges(
        "compile_policy",
        adapter._approval_route,
        {
            "approval": "request_human_approval",
            "plan": "plan",
            "failure": "finalize_failure",
        },
    )
    builder.add_conditional_edges(
        "request_human_approval",
        adapter._approval_route,
        {
            "approval": "request_human_approval",
            "plan": "plan",
            "failure": "finalize_failure",
        },
    )
    builder.add_edge("plan", "validate_plan")
    builder.add_edge("validate_plan", "schedule_ready_actors")
    builder.add_conditional_edges(
        "schedule_ready_actors",
        adapter._dispatch_route,
    )
    builder.add_edge("execute_planner", "verify")
    builder.add_edge("execute_actor", "collect_actor_results")
    builder.add_conditional_edges(
        "collect_actor_results",
        adapter._collect_route,
        {"schedule": "schedule_ready_actors", "verify": "verify"},
    )
    builder.add_edge("verify", "repair_router")
    builder.add_conditional_edges(
        "repair_router",
        adapter._repair_route,
        {
            "success": "finalize_success",
            "repair": "bounded_repair",
            "failure": "finalize_failure",
        },
    )
    builder.add_edge("bounded_repair", "verify")
    builder.add_edge("finalize_success", END)
    builder.add_edge("finalize_failure", END)
    return builder.compile(checkpointer=checkpointer)


__all__ = ["build_graph"]
