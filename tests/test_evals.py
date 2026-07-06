from __future__ import annotations

import subprocess
from pathlib import Path

from evals.cli import main as eval_cli_main
from evals.run_evals import copy_fixtures, evaluate_all, evaluate_task, load_tasks


def test_eval_suite_has_five_tasks():
    tasks = load_tasks()

    assert [task["id"] for task in tasks] == [
        "fix_failing_pytest",
        "add_type_hints",
        "refactor_small_module",
        "add_cli_argument",
        "update_readme_and_test",
    ]


def test_eval_runner_accepts_solved_candidates(tmp_path: Path):
    candidate_root = tmp_path / "candidates"
    _copy_all_fixtures(candidate_root)
    _solve_all(candidate_root)

    results = evaluate_all(candidate_root)

    assert all(result.passed for result in results), {
        result.task_id: result.failures
        for result in results
        if not result.passed
    }


def test_eval_runner_rejects_changed_files_outside_allowlist(tmp_path: Path):
    candidate_root = tmp_path / "candidates"
    _copy_all_fixtures(candidate_root)
    _solve_fix_failing_pytest(candidate_root / "fix_failing_pytest")
    (candidate_root / "fix_failing_pytest" / "unexpected.txt").write_text("oops", encoding="utf-8")

    task = next(task for task in load_tasks() if task["id"] == "fix_failing_pytest")
    result = evaluate_task(task, candidate_root)

    assert not result.passed
    assert any("outside allowlist" in failure for failure in result.failures)


def test_sca_eval_prepare_command_copies_fixtures(tmp_path: Path):
    candidate_root = tmp_path / "eval-runs"

    exit_code = eval_cli_main(["prepare", "--candidate-root", str(candidate_root)])

    assert exit_code == 0
    assert (candidate_root / "fix_failing_pytest" / "math_utils.py").is_file()
    assert (candidate_root / "fix_failing_pytest" / ".git").is_dir()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=candidate_root / "fix_failing_pytest",
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert status.returncode == 0
    assert status.stdout == ""


def _copy_all_fixtures(candidate_root: Path) -> None:
    copy_fixtures(candidate_root)


def _solve_all(candidate_root: Path) -> None:
    _solve_fix_failing_pytest(candidate_root / "fix_failing_pytest")
    _solve_add_type_hints(candidate_root / "add_type_hints")
    _solve_refactor_small_module(candidate_root / "refactor_small_module")
    _solve_add_cli_argument(candidate_root / "add_cli_argument")
    _solve_update_readme_and_test(candidate_root / "update_readme_and_test")


def _write_report(task_dir: Path) -> None:
    report_dir = task_dir / ".sca"
    report_dir.mkdir(exist_ok=True)
    (report_dir / "final_report.md").write_text(
        "Files changed: listed\nTests: passed\nRisk: low\n",
        encoding="utf-8",
    )


def _solve_fix_failing_pytest(task_dir: Path) -> None:
    (task_dir / "math_utils.py").write_text(
        "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    _write_report(task_dir)


def _solve_add_type_hints(task_dir: Path) -> None:
    (task_dir / "formatter.py").write_text(
        "def format_user(name: str, age: int) -> str:\n"
        "    return f\"{name} ({age})\"\n\n\n"
        "def initials(full_name: str) -> str:\n"
        "    return \"\".join(part[0].upper() for part in full_name.split())\n",
        encoding="utf-8",
    )
    _write_report(task_dir)


def _solve_refactor_small_module(task_dir: Path) -> None:
    (task_dir / "pricing.py").write_text(
        "TAX_RATE = 0.08\n\n\n"
        "def line_total(item):\n"
        "    return item[\"quantity\"] * item[\"unit_price\"]\n\n\n"
        "def total_with_tax(items):\n"
        "    subtotal = sum(line_total(item) for item in items)\n"
        "    return round(subtotal + subtotal * TAX_RATE, 2)\n\n\n"
        "def invoice_total(items):\n"
        "    return total_with_tax(items)\n\n\n"
        "def cart_total(items):\n"
        "    return total_with_tax(items)\n",
        encoding="utf-8",
    )
    _write_report(task_dir)


def _solve_add_cli_argument(task_dir: Path) -> None:
    (task_dir / "greeter.py").write_text(
        "from __future__ import annotations\n\n"
        "import argparse\n\n\n"
        "def build_parser() -> argparse.ArgumentParser:\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument(\"--name\", default=\"world\")\n"
        "    parser.add_argument(\"--shout\", action=\"store_true\")\n"
        "    return parser\n\n\n"
        "def main(argv: list[str] | None = None) -> str:\n"
        "    args = build_parser().parse_args(argv)\n"
        "    greeting = f\"Hello, {args.name}!\"\n"
        "    if args.shout:\n"
        "        return greeting.upper()\n"
        "    return greeting\n\n\n"
        "if __name__ == \"__main__\":\n"
        "    print(main())\n",
        encoding="utf-8",
    )
    _write_report(task_dir)


def _solve_update_readme_and_test(task_dir: Path) -> None:
    (task_dir / "README.md").write_text(
        "# Demo CLI\n\n"
        "Run the tool with a name:\n\n"
        "```bash\n"
        "python app.py --name Ada\n"
        "```\n",
        encoding="utf-8",
    )
    (task_dir / "test_docs.py").write_text(
        "from pathlib import Path\n\n\n"
        "def test_readme_documents_name_option():\n"
        "    readme = Path(\"README.md\").read_text(encoding=\"utf-8\")\n"
        "    assert \"--name\" in readme\n"
        "    assert \"python app.py --name Ada\" in readme\n",
        encoding="utf-8",
    )
    _write_report(task_dir)
