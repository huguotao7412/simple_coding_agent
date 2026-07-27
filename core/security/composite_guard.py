from __future__ import annotations

from collections.abc import Iterable

from .content_guard import ContentGuardProvider
from .models import (
    ContentGuardAssessment,
    ContentGuardRequest,
    GuardOutcome,
    GuardUsage,
    SecurityMode,
)


LOCAL_ONLY_CLASSIFICATIONS = frozenset({
    "source_code",
    "secret",
    "credential",
    "binary",
    "tool_output",
})


class CompositeContentGuardProvider:
    name = "composite"

    def __init__(
        self,
        providers: Iterable[ContentGuardProvider],
        mode: SecurityMode,
    ) -> None:
        self.providers = tuple(providers)
        self.mode = mode

    async def inspect(
        self,
        request: ContentGuardRequest,
    ) -> ContentGuardAssessment:
        providers = self.providers
        if (
            request.stage.value == "tool_output"
            or request.data_classification in LOCAL_ONLY_CLASSIFICATIONS
        ):
            providers = providers[:1]
        assessments = [await provider.inspect(request) for provider in providers]
        if not assessments:
            return ContentGuardAssessment(
                provider=self.name,
                outcome=GuardOutcome.ALLOW,
            )
        outcome = self._combined_outcome(assessments)
        return ContentGuardAssessment(
            provider=self.name,
            outcome=outcome,
            risk_level=max(item.risk_level for item in assessments),
            categories=tuple(dict.fromkeys(
                category
                for item in assessments
                for category in item.categories
            )),
            rule_ids=tuple(dict.fromkeys(
                rule_id
                for item in assessments
                for rule_id in item.rule_ids
            )),
            reason="Monotonic aggregate of local and optional external guardrails.",
            tripwire_triggered=any(
                item.tripwire_triggered for item in assessments
            ),
            provider_error="; ".join(
                item.provider_error
                for item in assessments
                if item.provider_error
            ),
            usage=GuardUsage(
                sum(item.usage.prompt_tokens for item in assessments),
                sum(item.usage.completion_tokens for item in assessments),
            ),
            latency_ms=sum(item.latency_ms for item in assessments),
            sanitized_metadata={
                "providers": tuple(item.provider for item in assessments),
            },
        )

    def _combined_outcome(
        self,
        assessments: list[ContentGuardAssessment],
    ) -> GuardOutcome:
        if any(item.outcome is GuardOutcome.DENY for item in assessments):
            return GuardOutcome.DENY
        if any(item.outcome is GuardOutcome.ERROR for item in assessments):
            return (
                GuardOutcome.DENY
                if self.mode is SecurityMode.STRICT
                else GuardOutcome.REVIEW
            )
        if any(item.outcome is GuardOutcome.REVIEW for item in assessments):
            return GuardOutcome.REVIEW
        return GuardOutcome.ALLOW


__all__ = [
    "CompositeContentGuardProvider",
    "LOCAL_ONLY_CLASSIFICATIONS",
]
