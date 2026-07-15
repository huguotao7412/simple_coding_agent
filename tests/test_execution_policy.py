from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.actors.contracts import ActorExecutionResult, ActorTaskSpec
from core.actors.worktree import WorktreeActorExecutor
from core.execution.assessment import TaskAssessor
from core.execution.models import ExecutionStrategy
from core.execution.policy import (
    BudgetExceeded,
    BudgetSnapshot,
    ExecutionBudget,
    ExecutionPolicy,
    PolicyViolation,
    RunBudgetLedger,
)
from core.planner import Planner
from core.runs.context import RunContext
from core.runs.models import RunRecord
from core.runs.sqlite_store import SQLiteRunStore
from core.runtime.conversation import ContextManager
from core.tools.delegate import DelegateTool
from core.tools.base import BaseTool, ToolResult
from core.tools.apply_patch import ApplyPatchTool
from core.verification.models import VerificationConfig


class CountingLLM:
    max_tokens = 128_000

    def __init__(self, *, tokens: int = 0) -> None:
        self.calls = 0
        self.tokens = tokens

    def count_messages_tokens(self, messages):
        return 1

    async def chat(self, messages, tools=None, on_token=None):
        self.calls += 1
        return {
            "role": "assistant",
            "content": "done",
            "_usage": {
                "prompt_tokens": self.tokens,
                "completion_tokens": 0,
                "estimated": False,
            },
        }


class ToolCallingLLM(CountingLLM):
    async def chat(self, messages, tools=None, on_token=None):
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_write",
                    "type": "function",
                    "function": {
                        "name": "apply_patch",
                        "arguments": "{}",
                    },
                }],
            }
        return {"role": "assistant", "content": "refused safely"}


class RecordingPatchTool(BaseTool):
    name = "apply_patch"
    description = "test patch tool"
    parameters = {}

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def execute(self, **kwargs):
        self.calls += 1
        return ToolResult.ok("patched")


class RecordingExecutor:
    def __init__(self) -> None:
        self.specs: list[ActorTaskSpec] = []

    async def execute(
        self,
        spec: ActorTaskSpec,
        run_context: RunContext,
    ) -> ActorExecutionResult:
        self.specs.append(spec)
        return ActorExecutionResult(
            task_id=spec.task_id,
            status="done",
            key_findings="done",
        )


class MinimalProvider:
    async def start(self, workspace_dir: str, tool_policy=None) -> None:
        return None

    async def shutdown(self) -> None:
        return None


def _policy(
    strategy: ExecutionStrategy,
    *,
    max_actors: int,
    roles: tuple[str, ...],
    max_tokens: int = 100,
    max_model_calls: int = 10,
    requires_approval: bool = False,
    approved: bool = False,
) -> ExecutionPolicy:
    return ExecutionPolicy(
        strategy=strategy,
        budget=ExecutionBudget(
            max_planner_steps=10,
            max_actor_steps=10,
            max_model_calls=max_model_calls,
            max_total_tokens=max_tokens,
            max_wall_time_seconds=60,
            max_failed_tool_calls=2,
            max_repair_attempts=1,
        ),
        max_actors=max_actors,
        allowed_actor_roles=roles,
        require_quality_gates=(strategy is ExecutionStrategy.CODER_WITH_GATES),
        requires_human_approval=requires_approval,
        human_approved=approved,
    )


def test_policy_compiles_deterministically_from_assessment(tmp_path: Path) -> None:
    assessment = TaskAssessor(tmp_path).assess("Explain app.py")

    policy = ExecutionPolicy.from_assessment(assessment)
    restored = ExecutionPolicy.from_dict(policy.to_dict())

    assert policy.strategy is ExecutionStrategy.PLANNER_DIRECT
    assert policy.max_actors == 0
    assert policy.allowed_actor_roles == ()
    assert restored == policy
    assert json.loads(policy.to_json()) == policy.to_dict()


