import json
from collections.abc import Generator
from contextlib import contextmanager
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db
from app.main import app
from app.models import AuditLog, Base, User
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


def test_chat_deduplicates_citations_by_document_and_page(monkeypatch) -> None:
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
            "app.tools.search_owned_chunks",
            lambda *args, **kwargs: [
                SearchResult(
                    document_id=document_one,
                    chunk_id=uuid4(),
                    filename="31Aug2026.pdf",
                    content="First page summary",
                    page_number=1,
                    similarity=0.96,
                ),
                SearchResult(
                    document_id=document_two,
                    chunk_id=uuid4(),
                    filename="27Aug2026.docx.pdf",
                    content="Second doc first page",
                    page_number=1,
                    similarity=0.94,
                ),
                SearchResult(
                    document_id=document_two,
                    chunk_id=uuid4(),
                    filename="27Aug2026.docx.pdf",
                    content="Second doc second page",
                    page_number=2,
                    similarity=0.92,
                ),
                SearchResult(
                    document_id=document_one,
                    chunk_id=uuid4(),
                    filename="31Aug2026.pdf",
                    content="First doc second page",
                    page_number=2,
                    similarity=0.90,
                ),
                SearchResult(
                    document_id=document_one,
                    chunk_id=uuid4(),
                    filename="31Aug2026.pdf",
                    content="Duplicate first page",
                    page_number=1,
                    similarity=0.89,
                ),
            ],
        )

        response = client.post(
            f"/conversations/{conversation_id}/messages",
            json={"content": "What are the key policy items?"},
        )

        assert response.status_code == 200
        events = _parse_sse_events(response.text)
        citation_event = next(event for event in events if event["event"] == "citation")
        done_event = next(event for event in events if event["event"] == "done")
        citations = citation_event["data"]["citations"]
        assert citations == done_event["data"]["citations"]
        assert len(citations) == 4
        assert [(item["document_id"], item["page_number"]) for item in citations] == [
            (str(document_one), 1),
            (str(document_two), 1),
            (str(document_two), 2),
            (str(document_one), 2),
        ]
        assert (
            "31Aug2026.pdf supports this answer. 31Aug2026.pdf supports this answer."
            not in response.text
        )


def test_chat_uses_no_context_when_similarity_is_below_threshold(monkeypatch) -> None:
    with build_client() as client:
        client.post(
            "/auth/register",
            json={"email": "lowmatch@example.com", "password": "correct horse", "name": "LowMatch"},
        )
        conversation = client.post("/conversations", json={"title": "Low match"})
        conversation_id = conversation.json()["id"]

        monkeypatch.setattr(
            "app.tools.search_owned_chunks",
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
            json={"content": "What does the policy say?"},
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
            "app.tools.search_owned_chunks",
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


VPN_GUIDE = (
    "Remote access guide. The VPN is configured with the corporate identity provider. "
    "Employees must enable MFA before connecting. Support tickets go to the help desk."
)


def _owner(session_factory: sessionmaker[Session], email: str) -> User:
    with session_factory() as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        db.expunge(user)
        return user


def test_general_questions_search_documents_and_quote_the_source(
    client_for, session_factory, add_document
) -> None:
    client = client_for("reader@example.com")
    with session_factory() as db:
        add_document(
            db, _owner(session_factory, "reader@example.com"), VPN_GUIDE, filename="vpn.md"
        )
    conversation_id = client.post("/conversations", json={}).json()["id"]

    # No "document"/"policy" marker words: this used to get a canned greeting.
    # The local hash embedding needs matching word forms ("configured", not "configure").
    response = client.post(
        f"/conversations/{conversation_id}/messages",
        json={"content": "How is the VPN configured?"},
    )

    events = _parse_sse_events(response.text)
    names = [event["event"] for event in events]
    assert names[:2] == ["tool_call", "tool_result"]
    assert names[-2:] == ["citation", "done"]
    answer = "".join(e["data"]["text"] for e in events if e["event"] == "token")
    assert "The VPN is configured with the corporate identity provider." in answer
    assert "vpn.md" in answer
    stored = client.get(f"/conversations/{conversation_id}").json()["messages"]
    # SQLite timestamps have one-second resolution, so compare by role, not position.
    replies = [message for message in stored if message["role"] == "assistant"]
    assert len(stored) == 2 and len(replies) == 1
    assert "corporate identity provider" in replies[0]["content"]


def test_unrelated_question_gets_capabilities_and_no_citations(
    client_for, session_factory, add_document
) -> None:
    client = client_for("reader@example.com")
    with session_factory() as db:
        add_document(db, _owner(session_factory, "reader@example.com"), VPN_GUIDE)
    conversation_id = client.post("/conversations", json={}).json()["id"]

    response = client.post(
        f"/conversations/{conversation_id}/messages",
        json={"content": "What is the weather today?"},
    )

    events = _parse_sse_events(response.text)
    answer = "".join(e["data"]["text"] for e in events if e["event"] == "token")
    assert "event: citation" not in response.text
    assert "report recorded service metrics" in answer


def test_metric_questions_do_not_run_document_retrieval(client_for, monkeypatch) -> None:
    client = client_for("metrics@example.com")
    conversation_id = client.post("/conversations", json={}).json()["id"]

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("retrieval should not run for metric questions")

    monkeypatch.setattr("app.tools.search_owned_chunks", fail)
    response = client.post(
        f"/conversations/{conversation_id}/messages", json={"content": "What is the latency?"}
    )

    events = _parse_sse_events(response.text)
    assert events[0] == {
        "event": "tool_call",
        "data": {"name": "get_metric", "arguments": {"name": "latency_p95"}},
    }
    assert events[-1]["data"]["status"] == "completed"


def test_stream_failures_are_reported_in_band_and_audited(
    client_for, session_factory, monkeypatch
) -> None:
    client = client_for("failure@example.com")
    conversation_id = client.post("/conversations", json={}).json()["id"]

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("vector store unavailable")

    monkeypatch.setattr("app.chat.search_knowledge", broken)
    response = client.post(
        f"/conversations/{conversation_id}/messages", json={"content": "Summarize the policy"}
    )

    assert response.status_code == 200
    events = _parse_sse_events(response.text)
    assert [event["event"] for event in events][-2:] == ["error", "done"]
    assert events[-1]["data"] == {"status": "failed"}
    assert "vector store" not in response.text
    with session_factory() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.event == "chat.failed")) is not None
