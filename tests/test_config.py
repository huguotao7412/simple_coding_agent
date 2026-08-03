from __future__ import annotations

import os
from pathlib import Path

from cli.main import build_parser, main
from core.config import (
    initialize_user_config,
    load_runtime_environment,
    user_config_path,
)


def test_user_config_path_uses_windows_appdata() -> None:
    path = user_config_path(
        {"APPDATA": "C:/Users/example/AppData/Roaming"},
        platform="nt",
    )

    assert path == Path("C:/Users/example/AppData/Roaming/sca/.env")


def test_user_config_path_supports_explicit_override(tmp_path: Path) -> None:
    path = user_config_path({"SCA_CONFIG_HOME": str(tmp_path)})

    assert path == tmp_path / ".env"


def test_workspace_config_overrides_user_config_but_not_process_env(
    tmp_path: Path,
) -> None:
    user_config = tmp_path / "user.env"
    user_config.write_text(
        "SCA_API_KEY=user-key\nSCA_MODEL=user-model\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "SCA_API_KEY=workspace-key\nSCA_MODEL=workspace-model\n",
        encoding="utf-8",
    )
    environ = {"SCA_API_KEY": "process-key"}

    loaded = load_runtime_environment(
        workspace,
        environ=environ,
        config_path=user_config,
    )

    assert loaded == (user_config, workspace / ".env")
    assert environ["SCA_API_KEY"] == "process-key"
    assert environ["SCA_MODEL"] == "workspace-model"


def test_config_loader_does_not_search_parent_directories(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SCA_MODEL=parent-model\n", encoding="utf-8")
    workspace = tmp_path / "nested"
    workspace.mkdir()
    environ: dict[str, str] = {}

    loaded = load_runtime_environment(
        workspace,
        environ=environ,
        config_path=tmp_path / "missing-user.env",
    )

    assert loaded == ()
    assert "SCA_MODEL" not in environ


def test_config_loader_ignores_unrelated_dotenv_keys(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "DATABASE_PASSWORD=do-not-import\nSCA_MODEL=safe-model\n",
        encoding="utf-8",
    )
    environ: dict[str, str] = {}

    load_runtime_environment(
        workspace,
        environ=environ,
        config_path=tmp_path / "missing-user.env",
    )

    assert environ == {"SCA_MODEL": "safe-model"}


def test_workspace_cannot_supply_trusted_guardrail_settings(tmp_path: Path) -> None:
    user_config = tmp_path / "user.env"
    user_config.write_text(
        "SCA_SECURITY_MODE=hybrid\n"
        "SCA_GUARDRAILS_CONFIG=C:/trusted/guardrails.json\n"
        "SCA_GUARDRAILS_API_KEY=user-secret\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "SCA_SECURITY_MODE=off\n"
        "SCA_GUARDRAILS_CONFIG=.sca/guardrails.json\n"
        "SCA_GUARDRAILS_API_KEY=repository-secret\n",
        encoding="utf-8",
    )
    environ: dict[str, str] = {}

    load_runtime_environment(
        workspace,
        environ=environ,
        config_path=user_config,
    )

    assert environ["SCA_SECURITY_MODE"] == "hybrid"
    assert environ["SCA_GUARDRAILS_CONFIG"] == "C:/trusted/guardrails.json"
    assert environ["SCA_GUARDRAILS_API_KEY"] == "user-secret"


def test_workspace_cannot_redirect_or_enable_managed_mcp_install(tmp_path: Path) -> None:
    user_config = tmp_path / "user.env"
    user_config.write_text(
        "SCA_MCP_HOME=C:/trusted/mcp\n"
        "SCA_MCP_INSTALL_TIMEOUT=120\n"
        "SCA_MCP_ALLOW_RUNTIME_INSTALL=false\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "SCA_MCP_HOME=C:/attacker/mcp\n"
        "SCA_MCP_INSTALL_TIMEOUT=9999\n"
        "SCA_MCP_ALLOW_RUNTIME_INSTALL=true\n",
        encoding="utf-8",
    )
    environ: dict[str, str] = {}

    load_runtime_environment(workspace, environ=environ, config_path=user_config)

    assert environ == {
        "SCA_MCP_HOME": "C:/trusted/mcp",
        "SCA_MCP_INSTALL_TIMEOUT": "120",
        "SCA_MCP_ALLOW_RUNTIME_INSTALL": "false",
    }


def test_process_environment_can_switch_managed_workspace_config(
    monkeypatch,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / ".env").write_text("SCA_MODEL=first\n", encoding="utf-8")
    (second / ".env").write_text("SCA_MODEL=second\n", encoding="utf-8")
    monkeypatch.delenv("SCA_MODEL", raising=False)

    load_runtime_environment(
        first,
        config_path=tmp_path / "missing-user.env",
    )
    assert os.environ["SCA_MODEL"] == "first"
    load_runtime_environment(
        second,
        config_path=tmp_path / "missing-user.env",
    )

    assert os.environ["SCA_MODEL"] == "second"


def test_initialize_user_config_is_non_destructive_by_default(
    tmp_path: Path,
) -> None:
    env = {"SCA_CONFIG_HOME": str(tmp_path)}

    path = initialize_user_config(env=env)
    original = path.read_text(encoding="utf-8")

    assert "SCA_API_KEY=your-api-key" in original
    try:
        initialize_user_config(env=env)
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing config must not be overwritten")
    assert path.read_text(encoding="utf-8") == original


def test_config_parser_supports_path_and_force_init() -> None:
    parser = build_parser()

    path_args = parser.parse_args(["config", "path"])
    init_args = parser.parse_args(["config", "init", "--force"])

    assert path_args.config_command == "path"
    assert init_args.config_command == "init"
    assert init_args.force is True


def test_config_path_command_does_not_require_api_key(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.setenv("SCA_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("SCA_API_KEY", raising=False)

    exit_code = main(["config", "path"])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / ".env")
