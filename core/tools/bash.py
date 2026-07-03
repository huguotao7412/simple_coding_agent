from __future__ import annotations

import platform
import subprocess
import asyncio
import uuid
import re
import os
import sys
from collections import deque

from .base import BaseTool, ToolResult, semantic_truncate

ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

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
    # --- Git history / remote mutation ---
    r"\bgit\s+merge\b",
    r"\bgit\s+push\b",
    r"\bgit\s+rebase\b",
    r"\bgit\s+pull\b",
    r"\bgit\s+fetch\b",
    # --- Git worktree manipulation ---
    r"\bgit\s+worktree\b",
    # --- Git destructive operations ---
    r"\bgit\s+branch\s+-D\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-fd\b",
    # --- Git remote manipulation ---
    r"\bgit\s+remote\b",
    # --- Git stash (can hide Actor changes from diff extraction) ---
    r"\bgit\s+stash\b",
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
        "The 'run' action uses a persistent shell session — environment state (cwd, "
        "env vars, venv activations) persists across calls. You CAN use `cd`, `source`, "
        "and `export` and they will stick for subsequent commands.\n\n"
        "• 'run' (default): execute in the persistent session (120s timeout), return full output.\n"
        "• 'background': launch a long-running server/daemon in a fresh process, return its PID immediately.\n"
        "• 'logs': fetch the last 500 lines of buffered output from a background process.\n"
        "• 'kill': terminate a background process and clean up its resources.\n"
        "Use background + logs + kill to implement the start→verify→stop dev loop."
    )
    parameters = {
        "command": {
            "type": "string",
            "description": (
                "The shell command to execute. Required for 'run' and 'background' actions. "
                "Can be empty for 'logs' and 'kill' actions. "
                "For 'run', the command runs in a persistent session — cd, source, and export "
                "effects are preserved."
            ),
        },
        "action": {
            "type": "string",
            "enum": ["run", "background", "logs", "kill"],
            "description": (
                "Execution mode: 'run' blocks for output in the persistent session (default), "
                "'background' starts a long-running process in a fresh shell, "
                "'logs' retrieves buffered output by PID, "
                "'kill' terminates a background process by PID."
            ),
        },
        "cwd": {
            "type": "string",
            "description": (
                "Optional relative path to execute the command in. "
                "Defaults to workspace root. The session's working directory will be updated "
                "to this path for subsequent commands."
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

    def __init__(self):
        self._session_proc: asyncio.subprocess.Process | None = None
        self._session_lock: asyncio.Lock = asyncio.Lock()

    async def _ensure_session(self, cwd: str) -> asyncio.subprocess.Process:
        """Start or recover the persistent shell session."""
        if self._session_proc is not None and self._session_proc.returncode is not None:
            # Session died — clean up
            try:
                self._session_proc.stdin.close()
            except Exception:
                pass
            self._session_proc = None

        if self._session_proc is None:
            # Anti-hang: prevent apt/npm/git from blocking on interactive prompts
            if not os.path.exists(cwd):
                cwd = os.getcwd()
            session_env = os.environ.copy()
            session_env["DEBIAN_FRONTEND"] = "noninteractive"
            session_env["CI"] = "1"
            session_env["GIT_TERMINAL_PROMPT"] = "0"
            if sys.platform == "win32":
                if "SystemRoot" not in session_env:
                    session_env["SystemRoot"] = "C:\\Windows"
                self._session_proc = await asyncio.create_subprocess_exec(
                    "cmd.exe",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=cwd,
                    env=session_env,
                )
            else:
                self._session_proc = await asyncio.create_subprocess_exec(
                    "/bin/bash", "--norc",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=cwd,
                    env=session_env,
                )

        return self._session_proc

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
            # Ensure stateful session exists, starting in target_dir
            await self._ensure_session(target_dir)
            # If cwd specified, prepend cd so session lands in the right dir
            full_cmd = command
            if cwd:
                if sys.platform == "win32":
                    full_cmd = f'cd /d "{target_dir}" && {command}'
                else:
                    full_cmd = f'cd "{target_dir}" && {command}'
            return await self._run_blocking(full_cmd)
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

    async def _run_blocking(self, command: str) -> ToolResult:
        """Execute a command in the persistent shell session with marker-based output capture.

        Writes the command followed by a unique echo marker to the session's stdin,
        then reads stdout line-by-line until the marker is found. The marker carries
        the exit code so the caller knows whether the command succeeded.
        """
        async with self._session_lock:
            proc = self._session_proc
            if proc is None or proc.returncode is not None:
                return ToolResult.fail("Shell session is not running. Please retry.")

            import sys
            import uuid  # 引入 uuid 生成动态不可预测的 Marker

            marker_id = f"__SCA_{uuid.uuid4().hex}__"

            if sys.platform == "win32":
                # Windows CMD: 利用延迟环境变量或直接设值，echo. 不会重置 errorlevel
                marker = f"set __SCA_ERR=%errorlevel%\r\necho.\r\necho {marker_id} %__SCA_ERR%"
            else:
                # Bash: 立即将 $? 保存到变量中，防止被后续的 echo 覆盖
                marker = f"__SCA_ERR=$?; echo \"\"; echo {marker_id} $__SCA_ERR"

            try:
                # --- Write command + marker into the session ---
                proc.stdin.write((command + "\n").encode())
                proc.stdin.write((marker + "\n").encode())
                await proc.stdin.drain()

                # --- Read output until marker line ---
                output_lines: list[str] = []
                exit_code = 0
                marker_found = False

                while True:
                    try:
                        line = await asyncio.wait_for(
                            proc.stdout.readline(), timeout=120
                        )
                    except asyncio.TimeoutError:
                        # Session hung — kill it completely so next call starts fresh
                        self._session_proc = None
                        try:
                            import sys
                            import subprocess
                            if sys.platform == "win32":
                                # 必须使用 /T (Tree) 彻底击杀子进程，防止端口与文件被锁
                                await asyncio.create_subprocess_exec("taskkill", "/F", "/T", "/PID", str(proc.pid), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                            else:
                                proc.kill()
                        except Exception:
                            pass
                        return ToolResult.fail("Command timed out after 120 seconds. Shell session reset.")

                    if not line:
                        # EOF — session died
                        break

                    decoded = line.decode("utf-8", errors="replace")

                    # 将硬编码的 __SCA_MARKER__ 替换为动态 marker_id
                    if marker_id in decoded:
                        marker_found = True
                        import re
                        match = re.search(f"{marker_id}\\s+(\\d+)", decoded)
                        if match:
                            exit_code = int(match.group(1))
                        break

                    output_lines.append(decoded)

                output = "".join(output_lines).strip()
                output = ANSI_ESCAPE.sub('', output)

                if not marker_found:
                    # Session died unexpectedly
                    self._session_proc = None
                    return ToolResult.fail(
                        f"Shell session terminated unexpectedly (returncode={proc.returncode}).",
                        content=semantic_truncate(output)[0] if output else output,
                    )

                if exit_code != 0:
                    return ToolResult.fail(
                        f"Exit code {exit_code}",
                        content=semantic_truncate(output)[0] if output else output,
                    )

                return ToolResult.ok(semantic_truncate(output or "(no output)")[0])

            except (BrokenPipeError, ConnectionResetError, ProcessLookupError):
                # Session pipe broke — reset for next call
                self._session_proc = None
                return ToolResult.fail(
                    "Shell session pipe broken. The session has been reset — please retry."
                )
            except Exception as e:
                self._session_proc = None
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

        return ToolResult.ok(semantic_truncate(logs or "(no output yet)")[0] + suffix)

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
                f"Final output ({len(remaining_logs)} chars):\n{semantic_truncate(remaining_logs)[0]}"
            )

        try:
            if platform.system() == "Windows":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(proc.pid),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                await killer.wait()
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
            f"Final output ({len(remaining_logs)} chars):\n{semantic_truncate(remaining_logs)[0]}"
        )
