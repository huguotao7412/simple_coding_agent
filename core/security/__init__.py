from typing import Any

from .approvals import (
    ApprovalGrant,
    ApprovalStore,
    canonical_action_fingerprint,
)
from .composite_guard import CompositeContentGuardProvider
from .content_guard import ContentGuardProvider
from .egress import DataEgressPolicy
from .local_guard import LocalContentGuardProvider
from .manager import SecurityManager
from .models import (
    Capability,
    ContentGuardAssessment,
    ContentGuardRequest,
    GuardOutcome,
    GuardStage,
    RiskLevel,
    SecurityDecision,
    SecurityMode,
    SecurityOutcome,
)
from .redaction import redact_structure, redact_text
from .tool_security import SecurityMiddleware


def build_security_manager(*args: Any, **kwargs: Any) -> SecurityManager:
    """Lazy compatibility entry point that keeps adapters out of package import."""
    from .factory import build_security_manager as build

    return build(*args, **kwargs)


def __getattr__(name: str) -> Any:
    if name == "OpenAIGuardrailsProvider":
        from .openai_guard import OpenAIGuardrailsProvider

        return OpenAIGuardrailsProvider
    raise AttributeError(name)

__all__ = [
    "ApprovalGrant",
    "ApprovalStore",
    "Capability",
    "CompositeContentGuardProvider",
    "ContentGuardAssessment",
    "ContentGuardProvider",
    "ContentGuardRequest",
    "DataEgressPolicy",
    "GuardOutcome",
    "GuardStage",
    "LocalContentGuardProvider",
    "OpenAIGuardrailsProvider",
    "RiskLevel",
    "SecurityDecision",
    "SecurityManager",
    "SecurityMiddleware",
    "SecurityMode",
    "SecurityOutcome",
    "build_security_manager",
    "canonical_action_fingerprint",
    "redact_structure",
    "redact_text",
]
