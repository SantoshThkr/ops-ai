"""HTTP-boundary authorization, configuration safety, and log hygiene."""

import json
import logging
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, insecure_settings
from app.main import app
from app.models import UserRole
from app.observability import JsonFormatter, request_id_context, safe_request_id

ClientFactory = Callable[..., TestClient]

PROPOSAL = {
    "title": "API errors",
    "summary": "Error rate is elevated",
    "action_kind": "restart_service",
    "action_parameters": {"service": "api"},
}


def test_analysts_can_propose_on_every_proposal_route(client_for: ClientFactory) -> None:
    analyst = client_for("analyst@example.com", UserRole.ANALYST)
    for path in ("/incidents", "/incidents/proposals", "/incidents/propose"):
        assert analyst.post(path, json=PROPOSAL).status_code == 201, path


def test_viewers_cannot_propose(client_for: ClientFactory) -> None:
    viewer = client_for("viewer@example.com")
    assert viewer.post("/incidents/proposals", json=PROPOSAL).status_code == 403
    assert viewer.post("/incidents", json=PROPOSAL).status_code == 403


def test_approval_and_execution_denials_are_403_and_state_conflicts_are_409(
    client_for: ClientFactory,
) -> None:
    analyst = client_for("analyst@example.com", UserRole.ANALYST)
    admin = client_for("admin@example.com", UserRole.ADMIN)
    action_id = analyst.post("/incidents", json=PROPOSAL).json()["actions"][0]["id"]

    assert (
        analyst.post(f"/actions/{action_id}/approve", json={"decision": "approved"}).status_code
        == 403
    )
    assert analyst.post(f"/actions/{action_id}/execute").status_code == 403
    assert admin.post(f"/actions/{action_id}/execute").status_code == 409

    approved = admin.post(
        f"/actions/{action_id}/approve",
        json={"decision": "approved"},
        headers={"Idempotency-Key": "approve-1"},
    )
    assert approved.status_code == 200
    assert approved.json()["decision"] == "approved"
    executed = admin.post(f"/actions/{action_id}/execute")
    assert executed.status_code == 200
    assert executed.json()["status"] == "executed"
    assert admin.post(f"/actions/{action_id}/execute").json()["status"] == "executed"


def test_incident_and_action_reads_are_owner_scoped(client_for: ClientFactory) -> None:
    owner = client_for("owner@example.com", UserRole.ANALYST)
    other = client_for("other@example.com", UserRole.ANALYST)
    admin = client_for("admin@example.com", UserRole.ADMIN)
    incident = owner.post("/incidents", json=PROPOSAL).json()
    action_id = incident["actions"][0]["id"]

    assert other.get(f"/incidents/{incident['id']}").status_code == 404
    assert other.get(f"/actions/{action_id}").status_code == 404
    assert other.post(f"/actions/{action_id}/execute").status_code == 404
    assert other.get("/incidents").json() == []
    assert [item["id"] for item in admin.get("/incidents").json()] == [incident["id"]]
    assert admin.get("/incidents").json()[0]["actions"][0]["id"] == action_id


def test_analysts_only_see_their_own_audit_trail(client_for: ClientFactory) -> None:
    analyst = client_for("analyst@example.com", UserRole.ANALYST)
    admin = client_for("admin@example.com", UserRole.ADMIN)
    analyst_id = analyst.get("/me").json()["id"]
    admin_id = admin.get("/me").json()["id"]
    analyst.post("/incidents", json=PROPOSAL)
    admin.post("/incidents", json={**PROPOSAL, "title": "Admin incident"})

    analyst_actors = {row["actor_id"] for row in analyst.get("/audit-logs").json()}
    admin_actors = {row["actor_id"] for row in admin.get("/audit-logs").json()}
    assert analyst_actors == {analyst_id}
    assert {analyst_id, admin_id} <= admin_actors


def test_blank_chat_messages_are_rejected(client_for: ClientFactory) -> None:
    viewer = client_for("viewer@example.com")
    conversation_id = viewer.post("/conversations", json={}).json()["id"]
    response = viewer.post(f"/conversations/{conversation_id}/messages", json={"content": "   "})
    assert response.status_code == 422


def test_production_refuses_placeholder_secrets_and_insecure_cookies() -> None:
    development = Settings(_env_file=None)
    assert "JWT_SECRET is still a placeholder value" in insecure_settings(development)

    short = Settings(_env_file=None, jwt_secret="too-short", auth_cookie_secure=True)
    assert insecure_settings(short) == ["JWT_SECRET must be at least 32 characters"]

    hardened = Settings(
        _env_file=None,
        jwt_secret="x" * 48,
        auth_cookie_secure=True,
        cors_origins="https://ops.example.com",
    )
    assert insecure_settings(hardened) == []


def test_api_startup_fails_fast_in_production_with_unsafe_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.main.settings", Settings(_env_file=None, app_env="production"))
    with pytest.raises(RuntimeError, match="Refusing to start in production"):
        with TestClient(app):
            pass


def test_request_ids_are_echoed_only_when_safe() -> None:
    with TestClient(app) as client:
        echoed = client.get("/version", headers={"x-request-id": "trace-123"})
        replaced = client.get("/version", headers={"x-request-id": "bad id\nwith newline"})
    assert echoed.headers["x-request-id"] == "trace-123"
    assert replaced.headers["x-request-id"] != "bad id\nwith newline"
    assert safe_request_id("a" * 129) is None


def test_json_logs_carry_request_id_and_redact_sensitive_fields() -> None:
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "auth.login", None, None)
    record.user_id = "user-1"
    record.password = "hunter2"
    record.authorization = "Bearer abc"
    token = request_id_context.set("req-42")
    try:
        payload = json.loads(JsonFormatter().format(record))
    finally:
        request_id_context.reset(token)
    assert payload["event"] == "auth.login"
    assert payload["request_id"] == "req-42"
    assert payload["user_id"] == "user-1"
    assert payload["password"] == "[redacted]"
    assert payload["authorization"] == "[redacted]"
