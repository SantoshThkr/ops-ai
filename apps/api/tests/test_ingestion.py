from io import BytesIO

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from app.config import Settings
from app.ingestion import LocalFileStorage, chunk_text, validate_upload


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
