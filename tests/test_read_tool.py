from __future__ import annotations

import asyncio
from pathlib import Path

from core.tools.read import ReadTool


def test_read_returns_line_addressable_source_slice(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    result = asyncio.run(
        ReadTool().execute(
            file_path="module.py",
            workspace_dir=str(tmp_path),
            offset=2,
            limit=2,
        )
    )

    assert result.success is True
    assert "module.py (lines 2-3 of 4)" in result.content
    assert "L2: two" in result.content
    assert "L3: three" in result.content
    assert "continue with offset=4" in result.content
    assert "one" not in result.content


def test_read_rejects_invalid_range_and_workspace_escape(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text("one\n", encoding="utf-8")
    tool = ReadTool()

    bad_offset = asyncio.run(
        tool.execute(
            file_path="module.py",
            workspace_dir=str(tmp_path),
            offset=0,
        )
    )
    escaped = asyncio.run(
        tool.execute(
            file_path=str(tmp_path.parent / "outside.py"),
            workspace_dir=str(tmp_path),
        )
    )

    assert bad_offset.success is False
    assert "offset must be at least 1" in (bad_offset.error or "")
    assert escaped.success is False
    assert "escapes workspace" in (escaped.error or "")


def test_read_caps_large_requested_ranges(tmp_path: Path) -> None:
    source = tmp_path / "large.py"
    source.write_text(
        "\n".join(f"line {index}" for index in range(1, 260)) + "\n",
        encoding="utf-8",
    )

    result = asyncio.run(
        ReadTool().execute(
            file_path="large.py",
            workspace_dir=str(tmp_path),
            offset=1,
            limit=500,
        )
    )

    assert result.success is True
    assert "large.py (lines 1-160 of 259)" in result.content
    assert "requested limit 500 capped at 160 lines" in result.content
    assert "continue with offset=161" in result.content
    assert "L161:" not in result.content
