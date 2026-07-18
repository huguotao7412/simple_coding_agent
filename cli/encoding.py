from __future__ import annotations

import os
import sys
from typing import Any


UTF8_CODE_PAGE = 65001


def configure_stdio_encoding(encoding: str = "utf-8") -> None:
    """Prefer UTF-8 for interactive CLI input and output."""
    if os.name == "nt":
        _set_windows_console_code_page(UTF8_CODE_PAGE)

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        _reconfigure_stream(stream, encoding)


def _reconfigure_stream(stream: Any, encoding: str) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding=encoding, errors="replace")
    except (OSError, ValueError):
        return


def _set_windows_console_code_page(code_page: int) -> None:
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetConsoleCP(code_page)
        kernel32.SetConsoleOutputCP(code_page)
    except (AttributeError, OSError, ValueError):
        return
