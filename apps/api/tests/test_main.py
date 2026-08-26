import pytest
from fastapi.testclient import TestClient

from app.main import app


def test_version_endpoint() -> None:
    response = TestClient(app).get("/version")
    assert response.status_code == 200
    assert response.json()["version"] == "0.1.0"


def test_health_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.main.check_database_connection", lambda: None)
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
