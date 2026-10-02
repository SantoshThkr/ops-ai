"""Document job ownership and recovery of jobs lost to worker crashes."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.ingestion import LocalFileStorage
from app.models import Base, Document, DocumentChunk, DocumentStatus, User, UserRole
from app.processing import (
    ABANDONED_MESSAGE,
    claim_document,
    process_document,
    recover_stale_documents,
)
from app.worker import recover_once


@pytest.fixture()
def db() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(User(email="o@example.com", password_hash="x", name="O", role=UserRole.VIEWER))
        session.commit()
        yield session


def _document(db: Session, status: DocumentStatus, *, age: timedelta | None = None) -> UUID:
    owner = db.scalars(select(User)).one()
    document = Document(
        owner_id=owner.id,
        filename="notes.txt",
        content_type="text/plain",
        file_size=5,
        checksum="0" * 64,
        storage_key=f"{owner.id}/{uuid4()}",
        status=status,
    )
    db.add(document)
    db.commit()
    if age is not None:
        db.execute(
            update(Document)
            .where(Document.id == document.id)
            .values(updated_at=datetime.now(UTC) - age)
        )
        db.commit()
    return document.id


def _status(db: Session, document_id: UUID) -> DocumentStatus:
    db.expire_all()
    document = db.get(Document, document_id)
    assert document is not None
    return document.status


def test_only_one_job_can_claim_an_uploaded_document(db: Session) -> None:
    document_id = _document(db, DocumentStatus.UPLOADED)
    assert claim_document(db, document_id) is True
    assert claim_document(db, document_id) is False
    assert _status(db, document_id) == DocumentStatus.PROCESSING
    for status in (DocumentStatus.FAILED, DocumentStatus.COMPLETED):
        assert claim_document(db, _document(db, status)) is False


def test_duplicate_deliveries_do_not_reprocess_or_duplicate_chunks(
    db: Session, tmp_path: Path
) -> None:
    file_storage = LocalFileStorage(tmp_path)
    document_id = _document(db, DocumentStatus.UPLOADED)
    document = db.get(Document, document_id)
    assert document is not None
    file_storage.save(document.storage_key, b"hello world")

    assert process_document(db, document_id, file_storage=file_storage) is True
    chunk_count = db.scalar(select(func.count()).select_from(DocumentChunk))
    assert process_document(db, document_id, file_storage=file_storage) is True
    assert db.scalar(select(func.count()).select_from(DocumentChunk)) == chunk_count

    in_flight = _document(db, DocumentStatus.PROCESSING)
    assert process_document(db, in_flight, file_storage=file_storage) is False
    assert _status(db, in_flight) == DocumentStatus.PROCESSING


def test_recovery_fails_abandoned_jobs_and_requeues_unclaimed_uploads(db: Session) -> None:
    abandoned = _document(db, DocumentStatus.PROCESSING, age=timedelta(hours=1))
    in_progress = _document(db, DocumentStatus.PROCESSING, age=timedelta(minutes=1))
    lost_upload = _document(db, DocumentStatus.UPLOADED, age=timedelta(hours=1))
    fresh_upload = _document(db, DocumentStatus.UPLOADED)
    finished = _document(db, DocumentStatus.COMPLETED, age=timedelta(hours=1))
    requeued: list[UUID] = []

    def requeue(document_id: UUID) -> bool:
        requeued.append(document_id)
        return True

    assert recover_stale_documents(db, requeue) == (1, 1)
    assert requeued == [lost_upload]
    assert _status(db, abandoned) == DocumentStatus.FAILED
    abandoned_document = db.get(Document, abandoned)
    assert abandoned_document is not None
    assert abandoned_document.error_message == ABANDONED_MESSAGE
    assert _status(db, in_progress) == DocumentStatus.PROCESSING
    assert _status(db, fresh_upload) == DocumentStatus.UPLOADED
    assert _status(db, finished) == DocumentStatus.COMPLETED

    # A re-queued upload is not queued again until another timeout window passes.
    assert recover_stale_documents(db, requeue) == (0, 0)
    assert requeued == [lost_upload]


def test_uploads_that_could_not_be_requeued_are_retried_next_sweep(db: Session) -> None:
    lost_upload = _document(db, DocumentStatus.UPLOADED, age=timedelta(hours=1))
    assert recover_stale_documents(db, lambda _document_id: False) == (0, 0)
    attempts: list[UUID] = []
    assert recover_stale_documents(db, lambda doc_id: attempts.append(doc_id) or True) == (0, 1)
    assert attempts == [lost_upload]


def test_worker_recovery_survives_database_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_session() -> None:
        raise RuntimeError("database restarting")

    monkeypatch.setattr("app.worker.SessionLocal", broken_session)
    recover_once(Settings(_env_file=None))
