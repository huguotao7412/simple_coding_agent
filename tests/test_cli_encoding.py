from __future__ import annotations

import os
import sys

import pytest

from cli import encoding


class ReconfigurableStream:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def reconfigure(self, **kwargs: str) -> None:
        self.calls.append(kwargs)


def test_configure_stdio_encoding_reconfigures_all_standard_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    streams = [ReconfigurableStream(), ReconfigurableStream(), ReconfigurableStream()]
    monkeypatch.setattr(sys, "stdin", streams[0])
    monkeypatch.setattr(sys, "stdout", streams[1])
    monkeypatch.setattr(sys, "stderr", streams[2])
    monkeypatch.setattr(os, "name", "posix")

    encoding.configure_stdio_encoding()

    assert [stream.calls for stream in streams] == [
        [{"encoding": "utf-8", "errors": "replace"}],
        [{"encoding": "utf-8", "errors": "replace"}],
        [{"encoding": "utf-8", "errors": "replace"}],
    ]


def test_configure_stdio_encoding_switches_windows_console_to_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int] = []
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(encoding, "_set_windows_console_code_page", seen.append)

    encoding.configure_stdio_encoding()

    assert seen == [encoding.UTF8_CODE_PAGE]
