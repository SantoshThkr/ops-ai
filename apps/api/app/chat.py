import logging
from collections.abc import Iterator, Sequence
from typing import Any, cast

from openai import OpenAI

from app.config import Settings, get_settings
from app.schemas import SearchResult

logger = logging.getLogger(__name__)


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
    def stream(
        self,
        question: str,
        relevant_chunks: Sequence[SearchResult],
        history: Sequence[str],
    ) -> Iterator[str]:
        unique_chunks = canonicalize_sources(relevant_chunks)
        if not unique_chunks:
            answer = "I couldn't find enough information in your uploaded documents to answer that."
            for token in answer.split():
                yield f"{token} "
            return

        summary = "Based on your uploaded documents: "
        unique_documents: list[SearchResult] = []
        seen_documents: set[str] = set()
        for chunk in unique_chunks:
            document_key = str(chunk.document_id)
            if document_key in seen_documents:
                continue
            seen_documents.add(document_key)
            unique_documents.append(chunk)
        references = " ".join(
            f"{chunk.filename} supports this answer." for chunk in unique_documents[:2]
        )
        answer = f"{summary}{references}"
        for token in answer.split():
            yield f"{token} "


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
            logger.exception("OpenAI Responses API call failed; falling back to local provider")
            yield from LocalChatProvider(self.settings).stream(question, relevant_chunks, history)


def get_chat_provider(settings: Settings | None = None) -> BaseChatProvider:
    selected = (settings or get_settings()).chat_provider.lower()
    if selected == "openai":
        return OpenAIChatProvider(settings or get_settings())
    return LocalChatProvider(settings or get_settings())
