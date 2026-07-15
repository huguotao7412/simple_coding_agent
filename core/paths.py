from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from pathlib import Path


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


__all__ = ["user_state_dir", "workspace_state_dir"]
