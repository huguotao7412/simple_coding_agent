from __future__ import annotations

from collections.abc import Callable
from typing import Any


def build_message_payload(
    messages: list[dict[str, Any]],
    dynamic_context_builder: Callable[[], dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if dynamic_context_builder is None:
        return messages
    return messages + [dynamic_context_builder()]


__all__ = ["build_message_payload"]
