from __future__ import annotations

import asyncio
import re
import os

from .base import BaseTool, ToolResult

BLACKLIST = [
    r"rm\s+-rf\s+/",
    r"sudo\b",
    r"chmod\s+777\s+/",
    r"mkfs\b",
    r"dd\s+if=",
    r":\(\)\s*\{\s*:\|\:&\s*\}\s*;",
    r">\s*/dev/sda",
]


class BashTool(BaseTool):
    name = "bash"
    description = (
        "Execute a shell command in the workspace directory. "
        "Returns stdout, stderr, and exit code. Commands timeout after 120s."
    )
    parameters = {
        "command": {
            "type": "string",
            "description": "The shell command to execute.",
        },
    }
    required_params = ["command"]

    async def execute(self, command: str, workspace_dir: str = "") -> ToolResult:
        # Security check
        for pattern in BLACKLIST:
            if re.search(pattern, command):
                return ToolResult.fail(f"Command blocked by security policy: matched pattern '{pattern}'")

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace_dir or os.getcwd(),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=120
            )
            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                detail = stderr_str or stdout_str or f"exit code {proc.returncode}"
                return ToolResult.fail(detail, content=stdout_str)

            return ToolResult.ok(stdout_str or "(no output)")
        except asyncio.TimeoutError:
            return ToolResult.fail("Command timed out after 120 seconds")
        except Exception as e:
            return ToolResult.fail(str(e))
