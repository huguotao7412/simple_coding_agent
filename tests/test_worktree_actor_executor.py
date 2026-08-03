from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.actors.contracts import ActorTaskSpec
from core.runs.context import RunContext
from core.actors.worktree import WorktreeActorExecutor
from core.verification.models import (
    GateResult,
    GateSpec,
    VerificationConfig,
    VerificationReport,
)


class FakeLLM:
    max_tokens = 128000


class FakeProvider:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def start(self, workspace_dir: str, tool_policy: Any) -> None:
        self.events.append(f"provider:start:{Path(workspace_dir).name}")

    async def shutdown(self) -> None:
        self.events.append("provider:shutdown")


class FailingProvider(FakeProvider):
    async def start(self, workspace_dir: str, tool_policy: Any) -> None:
        self.events.append("provider:start:failed")
        raise RuntimeError("MCP initialize: provider bootstrap failed")


class FakeActor:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def run(self, prompt: str) -> Any:
        self.events.append("actor:run")
        return SimpleNamespace(
            status="done",
            files_modified=[],
            bugs_found=["latent bug"],
            key_findings="implemented safely",
            suggested_next_steps="none",
        )


class FakeVerificationRunner:
    def __init__(self, events: list[str], report: VerificationReport) -> None:
        self.events = events
        self.report = report

    async def run(
        self,
        config: VerificationConfig,
        *,
        worktree: str | Path,
        task_id: str,
        attempt: int,
    ) -> VerificationReport:
        self.events.append(f"verify:{task_id}:{attempt}:{Path(worktree).name}")
        return self.report


class SequenceVerificationRunner:
    def __init__(
        self,
        events: list[str],
        reports: list[VerificationReport],
    ) -> None:
        self.events = events
        self.reports = reports

    async def run(
        self,
        config: VerificationConfig,
        *,
        worktree: str | Path,
        task_id: str,
        attempt: int,
    ) -> VerificationReport:
        self.events.append(f"verify:{attempt}")
        report = self.reports.pop(0)
        return VerificationReport(attempt=attempt, results=report.results)


class RepairActor(FakeActor):
    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.prompts: list[str] = []

    async def run(self, prompt: str) -> Any:
        self.prompts.append(prompt)
        self.events.append(f"actor:run:{len(self.prompts)}")
        return SimpleNamespace(
            status="done",
            files_modified=[],
            bugs_found=[],
            key_findings=f"turn {len(self.prompts)}",
            suggested_next_steps="none",
        )


