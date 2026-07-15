from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from .paths import (
    WorkspaceStateMetadata,
    read_workspace_metadata,
    safe_state_component,
    user_state_dir,
    workspace_state_dir,
    write_workspace_metadata,
)
from .runs.models import RunStatus
from .runs.sqlite_store import SQLiteRunStore


DEFAULT_RETENTION_DAYS = 30
DEFAULT_RETAIN_RUNS = 50
DEFAULT_ARTIFACT_MAX_BYTES = 1024 * 1024 * 1024


@dataclass(frozen=True)
class GCAction:
    action: str
    target: str
    reason: str
    bytes_reclaimed: int = 0


@dataclass
class GCReport:
    dry_run: bool
    actions: list[GCAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def bytes_reclaimed(self) -> int:
        return sum(action.bytes_reclaimed for action in self.actions)


def delete_run_artifacts(workspace_dir: str | Path, run_id: str) -> None:
    """Delete user-state files owned by one run and refresh the latest report."""
    state_dir = workspace_state_dir(workspace_dir)
    safe_run_id = safe_state_component(run_id, fallback="run")
    report_path = state_dir / "reports" / f"{safe_run_id}.md"
    artifact_path = state_dir / "artifacts" / "runs" / safe_run_id
    report_path.unlink(missing_ok=True)
    if artifact_path.is_dir():
        shutil.rmtree(artifact_path)
    _refresh_latest_report(state_dir)


def _refresh_latest_report(state_dir: Path) -> None:
    latest_path = state_dir / "final_report.md"
    reports_dir = state_dir / "reports"
    reports = (
        sorted(
            reports_dir.glob("*.md"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if reports_dir.is_dir()
        else []
    )
    if reports:
        latest_path.write_text(reports[0].read_text(encoding="utf-8"), encoding="utf-8")
    else:
        latest_path.unlink(missing_ok=True)


def _directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        try:
            total += item.stat().st_size
        except OSError:
            continue
    return total


def _workspace_roots(state_root: Path) -> list[Path]:
    workspaces = state_root / "workspaces"
    if not workspaces.is_dir():
        return []
    return sorted(path for path in workspaces.iterdir() if path.is_dir())


async def garbage_collect(
    *,
    dry_run: bool = False,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    retain_runs: int = DEFAULT_RETAIN_RUNS,
    artifact_max_bytes: int = DEFAULT_ARTIFACT_MAX_BYTES,
    now: float | None = None,
    state_root: str | Path | None = None,
) -> GCReport:
    if retention_days < 0 or retain_runs < 0 or artifact_max_bytes < 0:
        raise ValueError("GC limits must not be negative")
    timestamp = time.time() if now is None else now
    cutoff = timestamp - retention_days * 86400
    root = Path(state_root) if state_root is not None else user_state_dir()
    report = GCReport(dry_run=dry_run)
    retained_state_dirs: list[Path] = []
    planned_run_artifact_roots: set[Path] = set()

    for state_dir in _workspace_roots(root):
        try:
            metadata = read_workspace_metadata(state_dir)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            report.warnings.append(f"Skipping invalid state {state_dir}: {error}")
            continue
        if metadata is None:
            report.warnings.append(f"Skipping state without workspace.json: {state_dir}")
            continue

        workspace_exists = Path(metadata.workspace_path).exists()
        if not workspace_exists and metadata.orphaned_at is None:
            report.actions.append(GCAction(
                action="mark_orphan",
                target=str(state_dir),
                reason=f"workspace no longer exists: {metadata.workspace_path}",
            ))
            if not dry_run:
                write_workspace_metadata(
                    state_dir,
                    replace(metadata, orphaned_at=timestamp),
                )
            retained_state_dirs.append(state_dir)
            continue
        if (
            not workspace_exists
            and metadata.orphaned_at is not None
            and metadata.orphaned_at <= cutoff
        ):
            size = _directory_size(state_dir)
            report.actions.append(GCAction(
                action="delete_orphan",
                target=str(state_dir),
                reason=f"orphaned for at least {retention_days} day(s)",
                bytes_reclaimed=size,
            ))
            if not dry_run:
                shutil.rmtree(state_dir)
            continue

        retained_state_dirs.append(state_dir)
        deleted_run_ids: set[str] = set()
        database_path = state_dir / "runs.db"
        store = SQLiteRunStore(database_path) if database_path.is_file() else None
        if store is not None:
            await store.initialize()
            runs = await store.list_runs(limit=1_000_000)
        else:
            runs = []
        protected_ids = {run.run_id for run in runs[:retain_runs]}
        for run in runs:
            if run.run_id in protected_ids or run.updated_at >= cutoff:
                continue
            if run.status in {RunStatus.CREATED, RunStatus.RUNNING, RunStatus.PAUSED}:
                continue
            run_bytes = _directory_size(
                state_dir
                / "artifacts"
                / "runs"
                / safe_state_component(run.run_id, fallback="run")
            )
            planned_run_artifact_roots.add(
                state_dir
                / "artifacts"
                / "runs"
                / safe_state_component(run.run_id, fallback="run")
            )
            report_path = (
                state_dir
                / "reports"
                / f"{safe_state_component(run.run_id, fallback='run')}.md"
            )
            if report_path.is_file():
                run_bytes += report_path.stat().st_size
            report.actions.append(GCAction(
                action="delete_run",
                target=run.run_id,
                reason=(
                    f"older than {retention_days} day(s) and outside "
                    f"the newest {retain_runs} run(s)"
                ),
                bytes_reclaimed=run_bytes,
            ))
            if not dry_run:
                assert store is not None
                await store.delete_run(run.run_id)
                delete_run_artifacts(metadata.workspace_path, run.run_id)
            deleted_run_ids.add(run.run_id)

        durable_ids = {run.run_id for run in runs}
        history_dir = state_dir / "reports"
        history = (
            sorted(
                history_dir.glob("*.md"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            if history_dir.is_dir()
            else []
        )
        protected_history = {path.stem for path in history[:retain_runs]}
        for historical_report in history:
            run_id = historical_report.stem
            if (
                run_id in protected_history
                or historical_report.stat().st_mtime >= cutoff
                or run_id in durable_ids
                or run_id in deleted_run_ids
            ):
                continue
            artifact_root = state_dir / "artifacts" / "runs" / run_id
            size = historical_report.stat().st_size + _directory_size(artifact_root)
            planned_run_artifact_roots.add(artifact_root)
            report.actions.append(GCAction(
                action="delete_run_history",
                target=run_id,
                reason=(
                    f"older than {retention_days} day(s) and outside "
                    f"the newest {retain_runs} report(s)"
                ),
                bytes_reclaimed=size,
            ))
            if not dry_run:
                historical_report.unlink(missing_ok=True)
                if artifact_root.is_dir():
                    shutil.rmtree(artifact_root)
                _refresh_latest_report(state_dir)

    artifact_files: list[tuple[float, Path, int]] = []
    total_artifact_bytes = 0
    for state_dir in retained_state_dirs:
        artifacts = state_dir / "artifacts"
        if not artifacts.is_dir():
            continue
        for artifact in artifacts.rglob("*"):
            if not artifact.is_file():
                continue
            if any(
                artifact.is_relative_to(planned_root)
                for planned_root in planned_run_artifact_roots
            ):
                continue
            try:
                stat = artifact.stat()
            except OSError:
                continue
            artifact_files.append((stat.st_mtime, artifact, stat.st_size))
            total_artifact_bytes += stat.st_size

    for _, artifact, size in sorted(artifact_files):
        if total_artifact_bytes <= artifact_max_bytes:
            break
        report.actions.append(GCAction(
            action="delete_artifact",
            target=str(artifact),
            reason=f"artifact storage exceeds {artifact_max_bytes} bytes",
            bytes_reclaimed=size,
        ))
        total_artifact_bytes -= size
        if not dry_run:
            artifact.unlink(missing_ok=True)

    return report


__all__ = [
    "DEFAULT_ARTIFACT_MAX_BYTES",
    "DEFAULT_RETAIN_RUNS",
    "DEFAULT_RETENTION_DAYS",
    "GCAction",
    "GCReport",
    "delete_run_artifacts",
    "garbage_collect",
]
