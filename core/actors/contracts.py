from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from ..runs.context import RunContext


ActorExecutionStatus = Literal["done", "failed"]


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


@dataclass(frozen=True)
class ActorTaskSpec:
    task_id: str
    description: str
    context_files: tuple[str, ...] = ()
    context_summaries: tuple[str, ...] = ()
    role: str = "coder"
    max_steps: int | None = None
    dependencies: tuple[str, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        value: dict[str, Any],
        *,
        dependencies: tuple[str, ...] = (),
    ) -> ActorTaskSpec:
        raw_max_steps = value.get("max_steps")
        max_steps = raw_max_steps if isinstance(raw_max_steps, int) else None
        return cls(
            task_id=str(value.get("task_id", "")),
            description=str(value.get("description", "")),
            context_files=_string_tuple(value.get("context_files")),
            context_summaries=_string_tuple(value.get("context_summaries")),
            role=str(value.get("role", "coder")),
            max_steps=max_steps,
            dependencies=dependencies,
        )


@dataclass(frozen=True)
class ActorExecutionResult:
    task_id: str
    status: ActorExecutionStatus
    error: str = ""
    files_modified: tuple[str, ...] = ()
    bugs_found: tuple[str, ...] = ()
    key_findings: str = ""
    suggested_next_steps: str = ""
    diff_artifact: str = ""
    diff: str = ""


@runtime_checkable
class ActorExecutor(Protocol):
    async def execute(
        self,
        spec: ActorTaskSpec,
        run_context: RunContext,
    ) -> ActorExecutionResult: ...


__all__ = [
    "ActorExecutionResult",
    "ActorExecutionStatus",
    "ActorExecutor",
    "ActorTaskSpec",
]
