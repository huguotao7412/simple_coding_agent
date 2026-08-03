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


class FakeProgressRun(FakeInteractiveRun):
    async def start_stream(self):
        yield AgentEvent(
            type="actor_status",
            content=json.dumps({"actor_id": "actor_1", "phase": "start"}),
        )
        yield AgentEvent(
            type="actor_status",
            content=json.dumps({
                "actor_id": "actor_1",
                "phase": "complete",
                "status": "done",
            }),
            route="done",
        )
        yield AgentEvent(
            type="graph_node_started",
            node_name="collect_actor_results",
        )
        yield AgentEvent(type="graph_node_started", node_name="verify")
        yield AgentEvent(
            type="graph_route_selected",
            node_name="repair_router",
            route="success",
        )
        yield AgentEvent(type="done", content="completed")


class FakeSession:
    def __init__(
        self,
        run: FakeInteractiveRun,
        expected_input: str = "dangerous task",
    ) -> None:
        self.run = run
        self.expected_input = expected_input
        self.completed: list[tuple[FakeInteractiveRun, str]] = []

    async def start(self, user_input: str):
        assert user_input == self.expected_input
        return self.run

    def complete(self, run, final_output: str) -> None:
        self.completed.append((run, final_output))


class FakeUI:
    def __init__(self, inputs=("dangerous task", "exit")) -> None:
        self.inputs = iter(inputs)
        self.approval_payloads: list[dict] = []
        self.markdown: list[str] = []
        self.infos: list[str] = []

    def render_user_prompt(self):
        return next(self.inputs)

    def render_approval_prompt(self, payload):
        self.approval_payloads.append(payload)
        return True

    def render_welcome(self):
        return None

    def render_info(self, message):
        self.infos.append(message)

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


@pytest.mark.asyncio
async def test_cli_bridge_renders_only_important_progress_nodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = FakeProgressRun(tmp_path)
    session = FakeSession(active, expected_input="create calculator")
    ui = FakeUI(inputs=("create calculator", "exit"))
    monkeypatch.setattr(
        "cli.report.RunReport.write_final_report",
        lambda self, workspace, run_id: tmp_path / "report.md",
    )

    await Bridge(session=session, ui=ui).run()

    assert "Actor actor_1 started implementation." in ui.infos
    assert "Actor actor_1 finished: done." in ui.infos
    assert "Applying Actor changes..." in ui.infos
    assert "Verifying the run result..." in ui.infos
    assert "Verification passed." in ui.infos
