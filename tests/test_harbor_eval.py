from __future__ import annotations

import json
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals.harbor_runner import build_harbor_command
from evals.cli import main as eval_cli_main
from evals.harbor_entrypoint import (
    HarborTaskAssessor,
    build_harbor_task_prompt,
    run_harbor_agent,
)
from evals.harbor_support import (
    HARBOR_AGENT_IMPORT,
    apply_summary_to_context,
    container_environment,
    load_run_summary,
    normalize_harbor_model,
)
from core.execution.assessment import TaskAssessor
from core.execution.models import ExecutionStrategy, TaskComplexity, TaskIntent, TaskRisk


def test_normalize_harbor_model_strips_provider_prefix():
    assert normalize_harbor_model("deepseek/deepseek-v4-pro") == "deepseek-v4-pro"
    assert normalize_harbor_model("custom-model") == "custom-model"


@pytest.mark.parametrize("value", [None, "", "provider/"])
def test_normalize_harbor_model_rejects_empty_values(value: str | None):
    with pytest.raises(ValueError):
        normalize_harbor_model(value)


def test_container_environment_forces_harbor_to_own_isolation():
    result = container_environment(
        "deepseek/deepseek-v4-pro",
        {
            "SCA_API_KEY": "secret",
            "SCA_API_BASE": "https://example.invalid/v1",
            "SCA_SANDBOX_BACKEND": "e2b",
            "SCA_STATE_HOME": "/host/state",
            "SCA_HARBOR_WHEEL": "/host/sca.whl",
            "HTTP_PROXY": "http://127.0.0.1:7890",
            "https_proxy": "http://localhost:7890",
            "NO_PROXY": "localhost,127.0.0.1",
            "UNRELATED": "ignored",
        },
    )

    assert result == {
        "SCA_API_KEY": "secret",
        "SCA_API_BASE": "https://example.invalid/v1",
        "SCA_SANDBOX_BACKEND": "local",
        "SCA_STATE_HOME": "/logs/artifacts/sca",
        "SCA_MODEL": "deepseek-v4-pro",
        "HTTP_PROXY": "http://host.docker.internal:7890",
        "https_proxy": "http://host.docker.internal:7890",
        "NO_PROXY": "localhost,127.0.0.1",
    }


def test_load_summary_and_apply_it_to_harbor_context(tmp_path: Path):
    summary_path = tmp_path / "sca-run.json"
    summary_path.write_text(json.dumps({
        "schema_version": 1,
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "duration_ms": 123,
        "tool_calls": 4,
        "failed_tool_calls": 1,
        "usage_estimated": False,
        "runtime_error": None,
    }), encoding="utf-8")
    context = SimpleNamespace(
        n_input_tokens=None,
        n_output_tokens=None,
        metadata={"existing": True},
    )

    apply_summary_to_context(context, load_run_summary(summary_path))

    assert context.n_input_tokens == 12
    assert context.n_output_tokens == 5
    assert context.metadata["existing"] is True
    assert context.metadata["simple_coding_agent"]["tool_calls"] == 4


def test_load_summary_rejects_unknown_schema(tmp_path: Path):
    path = tmp_path / "summary.json"
    path.write_text('{"schema_version": 2}', encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported"):
        load_run_summary(path)


def test_build_harbor_command_uses_import_path_agent():
    command = build_harbor_command(
        executable="harbor",
        dataset="swe-rebench/swe-rebench-leaderboard",
        model="deepseek/deepseek-v4-pro",
        concurrency=3,
        extra_args=["--include-task-name", "demo"],
    )

    assert command[:2] == ["harbor", "run"]
    assert command[command.index("--agent") + 1] == HARBOR_AGENT_IMPORT
    assert command[command.index("--n-concurrent") + 1] == "3"
    assert command[-2:] == ["--include-task-name", "demo"]


def test_harbor_prompt_frames_issue_as_code_change(tmp_path: Path):
    prompt = build_harbor_task_prompt(
        "[Bug]: Connection failed\n\nCan anyone tell me why this happens?"
    )

    assert "Implement the necessary code changes" in prompt
    assert "Can anyone tell me why this happens?" in prompt
    assert TaskAssessor(tmp_path).assess(prompt).intent is TaskIntent.CODE_CHANGE


def test_harbor_task_assessor_uses_single_coder_for_benchmark_issue(tmp_path: Path):
    prompt = build_harbor_task_prompt(
        "[Bug]: broken\n"
        "Traceback in litellm/llms/vertex_ai/gemini/foo.py and tests/a/b.py"
    )

    assessment = HarborTaskAssessor(tmp_path).assess(prompt)

    assert assessment.intent is TaskIntent.CODE_CHANGE
    assert assessment.strategy is ExecutionStrategy.SINGLE_ACTOR
    assert assessment.complexity is TaskComplexity.MEDIUM
    assert assessment.risk is TaskRisk.LOW
    assert assessment.max_actors == 1
    assert assessment.requires_human_approval is False


def test_harbor_cli_loads_runtime_environment(monkeypatch, tmp_path: Path):
    loaded: list[Path] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "core.config.load_runtime_environment",
        lambda workspace: loaded.append(Path(workspace)),
    )
    monkeypatch.setattr("evals.harbor_runner.run_harbor", lambda **kwargs: 0)

    assert eval_cli_main(["harbor", "--model", "deepseek/demo"]) == 0
    assert loaded == [tmp_path]


def test_harbor_entrypoint_writes_trace_summary_and_report(monkeypatch, tmp_path: Path):
    from core.events import AgentEvent

    class FakePlanner:
        def __init__(self, task_assessor):
            self.task_assessor = task_assessor

        async def run_stream(self, instruction: str):
            assert isinstance(self.task_assessor, HarborTaskAssessor)
            assert "Implement the necessary code changes" in instruction
            assert "Benchmark issue:\nFix the repository" in instruction
            yield AgentEvent(type="tool_call", tool_name="read", tool_args={"path": "app.py"})
            yield AgentEvent(
                type="token_stats",
                prompt_tokens=11,
                completion_tokens=7,
                usage_estimated=False,
            )
            yield AgentEvent(type="done", content="completed")

    monkeypatch.setattr(
        "evals.harbor_entrypoint.build_planner",
        lambda workspace, model=None, task_assessor=None: FakePlanner(task_assessor),
    )
    workspace = tmp_path / "workspace"
    logs = tmp_path / "logs"
    artifacts = tmp_path / "artifacts"
    workspace.mkdir()

    exit_code, summary = asyncio.run(run_harbor_agent(
        "Fix the repository",
        workspace=workspace,
        agent_log_dir=logs,
        artifact_dir=artifacts,
        model="demo-model",
    ))

    assert exit_code == 0
    assert summary["prompt_tokens"] == 11
    assert summary["completion_tokens"] == 7
    assert summary["final_output"] == "completed"
    assert (logs / "run-trace.jsonl").is_file()
    assert (logs / "sca-run.json").is_file()
    assert (artifacts / "final_report.md").is_file()


def test_harbor_adapter_imports_when_optional_dependency_is_installed():
    pytest.importorskip("harbor")
    from evals.harbor_agent import SimpleCodingAgent, _remote_wheel_path, _run_command

    assert SimpleCodingAgent.name() == "simple-coding-agent"
    assert _remote_wheel_path(
        Path("simple_coding_agent-0.1.0-py3-none-any.whl")
    ) == "/installed-agent/simple_coding_agent-0.1.0-py3-none-any.whl"
    command = _run_command()
    assert command.index("/testbed") < command.index("/app")
    assert '--workspace "$workspace"' in command
    assert "--workspace /app" not in command
