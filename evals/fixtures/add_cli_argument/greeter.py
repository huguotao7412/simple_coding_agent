from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="world")
    return parser


def main(argv: list[str] | None = None) -> str:
    args = build_parser().parse_args(argv)
    return f"Hello, {args.name}!"


if __name__ == "__main__":
    print(main())
