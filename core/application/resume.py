from __future__ import annotations

from ..domain.runs import RunAggregate
from ..runs.models import RunStatus


def assert_resumable(aggregate: RunAggregate, status: RunStatus) -> None:
    if status is RunStatus.COMPLETED:
        raise RuntimeError("duplicate resume rejected: Run is already completed")
    if status not in {RunStatus.PAUSED, RunStatus.FAILED, RunStatus.RUNNING}:
        raise RuntimeError(f"Run status is not resumable: {status}")


__all__ = ["assert_resumable"]
