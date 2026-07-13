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


def test_run_report_renders_and_writes_eval_friendly_markdown(tmp_path):
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
    report_path = report.write_final_report(tmp_path)

    assert "## Files" in markdown
    assert "## Tests" in markdown
    assert "## Risk" in markdown
    assert "python -m pytest -q" in markdown
    assert report_path == tmp_path / ".sca" / "final_report.md"
    assert report_path.read_text(encoding="utf-8") == markdown
