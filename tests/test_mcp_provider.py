from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from mcp.types import Tool

from core.adapters.mcp import normalize_mcp_tool
from core.mcp.client import (
    MCPToolProvider,
    _node_bin_command,
    is_destructive_shell_command,
)
from core.policy import ToolPolicy
from core.runs.context import RunContext
from core.sandbox.contracts import SandboxExecutionResult


class BadSchemaTool:
    def model_dump(self, *, by_alias: bool = False):
        assert by_alias
        return {"name": "broken", "inputSchema": "not-an-object"}


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
        return SimpleNamespace(tools=[Tool(
            name="run",
            description="fake tool",
            inputSchema={"type": "object", "properties": {}},
        )])

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
    monkeypatch.setenv("SCA_MCP_ALLOW_RUNTIME_INSTALL", "true")
    captured_params = []

    def fake_stdio_client(params, errlog=None):
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

    def fake_stdio_client(params, errlog=None):
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


def test_mcp_servers_prefer_healthy_managed_runtime(monkeypatch, tmp_path):
    root = tmp_path / "managed"
    runtime_id = "runtime-test"
    active = root / "versions" / runtime_id
    bin_dir = active / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    suffix = ".cmd" if sys.platform == "win32" else ""
    for name in ("mcp-server-filesystem", "bash-mcp"):
        (bin_dir / f"{name}{suffix}").write_text("stub", encoding="utf-8")
    (root / "current.json").write_text(
        json.dumps({"schema_version": 1, "runtime_id": runtime_id}),
        encoding="utf-8",
    )
    (root / ".sca-managed-mcp.json").write_text(
        json.dumps({
            "schema_version": 1,
            "kind": "simple-coding-agent-managed-mcp",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("SCA_MCP_HOME", str(root))

    command = _node_bin_command("mcp-server-filesystem", "ignored@1")

    assert command == [str(bin_dir / f"mcp-server-filesystem{suffix}")]


@pytest.mark.asyncio
async def test_isolated_mode_omits_bash_mcp_and_routes_run_to_sandbox(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SCA_MCP_ALLOW_RUNTIME_INSTALL", "true")
    captured_params = []
    sandbox = FakeIsolatedSandbox()

    def fake_stdio_client(params, errlog=None):
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
async def test_provider_adapter_does_not_duplicate_gateway_command_policy(tmp_path):
    provider = MCPToolProvider()
    provider._worktree_path = str(tmp_path)
    provider._tool_routing["run"] = "bash"
    provider._sessions["bash"] = object()

    result = await provider.call_tool("run", {"command": "rm -rf important.txt"})

    assert result.error is None or "Destructive shell command blocked" not in result.error


@pytest.mark.asyncio
async def test_provider_adapter_does_not_duplicate_gateway_role_policy(tmp_path):
    run_context = RunContext.create(run_id="run_policy")
    provider = MCPToolProvider(run_context=run_context, actor_id="task_scout")
    provider._worktree_path = str(tmp_path)
    provider._tool_routing["run"] = "bash"
    provider._sessions["bash"] = FakeSession(None, None)
    provider.set_policy(ToolPolicy.for_role("scout", {"read_file"}))

    result = await provider.call_tool(
        "run",
        {"command": "python -c \"print('ok')\""},
    )

    assert result.success


@pytest.mark.asyncio
async def test_mcp_provider_allows_authorized_tool(tmp_path):
    provider = MCPToolProvider()
    provider._worktree_path = str(tmp_path)
    provider._tool_routing["run"] = "bash"
    provider._sessions["bash"] = FakeSession(None, None)
    provider.set_policy(ToolPolicy.for_role("coder", {"run"}))

    result = await provider.call_tool(
        "run",
        {"command": "python -c \"print('ok')\""},
    )

    assert result.success
    assert '"backend": "local"' in result.content
    assert '"exit_code": 0' in result.content


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


@pytest.mark.asyncio
async def test_mcp_provider_always_exposes_line_range_read_tool():
    provider = MCPToolProvider()

    schemas = await provider.list_tools()

    read_schema = next(
        schema for schema in schemas if schema["function"]["name"] == "read"
    )
    properties = read_schema["function"]["parameters"]["properties"]
    assert {"file_path", "offset", "limit"} <= set(properties)


@pytest.mark.asyncio
async def test_node_free_provider_exposes_baseline_coder_tools(monkeypatch, tmp_path):
    monkeypatch.setattr("core.mcp.client.shutil.which", lambda command: None)
    provider = MCPToolProvider()

    await provider.start(str(tmp_path))
    schemas = await provider.list_tools()
    await provider.shutdown()

    names = {schema["function"]["name"] for schema in schemas}
    assert {
        "list_dir",
        "search_codebase",
        "read",
        "edit_file",
        "write_file",
        "run",
    } <= names


@pytest.mark.asyncio
async def test_mcp_required_mode_fails_closed_when_node_tools_are_absent(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SCA_MCP_MODE", "required")
    monkeypatch.delenv("SCA_MCP_ALLOW_RUNTIME_INSTALL", raising=False)
    monkeypatch.setattr("core.mcp.client.PACKAGE_ROOT", tmp_path / "no-node-modules")
    monkeypatch.setattr("core.mcp.client.shutil.which", lambda command: None)
    provider = MCPToolProvider()

    with pytest.raises(RuntimeError, match="required"):
        await provider.start(str(tmp_path))

    assert provider.health().local_tools_available


@pytest.mark.asyncio
async def test_mcp_off_mode_never_starts_node(monkeypatch, tmp_path):
    monkeypatch.setenv("SCA_MCP_MODE", "off")
    monkeypatch.setattr(
        "core.mcp.client.stdio_client",
        lambda params, errlog=None: pytest.fail("MCP process must not start"),
    )
    provider = MCPToolProvider()

    await provider.start(str(tmp_path))

    assert provider.health().status == "healthy"


@pytest.mark.asyncio
async def test_local_run_nonzero_exit_returns_failed_tool_result(tmp_path):
    provider = MCPToolProvider()
    provider._worktree_path = str(tmp_path)
    provider.set_policy(ToolPolicy.for_role("coder", {"run"}))

    result = await provider.call_tool(
        "run",
        {"command": f'"{sys.executable}" -c "import sys; sys.exit(7)"'},
    )

    assert result.success is False
    assert "exit code 7" in (result.error or "")


def test_real_mcp_tool_normalizes_by_protocol_alias():
    tool = Tool(
        name="remote_search",
        description=None,
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )

    definition = normalize_mcp_tool(tool, source="contract")

    assert definition.name == "remote_search"
    assert definition.description == ""
    assert definition.input_schema.value["required"] == ["query"]
    assert definition.openai_schema()["function"]["parameters"]["type"] == "object"


@pytest.mark.asyncio
async def test_bad_tool_schema_isolated_from_valid_tool():
    class MixedSession:
        async def list_tools(self):
            return SimpleNamespace(tools=[
                BadSchemaTool(),
                Tool(
                    name="remote_ok",
                    inputSchema={"type": "object", "properties": {}},
                ),
            ])

    provider = MCPToolProvider()
    provider._sessions["mixed"] = MixedSession()
    provider._commit_local_catalog()

    await provider._build_routing_table()
    names = {schema["function"]["name"] for schema in provider._tool_schemas}

    assert "remote_ok" in names
    assert "broken" not in names
    assert any(item.phase == "normalize_schema" for item in provider.health().diagnostics)


@pytest.mark.asyncio
async def test_remote_arguments_do_not_leak_workspace_context(tmp_path):
    captured = {}

    class RemoteSession:
        async def call_tool(self, name, args):
            captured.update(args)
            return SimpleNamespace(content=[SimpleNamespace(text="ok")])

    provider = MCPToolProvider()
    provider._worktree_path = str(tmp_path)
    definition = normalize_mcp_tool(
        Tool(
            name="remote_echo",
            inputSchema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
            },
        ),
        source="remote",
    )
    provider._remote_tools["remote_echo"] = definition
    provider._tool_routing["remote_echo"] = "remote"
    provider._sessions["remote"] = RemoteSession()

    result = await provider.call_tool(
        "remote_echo",
        {"message": "hello", "workspace_dir": str(tmp_path), "unexpected": "drop"},
    )

    assert result.success
    assert captured == {"message": "hello"}
