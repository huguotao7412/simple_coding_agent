from __future__ import annotations

import json
from typing import Any, TypedDict


GRAPH_STATE_SCHEMA_VERSION = 1
MAX_GRAPH_STATE_BYTES = 256_000


class GraphState(TypedDict, total=False):
    schema_version: int
    run_id: str
    thread_id: str
    user_request: str
    task_assessment: dict[str, Any]
    execution_policy: dict[str, Any]
    approval_request: dict[str, Any]
    approval_result: dict[str, Any]
    plan_summary: dict[str, Any]
    active_actor_ids: list[str]
    completed_actor_ids: list[str]
    failed_actor_ids: list[str]
    handoff_refs: list[dict[str, Any]]
    artifact_refs: list[dict[str, Any]]
    verification_summary: dict[str, Any]
    usage_summary: dict[str, Any]
    repair_attempts: int
    max_repair_attempts: int
    failure_category: str
    final_status: str
    final_output: str


def validate_graph_state(state: GraphState) -> None:
    if state.get("schema_version") != GRAPH_STATE_SCHEMA_VERSION:
        raise ValueError("unsupported LangGraph state schema")
    run_id = state.get("run_id")
    if not run_id or state.get("thread_id") != run_id:
        raise ValueError("LangGraph run_id/thread_id mismatch")
    encoded = json.dumps(
        state,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_GRAPH_STATE_BYTES:
        raise ValueError("LangGraph state exceeds the bounded serialization limit")


__all__ = [
    "GRAPH_STATE_SCHEMA_VERSION",
    "GraphState",
    "MAX_GRAPH_STATE_BYTES",
    "validate_graph_state",
]
