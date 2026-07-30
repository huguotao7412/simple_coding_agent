from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VerificationDecision:
    passed: bool
    repairable: bool
    failure_category: str = ""


def decide_verification(*, failed: int, blocked: int) -> VerificationDecision:
    passed = failed == 0 and blocked == 0
    return VerificationDecision(
        passed=passed,
        repairable=False,
        failure_category="" if passed else "actor_failure",
    )


__all__ = ["VerificationDecision", "decide_verification"]
