from __future__ import annotations

import sqlite3
from pathlib import Path


def has_checkpoint(path: Path, run_id: str) -> bool:
    if not path.is_file():
        return False
    try:
        with sqlite3.connect(path) as connection:
            row = connection.execute(
                "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1",
                (run_id,),
            ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


__all__ = ["has_checkpoint"]
