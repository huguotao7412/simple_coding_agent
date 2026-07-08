from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .run_evals import (
    copy_fixtures,
    write_eval_comparison,
    evaluate_all,
    print_results,
    print_run_results,
    run_eval_suite,
)


DEFAULT_CANDIDATE_ROOT = Path("tmp") / "eval-runs"
DEFAULT_RESULTS_PATH = Path("eval_results.json")
DEFAULT_COMPARE_PATH = Path("eval_comparison.md")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sca-eval",
        description="Prepare and check local Simple Coding Agent eval tasks.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Copy fresh eval fixtures into a candidate directory.")
    prepare.add_argument(
        "--candidate-root",
        type=Path,
        default=DEFAULT_CANDIDATE_ROOT,
        help=f"Destination directory. Default: {DEFAULT_CANDIDATE_ROOT}",
    )

    check = subparsers.add_parser("check", help="Check completed candidate workspaces.")
    check.add_argument(
        "--candidate-root",
        type=Path,
        default=DEFAULT_CANDIDATE_ROOT,
        help=f"Candidate directory. Default: {DEFAULT_CANDIDATE_ROOT}",
    )

    run = subparsers.add_parser("run", help="Run the agent against all eval fixtures and check results.")
    run.add_argument(
        "--candidate-root",
        type=Path,
        default=DEFAULT_CANDIDATE_ROOT,
        help=f"Candidate directory. Default: {DEFAULT_CANDIDATE_ROOT}",
    )
    run.add_argument("--model", default=None, help="Model name (overrides .env).")
    run.add_argument(
        "--results-path",
        type=Path,
        default=DEFAULT_RESULTS_PATH,
        help=f"Aggregate JSON output path. Default: {DEFAULT_RESULTS_PATH}",
    )
    run.add_argument(
        "--no-prepare",
        action="store_true",
        help="Reuse existing candidate workspaces instead of copying fresh fixtures first.",
    )

    compare = subparsers.add_parser("compare", help="Compare two or more eval_results.json files.")
    compare.add_argument(
        "results",
        nargs="+",
        type=Path,
        help="Eval result JSON files. The first file is the baseline.",
    )
    compare.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_COMPARE_PATH,
        help=f"Markdown comparison report path. Default: {DEFAULT_COMPARE_PATH}",
    )

    args = parser.parse_args(argv)

    if args.command == "prepare":
        copy_fixtures(args.candidate_root)
        print(f"Copied eval fixtures to {args.candidate_root}")
        print("Next: run `sca --dir <candidate-task-dir>` for each task, then `sca-eval check`.")
        return 0

    if args.command == "check":
        results = evaluate_all(args.candidate_root)
        print_results(results)
        return 0 if all(result.passed for result in results) else 1

    if args.command == "run":
        results = asyncio.run(
            run_eval_suite(
                candidate_root=args.candidate_root,
                model=args.model,
                results_path=args.results_path,
                prepare=not args.no_prepare,
            )
        )
        print_run_results(results, args.results_path)
        return 0 if all(result.passed for result in results) else 1

    if args.command == "compare":
        try:
            output_path = write_eval_comparison(args.results, args.output)
        except (FileNotFoundError, ValueError) as e:
            print(f"Error: {e}")
            return 1
        print(f"Wrote eval comparison to {output_path}")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
