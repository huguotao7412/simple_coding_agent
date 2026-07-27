from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..runs.context import RunContext
from .composite_guard import CompositeContentGuardProvider
from .content_guard import ContentGuardProvider
from .egress import DataEgressPolicy
from .local_guard import LocalContentGuardProvider
from .manager import SecurityManager
from .models import GuardrailMetrics, SecurityMode
from .openai_guard import OpenAIGuardrailsProvider
from .tool_security import SecurityMiddleware


def build_security_manager(
    workspace: str,
    run_context: RunContext,
    *,
    environ: dict[str, str] | None = None,
    external_runtime: Any = None,
) -> SecurityManager:
    values = os.environ if environ is None else environ
    mode = _security_mode(values)
    providers: list[ContentGuardProvider] = []
    if mode is not SecurityMode.OFF:
        providers.append(LocalContentGuardProvider())

    external, warning = _external_provider(
        mode,
        values,
        external_runtime,
    )
    if external is not None:
        providers.append(external)

    base_url = values.get(
        "SCA_GUARDRAILS_BASE_URL",
        "https://api.openai.com",
    )
    host = (
        base_url.split("://", 1)[-1]
        .split("/", 1)[0]
        .split(":", 1)[0]
        .lower()
    )
    return SecurityManager(
        mode=mode,
        guard=CompositeContentGuardProvider(providers, mode),
        middleware=SecurityMiddleware(workspace),
        run_context=run_context,
        egress=DataEgressPolicy(
            allow_external_guardrails=mode in {
                SecurityMode.HYBRID,
                SecurityMode.STRICT,
            },
            allowed_provider_hosts=frozenset({host}),
        ),
        metrics=GuardrailMetrics(),
        startup_warning=warning,
        provider_url=base_url,
    )


def _security_mode(values: Mapping[str, str]) -> SecurityMode:
    raw_mode = values.get("SCA_SECURITY_MODE", "local").strip().lower()
    try:
        return SecurityMode(raw_mode)
    except ValueError as error:
        raise ValueError(f"invalid SCA_SECURITY_MODE: {raw_mode}") from error


def _external_provider(
    mode: SecurityMode,
    values: Mapping[str, str],
    external_runtime: Any,
) -> tuple[OpenAIGuardrailsProvider | None, str]:
    if mode not in {SecurityMode.HYBRID, SecurityMode.STRICT}:
        return None, ""
    config = values.get("SCA_GUARDRAILS_CONFIG", "").strip()
    api_key = values.get("SCA_GUARDRAILS_API_KEY", "").strip()
    dependency = (
        OpenAIGuardrailsProvider.dependency_available()
        or external_runtime is not None
    )
    if not (dependency and config and api_key):
        if mode is SecurityMode.STRICT:
            reason = (
                "dependency missing"
                if not dependency
                else "trusted config or dedicated API key missing"
            )
            raise RuntimeError(
                "strict security mode unavailable: "
                f"OpenAI Guardrails {reason}"
            )
        return None, (
            "Hybrid mode degraded to local: OpenAI Guardrails dependency "
            "or trusted config is unavailable."
        )
    if not Path(config).is_absolute():
        raise ValueError(
            "SCA_GUARDRAILS_CONFIG must be a trusted absolute path"
        )

    context: Any = None
    if external_runtime is None:
        openai_module = __import__("openai")
        context = SimpleNamespace(
            guardrail_llm=openai_module.AsyncOpenAI(
                api_key=api_key,
                base_url=values.get(
                    "SCA_GUARDRAILS_BASE_URL",
                    "https://api.openai.com/v1",
                ),
            )
        )
    return OpenAIGuardrailsProvider(
        config_source=config,
        timeout=float(values.get("SCA_GUARDRAILS_TIMEOUT", "10")),
        concurrency=int(values.get("SCA_GUARDRAILS_MAX_CONCURRENCY", "4")),
        runtime=external_runtime,
        context=context,
        max_calls=int(values.get("SCA_GUARDRAILS_MAX_CALLS", "20")),
        max_tokens=int(values.get("SCA_GUARDRAILS_MAX_TOKENS", "100000")),
        failure_threshold=int(
            values.get("SCA_GUARDRAILS_FAILURE_THRESHOLD", "3")
        ),
    ), ""


__all__ = ["build_security_manager"]
