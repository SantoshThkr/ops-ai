"""Document processing: claim, extract, chunk, embed, and recover abandoned jobs."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.ingestion import (
    EmbeddingProvider,
    Storage,
    chunk_text,
    embedding_provider,
    extract_text,
    storage,
)
from app.models import Document, DocumentChunk, DocumentStatus
from app.observability import elapsed_ms, log_event

logger = logging.getLogger(__name__)

# A job that has not finished within this window is treated as lost (worker crash or
# restart). Normal processing of a 10 MB upload finishes well inside it.
PROCESSING_TIMEOUT = timedelta(minutes=15)
ABANDONED_MESSAGE = "Processing did not finish (the worker may have restarted); upload again."


class UnreadableDocumentError(ValueError):
    """The file was valid but contained no extractable text (for example, a scanned PDF)."""


def process_document(
    db: Session,
    document_id: UUID,
    *,
    file_storage: Storage | None = None,
    embeddings: EmbeddingProvider | None = None,
    settings: Settings | None = None,
) -> bool:
    """Process an uploaded document. Returns True when the document ends up completed."""
    settings = settings or get_settings()
    started = time.monotonic()
    document = db.scalar(select(Document).where(Document.id == document_id))
    if document is None or document.status == DocumentStatus.COMPLETED:
        return document is not None
    if not claim_document(db, document_id):
        # Another job owns it, or it already failed: duplicate deliveries are no-ops.
        log_event(
            logger,
            "document.processing.skipped",
            document_id=str(document_id),
            status=document.status.value,
        )
        return False
    db.refresh(document)
    try:
        file_storage = file_storage or storage(settings)
        embeddings = embeddings or embedding_provider(settings)
        suffix = "." + document.filename.rsplit(".", 1)[-1].lower()
        pages = extract_text(file_storage.read(document.storage_key), suffix)
        chunks = chunk_text(pages, settings.chunk_size, settings.chunk_overlap)
        if not chunks:
            raise UnreadableDocumentError("No readable text found in the document")
        vectors = embeddings.embed([item[0] for item in chunks])
        if len(vectors) != len(chunks):
            raise ValueError("Embedding provider returned an invalid result")
        db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document.id))
        db.add_all(
            DocumentChunk(
                document_id=document.id,
                chunk_index=index,
                content=item[0],
                embedding=vectors[index],
                page_number=item[1],
                chunk_metadata=item[2],
            )
            for index, item in enumerate(chunks)
        )
        document.status = DocumentStatus.COMPLETED
        document.error_message = None
        db.commit()
        log_event(
            logger,
            "document.processing.completed",
            document_id=str(document_id),
            status="completed",
            chunk_count=len(chunks),
            duration_ms=elapsed_ms(started),
        )
        return True
    except Exception as error:
        db.rollback()
        document = db.scalar(select(Document).where(Document.id == document_id))
        if document is not None:
            document.status = DocumentStatus.FAILED
            # Only our own validation messages are shown to users; parser errors stay in logs.
            document.error_message = (
                str(error)
                if isinstance(error, UnreadableDocumentError)
                else "Document processing failed"
            )
            db.commit()
        logger.exception(
            "document.processing.failed",
            extra={
                "document_id": str(document_id),
                "status": "failed",
                "duration_ms": elapsed_ms(started),
            },
        )
        return False


def claim_document(db: Session, document_id: UUID) -> bool:
    """Atomically move an uploaded document to processing; only one job can win."""
    result = cast(
        CursorResult[Any],
        db.execute(
            update(Document)
            .where(Document.id == document_id, Document.status == DocumentStatus.UPLOADED)
            .values(status=DocumentStatus.PROCESSING, error_message=None, updated_at=func.now())
        ),
    )
    db.commit()
    return result.rowcount == 1


def recover_stale_documents(
    db: Session,
    requeue: Callable[[UUID], bool],
    *,
    timeout: timedelta = PROCESSING_TIMEOUT,
) -> tuple[int, int]:
    """Resolve documents whose job was lost. Returns (marked_failed, requeued).

    Abandoned "processing" documents are marked failed rather than retried, so a file
    that crashes the worker cannot cause a crash loop. "Uploaded" documents that no job
    ever claimed are queued again, at most once per timeout window.
    """
    cutoff = datetime.now(UTC) - timeout
    abandoned = cast(
        CursorResult[Any],
        db.execute(
            update(Document)
            .where(Document.status == DocumentStatus.PROCESSING, Document.updated_at < cutoff)
            .values(
                status=DocumentStatus.FAILED,
                error_message=ABANDONED_MESSAGE,
                updated_at=func.now(),
            )
        ),
    ).rowcount
    db.commit()
    waiting = db.scalars(
        select(Document.id)
        .where(Document.status == DocumentStatus.UPLOADED, Document.updated_at < cutoff)
        .limit(100)
    ).all()
    requeued = 0
    for document_id in waiting:
        if requeue(document_id):
            db.execute(
                update(Document)
                .where(Document.id == document_id, Document.status == DocumentStatus.UPLOADED)
                .values(updated_at=func.now())
            )
            requeued += 1
    db.commit()
    if abandoned or requeued:
        log_event(
            logger,
            "document.recovery.completed",
            status="completed",
            marked_failed=abandoned,
            requeued=requeued,
        )
    return abandoned, requeued
