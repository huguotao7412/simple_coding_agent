from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.main import main
from core.mcp.managed_runtime import (
    MCPRuntimeError,
    install_managed_runtime,
    managed_binary,
    managed_runtime_status,
    uninstall_managed_runtime,
)
import core.mcp.managed_runtime as managed_runtime


def _successful_npm(command, *, cwd, **kwargs):
    bin_dir = Path(cwd) / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    suffix = ".cmd" if os.name == "nt" else ""
    for name in ("mcp-server-filesystem", "bash-mcp"):
        (bin_dir / f"{name}{suffix}").write_text("stub", encoding="utf-8")
    return SimpleNamespace(returncode=0, stdout="installed", stderr="")


def test_managed_runtime_installs_locked_graph_atomically(tmp_path, monkeypatch):
    env = {"SCA_MCP_HOME": str(tmp_path / "runtime")}
    monkeypatch.setattr(
        "core.mcp.managed_runtime._commands",
        lambda: ("node", "npm.cmd" if os.name == "nt" else "npm"),
    )
    monkeypatch.setattr(
        "core.mcp.managed_runtime.subprocess.run",
        _successful_npm,
    )

    status = install_managed_runtime(env)

    assert status.healthy
    assert status.active_dir is not None
    assert (status.active_dir / "package-lock.json").is_file()
    assert managed_binary("mcp-server-filesystem", env) is not None
    marker = json.loads((status.root / "current.json").read_text(encoding="utf-8"))
    assert marker["runtime_id"] == status.runtime_id
    assert not list(status.root.glob("staging-*"))


def test_failed_install_cleans_staging_and_redacts_error(tmp_path, monkeypatch):
    env = {"SCA_MCP_HOME": str(tmp_path / "runtime")}
    monkeypatch.setattr(
        "core.mcp.managed_runtime._commands", lambda: ("node", "npm.cmd")
    )
    monkeypatch.setattr(
        "core.mcp.managed_runtime.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=7,
            stdout="",
            stderr="token=super-secret-value",
        ),
    )

    with pytest.raises(MCPRuntimeError) as captured:
        install_managed_runtime(env)

    assert "super-secret-value" not in str(captured.value)
    assert "[REDACTED]" in str(captured.value)
    assert not list((tmp_path / "runtime").glob("staging-*"))


def test_uninstall_removes_only_managed_root(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    (root / "versions" / "test").mkdir(parents=True)
    (root / "current.json").write_text("{}", encoding="utf-8")
    (root / ".sca-managed-mcp.json").write_text(
        json.dumps({
            "schema_version": 1,
            "kind": "simple-coding-agent-managed-mcp",
        }),
        encoding="utf-8",
    )
    outside = tmp_path / "keep.txt"
    outside.write_text("keep", encoding="utf-8")

    removed = uninstall_managed_runtime({"SCA_MCP_HOME": str(root)})

    assert removed == root.resolve()
    assert not root.exists()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_uninstall_refuses_unowned_custom_directory(tmp_path):
    root = tmp_path / "not-owned"
    root.mkdir()
    important = root / "important.txt"
    important.write_text("keep", encoding="utf-8")

    with pytest.raises(MCPRuntimeError, match="ownership marker"):
        uninstall_managed_runtime({"SCA_MCP_HOME": str(root)})

    assert important.read_text(encoding="utf-8") == "keep"


def test_install_refuses_unowned_non_empty_directory(tmp_path, monkeypatch):
    root = tmp_path / "not-owned"
    root.mkdir()
    important = root / "important.txt"
    important.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(
        "core.mcp.managed_runtime._commands", lambda: ("node", "npm.cmd")
    )
    invoked = False

    def unexpected_npm(*args, **kwargs):
        nonlocal invoked
        invoked = True
        return _successful_npm(*args, **kwargs)

    monkeypatch.setattr(
        "core.mcp.managed_runtime.subprocess.run",
        unexpected_npm,
    )

    with pytest.raises(MCPRuntimeError, match="refusing to claim"):
        install_managed_runtime({"SCA_MCP_HOME": str(root)})

    assert not invoked
    assert important.read_text(encoding="utf-8") == "keep"


def test_mcp_status_command_is_read_only(tmp_path, monkeypatch, capsys):
    root = tmp_path / "runtime"
    monkeypatch.setenv("SCA_MCP_HOME", str(root))

    exit_code = main(["mcp", "status"])

    assert exit_code == 1
    assert "Status: unavailable" in capsys.readouterr().out
    assert not root.exists()


def test_status_rejects_marker_escape(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "current.json").write_text(
        json.dumps({"schema_version": 1, "runtime_id": "../escape"}),
        encoding="utf-8",
    )

    status = managed_runtime_status({"SCA_MCP_HOME": str(root)})

    assert not status.healthy
    assert "escapes" in status.detail


def test_npm_environment_keeps_proxy_but_removes_credentials():
    sanitized = managed_runtime._npm_environment({
        "PATH": "bin",
        "HTTPS_PROXY": "http://proxy.example",
        "SCA_API_KEY": "secret",
        "NPM_TOKEN": "secret",
    })

    assert sanitized["HTTPS_PROXY"] == "http://proxy.example"
    assert sanitized["PATH"] == "bin"
    assert "SCA_API_KEY" not in sanitized
    assert "NPM_TOKEN" not in sanitized
