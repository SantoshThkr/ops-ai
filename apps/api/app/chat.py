"""Grounded chat: answer providers and the per-message orchestration used by the API."""

import logging
import re
from collections.abc import Iterator, Sequence
from typing import Any, cast
from uuid import UUID

from openai import OpenAI
from sqlalchemy.orm import Session

from app.agent import AGENT_INTENTS, IntentKind, parse_intent
from app.agent import stream as stream_agent
from app.config import Settings, get_settings
from app.models import User
from app.observability import log_event
from app.retrieval import content_terms
from app.schemas import CitationResponse, SearchResult
from app.tools import search_knowledge

logger = logging.getLogger(__name__)

NO_CONTEXT_MESSAGE = "I couldn't find enough information in your uploaded documents to answer that."
CAPABILITIES_MESSAGE = (
    "I can answer questions about your uploaded documents, report recorded service metrics "
    "(for example “What is the latency?”), look up an incident by ID, or create an incident "
    "proposal for administrator approval."
)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n+")
_MAX_EXCERPT_CHARS = 280


def _source_key(chunk: SearchResult) -> tuple[str, str]:
    document_id = str(chunk.document_id)
    if chunk.page_number is not None:
        return (document_id, f"page:{chunk.page_number}")
    if chunk.chunk_id is not None:
        return (document_id, f"chunk:{chunk.chunk_id}")
    return (document_id, "default")


def canonicalize_sources(chunks: Sequence[SearchResult]) -> list[SearchResult]:
    seen: set[tuple[str, str]] = set()
    unique: list[SearchResult] = []
    for chunk in chunks:
        key = _source_key(chunk)
        if key in seen:
            continue
        seen.add(key)
        unique.append(chunk)
    return unique


def citation_payload(chunks: Sequence[SearchResult]) -> list[CitationResponse]:
    return [
        CitationResponse(
            document_id=chunk.document_id,
            filename=chunk.filename,
            chunk_id=chunk.chunk_id,
            page_number=chunk.page_number,
        )
        for chunk in canonicalize_sources(chunks)
    ]


def _word_tokens(text: str) -> Iterator[str]:
    """Stream text word by word while preserving line breaks."""
    for index, line in enumerate(text.split("\n")):
        if index:
            yield "\n"
        for word in line.split():
            yield f"{word} "


def _excerpt(sentence: str) -> str:
    if len(sentence) <= _MAX_EXCERPT_CHARS:
        return sentence
    return sentence[: _MAX_EXCERPT_CHARS - 1].rsplit(" ", 1)[0] + "…"


def extract_excerpts(
    question: str, chunks: Sequence[SearchResult], limit: int = 2
) -> list[tuple[str, SearchResult]]:
    """Pick the retrieved sentences sharing the most content terms with the question.

    Ties go to the higher-ranked chunk, then the earlier sentence. If nothing overlaps,
    the first sentence of the best chunk is used so the answer always quotes a source.
    """
    terms = content_terms(question)
    candidates: list[tuple[int, int, int, str, SearchResult]] = []
    for rank, chunk in enumerate(chunks):
        for position, raw in enumerate(_SENTENCE_BOUNDARY.split(chunk.content)):
            sentence = " ".join(raw.split())
            if len(sentence) < 3:
                continue
            overlap = len(terms & content_terms(sentence))
            candidates.append((overlap, rank, position, sentence, chunk))
    if not candidates:
        return []
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    picked: list[tuple[str, SearchResult]] = []
    seen: set[str] = set()
    for overlap, _, _, sentence, chunk in candidates:
        if overlap == 0 or sentence in seen:
            continue
        seen.add(sentence)
        picked.append((_excerpt(sentence), chunk))
        if len(picked) == limit:
            break
    if not picked:
        _, _, _, sentence, chunk = min(candidates, key=lambda item: (item[1], item[2]))
        picked.append((_excerpt(sentence), chunk))
    return picked


class BaseChatProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def stream(
        self,
        question: str,
        relevant_chunks: Sequence[SearchResult],
        history: Sequence[str],
    ) -> Iterator[str]:
        raise NotImplementedError


