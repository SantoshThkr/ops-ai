import time
from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import UploadFile
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

from app.config import Settings
from app.ingestion import LocalFileStorage, chunk_text, extract_text, validate_upload
from app.models import Base, Document, DocumentChunk, DocumentStatus, User, UserRole
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


def _pdf(pages: list[str]) -> bytes:
    """Build a small text PDF, one string per page."""
    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _stored_document(db: Session, file_storage: LocalFileStorage, name: str, data: bytes) -> UUID:
    owner = User(email="pdf@example.com", password_hash="x", name="P", role=UserRole.VIEWER)
    db.add(owner)
    db.commit()
    document = Document(
        owner_id=owner.id,
        filename=name,
        content_type="application/pdf",
        file_size=len(data),
        checksum="0" * 64,
        storage_key=f"{owner.id}/{name}",
        status=DocumentStatus.UPLOADED,
    )
    db.add(document)
    db.commit()
    file_storage.save(document.storage_key, data)
    return document.id


def _memory_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def test_real_pdf_is_extracted_per_page_and_chunked(tmp_path: Path) -> None:
    data = _pdf(["The VPN uses the corporate identity provider.", "Refunds take 14 days."])
    assert extract_text(data, ".pdf") == [
        ("The VPN uses the corporate identity provider.", 1),
        ("Refunds take 14 days.", 2),
    ]
    file_storage = LocalFileStorage(tmp_path)
    with _memory_session() as db:
        document_id = _stored_document(db, file_storage, "guide.pdf", data)
        assert process_document(db, document_id, file_storage=file_storage) is True
        pages = db.scalars(
            select(DocumentChunk.page_number).order_by(DocumentChunk.chunk_index)
        ).all()
        assert pages == [1, 2]


def test_a_parser_that_never_returns_fails_the_document_instead_of_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hang(*args: object, **kwargs: object) -> None:
        while True:  # what a crafted PDF does to a vulnerable parser
            pass

    monkeypatch.setattr("app.processing.extract_text", hang)
    file_storage = LocalFileStorage(tmp_path)
    settings = Settings(_env_file=None, document_processing_time_limit_seconds=0.2)
    with _memory_session() as db:
        document_id = _stored_document(db, file_storage, "hang.pdf", _pdf(["x"]))
        started = time.monotonic()
        assert (
            process_document(db, document_id, file_storage=file_storage, settings=settings) is False
        )
        assert time.monotonic() - started < 5
        document = db.get(Document, document_id)
        assert document is not None
        assert document.status == DocumentStatus.FAILED
        assert document.error_message == "Document took too long to process"
