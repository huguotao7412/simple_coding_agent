from __future__ import annotations

import asyncio
from pathlib import Path
import re
import sys
from time import monotonic

from .models import GateResult, GateSpec, VerificationConfig, VerificationReport


_UNSAFE_PATH_CHARACTER = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_segment(value: str, *, fallback: str) -> str:
    sanitized = _UNSAFE_PATH_CHARACTER.sub("_", value).strip("._")
    return (sanitized or fallback)[:80]


def _expand_command(command: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sys.executable if argument == "{python}" else argument for argument in command)


def _decode_output(output: bytes) -> str:
    return output.decode("utf-8", errors="replace")


class VerificationRunner:
    def __init__(self, *, artifact_root: str | Path, excerpt_limit: int = 4000) -> None:
        if excerpt_limit <= 0:
            raise ValueError("excerpt_limit must be positive")
        self._artifact_root = Path(artifact_root)
        self._excerpt_limit = excerpt_limit

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
        command = _expand_command(gate.command)
        started = monotonic()
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=worktree,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        if process.stdout is None:  # pragma: no cover - guaranteed by PIPE above
            raise RuntimeError("verification process stdout pipe was not created")
        output_task = asyncio.create_task(process.stdout.read())
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=gate.timeout_seconds)
        except TimeoutError:
            timed_out = True
            process.kill()
            await process.wait()
        output = await output_task

        duration_ms = round((monotonic() - started) * 1000)
        output_text = _decode_output(output)
        artifact_path = self._artifact_path(task_id, attempt, gate.name)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(output_text, encoding="utf-8")
        exit_code = None if timed_out else process.returncode

        return GateResult(
            gate_name=gate.name,
            command=command,
            required=gate.required,
            passed=not timed_out and exit_code == 0,
            exit_code=exit_code,
            duration_ms=duration_ms,
            output_artifact=str(artifact_path.resolve()),
            output_excerpt=output_text[-self._excerpt_limit :],
            timed_out=timed_out,
        )

    def _artifact_path(self, task_id: str, attempt: int, gate_name: str) -> Path:
        return (
            self._artifact_root
            / _safe_segment(task_id, fallback="task")
            / f"attempt-{attempt}"
            / f"{_safe_segment(gate_name, fallback='gate')}.log"
        )


__all__ = ["VerificationRunner"]
