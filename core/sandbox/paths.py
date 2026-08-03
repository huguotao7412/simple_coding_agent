from __future__ import annotations

import os
from pathlib import Path, PurePosixPath


REMOTE_WORKSPACE_ROOT = PurePosixPath("/home/user/sca-workspace")
REMOTE_ARCHIVE_IN = PurePosixPath("/home/user/.sca-workspace-in.zip")
REMOTE_ARCHIVE_OUT = PurePosixPath("/home/user/.sca-workspace-out.zip")


def resolve_sandbox_cwd(workspace: Path, cwd: str) -> tuple[Path, str]:
    """Resolve a host cwd and matching container cwd without workspace escape."""
    root = workspace.resolve()
    candidate = Path(cwd)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        contained = os.path.commonpath([str(root), str(resolved)]) == str(root)
    except ValueError as error:
        raise ValueError("sandbox cwd is outside workspace") from error
    if not contained:
        raise ValueError("sandbox cwd is outside workspace")
    relative = resolved.relative_to(root)
    container = PurePosixPath(REMOTE_WORKSPACE_ROOT, *relative.parts).as_posix()
    return resolved, container


__all__ = [
    "REMOTE_ARCHIVE_IN",
    "REMOTE_ARCHIVE_OUT",
    "REMOTE_WORKSPACE_ROOT",
    "resolve_sandbox_cwd",
]
