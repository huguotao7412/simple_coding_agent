from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..events import AgentEvent
from ..runs.context import RunContext
from .content_guard import ContentGuardProvider
from .egress import DataEgressPolicy
from .tool_security import SecurityMiddleware
from .models import (
    ContentGuardAssessment,
    ContentGuardRequest,
    GuardOutcome,
    GuardStage,
    GuardrailMetrics,
    SecurityDecision,
    SecurityMode,
    SecurityOutcome,
    RiskLevel,
)
from .redaction import redact_text


@dataclass
class SecurityManager:
    mode: SecurityMode
    guard: ContentGuardProvider
    middleware: SecurityMiddleware
    run_context: RunContext
    egress: DataEgressPolicy
    metrics: GuardrailMetrics
    startup_warning: str = ""
    provider_url: str = "https://api.openai.com/v1"

    async def inspect(
        self,
        *,
        stage: GuardStage,
        text: str,
        actor_id: str = "",
        role: str = "planner",
        tool_name: str | None = None,
        data_classification: str = "user_content",
        emit_events: bool = True,
    ) -> ContentGuardAssessment:
        redaction = redact_text(text)
        effective_classification = (
            "secret" if redaction.count else data_classification
        )
        egress_denied = False
        if self.mode in {SecurityMode.HYBRID, SecurityMode.STRICT}:
            egress_decision = self.egress.authorize(
                stage=stage.value,
                classification=effective_classification,
                provider_url=self.provider_url,
                payload=str(redaction.value),
                redaction_count=redaction.count,
            )
            egress_denied = egress_decision.outcome is SecurityOutcome.DENY
            if emit_events:
                await self._event(
                    "data_egress_denied" if egress_denied else "data_egress_allowed",
                    stage=stage.value,
                    outcome=egress_decision.outcome.value,
                    rule_ids=egress_decision.rule_ids,
                    redaction_count=redaction.count,
                )
        request = ContentGuardRequest(
            stage=stage,
            text=str(redaction.value),
            run_id=self.run_context.run_id,
            actor_id=actor_id,
            task_id=actor_id,
            role=role,
            correlation_id=uuid.uuid4().hex,
            data_classification=(
                "tool_output" if egress_denied else effective_classification
            ),
            tool_name=tool_name,
        )
        if emit_events:
            await self._event("content_guard_started", stage=stage.value, provider="composite")
        assessment = await self.guard.inspect(request)
        if (
            egress_denied
            and self.mode is SecurityMode.STRICT
            and stage.value in self.egress.allowed_stages
            and effective_classification in self.egress.allowed_data_classifications
        ):
            assessment = ContentGuardAssessment(
                provider=assessment.provider,
                outcome=GuardOutcome.DENY,
                risk_level=max(assessment.risk_level, RiskLevel.HIGH),
                categories=assessment.categories + ("data_egress_denied",),
                rule_ids=assessment.rule_ids + ("SCA-EGRESS-POLICY",),
                reason="Strict external inspection could not be performed safely.",
                tripwire_triggered=True,
                provider_error=assessment.provider_error,
                usage=assessment.usage,
                latency_ms=assessment.latency_ms,
                sanitized_metadata=assessment.sanitized_metadata,
            )
        self.metrics.calls += 1
        self.metrics.prompt_tokens += assessment.usage.prompt_tokens
        self.metrics.completion_tokens += assessment.usage.completion_tokens
        self.metrics.latency_ms += assessment.latency_ms
        self.metrics.failures += int(bool(assessment.provider_error))
        self.metrics.tripwires += int(assessment.tripwire_triggered)
        if emit_events:
            await self.audit_assessment(stage, assessment)
        return assessment

    async def audit_assessment(
        self,
        stage: GuardStage,
        assessment: ContentGuardAssessment,
    ) -> None:
        await self._event(
            "content_guard_error" if assessment.provider_error else "content_guard_result",
            stage=stage.value,
            provider=assessment.provider,
            outcome=assessment.outcome.value,
            risk=assessment.risk_level.name.lower(),
            rule_ids=assessment.rule_ids,
            latency_ms=assessment.latency_ms,
            guardrail_prompt_tokens=assessment.usage.prompt_tokens,
            guardrail_completion_tokens=assessment.usage.completion_tokens,
            sanitized_error=assessment.provider_error,
        )

    async def authorize_tool(
        self,
        *,
        actor_id: str,
        role: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> SecurityDecision:
        sanitized, redaction_count, _ = self.middleware.redact_tool_output(arguments)
        if redaction_count:
            decision = SecurityDecision(
                SecurityOutcome.DENY,
                "Secrets are not permitted in tool arguments.",
                ("SCA-SECRET-TOOL-ARGUMENT",),
                RiskLevel.CRITICAL,
            )
            await self._event(
                "security_decision",
                stage=GuardStage.TOOL_INTENT.value,
                outcome=decision.outcome.value,
                risk=decision.risk_level.name.lower(),
                rule_ids=decision.rule_ids,
            )
            return decision
        summary = json.dumps(
            {"tool": tool_name, "arguments": sanitized},
            ensure_ascii=False,
            sort_keys=True,
        )
        guard = await self.inspect(
            stage=GuardStage.TOOL_INTENT,
            text=summary,
            actor_id=actor_id,
            role=role,
            tool_name=tool_name,
            data_classification="sanitized_summary",
        )
        decision = self.middleware.authorize_tool(
            run_id=self.run_context.run_id,
            actor_id=actor_id,
            role=role,
            tool_name=tool_name,
            arguments=arguments,
            guard=guard,
        )
        await self._event(
            "security_decision",
            stage=GuardStage.TOOL_INTENT.value,
            outcome=decision.outcome.value,
            risk=decision.risk_level.name.lower(),
            rule_ids=decision.rule_ids,
            action_fingerprint=decision.action_fingerprint,
            approval_status=(
                "required" if decision.outcome.value == "require_approval" else "not_required"
            ),
        )
        if decision.outcome is SecurityOutcome.REQUIRE_APPROVAL:
            await self._event(
                "approval_requested",
                stage=GuardStage.TOOL_INTENT.value,
                action_fingerprint=decision.action_fingerprint,
                risk=decision.risk_level.name.lower(),
                rule_ids=decision.rule_ids,
            )
        return decision

    async def record_tool_execution(
        self,
        *,
        started: bool,
        actor_id: str,
        tool_name: str,
        success: bool | None = None,
    ) -> None:
        await self._event(
            "tool_execution_started" if started else "tool_execution_finished",
            stage=GuardStage.TOOL_INTENT.value,
            actor_id=actor_id,
            tool_name=tool_name,
            success=success,
        )

    async def redact_output(
        self,
        value: str,
        *,
        stage: GuardStage,
        actor_id: str = "",
    ) -> str:
        redacted = redact_text(value)
        if redacted.count:
            await self._event(
                "output_redacted",
                stage=stage.value,
                redaction_count=redacted.count,
                categories=redacted.categories,
            )
        return str(redacted.value)

    async def emit_startup_warning(self) -> None:
        if self.startup_warning:
            await self._event(
                "content_guard_error",
                stage="startup",
                provider="openai_guardrails",
                outcome="warning",
                sanitized_error=self.startup_warning,
            )

    async def _event(self, event_type: str, **payload: Any) -> None:
        sanitized = self.middleware.redact_tool_output(payload)[0]
        content = json.dumps(sanitized, ensure_ascii=False, sort_keys=True)
        await self.run_context.emit(AgentEvent(type=event_type, content=content))
        if self.run_context.store is not None:
            await self.run_context.store.append_event(
                self.run_context.run_id,
                event_type,
                sanitized,
                time.time(),
            )


def build_security_manager(
    workspace: str,
    run_context: RunContext,
    *,
    environ: dict[str, str] | None = None,
    external_runtime: Any = None,
) -> SecurityManager:
    """Compatibility wrapper; construction policy lives in ``factory``."""
    from .factory import build_security_manager as build

    return build(
        workspace,
        run_context,
        environ=environ,
        external_runtime=external_runtime,
    )


__all__ = ["SecurityManager", "build_security_manager"]
