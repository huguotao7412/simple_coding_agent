from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from core.mcp.client import MCPToolProvider, is_destructive_shell_command
from core.policy import ToolPolicy
from core.runs.context import RunContext
from core.sandbox.contracts import SandboxExecutionResult


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

    async def call_tool(self, name, args):
        return SimpleNamespace(content=[SimpleNamespace(text=f"called:{name}")])


class FakeStdioContext:
    async def __aenter__(self):
        return object(), object()

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakeIsolatedSandbox:
    name = "fake-e2b"
    isolated = True
    python_executable = "python"

    def __init__(self):
        self.available = False
        self.requests = []

    async def ensure_available(self):
        self.available = True

    async def execute(self, request):
        self.requests.append(request)
        return SandboxExecutionResult(
            backend=self.name,
            isolated=True,
            command=request.command,
            exit_code=0,
            output="sandboxed output",
            duration_ms=5,
        )


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
        "--yes",
        "@modelcontextprotocol/server-filesystem@2026.1.14",
    ]
    assert captured_params[1].args[:2] == ["--yes", "bash-mcp@1.1.0"]
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


@pytest.mark.asyncio
async def test_isolated_mode_omits_bash_mcp_and_routes_run_to_sandbox(
    monkeypatch,
    tmp_path,
):
    captured_params = []
    sandbox = FakeIsolatedSandbox()

    def fake_stdio_client(params):
        captured_params.append(params)
        return FakeStdioContext()

    monkeypatch.setattr("core.mcp.client.stdio_client", fake_stdio_client)
    monkeypatch.setattr("core.mcp.client.ClientSession", FakeSession)
    monkeypatch.setattr("core.mcp.client.PACKAGE_ROOT", tmp_path / "without_bins")
    provider = MCPToolProvider(sandbox_backend=sandbox)

    await provider.start(str(tmp_path))
    schemas = await provider.list_tools()
    result = await provider.call_tool("run", {"command": "python -m pytest"})
    await provider.shutdown()

    assert sandbox.available
    assert len(captured_params) == 1
    assert "server-filesystem" in " ".join(captured_params[0].args)
    names = [schema["function"]["name"] for schema in schemas]
    assert names.count("run") == 1
    assert "run_background" not in names
    assert result.success
    assert '"isolated": true' in result.content
    assert sandbox.requests[0].workspace == tmp_path


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


@pytest.mark.asyncio
async def test_mcp_provider_denies_tool_not_in_allowlist(tmp_path):
    run_context = RunContext.create(run_id="run_policy")
    provider = MCPToolProvider(run_context=run_context, actor_id="task_scout")
    provider._worktree_path = str(tmp_path)
    provider._tool_routing["run"] = "bash"
    provider._sessions["bash"] = FakeSession(None, None)
    provider.set_policy(ToolPolicy.for_role("scout", {"read_file"}))

    result = await provider.call_tool("run", {"command": "git status"})

    assert not result.success
    assert "not permitted for role 'scout'" in (result.error or "")
    event = await run_context.events.get()
    assert event.type == "policy_denied"
    assert event.tool_name == "run"
    assert event.actor_id == "task_scout"
    assert event.run_id == "run_policy"


@pytest.mark.asyncio
async def test_mcp_provider_allows_authorized_tool(tmp_path):
    provider = MCPToolProvider()
    provider._worktree_path = str(tmp_path)
    provider._tool_routing["run"] = "bash"
    provider._sessions["bash"] = FakeSession(None, None)
    provider.set_policy(ToolPolicy.for_role("coder", {"run"}))

    result = await provider.call_tool("run", {"command": "git status"})

    assert result.success
    assert result.content == "called:run"


@pytest.mark.asyncio
async def test_mcp_provider_uses_policy_to_filter_schemas():
    provider = MCPToolProvider()
    provider._tool_schemas = [
        {"type": "function", "function": {"name": "read_file"}},
        {"type": "function", "function": {"name": "run"}},
    ]
    provider.set_policy(ToolPolicy.for_role("scout", {"read_file"}))

    schemas = await provider.list_tools()

    assert [schema["function"]["name"] for schema in schemas] == ["read_file"]
