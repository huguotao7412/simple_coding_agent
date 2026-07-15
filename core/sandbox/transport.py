from __future__ import annotations

import io
import os
import stat
import zipfile
from pathlib import Path, PurePosixPath


_IGNORED_PARTS = {
    ".git",
    ".sca",
    ".venv",
    ".worktrees",
    "__pycache__",
    "node_modules",
}
_SECRET_NAMES = {".env", ".npmrc", ".pypirc", "credentials", "credentials.json"}


class WorkspaceTransferError(RuntimeError):
    """Raised when a workspace cannot cross the remote sandbox boundary safely."""


def is_transferable(relative: Path) -> bool:
    parts = relative.parts
    if any(part in _IGNORED_PARTS for part in parts):
        return False
    name = relative.name.lower()
    if name in _SECRET_NAMES:
        return False
    if name.startswith(".env.") and name != ".env.example":
        return False
    return True


def pack_workspace(workspace: Path, *, max_bytes: int) -> bytes:
    root = workspace.resolve()
    if not root.is_dir():
        raise WorkspaceTransferError(f"workspace does not exist: {root}")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if not is_transferable(relative) or path.is_symlink() or not path.is_file():
                continue
            info = zipfile.ZipInfo(PurePosixPath(*relative.parts).as_posix())
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (path.stat().st_mode & 0xFFFF) << 16
            archive.writestr(info, path.read_bytes())
            if buffer.tell() > max_bytes:
                raise WorkspaceTransferError(
                    f"workspace archive exceeds {max_bytes} bytes"
                )
    payload = buffer.getvalue()
    if len(payload) > max_bytes:
        raise WorkspaceTransferError(f"workspace archive exceeds {max_bytes} bytes")
    return payload


def apply_workspace_archive(
    workspace: Path,
    payload: bytes,
    *,
    max_bytes: int,
) -> None:
    if len(payload) > max_bytes:
        raise WorkspaceTransferError(f"remote archive exceeds {max_bytes} bytes")
    root = workspace.resolve()
    incoming: dict[Path, tuple[bytes, int]] = {}
    with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
        for info in archive.infolist():
            posix = PurePosixPath(info.filename)
            if info.is_dir() or posix.is_absolute() or ".." in posix.parts:
                if not info.is_dir():
                    raise WorkspaceTransferError("remote archive contains unsafe path")
                continue
            relative = Path(*posix.parts)
            if not is_transferable(relative):
                raise WorkspaceTransferError(
                    f"remote archive contains forbidden path: {info.filename}"
                )
            data = archive.read(info)
            incoming[relative] = (data, info.external_attr >> 16)

    existing = {
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and is_transferable(path.relative_to(root))
    }
    for relative in existing - incoming.keys():
        (root / relative).unlink()
    for relative, (data, mode) in incoming.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        if os.name != "nt" and mode:
            destination.chmod(stat.S_IMODE(mode))
    for directory in sorted(root.rglob("*"), reverse=True):
        if directory.is_dir() and is_transferable(directory.relative_to(root)):
            try:
                directory.rmdir()
            except OSError:
                pass


REMOTE_UNPACK_COMMAND = """python3 - <<'PY'
import pathlib, shutil, zipfile
root = pathlib.Path('/workspace')
root.mkdir(parents=True, exist_ok=True)
for child in root.iterdir():
    shutil.rmtree(child) if child.is_dir() else child.unlink()
with zipfile.ZipFile('/tmp/sca-workspace-in.zip') as archive:
    archive.extractall(root)
PY"""

REMOTE_PACK_COMMAND = """python3 - <<'PY'
import pathlib, zipfile
root = pathlib.Path('/workspace')
ignored = {'.git', '.sca', '.venv', '.worktrees', '__pycache__', 'node_modules'}
secrets = {'.env', '.npmrc', '.pypirc', 'credentials', 'credentials.json'}
with zipfile.ZipFile('/tmp/sca-workspace-out.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root)
        name = rel.name.lower()
        forbidden = any(part in ignored for part in rel.parts)
        forbidden = forbidden or name in secrets or (name.startswith('.env.') and name != '.env.example')
        if path.is_file() and not path.is_symlink() and not forbidden:
            z.write(path, rel.as_posix())
PY"""


__all__ = [
    "REMOTE_PACK_COMMAND",
    "REMOTE_UNPACK_COMMAND",
    "WorkspaceTransferError",
    "apply_workspace_archive",
    "is_transferable",
    "pack_workspace",
]
