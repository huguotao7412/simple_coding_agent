from __future__ import annotations

import platform
import subprocess
import asyncio
import re
import os
from collections import deque

from .base import BaseTool, ToolResult, truncate_long_output

BLACKLIST = [
    # Recursive force delete: rm -r /, rm -rf /, rm -rfa /, rm -rf ~, etc.
    r"rm\s+-r\S*\s+[/~]",
    # Force delete with long flag: rm --force /
    r"rm\s+--force\S*\s+[/~]",
    # Privilege escalation
    r"\bsudo\b",
    # Permissive chmod on root/home
    r"chmod\s+[-R]*\s*777\s+[/~]",
    # Filesystem formatting
    r"\bmkfs\b",
    # Raw disk writes
    r"\bdd\s+if=",
    # Fork bomb
    r":\(\)\s*\{\s*:\|\:&\s*\}\s*;",
    # Overwrite block devices
    r">\s*/dev/sd[a-z]",
    # Windows: format drive
    r"\bformat\s+[A-Za-z]:",
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
        "Execute shell commands with four action modes.\n"
        "IMPORTANT: Bash is stateless — each call starts a fresh subshell. "
        "Do NOT use `cd` commands. Use the `cwd` parameter to specify the working directory.\n\n"
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
        "cwd": {
            "type": "string",
            "description": (
                "Optional relative path to execute the command in. "
                "Defaults to workspace root. Use this instead of 'cd' commands "
                "since each bash call is a fresh subshell."
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
        cwd: str = "",
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

        # Resolve working directory from cwd parameter (relative to workspace)
        base_dir = workspace_dir or os.getcwd()
        target_dir = base_dir
        if cwd:
            target_dir = os.path.abspath(os.path.join(base_dir, cwd))
            # Security: prevent directory escape
            abs_base = os.path.abspath(base_dir)
            if not target_dir.startswith(abs_base + os.sep) and target_dir != abs_base:
                return ToolResult.fail("Security Error: cwd escapes workspace.")
            if not os.path.isdir(target_dir):
                return ToolResult.fail(f"Directory not found: {cwd}")

        # ================================================================
        # Route by action
        # ================================================================
        if action == "run":
            return await self._run_blocking(command, target_dir)
        elif action == "background":
            return await self._run_background(command, target_dir)
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
            env = os.environ.copy()
            env["DEBIAN_FRONTEND"] = "noninteractive"
            env["CI"] = "1"
            env["GIT_TERMINAL_PROMPT"] = "0"
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
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
            env = os.environ.copy()
            env["DEBIAN_FRONTEND"] = "noninteractive"
            env["CI"] = "1"
            env["GIT_TERMINAL_PROMPT"] = "0"
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,  # merge stderr into stdout
                cwd=cwd,
                env=env,
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
