from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Capability, RiskLevel


def canonical_action_fingerprint(
    *,
    run_id: str,
    actor_id: str,
    role: str,
    workspace: str,
    tool_name: str,
    arguments: dict[str, Any],
    capabilities: frozenset[Capability],
    risk_level: RiskLevel,
    policy_version: str,
) -> str:
    payload = {
        "run_id": run_id,
        "actor_id": actor_id,
        "role": role,
        "workspace": os.path.normcase(str(Path(workspace).resolve())),
        "tool_name": tool_name,
        "arguments": arguments,
        "capabilities": sorted(cap.value for cap in capabilities),
        "risk_level": risk_level.name,
        "policy_version": policy_version,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class ApprovalGrant:
    run_id: str
    actor_id: str
    role: str
    workspace_identity: str
    tool_name: str
    arguments_hash: str
    capabilities: frozenset[Capability]
    risk_level: RiskLevel
    policy_version: str
    created_at: float
    expires_at: float
    single_use: bool = True
    consumed_at: float | None = None

    def consume(
        self,
        fingerprint: str,
        *,
        run_id: str,
        actor_id: str,
        role: str,
        workspace_identity: str,
        tool_name: str,
        capabilities: frozenset[Capability],
        risk_level: RiskLevel,
        policy_version: str,
        now: float | None = None,
    ) -> bool:
        current = time.time() if now is None else now
        expected_workspace = os.path.normcase(
            str(Path(workspace_identity).resolve())
        )
        grant_workspace = os.path.normcase(
            str(Path(self.workspace_identity).resolve())
        )
        if (
            self.arguments_hash != fingerprint
            or self.run_id != run_id
            or self.actor_id != actor_id
            or self.role != role
            or grant_workspace != expected_workspace
            or self.tool_name != tool_name
            or self.capabilities != capabilities
            or self.risk_level is not risk_level
            or self.policy_version != policy_version
            or current < self.created_at
            or current >= self.expires_at
        ):
            return False
        if self.single_use and self.consumed_at is not None:
            return False
        self.consumed_at = current
        return True


@dataclass
class ApprovalStore:
    grants: dict[str, ApprovalGrant] = field(default_factory=dict)

    def add(self, grant: ApprovalGrant) -> None:
        self.grants[grant.arguments_hash] = grant

    def consume(
        self,
        fingerprint: str,
        *,
        run_id: str,
        actor_id: str,
        role: str,
        workspace_identity: str,
        tool_name: str,
        capabilities: frozenset[Capability],
        risk_level: RiskLevel,
        policy_version: str,
    ) -> bool:
        grant = self.grants.get(fingerprint)
        return (
            grant.consume(
                fingerprint,
                run_id=run_id,
                actor_id=actor_id,
                role=role,
                workspace_identity=workspace_identity,
                tool_name=tool_name,
                capabilities=capabilities,
                risk_level=risk_level,
                policy_version=policy_version,
            )
            if grant is not None
            else False
        )


__all__ = [
    "ApprovalGrant",
    "ApprovalStore",
    "canonical_action_fingerprint",
]
