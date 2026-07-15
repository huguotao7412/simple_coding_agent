from __future__ import annotations

import os
from pathlib import Path, PurePosixPath


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
    container = PurePosixPath("/workspace", *relative.parts).as_posix()
    return resolved, container


__all__ = ["resolve_sandbox_cwd"]
