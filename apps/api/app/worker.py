"""Redis list worker: python -m app.worker."""

import logging
from typing import cast
from uuid import UUID

from redis import Redis

from app.config import get_settings
from app.db import SessionLocal
from app.processing import process_document

logging.basicConfig(level=logging.INFO)


def main() -> None:
    settings = get_settings()
    queue = Redis.from_url(settings.redis_url, decode_responses=True)
    while True:
        item = cast(
            tuple[str, str] | None,
            queue.blpop([settings.queue_name], timeout=5),
        )
        if item is None:
            continue
        try:
            with SessionLocal() as db:
                process_document(db, UUID(item[1]))
        except (ValueError, TypeError):
            logging.getLogger(__name__).warning("Ignoring invalid document queue item")


if __name__ == "__main__":
    main()
