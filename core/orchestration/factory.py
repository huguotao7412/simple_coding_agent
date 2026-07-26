from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Literal, cast

from ..planner import Planner
from .langgraph import LangGraphOrchestrator
from .legacy import LegacyOrchestrator
from .protocol import Orchestrator
from ..paths import workspace_state_dir


OrchestratorName = Literal["legacy", "langgraph"]


def resolve_orchestrator_name(value: str | None = None) -> OrchestratorName:
    name = (value or os.getenv("SCA_ORCHESTRATOR") or "langgraph").strip().lower()
    if name not in {"legacy", "langgraph"}:
        raise ValueError("SCA_ORCHESTRATOR must be 'legacy' or 'langgraph'")
    return cast(OrchestratorName, name)


def create_orchestrator(
    planner: Planner,
    *,
    name: str | None = None,
) -> Orchestrator:
    resolved = resolve_orchestrator_name(name)
    if resolved == "langgraph":
        return LangGraphOrchestrator(planner)
    return LegacyOrchestrator(planner)


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
    "OrchestratorName",
    "create_orchestrator",
    "has_langgraph_checkpoint",
    "langgraph_checkpoint_path",
    "resolve_orchestrator_name",
]