class LocalChatProvider(BaseChatProvider):
    """Deterministic extractive answers: quotes the most relevant retrieved sentences.

    It does not paraphrase, summarize, or reason across sources.
    """

    def stream(
        self,
        question: str,
        relevant_chunks: Sequence[SearchResult],
        history: Sequence[str],
    ) -> Iterator[str]:
        excerpts = extract_excerpts(question, canonicalize_sources(relevant_chunks))
        if not excerpts:
            yield from _word_tokens(NO_CONTEXT_MESSAGE)
            return
        lines = ["Relevant excerpts from your uploaded documents:"]
        for sentence, chunk in excerpts:
            page = f", page {chunk.page_number}" if chunk.page_number is not None else ""
            lines.append(f"“{sentence}” ({chunk.filename}{page})")
        yield from _word_tokens("\n".join(lines))


class OpenAIChatProvider(BaseChatProvider):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.client = OpenAI(
            api_key=settings.openai_api_key or "",
            timeout=settings.provider_timeout_seconds,
            max_retries=0,
        )

    def stream(
        self,
        question: str,
        relevant_chunks: Sequence[SearchResult],
        history: Sequence[str],
    ) -> Iterator[str]:
        if not self.settings.openai_api_key:
            yield from LocalChatProvider(self.settings).stream(question, relevant_chunks, history)
            return

        unique_chunks = canonicalize_sources(relevant_chunks)
        context_blocks = "\n\n".join(
            (
                f"Source: {chunk.filename} (page {chunk.page_number or 'unknown'}):"
                f"\n{chunk.content[:800]}"
            )
            for chunk in unique_chunks
        )
        history_text = "\n".join(history[-4:])
        system_prompt = (
            "You are a grounded assistant. Use only the retrieved document context. "
            "If it is insufficient, say you could not find enough information in the "
            "uploaded documents. Never invent facts or cite sources that were not "
            "retrieved."
        )
        input_payload: Any = {
            "role": "user",
            "content": (
                f"System policy: {system_prompt}\n\nConversation history:\n{history_text}\n\n"
                f"Retrieved context:\n{context_blocks}\n\nUser question: {question}"
            ),
        }

        try:
            response = self.client.responses.create(
                model=self.settings.openai_model,
                input=[cast(Any, input_payload)],
                stream=True,
            )
            for event in response:
                event_type = getattr(event, "type", "")
                if event_type == "response.output_text.delta":
                    delta = getattr(event, "delta", "")
                    if delta:
                        yield delta
                elif getattr(event, "type", "") == "response.output_text":
                    text = getattr(event, "text", "")
                    if text:
                        yield text
        except Exception:
            logger.exception(
                "chat.provider_failed", extra={"provider": "openai", "fallback": "local"}
            )
            yield from LocalChatProvider(self.settings).stream(question, relevant_chunks, history)


def get_chat_provider(settings: Settings | None = None) -> BaseChatProvider:
    selected = (settings or get_settings()).chat_provider.lower()
    if selected == "openai":
        return OpenAIChatProvider(settings or get_settings())
    return LocalChatProvider(settings or get_settings())


def stream_reply(
    db: Session,
    user: User,
    conversation_id: UUID,
    question: str,
    history: Sequence[str],
    settings: Settings | None = None,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield SSE-ready (event, payload) pairs for one user message.

    Tool intents (metrics, incidents, action requests) go to the deterministic agent.
    Everything else is answered from the user's own documents, or not at all.
    """
    settings = settings or get_settings()
    intent = parse_intent(question)
    if intent.kind in AGENT_INTENTS:
        yield from stream_agent(question, db, user, conversation_id=conversation_id)
        return

    log_event(
        logger,
        "agent.intent_classified",
        operation="intent_classification",
        user_id=str(user.id),
        status="completed",
        intent=intent.kind.value,
    )
    yield "tool_call", {"name": "search_knowledge", "arguments": {"query": question}}
    results = search_knowledge(db, user, question, settings.retrieval_top_k)
    yield (
        "tool_result",
        {"name": "search_knowledge", "result": {"count": len(results)}, "read_only": True},
    )
    if not results:
        text = NO_CONTEXT_MESSAGE
        if intent.kind == IntentKind.NONE:
            text = f"{text} {CAPABILITIES_MESSAGE}"
        for token in _word_tokens(text):
            yield "token", {"text": token}
        yield "done", {"status": "completed"}
        return

    for token in get_chat_provider(settings).stream(question, results, history):
        yield "token", {"text": token}
    citations = [item.model_dump(mode="json") for item in citation_payload(results)]
    yield "citation", {"citations": citations}
    yield "done", {"status": "completed", "citations": citations}
