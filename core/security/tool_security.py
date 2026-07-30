from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .approvals import ApprovalStore, canonical_action_fingerprint
from .capabilities import ROLE_CAPABILITIES, TOOL_CAPABILITIES
from .models import (
    Capability,
    ContentGuardAssessment,
    GuardOutcome,
    RiskLevel,
    SecurityDecision,
    SecurityOutcome,
)
from .redaction import redact_structure


DESTRUCTIVE = re.compile(
    r"(?i)(?:\brm\s+-(?:\w*[rf]\w*)|\bgit\s+reset\s+--hard|"
    r"\bgit\s+clean\s+-\w*f\w*d|\bdrop\s+(?:database|table)|"
    r"\bdelete\s+from\b|\bremove-item\b.*-recurse)"
)
NETWORK = re.compile(
    r"(?i)(?:https?://|\bcurl\b|\bwget\b|\binvoke-webrequest\b|\bssh\b|\bnc\b)"
)
DEPENDENCY = re.compile(
    r"(?i)(?:\b(?:pip|uv)\s+install\b|"
    r"\b(?:npm|pnpm|yarn)\s+(?:install|add)\b|"
    r"\bpoetry\s+add\b|\bcargo\s+add\b)"
)
GIT_COMMAND = re.compile(r"(?i)^\s*git(?:\.exe)?\s+([a-z-]+)")
GIT_READ_SUBCOMMANDS = frozenset({
    "branch",
    "diff",
    "grep",
    "log",
    "rev-parse",
    "show",
    "status",
})
PATH_ARGUMENT_KEYS = frozenset({
    "path",
    "paths",
    "file_path",
    "dir_path",
    "source",
    "destination",
    "workspace_dir",
})


