from __future__ import annotations

import asyncio
from pathlib import Path

from core.tools.file_ops import EditFileTool, WriteFileTool


def test_write_and_edit_file_inside_workspace(tmp_path: Path) -> None:
    write = asyncio.run(
        WriteFileTool().execute(
            file_path="pkg/module.py",
            content="value = 1\n",
            workspace_dir=str(tmp_path),
        )
    )
    edit = asyncio.run(
        EditFileTool().execute(
            file_path="pkg/module.py",
            old_text="value = 1",
            new_text="value = 2",
            workspace_dir=str(tmp_path),
        )
    )

    assert write.success is True
    assert edit.success is True
    assert (tmp_path / "pkg" / "module.py").read_text(encoding="utf-8") == "value = 2\n"


def test_write_and_edit_reject_workspace_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}_outside.py"
    write = asyncio.run(
        WriteFileTool().execute(
            file_path=str(outside),
            content="x = 1\n",
            workspace_dir=str(tmp_path),
        )
    )
    (tmp_path / "inside.py").write_text("x = 1\n", encoding="utf-8")
    edit = asyncio.run(
        EditFileTool().execute(
            file_path=str(outside),
            old_text="x = 1",
            new_text="x = 2",
            workspace_dir=str(tmp_path),
        )
    )

    assert write.success is False
    assert edit.success is False
    assert "escapes workspace" in (write.error or "")
    assert not outside.exists()
