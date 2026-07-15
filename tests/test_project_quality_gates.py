from __future__ import annotations

import tomllib
from pathlib import Path

from core.verification.config import load_verification_config


ROOT = Path(__file__).resolve().parents[1]


def test_pytest_excludes_generated_eval_workspaces() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    ignored = config["tool"]["pytest"]["ini_options"]["norecursedirs"]

    assert "tmp" in ignored


def test_ci_verifies_test_inventory_before_running_tests() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    inventory_step = workflow.find("- name: Verify test suite inventory")
    unit_test_step = workflow.find("- name: Run unit tests")

    assert inventory_step >= 0
    assert unit_test_step >= 0
    assert inventory_step < unit_test_step
    assert "expected at least 8 test modules" in workflow


def test_actor_executor_modules_are_in_trusted_mypy_boundary() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    trusted_modules = {
        "core/actors/contracts.py",
        "core/actors/worktree.py",
        "core/verification",
        "core/sandbox",
        "core/tools/sandbox_run.py",
    }

    configured_files = set(config["tool"]["mypy"]["files"])

    assert trusted_modules <= configured_files
    for module in trusted_modules:
        assert module in workflow


def test_repository_dogfoods_deterministic_quality_gates() -> None:
    config = load_verification_config(ROOT)

    assert config.enabled
    assert [gate.name for gate in config.gates] == ["unit", "types"]
    assert all(gate.required for gate in config.gates)