@pytest.mark.asyncio
async def test_high_risk_policy_stops_before_first_model_call(tmp_path: Path) -> None:
    llm = CountingLLM()
    planner = Planner(
        llm_client=llm,
        context_manager=ContextManager(system_prompt="system"),
        tools=[],
        workspace_dir=str(tmp_path),
    )

    events = [
        event
        async for event in planner.run_stream(
            "Deploy the database migration to production"
        )
    ]

    assert [event.type for event in events[:2]] == [
        "task_assessment",
        "execution_policy",
    ]
    assert any(event.type == "policy_denied" for event in events)
    assert any(event.type == "error" for event in events)
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_explicit_high_risk_approval_allows_model_call(tmp_path: Path) -> None:
    llm = CountingLLM()
    planner = Planner(
        llm_client=llm,
        context_manager=ContextManager(system_prompt="system"),
        tools=[],
        workspace_dir=str(tmp_path),
        high_risk_approved=True,
    )

    events = [
        event
        async for event in planner.run_stream(
            "Deploy the database migration to production"
        )
    ]

    assert any(event.type == "done" for event in events)
    assert not any(event.type == "policy_denied" for event in events)
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_interactive_planner_starts_fresh_policy_per_turn(tmp_path: Path) -> None:
    llm = CountingLLM()
    planner = Planner(
        llm_client=llm,
        context_manager=ContextManager(system_prompt="system"),
        tools=[],
        workspace_dir=str(tmp_path),
    )

    first = [event async for event in planner.run_stream("Explain app.py")]
    second = [event async for event in planner.run_stream("Fix app.py")]

    first_policy = json.loads(
        next(event.content for event in first if event.type == "execution_policy")
    )
    second_policy = json.loads(
        next(event.content for event in second if event.type == "execution_policy")
    )
    assert first_policy["strategy"] == "planner_direct"
    assert second_policy["strategy"] == "single_actor"
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_planner_direct_cannot_bypass_policy_with_apply_patch(
    tmp_path: Path,
) -> None:
    llm = ToolCallingLLM()
    patch_tool = RecordingPatchTool()
    planner = Planner(
        llm_client=llm,
        context_manager=ContextManager(system_prompt="system"),
        tools=[patch_tool],
        workspace_dir=str(tmp_path),
    )

    events = [event async for event in planner.run_stream("Explain app.py")]

    assert patch_tool.calls == 0
    assert any(
        event.type == "policy_denied" and event.tool_name == "apply_patch"
        for event in events
    )
    assert any(event.type == "done" for event in events)


@pytest.mark.asyncio
async def test_token_budget_crossing_discards_model_response(tmp_path: Path) -> None:
    llm = CountingLLM(tokens=11)
    context = RunContext.create()
    context.install_execution_policy(
        _policy(
            ExecutionStrategy.PLANNER_DIRECT,
            max_actors=0,
            roles=(),
            max_tokens=10,
        )
    )
    planner = Planner(
        llm_client=llm,
        context_manager=ContextManager(system_prompt="system"),
        tools=[],
        workspace_dir=str(tmp_path),
        run_context=context,
    )

    # Exercise the already-installed policy as a resumed execution; new tasks
    # intentionally cannot replace a Run policy.
    events = [event async for event in planner.run_stream("", resume=True)]

    assert any(event.type == "budget_exhausted" for event in events)
    assert not any(event.type == "done" for event in events)
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_actor_reservation_is_atomic_under_concurrency() -> None:
    ledger = RunBudgetLedger(
        _policy(
            ExecutionStrategy.SINGLE_ACTOR,
            max_actors=1,
            roles=("coder",),
        )
    )

    results = await asyncio.gather(
        ledger.reserve_actors(("coder",)),
        ledger.reserve_actors(("coder",)),
        return_exceptions=True,
    )

    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, BudgetExceeded) for result in results) == 1
    assert (await ledger.snapshot()).actors_started == 1


@pytest.mark.asyncio
async def test_checkpoint_round_trips_policy_and_consumption() -> None:
    context = RunContext.create(run_id="run_policy")
    policy = _policy(
        ExecutionStrategy.SINGLE_ACTOR,
        max_actors=1,
        roles=("coder",),
    )
    context.install_execution_policy(policy)
    assert context.budget_ledger is not None
    await context.budget_ledger.claim_model_call()
    await context.budget_ledger.reserve_actors(("coder",))
    await context.budget_ledger.record_actor_completed("coder")
    await context.record_usage(4, 3, False)

    checkpoint = await context.checkpoint([])
    restored = RunContext.from_checkpoint(
        record=RunRecord(run_id="run_policy", workspace_dir=".", model="test"),
        checkpoint=checkpoint,
    )

    assert restored.execution_policy == policy
    assert restored.budget_ledger is not None
    consumed = await restored.budget_ledger.snapshot()
    assert consumed.model_calls == 1
    assert consumed.total_tokens == 7
    assert consumed.actors_started == 1
    assert consumed.actor_roles == ("coder",)
    assert consumed.completed_actor_roles == ("coder",)


