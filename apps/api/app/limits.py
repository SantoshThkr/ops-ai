"""Best-effort Redis rate limiting; local development remains usable without Redis."""

from __future__ import annotations

import logging
from typing import Any

from redis import Redis
from redis.exceptions import RedisError

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


def check_rate_limit(
    scope: str,
    identity: str,
    limit: int,
    window_seconds: int = 60,
    settings: Settings | None = None,
) -> bool:
    settings = settings or get_settings()
    key = f"opsai:rate:{scope}:{identity}"
    try:
        client: Any = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
        )
        count = int(client.incr(key))
        if count == 1:
            client.expire(key, window_seconds)
        return count <= limit
    except (RedisError, OSError, TimeoutError):
        logger.info("rate_limit_unavailable", extra={"scope": scope})
        return True
    except ValueError:
        logger.warning("rate_limit_invalid_response", extra={"scope": scope})
        return False
