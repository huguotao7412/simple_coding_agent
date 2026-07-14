from __future__ import annotations

from pathlib import Path
import re
import tomllib
from typing import Any

from .models import GateSpec, VerificationConfig


_CONFIG_PATH = Path(".sca") / "quality-gates.toml"
_GATE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_TOP_LEVEL_FIELDS = frozenset({"max_repair_attempts", "gates"})
_GATE_FIELDS = frozenset({"name", "command", "timeout_seconds", "required"})


class VerificationConfigError(ValueError):
    """Raised when project verification configuration is invalid."""


def _gate_from_mapping(value: Any, *, index: int) -> GateSpec:
    if not isinstance(value, dict):
        raise VerificationConfigError(f"gate #{index} must be a TOML table")

    unknown = set(value) - _GATE_FIELDS
    if unknown:
        raise VerificationConfigError(
            f"gate #{index} has unknown fields: {', '.join(sorted(unknown))}"
        )

    name = value.get("name")
    if not isinstance(name, str) or not _GATE_NAME.fullmatch(name):
        raise VerificationConfigError(f"gate #{index} has invalid gate name")

    command = value.get("command")
    if not isinstance(command, list):
        raise VerificationConfigError(f"gate {name!r} command must be an array of arguments")
    if not command:
        raise VerificationConfigError(f"gate {name!r} command must not be empty")
    if any(not isinstance(argument, str) or not argument for argument in command):
        raise VerificationConfigError(
            f"gate {name!r} command arguments must be non-empty strings"
        )

    timeout = value.get("timeout_seconds", 120.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise VerificationConfigError(f"gate {name!r} timeout_seconds must be positive")

    required = value.get("required", True)
    if not isinstance(required, bool):
        raise VerificationConfigError(f"gate {name!r} required must be a boolean")

    return GateSpec(
        name=name,
        command=tuple(command),
        timeout_seconds=float(timeout),
        required=required,
    )


def load_verification_config(workspace: str | Path) -> VerificationConfig:
    config_path = Path(workspace) / _CONFIG_PATH
    if not config_path.is_file():
        return VerificationConfig()

    try:
        with config_path.open("rb") as config_file:
            raw = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as exc:
        raise VerificationConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    unknown = set(raw) - _TOP_LEVEL_FIELDS
    if unknown:
        raise VerificationConfigError(
            f"unknown top-level fields: {', '.join(sorted(unknown))}"
        )

    max_attempts = raw.get("max_repair_attempts", 2)
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 0 <= max_attempts <= 5
    ):
        raise VerificationConfigError("max_repair_attempts must be an integer from 0 to 5")

    raw_gates = raw.get("gates", [])
    if not isinstance(raw_gates, list):
        raise VerificationConfigError("gates must be an array of tables")
    gates = tuple(
        _gate_from_mapping(value, index=index)
        for index, value in enumerate(raw_gates, start=1)
    )
    names = [gate.name.casefold() for gate in gates]
    if len(names) != len(set(names)):
        raise VerificationConfigError("duplicate gate name")

    return VerificationConfig(gates=gates, max_repair_attempts=max_attempts)


__all__ = ["VerificationConfigError", "load_verification_config"]
