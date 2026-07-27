from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from time import monotonic

from .config import SandboxLimits
from .contracts import SandboxExecutionRequest, SandboxExecutionResult
from .paths import resolve_sandbox_cwd
from ..security.redaction import sanitized_subprocess_environment


class LocalSandboxBackend:
    """Compatibility backend; executes with the current host user's authority."""

    def __init__(self, limits: SandboxLimits | None = None) -> None:
        self._limits = limits or SandboxLimits()

    @property
    def name(self) -> str:
        return "local"

    @property
    def isolated(self) -> bool:
        return False

    @property
    def python_executable(self) -> str:
        return sys.executable

    async def ensure_available(self) -> None:
        return None

    async def close_workspace(self, workspace: Path) -> None:
        return None

    async def execute(
        self,
        request: SandboxExecutionRequest,
    ) -> SandboxExecutionResult:
        cwd, _ = resolve_sandbox_cwd(request.workspace, request.cwd)
        timeout = min(request.timeout_seconds, self._limits.max_timeout_seconds)
        started = monotonic()
        if request.shell:
            process = await asyncio.create_subprocess_shell(
                request.command[0],
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=sanitized_subprocess_environment(),
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *request.command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=sanitized_subprocess_environment(),
            )
        if process.stdout is None:  # pragma: no cover - PIPE guarantees this
            raise RuntimeError("sandbox process stdout pipe was not created")
        output_task = asyncio.create_task(process.stdout.read())
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            process.kill()
            await process.wait()
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            await output_task
            raise
        output = await output_task
        text = output.decode("utf-8", errors="replace")
        if len(text) > self._limits.max_output_chars:
            text = text[-self._limits.max_output_chars :]
        return SandboxExecutionResult(
            backend=self.name,
            isolated=self.isolated,
            command=request.command,
            exit_code=None if timed_out else process.returncode,
            output=text,
            duration_ms=round((monotonic() - started) * 1000),
            timed_out=timed_out,
        )


__all__ = ["LocalSandboxBackend"]
