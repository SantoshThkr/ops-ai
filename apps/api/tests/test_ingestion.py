from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import UploadFile
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

from app.config import Settings
from app.ingestion import LocalFileStorage, chunk_text, validate_upload
from app.models import Base, Document, DocumentStatus, User, UserRole
from app.processing import process_document
from app.worker import run_once


def upload(filename: str, content: bytes, content_type: str) -> UploadFile:
    return UploadFile(
        file=BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


def test_upload_validation_rejects_unsupported_empty_and_oversize() -> None:
    settings = Settings(max_file_size=4)
    with pytest.raises(ValueError):
        validate_upload(upload("data.exe", b"123", "application/octet-stream"), b"123", settings)
    with pytest.raises(ValueError):
        validate_upload(upload("data.txt", b"", "text/plain"), b"", settings)
    with pytest.raises(OverflowError):
        validate_upload(upload("data.txt", b"12345", "text/plain"), b"12345", settings)


def test_chunking_has_deterministic_boundaries_and_overlap() -> None:
    pages = [("abcdefghij", 2)]
    first = chunk_text(pages, size=6, overlap=2)
    second = chunk_text(pages, size=6, overlap=2)
    assert first == second
    assert [chunk[0] for chunk in first] == ["abcdef", "efghij"]
    assert first[0][1] == 2
    assert first[0][2] == {"page_number": 2, "start": 0, "end": 6}


def test_local_storage_prevents_traversal_and_supports_lifecycle(tmp_path: object) -> None:
    storage = LocalFileStorage(tmp_path)  # type: ignore[arg-type]
    storage.save("owner/document", b"content")
    assert storage.read("owner/document") == b"content"
    storage.delete("owner/document")
    with pytest.raises(FileNotFoundError):
        storage.read("owner/document")
    with pytest.raises(ValueError):
        storage.save("../outside", b"unsafe")


def _use_tmp_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalFileStorage:
    file_storage = LocalFileStorage(tmp_path)
    monkeypatch.setattr("app.main.storage", lambda _settings: file_storage)
    return file_storage


def test_upload_stores_the_file_and_queues_processing(
    client_for, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    file_storage = _use_tmp_storage(monkeypatch, tmp_path)
    queued: list[object] = []
    monkeypatch.setattr(
        "app.main.enqueue", lambda document_id, _settings: queued.append(document_id) or True
    )
    client = client_for("uploader@example.com")

    response = client.post(
        "/documents", files={"file": ("notes.md", b"# Notes\nhello", "text/markdown")}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "uploaded"
    assert [str(item) for item in queued] == [body["id"]]
    user_id = client.get("/me").json()["id"]
    assert file_storage.read(f"{user_id}/{body['id']}") == b"# Notes\nhello"


def test_upload_is_marked_failed_when_the_queue_is_unavailable(
    client_for, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_tmp_storage(monkeypatch, tmp_path)
    monkeypatch.setattr("app.main.enqueue", lambda _document_id, _settings: False)
    client = client_for("uploader@example.com")

    response = client.post("/documents", files={"file": ("notes.txt", b"hello", "text/plain")})

    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    assert "upload the document again" in response.json()["error_message"]


def test_upload_rejects_mismatched_and_spoofed_files(
    client_for, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_tmp_storage(monkeypatch, tmp_path)
    client = client_for("uploader@example.com")
    spoofed_pdf = client.post(
        "/documents", files={"file": ("report.pdf", b"not a pdf", "application/pdf")}
    )
    wrong_type = client.post("/documents", files={"file": ("notes.txt", b"hi", "application/pdf")})
    traversal = client.post(
        "/documents", files={"file": ("../../etc/passwd.txt", b"root", "text/plain")}
    )
    assert spoofed_pdf.status_code == 400
    assert wrong_type.status_code == 400
    # The client filename is metadata only; storage keys are generated server-side.
    assert traversal.status_code == 201
    assert traversal.json()["filename"] == "passwd.txt"


def test_unreadable_documents_fail_with_a_user_facing_reason(tmp_path: Path) -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    file_storage = LocalFileStorage(tmp_path)
    with Session(engine) as db:
        owner = User(email="o@example.com", password_hash="x", name="O", role=UserRole.VIEWER)
        db.add(owner)
        db.commit()
        document = Document(
            owner_id=owner.id,
            filename="blank.txt",
            content_type="text/plain",
            file_size=3,
            checksum="0" * 64,
            storage_key=f"{owner.id}/blank",
            status=DocumentStatus.UPLOADED,
        )
        db.add(document)
        db.commit()
        file_storage.save(document.storage_key, b"   ")

        assert process_document(db, document.id, file_storage=file_storage) is False
        db.refresh(document)
        assert document.status == DocumentStatus.FAILED
        assert document.error_message == "No readable text found in the document"


class _BrokenQueue:
    def blpop(self, *args: object, **kwargs: object) -> None:
        raise RedisConnectionError("redis restarting")


class _OneJobQueue:
    def __init__(self, item: tuple[str, str]) -> None:
        self.item = item

    def blpop(self, *args: object, **kwargs: object) -> tuple[str, str]:
        return self.item


def test_worker_survives_queue_outages_bad_jobs_and_crashing_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None)
    assert run_once(_BrokenQueue(), settings) is False
    assert run_once(_OneJobQueue(("queue", "not-a-uuid")), settings) is True

    def crash(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("database restarted")

    monkeypatch.setattr("app.worker.process_document", crash)
    monkeypatch.setattr("app.worker.SessionLocal", lambda: nullcontext(None))
    assert run_once(_OneJobQueue(("queue", str(uuid4()))), settings) is True
