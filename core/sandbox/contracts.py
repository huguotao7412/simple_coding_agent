from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


class SandboxError(RuntimeError):
    """Base error raised by sandbox configuration or execution."""


class SandboxUnavailableError(SandboxError):
    """Raised when an explicitly selected backend cannot execute commands."""


@dataclass(frozen=True)
class SandboxExecutionRequest:
    workspace: Path
    command: tuple[str, ...]
    timeout_seconds: float
    cwd: str = "."
    shell: bool = False

    def __post_init__(self) -> None:
        if not self.command or any(not argument for argument in self.command):
            raise ValueError("sandbox command must contain non-empty arguments")
        if self.shell and len(self.command) != 1:
            raise ValueError("shell sandbox commands must contain exactly one string")
        if self.timeout_seconds <= 0:
            raise ValueError("sandbox timeout_seconds must be positive")
        if not self.cwd:
            raise ValueError("sandbox cwd must not be empty")


@dataclass(frozen=True)
class SandboxExecutionResult:
    backend: str
    isolated: bool
    command: tuple[str, ...]
    exit_code: int | None
    output: str
    duration_ms: int
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        return not self.timed_out and self.exit_code == 0


@runtime_checkable
class SandboxBackend(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def isolated(self) -> bool: ...

    @property
    def python_executable(self) -> str: ...

    async def ensure_available(self) -> None: ...

    async def execute(
        self,
        request: SandboxExecutionRequest,
    ) -> SandboxExecutionResult: ...

    async def close_workspace(self, workspace: Path) -> None: ...


__all__ = [
    "SandboxBackend",
    "SandboxError",
    "SandboxExecutionRequest",
    "SandboxExecutionResult",
    "SandboxUnavailableError",
]
