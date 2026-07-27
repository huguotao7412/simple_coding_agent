from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from core.execution.assessment import TaskAssessor
from core.execution.models import ExecutionStrategy, TaskRisk
from core.execution.policy import ExecutionPolicy
from core.orchestration.langgraph import LangGraphOrchestrator
from core.orchestration.factory import create_application_service
from core.orchestration.plan import (
    PlannedActor,
    TaskPlan,
    validate_task_plan,
)
from core.orchestration.protocol import OrchestrationRequest
from core.orchestration.security import validate_artifact_uri
from core.orchestration.state import (
    GRAPH_STATE_SCHEMA_VERSION,
    GraphState,
    MAX_GRAPH_STATE_BYTES,
    migrate_graph_state,
    validate_graph_state,
)
from core.planner import Planner
from core.runs.context import RunContext
from core.runs.models import RunRecord
from core.runs.sqlite_store import SQLiteRunStore
from core.runtime.conversation import ContextManager
from core.tools.base import BaseTool, ToolResult
from langgraph.checkpoint.memory import InMemorySaver


class CountingLLM:
    max_tokens = 128_000

    def __init__(self) -> None:
        self.calls = 0

    def count_messages_tokens(self, messages):
        return 1

    async def chat(self, messages, tools=None, on_token=None):
        self.calls += 1
        return {
            "role": "assistant",
            "content": "safe final answer",
            "_usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "estimated": False,
            },
        }


class ApprovalAssessor(TaskAssessor):
    def assess(self, prompt: str):
        assessment = super().assess("Explain the architecture")
        return replace(
            assessment,
            risk=TaskRisk.HIGH,
            reasons=("explicit test approval boundary",),
            requires_human_approval=True,
        )


class ActorAssessor(TaskAssessor):
    def assess(self, prompt: str):
        return replace(
            super().assess("Fix app.py"),
            strategy=ExecutionStrategy.SINGLE_ACTOR,
            max_actors=1,
        )


class RecordingDelegate(BaseTool):
    name = "delegate"
    description = "test structured Actor adapter"
    parameters = {}
    required_params = []

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self._run_context = None

    async def execute(self, **kwargs):
        subtask = kwargs["subtasks"][0]
        actor_id = str(subtask["task_id"])
        self.calls.append(actor_id)
        state = self._run_context.state
        await state.update_task(
            actor_id,
            status="running",
            actor_role=str(subtask["role"]),
            assigned_actor=actor_id,
        )
        await state.add_summary(actor_id, "structured Actor completed")
        await state.update_task(actor_id, status="done")
        return ToolResult.ok("done")


def _planner(
    tmp_path: Path,
    llm: CountingLLM,
    *,
    assessor: TaskAssessor | None = None,
) -> Planner:
    return Planner(
        llm_client=llm,
        context_manager=ContextManager(system_prompt="test"),
        tools=[],
        workspace_dir=str(tmp_path),
        run_context=RunContext.create("run_langgraph_test"),
        task_assessor=assessor,
    )


def test_langgraph_is_the_only_application_service(
    tmp_path: Path,
) -> None:
    planner = _planner(tmp_path, CountingLLM())

    assert isinstance(create_application_service(planner), LangGraphOrchestrator)


def test_validated_plan_rejects_cycles_and_unapproved_roles(
    tmp_path: Path,
) -> None:
    assessment = TaskAssessor(tmp_path).assess("Fix app.py")
    policy = replace(
        ExecutionPolicy.from_assessment(assessment),
        max_actors=2,
    )
    cyclic = TaskPlan(
        actors=(
            PlannedActor("one", "coder", "one", ("two",)),
            PlannedActor("two", "coder", "two", ("one",)),
        ),
        direct_planner=False,
        repair_limit=0,
    )
    with pytest.raises(ValueError, match="cycle"):
        validate_task_plan(cyclic, policy, workspace_dir=tmp_path)


async def _events(orchestrator, request):
    return [event async for event in orchestrator.run_stream(request)]


@pytest.mark.asyncio
async def test_low_risk_graph_routes_through_finalize(tmp_path: Path) -> None:
    llm = CountingLLM()
    orchestrator = LangGraphOrchestrator.in_memory(_planner(tmp_path, llm))

    events = await _events(
        orchestrator,
        OrchestrationRequest(user_request="Explain the architecture"),
    )

    assert llm.calls == 1
    assert any(event.type == "task_assessment" for event in events)
    assert any(
        event.node_name == "finalize_success"
        for event in events
    )
    assert events[-1].type == "done"


