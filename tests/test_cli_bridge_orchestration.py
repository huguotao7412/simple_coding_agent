from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.bridge import Bridge
from core.events import AgentEvent


class FakeInteractiveRun:
    def __init__(self, workspace: Path) -> None:
        self.planner = SimpleNamespace(
            workspace_dir=str(workspace),
            run_context=SimpleNamespace(run_id="run_interactive"),
        )
        self.interrupted = False
        self.resume_decisions: list[bool] = []

    async def start_stream(self):
        yield AgentEvent(
            type="graph_interrupted",
            content=json.dumps({
                "risk_level": "high",
                "risk_reasons": ["test"],
                "requested_capabilities": ["actor:coder"],
                "target_scope": ["app.py"],
            }),
        )

    async def resume_stream(self, approved: bool):
        self.resume_decisions.append(approved)
        yield AgentEvent(type="done", content="approved result")


class FakeSession:
    def __init__(self, run: FakeInteractiveRun) -> None:
        self.run = run
        self.completed: list[tuple[FakeInteractiveRun, str]] = []

    async def start(self, user_input: str):
        assert user_input == "dangerous task"
        return self.run

    def complete(self, run, final_output: str) -> None:
        self.completed.append((run, final_output))


class FakeUI:
    def __init__(self) -> None:
        self.inputs = iter(("dangerous task", "exit"))
        self.approval_payloads: list[dict] = []
        self.markdown: list[str] = []

    def render_user_prompt(self):
        return next(self.inputs)

    def render_approval_prompt(self, payload):
        self.approval_payloads.append(payload)
        return True

    def render_welcome(self):
        return None

    def render_info(self, message):
        return None

    def render_markdown(self, message):
        self.markdown.append(message)

    def render_run_report(self, report):
        return None

    def clear_tool_status(self):
        return None

    def clear_actor_status(self):
        return None


@pytest.mark.asyncio
async def test_cli_bridge_resumes_langgraph_interrupt_with_terminal_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = FakeInteractiveRun(tmp_path)
    session = FakeSession(active)
    ui = FakeUI()
    monkeypatch.setattr(
        "cli.report.RunReport.write_final_report",
        lambda self, workspace, run_id: tmp_path / "report.md",
    )

    await Bridge(session=session, ui=ui).run()

    assert active.resume_decisions == [True]
    assert ui.approval_payloads[0]["risk_level"] == "high"
    assert ui.markdown == ["approved result"]
    assert session.completed == [(active, "approved result")]
