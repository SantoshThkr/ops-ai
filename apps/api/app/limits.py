"""Best-effort Redis rate limiting; local development remains usable without Redis."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from redis import Redis
from redis.exceptions import RedisError

from app.config import Settings, get_settings
from app.observability import log_event

logger = logging.getLogger(__name__)


@lru_cache(maxsize=4)
def _redis_client(redis_url: str) -> Any:
    # One pooled client per URL instead of a new connection pool per request.
    return Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=0.2,
        socket_timeout=0.2,
    )


def check_rate_limit(
    scope: str,
    identity: str,
    limit: int,
    window_seconds: int = 60,
    settings: Settings | None = None,
) -> bool:
    """Fixed-window counter. Fails open when Redis is unavailable (documented behavior)."""
    settings = settings or get_settings()
    key = f"opsai:rate:{scope}:{identity}"
    try:
        pipeline = _redis_client(settings.redis_url).pipeline(transaction=True)
        # The window's first request creates the key with its TTL; both commands commit
        # together, so a crash cannot leave a counter that never expires.
        pipeline.set(key, 0, ex=window_seconds, nx=True)
        pipeline.incr(key)
        _, count = pipeline.execute()
        return int(count) <= limit
    except (RedisError, OSError, TimeoutError):
        log_event(logger, "rate_limit.unavailable", scope=scope, status="fail_open")
        return True
    except (TypeError, ValueError):
        log_event(
            logger,
            "rate_limit.invalid_response",
            level=logging.WARNING,
            scope=scope,
            status="denied",
        )
        return False
