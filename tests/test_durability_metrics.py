from evals.durability import DurabilityResult, summarize_durability_results


def test_durability_metrics_are_calculated_from_observations() -> None:
    summary = summarize_durability_results([
        DurabilityResult(
            scenario="approval_resume",
            succeeded=True,
            recovered=True,
            duration_seconds=2,
            model_calls=1,
            tool_calls=2,
            prompt_tokens=3,
            completion_tokens=4,
        ),
        DurabilityResult(
            scenario="checkpoint_failure",
            succeeded=False,
            duration_seconds=4,
            failure_category="persistence",
        ),
    ])

    assert summary["task_success_rate"] == 0.5
    assert summary["recovery_success_rate"] == 0.5
    assert summary["average_duration_seconds"] == 3
    assert summary["duplicate_side_effect_count"] == 0
    assert summary["model_call_count"] == 1
    assert summary["tool_call_count"] == 2
    assert summary["token_usage"] == {
        "prompt_tokens": 3,
        "completion_tokens": 4,
    }
    assert summary["failure_category"] == {"persistence": 1}
