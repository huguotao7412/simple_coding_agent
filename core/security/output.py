from __future__ import annotations

from dataclasses import dataclass

from .redaction import RedactionResult, redact_text


@dataclass(frozen=True)
class SanitizedOutput:
    value: str
    redaction: RedactionResult
    truncated: bool
    original_bytes: int


@dataclass(frozen=True)
class OutputSanitizer:
    """Pure local redaction and byte limiting; it performs no external I/O."""

    max_output_bytes: int = 65_536

    def sanitize(self, value: str) -> SanitizedOutput:
        redaction = redact_text(value)
        output = str(redaction.value)
        encoded = output.encode("utf-8")
        truncated = len(encoded) > self.max_output_bytes
        if truncated:
            output = (
                encoded[: self.max_output_bytes].decode("utf-8", errors="ignore")
                + "\n[OUTPUT TRUNCATED BY SECURITY POLICY]"
            )
        return SanitizedOutput(
            value=output,
            redaction=redaction,
            truncated=truncated,
            original_bytes=len(encoded),
        )


__all__ = ["OutputSanitizer", "SanitizedOutput"]
