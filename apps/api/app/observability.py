from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    request_id = request_id_context.get()
    if request_id is not None:
        fields.setdefault("request_id", request_id)
    logger.info(event, extra=fields)


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
            status="failed",
            duration_ms=round((time.monotonic() - started) * 1000, 2),
            **fields,
        )
        raise
    else:
        log_event(
            logger,
            f"{operation}.completed",
            status="completed",
            duration_ms=round((time.monotonic() - started) * 1000, 2),
            **fields,
        )
