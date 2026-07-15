from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping


class SandboxMode(StrEnum):
    LOCAL = "local"
    E2B = "e2b"


@dataclass(frozen=True)
class SandboxLimits:
    max_timeout_seconds: float = 300.0
    max_output_chars: int = 100_000
    max_transfer_bytes: int = 50_000_000

    def __post_init__(self) -> None:
        if self.max_timeout_seconds <= 0:
            raise ValueError("sandbox max timeout must be positive")
        if self.max_output_chars < 1000:
            raise ValueError("sandbox max output must be at least 1000 characters")
        if self.max_transfer_bytes < 1_000_000:
            raise ValueError("sandbox max transfer must be at least 1000000 bytes")


@dataclass(frozen=True)
class SandboxConfig:
    mode: SandboxMode = SandboxMode.LOCAL
    e2b_api_key: str = ""
    e2b_template: str = "base"
    e2b_allow_internet: bool = False
    limits: SandboxLimits = field(default_factory=SandboxLimits)

    def __post_init__(self) -> None:
        if not self.e2b_template.strip():
            raise ValueError("E2B template must not be empty")


def load_sandbox_config(
    environ: Mapping[str, str] | None = None,
) -> SandboxConfig:
    env = os.environ if environ is None else environ
    raw_mode = env.get("SCA_SANDBOX_BACKEND", "local").strip().lower()
    try:
        mode = SandboxMode(raw_mode)
    except ValueError as error:
        raise ValueError(
            "SCA_SANDBOX_BACKEND must be 'local' or 'e2b'"
        ) from error
    return SandboxConfig(
        mode=mode,
        e2b_api_key=env.get("E2B_API_KEY", "").strip(),
        e2b_template=env.get("SCA_E2B_TEMPLATE", "base").strip(),
        e2b_allow_internet=_bool_value(
            env, "SCA_E2B_ALLOW_INTERNET", False
        ),
        limits=SandboxLimits(
            max_timeout_seconds=_float_value(
                env, "SCA_SANDBOX_MAX_TIMEOUT", 300.0
            ),
            max_output_chars=_int_value(
                env, "SCA_SANDBOX_MAX_OUTPUT", 100_000
            ),
            max_transfer_bytes=_int_value(
                env, "SCA_SANDBOX_MAX_TRANSFER", 50_000_000
            ),
        ),
    )


def _float_value(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be numeric") from error


def _int_value(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def _bool_value(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


__all__ = ["SandboxConfig", "SandboxLimits", "SandboxMode", "load_sandbox_config"]
