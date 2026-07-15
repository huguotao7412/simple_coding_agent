from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from dotenv import dotenv_values


USER_CONFIG_TEMPLATE = """# Simple Coding Agent user configuration
SCA_API_KEY=your-api-key
SCA_API_BASE=https://api.deepseek.com
SCA_MODEL=deepseek-v4-pro
SCA_MAX_TOKENS=128000
SCA_MAX_ACTORS=4
SCA_SANDBOX_BACKEND=local
# Optional runtime state override (reports, checkpoints, patches, verification logs)
# SCA_STATE_HOME=C:\\path\\to\\sca-state

# Optional E2B remote sandbox
# E2B_API_KEY=e2b_your-key
# SCA_E2B_TEMPLATE=base
# SCA_E2B_ALLOW_INTERNET=false
"""

_MANAGED_ENV_VALUES: dict[str, str] = {}


def _is_sca_setting(key: str) -> bool:
    return key.startswith("SCA_") or key == "E2B_API_KEY"


def user_config_dir(
    env: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
    platform: str | None = None,
) -> Path:
    """Return the per-user SCA config directory without creating it."""
    values = os.environ if env is None else env
    override = values.get("SCA_CONFIG_HOME", "").strip()
    if override:
        return Path(override).expanduser()

    resolved_platform = os.name if platform is None else platform
    if resolved_platform == "nt":
        app_data = values.get("APPDATA", "").strip()
        if app_data:
            return Path(app_data) / "sca"

    xdg_config = values.get("XDG_CONFIG_HOME", "").strip()
    if xdg_config:
        return Path(xdg_config).expanduser() / "sca"
    return (home or Path.home()) / ".config" / "sca"


def user_config_path(
    env: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
    platform: str | None = None,
) -> Path:
    return user_config_dir(env, home=home, platform=platform) / ".env"


def initialize_user_config(
    *,
    force: bool = False,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Create a user config template and return its path."""
    path = user_config_path(env)
    if path.exists() and not force:
        raise FileExistsError(f"user configuration already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(USER_CONFIG_TEMPLATE, encoding="utf-8", newline="\n")
    if os.name != "nt":
        path.chmod(0o600)
    return path


def load_runtime_environment(
    workspace_dir: str | Path,
    *,
    environ: MutableMapping[str, str] | None = None,
    config_path: Path | None = None,
) -> tuple[Path, ...]:
    """Load user and workspace config with explicit precedence.

    Existing process variables win over workspace `.env`, which wins over the
    user-level config. Only the exact workspace `.env` is considered; parent
    directory traversal would make behavior depend on where the terminal was
    launched from.
    """
    target = os.environ if environ is None else environ
    managed = _MANAGED_ENV_VALUES if target is os.environ else {}
    for key, previous_value in tuple(managed.items()):
        if target.get(key) == previous_value:
            target.pop(key, None)
    managed.clear()
    protected = set(target)
    user_path = config_path or user_config_path(target)
    workspace_path = Path(workspace_dir).resolve() / ".env"
    loaded: list[Path] = []

    for path in (user_path, workspace_path):
        if not path.is_file():
            continue
        values = dotenv_values(path)
        for key, value in values.items():
            if (
                value is not None
                and key not in protected
                and _is_sca_setting(key)
            ):
                target[key] = value
                managed[key] = value
        loaded.append(path)
    return tuple(loaded)


__all__ = [
    "USER_CONFIG_TEMPLATE",
    "initialize_user_config",
    "load_runtime_environment",
    "user_config_dir",
    "user_config_path",
]
