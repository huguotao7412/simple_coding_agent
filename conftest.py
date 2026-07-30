from __future__ import annotations

import os
from pathlib import Path
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_sca_state_home(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Path]:
    """Keep the offline test suite independent from the user's home directory."""
    state_home = tmp_path_factory.mktemp("sca-state")
    previous = os.environ.get("SCA_STATE_HOME")
    os.environ["SCA_STATE_HOME"] = str(state_home)
    yield state_home
    if previous is None:
        os.environ.pop("SCA_STATE_HOME", None)
    else:
        os.environ["SCA_STATE_HOME"] = previous
