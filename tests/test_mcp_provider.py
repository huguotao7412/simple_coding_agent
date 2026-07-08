from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from core.mcp.client import MCPToolProvider


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

    provider = MCPToolProvider()
    await provider.start(str(tmp_path))
    await provider.shutdown()

    assert [params.command for params in captured_params] == ["npx", "npx"]
    assert [params.cwd for params in captured_params] == [str(tmp_path), str(tmp_path)]


def test_mcp_provider_rejects_absolute_paths_outside_worktree(tmp_path):
    provider = MCPToolProvider()
    provider._worktree_path = str(tmp_path)

    assert provider._validate_path("relative/path.py")
    assert provider._validate_path(str(tmp_path / "inside.py"))
    assert not provider._validate_path(str(tmp_path.parent / "outside.py"))
