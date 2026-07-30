from __future__ import annotations

from pathlib import Path
import re

from ..sandbox.contracts import SandboxBackend, SandboxExecutionRequest
from ..sandbox.local import LocalSandboxBackend
from ..security.models import SecurityOutcome
from ..security.tool_security import SecurityMiddleware
from .models import GateResult, GateSpec, VerificationConfig, VerificationReport


_UNSAFE_PATH_CHARACTER = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_segment(value: str, *, fallback: str) -> str:
    sanitized = _UNSAFE_PATH_CHARACTER.sub("_", value).strip("._")
    return (sanitized or fallback)[:80]


def _expand_command(
    command: tuple[str, ...],
    *,
    python_executable: str,
) -> tuple[str, ...]:
    return tuple(
        python_executable if argument == "{python}" else argument
        for argument in command
    )


class VerificationRunner:
    def __init__(
        self,
        *,
        artifact_root: str | Path,
        excerpt_limit: int = 4000,
        sandbox_backend: SandboxBackend | None = None,
        security_middleware: SecurityMiddleware | None = None,
    ) -> None:
        if excerpt_limit <= 0:
            raise ValueError("excerpt_limit must be positive")
        self._artifact_root = Path(artifact_root)
        self._excerpt_limit = excerpt_limit
        self._sandbox_backend = sandbox_backend or LocalSandboxBackend()
        self._security_middleware = security_middleware

    async def run(
        self,
        config: VerificationConfig,
        *,
        worktree: str | Path,
        task_id: str,
        attempt: int,
    ) -> VerificationReport:
        if attempt < 1:
            raise ValueError("attempt must be positive")
        worktree_path = Path(worktree).resolve()
        if not worktree_path.is_dir():
            raise ValueError(f"verification worktree does not exist: {worktree_path}")

        results: list[GateResult] = []
        for gate in config.gates:
            results.append(
                await self._run_gate(
                    gate,
                    worktree=worktree_path,
                    task_id=task_id,
                    attempt=attempt,
                )
            )
        return VerificationReport(attempt=attempt, results=tuple(results))

    async def _run_gate(
        self,
        gate: GateSpec,
        *,
        worktree: Path,
        task_id: str,
        attempt: int,
    ) -> GateResult:
        command = _expand_command(
            gate.command,
            python_executable=self._sandbox_backend.python_executable,
        )
        middleware = self._security_middleware or SecurityMiddleware(str(worktree))
        decision = middleware.authorize_tool(
            run_id=f"verification:{task_id}",
            actor_id=task_id,
            role="verifier",
            tool_name="run",
            arguments={
                "command": " ".join(command),
                "workspace_dir": str(worktree),
            },
        )
        if decision.outcome is not SecurityOutcome.ALLOW:
            return GateResult(
                gate_name=gate.name,
                command=command,
                required=gate.required,
                passed=False,
                exit_code=None,
                duration_ms=0,
                output_artifact="",
                output_excerpt="Verification command denied by security policy.",
                execution_backend=self._sandbox_backend.name,
                isolated=self._sandbox_backend.isolated,
            )
        execution = await self._sandbox_backend.execute(SandboxExecutionRequest(
            workspace=worktree,
            command=command,
            timeout_seconds=gate.timeout_seconds,
        ))
        output_text = execution.output
        artifact_path = self._artifact_path(task_id, attempt, gate.name)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(output_text, encoding="utf-8")

        return GateResult(
            gate_name=gate.name,
            command=command,
            required=gate.required,
            passed=execution.succeeded,
            exit_code=execution.exit_code,
            duration_ms=execution.duration_ms,
            output_artifact=str(artifact_path.resolve()),
            output_excerpt=output_text[-self._excerpt_limit :],
            timed_out=execution.timed_out,
            execution_backend=execution.backend,
            isolated=execution.isolated,
        )

    def _artifact_path(self, task_id: str, attempt: int, gate_name: str) -> Path:
        return (
            self._artifact_root
            / _safe_segment(task_id, fallback="task")
            / f"attempt-{attempt}"
            / f"{_safe_segment(gate_name, fallback='gate')}.log"
        )


__all__ = ["VerificationRunner"]
