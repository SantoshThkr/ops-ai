from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agent import parse_intent
from app.incidents import approve, execute, propose
from app.limits import check_rate_limit
from app.main import app
from app.mcp import handle_request
from app.models import (
    ActionStatus,
    ApprovalDecision,
    AuditLog,
    Base,
    ServiceMetric,
    User,
    UserRole,
)
from app.schemas import IncidentProposalCreate
from app.tools import ALLOWED_METRICS, get_metric, tool_catalog


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _users(db: Session) -> tuple[User, User, User]:
    viewer = User(
        email="day9-viewer@example.com",
        password_hash="unused",
        name="Viewer",
        role=UserRole.VIEWER,
    )
    analyst = User(
        email="day9-analyst@example.com",
        password_hash="unused",
        name="Analyst",
        role=UserRole.ANALYST,
    )
    admin = User(
        email="day9-admin@example.com",
        password_hash="unused",
        name="Admin",
        role=UserRole.ADMIN,
    )
    db.add_all([viewer, analyst, admin])
    db.commit()
    return viewer, analyst, admin


def _proposal(db: Session, owner: User):
    incident = propose(
        db,
        owner,
        IncidentProposalCreate(
            title="Day 9 incident",
            summary="Test approval boundary",
            action_kind="restart_service",
            action_parameters={"service": "api"},
        ),
    )
    return incident.actions[0]


def test_allowlisted_metrics_and_unknown_metric_are_safe() -> None:
    with _session() as db:
        db.add(
            ServiceMetric(
                service="api",
                name="latency_p95",
                value=120,
                unit="ms",
                metric_metadata={},
            )
        )
        db.commit()

        assert "latency_p95" in ALLOWED_METRICS
        assert len(get_metric(db, name="latency_p95")) == 1
        assert get_metric(db, name="not_allowlisted") == []
        assert parse_intent("What is the latency?").metric_name == "latency_p95"


def test_incident_permissions_expiry_and_audit_records() -> None:
    with _session() as db:
        viewer, analyst, admin = _users(db)
        with pytest.raises(PermissionError):
            propose(
                db,
                viewer,
                IncidentProposalCreate(title="Denied", summary="Viewer proposal"),
            )

        action = _proposal(db, analyst)
        with pytest.raises(PermissionError):
            approve(db, action, analyst, ApprovalDecision.APPROVED, "analyst-approval")

        action.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
        with pytest.raises(ValueError, match="expired"):
            approve(db, action, admin, ApprovalDecision.APPROVED, "expired-approval")
        assert action.status == ActionStatus.EXPIRED

        audit_events = db.scalars(select(AuditLog.event)).all()
        assert "incident.proposed" in audit_events
        assert "permission.denied" in audit_events
        assert "action.expired" in audit_events


def test_execution_requires_admin_approval_and_is_idempotent() -> None:
    with _session() as db:
        _, analyst, admin = _users(db)
        action = _proposal(db, analyst)

        with pytest.raises(PermissionError):
            execute(db, action, analyst)

        approve(db, action, admin, ApprovalDecision.APPROVED, "day9-approval")
        executed = execute(db, action, admin)
        repeated = execute(db, action, admin)
        assert executed.status == ActionStatus.EXECUTED
        assert repeated.id == executed.id


def test_mcp_tool_allowlist_validation_and_rbac() -> None:
    with _session() as db:
        viewer, _, _ = _users(db)
        listed = handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, db, viewer)
        assert {item["name"] for item in listed["result"]["tools"]} == {
            item["name"] for item in tool_catalog()
        }

        invalid = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "search_knowledge", "arguments": {}},
            },
            db,
            viewer,
        )
        assert invalid["error"]["code"] == -32602

        unknown = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "not_a_tool", "arguments": {}},
            },
            db,
            viewer,
        )
        assert unknown["error"]["code"] == -32602

        denied = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "create_incident",
                    "arguments": {"title": "Denied", "summary": "Viewer"},
                },
            },
            db,
            viewer,
        )
        assert denied["error"]["code"] == -32003


class _FakeRedis:
    def __init__(self, values: list[int] | None = None) -> None:
        self.values = iter(values or [])
        self.expired: list[tuple[str, int]] = []

    def incr(self, _key: str) -> int:
        return next(self.values)

    def expire(self, key: str, seconds: int) -> None:
        self.expired.append((key, seconds))


def test_rate_limiter_handles_limit_and_dependency_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeRedis([1, 3])
    monkeypatch.setattr("app.limits.Redis.from_url", lambda *args, **kwargs: fake)
    assert check_rate_limit("chat", "user", 2) is True
    assert check_rate_limit("chat", "user", 2) is False
    assert fake.expired

    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise RedisError("redis unavailable")

    monkeypatch.setattr("app.limits.Redis.from_url", unavailable)
    assert check_rate_limit("chat", "user", 2) is True


def test_health_and_readiness_report_dependency_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.main.check_database_connection", lambda: None)
    monkeypatch.setattr("app.main.Redis.from_url", lambda *args, **kwargs: _ReadyRedis())
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/ready").json() == {"status": "ready"}

    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise RedisError("redis unavailable")

    monkeypatch.setattr("app.main.Redis.from_url", unavailable)
    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["detail"] == "Dependencies unavailable"


class _ReadyRedis:
    def ping(self) -> bool:
        return True
