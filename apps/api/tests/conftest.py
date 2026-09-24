from collections.abc import Callable, Generator, Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import get_db
from app.ingestion import LocalEmbeddingProvider, chunk_text
from app.limits import _redis_client
from app.main import app
from app.models import Base, Document, DocumentChunk, DocumentStatus, User, UserRole


@pytest.fixture(autouse=True)
def _reset_cached_redis_client() -> Iterator[None]:
    _redis_client.cache_clear()
    yield
    _redis_client.cache_clear()


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    yield factory
    app.dependency_overrides.clear()
    engine.dispose()


ClientFactory = Callable[..., TestClient]


@pytest.fixture()
def client_for(session_factory: sessionmaker[Session]) -> Iterator[ClientFactory]:
    """Create authenticated TestClients (one cookie jar per user) with a given role."""
    clients: list[TestClient] = []

    def make(email: str, role: UserRole = UserRole.VIEWER) -> TestClient:
        client = TestClient(app)
        clients.append(client)
        registered = client.post(
            "/auth/register",
            json={"email": email, "password": "correct horse", "name": email.split("@")[0]},
        )
        assert registered.status_code == 201, registered.text
        if role != UserRole.VIEWER:
            with session_factory() as db:
                db.execute(update(User).where(User.email == email).values(role=role))
                db.commit()
        return client

    yield make
    for client in clients:
        client.close()


def _add_document(
    db: Session,
    owner: User,
    text: str,
    *,
    filename: str = "runbook.md",
    status: DocumentStatus = DocumentStatus.COMPLETED,
) -> UUID:
    """Store a document with real local-embedding chunks, as the worker would."""
    settings = get_settings()
    document = Document(
        id=uuid4(),
        owner_id=owner.id,
        filename=filename,
        content_type="text/markdown",
        file_size=len(text),
        checksum="0" * 64,
        storage_key=f"{owner.id}/{uuid4()}",
        status=status,
    )
    db.add(document)
    chunks = chunk_text([(text, None)], settings.chunk_size, settings.chunk_overlap)
    vectors = LocalEmbeddingProvider(settings.embedding_dimension).embed([c[0] for c in chunks])
    db.add_all(
        DocumentChunk(
            document_id=document.id,
            chunk_index=index,
            content=content,
            embedding=vectors[index],
            page_number=page,
            chunk_metadata=metadata,
        )
        for index, (content, page, metadata) in enumerate(chunks)
    )
    db.commit()
    return document.id


@pytest.fixture()
def add_document() -> Callable[..., UUID]:
    return _add_document
