from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = REPO_ROOT / "core"
LEGACY_MODULES = {
    "core.actor_execution",
    "core.agent",
    "core.context",
    "core.role_config",
    "core.run_context",
    "core.run_state",
    "core.run_store",
    "core.runtime",
    "core.sqlite_run_store",
    "core.state",
    "core.worktree_actor_executor",
}
LEGACY_FILES = {
    f"{module.rsplit('.', 1)[-1]}.py" for module in LEGACY_MODULES
}


def test_core_uses_cohesive_runtime_runs_and_actors_packages() -> None:
    for package in ("runtime", "runs", "actors"):
        package_dir = CORE_ROOT / package
        assert package_dir.is_dir(), f"missing core package: {package}"
        assert (package_dir / "__init__.py").is_file()

    flat_modules = {path.name for path in CORE_ROOT.glob("*.py")}
    assert not (flat_modules & LEGACY_FILES)


def test_python_consumers_do_not_import_legacy_flat_core_modules() -> None:
    violations: list[str] = []
    for area in ("core", "cli", "web", "evals", "tests"):
        for path in (REPO_ROOT / area).rglob("*.py"):
            if path == Path(__file__):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                imported: list[str] = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.append(node.module)
                elif isinstance(node, ast.Import):
                    imported.extend(alias.name for alias in node.names)
                for module in imported:
                    if module in LEGACY_MODULES:
                        relative = path.relative_to(REPO_ROOT).as_posix()
                        violations.append(f"{relative}:{node.lineno} imports {module}")
    assert violations == []
