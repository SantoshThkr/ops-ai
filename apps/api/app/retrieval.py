"""Owner-scoped vector retrieval shared by the search API, chat, agent tools, and MCP."""

from __future__ import annotations

import logging
import math
import re
import time

from sqlalchemy import Float, bindparam, literal_column, select, type_coerce
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.ingestion import embedding_provider
from app.models import Document, DocumentChunk, DocumentStatus, User
from app.observability import elapsed_ms, log_event
from app.schemas import SearchResult

logger = logging.getLogger(__name__)

# Function words carry no topical signal. They are ignored only by the lexical guard
# below; stored embeddings are unchanged.
STOPWORDS = frozenset(
    """a about an and any are as at be been but by can could did do does for from had has
    have how i if in into is it its just me my no not of on or our please should so some
    something tell than that the their them then there these they this to up us was we
    were what when where which who whom why will with would you your""".split()
)


def content_terms(text: str) -> set[str]:
    """Non-stopword terms truncated to five characters, so "configure" matches "configured"."""
    return {
        token[:5]
        for token in re.findall(r"\w+", text.casefold())
        if len(token) > 1 and token not in STOPWORDS
    }


def cosine(left: list[float], right: list[float]) -> float:
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    return (
        sum(a * b for a, b in zip(left, right, strict=False)) / denominator if denominator else 0.0
    )


def _uses_lexical_guard(settings: Settings) -> bool:
    # The local hashed bag-of-words embedding lets shared function words ("what is the")
    # clear the similarity threshold. Requiring at least one shared content term keeps
    # off-topic questions ungrounded. Semantic providers can match paraphrases, so they
    # are not filtered.
    return settings.embedding_provider.lower() == "local"


def search_owned_chunks(
    db: Session,
    user: User,
    query: str,
    top_k: int,
    settings: Settings | None = None,
) -> list[SearchResult]:
    """Return the caller's completed-document chunks at or above the similarity threshold."""
    settings = settings or get_settings()
    started = time.monotonic()
    guard = _uses_lexical_guard(settings)
    candidate_limit = min(top_k * 4, 200) if guard else top_k
    query_vector = embedding_provider(settings).embed([query])[0]
    owned = (Document.owner_id == user.id, Document.status == DocumentStatus.COMPLETED)

    dialect_name = db.bind.dialect.name if db.bind is not None else "sqlite"
    scored: list[tuple[float, DocumentChunk, str]]
    if dialect_name == "postgresql":
        from pgvector.sqlalchemy import Vector

        query_param = bindparam(
            "query_embedding", value=query_vector, type_=Vector(settings.embedding_dimension)
        )
        distance = DocumentChunk.embedding.op("<=>")(query_param)
        # A concrete type keeps the select() row tuple typed (SQLAlchemy 2.1 stubs).
        similarity = type_coerce(literal_column("1.0") - distance, Float).label("similarity")
        vector_rows = db.execute(
            select(DocumentChunk, Document.filename, similarity)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(*owned)
            .order_by(similarity.desc(), DocumentChunk.id.asc())
            .limit(candidate_limit)
        ).all()
        scored = [(float(score), chunk, filename) for chunk, filename, score in vector_rows]
    else:
        # Portable fallback used by SQLite unit tests and the offline evaluator.
        rows = db.execute(
            select(DocumentChunk, Document.filename)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(*owned)
        ).all()
        scored = [
            (cosine(query_vector, list(chunk.embedding)), chunk, filename)
            for chunk, filename in rows
        ]
        scored.sort(key=lambda item: (-item[0], str(item[1].id)))
        scored = scored[:candidate_limit]

    query_terms = content_terms(query)
    results = [
        SearchResult(
            document_id=chunk.document_id,
            chunk_id=chunk.id,
            filename=filename,
            content=chunk.content,
            page_number=chunk.page_number,
            similarity=round(score, 6),
        )
        for score, chunk, filename in scored
        if score >= settings.retrieval_similarity_threshold
        and (not guard or query_terms & content_terms(chunk.content))
    ][:top_k]
    log_event(
        logger,
        "rag.retrieval.completed",
        operation="rag_retrieval",
        user_id=str(user.id),
        status="completed",
        candidate_count=len(scored),
        result_count=len(results),
        lexical_guard=guard,
        duration_ms=elapsed_ms(started),
    )
    return results
