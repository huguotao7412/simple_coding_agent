from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from core.a2a_lite.models import (
    A2A_LITE_SCHEMA_VERSION,
    AgentHandoff,
    AgentMessage,
    ArtifactRef,
)


def test_versioned_handoff_message_round_trips() -> None:
    artifact = ArtifactRef.create(
        kind="patch",
        uri=".sca/artifacts/actor-diffs/task.patch",
        media_type="text/x-diff",
        producer_task_id="task_1",
        content="diff payload",
        description="Actor patch",
    )
    handoff = AgentHandoff(
        findings=("The parser accepts malformed input.",),
        decisions=("Validate before dispatch.",),
        constraints=("Keep the public CLI stable.",),
        unresolved_questions=("Should empty input be valid?",),
        artifacts=(artifact,),
    )
    message = AgentMessage.handoff_message(
        run_id="run_1",
        task_id="task_1",
        sender_id="actor:task_1",
        recipient_id="planner",
        handoff=handoff,
    )

    restored = AgentMessage.from_dict(message.to_dict())

    assert restored == message
    assert restored.schema_version == A2A_LITE_SCHEMA_VERSION
    assert restored.handoff.artifacts[0].sha256
    assert restored.to_json() == message.to_json()


def test_message_contract_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValueError, match="schema version"):
        AgentMessage.from_dict({
            "schema_version": "a2a-lite/99.0",
            "kind": "task.completed",
            "handoff": {},
        })


def test_a2a_lite_values_are_immutable() -> None:
    handoff = AgentHandoff(findings=("stable",))

    with pytest.raises(FrozenInstanceError):
        handoff.findings = ("mutated",)  # type: ignore[misc]


def test_prompt_message_serialization_is_bounded() -> None:
    message = AgentMessage.handoff_message(
        run_id="run_1",
        task_id="task_1",
        sender_id="actor:task_1",
        recipient_id="planner",
        handoff=AgentHandoff(findings=("x" * 100, "second")),
    )

    prompt_json = message.to_prompt_json(max_items=1, max_text_chars=10)

    assert "x" * 10 in prompt_json
    assert "x" * 11 not in prompt_json
    assert "second" not in prompt_json
