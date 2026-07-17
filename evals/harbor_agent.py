from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import override

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from evals.harbor_support import (
    PROXY_ENVIRONMENT_KEYS,
    SUMMARY_FILENAME,
    apply_summary_to_context,
    container_environment,
    load_run_summary,
)


REMOTE_ROOT = "/installed-agent"
REMOTE_VENV = f"{REMOTE_ROOT}/venv"
REMOTE_INSTRUCTION = f"{REMOTE_ROOT}/instruction.md"
WORKSPACE_CANDIDATES = ("/testbed", "/app", "/workspace", "/repo")


def _remote_wheel_path(wheel_path: Path) -> str:
    """Keep the valid PEP 427 filename when uploading a wheel."""
    return f"{REMOTE_ROOT}/{wheel_path.name}"


def _run_command() -> str:
    candidates = " ".join(shlex.quote(path) for path in WORKSPACE_CANDIDATES)
    return (
        "set -euo pipefail; workspace=''; "
        f"for candidate in {candidates}; do "
        "if [ -d \"$candidate/.git\" ]; then workspace=$candidate; break; fi; "
        "done; "
        f"if [ -z \"$workspace\" ]; then for candidate in {candidates}; do "
        "if [ -d \"$candidate\" ]; then workspace=$candidate; break; fi; done; fi; "
        "if [ -z \"$workspace\" ]; then "
        "echo 'Could not locate the Harbor task workspace' >&2; exit 1; fi; "
        f"exec {REMOTE_VENV}/bin/sca-harbor-agent "
        f"--instruction-file {REMOTE_INSTRUCTION} --workspace \"$workspace\""
    )


class SimpleCodingAgent(BaseInstalledAgent):
    """Harbor adapter that installs and runs the current SCA project wheel."""

    @staticmethod
    @override
    def name() -> str:
        return "simple-coding-agent"

    @override
    def get_version_command(self) -> str | None:
        return (
            f"{REMOTE_VENV}/bin/python -c \"import importlib.metadata as m; "
            "print(m.version('simple-coding-agent'))\""
        )

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        wheel_value = self._get_env("SCA_HARBOR_WHEEL")
        if not wheel_value:
            raise RuntimeError(
                "SCA_HARBOR_WHEEL is missing. Run Harbor through `sca-eval harbor` "
                "or point it at a built project wheel."
            )
        wheel_path = Path(wheel_value).expanduser().resolve()
        if not wheel_path.is_file():
            raise RuntimeError(f"SCA Harbor wheel does not exist: {wheel_path}")

        host_environment = {**os.environ, **self.extra_env}
        install_environment = {
            key: value
            for key, value in container_environment(
                self.model_name, host_environment
            ).items()
            if key in PROXY_ENVIRONMENT_KEYS
        }
        remote_wheel = _remote_wheel_path(wheel_path)
        await environment.upload_file(wheel_path, remote_wheel)
        await self.exec_as_root(
            environment,
            command=(
                f"chmod 777 {REMOTE_ROOT} && "
                "if ! command -v curl >/dev/null 2>&1 || "
                "! command -v git >/dev/null 2>&1; then "
                "command -v apt-get >/dev/null 2>&1 || "
                "{ echo 'curl and git are required to install SCA'; exit 1; }; "
                "apt-get update && DEBIAN_FRONTEND=noninteractive "
                "apt-get install -y curl git ca-certificates; fi"
            ),
            env=install_environment,
        )
        await self.exec_as_agent(
            environment,
            command=(
                f"set -euo pipefail; mkdir -p {REMOTE_ROOT}/bin; "
                f"export UV_INSTALL_DIR={REMOTE_ROOT}/bin; "
                "curl -LsSf https://astral.sh/uv/install.sh | sh; "
                f"{REMOTE_ROOT}/bin/uv venv --python 3.12 {REMOTE_VENV}; "
                f"{REMOTE_ROOT}/bin/uv pip install --python "
                f"{REMOTE_VENV}/bin/python {shlex.quote(remote_wheel)}"
            ),
            env=install_environment,
        )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        local_instruction = self.logs_dir / "instruction.md"
        local_instruction.parent.mkdir(parents=True, exist_ok=True)
        local_instruction.write_text(instruction, encoding="utf-8")
        await environment.upload_file(local_instruction, REMOTE_INSTRUCTION)
        host_environment = {**os.environ, **self.extra_env}
        await self.exec_as_agent(
            environment,
            command=_run_command(),
            env=container_environment(self.model_name, host_environment),
        )

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        summary_path = self.logs_dir / SUMMARY_FILENAME
        if not summary_path.is_file():
            context.metadata = {
                **(context.metadata or {}),
                "simple_coding_agent": {
                    "summary_error": f"missing {SUMMARY_FILENAME}",
                },
            }
            return
        apply_summary_to_context(context, load_run_summary(summary_path))


__all__ = ["SimpleCodingAgent"]
