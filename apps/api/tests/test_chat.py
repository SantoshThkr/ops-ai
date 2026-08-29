import json
from collections.abc import Generator
from contextlib import contextmanager
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db
from app.main import app
from app.models import Base
from app.schemas import SearchResult


@contextmanager
def build_client() -> Generator[TestClient, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_conversations_are_scoped_to_the_authenticated_user() -> None:
    with build_client() as first_client, build_client() as second_client:
        assert (
            first_client.post(
                "/auth/register",
                json={"email": "first@example.com", "password": "correct horse", "name": "First"},
            ).status_code
            == 201
        )
        assert (
            second_client.post(
                "/auth/register",
                json={"email": "second@example.com", "password": "correct horse", "name": "Second"},
            ).status_code
            == 201
        )

        created = first_client.post("/conversations", json={"title": "First convo"})
        assert created.status_code == 201
        first_id = created.json()["id"]

        assert first_client.get("/conversations").json()["total"] == 1
        assert second_client.get("/conversations").json()["total"] == 0
        assert second_client.get(f"/conversations/{first_id}").status_code == 404


def _parse_sse_events(response_text: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for block in response_text.strip().split("\n\n"):
        if not block:
            continue
        event_name = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
        if event_name:
            payload: dict[str, object] = {"event": event_name}
            if data:
                payload["data"] = json.loads(data)
            events.append(payload)
    return events


def test_chat_stream_returns_no_context_message_when_nothing_is_retrieved() -> None:
    with build_client() as client:
        client.post(
            "/auth/register",
            json={"email": "owner@example.com", "password": "correct horse", "name": "Owner"},
        )
        conversation = client.post("/conversations", json={"title": "Question"})
        conversation_id = conversation.json()["id"]

        response = client.post(
            f"/conversations/{conversation_id}/messages",
            json={"content": "Summarize the policy"},
        )

        assert response.status_code == 200
        body = response.text
        assert "couldn" in body
        assert "information" in body
        assert "event: done" in body


def test_chat_deduplicates_citations_by_document_id(monkeypatch) -> None:
    with build_client() as client:
        client.post(
            "/auth/register",
            json={"email": "dedup@example.com", "password": "correct horse", "name": "Dedup"},
        )
        conversation = client.post("/conversations", json={"title": "Policy"})
        conversation_id = conversation.json()["id"]

        document_one = uuid4()
        document_two = uuid4()

        monkeypatch.setattr(
            "app.main._search_owned_chunks",
            lambda *args, **kwargs: [
                SearchResult(
                    document_id=document_one,
                    chunk_id=uuid4(),
                    filename="resume.pdf",
                    content="Resume summary",
                    page_number=1,
                    similarity=0.92,
                ),
                SearchResult(
                    document_id=document_one,
                    chunk_id=uuid4(),
                    filename="resume.pdf",
                    content="Resume summary repeated",
                    page_number=2,
                    similarity=0.88,
                ),
                SearchResult(
                    document_id=document_two,
                    chunk_id=uuid4(),
                    filename="policy.pdf",
                    content="Policy content",
                    page_number=3,
                    similarity=0.81,
                ),
            ],
        )

        response = client.post(
            f"/conversations/{conversation_id}/messages",
            json={"content": "What are the key policy items?"},
        )

        assert response.status_code == 200
        citation_events = [
            event for event in _parse_sse_events(response.text) if event["event"] == "citation"
        ]
        assert citation_events
        citations = citation_events[0]["data"]["citations"]
        assert len(citations) == 2
        assert {item["document_id"] for item in citations} == {
            str(document_one),
            str(document_two),
        }
        assert (
            "resume.pdf supports this answer. resume.pdf supports this answer." not in response.text
        )


def test_chat_uses_no_context_when_similarity_is_below_threshold(monkeypatch) -> None:
    with build_client() as client:
        client.post(
            "/auth/register",
            json={"email": "lowmatch@example.com", "password": "correct horse", "name": "LowMatch"},
        )
        conversation = client.post("/conversations", json={"title": "Casual"})
        conversation_id = conversation.json()["id"]

        monkeypatch.setattr(
            "app.main._search_owned_chunks",
            lambda *args, **kwargs: [
                SearchResult(
                    document_id=uuid4(),
                    chunk_id=uuid4(),
                    filename="notes.txt",
                    content="some general text",
                    page_number=1,
                    similarity=0.2,
                )
            ],
        )

        response = client.post(
            f"/conversations/{conversation_id}/messages",
            json={"content": "how are u"},
        )

        assert response.status_code == 200
        body = response.text
        assert "couldn" in body
        assert "information" in body
        assert "event: citation" not in body
        assert '"citations": []' not in body


def test_chat_accepts_sufficient_similarity_as_grounded(monkeypatch) -> None:
    with build_client() as client:
        client.post(
            "/auth/register",
            json={"email": "grounded@example.com", "password": "correct horse", "name": "Grounded"},
        )
        conversation = client.post("/conversations", json={"title": "Grounded"})
        conversation_id = conversation.json()["id"]

        monkeypatch.setattr(
            "app.main._search_owned_chunks",
            lambda *args, **kwargs: [
                SearchResult(
                    document_id=uuid4(),
                    chunk_id=uuid4(),
                    filename="policies.pdf",
                    content="Remote worker policy includes time tracking and required approvals.",
                    page_number=4,
                    similarity=0.75,
                )
            ],
        )

        response = client.post(
            f"/conversations/{conversation_id}/messages",
            json={"content": "What is the remote work policy?"},
        )

        assert response.status_code == 200
        body = response.text
        assert "I couldn't find enough information" not in body
        assert "event: citation" in body
        assert '"citations": []' not in body
