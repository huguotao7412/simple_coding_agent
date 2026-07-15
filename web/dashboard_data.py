from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TIMELINE_EVENT_TYPES = {
    "task_assessment",
    "sandbox_execution",
    "thought",
    "tool_call",
    "tool_result",
    "actor_update",
    "compaction",
    "token_stats",
    "error",
    "done",
    "runner_error",
}


@dataclass
class DashboardSummary:
    total: int = 0
    passed: int = 0
    failed: int = 0
    pass_rate: float = 0.0
    total_duration_ms: int = 0
    total_tool_calls: int = 0
    failed_tool_calls: int = 0
    total_tokens: int = 0
    strategy_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class TimelineEvent:
    type: str
    label: str
    content: str = ""
    elapsed_ms: int | None = None
    success: bool | None = None


@dataclass
class DashboardTask:
    task_id: str
    title: str
    model: str | None
    passed: bool
    duration_ms: int
    tool_calls: int
    failed_tool_calls: int
    total_tokens: int
    trace_path: Path
    report_path: Path
    task_strategy: str | None = None
    failures: list[str] = field(default_factory=list)
    final_output: str = ""
    timeline: list[TimelineEvent] = field(default_factory=list)
    report_text: str = ""
    patch_artifacts: list[Path] = field(default_factory=list)


@dataclass
class DashboardData:
    results_path: Path
    summary: DashboardSummary
    tasks: list[DashboardTask]


def load_dashboard_data(results_path: str | Path) -> DashboardData:
    """Load eval result JSON plus linked trace/report/artifact files."""
    path = Path(results_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    tasks = [
        _load_task(task_payload, base_dir=path.parent)
        for task_payload in payload.get("tasks", [])
    ]
    return DashboardData(
        results_path=path,
        summary=_load_summary(payload.get("summary", {}), tasks),
        tasks=tasks,
    )


def _load_summary(raw: dict[str, Any], tasks: list[DashboardTask]) -> DashboardSummary:
    failed_tool_calls = sum(task.failed_tool_calls for task in tasks)
    return DashboardSummary(
        total=int(raw.get("total", len(tasks)) or 0),
        passed=int(raw.get("passed", sum(task.passed for task in tasks)) or 0),
        failed=int(raw.get("failed", sum(not task.passed for task in tasks)) or 0),
        pass_rate=float(raw.get("pass_rate", 0.0) or 0.0),
        total_duration_ms=int(raw.get("total_duration_ms", 0) or 0),
        total_tool_calls=int(raw.get("total_tool_calls", 0) or 0),
        failed_tool_calls=failed_tool_calls,
        total_tokens=int(raw.get("total_tokens", 0) or 0),
        strategy_counts={
            str(key): int(value)
            for key, value in (raw.get("strategy_counts", {}) or {}).items()
        },
    )


def _load_task(raw: dict[str, Any], base_dir: Path) -> DashboardTask:
    trace_path = _resolve_path(raw.get("trace_path", ""), base_dir)
    report_path = _resolve_path(raw.get("report_path", ""), base_dir)
    workspace_dir = _workspace_from_report(report_path)
    return DashboardTask(
        task_id=str(raw.get("task_id", "")),
        title=str(raw.get("title", raw.get("task_id", ""))),
        model=raw.get("model"),
        passed=bool(raw.get("passed")),
        duration_ms=int(raw.get("duration_ms", 0) or 0),
        tool_calls=int(raw.get("tool_calls", 0) or 0),
        failed_tool_calls=int(raw.get("failed_tool_calls", 0) or 0),
        total_tokens=int(raw.get("total_tokens", 0) or 0),
        trace_path=trace_path,
        report_path=report_path,
        task_strategy=(str(raw["task_strategy"]) if raw.get("task_strategy") else None),
        failures=list(raw.get("failures", [])),
        final_output=str(raw.get("final_output", "") or ""),
        timeline=load_timeline(trace_path),
        report_text=_read_text_if_exists(report_path),
        patch_artifacts=_find_patch_artifacts(workspace_dir),
    )


def load_timeline(trace_path: Path) -> list[TimelineEvent]:
    if not trace_path.is_file():
        return []

    events: list[TimelineEvent] = []
    thought_buffer: list[str] = []
    thought_elapsed: int | None = None

    for line in trace_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = str(record.get("type", ""))
        if event_type not in TIMELINE_EVENT_TYPES:
            continue

        if event_type == "thought":
            thought_buffer.append(str(record.get("token") or record.get("content") or ""))
            thought_elapsed = _maybe_int(record.get("elapsed_ms"))
            continue

        if thought_buffer:
            events.append(TimelineEvent(
                type="thought",
                label="Model thought",
                content="".join(thought_buffer).strip(),
                elapsed_ms=thought_elapsed,
            ))
            thought_buffer = []
            thought_elapsed = None

        events.append(_timeline_event_from_record(record))

    if thought_buffer:
        events.append(TimelineEvent(
            type="thought",
            label="Model thought",
            content="".join(thought_buffer).strip(),
            elapsed_ms=thought_elapsed,
        ))

    return events


def _timeline_event_from_record(record: dict[str, Any]) -> TimelineEvent:
    event_type = str(record.get("type", ""))
    tool_result = record.get("tool_result") or {}
    success = tool_result.get("success")
    if event_type == "task_assessment":
        label = "Task assessment"
        content = str(record.get("content") or "")
    elif event_type == "tool_call":
        label = f"Tool call: {record.get('tool_name') or 'unknown'}"
        content = _compact_json(record.get("tool_args") or {})
    elif event_type == "tool_result":
        label = f"Tool result: {record.get('tool_name') or 'unknown'}"
        content = str(tool_result.get("content") or tool_result.get("error") or "")
    elif event_type == "done":
        label = "Done"
        content = str(record.get("content") or "")
    elif event_type == "error":
        label = "Error"
        content = str(record.get("content") or "")
    elif event_type == "token_stats":
        label = "Token usage"
        content = str(record.get("content") or "")
    else:
        label = event_type.replace("_", " ").title()
        content = str(record.get("content") or "")
    return TimelineEvent(
        type=event_type,
        label=label,
        content=content,
        elapsed_ms=_maybe_int(record.get("elapsed_ms")),
        success=success if isinstance(success, bool) else None,
    )


def _resolve_path(raw_path: str, base_dir: Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return base_dir / path


def _workspace_from_report(report_path: Path) -> Path:
    if report_path.name == "final_report.md" and report_path.parent.name == ".sca":
        return report_path.parent.parent
    return report_path.parent


def _find_patch_artifacts(workspace_dir: Path) -> list[Path]:
    artifact_dir = workspace_dir / ".sca" / "artifacts" / "actor-diffs"
    if not artifact_dir.is_dir():
        return []
    return sorted(path for path in artifact_dir.glob("*.patch") if path.is_file())


def _read_text_if_exists(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _maybe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
