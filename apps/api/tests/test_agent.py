import logging

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agent import IntentKind, parse_intent, stream
from app.incidents import approve, execute, propose
from app.mcp import handle_request
from app.models import (
    Action,
    ActionStatus,
    ApprovalDecision,
    Base,
    Incident,
    ServiceMetric,
    User,
    UserRole,
)
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


ROUTING_CASES = [
    ("what is the latency?", IntentKind.METRIC),
    ("what is latency p95?", IntentKind.METRIC),
    ("what metrics are available?", IntentKind.METRIC),
    ("create an incident because latency is high", IntentKind.INCIDENT),
    ("create an incident and restart the API", IntentKind.INCIDENT),
    ("can you restart the API?", IntentKind.ACTION_REQUEST),
    ("please restart the database", IntentKind.ACTION_REQUEST),
    ("roll back the deployment", IntentKind.ACTION_REQUEST),
    ("show me incident 123", IntentKind.INCIDENT_LOOKUP),
    ("search my resume for React", IntentKind.KNOWLEDGE),
    ("what does the runbook say about latency?", IntentKind.KNOWLEDGE),
    ("what does the rollback deployment runbook say?", IntentKind.KNOWLEDGE),
    ("why did we roll back yesterday?", IntentKind.NONE),
    ("don't create an incident", IntentKind.NONE),
    ("tell me something unrelated", IntentKind.NONE),
    ("How do I configure the VPN?", IntentKind.NONE),
]


@pytest.mark.parametrize(("question", "expected"), ROUTING_CASES)
def test_routing_table(question: str, expected: IntentKind) -> None:
    assert parse_intent(question).kind == expected


def test_incident_intent_only_carries_explicit_allowlisted_targets() -> None:
    latency = parse_intent("create an incident because latency is high")
    assert (latency.action_kind, latency.service) == (None, None)

    restart = parse_intent("Create an incident and restart the API service")
    assert (restart.action_kind, restart.service) == ("restart_service", "api")

    unknown = parse_intent("create an incident and restart the database")
    assert (unknown.action_kind, unknown.service) == ("restart_service", None)


def _analyst(db: Session) -> User:
    user = User(
        email="routing-analyst@example.com",
        password_hash="unused",
        name="Analyst",
        role=UserRole.ANALYST,
    )
    db.add(user)
    db.commit()
    return user


@pytest.mark.parametrize(
    "question",
    [
        "can you restart the API?",
        "please restart the database",
        "roll back the deployment",
        "why did we roll back yesterday?",
        "don't create an incident",
        "what does the rollback deployment runbook say?",
    ],
)
def test_ambiguous_or_operational_requests_never_create_incidents(question: str) -> None:
    with _session() as db:
        analyst = _analyst(db)
        events = list(stream(question, db, analyst))
        assert db.scalar(select(func.count()).select_from(Incident)) == 0
        assert db.scalar(select(func.count()).select_from(Action)) == 0
    assert all(
        name != "tool_call" or payload["name"] != "create_incident" for name, payload in events
    )


def test_incident_without_requested_action_attaches_no_action() -> None:
    with _session() as db:
        analyst = _analyst(db)
        events = list(stream("Create an incident because latency is high", db, analyst))
        incident = db.scalars(select(Incident)).one()
        assert incident.actions == []
    names = [name for name, _ in events]
    assert "approval_required" not in names
    assert names[-1] == "done"


def test_restart_proposal_uses_the_named_service_and_requires_approval() -> None:
    with _session() as db:
        analyst = _analyst(db)
        events = list(stream("create an incident and restart the worker", db, analyst))
        action = db.scalars(select(Action)).one()
        assert (action.kind, action.parameters, action.status) == (
            "restart_service",
            {"service": "worker"},
            ActionStatus.PENDING,
        )
    approval = next(payload for name, payload in events if name == "approval_required")
    assert approval["parameters"] == {"service": "worker"}


@pytest.mark.parametrize(
    "question",
    [
        "create an incident and restart the database",
        "create an incident and roll back the deployment",
    ],
)
def test_unsupported_action_targets_do_not_crash_or_attach_actions(question: str) -> None:
    with _session() as db:
        analyst = _analyst(db)
        events = list(stream(question, db, analyst))
        assert db.scalar(select(func.count()).select_from(Action)) == 0
        assert db.scalar(select(func.count()).select_from(Incident)) == 1
    text = "".join(payload["text"] for name, payload in events if name == "token")
    assert "no operational action attached" in text
    assert events[-1] == ("done", events[-1][1]) and events[-1][1]["status"] == "completed"


def test_incident_lookup_is_read_only_and_owner_scoped() -> None:
    with _session() as db:
        analyst = _analyst(db)
        other = User(
            email="other-analyst@example.com",
            password_hash="unused",
            name="Other",
            role=UserRole.ANALYST,
        )
        db.add(other)
        db.commit()
        incident = propose(db, analyst, IncidentProposalCreate(title="Queue backlog", summary="s"))

        own = list(stream(f"show me incident {incident.id}", db, analyst))
        foreign = list(stream(f"show me incident {incident.id}", db, other))
        invalid = list(stream("show me incident 123", db, analyst))
        assert db.scalar(select(func.count()).select_from(Incident)) == 1

    assert "Queue backlog" in "".join(p["text"] for n, p in own if n == "token")
    assert "couldn't find" in "".join(p["text"] for n, p in foreign if n == "token")
    assert "UUIDs" in "".join(p["text"] for n, p in invalid if n == "token")
