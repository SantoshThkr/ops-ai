"""Retrieval behavior with the real local embedding provider (SQLite scoring path)."""

from collections.abc import Callable, Iterator
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import Base, DocumentStatus, User, UserRole
from app.retrieval import content_terms, search_owned_chunks

AddDocument = Callable[..., UUID]

RUNBOOK = (
    "Incident response runbook. When API latency exceeds the p95 budget of 300 ms, the "
    "on-call engineer should check the database connection pool and the queue depth before "
    "restarting any service. Restarts of the api service require an approved incident "
    "proposal. Rollbacks are performed by the release manager and must reference the "
    "deployment identifier. You are expected to document how the issue was detected, what "
    "you did, and whether it is resolved. Refunds are processed within 14 days. The VPN is "
    "configured with the corporate identity provider; employees are required to enable MFA."
)


@pytest.fixture()
def db() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _user(db: Session, email: str) -> User:
    user = User(email=email, password_hash="x", name=email, role=UserRole.VIEWER)
    db.add(user)
    db.commit()
    return user


def test_relevant_question_is_grounded_in_the_owners_document(
    db: Session, add_document: AddDocument
) -> None:
    owner = _user(db, "owner@example.com")
    document_id = add_document(db, owner, RUNBOOK)
    results = search_owned_chunks(db, owner, "How is the VPN configured?", 5)
    assert [result.document_id for result in results] == [document_id]


def test_stopword_only_overlap_does_not_ground_an_off_topic_question(
    db: Session, add_document: AddDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _user(db, "owner@example.com")
    add_document(db, owner, RUNBOOK)
    question = "What is the weather today?"
    assert search_owned_chunks(db, owner, question, 5) == []

    # Without the lexical guard, "what is the ..." alone clears the cosine threshold.
    monkeypatch.setattr("app.retrieval._uses_lexical_guard", lambda _settings: False)
    assert search_owned_chunks(db, owner, question, 5) != []


def test_other_users_and_unfinished_documents_are_never_searched(
    db: Session, add_document: AddDocument
) -> None:
    owner = _user(db, "owner@example.com")
    stranger = _user(db, "stranger@example.com")
    add_document(db, owner, RUNBOOK)
    add_document(db, stranger, RUNBOOK, status=DocumentStatus.PROCESSING)
    assert search_owned_chunks(db, stranger, "How is the VPN configured?", 5) == []


def test_content_terms_ignore_function_words_and_match_word_forms() -> None:
    assert content_terms("How is the VPN configured?") == {"vpn", "confi"}
    assert content_terms("configure") <= content_terms("configured")
