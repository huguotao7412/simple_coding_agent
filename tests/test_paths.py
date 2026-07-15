from pathlib import Path

from core.paths import user_state_dir, workspace_state_dir


def test_user_state_dir_uses_platform_conventions() -> None:
    assert user_state_dir(
        {"LOCALAPPDATA": "C:/Local"},
        platform="nt",
    ) == Path("C:/Local/sca")
    assert user_state_dir(
        {"XDG_STATE_HOME": "/state"},
        home=Path("/home/test"),
        platform="posix",
    ) == Path("/state/sca")


def test_workspace_state_dir_is_stable_and_external(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    first = workspace_state_dir(
        workspace,
        {"SCA_STATE_HOME": str(tmp_path / "state")},
    )
    second = workspace_state_dir(
        workspace,
        {"SCA_STATE_HOME": str(tmp_path / "state")},
    )

    assert first == second
    assert first.parent == tmp_path / "state" / "workspaces"
    assert first.name.startswith("project-")
