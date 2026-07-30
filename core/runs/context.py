from __future__ import annotations

import asyncio
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any

from ..execution.policy import (
    BudgetSnapshot,
    ExecutionPolicy,
    RunBudgetLedger,
)
from ..events import AgentEvent, QueueEventSink
from ..domain.runs import RunAggregate, UsageTotals
from .models import RunCheckpoint, RunRecord, RunStatus, transition_run
from .store import RunStore
from .task_state import GlobalState


@dataclass
class RunContext:
    aggregate: RunAggregate
    events: QueueEventSink
    record: RunRecord | None = None
    store: RunStore | None = None
    _usage_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _persistence_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def run_id(self) -> str:
        return self.aggregate.run_id

    @property
    def state(self) -> GlobalState:
        return self.aggregate.state

    @state.setter
    def state(self, value: GlobalState) -> None:
        self.aggregate.state = value

    @property
    def usage(self) -> UsageTotals:
        return self.aggregate.usage

    @property
    def completed_tool_calls(self) -> dict[str, str]:
        return self.aggregate.completed_tool_calls

    @property
    def execution_policy(self) -> ExecutionPolicy | None:
        return self.aggregate.execution_policy

    @execution_policy.setter
    def execution_policy(self, value: ExecutionPolicy | None) -> None:
        self.aggregate.execution_policy = value

    @property
    def budget_ledger(self) -> RunBudgetLedger | None:
        return self.aggregate.budget_ledger

    @budget_ledger.setter
    def budget_ledger(self, value: RunBudgetLedger | None) -> None:
        self.aggregate.budget_ledger = value

    @classmethod
    def create(
        cls,
        run_id: str | None = None,
        *,
        record: RunRecord | None = None,
        store: RunStore | None = None,
    ) -> RunContext:
        resolved_run_id = run_id or (record.run_id if record is not None else None)
        resolved_run_id = resolved_run_id or f"run_{uuid.uuid4().hex[:12]}"
        if record is not None and record.run_id != resolved_run_id:
            raise ValueError("record run_id does not match RunContext run_id")
        return cls(
            aggregate=RunAggregate(
                run_id=resolved_run_id,
                state=GlobalState(),
                lifecycle_status=(
                    record.status.value
                    if record is not None
                    else RunStatus.CREATED.value
                ),
            ),
            events=QueueEventSink(),
            record=record,
            store=store,
        )

    @classmethod
    def from_checkpoint(
        cls,
        record: RunRecord,
        checkpoint: RunCheckpoint,
        *,
        store: RunStore | None = None,
    ) -> RunContext:
        if record.run_id != checkpoint.run_id:
            raise ValueError("checkpoint run_id does not match record run_id")
        policy = (
            ExecutionPolicy.from_dict(checkpoint.execution_policy)
            if checkpoint.execution_policy is not None
            else None
        )
        budget_snapshot = (
            BudgetSnapshot.from_dict(checkpoint.budget_snapshot)
            if checkpoint.budget_snapshot is not None
            else None
        )
        return cls(
            aggregate=RunAggregate(
                run_id=record.run_id,
                state=GlobalState.from_snapshot(checkpoint.task_snapshot),
                lifecycle_status=record.status.value,
                usage=UsageTotals(
                    prompt_tokens=checkpoint.prompt_tokens,
                    completion_tokens=checkpoint.completion_tokens,
                    estimated=checkpoint.usage_estimated,
                ),
                completed_tool_calls=dict(checkpoint.completed_tool_calls or {}),
                execution_policy=policy,
                budget_ledger=(
                    RunBudgetLedger(policy, budget_snapshot)
                    if policy is not None
                    else None
                ),
                conversation=tuple(deepcopy(checkpoint.messages)),
                approval_references=set(
                    (checkpoint.aggregate_snapshot or {}).get(
                        "approval_references", []
                    )
                ),
                artifact_references=list(
                    (checkpoint.aggregate_snapshot or {}).get(
                        "artifact_references", []
                    )
                ),
                verification_summary=dict(
                    (checkpoint.aggregate_snapshot or {}).get(
                        "verification_summary", {}
                    )
                ),
                security_metric_references=dict(
                    (checkpoint.aggregate_snapshot or {}).get(
                        "security_metric_references", {}
                    )
                ),
            ),
            events=QueueEventSink(),
            record=record,
            store=store,
        )

    async def emit(self, event: AgentEvent) -> None:
        if not event.run_id:
            event.run_id = self.run_id
        await self.events.emit(event)

    async def record_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        estimated: bool,
    ) -> UsageTotals:
        async with self._usage_lock:
            self.usage.prompt_tokens += prompt_tokens
            self.usage.completion_tokens += completion_tokens
            self.usage.estimated = self.usage.estimated or estimated
            snapshot = replace(self.usage)
        if self.budget_ledger is not None:
            await self.budget_ledger.charge_tokens(
                prompt_tokens + completion_tokens
            )
        return snapshot

    def install_execution_policy(self, policy: ExecutionPolicy) -> None:
        """Install an immutable policy exactly once for a new Run."""
        if self.execution_policy is not None:
            if self.execution_policy != policy:
                raise ValueError("execution policy is already installed for this Run")
            return
        self.execution_policy = policy
        self.budget_ledger = RunBudgetLedger(policy)

    def begin_interactive_task(self, policy: ExecutionPolicy) -> None:
        """Start a fresh policy scope for a non-durable REPL turn."""
        if self.store is not None or self.record is not None:
            raise ValueError("durable Runs cannot replace their execution policy")
        self.execution_policy = policy
        self.budget_ledger = RunBudgetLedger(policy)

    async def grant_human_approval(self) -> None:
        """Record an explicit external approval while preserving consumption."""
        policy = self.execution_policy
        if (
            policy is None
            or not policy.requires_human_approval
            or policy.human_approved
        ):
            return
        snapshot = (
            await self.budget_ledger.snapshot()
            if self.budget_ledger is not None
            else BudgetSnapshot()
        )
        approved = policy.with_approval(True)
        self.execution_policy = approved
        self.budget_ledger = RunBudgetLedger(approved, snapshot)

    async def usage_snapshot(self) -> UsageTotals:
        async with self._usage_lock:
            return replace(self.usage)

    async def checkpoint(
        self,
        messages: list[dict[str, Any]],
        *,
        saved_at: float | None = None,
    ) -> RunCheckpoint:
        usage = await self.usage_snapshot()
        task_snapshot = await self.state.snapshot(truncate_diffs=False)
        budget_snapshot = (
            await self.budget_ledger.snapshot()
            if self.budget_ledger is not None
            else None
        )
        self.aggregate.conversation = tuple(deepcopy(messages))
        aggregate_snapshot = self.aggregate.snapshot(
            task_snapshot=task_snapshot,
            budget_snapshot=(
                budget_snapshot.to_dict()
                if budget_snapshot is not None
                else None
            ),
        )
        return RunCheckpoint(
            run_id=self.run_id,
            messages=tuple(deepcopy(messages)),
            task_snapshot=task_snapshot,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            usage_estimated=usage.estimated,
            completed_tool_calls=dict(self.completed_tool_calls),
            execution_policy=(
                self.execution_policy.to_dict()
                if self.execution_policy is not None
                else None
            ),
            budget_snapshot=(
                budget_snapshot.to_dict()
                if budget_snapshot is not None
                else None
            ),
            saved_at=time.time() if saved_at is None else saved_at,
            schema_version=2,
            aggregate_snapshot=aggregate_snapshot,
        )

    async def persist_checkpoint(
        self,
        messages: list[dict[str, Any]],
        *,
        event_type: str,
        status: RunStatus | None = None,
        error: str = "",
    ) -> None:
        """Atomically replace the durable checkpoint using optimistic locking."""
        if self.store is None or self.record is None:
            return
        async with self._persistence_lock:
            current = self.record
            now = time.time()
            if status is not None and status is not current.status:
                updated = transition_run(current, status, error=error, now=now)
            else:
                updated = replace(
                    current,
                    version=current.version + 1,
                    updated_at=now,
                    error=error if status is RunStatus.FAILED else current.error,
                )
            checkpoint = await self.checkpoint(messages, saved_at=now)
            await self.store.save_run(
                updated,
                checkpoint,
                expected_version=current.version,
            )
            self.record = updated
            self.aggregate.lifecycle_status = updated.status.value
            await self.store.append_event(
                self.run_id,
                event_type,
                {"status": updated.status.value, "version": updated.version},
                now,
            )


__all__ = ["RunContext", "UsageTotals"]
