from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PolicyOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyDecision:
    outcome: PolicyOutcome
    tool_name: str
    role: str
    reason: str

    @property
    def allowed(self) -> bool:
        return self.outcome is PolicyOutcome.ALLOW


@dataclass(frozen=True)
class ToolPolicy:
    role: str
    allowed_tools: frozenset[str] | None = None

    @classmethod
    def for_role(
        cls,
        role: str,
        allowed_tools: set[str] | None,
    ) -> ToolPolicy:
        normalized = None if allowed_tools is None else frozenset(allowed_tools)
        return cls(role=role, allowed_tools=normalized)

    def authorize(self, tool_name: str) -> PolicyDecision:
        allowed = self.allowed_tools is None or tool_name in self.allowed_tools
        if allowed:
            return PolicyDecision(
                outcome=PolicyOutcome.ALLOW,
                tool_name=tool_name,
                role=self.role,
                reason="allowed",
            )
        return PolicyDecision(
            outcome=PolicyOutcome.DENY,
            tool_name=tool_name,
            role=self.role,
            reason=f"tool '{tool_name}' is not permitted for role '{self.role}'",
        )
