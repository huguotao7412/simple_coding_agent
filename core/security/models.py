from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any


class SecurityMode(StrEnum):
    LOCAL = "local"
    HYBRID = "hybrid"
    STRICT = "strict"
    OFF = "off"


class GuardStage(StrEnum):
    USER_INPUT = "user_input"
    PRE_MODEL = "pre_model"
    TOOL_INTENT = "tool_intent"
    TOOL_OUTPUT = "tool_output"
    FINAL_OUTPUT = "final_output"


class GuardOutcome(StrEnum):
    ALLOW = "allow"
    REVIEW = "review"
    DENY = "deny"
    ERROR = "error"


class RiskLevel(IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3


class SecurityOutcome(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class Capability(StrEnum):
    READ_WORKSPACE = "read_workspace"
    WRITE_WORKSPACE = "write_workspace"
    CREATE_FILE = "create_file"
    DELETE_FILE = "delete_file"
    EXECUTE_PROCESS = "execute_process"
    NETWORK_ACCESS = "network_access"
    GIT_READ = "git_read"
    GIT_MUTATION = "git_mutation"
    CHANGE_DEPENDENCIES = "change_dependencies"
    ACCESS_SECRETS = "access_secrets"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    APPLY_VERIFIED_PATCH = "apply_verified_patch"
    DELEGATE_ACTOR = "delegate_actor"


@dataclass(frozen=True)
class GuardUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class ContentGuardRequest:
    stage: GuardStage
    text: str
    run_id: str
    actor_id: str = ""
    task_id: str = ""
    role: str = "planner"
    correlation_id: str = ""
    source_trust: str = "untrusted"
    data_classification: str = "user_content"
    original_user_intent_summary: str = ""
    tool_name: str | None = None
    requested_capabilities: frozenset[Capability] = frozenset()


@dataclass(frozen=True)
class ContentGuardAssessment:
    provider: str
    outcome: GuardOutcome
    risk_level: RiskLevel = RiskLevel.LOW
    categories: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    reason: str = ""
    tripwire_triggered: bool = False
    provider_error: str = ""
    usage: GuardUsage = GuardUsage()
    latency_ms: float = 0.0
    sanitized_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SecurityDecision:
    outcome: SecurityOutcome
    reason: str
    rule_ids: tuple[str, ...] = ()
    risk_level: RiskLevel = RiskLevel.LOW
    capabilities: frozenset[Capability] = frozenset()
    action_fingerprint: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome is SecurityOutcome.ALLOW


@dataclass
class GuardrailMetrics:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    failures: int = 0
    tripwires: int = 0
    latency_ms: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


__all__ = [
    "Capability",
    "ContentGuardAssessment",
    "ContentGuardRequest",
    "GuardOutcome",
    "GuardStage",
    "GuardUsage",
    "GuardrailMetrics",
    "RiskLevel",
    "SecurityDecision",
    "SecurityMode",
    "SecurityOutcome",
]
