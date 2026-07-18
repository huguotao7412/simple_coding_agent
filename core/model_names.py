from __future__ import annotations


DEFAULT_MODEL = "deepseek-v4-pro"

_DEEPSEEK_PRO_ALIASES = {
    "deepseek-chat": DEFAULT_MODEL,
    "deepseek-reasoner": DEFAULT_MODEL,
}


def normalize_model_name(model_name: str | None, *, default: str = DEFAULT_MODEL) -> str:
    """Return the concrete model id SCA should send to the provider."""
    if model_name is None or not model_name.strip():
        return default
    normalized = model_name.strip()
    if "/" in normalized:
        _, normalized = normalized.split("/", 1)
    if not normalized:
        raise ValueError(f"invalid model name: {model_name!r}")
    return _DEEPSEEK_PRO_ALIASES.get(normalized, normalized)


__all__ = ["DEFAULT_MODEL", "normalize_model_name"]