@pytest.mark.asyncio
async def test_sqlite_round_trips_policy_and_budget_snapshot(tmp_path: Path) -> None:
    store = SQLiteRunStore(tmp_path / "runs.db")
    await store.initialize()
    record = RunRecord(run_id="run_sql_policy", workspace_dir=".", model="test")
    context = RunContext.create(run_id=record.run_id, record=record, store=store)
    policy = _policy(
        ExecutionStrategy.SINGLE_ACTOR,
        max_actors=1,
        roles=("coder",),
    )
    context.install_execution_policy(policy)
    assert context.budget_ledger is not None
    await context.budget_ledger.claim_model_call()
    checkpoint = await context.checkpoint([])

    await store.create_run(record, checkpoint)
    stored = await store.load_run(record.run_id)

    assert stored is not None and stored.checkpoint is not None
    restored = RunContext.from_checkpoint(
        stored.record,
        stored.checkpoint,
        store=store,
    )
    assert restored.execution_policy == policy
    assert restored.budget_ledger is not None
    assert (await restored.budget_ledger.snapshot()).model_calls == 1


@pytest.mark.asyncio
async def test_planner_direct_policy_denies_delegate(tmp_path: Path) -> None:
    state_context = RunContext.create()
    state_context.install_execution_policy(
        _policy(
            ExecutionStrategy.PLANNER_DIRECT,
            max_actors=0,
            roles=(),
        )
    )
    task_id = await state_context.state.add_task("must not run")
    executor = RecordingExecutor()
    tool = DelegateTool(
        state=state_context.state,
        run_context=state_context,
        actor_executor=executor,
    )

    result = await tool.execute(subtasks=[{
        "task_id": task_id,
        "description": "write code",
        "role": "coder",
    }])

    assert result.success is False
    assert "does not permit" in (result.error or "")
    assert executor.specs == []


@pytest.mark.asyncio
async def test_scout_then_coder_requires_dependency_and_runs_in_order() -> None:
    context = RunContext.create()
    context.install_execution_policy(
        _policy(
            ExecutionStrategy.SCOUT_THEN_CODER,
            max_actors=2,
            roles=("scout", "coder"),
        )
    )
    scout_id = await context.state.add_task("inspect")
    coder_id = await context.state.add_task("implement", dependencies=[scout_id])
    executor = RecordingExecutor()
    tool = DelegateTool(
        state=context.state,
        run_context=context,
        actor_executor=executor,
    )

    result = await tool.execute(subtasks=[
        {"task_id": scout_id, "description": "inspect", "role": "scout"},
        {"task_id": coder_id, "description": "implement", "role": "coder"},
    ])

    assert result.success is True
    assert [spec.role for spec in executor.specs] == ["scout", "coder"]
    assert context.budget_ledger is not None
    consumed = await context.budget_ledger.snapshot()
    assert consumed.completed_actor_roles == ("scout", "coder")


@pytest.mark.asyncio
async def test_scout_then_dag_denies_coder_only_first_wave() -> None:
    context = RunContext.create()
    context.install_execution_policy(
        _policy(
            ExecutionStrategy.SCOUT_THEN_DAG,
            max_actors=4,
            roles=("scout", "coder", "verifier"),
        )
    )
    coder_id = await context.state.add_task("implement")
    executor = RecordingExecutor()
    tool = DelegateTool(
        state=context.state,
        run_context=context,
        actor_executor=executor,
    )

    result = await tool.execute(subtasks=[{
        "task_id": coder_id,
        "description": "implement",
        "role": "coder",
    }])

    assert result.success is False
    assert "requires a Scout" in (result.error or "")
    assert executor.specs == []


