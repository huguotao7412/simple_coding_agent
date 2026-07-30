from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..events import AgentEvent
from ..runs.context import RunContext
from .content_guard import ContentGuardProvider
from .capabilities import TOOL_CAPABILITIES
from .egress import DataEgressPolicy
from .tool_security import SecurityMiddleware
from .models import (
    Capability,
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
    max_output_bytes: int = 65_536

    async def inspect(
        self,
        *,
        stage: GuardStage,
        text: str,
        actor_id: str = "",
        role: str = "planner",
        tool_name: str | None = None,
        data_classification: str = "user_content",
        source_trust: str = "untrusted",
        original_user_intent_summary: str = "",
        requested_capabilities: frozenset[Capability] = frozenset(),
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
            source_trust=source_trust,
            data_classification=(
                "tool_output" if egress_denied else effective_classification
            ),
            original_user_intent_summary=original_user_intent_summary,
            tool_name=tool_name,
            requested_capabilities=requested_capabilities,
        )
        if emit_events:
            await self._event("content_guard_started", stage=stage.value, provider="composite")
        assessment = await self.guard.inspect(request)
        if redaction.count and stage is GuardStage.USER_INPUT:
            assessment = ContentGuardAssessment(
                provider=assessment.provider,
                outcome=GuardOutcome.DENY,
                risk_level=max(assessment.risk_level, RiskLevel.CRITICAL),
                categories=tuple(dict.fromkeys(
                    assessment.categories + ("secret_value",)
                )),
                rule_ids=tuple(dict.fromkeys(
                    assessment.rule_ids + ("SCA-SECRET-VALUE",)
                )),
                reason="Input contained credential material that cannot be processed.",
                tripwire_triggered=True,
                provider_error=assessment.provider_error,
                usage=assessment.usage,
                latency_ms=assessment.latency_ms,
                sanitized_metadata=assessment.sanitized_metadata,
            )
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
        providers = assessment.sanitized_metadata.get("providers", ())
        if "openai_guardrails" in providers:
            provider_latencies = assessment.sanitized_metadata.get(
                "provider_latency_ms",
                {},
            )
            self.metrics.calls += 1
            self.metrics.prompt_tokens += assessment.usage.prompt_tokens
            self.metrics.completion_tokens += assessment.usage.completion_tokens
            self.metrics.latency_ms += float(
                provider_latencies.get("openai_guardrails", 0.0)
                if isinstance(provider_latencies, dict)
                else 0.0
            )
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
        providers = assessment.sanitized_metadata.get("providers", ())
        provider_latencies = assessment.sanitized_metadata.get(
            "provider_latency_ms",
            {},
        )
        provider_tripwires = assessment.sanitized_metadata.get(
            "provider_tripwires",
            {},
        )
        external_called = "openai_guardrails" in providers
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
            guardrail_total_tokens=assessment.usage.total_tokens,
            guardrail_called=external_called,
            guardrail_tripwire=bool(
                provider_tripwires.get("openai_guardrails", False)
                if isinstance(provider_tripwires, dict)
                else False
            ),
            guardrail_latency_ms=float(
                provider_latencies.get("openai_guardrails", 0.0)
                if isinstance(provider_latencies, dict)
                else 0.0
            ),
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
        capabilities = TOOL_CAPABILITIES.get(tool_name, frozenset())
        summary = json.dumps(
            {
                "tool": tool_name,
                "capabilities": sorted(cap.value for cap in capabilities),
                "arguments": _summarize_tool_arguments(sanitized),
            },
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
            requested_capabilities=capabilities,
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
        elif decision.approval_consumed:
            await self._event(
                "approval_consumed",
                stage=GuardStage.TOOL_INTENT.value,
                action_fingerprint=decision.action_fingerprint,
                risk=decision.risk_level.name.lower(),
                approval_status="consumed",
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
        output = str(redacted.value)
        encoded = output.encode("utf-8")
        truncated = len(encoded) > self.max_output_bytes
        if truncated:
            output = (
                encoded[: self.max_output_bytes]
                .decode("utf-8", errors="ignore")
                + "\n[OUTPUT TRUNCATED BY SECURITY POLICY]"
            )
        if redacted.count:
            await self._event(
                "output_redacted",
                stage=stage.value,
                redaction_count=redacted.count,
                categories=redacted.categories,
            )
        if truncated:
            await self._event(
                "output_redacted",
                stage=stage.value,
                redaction_count=0,
                categories=("output_size_limit",),
                original_bytes=len(encoded),
                retained_bytes=self.max_output_bytes,
            )
        return output

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


_SAFE_ARGUMENT_KEYS = frozenset({
    "attempt",
    "cwd",
    "mode",
    "strategy",
    "task_id",
    "timeout",
})
_PATH_ARGUMENT_KEYS = frozenset({
    "path",
    "paths",
    "file_path",
    "dir_path",
    "source",
    "destination",
    "workspace_dir",
})


def _summarize_tool_arguments(value: Any, key: str = "") -> Any:
    """Return egress-safe metadata, never raw source, diffs, or commands."""
    if isinstance(value, dict):
        return {
            str(item_key): _summarize_tool_arguments(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return {
            "type": "array",
            "items": len(value),
            "sample": [
                _summarize_tool_arguments(item, key) for item in value[:5]
            ],
        }
    if isinstance(value, str):
        if key in _PATH_ARGUMENT_KEYS:
            return {
                "type": "path",
                "basename": os.path.basename(value.rstrip("/\\")),
                "absolute": os.path.isabs(value),
            }
        if key in {"command", "cmd", "script"}:
            executable = value.strip().split(maxsplit=1)[0] if value.strip() else ""
            return {
                "type": "command",
                "executable": os.path.basename(executable),
                "characters": len(value),
            }
        if key in _SAFE_ARGUMENT_KEYS and len(value) <= 128:
            return value
        return {"type": "string", "characters": len(value)}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return {"type": type(value).__name__}


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
