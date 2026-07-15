from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, replace
from typing import Any

from .models import ExecutionStrategy, TaskAssessment


EXECUTION_POLICY_SCHEMA_VERSION = 1


class PolicyViolation(RuntimeError):
    """Raised when an execution request contradicts the installed policy."""


class BudgetExceeded(RuntimeError):
    """Raised when a Run attempts to consume beyond a configured limit."""


@dataclass(frozen=True)
class ExecutionBudget:
    max_planner_steps: int
    max_actor_steps: int
    max_model_calls: int
    max_total_tokens: int
    max_wall_time_seconds: float
    max_failed_tool_calls: int
    max_repair_attempts: int

    def __post_init__(self) -> None:
        integer_values = (
            self.max_planner_steps,
            self.max_actor_steps,
            self.max_model_calls,
            self.max_total_tokens,
            self.max_failed_tool_calls,
            self.max_repair_attempts,
        )
        if any(value < 0 for value in integer_values):
            raise ValueError("execution budget values must not be negative")
        if self.max_wall_time_seconds <= 0:
            raise ValueError("max_wall_time_seconds must be positive")

    def to_dict(self) -> dict[str, int | float]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ExecutionBudget:
        return cls(
            max_planner_steps=int(value["max_planner_steps"]),
            max_actor_steps=int(value["max_actor_steps"]),
            max_model_calls=int(value["max_model_calls"]),
            max_total_tokens=int(value["max_total_tokens"]),
            max_wall_time_seconds=float(value["max_wall_time_seconds"]),
            max_failed_tool_calls=int(value["max_failed_tool_calls"]),
            max_repair_attempts=int(value["max_repair_attempts"]),
        )


_BUDGETS: dict[ExecutionStrategy, ExecutionBudget] = {
    ExecutionStrategy.PLANNER_DIRECT: ExecutionBudget(20, 0, 20, 80_000, 300, 5, 0),
    ExecutionStrategy.SINGLE_ACTOR: ExecutionBudget(40, 30, 50, 160_000, 900, 10, 2),
    ExecutionStrategy.CODER_WITH_GATES: ExecutionBudget(40, 30, 50, 160_000, 900, 10, 2),
    ExecutionStrategy.SCOUT_THEN_CODER: ExecutionBudget(50, 40, 90, 280_000, 1500, 16, 2),
    ExecutionStrategy.SCOUT_THEN_DAG: ExecutionBudget(60, 60, 180, 600_000, 2700, 30, 3),
}