@pytest.mark.asyncio
async def test_gated_policy_fails_before_actor_when_gates_are_missing(
    tmp_path: Path,
) -> None:
    context = RunContext.create()
    context.install_execution_policy(
        _policy(
            ExecutionStrategy.CODER_WITH_GATES,
            max_actors=1,
            roles=("coder",),
        )
    )
    worktree = tmp_path / "worktree"

    def setup(workspace: str, task_id: str) -> str:
        worktree.mkdir()
        return str(worktree)

    executor = WorktreeActorExecutor(
        llm_client=CountingLLM(),
        workspace_dir=str(tmp_path),
        worktree_factory=setup,
        worktree_cleanup=lambda path: None,
        tool_provider_factory=lambda run_context, actor_id: MinimalProvider(),
        actor_factory=lambda **kwargs: pytest.fail("Actor must not start"),
        verification_config_loader=lambda workspace: VerificationConfig(),
    )

    result = await executor.execute(
        ActorTaskSpec(task_id="task_gated", description="implement"),
        context,
    )

    assert result.status == "failed"
    assert "requires a non-empty" in result.error


@pytest.mark.asyncio
async def test_apply_patch_requires_matching_verified_coder_provenance() -> None:
    context = RunContext.create()
    context.install_execution_policy(
        _policy(
            ExecutionStrategy.CODER_WITH_GATES,
            max_actors=1,
            roles=("coder",),
        )
    )
    task_id = await context.state.add_task("implement")
    await context.state.update_task(
        task_id,
        status="done",
        assigned_actor=task_id,
        actor_role="coder",
    )
    await context.state.add_summary(
        task_id,
        "implemented",
        diff="",
        verification_passed=False,
    )
    tool = ApplyPatchTool(context.state, context)

    unverified = await tool.execute(diff="", task_id=task_id)
    forged = await tool.execute(diff="forged diff", task_id=task_id)
    await context.state.add_summary(
        task_id,
        "implemented",
        diff="",
        verification_passed=True,
    )
    verified = await tool.execute(task_id=task_id)

    assert unverified.success is False
    assert unverified.policy_denied is True
    assert "quality gates" in (unverified.error or "")
    assert forged.success is False
    assert "does not match" in (forged.error or "")
    assert verified.success is True


@pytest.mark.asyncio
async def test_actor_requested_steps_are_capped_by_policy(tmp_path: Path) -> None:
    context = RunContext.create()
    context.install_execution_policy(
        _policy(
            ExecutionStrategy.SINGLE_ACTOR,
            max_actors=1,
            roles=("coder",),
        )
    )
    worktree = tmp_path / "worktree"
    captured: dict[str, int] = {}

    def actor_factory(**kwargs):
        captured["max_steps"] = kwargs["max_steps"]

        class Actor:
            async def run(self, prompt: str):
                return SimpleNamespace(
                    status="done",
                    files_modified=[],
                    bugs_found=[],
                    key_findings="done",
                    suggested_next_steps="",
                )

        return Actor()

    def setup(workspace: str, task_id: str) -> str:
        worktree.mkdir()
        return str(worktree)

    async def empty_diff(path: str) -> str:
        return ""

    executor = WorktreeActorExecutor(
        llm_client=CountingLLM(),
        workspace_dir=str(tmp_path),
        worktree_factory=setup,
        worktree_cleanup=lambda path: None,
        diff_extractor=empty_diff,
        tool_provider_factory=lambda run_context, actor_id: MinimalProvider(),
        actor_factory=actor_factory,
        verification_config_loader=lambda workspace: VerificationConfig(),
    )

    result = await executor.execute(
        ActorTaskSpec(
            task_id="task_steps",
            description="implement",
            max_steps=999,
        ),
        context,
    )

    assert result.status == "done"
    assert captured["max_steps"] == 10


def test_budget_snapshot_rejects_negative_consumption() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        BudgetSnapshot(model_calls=-1)


@pytest.mark.asyncio
async def test_unapproved_ledger_fails_closed() -> None:
    ledger = RunBudgetLedger(
        _policy(
            ExecutionStrategy.SCOUT_THEN_DAG,
            max_actors=4,
            roles=("scout", "coder", "verifier"),
            requires_approval=True,
        )
    )

    with pytest.raises(PolicyViolation, match="explicit human approval"):
        await ledger.claim_model_call()
