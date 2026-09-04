import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agent import IntentKind, parse_intent, stream
from app.incidents import approve, execute, propose
from app.mcp import handle_request
from app.models import ActionStatus, ApprovalDecision, Base, ServiceMetric, User, UserRole
from app.schemas import IncidentProposalCreate


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def test_parse_intent_prioritizes_incident_requests_over_metrics() -> None:
    incident = parse_intent("Create an incident for the api service because latency is high.")
    assert incident.kind == IntentKind.INCIDENT

    for question in ("What is the latency?", "What is the latency metric?"):
        metric = parse_intent(question)
        assert metric.kind == IntentKind.METRIC
        assert metric.metric_name == "latency_p95"

    proposal = parse_intent("Propose an incident because of high latency.")
    assert proposal.kind == IntentKind.INCIDENT


def test_local_agent_metrics_are_deterministic_and_read_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    with _session() as db:
        user = User(
            email="agent@example.com",
            password_hash="unused",
            name="Agent",
            role=UserRole.VIEWER,
        )
        db.add_all(
            [
                user,
                ServiceMetric(
                    service="api",
                    name="error_rate",
                    value=0.02,
                    unit="ratio",
                    metric_metadata={"source": "test"},
                ),
            ]
        )
        db.commit()
        events = list(stream("show service health metrics", db, user))

    names = [event[0] for event in events]
    assert names == ["tool_call", "tool_result", "token", "done"]
    assert events[1][1]["read_only"] is True
    assert "api error_rate" in events[2][1]["text"]
    messages = [record.message for record in caplog.records]
    assert "agent.intent_classified" in messages
    assert "agent.tool_execution.completed" in messages


def test_casual_chat_does_not_invoke_a_tool() -> None:
    with _session() as db:
        user = User(
            email="casual@example.com",
            password_hash="unused",
            name="Casual",
            role=UserRole.VIEWER,
        )
        db.add(user)
        db.commit()
        events = list(stream("Hi, how are you?", db, user))

    assert events == []


def test_mcp_exposes_only_local_tool_protocol() -> None:
    with _session() as db:
        user = User(
            email="mcp@example.com",
            password_hash="unused",
            name="MCP",
            role=UserRole.VIEWER,
        )
        db.add(user)
        db.commit()
        response = handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, db, user)

    assert response["id"] == 1
    assert {tool["name"] for tool in response["result"]["tools"]} == {
        "search_knowledge",
        "get_metric",
        "get_incident",
        "create_incident",
    }


def test_incident_proposal_requires_approval_and_is_one_time() -> None:
    with _session() as db:
        analyst = User(
            email="analyst@example.com",
            password_hash="unused",
            name="Analyst",
            role=UserRole.ANALYST,
        )
        admin = User(
            email="admin@example.com",
            password_hash="unused",
            name="Admin",
            role=UserRole.ADMIN,
        )
        db.add_all([analyst, admin])
        db.commit()
        incident = propose(
            db,
            analyst,
            IncidentProposalCreate(
                title="API outage",
                summary="Restart the API after approval",
                action_kind="restart_service",
                action_parameters={"service": "api"},
            ),
        )
        action = incident.actions[0]
        assert action.status == ActionStatus.PENDING
        approval = approve(db, action, admin, ApprovalDecision.APPROVED, "approve-1")
        assert approval.decision == ApprovalDecision.APPROVED
        executed = execute(db, action, admin)
        assert executed.status == ActionStatus.EXECUTED
