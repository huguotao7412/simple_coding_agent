from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..events import AgentEvent
from ..runs.context import RunContext
from ..sandbox.contracts import (
    SandboxBackend,
    SandboxExecutionRequest,
    SandboxUnavailableError,
)
from .base import BaseTool, ToolResult


class SandboxRunTool(BaseTool):
    """Foreground shell adapter backed by an isolated SandboxBackend."""

    name = "run"
    description = (
        "Execute one foreground shell command inside the configured sandbox. "
        "The working directory must stay inside the Actor workspace."
    )
    parameters = {
        "command": {"type": "string", "description": "Shell command to execute."},
        "cwd": {
            "type": "string",
            "description": "Workspace-relative working directory (default: workspace root).",
        },
        "timeout": {
            "type": "number",
            "description": "Timeout in milliseconds (default: 30000).",
        },
    }
    required_params = ["command"]

    def __init__(
        self,
        backend: SandboxBackend,
        *,
        run_context: RunContext | None = None,
        actor_id: str = "",
    ) -> None:
        super().__init__()
        self._backend = backend
        self._run_context = run_context
        self._actor_id = actor_id

    async def execute(self, **kwargs: Any) -> ToolResult:
        command = kwargs.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolResult.fail("'command' must be a non-empty string")
        workspace = kwargs.get("workspace_dir")
        if not isinstance(workspace, str) or not workspace:
            return ToolResult.fail("sandbox workspace is not configured")
        cwd = kwargs.get("cwd", ".")
        if not isinstance(cwd, str):
            return ToolResult.fail("'cwd' must be a string")
        raw_timeout = kwargs.get("timeout", 30_000)
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
            return ToolResult.fail("'timeout' must be a positive number of milliseconds")
        if raw_timeout <= 0:
            return ToolResult.fail("'timeout' must be a positive number of milliseconds")
        try:
            result = await self._backend.execute(SandboxExecutionRequest(
                workspace=Path(workspace),
                command=(command,),
                timeout_seconds=float(raw_timeout) / 1000.0,
                cwd=cwd,
                shell=True,
            ))
        except (OSError, ValueError, SandboxUnavailableError) as error:
            return ToolResult.fail(f"Sandbox execution failed: {error}")

        payload = {
            "success": result.succeeded,
            "backend": result.backend,
            "isolated": result.isolated,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "duration_ms": result.duration_ms,
            "output": result.output,
            "command": command,
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        if self._run_context is not None:
            await self._run_context.emit(AgentEvent(
                type="sandbox_execution",
                content=json.dumps({
                    key: value for key, value in payload.items()
                    if key not in {"output", "command"}
                }, ensure_ascii=False),
                tool_name=self.name,
                actor_id=self._actor_id,
                task_id=self._actor_id,
            ))
        if result.timed_out:
            return ToolResult.fail("Sandbox command timed out", content=content)
        if not result.succeeded:
            return ToolResult.fail(
                f"Sandbox command failed with exit code {result.exit_code}",
                content=content,
            )
        return ToolResult.ok(content)


__all__ = ["SandboxRunTool"]
