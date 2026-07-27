from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


REDACTED = "[REDACTED]"
SECRET_ENV_NAMES = {
    "SCA_API_KEY",
    "SCA_GUARDRAILS_API_KEY",
    "E2B_API_KEY",
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
    "GITLAB_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AZURE_CLIENT_SECRET",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
}
SENSITIVE_KEYS = re.compile(
    r"(?:api[-_]?key|authorization|password|passwd|secret|token|credential|cookie)",
    re.IGNORECASE,
)
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.DOTALL)),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("generic_secret", re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*([\"']?)[^\s,\"']{8,}\2")),
)


@dataclass(frozen=True)
class RedactionResult:
    value: Any
    count: int
    categories: tuple[str, ...]


def _known_values(environ: dict[str, str] | None = None) -> tuple[str, ...]:
    source = os.environ if environ is None else environ
    return tuple(
        value for name in SECRET_ENV_NAMES
        if len(value := source.get(name, "")) >= 6
    )


def redact_text(text: str, environ: dict[str, str] | None = None) -> RedactionResult:
    output = text
    count = 0
    categories: set[str] = set()
    for secret in _known_values(environ):
        occurrences = output.count(secret)
        if occurrences:
            output = output.replace(secret, REDACTED)
            count += occurrences
            categories.add("known_environment_secret")
    for category, pattern in PATTERNS:
        output, found = pattern.subn(REDACTED, output)
        if found:
            count += found
            categories.add(category)
    output, url_count = _redact_urls(output)
    if url_count:
        count += url_count
        categories.add("url_credential")
    return RedactionResult(output, count, tuple(sorted(categories)))


def _redact_urls(text: str) -> tuple[str, int]:
    count = 0
    url_pattern = re.compile(r"https?://[^\s<>'\"]+")

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        raw = match.group(0)
        try:
            parts = urlsplit(raw)
        except ValueError:
            return raw
        hostname = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        netloc = f"{hostname}{port}"
        changed = bool(parts.username or parts.password)
        query: list[tuple[str, str]] = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if SENSITIVE_KEYS.search(key):
                query.append((key, REDACTED))
                changed = True
            else:
                query.append((key, value))
        if not changed:
            return raw
        count += 1
        return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), parts.fragment))

    return url_pattern.sub(replace, text), count


def redact_structure(value: Any, environ: dict[str, str] | None = None) -> RedactionResult:
    categories: set[str] = set()
    count = 0

    def walk(item: Any, key: str = "") -> Any:
        nonlocal count
        if SENSITIVE_KEYS.search(key):
            count += 1
            categories.add("sensitive_field")
            return REDACTED
        if isinstance(item, dict):
            return {str(k): walk(v, str(k)) for k, v in item.items()}
        if isinstance(item, list):
            return [walk(v) for v in item]
        if isinstance(item, tuple):
            return tuple(walk(v) for v in item)
        if isinstance(item, str):
            result = redact_text(item, environ)
            count += result.count
            categories.update(result.categories)
            return result.value
        return item

    return RedactionResult(walk(value), count, tuple(sorted(categories)))


def sanitized_subprocess_environment(
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    source = dict(os.environ if environ is None else environ)
    return {
        key: value
        for key, value in source.items()
        if key not in SECRET_ENV_NAMES and not SENSITIVE_KEYS.search(key)
    }


__all__ = [
    "REDACTED",
    "RedactionResult",
    "redact_structure",
    "redact_text",
    "sanitized_subprocess_environment",
]
