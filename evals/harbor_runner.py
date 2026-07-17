from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from evals.harbor_support import (
    DEFAULT_HARBOR_DATASET,
    HARBOR_AGENT_IMPORT,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WHEEL_DIR = REPO_ROOT / "tmp" / "harbor-wheel"


class HarborSetupError(RuntimeError):
    pass


def build_project_wheel(output_dir: Path = DEFAULT_WHEEL_DIR) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    before = set(output_dir.glob("simple_coding_agent-*.whl"))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(output_dir),
            str(REPO_ROOT),
        ],
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=180,
    )
    if result.returncode != 0:
        detail = (result.stdout + "\n" + result.stderr).strip()
        raise HarborSetupError(f"failed to build SCA wheel:\n{detail[-2000:]}")
    wheels = set(output_dir.glob("simple_coding_agent-*.whl"))
    created = sorted(wheels - before, key=lambda path: path.stat().st_mtime)
    candidates = created or sorted(wheels, key=lambda path: path.stat().st_mtime)
    if not candidates:
        raise HarborSetupError(f"wheel build produced no artifact in {output_dir}")
    return candidates[-1].resolve()


def resolve_harbor_executable() -> str:
    sibling_name = "harbor.exe" if os.name == "nt" else "harbor"
    sibling = Path(sys.executable).with_name(sibling_name)
    if sibling.is_file():
        return str(sibling)
    executable = shutil.which("harbor")
    if executable is None:
        raise HarborSetupError(
            "Harbor is not installed. Install benchmark dependencies with "
            "`python -m pip install -e .[benchmark]`."
        )
    return executable


def build_harbor_command(
    *,
    executable: str,
    dataset: str = DEFAULT_HARBOR_DATASET,
    model: str,
    concurrency: int = 4,
    extra_args: list[str] | None = None,
) -> list[str]:
    if concurrency < 1:
        raise ValueError("Harbor concurrency must be at least 1")
    if not dataset.strip():
        raise ValueError("Harbor dataset must not be empty")
    if not model.strip():
        raise ValueError("Harbor model must not be empty")
    command = [
        executable,
        "run",
        "--dataset",
        dataset,
        "--model",
        model,
        "--agent",
        HARBOR_AGENT_IMPORT,
        "--n-concurrent",
        str(concurrency),
    ]
    command.extend(extra_args or [])
    return command


def run_harbor(
    *,
    dataset: str,
    model: str,
    concurrency: int,
    wheel: Path | None = None,
    wheel_dir: Path = DEFAULT_WHEEL_DIR,
    extra_args: list[str] | None = None,
) -> int:
    resolved_wheel = wheel.resolve() if wheel is not None else build_project_wheel(wheel_dir)
    if not resolved_wheel.is_file():
        raise HarborSetupError(f"SCA wheel does not exist: {resolved_wheel}")
    command = build_harbor_command(
        executable=resolve_harbor_executable(),
        dataset=dataset,
        model=model,
        concurrency=concurrency,
        extra_args=extra_args,
    )
    env = os.environ.copy()
    env["SCA_HARBOR_WHEEL"] = str(resolved_wheel)
    print(f"Using SCA wheel: {resolved_wheel}")
    print(f"Running Harbor dataset: {dataset}")
    return subprocess.run(command, cwd=REPO_ROOT, env=env).returncode


__all__ = [
    "DEFAULT_WHEEL_DIR",
    "HarborSetupError",
    "build_harbor_command",
    "build_project_wheel",
    "resolve_harbor_executable",
    "run_harbor",
]
