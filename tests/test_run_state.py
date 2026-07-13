from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from core.runs.models import (
    RunCheckpoint,
    RunRecord,
    RunStatus,
    RunTransitionError,
    transition_run,
)


def make_record(status: RunStatus = RunStatus.CREATED, version: int = 0) -> RunRecord:
    return RunRecord(
        run_id="run_test",
        workspace_dir="C:/workspace",
        model="test-model",
        status=status,
        version=version,
        created_at=100.0,
        updated_at=100.0,
    )


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RunStatus.CREATED, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.PAUSED),
        (RunStatus.RUNNING, RunStatus.COMPLETED),
        (RunStatus.RUNNING, RunStatus.FAILED),
        (RunStatus.PAUSED, RunStatus.RUNNING),
        (RunStatus.PAUSED, RunStatus.FAILED),
        (RunStatus.FAILED, RunStatus.RUNNING),
    ],
)
def test_transition_run_accepts_legal_lifecycle(
    source: RunStatus,
    target: RunStatus,
) -> None:
    record = make_record(source, version=3)

    updated = transition_run(record, target, now=123.0)

    assert updated.status is target
    assert updated.version == 4
    assert updated.updated_at == 123.0
    assert record.status is source


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RunStatus.CREATED, RunStatus.COMPLETED),
        (RunStatus.PAUSED, RunStatus.COMPLETED),
        (RunStatus.FAILED, RunStatus.COMPLETED),
        (RunStatus.COMPLETED, RunStatus.RUNNING),
        (RunStatus.COMPLETED, RunStatus.FAILED),
        (RunStatus.RUNNING, RunStatus.CREATED),
    ],
)
def test_transition_run_rejects_illegal_lifecycle(
    source: RunStatus,
    target: RunStatus,
) -> None:
    with pytest.raises(RunTransitionError, match=f"{source}.*{target}"):
        transition_run(make_record(source), target)


def test_transition_run_records_failure_reason() -> None:
    failed = transition_run(
        make_record(RunStatus.RUNNING),
        RunStatus.FAILED,
        error="provider unavailable",
        now=200.0,
    )

    assert failed.error == "provider unavailable"

    resumed = transition_run(failed, RunStatus.RUNNING, now=201.0)

    assert resumed.error == ""


def test_run_records_and_checkpoints_are_immutable() -> None:
    record = make_record()
    checkpoint = RunCheckpoint(
        run_id=record.run_id,
        messages=({"role": "system", "content": "system"},),
        task_snapshot={"task_tree": {}, "change_count": 0},
        prompt_tokens=10,
        completion_tokens=5,
        usage_estimated=False,
        completed_tool_calls={"call_1": "ok"},
        saved_at=101.0,
    )

    with pytest.raises(FrozenInstanceError):
        record.version = 1  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        checkpoint.saved_at = 102.0  # type: ignore[misc]


def test_run_checkpoint_rejects_a_mismatched_or_empty_run_id() -> None:
    with pytest.raises(ValueError, match="run_id"):
        RunCheckpoint(
            run_id="",
            messages=(),
            task_snapshot={},
            saved_at=1.0,
        )
