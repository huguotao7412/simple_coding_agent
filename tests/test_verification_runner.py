from __future__ import annotations

import sys
from pathlib import Path

import pytest

from core.verification.models import GateSpec, VerificationConfig
from core.verification.runner import VerificationRunner
from core.sandbox.contracts import SandboxExecutionResult


@pytest.mark.asyncio
async def test_runner_executes_in_worktree_and_writes_complete_artifact(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    artifact_root = tmp_path / "artifacts"
    gate = GateSpec(
        name="cwd",
        command=("{python}", "-c", "import os; print(os.getcwd()); print('x' * 200)"),
        timeout_seconds=5,
    )
    runner = VerificationRunner(artifact_root=artifact_root, excerpt_limit=80)

    report = await runner.run(
        VerificationConfig(gates=(gate,)),
        worktree=worktree,
        task_id="task/unsafe",
        attempt=1,
    )

    result = report.results[0]
    assert report.passed
    assert result.passed
    assert result.command[0] == sys.executable
    assert len(result.output_excerpt) <= 80
    artifact = Path(result.output_artifact)
    assert artifact.is_file()
    complete_output = artifact.read_text(encoding="utf-8")
    assert str(worktree) in complete_output
    assert "x" * 200 in complete_output
    assert artifact.relative_to(artifact_root).parts[:2] == ("task_unsafe", "attempt-1")


@pytest.mark.asyncio
async def test_runner_reports_nonzero_exit_and_runs_gates_in_order(tmp_path: Path) -> None:
    config = VerificationConfig(
        gates=(
            GateSpec("failure", ("{python}", "-c", "import sys; print('bad'); sys.exit(7)")),
            GateSpec("success", ("{python}", "-c", "print('good')")),
        )
    )
    runner = VerificationRunner(artifact_root=tmp_path / "artifacts")

    report = await runner.run(config, worktree=tmp_path, task_id="task", attempt=2)

    assert [result.gate_name for result in report.results] == ["failure", "success"]
    assert report.results[0].exit_code == 7
    assert report.results[0].output_excerpt.strip() == "bad"
    assert report.results[1].passed
    assert not report.passed
    assert report.failure_fingerprint


@pytest.mark.asyncio
async def test_optional_gate_does_not_fail_report(tmp_path: Path) -> None:
    config = VerificationConfig(
        gates=(
            GateSpec(
                "optional",
                ("{python}", "-c", "raise SystemExit(1)"),
                required=False,
            ),
        )
    )

    report = await VerificationRunner(artifact_root=tmp_path / "artifacts").run(
        config,
        worktree=tmp_path,
        task_id="task",
        attempt=1,
    )

    assert report.passed
    assert not report.results[0].passed


@pytest.mark.asyncio
async def test_runner_times_out_and_terminates_process(tmp_path: Path) -> None:
    gate = GateSpec(
        "slow",
        ("{python}", "-c", "import time; print('started', flush=True); time.sleep(5)"),
        timeout_seconds=0.2,
    )

    report = await VerificationRunner(artifact_root=tmp_path / "artifacts").run(
        VerificationConfig(gates=(gate,)),
        worktree=tmp_path,
        task_id="task",
        attempt=1,
    )

    result = report.results[0]
    assert not report.passed
    assert result.timed_out
    assert result.exit_code is None
    assert "started" in Path(result.output_artifact).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_runner_uses_injected_isolated_backend_and_container_python(
    tmp_path: Path,
) -> None:
    requests = []

    class FakeBackend:
        name = "e2b"
        isolated = True
        python_executable = "python"

        async def ensure_available(self):
            return None

        async def execute(self, request):
            requests.append(request)
            return SandboxExecutionResult(
                backend=self.name,
                isolated=self.isolated,
                command=request.command,
                exit_code=0,
                output="isolated pass",
                duration_ms=8,
            )

    runner = VerificationRunner(
        artifact_root=tmp_path / "artifacts",
        sandbox_backend=FakeBackend(),
    )
    report = await runner.run(
        VerificationConfig(gates=(GateSpec("unit", ("{python}", "-m", "pytest")),)),
        worktree=tmp_path,
        task_id="task",
        attempt=1,
    )

    assert requests[0].command == ("python", "-m", "pytest")
    assert report.results[0].execution_backend == "e2b"
    assert report.results[0].isolated is True


@pytest.mark.asyncio
async def test_runner_reauthorizes_final_verification_command_before_backend(
    tmp_path: Path,
) -> None:
    requests = []

    class FakeBackend:
        name = "fake"
        isolated = True
        python_executable = "python"

        async def ensure_available(self):
            return None

        async def execute(self, request):
            requests.append(request)
            raise AssertionError("denied verification command must not execute")

    runner = VerificationRunner(
        artifact_root=tmp_path / "artifacts",
        sandbox_backend=FakeBackend(),
    )
    report = await runner.run(
        VerificationConfig(gates=(
            GateSpec("network", ("curl", "https://example.test")),
        )),
        worktree=tmp_path,
        task_id="task",
        attempt=1,
    )

    assert requests == []
    assert not report.passed
    assert "denied by security policy" in report.results[0].output_excerpt
