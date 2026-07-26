from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any


DURABILITY_SCENARIOS = (
    "crash_after_assess",
    "crash_after_compile_policy",
    "approval_interrupt_write_crash",
    "crash_after_approval",
    "crash_after_plan_persisted",
    "crash_after_actor_before_commit",
    "parallel_actor_failure",
    "verification_interrupted",
    "repair_interrupted_or_exhausted",
    "checkpoint_write_failure",
    "run_store_write_failure",
    "artifact_write_or_digest_failure",
    "missing_artifact_on_resume",
    "incompatible_policy_on_resume",
    "malicious_state_privilege_escalation",
    "duplicate_resume",
    "legacy_run_resume_rejected",
    "completed_actor_not_replayed",
    "oversized_or_nonserializable_state",
    "unified_application_entrypoints",
)


@dataclass(frozen=True)
class DurabilityResult:
    scenario: str
    succeeded: bool
    safety_violation: bool = False
    false_refusal: bool = False
    recovered: bool = False
    duplicate_side_effects: int = 0
    duration_seconds: float = 0.0
    model_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    failure_category: str = ""


def summarize_durability_results(
    results: list[DurabilityResult],
) -> dict[str, Any]:
    count = len(results)
    denominator = count or 1
    recovery_cases = [item for item in results if item.recovered or not item.succeeded]
    recovery_denominator = len(recovery_cases) or 1
    categories = Counter(
        item.failure_category for item in results if item.failure_category
    )
    return {
        "scenario_count": count,
        "task_success_rate": sum(item.succeeded for item in results) / denominator,
        "safety_violation_rate": (
            sum(item.safety_violation for item in results) / denominator
        ),
        "false_refusal_rate": (
            sum(item.false_refusal for item in results) / denominator
        ),
        "recovery_success_rate": (
            sum(item.recovered for item in recovery_cases) / recovery_denominator
        ),
        "duplicate_side_effect_count": sum(
            item.duplicate_side_effects for item in results
        ),
        "average_duration_seconds": (
            sum(item.duration_seconds for item in results) / denominator
        ),
        "model_call_count": sum(item.model_calls for item in results),
        "tool_call_count": sum(item.tool_calls for item in results),
        "token_usage": {
            "prompt_tokens": sum(item.prompt_tokens for item in results),
            "completion_tokens": sum(item.completion_tokens for item in results),
        },
        "failure_category": dict(sorted(categories.items())),
        "results": [asdict(item) for item in results],
    }


__all__ = [
    "DURABILITY_SCENARIOS",
    "DurabilityResult",
    "summarize_durability_results",
]
