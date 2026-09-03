"""Document validation, storage, extraction, chunking, embeddings, and queueing."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import re
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol
from uuid import UUID

from fastapi import UploadFile
from pypdf import PdfReader
from redis import Redis
from redis.exceptions import RedisError

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

ALLOWED_TYPES = {
    ".pdf": {"application/pdf"},
    ".txt": {"text/plain"},
    ".md": {"text/markdown", "text/plain"},
    ".markdown": {"text/markdown", "text/plain"},
}


class Storage(Protocol):
    def save(self, key: str, data: bytes) -> None: ...
    def read(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class LocalFileStorage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError("Invalid storage key")
        return path

    def save(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()


def storage(settings: Settings | None = None) -> LocalFileStorage:
    return LocalFileStorage((settings or get_settings()).storage_dir)


def validate_upload(upload: UploadFile, data: bytes, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    filename = (upload.filename or "").strip()
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_TYPES:
        raise ValueError("Only PDF, TXT, and Markdown files are supported")
    if upload.content_type not in ALLOWED_TYPES[suffix]:
        raise ValueError("File type does not match its extension")
    if not data:
        raise ValueError("The uploaded file is empty")
    if len(data) > settings.max_file_size:
        raise OverflowError("The uploaded file is too large")
    if suffix == ".pdf" and not data.startswith(b"%PDF-"):
        raise ValueError("The uploaded file is not a valid PDF")
    if suffix != ".pdf":
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("Text files must be UTF-8 encoded") from error
        if b"\x00" in data:
            raise ValueError("Binary files are not supported")
    return suffix


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_text(data: bytes, suffix: str) -> list[tuple[str, int | None]]:
    if suffix == ".pdf":
        pages: list[tuple[str, int | None]] = []
        reader = PdfReader(io.BytesIO(data))
        for index, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append((text, index))
        return pages
    text = data.decode("utf-8-sig", errors="replace").strip()
    return [(text, None)] if text else []


def chunk_text(
    pages: Iterable[tuple[str, int | None]], size: int, overlap: int
) -> list[tuple[str, int | None, dict[str, object]]]:
    if size <= 0 or overlap < 0 or overlap >= size:
        raise ValueError("Chunk overlap must be smaller than chunk size")
    chunks: list[tuple[str, int | None, dict[str, object]]] = []
    for text, page_number in pages:
        start = 0
        while start < len(text):
            end = min(start + size, len(text))
            content = text[start:end].strip()
            if content:
                chunks.append(
                    (content, page_number, {"page_number": page_number, "start": start, "end": end})
                )
            if end >= len(text):
                break
            start = end - overlap
    return chunks


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class LocalEmbeddingProvider:
    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimension
            for token in re.findall(r"\w+", text.lower()):
                digest = hashlib.sha256(token.encode()).digest()
                position = int.from_bytes(digest[:4], "big") % self.dimension
                vector[position] += 1.0 if digest[4] % 2 else -1.0
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


class ExternalEmbeddingProvider:
    def __init__(self, settings: Settings) -> None:
        if not settings.embedding_api_url or not settings.embedding_api_key:
            raise ValueError("External embedding provider is not configured")
        self.settings = settings
        self.url = settings.embedding_api_url

    def embed(self, texts: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self.settings.embedding_model, "input": texts}).encode()
        request = urllib.request.Request(
            self.url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.settings.embedding_api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(  # noqa: S310
            request, timeout=self.settings.provider_timeout_seconds
        ) as response:
            body = json.loads(response.read())
        return [item["embedding"] for item in body["data"]]


def embedding_provider(settings: Settings | None = None) -> EmbeddingProvider:
    settings = settings or get_settings()
    if settings.embedding_provider.lower() == "external":
        return ExternalEmbeddingProvider(settings)
    return LocalEmbeddingProvider(settings.embedding_dimension)


def enqueue(document_id: UUID, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    try:
        Redis.from_url(settings.redis_url, decode_responses=True).rpush(
            settings.queue_name, str(document_id)
        )
        return True
    except RedisError:
        logger.warning("Document %s could not be queued", document_id)
        return False
