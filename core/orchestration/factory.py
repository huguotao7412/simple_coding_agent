from __future__ import annotations

from pathlib import Path

from ..adapters.sqlite.checkpoint_probe import has_checkpoint
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
    return has_checkpoint(path, run_id)


__all__ = [
    "create_application_service",
    "has_langgraph_checkpoint",
    "langgraph_checkpoint_path",
]
