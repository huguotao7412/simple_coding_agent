from __future__ import annotations

from pathlib import Path

import pytest

from core.verification.config import VerificationConfigError, load_verification_config


def test_missing_config_disables_verification(tmp_path: Path) -> None:
    config = load_verification_config(tmp_path)

    assert config.gates == ()
    assert config.max_repair_attempts == 2
    assert not config.enabled


def test_loads_ordered_quality_gates(tmp_path: Path) -> None:
    config_dir = tmp_path / ".sca"
    config_dir.mkdir()
    (config_dir / "quality-gates.toml").write_text(
        """
max_repair_attempts = 1

[[gates]]
name = "unit"
command = ["{python}", "-m", "pytest", "-q"]
timeout_seconds = 90

[[gates]]
name = "types"
command = ["{python}", "-m", "mypy", "core"]
required = false
""".strip(),
        encoding="utf-8",
    )

    config = load_verification_config(tmp_path)

    assert config.max_repair_attempts == 1
    assert [gate.name for gate in config.gates] == ["unit", "types"]
    assert config.gates[0].command == ("{python}", "-m", "pytest", "-q")
    assert config.gates[0].timeout_seconds == 90.0
    assert config.gates[0].required
    assert not config.gates[1].required


@pytest.mark.parametrize(
    "content, message",
    [
        ('unknown = true\n', "unknown top-level"),
        ('max_repair_attempts = 6\n', "max_repair_attempts"),
        ('[[gates]]\nname = "unit"\ncommand = "pytest"\n', "array of arguments"),
        ('[[gates]]\nname = "unit"\ncommand = []\n', "must not be empty"),
        ('[[gates]]\nname = "unit"\ncommand = ["pytest", ""]\n', "non-empty strings"),
        ('[[gates]]\nname = "../unit"\ncommand = ["pytest"]\n', "invalid gate name"),
        (
            '[[gates]]\nname = "unit"\ncommand = ["pytest"]\ntimeout_seconds = 0\n',
            "timeout_seconds",
        ),
        (
            '[[gates]]\nname = "unit"\ncommand = ["pytest"]\nextra = true\n',
            "unknown fields",
        ),
        (
            '[[gates]]\nname = "unit"\ncommand = ["pytest"]\n'
            '[[gates]]\nname = "UNIT"\ncommand = ["mypy"]\n',
            "duplicate gate name",
        ),
    ],
)
def test_rejects_ambiguous_or_unsafe_configuration(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    config_dir = tmp_path / ".sca"
    config_dir.mkdir()
    (config_dir / "quality-gates.toml").write_text(content, encoding="utf-8")

    with pytest.raises(VerificationConfigError, match=message):
        load_verification_config(tmp_path)
