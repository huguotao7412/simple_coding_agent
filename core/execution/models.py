from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


TASK_ASSESSMENT_SCHEMA_VERSION = 1


class TaskIntent(StrEnum):
    READ_ONLY = "read_only"
    CODE_CHANGE = "code_change"
    TEST_CHANGE = "test_change"
    DOCUMENTATION = "documentation"
    OPERATIONS = "operations"


class TaskComplexity(StrEnum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class TaskRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ExecutionStrategy(StrEnum):
    PLANNER_DIRECT = "planner_direct"
    SINGLE_ACTOR = "single_actor"
    CODER_WITH_GATES = "coder_with_gates"
    SCOUT_THEN_CODER = "scout_then_coder"
    SCOUT_THEN_DAG = "scout_then_dag"


@dataclass(frozen=True)
class WorkspaceProfile:
    file_count: int
    source_file_count: int
    test_file_count: int
    top_level_dirs: tuple[str, ...]
    languages: tuple[str, ...]
    has_git: bool
    has_quality_gates: bool
    scan_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> WorkspaceProfile:
        return cls(
            file_count=int(value["file_count"]),
            source_file_count=int(value["source_file_count"]),
            test_file_count=int(value["test_file_count"]),
            top_level_dirs=tuple(str(item) for item in value["top_level_dirs"]),
            languages=tuple(str(item) for item in value["languages"]),
            has_git=bool(value["has_git"]),
            has_quality_gates=bool(value["has_quality_gates"]),
            scan_truncated=bool(value.get("scan_truncated", False)),
        )


@dataclass(frozen=True)
class TaskAssessment:
    intent: TaskIntent
    complexity: TaskComplexity
    risk: TaskRisk
    strategy: ExecutionStrategy
    reasons: tuple[str, ...]
    explicit_paths: tuple[str, ...]
    workspace: WorkspaceProfile
    max_actors: int
    verifier_recommended: bool
    requires_human_approval: bool
    schema_version: int = TASK_ASSESSMENT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "intent": self.intent.value,
            "complexity": self.complexity.value,
            "risk": self.risk.value,
            "strategy": self.strategy.value,
            "reasons": list(self.reasons),
            "explicit_paths": list(self.explicit_paths),
            "workspace": self.workspace.to_dict(),
            "execution_hints": {
                "max_actors": self.max_actors,
                "verifier_recommended": self.verifier_recommended,
                "requires_human_approval": self.requires_human_approval,
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    def to_system_message(self) -> str:
        return (
            "<task_assessment>\n"
            + self.to_json()
            + "\n</task_assessment>\n"
            "This is a deterministic initial policy recommendation. Use the recommended "
            "strategy by default. Override it only when repository evidence contradicts "
            "the assessment, and explain the reason in the final response."
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TaskAssessment:
        if int(value.get("schema_version", 0)) != TASK_ASSESSMENT_SCHEMA_VERSION:
            raise ValueError("unsupported task assessment schema")
        workspace = value.get("workspace")
        hints = value.get("execution_hints")
        if not isinstance(workspace, dict) or not isinstance(hints, dict):
            raise ValueError("invalid task assessment payload")
        return cls(
            schema_version=TASK_ASSESSMENT_SCHEMA_VERSION,
            intent=TaskIntent(str(value["intent"])),
            complexity=TaskComplexity(str(value["complexity"])),
            risk=TaskRisk(str(value["risk"])),
            strategy=ExecutionStrategy(str(value["strategy"])),
            reasons=tuple(str(item) for item in value.get("reasons", [])),
            explicit_paths=tuple(
                str(item) for item in value.get("explicit_paths", [])
            ),
            workspace=WorkspaceProfile.from_dict(workspace),
            max_actors=int(hints["max_actors"]),
            verifier_recommended=bool(hints["verifier_recommended"]),
            requires_human_approval=bool(hints["requires_human_approval"]),
        )


__all__ = [
    "ExecutionStrategy",
    "TASK_ASSESSMENT_SCHEMA_VERSION",
    "TaskAssessment",
    "TaskComplexity",
    "TaskIntent",
    "TaskRisk",
    "WorkspaceProfile",
]
