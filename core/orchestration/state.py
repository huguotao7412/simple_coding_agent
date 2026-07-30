from __future__ import annotations

import json
import operator
from typing import Annotated, Any, TypedDict, cast


GRAPH_STATE_SCHEMA_VERSION = 2
MAX_GRAPH_STATE_BYTES = 256_000


def _merge_attempts(
    left: dict[str, int],
    right: dict[str, int],
) -> dict[str, int]:
    merged = dict(left)
    for actor_id, attempt in right.items():
        merged[actor_id] = max(merged.get(actor_id, 0), attempt)
    return merged


class GraphState(TypedDict, total=False):
    schema_version: int
    workflow_schema_version: int
    current_stage: str
    domain_state_ref: str
    interrupt_info: dict[str, Any]
    run_id: str
    thread_id: str
    user_request: str
    task_assessment: dict[str, Any]
    execution_policy: dict[str, Any]
    approval_request: dict[str, Any]
    approval_result: dict[str, Any]
    validated_plan: dict[str, Any]
    ready_actor_ids: list[str]
    active_actor_ids: list[str]
    completed_actor_ids: list[str]
    failed_actor_ids: list[str]
    blocked_actor_ids: list[str]
    actor_attempts: Annotated[dict[str, int], _merge_attempts]
    actor_results: Annotated[list[dict[str, Any]], operator.add]
    dispatch_actor_id: str
    handoff_refs: list[dict[str, Any]]
    artifact_refs: list[dict[str, Any]]
    verification_summary: dict[str, Any]
    usage_summary: dict[str, Any]
    repair_attempts: int
    max_repair_attempts: int
    failure_category: str
    final_status: str
    final_output: str
    final_output_ref: str


def migrate_graph_state(value: dict[str, Any]) -> GraphState:
    """Normalize supported checkpoint schemas without inventing graph position."""
    version = int(value.get("schema_version", 0))
    if version == GRAPH_STATE_SCHEMA_VERSION:
        return cast(GraphState, dict(value))
    if version != 1:
        raise ValueError("unsupported LangGraph state schema")
    migrated = dict(value)
    summary = migrated.pop("plan_summary", {})
    migrated["validated_plan"] = {
        "schema_version": 1,
        "direct_planner": True,
        "repair_limit": int(migrated.get("max_repair_attempts", 0)),
        "actors": [],
        "legacy_summary": summary,
    }
    migrated.setdefault("ready_actor_ids", [])
    migrated.setdefault("blocked_actor_ids", [])
    migrated.setdefault("actor_attempts", {})
    migrated.setdefault("actor_results", [])
    migrated.setdefault("final_output_ref", "")
    migrated["schema_version"] = GRAPH_STATE_SCHEMA_VERSION
    return cast(GraphState, migrated)


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
    "migrate_graph_state",
    "validate_graph_state",
]
