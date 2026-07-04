#!/usr/bin/env python3
"""
A simple interactive calculator with basic arithmetic operations.

Supports addition, subtraction, multiplication, division,
exponentiation, modulus, and floor division.

Usage:
    python calculator.py
    Enter expressions like: 5 + 3, 10 ** 2, 15 % 4
    Type 'quit' or 'exit' to terminate.
"""

from __future__ import annotations

import math
import operator
import sys
from typing import Callable

# ---------------------------------------------------------------------------
# Operation registry
# ---------------------------------------------------------------------------

OPERATIONS: dict[str, tuple[Callable[[float, float], float], str]] = {
    "+": (operator.add, "addition"),
    "-": (operator.sub, "subtraction"),
    "*": (operator.mul, "multiplication"),
    "/": (operator.truediv, "division"),
    "**": (operator.pow, "exponentiation"),
    "%": (operator.mod, "modulus"),
    "//": (operator.floordiv, "floor division"),
}


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def parse_expression(expression: str) -> tuple[float, str, float]:
    """Parse a string expression into (left_operand, operator_symbol, right_operand).

    Args:
        expression: Raw input string, e.g. '5 + 3'.

    Returns:
        A tuple of (left, operator, right).

    Raises:
        ValueError: If the expression cannot be parsed.
    """
    parts = expression.strip().split()
    if len(parts) != 3:
        raise ValueError(
            "Invalid format. Expected: <number> <operator> <number>"
        )

    left_str, op_symbol, right_str = parts

    if op_symbol not in OPERATIONS:
        raise ValueError(
            f"Unknown operator '{op_symbol}'. "
            f"Supported: {', '.join(OPERATIONS)}"
        )

    try:
        left = float(left_str)
    except ValueError:
        raise ValueError(f"Invalid left operand: '{left_str}'")

    try:
        right = float(right_str)
    except ValueError:
        raise ValueError(f"Invalid right operand: '{right_str}'")

    return left, op_symbol, right


def evaluate(left: float, op_symbol: str, right: float) -> float:
    """Perform the calculation.

    Args:
        left: Left-hand operand.
        op_symbol: Operator character.
        right: Right-hand operand.

    Returns:
        The computed result.

    Raises:
        ZeroDivisionError: If division or modulo by zero is attempted.
    """
    func, _ = OPERATIONS[op_symbol]
    if op_symbol in ("/", "//", "%") and right == 0:
        raise ZeroDivisionError("Division by zero is not allowed.")
    return func(left, right)


# ---------------------------------------------------------------------------
# REPL (Read-Eval-Print Loop)
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the interactive calculator REPL."""
    print("Simple Calculator")
    print("Supported operators:", ", ".join(OPERATIONS))
    print("Type 'quit' or 'exit' to stop.\n")

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if user_input.lower() in ("quit", "exit"):
            print("Goodbye!")
            break

        if not user_input:
            continue

        try:
