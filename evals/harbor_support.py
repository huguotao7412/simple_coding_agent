from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.model_names import normalize_model_name


SUMMARY_FILENAME = "sca-run.json"
TRACE_FILENAME = "run-trace.jsonl"
DEFAULT_HARBOR_DATASET = "swe-rebench/swe-rebench-leaderboard"
HARBOR_AGENT_IMPORT = "evals.harbor_agent:SimpleCodingAgent"
PROXY_ENVIRONMENT_KEYS = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
}
_LOOPBACK_PROXY_HOST = re.compile(
    r"(?i)(://(?:[^/@]+@)?)(?:localhost|127\.0\.0\.1|\[::1\])(?=[:/]|$)"
)


def normalize_harbor_model(model_name: str | None) -> str:
    """Translate Harbor's provider/model identifier to the SCA model name."""
    if not model_name or not model_name.strip():
        raise ValueError("Harbor must provide a non-empty model name")
    try:
        normalized = normalize_model_name(model_name)
    except ValueError:
        raise ValueError(f"invalid Harbor model name: {model_name!r}")
    return normalized


def _container_proxy_url(value: str) -> str:
    """Translate a host loopback proxy URL for use inside Docker Desktop."""
    return _LOOPBACK_PROXY_HOST.sub(r"\1host.docker.internal", value, count=1)


def container_environment(
    model_name: str | None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the explicit environment passed to SCA inside a Harbor sandbox."""
    source = os.environ if environ is None else environ
    result: dict[str, str] = {}
    for key, value in source.items():
        if key in PROXY_ENVIRONMENT_KEYS:
            result[key] = _container_proxy_url(value)
        elif key.startswith("SCA_") and key not in {
            "SCA_HARBOR_WHEEL",
            "SCA_CONFIG_HOME",
            "SCA_STATE_HOME",
        }:
            result[key] = value
    result["SCA_MODEL"] = normalize_harbor_model(model_name)
    result["SCA_SANDBOX_BACKEND"] = "local"
    result["SCA_STATE_HOME"] = "/logs/artifacts/sca"
    return result


def load_run_summary(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError(f"unsupported SCA Harbor summary: {path}")
    return payload


def apply_summary_to_context(context: Any, payload: Mapping[str, Any]) -> None:
    """Populate Harbor's AgentContext without importing Harbor in this module."""
    context.n_input_tokens = int(payload.get("prompt_tokens", 0) or 0)
    context.n_output_tokens = int(payload.get("completion_tokens", 0) or 0)
    existing = context.metadata if isinstance(context.metadata, dict) else {}
    context.metadata = {
        **existing,
        "simple_coding_agent": {
            "duration_ms": int(payload.get("duration_ms", 0) or 0),
            "tool_calls": int(payload.get("tool_calls", 0) or 0),
            "failed_tool_calls": int(payload.get("failed_tool_calls", 0) or 0),
            "usage_estimated": bool(payload.get("usage_estimated", False)),
            "runtime_error": payload.get("runtime_error"),
            "trace_file": TRACE_FILENAME,
            "summary_file": SUMMARY_FILENAME,
        },
    }


__all__ = [
    "DEFAULT_HARBOR_DATASET",
    "HARBOR_AGENT_IMPORT",
    "PROXY_ENVIRONMENT_KEYS",
    "SUMMARY_FILENAME",
    "TRACE_FILENAME",
    "apply_summary_to_context",
    "container_environment",
    "load_run_summary",
    "normalize_harbor_model",
]
