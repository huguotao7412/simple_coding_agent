from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from time import monotonic
from typing import Any

from .config import SandboxConfig
from .contracts import (
    SandboxExecutionRequest,
    SandboxExecutionResult,
    SandboxUnavailableError,
)
from .paths import REMOTE_ARCHIVE_IN, REMOTE_ARCHIVE_OUT, resolve_sandbox_cwd
from .transport import (
    REMOTE_PACK_COMMAND,
    REMOTE_UNPACK_COMMAND,
    apply_workspace_archive,
    pack_workspace,
)


class E2BSandboxBackend:
    """Execute commands in persistent E2B sandboxes with guarded workspace sync."""

    def __init__(self, config: SandboxConfig) -> None:
        self._config = config
        self._sessions: dict[Path, Any] = {}
        self._locks: dict[Path, asyncio.Lock] = {}

    @property
    def name(self) -> str:
        return "e2b"

    @property
    def isolated(self) -> bool:
        return True

    @property
    def python_executable(self) -> str:
        return "python3"

    async def ensure_available(self) -> None:
        if not self._config.e2b_api_key:
            raise SandboxUnavailableError(
                "E2B sandbox selected but E2B_API_KEY is empty. "
                "Create a key at https://e2b.dev/dashboard."
            )
        try:
            from e2b import AsyncSandbox  # noqa: F401
        except ImportError as error:
            raise SandboxUnavailableError(
                "E2B Python SDK is not installed; install project dependencies."
            ) from error

    async def _session(self, workspace: Path) -> Any:
        root = workspace.resolve()
        existing = self._sessions.get(root)
        if existing is not None and await existing.is_running():
            return existing
        await self.ensure_available()
        from e2b import AsyncSandbox

        session = await AsyncSandbox.create(
            template=self._config.e2b_template,
            timeout=round(self._config.limits.max_timeout_seconds) + 60,
            api_key=self._config.e2b_api_key,
            secure=True,
            allow_internet_access=self._config.e2b_allow_internet,
            metadata={"application": "simple-coding-agent"},
        )
        self._sessions[root] = session
        return session

    async def execute(
        self,
        request: SandboxExecutionRequest,
    ) -> SandboxExecutionResult:
        root = request.workspace.resolve()
        _, remote_cwd = resolve_sandbox_cwd(root, request.cwd)
        lock = self._locks.setdefault(root, asyncio.Lock())
        async with lock:
            session = await self._session(root)
            limit = self._config.limits.max_transfer_bytes
            payload = await asyncio.to_thread(pack_workspace, root, max_bytes=limit)
            await session.files.write(REMOTE_ARCHIVE_IN.as_posix(), payload)
            await session.commands.run(REMOTE_UNPACK_COMMAND, timeout=60)
            command = request.command[0] if request.shell else shlex.join(request.command)
            timeout = min(
                request.timeout_seconds,
                self._config.limits.max_timeout_seconds,
            )
            started = monotonic()
            timed_out = False
            exit_code: int | None = None
            output = ""
            try:
                result = await session.commands.run(
                    command,
                    cwd=remote_cwd,
                    timeout=timeout,
                )
                exit_code = result.exit_code
                output = f"{result.stdout}{result.stderr}"
            except Exception as error:
                exit_code = getattr(error, "exit_code", None)
                stdout = getattr(error, "stdout", "")
                stderr = getattr(error, "stderr", "")
                output = f"{stdout}{stderr}" or str(error)
                timed_out = "timeout" in type(error).__name__.lower()
            await session.commands.run(REMOTE_PACK_COMMAND, timeout=60)
            remote_payload = bytes(await session.files.read(
                REMOTE_ARCHIVE_OUT.as_posix(),
                format="bytes",
            ))
            await asyncio.to_thread(
                apply_workspace_archive,
                root,
                remote_payload,
                max_bytes=limit,
            )
            output_limit = self._config.limits.max_output_chars
            return SandboxExecutionResult(
                backend=self.name,
                isolated=True,
                command=request.command,
                exit_code=None if timed_out else exit_code,
                output=output[-output_limit:],
                duration_ms=round((monotonic() - started) * 1000),
                timed_out=timed_out,
            )

    async def close_workspace(self, workspace: Path) -> None:
        root = workspace.resolve()
        session = self._sessions.pop(root, None)
        self._locks.pop(root, None)
        if session is not None:
            try:
                await session.kill()
            except Exception:
                return


__all__ = ["E2BSandboxBackend"]
