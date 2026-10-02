"""Redis list worker: python -m app.worker."""

import logging
import time
from typing import Any, cast
from uuid import UUID

from redis import Redis
from redis.exceptions import RedisError

from app.config import Settings, get_settings
from app.db import SessionLocal
from app.ingestion import enqueue
from app.observability import configure_logging, log_event
from app.processing import process_document, recover_stale_documents

logger = logging.getLogger(__name__)
_REDIS_RETRY_SECONDS = 2.0
_RECOVERY_INTERVAL_SECONDS = 60.0


def run_once(queue: Any, settings: Settings) -> bool:
    """Process at most one queued document. Returns False when Redis could not be read.

    Failures are logged and swallowed so one bad job or a transient database error
    does not stop the worker. A job lost to a crash is not replayed from the queue;
    recover_once() later marks its document failed (or re-queues it if never claimed).
    """
    try:
        item = cast(tuple[str, str] | None, queue.blpop([settings.queue_name], timeout=5))
    except (RedisError, OSError):
        log_event(logger, "worker.queue_unavailable", level=logging.WARNING, status="retrying")
        return False
    if item is None:
        return True
    try:
        document_id = UUID(item[1])
    except (ValueError, TypeError):
        log_event(logger, "worker.invalid_job", level=logging.WARNING, status="ignored")
        return True
    try:
        with SessionLocal() as db:
            processed = process_document(db, document_id)
        log_event(
            logger,
            "worker.job_finished",
            document_id=str(document_id),
            status="completed" if processed else "failed",
        )
    except Exception:
        logger.exception(
            "worker.job_crashed", extra={"document_id": str(document_id), "status": "failed"}
        )
    return True


def recover_once(settings: Settings) -> None:
    """Resolve documents whose job was lost; failures are logged and retried next interval."""
    try:
        with SessionLocal() as db:
            recover_stale_documents(db, lambda document_id: enqueue(document_id, settings))
    except Exception:
        logger.exception("worker.recovery_failed", extra={"status": "failed"})


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    queue = Redis.from_url(settings.redis_url, decode_responses=True)
    log_event(logger, "worker.started", queue=settings.queue_name)
    next_recovery = 0.0
    while True:
        # Runs at startup (picking up jobs lost by a previous crash) and then periodically.
        if time.monotonic() >= next_recovery:
            recover_once(settings)
            next_recovery = time.monotonic() + _RECOVERY_INTERVAL_SECONDS
        if not run_once(queue, settings):
            time.sleep(_REDIS_RETRY_SECONDS)


if __name__ == "__main__":
    main()
