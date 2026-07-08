from __future__ import annotations

import argparse
from pathlib import Path

from .run_evals import copy_fixtures, evaluate_all, print_results


DEFAULT_CANDIDATE_ROOT = Path("tmp") / "eval-runs"


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

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
