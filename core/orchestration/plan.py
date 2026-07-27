from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..execution.models import ExecutionStrategy, TaskAssessment
from ..execution.policy import ExecutionPolicy


PLAN_SCHEMA_VERSION = 1
MAX_PLAN_TASKS = 32


@dataclass(frozen=True)
class PlannedActor:
    actor_id: str
    role: str
    description: str
    dependencies: tuple[str, ...] = ()
    target_paths: tuple[str, ...] = ()
    verification_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "role": self.role,
            "description": self.description,
            "dependencies": list(self.dependencies),
            "target_paths": list(self.target_paths),
            "verification_required": self.verification_required,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PlannedActor:
        return cls(
            actor_id=str(value["actor_id"]),
            role=str(value["role"]),
            description=str(value["description"]),
            dependencies=tuple(str(item) for item in value.get("dependencies", [])),
            target_paths=tuple(str(item) for item in value.get("target_paths", [])),
            verification_required=bool(value.get("verification_required", False)),
        )


@dataclass(frozen=True)
class TaskPlan:
    actors: tuple[PlannedActor, ...]
    direct_planner: bool
    repair_limit: int
    schema_version: int = PLAN_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "direct_planner": self.direct_planner,
            "repair_limit": self.repair_limit,
            "actors": [actor.to_dict() for actor in self.actors],
        }
        payload["digest"] = plan_digest(payload)
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TaskPlan:
        if int(value.get("schema_version", 0)) != PLAN_SCHEMA_VERSION:
            raise ValueError("unsupported task plan schema")
        actors = value.get("actors")
        if not isinstance(actors, list):
            raise ValueError("task plan actors must be a list")
        plan = cls(
            actors=tuple(PlannedActor.from_dict(item) for item in actors),
            direct_planner=bool(value.get("direct_planner", False)),
            repair_limit=int(value.get("repair_limit", 0)),
        )
        expected = value.get("digest")
        if expected and expected != plan.to_dict()["digest"]:
            raise ValueError("task plan digest mismatch")
        return plan


def plan_digest(payload: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "digest"}
    encoded = json.dumps(
        unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compile_task_plan(
    request: str,
    assessment: TaskAssessment,
    policy: ExecutionPolicy,
) -> TaskPlan:
    """Compile a compact deterministic DAG; it never grants capabilities."""
    targets = tuple(assessment.explicit_paths)
    repair_limit = policy.budget.max_repair_attempts
    if policy.strategy is ExecutionStrategy.PLANNER_DIRECT:
        return TaskPlan(actors=(), direct_planner=True, repair_limit=repair_limit)

    def actor(
        index: int,
        role: str,
        description: str,
        dependencies: tuple[str, ...] = (),
        *,
        verification_required: bool = False,
    ) -> PlannedActor:
        return PlannedActor(
            actor_id=f"actor_{index}",
            role=role,
            description=description,
            dependencies=dependencies,
            target_paths=targets,
            verification_required=verification_required,
        )

    actors: tuple[PlannedActor, ...]
    if policy.strategy in {
        ExecutionStrategy.SINGLE_ACTOR,
        ExecutionStrategy.CODER_WITH_GATES,
    }:
        actors = (actor(
            1,
            "coder",
            request,
            verification_required=policy.require_quality_gates,
        ),)
    elif policy.strategy is ExecutionStrategy.SCOUT_THEN_CODER:
        actors = (
            actor(1, "scout", f"Inspect the workspace for this request: {request}"),
            actor(2, "coder", request, ("actor_1",)),
        )
    else:
        actors = (
            actor(1, "scout", f"Inspect the workspace for this request: {request}"),
            actor(2, "coder", request, ("actor_1",)),
            actor(
                3,
                "verifier",
                f"Verify the implementation for: {request}",
                ("actor_2",),
                verification_required=True,
            ),
        )
    return TaskPlan(actors=actors, direct_planner=False, repair_limit=repair_limit)


def validate_task_plan(
    plan: TaskPlan,
    policy: ExecutionPolicy,
    *,
    workspace_dir: str | Path,
) -> None:
    if len(plan.actors) > min(MAX_PLAN_TASKS, policy.max_actors):
        raise ValueError("task plan exceeds execution policy Actor limit")
    if plan.repair_limit < 0 or plan.repair_limit > policy.budget.max_repair_attempts:
        raise ValueError("task plan exceeds repair limit")
    if plan.direct_planner != (policy.strategy is ExecutionStrategy.PLANNER_DIRECT):
        raise ValueError("task plan execution mode contradicts policy")

    ids = [actor.actor_id for actor in plan.actors]
    if len(ids) != len(set(ids)) or any(not actor_id for actor_id in ids):
        raise ValueError("task plan Actor IDs must be unique and non-empty")
    known = set(ids)
    root = Path(workspace_dir).resolve()
    graph: dict[str, tuple[str, ...]] = {}
    for actor in plan.actors:
        if actor.role not in policy.allowed_actor_roles:
            raise ValueError(f"task plan role is not allowed: {actor.role}")
        if not actor.description.strip():
            raise ValueError("task plan Actor description must not be empty")
        if actor.actor_id in actor.dependencies:
            raise ValueError("task plan Actor cannot depend on itself")
        unknown = set(actor.dependencies) - known
        if unknown:
            raise ValueError(
                "task plan contains unknown dependencies: " + ", ".join(sorted(unknown))
            )
        for target in actor.target_paths:
            candidate = (root / target).resolve() if not Path(target).is_absolute() else Path(target).resolve()
            if candidate != root and not candidate.is_relative_to(root):
                raise ValueError("task plan target escapes workspace")
        graph[actor.actor_id] = actor.dependencies

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(actor_id: str) -> None:
        if actor_id in visiting:
            raise ValueError("task plan DAG contains a cycle")
        if actor_id in visited:
            return
        visiting.add(actor_id)
        for dependency in graph[actor_id]:
            visit(dependency)
        visiting.remove(actor_id)
        visited.add(actor_id)

    for actor_id in ids:
        visit(actor_id)

    if policy.require_quality_gates and not any(
        actor.role == "coder" and actor.verification_required
        for actor in plan.actors
    ):
        raise ValueError("task plan omits required verification")


__all__ = [
    "MAX_PLAN_TASKS",
    "PLAN_SCHEMA_VERSION",
    "PlannedActor",
    "TaskPlan",
    "compile_task_plan",
    "plan_digest",
    "validate_task_plan",
]
