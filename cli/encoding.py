from __future__ import annotations

import sys
from typing import Any


def configure_stdio_encoding(encoding: str = "utf-8") -> None:
    """Prefer UTF-8 for interactive CLI input and output."""
    # Python's Windows console streams already use Unicode console APIs.
    # Changing the process console code page also changes the parent terminal's
    # shared console and can corrupt subsequent PowerShell input/output.
    # Never silently replace malformed user input. Replacement characters make
    # it into the durable task plan and leave Actors working from a corrupted
    # objective. Output remains best-effort so diagnostics can still be shown.
    _reconfigure_stream(sys.stdin, encoding, errors="strict")
    for stream in (sys.stdout, sys.stderr):
        _reconfigure_stream(stream, encoding, errors="replace")


def _reconfigure_stream(stream: Any, encoding: str, *, errors: str) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding=encoding, errors=errors)
    except (OSError, ValueError):
        return
