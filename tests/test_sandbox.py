from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from core.sandbox.config import SandboxConfig, SandboxMode, load_sandbox_config
from core.sandbox.contracts import SandboxExecutionRequest, SandboxUnavailableError
from core.sandbox.e2b import E2BSandboxBackend
from core.sandbox.local import LocalSandboxBackend
from core.sandbox.paths import resolve_sandbox_cwd
from core.sandbox.transport import apply_workspace_archive, pack_workspace


def test_load_sandbox_config_defaults_to_explicit_nonisolated_local_mode():
    config = load_sandbox_config({})

    assert config.mode is SandboxMode.LOCAL
    assert config.limits.max_transfer_bytes == 50_000_000


def test_load_sandbox_config_validates_mode_and_boolean():
    with pytest.raises(ValueError, match="local.*e2b"):
        load_sandbox_config({"SCA_SANDBOX_BACKEND": "docker"})
    with pytest.raises(ValueError, match="true or false"):
        load_sandbox_config({"SCA_E2B_ALLOW_INTERNET": "sometimes"})


def test_resolve_sandbox_cwd_rejects_workspace_escape(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()

    host, remote = resolve_sandbox_cwd(workspace, "src")

    assert host == (workspace / "src").resolve()
    assert remote == "/home/user/sca-workspace/src"
    with pytest.raises(ValueError, match="outside workspace"):
        resolve_sandbox_cwd(workspace, "../outside")


def test_workspace_transport_excludes_secrets_and_applies_remote_changes(
    tmp_path: Path,
):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "app.py").write_text("new", encoding="utf-8")
    (source / ".env").write_text("SECRET=value", encoding="utf-8")
    (source / ".env.example").write_text("SECRET=placeholder", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("secret", encoding="utf-8")
    (target / "deleted.py").write_text("old", encoding="utf-8")
    (target / ".env").write_text("KEEP=me", encoding="utf-8")

    payload = pack_workspace(source, max_bytes=1_000_000)
    apply_workspace_archive(target, payload, max_bytes=1_000_000)

    assert (target / "app.py").read_text(encoding="utf-8") == "new"
    assert (target / ".env.example").exists()
    assert (target / ".env").read_text(encoding="utf-8") == "KEEP=me"
    assert not (target / "deleted.py").exists()
    assert not (target / ".git").exists()


@pytest.mark.asyncio
async def test_local_backend_reports_that_execution_is_not_isolated(tmp_path: Path):
    result = await LocalSandboxBackend().execute(SandboxExecutionRequest(
        workspace=tmp_path,
        command=(__import__("sys").executable, "-c", "print('ok')"),
        timeout_seconds=5,
    ))

    assert result.succeeded
    assert result.output.strip() == "ok"
    assert result.backend == "local"
    assert result.isolated is False


@pytest.mark.asyncio
async def test_local_backend_timeout_terminates_shell_descendants(
    tmp_path: Path,
):
    command = f'"{sys.executable}" -c "import time; time.sleep(60)"'

    result = await asyncio.wait_for(
        LocalSandboxBackend().execute(SandboxExecutionRequest(
            workspace=tmp_path,
            command=(command,),
            timeout_seconds=0.1,
            shell=True,
        )),
        timeout=5,
    )

    assert result.timed_out is True


@pytest.mark.asyncio
async def test_e2b_backend_fails_closed_without_api_key():
    backend = E2BSandboxBackend(SandboxConfig(mode=SandboxMode.E2B))

    with pytest.raises(SandboxUnavailableError, match="E2B_API_KEY"):
        await backend.ensure_available()


@pytest.mark.asyncio
async def test_e2b_backend_syncs_workspace_around_command(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    (workspace / "before.txt").write_text("before", encoding="utf-8")

    class Result:
        exit_code = 0
        stdout = "ok\n"
        stderr = ""

    class Files:
        uploaded = b""
        write_path = ""
        read_path = ""

        async def write(self, path, data):
            self.write_path = path
            self.uploaded = bytes(data)

        async def read(self, path, format):
            self.read_path = path
            remote = tmp_path / "remote"
            remote.mkdir(exist_ok=True)
            (remote / "after.txt").write_text("after", encoding="utf-8")
            return bytearray(pack_workspace(remote, max_bytes=1_000_000))

    class Commands:
        calls = []

        async def run(self, command, **kwargs):
            self.calls.append((command, kwargs))
            return Result()

    class Session:
        files = Files()
        commands = Commands()

    backend = E2BSandboxBackend(SandboxConfig(
        mode=SandboxMode.E2B,
        e2b_api_key="e2b_test",
    ))
    async def fake_session(workspace):
        return Session()
    monkeypatch.setattr(backend, "_session", fake_session)

    result = await backend.execute(SandboxExecutionRequest(
        workspace=workspace,
        command=("python3", "-c", "print('ok')"),
        timeout_seconds=10,
    ))

    assert result.succeeded
    assert result.backend == "e2b"
    assert (workspace / "after.txt").read_text(encoding="utf-8") == "after"
    assert not (workspace / "before.txt").exists()
    assert Session.files.uploaded
    assert Session.files.write_path == "/home/user/.sca-workspace-in.zip"
    assert Session.files.read_path == "/home/user/.sca-workspace-out.zip"
    assert "/home/user/sca-workspace" in Session.commands.calls[0][0]
    assert Session.commands.calls[1][1]["cwd"] == "/home/user/sca-workspace"
    assert "/home/user/sca-workspace" in Session.commands.calls[-1][0]
