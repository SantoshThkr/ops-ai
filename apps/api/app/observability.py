from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SENSITIVE_KEY_PARTS = ("password", "secret", "token", "cookie", "authorization", "api_key")
# Attributes present on every LogRecord; anything else was passed through `extra`.
_RESERVED_RECORD_KEYS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys() | {"message", "asctime"}
)


def safe_request_id(candidate: str | None) -> str | None:
    """Accept caller-supplied correlation IDs only when they are short and log-safe."""
    if candidate and _REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return None


def _redact(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
        return "[redacted]"
    return value


class JsonFormatter(logging.Formatter):
    """One JSON object per line: timestamp, level, logger, event, and structured fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_KEYS and not key.startswith("_"):
                payload[key] = _redact(key, value)
        request_id = request_id_context.get()
        if request_id is not None:
            payload.setdefault("request_id", request_id)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON handler on the root logger once; safe to call repeatedly."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    if any(isinstance(handler.formatter, JsonFormatter) for handler in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    request_id = request_id_context.get()
    if request_id is not None:
        fields.setdefault("request_id", request_id)
    logger.log(level, event, extra=fields)


@contextmanager
def timed_operation(
    logger: logging.Logger,
    operation: str,
    **fields: Any,
) -> Iterator[None]:
    started = time.monotonic()
    try:
        yield
    except Exception:
        log_event(
            logger,
            f"{operation}.failed",
            level=logging.WARNING,
            status="failed",
            duration_ms=elapsed_ms(started),
            **fields,
        )
        raise
    else:
        log_event(
            logger,
            f"{operation}.completed",
            status="completed",
            duration_ms=elapsed_ms(started),
            **fields,
        )


def elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 2)
