from __future__ import annotations

import re
import time

from .models import (
    ContentGuardAssessment,
    ContentGuardRequest,
    GuardOutcome,
    RiskLevel,
)


Rule = tuple[str, str, RiskLevel, GuardOutcome, re.Pattern[str]]


class LocalContentGuardProvider:
    name = "local"
    _RULES: tuple[Rule, ...] = (
        (
            "SCA-SECRET-REQUEST",
            "secret_access",
            RiskLevel.CRITICAL,
            GuardOutcome.DENY,
            re.compile(
                "(?i)(?:\\b(?:show|print|reveal|leak|"
                "exfiltrat\\w*)\\b|\u8bfb\u53d6|\u6cc4\u9732|"
                "\u663e\u793a).{0,30}(?:\\b(?:api.?key|token|"
                "password|private key|credential)\\b|\u5bc6\u94a5|"
                "\u51ed\u8bc1)"
            ),
        ),
        (
            "SCA-BOUNDARY-ESCAPE",
            "workspace_escape",
            RiskLevel.HIGH,
            GuardOutcome.DENY,
            re.compile(
                "(?i)(?:\\.\\.[\\\\/]|outside (?:the )?workspace|"
                "workspace \u5916|\u5de5\u4f5c\u533a\u5916|"
                "/etc/(?:passwd|shadow))"
            ),
        ),
        (
            "SCA-POLICY-BYPASS",
            "control_bypass",
            RiskLevel.CRITICAL,
            GuardOutcome.DENY,
            re.compile(
                "(?i)(?:\\b(?:bypass|disable|ignore)\\b|\u7ed5\u8fc7|"
                "\u5173\u95ed|\u8df3\u8fc7).{0,30}(?:\\b(?:policy|"
                "approval|sandbox|verification|audit)\\b|\u7b56\u7565|"
                "\u5ba1\u6279|\u6c99\u7bb1|\u9a8c\u8bc1|\u5ba1\u8ba1)"
            ),
        ),
        (
            "SCA-FORGERY",
            "evidence_forgery",
            RiskLevel.CRITICAL,
            GuardOutcome.DENY,
            re.compile(
                "(?i)(?:\\b(?:fake|forge|fabricate)\\b|\u4f2a\u9020|"
                "\u634f\u9020).{0,30}(?:\\b(?:task.?id|diff|"
                "verification|result)\\b|\u9a8c\u8bc1\u7ed3\u679c)"
            ),
        ),
        (
            "SCA-DESTRUCTIVE",
            "destructive_action",
            RiskLevel.CRITICAL,
            GuardOutcome.REVIEW,
            re.compile(
                "(?i)(?:rm\\s+-rf|git\\s+reset\\s+--hard|"
                "git\\s+clean\\s+-fd|drop\\s+(?:database|table)|"
                "delete\\s+from|\u5220\u9664\u6570\u636e\u5e93|"
                "\u751f\u4ea7\u90e8\u7f72|\u6743\u9650\u53d8\u66f4|"
                "chmod\\s+777)"
            ),
        ),
        (
            "SCA-INJECTION",
            "prompt_injection",
            RiskLevel.MEDIUM,
            GuardOutcome.REVIEW,
            re.compile(
                "(?i)(?:ignore (?:all |the )?(?:previous|prior) "
                "instructions|system message|developer message|"
                "\u5ffd\u7565.{0,12}(?:\u4e4b\u524d|\u4ee5\u4e0a)"
                ".{0,8}\u6307\u4ee4|"
                "\u4f60\u73b0\u5728\u662f\u7cfb\u7edf)"
            ),
        ),
        (
            "SCA-TOOL-INSTRUCTION",
            "untrusted_tool_instruction",
            RiskLevel.HIGH,
            GuardOutcome.REVIEW,
            re.compile(
                "(?i)(?:<system>|<developer>|SYSTEM\\s*:|"
                "DEVELOPER\\s*:).{0,80}(?:instruction|execute|run|"
                "\u6307\u4ee4|\u6267\u884c)"
            ),
        ),
        (
            "SCA-NETWORK-INTENT",
            "network_intent",
            RiskLevel.MEDIUM,
            GuardOutcome.REVIEW,
            re.compile(
                "(?i)(?:https?://|curl\\s|wget\\s|invoke-webrequest|"
                "\u8bbf\u95ee\u7f51\u7edc|\u8054\u7f51)"
            ),
        ),
        (
            "SCA-PII",
            "pii",
            RiskLevel.MEDIUM,
            GuardOutcome.REVIEW,
            re.compile(
                r"(?:\b\d{3}-\d{2}-\d{4}\b|"
                r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b)"
            ),
        ),
        (
            "SCA-SECRET-VALUE",
            "secret_value",
            RiskLevel.HIGH,
            GuardOutcome.DENY,
            re.compile(
                r"(?i)(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
                r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}|"
                r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,})"
            ),
        ),
    )

    async def inspect(
        self,
        request: ContentGuardRequest,
    ) -> ContentGuardAssessment:
        started = time.perf_counter()
        hits = [
            (rule_id, category, risk, outcome)
            for rule_id, category, risk, outcome, pattern in self._RULES
            if pattern.search(request.text)
        ]
        if not hits:
            return ContentGuardAssessment(
                provider=self.name,
                outcome=GuardOutcome.ALLOW,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        outcome = max((hit[3] for hit in hits), key=_outcome_rank)
        risk = max(hit[2] for hit in hits)
        return ContentGuardAssessment(
            provider=self.name,
            outcome=outcome,
            risk_level=risk,
            categories=tuple(dict.fromkeys(hit[1] for hit in hits)),
            rule_ids=tuple(dict.fromkeys(hit[0] for hit in hits)),
            reason="Local deterministic content policy detected elevated risk.",
            tripwire_triggered=outcome is GuardOutcome.DENY,
            latency_ms=(time.perf_counter() - started) * 1000,
        )


def _outcome_rank(outcome: GuardOutcome) -> int:
    return {
        GuardOutcome.ALLOW: 0,
        GuardOutcome.REVIEW: 1,
        GuardOutcome.ERROR: 2,
        GuardOutcome.DENY: 3,
    }[outcome]


__all__ = ["LocalContentGuardProvider"]