@pytest.mark.asyncio
async def test_structured_actor_lifecycle_does_not_wrap_planner_loop(
    tmp_path: Path,
) -> None:
    llm = CountingLLM()
    delegate = RecordingDelegate()
    planner = Planner(
        llm_client=llm,
        context_manager=ContextManager(system_prompt="test"),
        tools=[delegate],
        workspace_dir=str(tmp_path),
        run_context=RunContext.create("run_structured_actor"),
        task_assessor=ActorAssessor(tmp_path),
    )

    events = await _events(
        LangGraphOrchestrator.in_memory(planner),
        OrchestrationRequest(user_request="Fix app.py"),
    )

    assert delegate.calls == ["actor_1"]
    assert llm.calls == 0
    assert events[-1].type == "done"
    assert "structured Actor completed" in events[-1].content
    node_names = {event.node_name for event in events}
    assert {
        "plan",
        "validate_plan",
        "schedule_ready_actors",
        "execute_actor",
        "collect_actor_results",
        "verify",
        "finalize_success",
    } <= node_names


@pytest.mark.asyncio
async def test_high_risk_interrupts_before_model_and_resumes_same_thread(
    tmp_path: Path,
) -> None:
    llm = CountingLLM()
    planner = _planner(tmp_path, llm, assessor=ApprovalAssessor(tmp_path))
    orchestrator = LangGraphOrchestrator.in_memory(planner)

    paused = await _events(
        orchestrator,
        OrchestrationRequest(user_request="reviewed high risk request"),
    )

    assert llm.calls == 0
    assert any(event.type == "graph_interrupted" for event in paused)
    assert planner.run_context.execution_policy is not None
    assert not planner.run_context.execution_policy.human_approved

    resumed = await _events(
        orchestrator,
        OrchestrationRequest(
            user_request="",
            resume=True,
            approval=True,
        ),
    )

    assert llm.calls == 1
    assert planner.run_context.execution_policy is not None
    assert planner.run_context.execution_policy.human_approved
    assert any(event.type == "graph_resumed" for event in resumed)
    assert resumed[-1].type == "done"


@pytest.mark.asyncio
async def test_rejected_approval_never_calls_model(tmp_path: Path) -> None:
    llm = CountingLLM()
    planner = _planner(tmp_path, llm, assessor=ApprovalAssessor(tmp_path))
    orchestrator = LangGraphOrchestrator.in_memory(planner)
    await _events(
        orchestrator,
        OrchestrationRequest(user_request="reviewed high risk request"),
    )

    events = await _events(
        orchestrator,
        OrchestrationRequest(user_request="", resume=True, approval=False),
    )

    assert llm.calls == 0
    assert events[-1].type == "error"
    assert "rejected" in events[-1].content.lower()


def test_graph_state_is_bounded_json_and_has_stable_thread_id() -> None:
    state = GraphState(
        schema_version=GRAPH_STATE_SCHEMA_VERSION,
        run_id="run_1",
        thread_id="run_1",
        user_request="small",
    )
    validate_graph_state(state)

    state["thread_id"] = "other"
    with pytest.raises(ValueError, match="mismatch"):
        validate_graph_state(state)

    oversized = GraphState(
        schema_version=GRAPH_STATE_SCHEMA_VERSION,
        run_id="run_2",
        thread_id="run_2",
        user_request="x" * (MAX_GRAPH_STATE_BYTES + 1),
    )
    with pytest.raises(ValueError, match="bounded serialization"):
        validate_graph_state(oversized)


def test_state_migration_is_explicit_and_plan_cannot_expand_policy(
    tmp_path: Path,
) -> None:
    migrated = migrate_graph_state({
        "schema_version": 1,
        "run_id": "run_old",
        "thread_id": "run_old",
        "plan_summary": {"task_count": 0},
    })
    validate_graph_state(migrated)
    assert migrated["schema_version"] == GRAPH_STATE_SCHEMA_VERSION

    assessment = TaskAssessor(tmp_path).assess("Fix app.py")
    policy = ExecutionPolicy.from_assessment(assessment)
    malicious = TaskPlan(
        actors=(PlannedActor("root", "admin", "run arbitrary command"),),
        direct_planner=False,
        repair_limit=0,
    )
    with pytest.raises(ValueError, match="role is not allowed"):
        validate_task_plan(malicious, policy, workspace_dir=tmp_path)


