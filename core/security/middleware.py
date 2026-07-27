"""Compatibility facade for deterministic security policy components.

New code should import from ``approvals``, ``capabilities``, ``egress``, or
``tool_security``. This module preserves the original public import path.
"""

from .approvals import (
    ApprovalGrant,
    ApprovalStore,
    canonical_action_fingerprint,
)
from .capabilities import ROLE_CAPABILITIES, TOOL_CAPABILITIES
from .egress import DataEgressPolicy
from .tool_security import SecurityMiddleware

__all__ = [
    "ApprovalGrant",
    "ApprovalStore",
    "DataEgressPolicy",
    "ROLE_CAPABILITIES",
    "SecurityMiddleware",
    "TOOL_CAPABILITIES",
    "canonical_action_fingerprint",
]
