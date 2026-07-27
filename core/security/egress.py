from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from .models import RiskLevel, SecurityDecision, SecurityOutcome


@dataclass(frozen=True)
class DataEgressPolicy:
    allow_external_guardrails: bool = False
    allowed_stages: frozenset[str] = frozenset({
        "user_input",
        "tool_intent",
        "final_output",
    })
    allowed_data_classifications: frozenset[str] = frozenset({
        "user_content",
        "sanitized_summary",
    })
    allowed_provider_hosts: frozenset[str] = frozenset({"api.openai.com"})
    max_payload_bytes: int = 32_768
    redact_before_send: bool = True
    audit_required: bool = True

    def authorize(
        self,
        *,
        stage: str,
        classification: str,
        provider_url: str,
        payload: str,
        redaction_count: int,
    ) -> SecurityDecision:
        host = (urlsplit(provider_url).hostname or "").lower()
        denied = (
            not self.allow_external_guardrails
            or stage not in self.allowed_stages
            or classification not in self.allowed_data_classifications
            or host not in self.allowed_provider_hosts
            or len(payload.encode("utf-8")) > self.max_payload_bytes
            or classification in {
                "secret",
                "credential",
                "source_code",
                "binary",
                "tool_output",
            }
            or (self.redact_before_send and redaction_count < 0)
        )
        return SecurityDecision(
            outcome=SecurityOutcome.DENY if denied else SecurityOutcome.ALLOW,
            reason=(
                "Data egress denied by deterministic policy."
                if denied
                else "Data egress allowed."
            ),
            rule_ids=("SCA-EGRESS-POLICY",) if denied else (),
            risk_level=RiskLevel.HIGH if denied else RiskLevel.LOW,
        )


__all__ = ["DataEgressPolicy"]
