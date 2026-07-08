from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from core.mcp.client import MCPToolProvider, is_destructive_shell_command


@dataclass
class FakeTool:
    name: str
    description: str = "fake tool"
    inputSchema: dict | None = None


class FakeSession:
    def __init__(self, read_stream, write_stream):
        self.read_stream = read_stream
        self.write_stream = write_stream

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def initialize(self):
        return None

    async def list_tools(self):
        return SimpleNamespace(tools=[FakeTool("run")])


class FakeStdioContext:
    async def __aenter__(self):
        return object(), object()

    async def __aexit__(self, exc_type, exc, tb):
        return None


@pytest.mark.asyncio
async def test_mcp_servers_start_with_worktree_cwd(monkeypatch, tmp_path):
    captured_params = []

    def fake_stdio_client(params):
        captured_params.append(params)
        return FakeStdioContext()

    monkeypatch.setattr("core.mcp.client.stdio_client", fake_stdio_client)
    monkeypatch.setattr("core.mcp.client.ClientSession", FakeSession)
    monkeypatch.setattr("core.mcp.client.PACKAGE_ROOT", tmp_path / "without_node_modules")

    provider = MCPToolProvider()
    await provider.start(str(tmp_path))
    await provider.shutdown()

    assert [params.command for params in captured_params] == ["npx", "npx"]
    assert captured_params[0].args[:2] == [
        "--no-install",
        "@modelcontextprotocol/server-filesystem@2026.1.14",
    ]
    assert captured_params[1].args[:2] == ["--no-install", "bash-mcp@1.1.0"]
    assert [params.cwd for params in captured_params] == [str(tmp_path), str(tmp_path)]


@pytest.mark.asyncio
async def test_mcp_servers_prefer_local_node_bins(monkeypatch, tmp_path):
    captured_params = []
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    suffix = ".cmd" if __import__("sys").platform == "win32" else ""
    fs_bin = bin_dir / f"mcp-server-filesystem{suffix}"
    bash_bin = bin_dir / f"bash-mcp{suffix}"
    fs_bin.write_text("", encoding="utf-8")
    bash_bin.write_text("", encoding="utf-8")

    def fake_stdio_client(params):
        captured_params.append(params)
        return FakeStdioContext()

    monkeypatch.setattr("core.mcp.client.PACKAGE_ROOT", tmp_path)
    monkeypatch.setattr("core.mcp.client.stdio_client", fake_stdio_client)
    monkeypatch.setattr("core.mcp.client.ClientSession", FakeSession)

    provider = MCPToolProvider()
    await provider.start(str(tmp_path))
    await provider.shutdown()

    assert captured_params[0].command == str(fs_bin)
    assert captured_params[0].args == [str(tmp_path)]
    assert captured_params[1].command == str(bash_bin)
    assert captured_params[1].args == []


def test_mcp_provider_rejects_absolute_paths_outside_worktree(tmp_path):
    provider = MCPToolProvider()
    provider._worktree_path = str(tmp_path)

    assert provider._validate_path("relative/path.py")
    assert provider._validate_path(str(tmp_path / "inside.py"))
    assert not provider._validate_path(str(tmp_path.parent / "outside.py"))


def test_destructive_shell_command_detection():
    assert is_destructive_shell_command("rm -rf important")
    assert is_destructive_shell_command("git reset --hard HEAD")
    assert is_destructive_shell_command("git clean -fd")
    assert not is_destructive_shell_command("python -m pytest -q")
    assert not is_destructive_shell_command("git status --short")


@pytest.mark.asyncio
async def test_mcp_provider_blocks_destructive_bash_command(tmp_path):
    provider = MCPToolProvider()
    provider._worktree_path = str(tmp_path)
    provider._tool_routing["run"] = "bash"
    provider._sessions["bash"] = object()

    result = await provider.call_tool("run", {"command": "rm -rf important.txt"})

    assert not result.success
    assert result.error is not None
    assert "Destructive shell command blocked" in result.error