@pytest.mark.asyncio
async def test_provider_bootstrap_failure_skips_actor_diff_and_verification(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    worktree = tmp_path / "actor-worktree"
    worktree.mkdir()

    async def forbidden_diff(path: str) -> str:
        pytest.fail("diff extraction must not run after provider bootstrap failure")

    executor = WorktreeActorExecutor(
        llm_client=FakeLLM(),
        workspace_dir=str(tmp_path),
        worktree_factory=lambda workspace, task: str(worktree),
        worktree_cleanup=lambda path: events.append("worktree:cleanup"),
        diff_extractor=forbidden_diff,
        tool_provider_factory=lambda context, actor_id: FailingProvider(events),
        actor_factory=lambda **kwargs: pytest.fail("Actor must not start"),
        verification_config_loader=lambda workspace: pytest.fail(
            "quality gates must not load after bootstrap failure"
        ),
    )

    result = await executor.execute(
        ActorTaskSpec(task_id="task_bootstrap", description="Create file", role="scout"),
        RunContext.create(run_id="run_bootstrap"),
    )

    assert result.status == "failed"
    assert result.failure_category == "tool provider failure"
    assert result.diff == ""
    assert result.verification_reports == ()
    assert events == ["provider:start:failed", "provider:shutdown", "worktree:cleanup"]


@pytest.mark.asyncio
async def test_worktree_executor_owns_actor_lifecycle(tmp_path: Path) -> None:
    events: list[str] = []
    worktree = tmp_path / "actor-worktree"

    def setup(workspace_dir: str, task_id: str) -> str:
        events.append(f"worktree:setup:{task_id}")
        worktree.mkdir()
        return str(worktree)

    async def extract(worktree_path: str) -> str:
        events.append("diff:extract")
        return "diff --git a/module.py b/module.py\n"

    def write_artifact(
        workspace_dir: str,
        task_id: str,
        diff: str,
        run_id: str,
    ) -> str:
        events.append("artifact:write")
        return ".sca/artifacts/actor-diffs/task_1.patch"

    executor = WorktreeActorExecutor(
        llm_client=FakeLLM(),
        workspace_dir=str(tmp_path),
        worktree_factory=setup,
        worktree_cleanup=lambda path: events.append("worktree:cleanup"),
        diff_extractor=extract,
        artifact_writer=write_artifact,
        tool_provider_factory=lambda context, actor_id: FakeProvider(events),
        actor_factory=lambda **kwargs: FakeActor(events),
    )

    result = await executor.execute(
        ActorTaskSpec(task_id="task_1", description="Change module.py"),
        RunContext.create(run_id="run_test"),
    )

    assert result.status == "done"
    assert result.files_modified == ("module.py",)
    assert result.bugs_found == ("latent bug",)
    assert result.key_findings == "implemented safely"
    assert result.diff_artifact.endswith("task_1.patch")
    assert events == [
        "worktree:setup:task_1",
        "provider:start:actor-worktree",
        "actor:run",
        "diff:extract",
        "artifact:write",
        "provider:shutdown",
        "worktree:cleanup",
    ]


@pytest.mark.asyncio
async def test_coder_diff_is_exported_only_after_required_gates_pass(tmp_path: Path) -> None:
    events: list[str] = []
    worktree = tmp_path / "actor-worktree"
    passing_report = VerificationReport(
        attempt=1,
        results=(
            GateResult(
                gate_name="unit",
                command=("pytest",),
                required=True,
                passed=True,
                exit_code=0,
                duration_ms=10,
                output_artifact=str(tmp_path / "unit.log"),
                output_excerpt="passed",
            ),
        ),
    )

    def setup(workspace_dir: str, task_id: str) -> str:
        worktree.mkdir()
        return str(worktree)

    async def extract(worktree_path: str) -> str:
        events.append("diff:extract")
        return "diff --git a/module.py b/module.py\n"

    executor = WorktreeActorExecutor(
        llm_client=FakeLLM(),
        workspace_dir=str(tmp_path),
        worktree_factory=setup,
        worktree_cleanup=lambda path: None,
        diff_extractor=extract,
        artifact_writer=lambda workspace, task, diff, run_id: "task.patch",
        tool_provider_factory=lambda context, actor_id: FakeProvider(events),
        actor_factory=lambda **kwargs: FakeActor(events),
        verification_config_loader=lambda workspace: VerificationConfig(
            gates=(GateSpec("unit", ("pytest",)),)
        ),
        verification_runner=FakeVerificationRunner(events, passing_report),
    )

    result = await executor.execute(
        ActorTaskSpec(task_id="task_verified", description="Change module.py"),
        RunContext.create(run_id="run_test"),
    )

    assert result.status == "done"
    assert result.verification_reports == (passing_report,)
    assert result.diff_artifact == "task.patch"
    assert events.index("verify:task_verified:1:actor-worktree") < events.index(
        "diff:extract"
    )


@pytest.mark.asyncio
async def test_failed_required_gate_blocks_diff_export(tmp_path: Path) -> None:
    events: list[str] = []
    worktree = tmp_path / "actor-worktree"
    failing_report = VerificationReport(
        attempt=1,
        results=(
            GateResult(
                gate_name="unit",
                command=("pytest",),
                required=True,
                passed=False,
                exit_code=1,
                duration_ms=10,
                output_artifact=str(tmp_path / "unit.log"),
                output_excerpt="one failed",
            ),
        ),
    )

    def setup(workspace_dir: str, task_id: str) -> str:
        worktree.mkdir()
        return str(worktree)

    async def forbidden_extract(worktree_path: str) -> str:
        raise AssertionError("unverified diff must not be exported")

    executor = WorktreeActorExecutor(
        llm_client=FakeLLM(),
        workspace_dir=str(tmp_path),
        worktree_factory=setup,
        worktree_cleanup=lambda path: events.append("worktree:cleanup"),
        diff_extractor=forbidden_extract,
        tool_provider_factory=lambda context, actor_id: FakeProvider(events),
        actor_factory=lambda **kwargs: FakeActor(events),
        verification_config_loader=lambda workspace: VerificationConfig(
            gates=(GateSpec("unit", ("pytest",)),), max_repair_attempts=0
        ),
        verification_runner=FakeVerificationRunner(events, failing_report),
    )

    result = await executor.execute(
        ActorTaskSpec(task_id="task_failed", description="Change module.py"),
        RunContext.create(run_id="run_test"),
    )

    assert result.status == "failed"
    assert result.verification_reports == (failing_report,)
    assert result.diff == ""
    assert result.diff_artifact == ""
    assert "unit" in result.error
    assert "unit.log" in result.error
    assert events[-2:] == ["provider:shutdown", "worktree:cleanup"]


@pytest.mark.asyncio
async def test_non_coder_role_skips_project_quality_gates(tmp_path: Path) -> None:
    events: list[str] = []
    worktree = tmp_path / "actor-worktree"

    def setup(workspace_dir: str, task_id: str) -> str:
        worktree.mkdir()
        return str(worktree)

    def forbidden_loader(workspace: str) -> VerificationConfig:
        raise AssertionError("non-coder roles must not load implementation gates")

    executor = WorktreeActorExecutor(
        llm_client=FakeLLM(),
        workspace_dir=str(tmp_path),
        worktree_factory=setup,
        worktree_cleanup=lambda path: None,
        diff_extractor=lambda path: _empty_diff(),
        tool_provider_factory=lambda context, actor_id: FakeProvider(events),
        actor_factory=lambda **kwargs: FakeActor(events),
        verification_config_loader=forbidden_loader,
    )

    result = await executor.execute(
        ActorTaskSpec(task_id="task_scout", description="Inspect", role="scout"),
        RunContext.create(run_id="run_test"),
    )

    assert result.status == "done"
    assert result.verification_reports == ()


@pytest.mark.asyncio
async def test_failed_gate_is_repaired_and_reverified_before_diff_export(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    worktree = tmp_path / "actor-worktree"
    worktree.mkdir()
    actor = RepairActor(events)
    failed = _gate_report(tmp_path, passed=False, output="one failed")
    passed = _gate_report(tmp_path, passed=True, output="all passed")

    async def extract(path: str) -> str:
        events.append("diff:extract")
        return "diff --git a/module.py b/module.py\n"

    executor = WorktreeActorExecutor(
        llm_client=FakeLLM(),
        workspace_dir=str(tmp_path),
        worktree_factory=lambda workspace, task: str(worktree),
        worktree_cleanup=lambda path: None,
        diff_extractor=extract,
        artifact_writer=lambda workspace, task, diff, run_id: "task.patch",
        tool_provider_factory=lambda context, actor_id: FakeProvider(events),
        actor_factory=lambda **kwargs: actor,
        verification_config_loader=lambda workspace: VerificationConfig(
            gates=(GateSpec("unit", ("pytest",)),), max_repair_attempts=2
        ),
        verification_runner=SequenceVerificationRunner(events, [failed, passed]),
    )

    result = await executor.execute(
        ActorTaskSpec(task_id="task_repair", description="Change module.py"),
        RunContext.create(run_id="run_test"),
    )

    assert result.status == "done"
    assert [report.passed for report in result.verification_reports] == [False, True]
    assert len(actor.prompts) == 2
    assert "Deterministic project verification failed" in actor.prompts[1]
    assert events.index("verify:2") < events.index("diff:extract")
    assert result.key_findings == "turn 2"


@pytest.mark.asyncio
async def test_repeated_failure_stops_repair_early_and_blocks_diff(tmp_path: Path) -> None:
    events: list[str] = []
    worktree = tmp_path / "actor-worktree"
    worktree.mkdir()
    actor = RepairActor(events)
    failed = _gate_report(tmp_path, passed=False, output="same failure")

    async def forbidden_extract(path: str) -> str:
        raise AssertionError("a repeated failure must block diff export")

    executor = WorktreeActorExecutor(
        llm_client=FakeLLM(),
        workspace_dir=str(tmp_path),
        worktree_factory=lambda workspace, task: str(worktree),
        worktree_cleanup=lambda path: None,
        diff_extractor=forbidden_extract,
        tool_provider_factory=lambda context, actor_id: FakeProvider(events),
        actor_factory=lambda **kwargs: actor,
        verification_config_loader=lambda workspace: VerificationConfig(
            gates=(GateSpec("unit", ("pytest",)),), max_repair_attempts=3
        ),
        verification_runner=SequenceVerificationRunner(events, [failed, failed]),
    )

    result = await executor.execute(
        ActorTaskSpec(task_id="task_stuck", description="Change module.py"),
        RunContext.create(run_id="run_test"),
    )

    assert result.status == "failed"
    assert "no progress" in result.error
    assert len(result.verification_reports) == 2
    assert len(actor.prompts) == 2
    assert result.diff == ""


def _gate_report(tmp_path: Path, *, passed: bool, output: str) -> VerificationReport:
    return VerificationReport(
        attempt=1,
        results=(
            GateResult(
                gate_name="unit",
                command=("pytest",),
                required=True,
                passed=passed,
                exit_code=0 if passed else 1,
                duration_ms=10,
                output_artifact=str(tmp_path / "unit.log"),
                output_excerpt=output,
            ),
        ),
    )


async def _empty_diff() -> str:
    return ""


def test_injected_context_refuses_files_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("outside secret", encoding="utf-8")
    executor = WorktreeActorExecutor(
        llm_client=FakeLLM(),
        workspace_dir=str(workspace),
    )

    context = executor._build_injected_context(
        ActorTaskSpec(
            task_id="task_escape",
            description="Read context",
            context_files=("../secret.txt",),
        )
    )

    assert "outside secret" not in context
    assert "(unable to read)" in context


def test_injected_context_renders_versioned_dependency_handoff(tmp_path: Path) -> None:
    from core.a2a_lite.models import AgentHandoff, AgentMessage, ArtifactRef

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    message = AgentMessage.handoff_message(
        run_id="run_1",
        task_id="parent",
        sender_id="actor:parent",
        recipient_id="planner",
        handoff=AgentHandoff(
            findings=("Change parser.py",),
            artifacts=(ArtifactRef.create(
                kind="patch",
                uri=".sca/artifacts/parent.patch",
                media_type="text/x-diff",
                producer_task_id="parent",
                content="diff",
            ),),
        ),
    )
    executor = WorktreeActorExecutor(
        llm_client=FakeLLM(),
        workspace_dir=str(workspace),
    )

    context = executor._build_injected_context(ActorTaskSpec(
        task_id="child",
        description="Continue parent work",
        dependency_handoffs=(message,),
    ))

    assert "A2A_lite Structured Dependency Handoffs" in context
    assert '"schema_version": "a2a-lite/1.0"' in context
    assert '"uri": ".sca/artifacts/parent.patch"' in context
    assert "Change parser.py" in context
