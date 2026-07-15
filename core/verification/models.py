from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json


@dataclass(frozen=True)
class GateSpec:
    name: str
    command: tuple[str, ...]
    timeout_seconds: float = 120.0
    required: bool = True


@dataclass(frozen=True)
class GateResult:
    gate_name: str
    command: tuple[str, ...]
    required: bool
    passed: bool
    exit_code: int | None
    duration_ms: int
    output_artifact: str
    output_excerpt: str
    timed_out: bool = False
    execution_backend: str = "local"
    isolated: bool = False


@dataclass(frozen=True)
class VerificationReport:
    attempt: int
    results: tuple[GateResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.passed or not result.required for result in self.results)

    @property
    def failure_fingerprint(self) -> str:
        failures = [
            {
                "gate": result.gate_name,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "output": result.output_excerpt.strip(),
            }
            for result in self.results
            if result.required and not result.passed
        ]
        if not failures:
            return ""
        payload = json.dumps(failures, ensure_ascii=True, sort_keys=True)
        return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VerificationConfig:
    gates: tuple[GateSpec, ...] = ()
    max_repair_attempts: int = 2

    @property
    def enabled(self) -> bool:
        return bool(self.gates)


__all__ = ["GateResult", "GateSpec", "VerificationConfig", "VerificationReport"]
