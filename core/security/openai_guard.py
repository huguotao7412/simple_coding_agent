from __future__ import annotations

import asyncio
import importlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from .models import (
    ContentGuardAssessment,
    ContentGuardRequest,
    GuardOutcome,
    GuardUsage,
    RiskLevel,
)


RuntimeCallable = Callable[..., Awaitable[list[Any]]]


class OpenAIGuardrailsProvider:
    name = "openai_guardrails"

    def __init__(
        self,
        *,
        config_source: str,
        timeout: float = 10.0,
        concurrency: int = 4,
        model: str = "",
        runtime: RuntimeCallable | None = None,
        context: Any = None,
        max_calls: int = 20,
        max_tokens: int = 100_000,
        failure_threshold: int = 3,
    ) -> None:
        if timeout <= 0:
            raise ValueError("guardrail timeout must be positive")
        if concurrency <= 0:
            raise ValueError("guardrail concurrency must be positive")
        if max_calls <= 0 or max_tokens <= 0 or failure_threshold <= 0:
            raise ValueError("guardrail budgets and failure threshold must be positive")
        self.config_source = config_source
        self.timeout = timeout
        self.concurrency = concurrency
        self.model = model.strip()
        self._runtime = runtime
        self.context = context
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self.failure_threshold = failure_threshold
        self._calls = 0
        self._tokens = 0
        self._consecutive_failures = 0

    @staticmethod
    def dependency_available() -> bool:
        try:
            importlib.import_module("guardrails")
        except ImportError:
            return False
        return True

    def _resolve_runtime(self) -> RuntimeCallable:
        if self._runtime is not None:
            return self._runtime
        module = importlib.import_module("guardrails")
        return cast(RuntimeCallable, module.check_plain_text)

    async def inspect(
        self,
        request: ContentGuardRequest,
    ) -> ContentGuardAssessment:
        started = time.perf_counter()
        if (
            self._calls >= self.max_calls
            or self._tokens >= self.max_tokens
            or self._consecutive_failures >= self.failure_threshold
        ):
            return ContentGuardAssessment(
                provider=self.name,
                outcome=GuardOutcome.ERROR,
                risk_level=RiskLevel.HIGH,
                reason="External guardrail budget or circuit breaker is closed.",
                provider_error="GuardrailCircuitOpen: guardrail unavailable",
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        self._calls += 1
        try:
            runtime = self._resolve_runtime()
            async with asyncio.timeout(self.timeout):
                results = await runtime(
                    request.text,
                    bundle_path=self._bundle_source(),
                    ctx=self.context,
                    concurrency=self.concurrency,
                    suppress_tripwire=True,
                    raise_guardrail_errors=True,
                    stage_name=request.stage.value,
                )
            assessment = self._map_results(
                results,
                (time.perf_counter() - started) * 1000,
            )
            self._tokens += assessment.usage.total_tokens
            self._consecutive_failures = 0
            return assessment
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._consecutive_failures += 1
            return ContentGuardAssessment(
                provider=self.name,
                outcome=GuardOutcome.ERROR,
                risk_level=RiskLevel.HIGH,
                reason="External guardrail inspection failed.",
                provider_error=f"{type(error).__name__}: guardrail unavailable",
                latency_ms=(time.perf_counter() - started) * 1000,
            )

    def _bundle_source(self) -> str | dict[str, Any]:
        """Apply the trusted model override to real 0.2.x config model fields."""
        if not self.model:
            return self.config_source
        with open(self.config_source, encoding="utf-8") as config_file:
            bundle = json.load(config_file)
        if not isinstance(bundle, dict):
            raise ValueError("guardrail config must be an object")
        guardrails = bundle.get("guardrails")
        if not isinstance(guardrails, list):
            raise ValueError("guardrail config must contain a guardrails array")
        for item in guardrails:
            if not isinstance(item, dict):
                raise ValueError("guardrail entries must be objects")
            config = item.get("config")
            if isinstance(config, dict) and "model" in config:
                config["model"] = self.model
        return bundle

    def _map_results(
        self,
        results: Any,
        latency_ms: float,
    ) -> ContentGuardAssessment:
        if not isinstance(results, list):
            raise ValueError("guardrail runtime returned a malformed result")
        tripwires = False
        rule_ids: list[str] = []
        prompt_tokens = 0
        completion_tokens = 0
        for result in results:
            execution_failed = getattr(result, "execution_failed", None)
            if not isinstance(execution_failed, bool):
                raise ValueError("guardrail result missing execution_failed")
            if execution_failed:
                raise ValueError("guardrail reported an execution failure")
            tripwire = getattr(result, "tripwire_triggered", None)
            if not isinstance(tripwire, bool):
                raise ValueError("guardrail result missing tripwire_triggered")
            tripwires = tripwires or tripwire
            info = getattr(result, "info", None)
            if not isinstance(info, dict):
                raise ValueError("guardrail result info must be an object")
            name = info.get("guardrail_name") or info.get("name")
            if isinstance(name, str):
                rule_ids.append(f"OPENAI-{name}")
            usage = info.get("token_usage") or info.get("usage")
            if isinstance(usage, dict):
                prompt_tokens += _nonnegative_int(
                    usage.get("prompt_tokens", 0),
                    "prompt_tokens",
                )
                completion_tokens += _nonnegative_int(
                    usage.get("completion_tokens", 0),
                    "completion_tokens",
                )
        return ContentGuardAssessment(
            provider=self.name,
            outcome=(
                GuardOutcome.DENY if tripwires else GuardOutcome.ALLOW
            ),
            risk_level=RiskLevel.HIGH if tripwires else RiskLevel.LOW,
            categories=("external_tripwire",) if tripwires else (),
            rule_ids=tuple(rule_ids),
            reason=(
                "External guardrail tripwire triggered." if tripwires else ""
            ),
            tripwire_triggered=tripwires,
            usage=GuardUsage(prompt_tokens, completion_tokens),
            latency_ms=latency_ms,
        )


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"guardrail usage {field} is malformed")
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError) as error:
        raise ValueError(f"guardrail usage {field} is malformed") from error
    if parsed < 0:
        raise ValueError(f"guardrail usage {field} is malformed")
    return parsed


__all__ = ["OpenAIGuardrailsProvider", "RuntimeCallable"]
