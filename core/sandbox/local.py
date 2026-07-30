from __future__ import annotations

import asyncio
import os
import signal
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
                start_new_session=sys.platform != "win32",
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *request.command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=sanitized_subprocess_environment(),
                start_new_session=sys.platform != "win32",
            )
        if process.stdout is None:  # pragma: no cover - PIPE guarantees this
            raise RuntimeError("sandbox process stdout pipe was not created")
        output_task = asyncio.create_task(process.stdout.read())
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            await _terminate_process_tree(process)
        except asyncio.CancelledError:
            await _terminate_process_tree(process)
            await _finish_output_task(output_task)
            raise
        output = await _finish_output_task(output_task)
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


async def _terminate_process_tree(
    process: asyncio.subprocess.Process,
) -> None:
    """Terminate the command and descendants without leaving pipe holders alive."""
    if process.returncode is not None:
        return

    if sys.platform == "win32":
        try:
            await asyncio.to_thread(_terminate_windows_process_tree, process.pid)
        except OSError:
            if process.returncode is None:
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            if process.returncode is None:
                process.kill()

    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        if process.returncode is None:
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            pass


def _terminate_windows_process_tree(root_pid: int) -> None:
    """Terminate a Windows process tree using Toolhelp, children first."""
    import ctypes
    from ctypes import wintypes

    process_terminate = 0x0001
    snapshot_processes = 0x00000002
    invalid_handle = ctypes.c_void_p(-1).value

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessEntry32W),
    ]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessEntry32W),
    ]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(snapshot_processes, 0)
    if snapshot == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())

    parent_by_pid: dict[int, int] = {}
    entry = ProcessEntry32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        has_entry = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while has_entry:
            parent_by_pid[int(entry.th32ProcessID)] = int(
                entry.th32ParentProcessID
            )
            has_entry = bool(
                kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            )
    finally:
        kernel32.CloseHandle(snapshot)

    descendants: list[int] = []
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop()
        children = [
            pid for pid, parent_pid in parent_by_pid.items()
            if parent_pid == parent and pid not in descendants
        ]
        descendants.extend(children)
        frontier.extend(children)

    terminated_any = False
    for pid in [*reversed(descendants), root_pid]:
        handle = kernel32.OpenProcess(process_terminate, False, pid)
        if not handle:
            continue
        try:
            terminated_any = bool(kernel32.TerminateProcess(handle, 1)) or terminated_any
        finally:
            kernel32.CloseHandle(handle)
    if not terminated_any:
        raise OSError(f"unable to terminate process tree rooted at PID {root_pid}")


async def _finish_output_task(output_task: asyncio.Task[bytes]) -> bytes:
    try:
        return await asyncio.wait_for(output_task, timeout=2)
    except TimeoutError:
        output_task.cancel()
        await asyncio.gather(output_task, return_exceptions=True)
        return b""


__all__ = ["LocalSandboxBackend"]
