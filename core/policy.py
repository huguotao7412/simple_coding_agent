from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


DEFAULT_ROLE_TOOLS: dict[str, frozenset[str]] = {
    "scout": frozenset({
        "list_dir", "list_directory", "directory_tree", "read", "read_file",
        "read_text_file", "read_multiple_files", "read_outline",
        "search_codebase", "search_files", "get_file_info",
        "list_allowed_directories",
    }),
    "coder": frozenset({
        "list_dir", "list_directory", "directory_tree", "read", "read_file",
        "read_text_file", "read_multiple_files", "read_outline",
        "search_codebase", "search_files", "get_file_info",
        "list_allowed_directories", "edit", "edit_file", "write",
        "write_file", "create_directory", "run",
    }),
    "verifier": frozenset({
        "list_dir", "list_directory", "directory_tree", "read", "read_file",
        "read_text_file", "read_multiple_files", "search_files",
        "get_file_info", "list_allowed_directories", "run",
    }),
}
DEFAULT_ROLE_TOOLS["actor"] = DEFAULT_ROLE_TOOLS["coder"]
# Compatibility callers construct a provider before binding an Actor role. This
# remains explicit and bounded rather than restoring the former None=all policy.
DEFAULT_ROLE_TOOLS["legacy"] = DEFAULT_ROLE_TOOLS["coder"]


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
    allowed_tools: frozenset[str] = frozenset()

    @classmethod
    def for_role(
        cls,
        role: str,
        allowed_tools: set[str] | None,
    ) -> ToolPolicy:
        normalized = (
            DEFAULT_ROLE_TOOLS.get(role, frozenset())
            if allowed_tools is None
            else frozenset(allowed_tools)
        )
        return cls(role=role, allowed_tools=normalized)

    def authorize(self, tool_name: str) -> PolicyDecision:
        allowed = tool_name in self.allowed_tools
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
