from __future__ import annotations

from .config import SandboxConfig, SandboxMode, load_sandbox_config
from .contracts import SandboxBackend
from .e2b import E2BSandboxBackend
from .local import LocalSandboxBackend


def create_sandbox_backend(
    config: SandboxConfig | None = None,
) -> SandboxBackend:
    resolved = config or load_sandbox_config()
    if resolved.mode is SandboxMode.E2B:
        return E2BSandboxBackend(resolved)
    return LocalSandboxBackend(resolved.limits)


__all__ = ["create_sandbox_backend"]
