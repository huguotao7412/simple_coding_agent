from __future__ import annotations

import json
from pathlib import Path

from web.dashboard_data import load_dashboard_data, load_timeline


def test_load_timeline_groups_thought_tokens_and_tool_events(tmp_path: Path):
    trace_path = tmp_path / "run_trace.jsonl"
    _write_jsonl(trace_path, [
        {"type": "task_assessment", "content": "{\"strategy\":\"single_actor\"}", "elapsed_ms": 1},
        {"type": "thought", "token": "plan ", "elapsed_ms": 10},
        {"type": "thought", "token": "work", "elapsed_ms": 12},
        {"type": "tool_call", "tool_name": "read", "tool_args": {"path": "app.py"}, "elapsed_ms": 20},
        {
            "type": "tool_result",
            "tool_name": "read",
            "tool_result": {"success": True, "content": "ok", "error": None},
            "elapsed_ms": 30,
        },
        {"type": "done", "content": "finished", "elapsed_ms": 40},
    ])

    timeline = load_timeline(trace_path)

    assert [event.type for event in timeline] == [
        "task_assessment", "thought", "tool_call", "tool_result", "done",
    ]
    assert timeline[0].label == "Task assessment"
    assert timeline[1].content == "plan work"
    assert timeline[2].label == "Tool call: read"
    assert timeline[3].success is True


def test_load_dashboard_data_reads_report_and_patch_artifacts(tmp_path: Path):
    workspace = tmp_path / "runs" / "demo_task"
    trace_path = workspace / ".sca" / "traces" / "run_trace.jsonl"
    report_path = workspace / ".sca" / "final_report.md"
    patch_path = workspace / ".sca" / "artifacts" / "actor-diffs" / "task.patch"
    trace_path.parent.mkdir(parents=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.parent.mkdir(parents=True)
    _write_jsonl(trace_path, [{"type": "done", "content": "done", "elapsed_ms": 1}])
    report_path.write_text("# Report\n\nTests passed.\n", encoding="utf-8")
    patch_path.write_text("diff --git a/app.py b/app.py\n", encoding="utf-8")

    results_path = tmp_path / "eval_results.json"
    payload = {
        "summary": {
            "total": 1,
            "passed": 1,
            "failed": 0,
            "pass_rate": 1.0,
            "total_duration_ms": 123,
            "total_tool_calls": 2,
            "total_tokens": 15,
        },
        "tasks": [
            {
                "task_id": "demo_task",
                "title": "Demo Task",
                "model": "demo-model",
                "passed": True,
                "duration_ms": 123,
                "tool_calls": 2,
                "failed_tool_calls": 0,
                "total_tokens": 15,
                "trace_path": str(trace_path),
                "report_path": str(report_path),
                "failures": [],
                "final_output": "done",
            }
        ],
    }
    results_path.write_text(json.dumps(payload), encoding="utf-8")

    data = load_dashboard_data(results_path)

    assert data.summary.pass_rate == 1.0
    assert data.summary.failed_tool_calls == 0
    assert data.tasks[0].report_text.startswith("# Report")
    assert data.tasks[0].patch_artifacts == [patch_path]
    assert data.tasks[0].timeline[0].label == "Done"


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
