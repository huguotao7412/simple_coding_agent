from pathlib import Path

from core.paths import (
    read_workspace_metadata,
    touch_workspace_state,
    user_state_dir,
    workspace_state_dir,
)


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


def test_touch_workspace_state_records_path_and_access_time(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setenv("SCA_STATE_HOME", str(tmp_path / "state"))

    state_dir = touch_workspace_state(workspace, now=100.0)
    touch_workspace_state(workspace, now=200.0)
    metadata = read_workspace_metadata(state_dir)

    assert metadata is not None
    assert metadata.workspace_path == str(workspace.resolve())
    assert metadata.created_at == 100.0
    assert metadata.last_accessed_at == 200.0
    assert metadata.orphaned_at is None
