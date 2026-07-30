from __future__ import annotations

from typing import Any

import pytest

from core.security.models import RiskLevel, SecurityDecision, SecurityOutcome
from core.tools.base import ToolResult
from core.tools.catalog import ToolCatalog, ToolRegistration
from core.tools.gateway import ToolGateway
from core.tools.models import ToolCall


class RecordingPolicy:
    def __init__(self, outcome: SecurityOutcome = SecurityOutcome.ALLOW) -> None:
        self.outcome = outcome
        self.arguments: list[dict[str, Any]] = []

    def authorize_tool(self, **kwargs: Any) -> SecurityDecision:
        self.arguments.append(kwargs["arguments"])
        return SecurityDecision(
            self.outcome,
            "test decision",
            risk_level=RiskLevel.LOW,
            action_fingerprint="fingerprint",
        )


def call(arguments: dict[str, Any], name: str = "write") -> ToolCall:
    return ToolCall(
        call_id="call-1",
        name=name,
        arguments=arguments,
        run_id="run-1",
        actor_id="actor-1",
        role="coder",
        workspace_identity="C:/workspace",
        correlation_id="correlation-1",
    )


def registration(dispatch: Any) -> ToolRegistration:
    return ToolRegistration(
        name="write",
        schema={
            "type": "function",
            "function": {
                "name": "write",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "workspace_dir": {"type": "string"},
                    },
                    "required": ["path"],
                },
            },
        },
        dispatch=dispatch,
        workspace_aware=True,
    )


@pytest.mark.asyncio
async def test_gateway_injects_workspace_before_authorization_and_dispatches_copy() -> None:
    dispatched: list[dict[str, Any]] = []

    async def dispatch(arguments: dict[str, Any]) -> ToolResult:
        dispatched.append(arguments)
        arguments["path"] = "adapter-local-mutation"
        return ToolResult.ok("token=super-secret-value")

    catalog = ToolCatalog()
    catalog.register(registration(dispatch))
    policy = RecordingPolicy()
    gateway = ToolGateway(
        catalog,
        workspace_dir="C:/workspace",
        middleware=policy,  # type: ignore[arg-type]
    )
    original = call({"path": "file.txt"})
    result = await gateway.execute(original)

    assert policy.arguments == [{
        "path": "file.txt",
        "workspace_dir": "C:/workspace",
    }]
    assert dispatched[0]["workspace_dir"] == "C:/workspace"
    assert original.arguments["path"] == "file.txt"
    assert "[REDACTED]" in result.content
    assert "super-secret-value" not in result.content


@pytest.mark.asyncio
async def test_gateway_denies_unknown_schema_and_policy_before_dispatch() -> None:
    dispatch_count = 0

    async def dispatch(arguments: dict[str, Any]) -> ToolResult:
        nonlocal dispatch_count
        dispatch_count += 1
        return ToolResult.ok("unexpected")

    catalog = ToolCatalog()
    catalog.register(registration(dispatch))
    policy = RecordingPolicy(SecurityOutcome.DENY)
    gateway = ToolGateway(
        catalog,
        workspace_dir="C:/workspace",
        middleware=policy,  # type: ignore[arg-type]
    )

    assert (await gateway.execute(call({}, name="missing"))).policy_denied
    assert (await gateway.execute(call({"path": 7}))).policy_denied
    assert (await gateway.execute(call({"path": "file.txt"}))).policy_denied
    assert dispatch_count == 0
