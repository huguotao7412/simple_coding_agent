from __future__ import annotations

import hashlib
from pathlib import Path

from ..paths import workspace_state_dir


def validate_artifact_uri(
    uri: str,
    *,
    workspace_dir: str | Path,
    require_exists: bool = True,
    expected_digest: str = "",
) -> Path:
    """Resolve an untrusted artifact reference inside an approved state boundary."""
    if not uri:
        raise ValueError("artifact reference must not be empty")
    candidate = Path(uri)
    if not candidate.is_absolute():
        candidate = Path(workspace_dir) / candidate
    resolved = candidate.resolve(strict=False)
    roots = (
        Path(workspace_dir).resolve(),
        workspace_state_dir(workspace_dir).resolve(),
    )
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise ValueError("artifact reference escapes workspace/state root")
    if require_exists and not resolved.is_file():
        raise ValueError(f"artifact reference is missing: {resolved}")
    if expected_digest and resolved.is_file():
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if digest != expected_digest.removeprefix("sha256:"):
            raise ValueError(f"artifact digest mismatch: {resolved}")
    return resolved


__all__ = ["validate_artifact_uri"]
