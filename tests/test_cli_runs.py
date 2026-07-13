from __future__ import annotations

from pathlib import Path

import pytest

from cli.main import main, resume_once
from cli.runs import (
    RunLookupError,
    create_durable_run_context,
    load_resumable_run,
    render_run_detail,
    render_run_list,
    run_database_path,
)
from core.context import ContextManager
from core.run_state import RunRecord, RunStatus
from core.sqlite_run_store import SQLiteRunStore


@pytest.mark.asyncio
async def test_create_durable_context_persists_initial_checkpoint(tmp_path: Path):
    messages = ContextManager(system_prompt="system")

    context = await create_durable_run_context(
        workspace_dir=str(tmp_path),
        model="test-model",
        messages=messages.messages,
        run_id="run_created",
        now=100.0,
    )

    assert context.record is not None
    assert context.record.status is RunStatus.CREATED
    assert context.store is not None
    stored = await context.store.load_run("run_created")
    assert stored is not None
    assert stored.checkpoint is not None
    assert stored.checkpoint.messages == ({"role": "system", "content": "system"},)
    assert run_database_path(str(tmp_path)) == tmp_path / ".sca" / "runs.db"


@pytest.mark.asyncio
async def test_load_resumable_run_validates_status_workspace_and_checkpoint(
    tmp_path: Path,
):
    store = SQLiteRunStore(tmp_path / "runs.db")
    await store.initialize()
    workspace = str(tmp_path / "workspace")
    record = RunRecord(
        run_id="run_paused",
        workspace_dir=workspace,
        model="test-model",
        status=RunStatus.PAUSED,
        created_at=100.0,
        updated_at=101.0,
    )
    context = ContextManager(system_prompt="system")
    temporary = await create_durable_run_context(
        workspace_dir=str(tmp_path / "other"),
        model="test-model",
        messages=context.messages,
        run_id="run_temporary",
    )
    checkpoint = await temporary.checkpoint(context.messages)
    checkpoint = type(checkpoint)(
        run_id=record.run_id,
        messages=checkpoint.messages,
        task_snapshot=checkpoint.task_snapshot,
        saved_at=checkpoint.saved_at,
    )
    await store.create_run(record, checkpoint)

    loaded = await load_resumable_run(store, "run_paused", workspace)
    assert loaded.record == record

    with pytest.raises(RunLookupError, match="workspace"):
        await load_resumable_run(store, "run_paused", str(tmp_path / "wrong"))
    with pytest.raises(RunLookupError, match="not found"):
        await load_resumable_run(store, "missing", workspace)

    completed = RunRecord(
        run_id="run_complete",
        workspace_dir=workspace,
        model="test-model",
        status=RunStatus.COMPLETED,
        created_at=100.0,
        updated_at=102.0,
    )
    await store.create_run(completed, type(checkpoint)(
        run_id=completed.run_id,
        messages=checkpoint.messages,
        task_snapshot=checkpoint.task_snapshot,
        saved_at=checkpoint.saved_at,
    ))
    with pytest.raises(RunLookupError, match="completed"):
        await load_resumable_run(store, "run_complete", workspace)


def test_run_renderers_are_deterministic():
    records = [
        RunRecord(
            run_id="run_2",
            workspace_dir="C:/workspace",
            model="model-b",
            status=RunStatus.PAUSED,
            version=4,
            created_at=100.0,
            updated_at=200.0,
        ),
        RunRecord(
            run_id="run_1",
            workspace_dir="C:/workspace",
            model="model-a",
            status=RunStatus.COMPLETED,
            version=2,
            created_at=90.0,
            updated_at=150.0,
        ),
    ]

    listing = render_run_list(records)
    detail = render_run_detail(records[0], event_count=7, message_count=5)

    assert listing.splitlines()[1].startswith("run_2\tpaused")
    assert "run_1\tcompleted" in listing
    assert "Run: run_2" in detail
    assert "Version: 4" in detail
    assert "Events: 7" in detail
    assert "Messages: 5" in detail


def test_runs_command_does_not_require_api_key(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv("SCA_API_KEY", raising=False)

    exit_code = main(["--dir", str(tmp_path), "runs"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "No durable runs found." in captured.out
    assert "SCA_API_KEY" not in captured.err


def test_inspect_unknown_run_returns_nonzero_without_api_key(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    monkeypatch.delenv("SCA_API_KEY", raising=False)

    exit_code = main(["--dir", str(tmp_path), "inspect", "run_missing"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "run_missing" in captured.err
    assert "not found" in captured.err


@pytest.mark.asyncio
async def test_resume_once_reconstructs_planner_and_completes_run(
    tmp_path: Path,
    monkeypatch,
):
    class ResumeLLM:
        max_tokens = 128000
        observed_messages = []

        def __init__(self, **kwargs):
            self.model = kwargs["model"]

        def count_messages_tokens(self, messages):
            return 1

        def count_tokens(self, text):
            return 1

        async def chat(self, messages, tools=None, on_token=None):
            type(self).observed_messages = list(messages)
            return {
                "role": "assistant",
                "content": "resumed successfully",
                "_usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "estimated": True,
                },
            }

    monkeypatch.setenv("SCA_API_KEY", "test-key")
    monkeypatch.setattr("core.llm.LLMClient", ResumeLLM)
    store = SQLiteRunStore(run_database_path(str(tmp_path)))
    await store.initialize()
    record = RunRecord(
        run_id="run_cli_resume",
        workspace_dir=str(tmp_path),
        model="resume-model",
        status=RunStatus.PAUSED,
        created_at=100.0,
        updated_at=101.0,
    )
    messages = ContextManager(system_prompt="system")
    messages.add_user_message("original request")
    context = await create_durable_run_context(
        workspace_dir=str(tmp_path / "seed"),
        model="resume-model",
        messages=messages.messages,
        run_id="run_seed",
    )
    checkpoint = await context.checkpoint(messages.messages)
    checkpoint = type(checkpoint)(
        run_id=record.run_id,
        messages=checkpoint.messages,
        task_snapshot=checkpoint.task_snapshot,
        saved_at=checkpoint.saved_at,
    )
    await store.create_run(record, checkpoint)

    result = await resume_once(record.run_id, str(tmp_path))

    assert result == "resumed successfully"
    assert ResumeLLM.observed_messages[1]["content"] == "original request"
    assert len(ResumeLLM.observed_messages) == 2
    completed = await store.load_run(record.run_id)
    assert completed is not None
    assert completed.record.status is RunStatus.COMPLETED
