from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import get_db
from app.main import app
from app.models import Base


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
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
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_register_login_me_and_logout(client: TestClient) -> None:
    registered = client.post(
        "/auth/register",
        json={"email": "User@example.com", "password": "correct horse", "name": "User"},
    )
    assert registered.status_code == 201
    assert registered.json()["user"]["role"] == "viewer"
    assert "access_token" not in registered.json()
    assert registered.cookies.get(get_settings().auth_cookie_name)
    assert "HttpOnly" in registered.headers["set-cookie"]
    assert client.get("/me").json()["email"] == "user@example.com"

    logged_in = client.post(
        "/auth/login",
        json={"email": "user@example.com", "password": "correct horse"},
    )
    assert logged_in.status_code == 200
    assert "access_token" not in logged_in.json()
    assert logged_in.cookies.get(get_settings().auth_cookie_name)
    assert "HttpOnly" in logged_in.headers["set-cookie"]
    assert client.get("/me").status_code == 200
    assert client.post("/auth/logout").status_code == 204
    assert client.get("/me").status_code == 401


def test_rbac_rejects_viewer_from_admin_probe(client: TestClient) -> None:
    client.post(
        "/auth/register",
        json={"email": "viewer@example.com", "password": "correct horse", "name": "Viewer"},
    )
    assert client.get("/rbac/viewer").status_code == 200
    assert client.get("/rbac/analyst").status_code == 403
    assert client.get("/rbac/admin").status_code == 403


def test_auth_validation_errors_are_consistent(client: TestClient) -> None:
    response = client.post(
        "/auth/register",
        json={"email": "not-an-email", "password": "short", "name": " "},
    )
    assert response.status_code == 422
    assert "detail" in response.json()
