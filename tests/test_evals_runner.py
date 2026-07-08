from __future__ import annotations

import json
import sys
from pathlib import Path

from evals.run_evals import discover_cases, run_case, write_reports


def test_discover_cases_loads_sample_case():
    cases = discover_cases(Path("evals/cases"), selected_case="fix_failing_pytest")

    assert len(cases) == 1
    assert cases[0].name == "fix_failing_pytest"
    assert cases[0].verification_commands == ["python -m pytest -q"]


def test_dry_run_writes_reports(tmp_path):
    case = discover_cases(Path("evals/cases"), selected_case="fix_failing_pytest")[0]
    result = run_case(case, reports_dir=tmp_path, dry_run=True)

    json_path, md_path = write_reports([result], tmp_path)

    assert result.passed is True
    assert json_path.exists()
    assert md_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["total"] == 1
    assert payload["summary"]["passed"] == 1


def test_verification_success_case_passes(tmp_path):
    case_dir = _write_case(
        tmp_path,
        name="passing_case",
        source="def add(a, b):\n    return a + b\n",
        test_source=(
            "from sample import add\n\n"
            "def test_add():\n"
            "    assert add(2, 3) == 5\n"
        ),
    )
    case = discover_cases(tmp_path / "cases", selected_case="passing_case")[0]

    result = run_case(case, reports_dir=tmp_path / "reports")

    assert result.passed is True
    assert result.verification_passed is True
    assert result.verification_results[0].returncode == 0


def test_verification_failure_case_fails(tmp_path):
    case_dir = _write_case(
        tmp_path,
        name="failing_case",
        source="def add(a, b):\n    return a - b\n",
        test_source=(
            "from sample import add\n\n"
            "def test_add():\n"
            "    assert add(2, 3) == 5\n"
        ),
    )
    case = discover_cases(tmp_path / "cases", selected_case=case_dir.name)[0]

    result = run_case(case, reports_dir=tmp_path / "reports")

    assert result.passed is False
    assert result.verification_passed is False
    assert result.verification_results[0].returncode != 0


def test_agent_command_can_modify_workspace(tmp_path):
    case_dir = _write_case(
        tmp_path,
        name="agent_fix_case",
        source="def add(a, b):\n    return a - b\n",
        test_source=(
            "from sample import add\n\n"
            "def test_add():\n"
            "    assert add(2, 3) == 5\n"
        ),
    )
    case = discover_cases(tmp_path / "cases", selected_case=case_dir.name)[0]
    command = (
        f'"{sys.executable}" -c '
        '"from pathlib import Path; '
        "p=Path(r'{workspace}')/'sample.py'; "
        "p.write_text('def add(a, b):\\n    return a + b\\n', encoding='utf-8')\""
    )

    result = run_case(
        case,
        reports_dir=tmp_path / "reports",
        agent_command=command,
    )

    assert result.passed is True
    assert result.changed_files == ["sample.py"]


def _write_case(tmp_path, name: str, source: str, test_source: str) -> Path:
    case_dir = tmp_path / "cases" / name
    repo_dir = case_dir / "repo"
    repo_dir.mkdir(parents=True)
    (case_dir / "eval.json").write_text(
        json.dumps(
            {
                "name": name,
                "prompt": "Fix the tests.",
                "verification_commands": [f'"{sys.executable}" -m pytest -q'],
                "expected_files": ["sample.py", "test_sample.py"],
                "timeout_seconds": 30,
            }
        ),
        encoding="utf-8",
    )
    (repo_dir / "sample.py").write_text(source, encoding="utf-8")
    (repo_dir / "test_sample.py").write_text(test_source, encoding="utf-8")
    return case_dir
