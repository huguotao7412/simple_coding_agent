from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from cli.runs import create_durable_run_context
from core.execution.assessment import TaskAssessor
from core.execution.models import TaskRisk
from core.orchestration.interactive import InteractiveOrchestrationSession
from core.planner import Planner
from core.runtime.conversation import ContextManager


class RecordingLLM:
    max_tokens = 128_000

    def __init__(self) -> None:
        self.calls = 0
        self.observed_messages: list[list[dict]] = []

    def count_messages_tokens(self, messages):
        return 1

    async def chat(self, messages, tools=None, on_token=None):
        self.calls += 1
        self.observed_messages.append(list(messages))
        return {
            "role": "assistant",
            "content": f"answer-{self.calls}",
            "_usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "estimated": False,
            },
        }


class ApprovalAssessor(TaskAssessor):
    def assess(self, prompt: str):
        assessment = super().assess("Explain this project")
        return replace(
            assessment,
            risk=TaskRisk.HIGH,
            reasons=("interactive approval test",),
            requires_human_approval=True,
        )


def _session(
    tmp_path: Path,
    llm: RecordingLLM,
    *,
    approval: bool = False,
) -> InteractiveOrchestrationSession:
    async def context_factory(messages):
        return await create_durable_run_context(
            workspace_dir=str(tmp_path),
            model="fake",
            messages=messages,
        )

    def planner_factory(context: ContextManager, run_context):
        return Planner(
            llm_client=llm,
            context_manager=context,
            tools=[],
            workspace_dir=str(tmp_path),
            run_context=run_context,
            task_assessor=(
                ApprovalAssessor(tmp_path)
                if approval
                else TaskAssessor(tmp_path)
            ),
        )

    return InteractiveOrchestrationSession(
        system_prompt="test-system",
        context_factory=context_factory,
        planner_factory=planner_factory,
    )


async def _collect(stream):
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_interactive_turns_use_distinct_graph_threads_and_bounded_history(
    tmp_path: Path,
) -> None:
    llm = RecordingLLM()
    session = _session(tmp_path, llm)

    first = await session.start("Explain the first topic")
    first_events = await _collect(first.start_stream())
    first_output = next(
        event.content for event in first_events if event.type == "done"
    )
    session.complete(first, first_output)

    second = await session.start("Explain the second topic")
    second_events = await _collect(second.start_stream())
    second_output = next(
        event.content for event in second_events if event.type == "done"
    )
    session.complete(second, second_output)

    assert first.planner.run_context.run_id != second.planner.run_context.run_id
    assert llm.calls == 2
    assert any(
        message.get("content") == first_output
        for message in llm.observed_messages[1]
    )
    assert session.history == (
        {"role": "user", "content": "Explain the first topic"},
        {"role": "assistant", "content": first_output},
        {"role": "user", "content": "Explain the second topic"},
        {"role": "assistant", "content": second_output},
    )


@pytest.mark.asyncio
async def test_interactive_high_risk_interrupt_resumes_same_thread(
    tmp_path: Path,
) -> None:
    llm = RecordingLLM()
    session = _session(tmp_path, llm, approval=True)
    active = await session.start("reviewed high-risk operation")
    run_id = active.planner.run_context.run_id

    paused = await _collect(active.start_stream())

    assert llm.calls == 0
    assert any(event.type == "graph_interrupted" for event in paused)

    resumed = await _collect(active.resume_stream(True))

    assert active.planner.run_context.run_id == run_id
    assert llm.calls == 1
    assert any(event.type == "graph_resumed" for event in resumed)
    assert resumed[-1].type == "done"
