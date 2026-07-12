from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.actor_execution import ActorTaskSpec
from core.run_context import RunContext
from core.worktree_actor_executor import WorktreeActorExecutor


class FakeLLM:
    max_tokens = 128000


class FakeProvider:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def start(self, workspace_dir: str, tool_policy: Any) -> None:
        self.events.append(f"provider:start:{Path(workspace_dir).name}")

    async def shutdown(self) -> None:
        self.events.append("provider:shutdown")


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

    def write_artifact(workspace_dir: str, task_id: str, diff: str) -> str:
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
