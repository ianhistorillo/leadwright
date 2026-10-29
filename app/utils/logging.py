"""Structured logging helpers that never expose secrets."""

from __future__ import annotations

import logging
import re
from typing import Any

_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|password|secret|token|credential)",
    re.IGNORECASE,
)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger with a basic configured root if needed."""
    logger = logging.getLogger(name)
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
    return logger


def redact_secrets(value: Any) -> Any:
    """Recursively redact likely secret fields from dict-like structures."""
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SENSITIVE_KEY_RE.search(key):
                redacted[key] = "***REDACTED***"
            else:
                redacted[key] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str) and _SENSITIVE_KEY_RE.search(value):
        return "***REDACTED***"
    return value


def sanitize_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Return a copy of headers with credential values redacted."""
    if not headers:
        return {}
    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        if _SENSITIVE_KEY_RE.search(key):
            sanitized[key] = "***REDACTED***"
        else:
            sanitized[key] = value
    return sanitized
