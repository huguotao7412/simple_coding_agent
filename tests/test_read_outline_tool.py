from __future__ import annotations

import asyncio
from pathlib import Path

from core.tools.read_outline import ReadOutlineTool


def test_read_outline_accepts_offset_and_limit(tmp_path: Path) -> None:
    module = tmp_path / "module.py"
    module.write_text(
        "\n".join(
            [
                "def first():",
                "    return 1",
                "",
                "class Target:",
                "    def method(self):",
                "        return 2",
                "",
                "def last():",
                "    return 3",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = asyncio.run(
        ReadOutlineTool().execute(
            file_path="module.py",
            workspace_dir=str(tmp_path),
            offset=4,
            limit=2,
        )
    )

    assert result.success is True
    assert result.error is None
    assert "Target" in result.content
    assert result.content.count("\n") <= 3
