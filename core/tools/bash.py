from __future__ import annotations

import platform
import subprocess
import asyncio
import re
import os
from collections import deque

from .base import BaseTool, ToolResult, truncate_long_output

BLACKLIST = [
    r"rm\s+-rf\s+/",
    r"sudo\b",
    r"chmod\s+777\s+/",
    r"mkfs\b",
    r"dd\s+if=",
    r":\(\)\s*\{\s*:\|\:&\s*\}\s*;",
    r">\s*/dev/sda",
]

# =====================================================================
# Background process registry
# =====================================================================
# Structure: { pid: {"proc": asyncio.subprocess.Process, "logs": deque(maxlen=500)} }
_BACKGROUND_PROCESSES: dict[int, dict] = {}


async def _read_stream(stream: asyncio.StreamReader | None, buffer: deque[str]) -> None:
    """Lightweight coroutine that drains a stream line-by-line into a deque."""
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            break
        buffer.append(line.decode("utf-8", errors="replace"))


class BashTool(BaseTool):
    name = "bash"
    description = (
        "Execute shell commands with four action modes:\n"
        "• 'run' (default): block until completion (120s timeout), return full output.\n"
        "• 'background': launch a long-running server/daemon, return its PID immediately.\n"
        "• 'logs': fetch the last 500 lines of buffered output from a background process.\n"
        "• 'kill': terminate a background process and clean up its resources.\n"
        "Use background + logs + kill to implement the start→verify→stop dev loop."
    )
    parameters = {
        "command": {
            "type": "string",
            "description": (
                "The shell command to execute. Required for 'run' and 'background' actions. "
                "Can be empty for 'logs' and 'kill' actions."
            ),
        },
        "action": {
            "type": "string",
            "enum": ["run", "background", "logs", "kill"],
            "description": (
                "Execution mode: 'run' blocks for output (default), "
                "'background' starts a long-running process, "
                "'logs' retrieves buffered output by PID, "
                "'kill' terminates a background process by PID."
            ),
        },
        "pid": {
            "type": "integer",
            "description": (
                "Process ID of a background process. Required for 'logs' and 'kill' actions. "
                "The PID is returned by the 'background' action."
            ),
        },
    }
    required_params = ["command"]

    async def execute(
        self,
        command: str,
        action: str = "run",
        pid: int | None = None,
        workspace_dir: str = "",
    ) -> ToolResult:
        # Security check (applies to all actions that execute a command)
        if action in ("run", "background"):
            for pattern in BLACKLIST:
                if re.search(pattern, command):
                    return ToolResult.fail(
                        f"Command blocked by security policy: matched pattern '{pattern}'"
                    )

        cwd = workspace_dir or os.getcwd()

        # ================================================================
        # Route by action
        # ================================================================
        if action == "run":
            return await self._run_blocking(command, cwd)
        elif action == "background":
            return await self._run_background(command, cwd)
        elif action == "logs":
            return self._get_logs(pid)
        elif action == "kill":
            return await self._kill_process(pid)
        else:
            return ToolResult.fail(f"Unknown action: '{action}'. Valid: run, background, logs, kill.")

    # ------------------------------------------------------------------
    # Action handlers
    # ------------------------------------------------------------------

    async def _run_blocking(self, command: str, cwd: str) -> ToolResult:
        """Original blocking behaviour with 120s timeout."""
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=120
            )
            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                detail = stderr_str or stdout_str or f"exit code {proc.returncode}"
                return ToolResult.fail(
                    detail,
                    content=truncate_long_output(stdout_str) if stdout_str else stdout_str,
                )

            return ToolResult.ok(truncate_long_output(stdout_str or "(no output)"))
        except asyncio.TimeoutError:
            try:
                if platform.system() == "Windows":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
                else:
                    proc.kill()
                await proc.wait()
            except Exception:
                pass
            return ToolResult.fail("Command timed out after 120 seconds")
        except Exception as e:
            return ToolResult.fail(str(e))

    async def _run_background(self, command: str, cwd: str) -> ToolResult:
        """Launch a command in the background, capture its output asynchronously."""
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,  # merge stderr into stdout
                cwd=cwd,
            )
        except Exception as e:
            return ToolResult.fail(f"Failed to start background process: {e}")

        log_deque: deque[str] = deque(maxlen=500)
        _BACKGROUND_PROCESSES[proc.pid] = {"proc": proc, "logs": log_deque}

        # Start a lightweight background task to drain stdout into the buffer
        asyncio.create_task(_read_stream(proc.stdout, log_deque))

        return ToolResult.ok(
            f"[Success] Command '{command}' is running in background.\nPID: {proc.pid}"
        )

    def _get_logs(self, pid: int | None) -> ToolResult:
        """Return buffered logs for a background process."""
        if pid is None:
            return ToolResult.fail(
                "The 'logs' action requires a 'pid' argument. "
                "Use the PID returned by the 'background' action."
            )
        entry = _BACKGROUND_PROCESSES.get(pid)
        if entry is None:
            running_pids = list(_BACKGROUND_PROCESSES.keys())
            return ToolResult.fail(
                f"No background process found with PID {pid}. "
                f"Currently tracked PIDs: {running_pids if running_pids else '(none)'}"
            )

        proc = entry["proc"]
        logs = "".join(entry["logs"])

        # Check if process has exited
        if proc.returncode is not None:
            suffix = f"\n[Process {pid} has exited with code {proc.returncode}]"
        else:
            suffix = f"\n[Process {pid} is still running]"

        return ToolResult.ok(truncate_long_output(logs or "(no output yet)") + suffix)

    async def _kill_process(self, pid: int | None) -> ToolResult:
        """Terminate a background process and clean up its entry."""
        if pid is None:
            return ToolResult.fail(
                "The 'kill' action requires a 'pid' argument. "
                "Use the PID returned by the 'background' action."
            )
        entry = _BACKGROUND_PROCESSES.pop(pid, None)
        if entry is None:
            running_pids = list(_BACKGROUND_PROCESSES.keys())
            return ToolResult.fail(
                f"No background process found with PID {pid}. "
                f"Currently tracked PIDs: {running_pids if running_pids else '(none)'}"
            )

        proc = entry["proc"]
        remaining_logs = "".join(entry["logs"])

        # Handle already-exited process gracefully
        if proc.returncode is not None:
            return ToolResult.ok(
                f"Process {pid} had already exited with code {proc.returncode}.\n"
                f"Final output ({len(remaining_logs)} chars):\n{truncate_long_output(remaining_logs)}"
            )

        try:
            if platform.system() == "Windows":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            else:
                proc.kill()
            await proc.wait()
        except ProcessLookupError:
            # Process already gone (race condition) — that's fine
            pass
        except Exception as e:
            return ToolResult.fail(f"Failed to kill process {pid}: {e}")

        return ToolResult.ok(
            f"Process {pid} terminated.\n"
            f"Final output ({len(remaining_logs)} chars):\n{truncate_long_output(remaining_logs)}"
        )
