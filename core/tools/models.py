from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from ..security.models import Capability, RiskLevel


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({
            str(key): _freeze(item) for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def mutable_arguments(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return an adapter-owned copy without exposing the authorized envelope."""
    def thaw(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {str(key): thaw(child) for key, child in item.items()}
        if isinstance(item, (tuple, frozenset)):
            return [thaw(child) for child in item]
        return item

    return {str(key): thaw(item) for key, item in value.items()}


@dataclass(frozen=True)
class ToolCall:
    """Final model-produced tool call presented to the application PEP."""

    call_id: str
    name: str
    arguments: Mapping[str, Any]
    run_id: str
    actor_id: str
    role: str
    workspace_identity: str
    correlation_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", _freeze(self.arguments))


@dataclass(frozen=True)
class AuthorizedToolCall:
    """Immutable call envelope after deterministic authorization."""

    call: ToolCall
    canonical_arguments: Mapping[str, Any]
    capabilities: frozenset[Capability]
    risk: RiskLevel
    action_fingerprint: str
    approval_consumed: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "canonical_arguments",
            _freeze(self.canonical_arguments),
        )


__all__ = ["AuthorizedToolCall", "ToolCall", "mutable_arguments"]