class SecurityMiddleware:
    """Final deterministic PDP/PEP for every tool call."""

    policy_version = "sca-security/1"

    def __init__(
        self,
        workspace: str,
        *,
        approvals: ApprovalStore | None = None,
    ) -> None:
        self.workspace = str(Path(workspace).resolve())
        self.approvals = approvals or ApprovalStore()

    def authorize_tool(
        self,
        *,
        run_id: str,
        actor_id: str,
        role: str,
        tool_name: str,
        arguments: dict[str, Any],
        guard: ContentGuardAssessment | None = None,
    ) -> SecurityDecision:
        registered_capabilities = TOOL_CAPABILITIES.get(tool_name)
        if registered_capabilities is None:
            return SecurityDecision(
                SecurityOutcome.DENY,
                "Unknown tool denied by default.",
                ("SCA-UNKNOWN-TOOL",),
                RiskLevel.HIGH,
            )
        if not isinstance(arguments, dict):
            return SecurityDecision(
                SecurityOutcome.DENY,
                "Tool arguments do not match the required object schema.",
                ("SCA-TOOL-SCHEMA",),
                RiskLevel.HIGH,
                registered_capabilities,
            )
        command = _command(arguments)
        capabilities = _effective_capabilities(
            registered_capabilities,
            command,
        )
        risk = RiskLevel.LOW
        approval_rules: list[str] = []
        if command and NETWORK.search(command):
            risk = RiskLevel.HIGH
            approval_rules.append("SCA-NETWORK-AUTHORIZATION")
        if command and DEPENDENCY.search(command):
            risk = max(risk, RiskLevel.HIGH)
            approval_rules.append("SCA-DEPENDENCY-CHANGE")
        if guard is not None:
            risk = max(risk, guard.risk_level)
        fingerprint = canonical_action_fingerprint(
            run_id=run_id,
            actor_id=actor_id,
            role=role,
            workspace=self.workspace,
            tool_name=tool_name,
            arguments=arguments,
            capabilities=capabilities,
            risk_level=risk,
            policy_version=self.policy_version,
        )
        allowed = ROLE_CAPABILITIES.get(role, frozenset())
        if capabilities - allowed:
            return SecurityDecision(
                SecurityOutcome.DENY,
                "Role lacks one or more required capabilities.",
                ("SCA-ROLE-CAPABILITY",),
                RiskLevel.HIGH,
                capabilities,
                fingerprint,
            )
        if self._escapes_workspace(arguments):
            return SecurityDecision(
                SecurityOutcome.DENY,
                "Path access escapes the authorized workspace.",
                ("SCA-WORKSPACE-BOUNDARY",),
                RiskLevel.CRITICAL,
                capabilities,
                fingerprint,
            )
        if command and DESTRUCTIVE.search(command):
            return SecurityDecision(
                SecurityOutcome.DENY,
                "Destructive command blocked by deterministic policy.",
                ("SCA-DESTRUCTIVE-COMMAND",),
                RiskLevel.CRITICAL,
                capabilities,
                fingerprint,
            )
        if guard is not None:
            if guard.outcome is GuardOutcome.DENY:
                return SecurityDecision(
                    SecurityOutcome.DENY,
                    "Content risk signal blocked this action.",
                    guard.rule_ids,
                    risk,
                    capabilities,
                    fingerprint,
                )
            if guard.outcome in {GuardOutcome.REVIEW, GuardOutcome.ERROR}:
                approval_rules.extend(guard.rule_ids or ("SCA-GUARD-REVIEW",))
        approval_consumed = False
        if approval_rules:
            approval_consumed = self.approvals.consume(
                fingerprint,
                run_id=run_id,
                actor_id=actor_id,
                role=role,
                workspace_identity=self.workspace,
                tool_name=tool_name,
                capabilities=capabilities,
                risk_level=risk,
                policy_version=self.policy_version,
            )
            if not approval_consumed:
                return SecurityDecision(
                    SecurityOutcome.REQUIRE_APPROVAL,
                    "This exact action requires approval.",
                    tuple(dict.fromkeys(approval_rules)),
                    risk,
                    capabilities,
                    fingerprint,
                )
        return SecurityDecision(
            SecurityOutcome.ALLOW,
            "Allowed by deterministic security policy.",
            (),
            risk,
            capabilities,
            fingerprint,
            approval_consumed,
        )

    def redact_tool_output(self, value: Any) -> tuple[Any, int, tuple[str, ...]]:
        result = redact_structure(value)
        return result.value, result.count, result.categories

    def _escapes_workspace(self, arguments: dict[str, Any]) -> bool:
        for key, value in _walk(arguments):
            if (
                key not in PATH_ARGUMENT_KEYS
                or not isinstance(value, str)
                or not value
            ):
                continue
            if key == "workspace_dir":
                candidate = Path(value).resolve()
            else:
                raw = Path(value)
                candidate = (
                    raw.resolve()
                    if raw.is_absolute()
                    else (Path(self.workspace) / raw).resolve()
                )
            try:
                candidate.relative_to(self.workspace)
            except ValueError:
                return True
        return False


def _walk(value: Any) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.append((str(key), item))
            found.extend(_walk(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_walk(item))
    return found


def _command(arguments: dict[str, Any]) -> str:
    for key in ("command", "cmd", "script"):
        if isinstance(value := arguments.get(key), str):
            return value
    return ""


def _effective_capabilities(
    registered: frozenset[Capability],
    command: str,
) -> frozenset[Capability]:
    effective = set(registered)
    if NETWORK.search(command):
        effective.update({
            Capability.NETWORK_ACCESS,
            Capability.EXTERNAL_SIDE_EFFECT,
        })
    if DEPENDENCY.search(command):
        effective.add(Capability.CHANGE_DEPENDENCIES)
    if match := GIT_COMMAND.search(command):
        effective.add(
            Capability.GIT_READ
            if match.group(1).lower() in GIT_READ_SUBCOMMANDS
            else Capability.GIT_MUTATION
        )
    return frozenset(effective)


__all__ = ["SecurityMiddleware"]
