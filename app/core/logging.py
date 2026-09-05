"""Structured logging configuration.

Every log record carries a timestamp, level, event name and (when available)
the correlation id. Secrets are scrubbed defensively: even if a caller passes a
credential-looking key it is replaced before the record is rendered.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

from app.core.correlation import get_correlation_id

_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passphrase",
        "secret",
        "api_secret",
        "api_key",
        "apikey",
        "token",
        "bot_token",
        "authorization",
        "signature",
        "private_key",
        "secret_key",
        "webhook_secret",
        "encryption_key",
        "credentials",
        "pepper",
        "x-bapi-sign",
        "ok-access-sign",
        "ok-access-passphrase",
        "x-mbx-apikey",
    }
)

_BOT_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b")
_MASK = "***redacted***"


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return value
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS:
                out[key] = _MASK
            else:
                out[key] = _scrub(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub(item, depth + 1) for item in value)
    if isinstance(value, str):
        return _BOT_TOKEN_RE.sub(_MASK, value)
    return value


def _scrub_processor(_logger: Any, _name: str, event_dict: dict) -> dict:
    return _scrub(event_dict)


def _correlation_processor(_logger: Any, _name: str, event_dict: dict) -> dict:
    cid = get_correlation_id()
    if cid and "correlation_id" not in event_dict:
        event_dict["correlation_id"] = cid
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    """Configure structlog + stdlib logging once at process start."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
        force=True,
    )
    # Third-party loggers are noisy at INFO; keep them at WARNING.
    for noisy in ("aiogram.event", "httpx", "httpcore", "asyncio", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _correlation_processor,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _scrub_processor,
    ]
    if json_output:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
