"""Unified logging configuration for Simple Coding Agent.

Controls:
  SCA_LOG_LEVEL  — DEBUG, INFO, WARNING, ERROR (default: INFO)
  SCA_LOG_JSON   — "1" / "true" / "yes" for JSON output (default: console)
"""

from __future__ import annotations

import logging
import os
import sys


def setup_logging(level: str | None = None, json_format: bool | None = None) -> None:
    """Configure root logger for the entire application.

    Args:
        level: Log level string. Defaults to SCA_LOG_LEVEL env or "INFO".
        json_format: If True, output JSON lines. Defaults to SCA_LOG_JSON env or False.
    """
    if level is None:
        level = os.getenv("SCA_LOG_LEVEL", "INFO")
    if json_format is None:
        json_format = os.getenv("SCA_LOG_JSON", "").lower() in ("1", "true", "yes")

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers to avoid duplication on re-config
    for h in root.handlers[:]:
        root.removeHandler(h)

    if json_format:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JSONFormatter())
        root.addHandler(handler)
    else:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)-8s %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(handler)

    # Suppress noisy third-party HTTP logs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class _JSONFormatter(logging.Formatter):
    """Structured JSON log formatter for machine consumption."""

    def format(self, record: logging.LogRecord) -> str:
        import json
        payload: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            payload["exc"] = str(record.exc_info[1])
        return json.dumps(payload, ensure_ascii=False)
