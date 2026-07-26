from __future__ import annotations

import sqlite3
from pathlib import Path

from ..planner import Planner
from .langgraph import LangGraphOrchestrator
from .protocol import ApplicationService
from ..paths import workspace_state_dir


def create_application_service(
    planner: Planner,
) -> ApplicationService:
    return LangGraphOrchestrator(planner)


def langgraph_checkpoint_path(workspace_dir: str | Path) -> Path:
    return workspace_state_dir(workspace_dir) / "langgraph-checkpoints.sqlite"


def has_langgraph_checkpoint(
    workspace_dir: str | Path,
    run_id: str,
) -> bool:
    path = langgraph_checkpoint_path(workspace_dir)
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(path) as connection:
            row = connection.execute(
                "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1",
                (run_id,),
            ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


__all__ = [
    "create_application_service",
    "has_langgraph_checkpoint",
    "langgraph_checkpoint_path",
]
