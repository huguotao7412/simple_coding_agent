from __future__ import annotations

import json

from cli.report import RunReport
from core.events import AgentEvent
from core.tools.base import ToolResult


def test_run_report_tracks_tools_files_tokens_and_errors():
    report = RunReport()

    report.observe(AgentEvent(
        type="tool_call",
        tool_name="read_file",
        tool_args={"path": "core/runtime.py"},
    ))
    report.observe(AgentEvent(
        type="tool_result",
        tool_name="read_file",
        tool_result=ToolResult.ok("content"),
    ))
    report.observe(AgentEvent(
        type="token_stats",
        content=json.dumps({"prompt_tokens": 10, "completion_tokens": 5}),
    ))
    report.observe(AgentEvent(type="error", content="something failed"))
    report.observe(AgentEvent(type="done", content="final"))

    assert len(report.tool_calls) == 1
    assert report.tool_calls[0].success is True
    assert report.files_referenced == {"core/runtime.py"}
    assert report.prompt_tokens == 10
    assert report.completion_tokens == 5
    assert report.total_tokens == 15
    assert report.errors == ["something failed"]
    assert report.final_output == "final"


def test_run_report_prefers_structured_token_stats():
    report = RunReport()

    report.observe(AgentEvent(
        type="token_stats",
        content=json.dumps({"prompt_tokens": 1, "completion_tokens": 1}),
        prompt_tokens=15,
        completion_tokens=7,
        usage_estimated=True,
    ))

    assert report.prompt_tokens == 15
    assert report.completion_tokens == 7
    assert report.total_tokens == 22


def test_run_report_falls_back_to_accumulated_model_usage():
    report = RunReport()

    report.observe(AgentEvent(
        type="model_usage",
        prompt_tokens=10,
        completion_tokens=2,
    ))
    report.observe(AgentEvent(
        type="model_usage",
        content=json.dumps({
            "prompt_tokens": 20,
            "completion_tokens": 3,
            "estimated": True,
        }),
    ))

    assert report.prompt_tokens == 30
    assert report.completion_tokens == 5
    assert report.total_tokens == 35
    assert report.usage_estimated is True


def test_terminal_token_stats_replace_model_usage_fallback():
    report = RunReport()
    report.observe(AgentEvent(
        type="model_usage",
        prompt_tokens=10,
        completion_tokens=2,
    ))

    report.observe(AgentEvent(
        type="token_stats",
        prompt_tokens=15,
        completion_tokens=4,
    ))

    assert report.prompt_tokens == 15
    assert report.completion_tokens == 4


def test_run_report_tracks_failed_tools_and_actor_statuses():
    report = RunReport()

    report.observe(AgentEvent(type="tool_call", tool_name="bash", tool_args={"command": "pytest"}))
    report.observe(AgentEvent(
        type="tool_result",
        tool_name="bash",
        tool_result=ToolResult.fail("tests failed"),
    ))
    report.observe(AgentEvent(
        type="actor_update",
        content=json.dumps({
            "task_tree": {
                "task_1": {"status": "done"},
                "task_2": {"status": "failed"},
                "task_3": {"status": "done"},
            }
        }),
    ))
    report.observe(AgentEvent(type="compaction", content="lightweight"))

    assert report.failed_tool_count == 1
    assert report.actor_status_counts == {"done": 2, "failed": 1}
    assert report.compactions == 1


def test_run_report_renders_and_writes_eval_friendly_markdown(
    tmp_path,
    monkeypatch,
):
    state_home = tmp_path / "state"
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("SCA_STATE_HOME", str(state_home))
    report = RunReport()
    report.observe(AgentEvent(
        type="tool_call",
        tool_name="run",
        tool_args={"command": "python -m pytest -q", "path": "tests/test_sample.py"},
    ))
    report.observe(AgentEvent(
        type="tool_result",
        tool_name="run",
        tool_result=ToolResult.ok("1 passed"),
    ))
    report.observe(AgentEvent(type="done", content="All set."))

    markdown = report.to_markdown()
    report_path = report.write_final_report(workspace, "run_test")

    assert "## Files" in markdown
    assert "## Tests" in markdown
    assert "## Risk" in markdown
    assert "python -m pytest -q" in markdown
    assert report_path.parent.parent == state_home / "workspaces"
    assert report_path.name == "final_report.md"
    assert (report_path.parent / "reports" / "run_test.md").is_file()
    assert not (workspace / ".sca").exists()
    assert report_path.read_text(encoding="utf-8") == markdown


def test_run_report_records_versioned_task_assessment():
    report = RunReport()
    report.observe(AgentEvent(
        type="task_assessment",
        content=json.dumps({
            "schema_version": 1,
            "intent": "code_change",
            "complexity": "small",
            "risk": "low",
            "strategy": "coder_with_gates",
            "reasons": ["repository defines deterministic quality gates"],
            "execution_hints": {
                "max_actors": 1,
                "requires_human_approval": False,
            },
        }),
    ))

    markdown = report.to_markdown()

    assert report.task_assessment is not None
    assert report.task_assessment["strategy"] == "coder_with_gates"
    assert "## Task Assessment" in markdown
    assert "Strategy: coder_with_gates" in markdown


def test_report_run_id_cannot_escape_state_directory(tmp_path, monkeypatch):
    state_home = tmp_path / "state"
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("SCA_STATE_HOME", str(state_home))

    report_path = RunReport().write_final_report(workspace, "../../outside")

    assert report_path.is_relative_to(state_home)
    assert not (tmp_path / "outside.md").exists()


def test_run_report_risk_section_respects_high_task_risk():
    report = RunReport(task_assessment={
        "schema_version": 1,
        "risk": "high",
        "strategy": "scout_then_dag",
        "execution_hints": {"max_actors": 4, "requires_human_approval": True},
    })

    markdown = report.to_markdown()

    assert "High: task assessment requires review or human approval." in markdown
    assert "Low: no runtime errors" not in markdown


def test_run_report_records_sandbox_backend_evidence():
    report = RunReport()
    report.observe(AgentEvent(
        type="sandbox_execution",
        content=json.dumps({"backend": "e2b", "isolated": True}),
    ))

    markdown = report.to_markdown()

    assert report.sandbox_backends == {"e2b"}
    assert report.isolated_execution_observed is True
    assert "## Command Sandbox" in markdown
    assert "Isolated execution observed: True" in markdown
