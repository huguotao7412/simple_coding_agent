from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path


_metadata_lock = threading.RLock()


def user_state_dir(
    env: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
    platform: str | None = None,
) -> Path:
    """Return the per-user runtime state directory without creating it."""
    values = os.environ if env is None else env
    override = values.get("SCA_STATE_HOME", "").strip()
    if override:
        return Path(override).expanduser()

    resolved_platform = os.name if platform is None else platform
    if resolved_platform == "nt":
        local_app_data = values.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / "sca"

    xdg_state = values.get("XDG_STATE_HOME", "").strip()
    if xdg_state:
        return Path(xdg_state).expanduser() / "sca"
    return (home or Path.home()) / ".local" / "state" / "sca"


def workspace_state_dir(
    workspace_dir: str | Path,
    env: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
    platform: str | None = None,
) -> Path:
    """Return a stable user-level runtime directory for one workspace."""
    workspace = os.path.normcase(os.path.realpath(workspace_dir))
    digest = hashlib.sha256(workspace.encode("utf-8")).hexdigest()[:12]
    raw_name = Path(workspace).name or "workspace"
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_name).strip("._")
    return user_state_dir(
        env,
        home=home,
        platform=platform,
    ) / "workspaces" / f"{safe_name or 'workspace'}-{digest}"


def safe_state_component(value: str, *, fallback: str = "item") -> str:
    """Return one non-traversing filesystem component for trusted state paths."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return safe or fallback


@dataclass(frozen=True)
class WorkspaceStateMetadata:
    schema_version: int
    workspace_path: str
    created_at: float
    last_accessed_at: float
    orphaned_at: float | None = None

    @classmethod
    def from_dict(cls, payload: object) -> WorkspaceStateMetadata:
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("unsupported workspace state metadata")
        return cls(
            schema_version=1,
            workspace_path=str(payload["workspace_path"]),
            created_at=float(payload["created_at"]),
            last_accessed_at=float(payload["last_accessed_at"]),
            orphaned_at=(
                float(payload["orphaned_at"])
                if payload.get("orphaned_at") is not None
                else None
            ),
        )


def read_workspace_metadata(state_dir: str | Path) -> WorkspaceStateMetadata | None:
    path = Path(state_dir) / "workspace.json"
    if not path.is_file():
        return None
    return WorkspaceStateMetadata.from_dict(
        json.loads(path.read_text(encoding="utf-8"))
    )


def write_workspace_metadata(
    state_dir: str | Path,
    metadata: WorkspaceStateMetadata,
) -> Path:
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "workspace.json"
    temporary = root / f"workspace.json.{os.getpid()}.{threading.get_ident()}.tmp"
    temporary.write_text(
        json.dumps(asdict(metadata), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def touch_workspace_state(
    workspace_dir: str | Path,
    *,
    now: float | None = None,
) -> Path:
    timestamp = time.time() if now is None else now
    state_dir = workspace_state_dir(workspace_dir)
    with _metadata_lock:
        existing: WorkspaceStateMetadata | None
        try:
            existing = read_workspace_metadata(state_dir)
        except (OSError, ValueError, json.JSONDecodeError):
            existing = None
        metadata = WorkspaceStateMetadata(
            schema_version=1,
            workspace_path=os.path.realpath(workspace_dir),
            created_at=existing.created_at if existing is not None else timestamp,
            last_accessed_at=timestamp,
            orphaned_at=None,
        )
        write_workspace_metadata(state_dir, metadata)
    return state_dir


__all__ = [
    "WorkspaceStateMetadata",
    "read_workspace_metadata",
    "safe_state_component",
    "touch_workspace_state",
    "user_state_dir",
    "workspace_state_dir",
    "write_workspace_metadata",
]
