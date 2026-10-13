"""Request-level protections: auth coverage, CSRF, body limits, error and token hygiene."""

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import jwt
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.auth import password_hash
from app.config import Settings, get_settings, insecure_settings
from app.main import app

ClientFactory = Callable[..., TestClient]

# Every other route must reject anonymous callers. Adding a route here is a decision.
PUBLIC_ROUTES = {
    ("GET", "/health"),
    ("GET", "/ready"),
    ("GET", "/version"),
    ("POST", "/auth/register"),
    ("POST", "/auth/login"),
    ("POST", "/auth/logout"),
}
FOREIGN_ORIGIN = {"Origin": "http://localhost:5173"}


@pytest.mark.usefixtures("session_factory")
def test_every_route_requires_authentication_unless_explicitly_public() -> None:
    client = TestClient(app)
    checked = 0
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        url = re.sub(r"\{[^}]+\}", str(uuid4()), route.path)
        for method in sorted(route.methods):
            if (method, route.path) in PUBLIC_ROUTES:
                continue
            response = client.request(method, url, json={} if method == "POST" else None)
            assert response.status_code == 401, f"{method} {route.path} answered anonymously"
            checked += 1
    assert checked >= 25


def test_cross_origin_writes_are_blocked_even_with_a_valid_session(
    client_for: ClientFactory,
) -> None:
    client = client_for("victim@example.com")
    upload = {"file": ("x.txt", b"forged upload", "text/plain")}

    # Simple (preflight-free) requests another same-site origin could forge.
    assert client.post("/documents", files=upload, headers=FOREIGN_ORIGIN).status_code == 403
    assert client.post(f"/actions/{uuid4()}/execute", headers=FOREIGN_ORIGIN).status_code == 403
    assert client.post("/auth/logout", headers={"Origin": "null"}).status_code == 403
    assert client.get("/me").status_code == 200  # the forged logout did not sign them out

    # The configured web origin, same-origin tools, and non-browser clients still work.
    allowed = {"Origin": get_settings().cors_origin_list[0]}
    assert client.post("/conversations", json={}, headers=allowed).status_code == 201
    assert (
        client.post("/conversations", json={}, headers={"Origin": "http://testserver"}).status_code
        == 201
    )
    assert client.post("/conversations", json={}).status_code == 201
    assert client.get("/conversations", headers=FOREIGN_ORIGIN).status_code == 200


def test_oversized_and_unbounded_bodies_are_refused_before_handlers_run(
    client_for: ClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = client_for("uploader@example.com")

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("handler reached for an oversized body")

    monkeypatch.setattr("app.main.validate_upload", fail)
    too_large = b"x" * (get_settings().max_file_size + 128 * 1024)
    oversized_upload = client.post(
        "/documents", files={"file": ("big.txt", too_large, "text/plain")}
    )
    assert oversized_upload.status_code == 413

    conversation_id = client.post("/conversations", json={}).json()["id"]
    oversized_json = client.post(
        f"/conversations/{conversation_id}/messages", json={"content": "x" * 300_000}
    )
    assert oversized_json.status_code == 413

    def chunks() -> Iterator[bytes]:
        yield b'{"title": "streamed without a length"}'

    streamed = client.post(
        "/conversations", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert streamed.status_code == 411


@pytest.mark.usefixtures("session_factory")
def test_validation_errors_do_not_echo_submitted_values() -> None:
    response = TestClient(app).post(
        "/auth/register",
        json={"email": "new@example.com", "password": "hunter2", "name": "N"},
    )
    assert response.status_code == 422
    assert "hunter2" not in response.text
    detail = response.json()["detail"][0]
    assert set(detail) == {"type", "loc", "msg"}
    assert detail["loc"] == ["body", "password"]


def test_tokens_must_carry_an_expiry(client_for: ClientFactory) -> None:
    user_id = client_for("token@example.com").get("/me").json()["id"]
    secret = get_settings().jwt_secret
    claims = {"sub": user_id, "type": "access"}
    eternal = jwt.encode(claims, secret, algorithm="HS256")
    expiring = jwt.encode(
        {**claims, "exp": datetime.now(UTC) + timedelta(minutes=5)}, secret, algorithm="HS256"
    )
    expired = jwt.encode(
        {**claims, "exp": datetime.now(UTC) - timedelta(minutes=5)}, secret, algorithm="HS256"
    )
    other_secret = jwt.encode(
        {**claims, "exp": datetime.now(UTC) + timedelta(minutes=5)}, "x" * 48, algorithm="HS256"
    )
    anonymous = TestClient(app)

    def status(token: str) -> int:
        return anonymous.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code

    assert (status(eternal), status(expired), status(other_secret)) == (401, 401, 401)
    assert status(expiring) == 200


@pytest.mark.usefixtures("session_factory")
def test_unknown_accounts_cost_the_same_password_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified: list[str] = []
    real_verify = password_hash.verify

    def counting_verify(password: str, hashed: str) -> bool:
        verified.append(password)
        return real_verify(password, hashed)

    monkeypatch.setattr("app.auth.password_hash.verify", counting_verify)
    response = TestClient(app).post(
        "/auth/login", json={"email": "nobody@example.com", "password": "guess"}
    )
    assert response.status_code == 401
    assert verified == ["guess"]


def test_production_refuses_credentials_published_in_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    burned_secret, burned_password = "s" * 48, "once-public-password"
    monkeypatch.setattr(
        "app.config._PUBLISHED_CREDENTIAL_HASHES",
        frozenset(sha256(value.encode()).hexdigest() for value in (burned_secret, burned_password)),
    )
    problems = insecure_settings(
        Settings(
            _env_file=None,
            jwt_secret=burned_secret,
            auth_cookie_secure=True,
            database_url=f"postgresql+psycopg://opsai:{burned_password}@db:5432/opsai",
        )
    )
    assert problems == [
        "JWT_SECRET was published in this repository's history; rotate it",
        "DATABASE_URL uses a placeholder or published password; rotate it",
    ]


def test_cors_preflight_allows_only_the_configured_origin_and_methods() -> None:
    client = TestClient(app)
    origin = get_settings().cors_origin_list[0]

    def preflight(from_origin: str, method: str) -> tuple[int, str | None]:
        response = client.options(
            "/conversations",
            headers={
                "Origin": from_origin,
                "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": "content-type,idempotency-key",
            },
        )
        return response.status_code, response.headers.get("access-control-allow-origin")

    assert preflight(origin, "POST") == (200, origin)
    assert preflight(origin, "DELETE")[0] == 400
    assert preflight("https://evil.example", "POST") == (400, None)


def test_production_app_hides_docs_and_sends_hsts() -> None:
    """The app object is built at import time, so check production in a fresh interpreter."""
    script = """
import json
from fastapi.testclient import TestClient
from app.main import app
client = TestClient(app)  # no lifespan: only the app's static configuration is checked
response = client.get("/version")
print(json.dumps({
    "docs": [client.get(path).status_code for path in ("/docs", "/redoc", "/openapi.json")],
    "hsts": response.headers.get("strict-transport-security"),
    "version": response.status_code,
}))
"""
    environment = {
        **os.environ,
        "APP_ENV": "production",
        "JWT_SECRET": "p" * 48,
        "AUTH_COOKIE_SECURE": "true",
        "DATABASE_URL": "postgresql+psycopg://opsai:a-unique-production-password@db:5432/opsai",
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=environment,
        cwd=Path(__file__).resolve().parents[1],
        timeout=60,
        check=True,
    )
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "docs": [404, 404, 404],
        "hsts": "max-age=31536000",
        "version": 200,
    }
