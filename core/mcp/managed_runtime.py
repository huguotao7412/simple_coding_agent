"""Explicit, per-user installation of the optional Node MCP runtime."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..paths import user_state_dir
from ..security.redaction import SECRET_ENV_NAMES, SENSITIVE_KEYS, redact_text


MANAGED_RUNTIME_SCHEMA_VERSION = 1
OWNERSHIP_MARKER = ".sca-managed-mcp.json"
MANIFEST_NAMES = ("package.json", "package-lock.json")
REQUIRED_BINARIES = ("mcp-server-filesystem", "bash-mcp")
DEFAULT_INSTALL_TIMEOUT_SECONDS = 300.0


class MCPRuntimeError(RuntimeError):
    """The optional managed MCP runtime could not be installed or validated."""


@dataclass(frozen=True)
class ManagedMCPStatus:
    root: Path
    active_dir: Path | None
    manifest_dir: Path | None
    runtime_id: str
    node_command: str | None
    npm_command: str | None
    binaries: dict[str, Path]
    healthy: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "active_dir": str(self.active_dir) if self.active_dir else None,
            "manifest_dir": str(self.manifest_dir) if self.manifest_dir else None,
            "runtime_id": self.runtime_id,
            "node_command": self.node_command,
            "npm_command": self.npm_command,
            "binaries": {name: str(path) for name, path in self.binaries.items()},
            "healthy": self.healthy,
            "detail": self.detail,
        }


def managed_runtime_root(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    override = values.get("SCA_MCP_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (user_state_dir(values) / "mcp-runtime").resolve()


def bundled_manifest_dir() -> Path | None:
    source_root = Path(__file__).resolve().parents[2]
    if all((source_root / name).is_file() for name in MANIFEST_NAMES):
        return source_root
    installed = Path(sys.prefix) / "share" / "simple-coding-agent" / "mcp"
    if all((installed / name).is_file() for name in MANIFEST_NAMES):
        return installed
    return None


def _manifest_identity(manifest_dir: Path) -> str:
    digest = hashlib.sha256()
    for name in MANIFEST_NAMES:
        digest.update(name.encode("utf-8"))
        digest.update((manifest_dir / name).read_bytes())
    return digest.hexdigest()[:16]


def _binary_path(runtime_dir: Path, name: str) -> Path:
    suffix = ".cmd" if os.name == "nt" else ""
    return runtime_dir / "node_modules" / ".bin" / f"{name}{suffix}"


def _commands() -> tuple[str | None, str | None]:
    node = shutil.which("node")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    return node, npm


def _read_active_dir(root: Path) -> tuple[Path | None, str]:
    marker = root / "current.json"
    if not marker.is_file():
        return None, "managed MCP runtime is not installed"
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if payload.get("schema_version") != MANAGED_RUNTIME_SCHEMA_VERSION:
            raise ValueError("unsupported marker schema")
        runtime_id = str(payload["runtime_id"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        return None, f"invalid managed runtime marker: {type(error).__name__}"
    candidate = (root / "versions" / runtime_id).resolve()
    versions_root = (root / "versions").resolve()
    if candidate.parent != versions_root:
        return None, "managed runtime marker escapes versions directory"
    return candidate, "installed"


def _has_valid_ownership(root: Path) -> bool:
    try:
        payload = json.loads((root / OWNERSHIP_MARKER).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    schema_matches = payload.get("schema_version") == MANAGED_RUNTIME_SCHEMA_VERSION
    kind_matches = payload.get("kind") == "simple-coding-agent-managed-mcp"
    return bool(schema_matches and kind_matches)


def managed_runtime_status(
    env: Mapping[str, str] | None = None,
) -> ManagedMCPStatus:
    root = managed_runtime_root(env)
    manifest_dir = bundled_manifest_dir()
    runtime_id = _manifest_identity(manifest_dir) if manifest_dir else ""
    node, npm = _commands()
    active_dir, detail = _read_active_dir(root)
    owned = _has_valid_ownership(root)
    binaries: dict[str, Path] = {}
    if active_dir is not None:
        binaries = {
            name: path
            for name in REQUIRED_BINARIES
            if (path := _binary_path(active_dir, name)).is_file()
        }
    healthy = (
        active_dir is not None
        and active_dir.is_dir()
        and len(binaries) == len(REQUIRED_BINARIES)
        and node is not None
        and owned
    )
    if active_dir is not None and not healthy:
        missing = sorted(set(REQUIRED_BINARIES) - set(binaries))
        detail = "managed runtime incomplete"
        if missing:
            detail += "; missing: " + ", ".join(missing)
        if node is None:
            detail += "; Node is unavailable"
        if not owned:
            detail += "; ownership marker is missing or invalid"
    return ManagedMCPStatus(
        root=root,
        active_dir=active_dir,
        manifest_dir=manifest_dir,
        runtime_id=runtime_id,
        node_command=node,
        npm_command=npm,
        binaries=binaries,
        healthy=healthy,
        detail=detail,
    )


@contextmanager
def _installation_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "install.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise MCPRuntimeError(
            f"another MCP runtime operation holds {lock_path}"
        ) from error
    try:
        os.write(descriptor, f"pid={os.getpid()} time={time.time()}\n".encode())
        os.close(descriptor)
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _npm_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    source = dict(os.environ if env is None else env)
    # Keep proxy/registry settings required for an explicit install, but never
    # forward SCA/model credentials to npm or lifecycle subprocesses.
    blocked_names = SECRET_ENV_NAMES - {"HTTP_PROXY", "HTTPS_PROXY"}
    return {
        key: value
        for key, value in source.items()
        if key not in blocked_names
        and not key.startswith("SCA_")
        and not SENSITIVE_KEYS.search(key)
    }


def install_managed_runtime(
    env: Mapping[str, str] | None = None,
    *,
    timeout_seconds: float | None = None,
) -> ManagedMCPStatus:
    status = managed_runtime_status(env)
    if status.manifest_dir is None:
        raise MCPRuntimeError("wheel/source installation does not contain MCP lock files")
    if status.node_command is None or status.npm_command is None:
        raise MCPRuntimeError("Node.js and npm are required for `sca mcp install`")
    if status.healthy and status.active_dir is not None:
        return status
    timeout = timeout_seconds
    if timeout is None:
        raw_timeout = (os.environ if env is None else env).get(
            "SCA_MCP_INSTALL_TIMEOUT", str(DEFAULT_INSTALL_TIMEOUT_SECONDS)
        )
        try:
            timeout = float(raw_timeout)
        except ValueError as error:
            raise MCPRuntimeError("SCA_MCP_INSTALL_TIMEOUT must be numeric") from error
    if timeout <= 0:
        raise MCPRuntimeError("MCP install timeout must be positive")

    root = status.root
    runtime_id = status.runtime_id
    target = root / "versions" / runtime_id
    staging = root / f"staging-{runtime_id}-{os.getpid()}"
    with _installation_lock(root):
        existing = [child for child in root.iterdir() if child.name != "install.lock"]
        if existing and not _has_valid_ownership(root):
            raise MCPRuntimeError(
                "refusing to claim a non-empty MCP runtime directory without "
                f"a valid ownership marker: {root}"
            )
        if staging.exists():
            raise MCPRuntimeError(f"staging directory already exists: {staging}")
        staging.mkdir(parents=True)
        try:
            for name in MANIFEST_NAMES:
                shutil.copy2(status.manifest_dir / name, staging / name)
            try:
                completed = subprocess.run(
                    [
                        status.npm_command,
                        "ci",
                        "--ignore-scripts",
                        "--no-audit",
                        "--no-fund",
                    ],
                    cwd=staging,
                    env=_npm_environment(env),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise MCPRuntimeError(
                    f"npm ci exceeded the {timeout:g}s MCP install timeout"
                ) from error
            if completed.returncode != 0:
                summary = str(redact_text(completed.stderr or completed.stdout).value)
                raise MCPRuntimeError(
                    f"npm ci failed with exit code {completed.returncode}: {summary[-1000:]}"
                )
            missing = [
                name for name in REQUIRED_BINARIES
                if not _binary_path(staging, name).is_file()
            ]
            if missing:
                raise MCPRuntimeError(
                    "npm install completed without required binaries: "
                    + ", ".join(missing)
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(staging)
            else:
                staging.replace(target)
            marker = root / "current.json"
            ownership = root / OWNERSHIP_MARKER
            ownership.write_text(
                json.dumps({
                    "schema_version": MANAGED_RUNTIME_SCHEMA_VERSION,
                    "kind": "simple-coding-agent-managed-mcp",
                }, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary = root / f"current.json.{os.getpid()}.tmp"
            temporary.write_text(
                json.dumps({
                    "schema_version": MANAGED_RUNTIME_SCHEMA_VERSION,
                    "runtime_id": runtime_id,
                    "installed_at": time.time(),
                }, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(marker)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise
    result = managed_runtime_status(env)
    if not result.healthy:
        raise MCPRuntimeError(result.detail)
    return result


def uninstall_managed_runtime(
    env: Mapping[str, str] | None = None,
) -> Path:
    root = managed_runtime_root(env)
    if not root.exists():
        return root
    # The target is fixed by user-state/SCA_MCP_HOME resolution and must never
    # collapse to a drive, home, or state root.
    resolved = root.resolve()
    unsafe = {Path(resolved.anchor), Path.home().resolve(), user_state_dir(env).resolve()}
    if resolved in unsafe or resolved.parent == resolved:
        raise MCPRuntimeError(f"refusing unsafe MCP runtime removal target: {resolved}")
    if not _has_valid_ownership(resolved):
        raise MCPRuntimeError(
            f"refusing MCP runtime removal without a valid ownership marker: {resolved}"
        )
    with _installation_lock(root):
        for child in tuple(root.iterdir()):
            if child.name == "install.lock":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    try:
        root.rmdir()
    except OSError:
        pass
    return root


def managed_binary(
    name: str,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    if name not in REQUIRED_BINARIES:
        return None
    status = managed_runtime_status(env)
    return status.binaries.get(name) if status.healthy else None


__all__ = [
    "MCPRuntimeError",
    "ManagedMCPStatus",
    "bundled_manifest_dir",
    "install_managed_runtime",
    "managed_binary",
    "managed_runtime_root",
    "managed_runtime_status",
    "uninstall_managed_runtime",
]