@dataclass(frozen=True)
class ExecutionPolicy:
    strategy: ExecutionStrategy
    budget: ExecutionBudget
    max_actors: int
    allowed_actor_roles: tuple[str, ...]
    require_quality_gates: bool
    requires_human_approval: bool
    human_approved: bool = False
    schema_version: int = EXECUTION_POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.max_actors < 0:
            raise ValueError("max_actors must not be negative")
        if self.schema_version != EXECUTION_POLICY_SCHEMA_VERSION:
            raise ValueError(f"unsupported execution policy schema: {self.schema_version}")

    @classmethod
    def from_assessment(
        cls,
        assessment: TaskAssessment,
        *,
        human_approved: bool = False,
    ) -> ExecutionPolicy:
        strategy = assessment.strategy
        roles: tuple[str, ...]
        if strategy is ExecutionStrategy.PLANNER_DIRECT:
            roles = ()
            max_actors = 0
        elif strategy in {
            ExecutionStrategy.SINGLE_ACTOR,
            ExecutionStrategy.CODER_WITH_GATES,
        }:
            roles = ("coder",)
            max_actors = 1
        elif strategy is ExecutionStrategy.SCOUT_THEN_CODER:
            roles = ("scout", "coder")
            max_actors = 2
        else:
            roles = ("scout", "coder", "verifier")
            max_actors = min(4, assessment.max_actors)
        return cls(
            strategy=strategy,
            budget=_BUDGETS[strategy],
            max_actors=max_actors,
            allowed_actor_roles=roles,
            require_quality_gates=(strategy is ExecutionStrategy.CODER_WITH_GATES),
            requires_human_approval=assessment.requires_human_approval,
            human_approved=(
                human_approved and assessment.requires_human_approval
            ),
        )

    @property
    def approval_satisfied(self) -> bool:
        return not self.requires_human_approval or self.human_approved

    def with_approval(self, approved: bool = True) -> ExecutionPolicy:
        return replace(self, human_approved=approved)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "strategy": self.strategy.value,
            "budget": self.budget.to_dict(),
            "max_actors": self.max_actors,
            "allowed_actor_roles": list(self.allowed_actor_roles),
            "require_quality_gates": self.require_quality_gates,
            "requires_human_approval": self.requires_human_approval,
            "human_approved": self.human_approved,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    def to_system_message(self) -> str:
        return (
            "<execution_policy>\n"
            + self.to_json()
            + "\n</execution_policy>\n"
            "This policy is enforced by the runtime. Tool calls cannot override "
            "its topology, approval, or resource limits."
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ExecutionPolicy:
        raw_roles = value.get("allowed_actor_roles", [])
        if not isinstance(raw_roles, list):
            raise ValueError("allowed_actor_roles must be a list")
        raw_budget = value.get("budget")
        if not isinstance(raw_budget, dict):
            raise ValueError("execution policy budget must be an object")
        return cls(
            schema_version=int(value.get("schema_version", 0)),
            strategy=ExecutionStrategy(str(value["strategy"])),
            budget=ExecutionBudget.from_dict(raw_budget),
            max_actors=int(value["max_actors"]),
            allowed_actor_roles=tuple(str(role) for role in raw_roles),
            require_quality_gates=bool(value.get("require_quality_gates", False)),
            requires_human_approval=bool(value.get("requires_human_approval", False)),
            human_approved=bool(value.get("human_approved", False)),
        )


@dataclass(frozen=True)
class BudgetSnapshot:
    model_calls: int = 0
    total_tokens: int = 0
    failed_tool_calls: int = 0
    actors_started: int = 0
    actor_roles: tuple[str, ...] = ()
    completed_actor_roles: tuple[str, ...] = ()
    repair_attempts: int = 0
    active_wall_seconds: float = 0.0

    def __post_init__(self) -> None:
        values = (
            self.model_calls,
            self.total_tokens,
            self.failed_tool_calls,
            self.actors_started,
            self.repair_attempts,
        )
        if any(value < 0 for value in values) or self.active_wall_seconds < 0:
            raise ValueError("budget consumption must not be negative")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = asdict(self)
        payload["actor_roles"] = list(self.actor_roles)
        payload["completed_actor_roles"] = list(self.completed_actor_roles)
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> BudgetSnapshot:
        raw_roles = value.get("actor_roles", [])
        raw_completed_roles = value.get("completed_actor_roles", [])
        if not isinstance(raw_roles, list) or not isinstance(
            raw_completed_roles, list
        ):
            raise ValueError("budget Actor role fields must be lists")
        return cls(
            model_calls=int(value.get("model_calls", 0)),
            total_tokens=int(value.get("total_tokens", 0)),
            failed_tool_calls=int(value.get("failed_tool_calls", 0)),
            actors_started=int(value.get("actors_started", 0)),
            actor_roles=tuple(str(role) for role in raw_roles),
            completed_actor_roles=tuple(
                str(role) for role in raw_completed_roles
            ),
            repair_attempts=int(value.get("repair_attempts", 0)),
            active_wall_seconds=float(value.get("active_wall_seconds", 0.0)),
        )


class RunBudgetLedger:
    """Atomic, Run-scoped consumption ledger shared by Planner and Actors."""

    def __init__(
        self,
        policy: ExecutionPolicy,
        snapshot: BudgetSnapshot | None = None,
    ) -> None:
        self.policy = policy
        self._snapshot = snapshot or BudgetSnapshot()
        self._segment_started = time.monotonic()
        self._lock = asyncio.Lock()

    def _active_wall_seconds(self) -> float:
        return self._snapshot.active_wall_seconds + (
            time.monotonic() - self._segment_started
        )

    def _check_wall_time(self) -> None:
        elapsed = self._active_wall_seconds()
        if elapsed > self.policy.budget.max_wall_time_seconds:
            raise BudgetExceeded(
                "Run active wall-time budget exhausted: "
                f"{elapsed:.1f}s/{self.policy.budget.max_wall_time_seconds:.1f}s"
            )

    async def ensure_can_execute(self) -> None:
        async with self._lock:
            if not self.policy.approval_satisfied:
                raise PolicyViolation(
                    "High-risk task requires explicit human approval before execution"
                )
            self._check_wall_time()

    async def claim_model_call(self) -> None:
        async with self._lock:
            if not self.policy.approval_satisfied:
                raise PolicyViolation(
                    "High-risk task requires explicit human approval before execution"
                )
            self._check_wall_time()
            if self._snapshot.model_calls >= self.policy.budget.max_model_calls:
                raise BudgetExceeded("Run model-call budget exhausted")
            if self._snapshot.total_tokens >= self.policy.budget.max_total_tokens:
                raise BudgetExceeded("Run token budget exhausted")
            self._snapshot = replace(
                self._snapshot,
                model_calls=self._snapshot.model_calls + 1,
            )

    async def charge_tokens(self, count: int) -> None:
        if count < 0:
            raise ValueError("token charge must not be negative")
        async with self._lock:
            self._check_wall_time()
            total = self._snapshot.total_tokens + count
            self._snapshot = replace(self._snapshot, total_tokens=total)
            if total > self.policy.budget.max_total_tokens:
                raise BudgetExceeded(
                    "Run token budget exhausted: "
                    f"{total}/{self.policy.budget.max_total_tokens}"
                )

    async def charge_failed_tool_call(self) -> None:
        async with self._lock:
            self._check_wall_time()
            total = self._snapshot.failed_tool_calls + 1
            self._snapshot = replace(self._snapshot, failed_tool_calls=total)
            if total > self.policy.budget.max_failed_tool_calls:
                raise BudgetExceeded(
                    "Run failed-tool-call budget exhausted: "
                    f"{total}/{self.policy.budget.max_failed_tool_calls}"
                )

    async def reserve_actors(self, roles: tuple[str, ...]) -> None:
        async with self._lock:
            self._check_wall_time()
            total = self._snapshot.actors_started + len(roles)
            if total > self.policy.max_actors:
                raise BudgetExceeded(
                    f"Run Actor budget exhausted: {total}/{self.policy.max_actors}"
                )
            self._snapshot = replace(
                self._snapshot,
                actors_started=total,
                actor_roles=self._snapshot.actor_roles + roles,
            )

    async def claim_repair_attempt(self) -> None:
        async with self._lock:
            self._check_wall_time()
            total = self._snapshot.repair_attempts + 1
            if total > self.policy.budget.max_repair_attempts:
                raise BudgetExceeded(
                    "Run repair-attempt budget exhausted: "
                    f"{total}/{self.policy.budget.max_repair_attempts}"
                )
            self._snapshot = replace(self._snapshot, repair_attempts=total)

    async def record_actor_completed(self, role: str) -> None:
        async with self._lock:
            self._check_wall_time()
            self._snapshot = replace(
                self._snapshot,
                completed_actor_roles=(
                    self._snapshot.completed_actor_roles + (role,)
                ),
            )

    async def snapshot(self) -> BudgetSnapshot:
        async with self._lock:
            return replace(
                self._snapshot,
                active_wall_seconds=self._active_wall_seconds(),
            )


__all__ = [
    "BudgetExceeded",
    "BudgetSnapshot",
    "EXECUTION_POLICY_SCHEMA_VERSION",
    "ExecutionBudget",
    "ExecutionPolicy",
    "PolicyViolation",
    "RunBudgetLedger",
]
