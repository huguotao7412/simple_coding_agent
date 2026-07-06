from __future__ import annotations

import json

from cli.report import RunReport
from core.runtime import AgentEvent
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
