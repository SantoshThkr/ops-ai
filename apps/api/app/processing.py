"""Idempotent document processing worker."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import delete, select
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

logger = logging.getLogger(__name__)


def process_document(
    db: Session,
    document_id: UUID,
    *,
    file_storage: Storage | None = None,
    embeddings: EmbeddingProvider | None = None,
    settings: Settings | None = None,
) -> bool:
    settings = settings or get_settings()
    document = db.scalar(select(Document).where(Document.id == document_id))
    if document is None or document.status == DocumentStatus.COMPLETED:
        return document is not None
    document.status = DocumentStatus.PROCESSING
    document.error_message = None
    db.commit()
    try:
        file_storage = file_storage or storage(settings)
        embeddings = embeddings or embedding_provider(settings)
        suffix = "." + document.filename.rsplit(".", 1)[-1].lower()
        pages = extract_text(file_storage.read(document.storage_key), suffix)
        chunks = chunk_text(pages, settings.chunk_size, settings.chunk_overlap)
        if not chunks:
            raise ValueError("No readable text found")
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
        db.commit()
        return True
    except Exception:
        db.rollback()
        document = db.scalar(select(Document).where(Document.id == document_id))
        if document is not None:
            document.status = DocumentStatus.FAILED
            document.error_message = "Document processing failed"
            db.commit()
        logger.exception("Document %s processing failed", document_id)
        return False
