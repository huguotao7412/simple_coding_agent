"""Actor role configuration — system prompt, tool allowlist, and step budget per role.

Used by DelegateTool.run_one() to dispatch Actors with the correct configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ActorRole(Enum):
    SCOUT = "scout"          # Read-only exploration
    CODER = "coder"          # Read-write implementation
    VERIFIER = "verifier"    # Test & verification


@dataclass
class RoleConfig:
    system_prompt: str
    tool_allowlist: set[str] | None   # None = all tools available
    default_max_steps: int = 30


# Actual prompt strings are imported lazily to avoid circular imports
# with system_prompt.py. See get_role_config() below.

ROLE_CONFIG: dict[ActorRole, RoleConfig] = {}


def _build_role_config() -> dict[ActorRole, RoleConfig]:
    """Build the role configuration table.

    Lazy import of system_prompt constants to avoid circular dependency
    (system_prompt.py may import from role_config in the future).
    """
    from ..system_prompt import (  # noqa: PLC0415
        SCOUT_SYSTEM_PROMPT,
        ACTOR_SYSTEM_PROMPT,
        VERIFIER_SYSTEM_PROMPT,
    )

    return {
        ActorRole.SCOUT: RoleConfig(
            system_prompt=SCOUT_SYSTEM_PROMPT,
            tool_allowlist={
                "list_dir", "read", "read_outline", "search_codebase",
                "read_file", "read_text_file", "read_multiple_files", "list_directory",
                "directory_tree", "search_files", "get_file_info",
                "list_allowed_directories",
            },
            default_max_steps=12,
        ),
        ActorRole.CODER: RoleConfig(
            system_prompt=ACTOR_SYSTEM_PROMPT,
            tool_allowlist={
                "list_dir", "read", "read_outline", "search_codebase",
                "read_file", "read_text_file", "read_multiple_files",
                "list_directory", "directory_tree", "search_files",
                "get_file_info", "list_allowed_directories",
                "edit", "edit_file", "write", "write_file", "create_directory",
                "run",
            },
            default_max_steps=30,
        ),
        ActorRole.VERIFIER: RoleConfig(
            system_prompt=VERIFIER_SYSTEM_PROMPT,
            tool_allowlist={
                "list_dir",
                "read", "read_file", "read_text_file", "read_multiple_files",
                "write_file", "edit_file",
                "create_directory", "list_directory", "directory_tree",
                "get_file_info", "list_allowed_directories",
                "run",
            },
            default_max_steps=25,
        ),
    }


def get_role_config(role: ActorRole) -> RoleConfig:
    """Return the RoleConfig for the given role, initializing lazily if needed."""
    if not ROLE_CONFIG:
        ROLE_CONFIG.update(_build_role_config())
    return ROLE_CONFIG[role]
