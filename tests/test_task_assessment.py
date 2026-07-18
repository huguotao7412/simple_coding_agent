from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.execution.assessment import TaskAssessor
from core.execution.models import (
    ExecutionStrategy,
    TaskComplexity,
    TaskIntent,
    TaskRisk,
)
from core.planner import Planner
from core.runtime.conversation import ContextManager


class CaptureLLM:
    max_tokens = 128000

    def __init__(self) -> None:
        self.messages: list[dict] = []

    def count_messages_tokens(self, messages):
        return 1

    async def chat(self, messages, tools=None, on_token=None):
        self.messages = list(messages)
        return {"role": "assistant", "content": "done"}


def test_workspace_profile_is_bounded_and_ignores_generated_directories(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text("def test_app(): pass\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text("ignored", encoding="utf-8")
    (tmp_path / ".sca").mkdir()
    (tmp_path / ".sca" / "quality-gates.toml").write_text("gates = []\n", encoding="utf-8")

    profile = TaskAssessor(tmp_path).profile_workspace()

    assert profile.file_count == 2
    assert profile.source_file_count == 2
    assert profile.test_file_count == 1
    assert profile.languages == ("Python",)
    assert profile.has_quality_gates is True
    assert profile.scan_truncated is False


def test_small_targeted_change_prefers_deterministic_gates(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".sca").mkdir()
    (tmp_path / ".sca" / "quality-gates.toml").write_text("gates = []\n", encoding="utf-8")

    assessment = TaskAssessor(tmp_path).assess("Fix the bug in app.py")

    assert assessment.intent is TaskIntent.CODE_CHANGE
    assert assessment.complexity is TaskComplexity.SMALL
    assert assessment.risk is TaskRisk.LOW
    assert assessment.strategy is ExecutionStrategy.CODER_WITH_GATES
    assert assessment.explicit_paths == ("app.py",)
    assert assessment.max_actors == 1
    assert assessment.verifier_recommended is False


def test_fixing_pytest_is_a_code_change_unless_tests_are_explicit_target(tmp_path: Path):
    assessor = TaskAssessor(tmp_path)

    fix = assessor.assess("Fix the failing pytest without changing the test")
    add = assessor.assess("Add unit tests for the formatter")

    assert fix.intent is TaskIntent.CODE_CHANGE
    assert add.intent is TaskIntent.TEST_CHANGE


def test_ascii_risk_terms_use_word_boundaries(tmp_path: Path):
    assessment = TaskAssessor(tmp_path).assess("Fix capitalization in app.py")

    assert assessment.risk is TaskRisk.LOW


def test_broad_architecture_change_uses_scout_led_dag(tmp_path: Path):
    assessment = TaskAssessor(tmp_path).assess(
        "Rewrite the architecture across multiple modules and implement the migration"
    )

    assert assessment.complexity is TaskComplexity.LARGE
    assert assessment.strategy is ExecutionStrategy.SCOUT_THEN_DAG
    assert assessment.max_actors == 4


def test_bug_report_traceback_paths_do_not_force_large_scout_dag(tmp_path: Path):
    (tmp_path / "litellm" / "llms" / "vertex_ai" / "gemini").mkdir(parents=True)
    (tmp_path / "tests" / "llm_translation").mkdir(parents=True)
    (tmp_path / "litellm" / "llms" / "vertex_ai" / "gemini" / "vertex_and_google_ai_studio_gemini.py").write_text(
        "def completion(): pass\n",
        encoding="utf-8",
    )
    prompt = """
    Implement the necessary code changes in the current repository.

    [Bug]: Connection to gemini-2.5-flash-image-preview failed
    <img src="https://github.com/user-attachments/assets/e62b98e7-604c" />
    File "/usr/lib/python3.13/site-packages/litellm/llms/vertex_ai/gemini/vertex_and_google_ai_studio_gemini.py", line 1697
    File "/usr/lib/python3.13/site-packages/httpx/_models.py", line 829
    Why does this happen?
    """

    assessment = TaskAssessor(tmp_path).assess(prompt)

    assert assessment.intent is TaskIntent.CODE_CHANGE
    assert assessment.complexity is not TaskComplexity.LARGE
    assert assessment.strategy is ExecutionStrategy.SINGLE_ACTOR
    assert assessment.explicit_paths == ()


def test_high_impact_operations_require_human_approval(tmp_path: Path):
    assessment = TaskAssessor(tmp_path).assess(
        "Deploy the database migration to production"
    )

    assert assessment.intent is TaskIntent.OPERATIONS
    assert assessment.risk is TaskRisk.HIGH
    assert assessment.requires_human_approval is True
    assert assessment.verifier_recommended is True


def test_read_only_request_stays_in_planner(tmp_path: Path):
    assessment = TaskAssessor(tmp_path).assess("Explain why app.py uses this pattern")

    assert assessment.intent is TaskIntent.READ_ONLY
    assert assessment.strategy is ExecutionStrategy.PLANNER_DIRECT


@pytest.mark.parametrize("prompt", ["hello", "Hi!", "\u4f60\u597d", "\u5728\u5417\uff1f"])
def test_small_talk_stays_in_planner(tmp_path: Path, prompt: str):
    (tmp_path / ".sca").mkdir()
    (tmp_path / ".sca" / "quality-gates.toml").write_text("gates = []\n", encoding="utf-8")

    assessment = TaskAssessor(tmp_path).assess(prompt)

    assert assessment.intent is TaskIntent.READ_ONLY
    assert assessment.strategy is ExecutionStrategy.PLANNER_DIRECT


def test_read_only_production_question_does_not_request_approval(tmp_path: Path):
    assessment = TaskAssessor(tmp_path).assess(
        "Explain the current production deployment configuration"
    )

    assert assessment.intent is TaskIntent.READ_ONLY
    assert assessment.risk is TaskRisk.LOW
    assert assessment.strategy is ExecutionStrategy.PLANNER_DIRECT
    assert assessment.requires_human_approval is False


@pytest.mark.asyncio
async def test_planner_emits_and_injects_durable_task_assessment(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    llm = CaptureLLM()
    context = ContextManager(system_prompt="system")
    planner = Planner(
        llm_client=llm,
        context_manager=context,
        tools=[],
        workspace_dir=str(tmp_path),
    )

    events = [event async for event in planner.run_stream("Fix app.py")]

    assert events[0].type == "task_assessment"
    payload = json.loads(events[0].content)
    assert payload["schema_version"] == 1
    assert payload["strategy"] == "single_actor"
    assessment_messages = [
        message for message in context.messages
        if message.get("role") == "system"
        and str(message.get("content", "")).startswith("<task_assessment>")
    ]
    assert len(assessment_messages) == 1
    assert assessment_messages[0] in llm.messages
    assert context.messages.index(assessment_messages[0]) < len(context.messages) - 1


@pytest.mark.asyncio
async def test_planner_reemits_checkpointed_assessment_on_resume(tmp_path: Path):
    first_llm = CaptureLLM()
    context = ContextManager(system_prompt="system")
    first = Planner(first_llm, context, [], str(tmp_path))
    _ = [event async for event in first.run_stream("Explain the project")]

    resumed = Planner(CaptureLLM(), context, [], str(tmp_path))
    events = [event async for event in resumed.run_stream("", resume=True)]

    assert events[0].type == "task_assessment"
    assert json.loads(events[0].content)["strategy"] == "planner_direct"