def test_artifact_reference_cannot_escape_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv("SCA_STATE_HOME", str(state_root))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "safe.patch"
    artifact.write_text("diff", encoding="utf-8")

    assert validate_artifact_uri(
        str(artifact),
        workspace_dir=workspace,
    ) == artifact.resolve()
    with pytest.raises(ValueError, match="digest mismatch"):
        validate_artifact_uri(
            str(artifact),
            workspace_dir=workspace,
            expected_digest="0" * 64,
        )
    with pytest.raises(ValueError, match="escapes"):
        validate_artifact_uri(
            str(tmp_path / "outside.patch"),
            workspace_dir=workspace,
            require_exists=False,
        )
    with pytest.raises(ValueError, match="missing"):
        validate_artifact_uri(
            "missing.patch",
            workspace_dir=workspace,
        )


@pytest.mark.asyncio
async def test_sqlite_checkpoint_resumes_approval_across_instances(
    tmp_path: Path,
) -> None:
    store = SQLiteRunStore(tmp_path / "runs.db")
    await store.initialize()
    record = RunRecord(
        run_id="run_durable_graph",
        workspace_dir=str(tmp_path),
        model="fake",
    )
    first_context = RunContext.create(record=record, store=store)
    await store.create_run(
        record,
        await first_context.checkpoint([{"role": "system", "content": "test"}]),
    )
    first_llm = CountingLLM()
    first_planner = Planner(
        llm_client=first_llm,
        context_manager=ContextManager(system_prompt="test"),
        tools=[],
        workspace_dir=str(tmp_path),
        run_context=first_context,
        task_assessor=ApprovalAssessor(tmp_path),
    )
    checkpoint_path = tmp_path / "graph.db"

    paused = await _events(
        LangGraphOrchestrator(first_planner, checkpoint_path=checkpoint_path),
        OrchestrationRequest(user_request="reviewed high risk request"),
    )
    assert any(event.type == "graph_interrupted" for event in paused)
    assert first_llm.calls == 0

    stored = await store.load_run(record.run_id)
    assert stored is not None and stored.checkpoint is not None
    restored_context = RunContext.from_checkpoint(
        stored.record,
        stored.checkpoint,
        store=store,
    )
    restored_messages = ContextManager(system_prompt="test")
    restored_messages.restore_messages(list(stored.checkpoint.messages))
    resumed_llm = CountingLLM()
    resumed_planner = Planner(
        llm_client=resumed_llm,
        context_manager=restored_messages,
        tools=[],
        workspace_dir=str(tmp_path),
        run_context=restored_context,
    )

    resumed = await _events(
        LangGraphOrchestrator(
            resumed_planner,
            checkpoint_path=checkpoint_path,
        ),
        OrchestrationRequest(user_request="", resume=True, approval=True),
    )

    assert resumed_llm.calls == 1
    assert resumed[-1].type == "done"
    final = await store.load_run(record.run_id)
    assert final is not None
    assert final.record.status.value == "completed"
    assert final.checkpoint is not None
    assert final.checkpoint.execution_policy is not None
    assert final.checkpoint.execution_policy["human_approved"] is True
    audit = await store.list_events(record.run_id)
    assert any(event.event_type == "graph_interrupted" for event in audit)
    assert any(event.event_type == "graph_resumed" for event in audit)


class FailingCheckpointer(InMemorySaver):
    async def aput(self, *args, **kwargs):
        raise OSError("injected checkpoint write failure")


@pytest.mark.asyncio
async def test_checkpointer_failure_is_explicit_and_never_reports_success(
    tmp_path: Path,
) -> None:
    llm = CountingLLM()
    planner = _planner(tmp_path, llm)
    orchestrator = LangGraphOrchestrator(
        planner,
        checkpointer=FailingCheckpointer(),
    )

    events = await _events(
        orchestrator,
        OrchestrationRequest(user_request="Explain the architecture"),
    )

    assert events[-1].type == "error"
    assert "checkpoint write failure" in events[-1].content
    assert not any(event.type == "done" for event in events)
