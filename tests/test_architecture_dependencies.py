from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_domain_has_no_framework_or_adapter_dependencies() -> None:
    banned = ("langgraph", "sqlite3", "guardrails", "mcp", "e2b", "core.adapters")
    for path in (ROOT / "core" / "domain").glob("*.py"):
        found = imports(path)
        assert not any(name.startswith(banned) for name in found), (path, found)


def test_ports_do_not_depend_on_adapters() -> None:
    for path in (ROOT / "core" / "ports").glob("*.py"):
        assert not any("adapters" in name for name in imports(path)), path


def test_third_party_imports_stay_at_adapter_boundaries() -> None:
    sqlite_importers: list[Path] = []
    langgraph_importers: list[Path] = []
    for path in (ROOT / "core").rglob("*.py"):
        found = imports(path)
        if "sqlite3" in found:
            sqlite_importers.append(path)
        if any(name.startswith("langgraph") for name in found):
            langgraph_importers.append(path)
    assert all("adapters/sqlite" in path.as_posix() for path in sqlite_importers)
    assert all("orchestration" in path.as_posix() for path in langgraph_importers)


def test_compatibility_facades_are_thin() -> None:
    facades = {
        "core/orchestration/langgraph.py": 20,
        "core/runtime/engine.py": 30,
        "core/runs/sqlite_store.py": 20,
        "core/security/openai_guard.py": 20,
    }
    for relative, maximum in facades.items():
        path = ROOT / relative
        assert len(path.read_text(encoding="utf-8").splitlines()) <= maximum


def test_guardrails_adapter_can_import_without_compatibility_cycle() -> None:
    from core.adapters.openai_guardrails.provider import OpenAIGuardrailsProvider

    assert OpenAIGuardrailsProvider.name == "openai_guardrails"
