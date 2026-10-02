"""Offline regression evaluation for deterministic OpsAI behavior.

Every case runs real code paths against an in-memory SQLite database: document
processing and local-embedding retrieval, intent routing, agent mutation safety,
the incident approval lifecycle, and the MCP-style adapter. It checks behavior
contracts; it is not a measure of language-model answer quality.

    python -m app.evaluation
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agent import IntentKind, parse_intent
from app.agent import stream as stream_agent
from app.chat import LocalChatProvider
from app.config import Settings
from app.incidents import approve, execute, propose
from app.mcp import handle_request
from app.models import (
    Action,
    ActionStatus,
    ApprovalDecision,
    Base,
    Document,
    DocumentStatus,
    Incident,
    ServiceMetric,
    User,
    UserRole,
)
from app.processing import process_document
from app.retrieval import search_owned_chunks
from app.schemas import IncidentProposalCreate
from app.tools import get_metric

# Pinned so environment variables (for example an external embedding provider) cannot
# change the outcome.
EVALUATION_SETTINGS = Settings(
    _env_file=None,
    embedding_provider="local",
    embedding_dimension=384,
    chat_provider="local",
    chunk_size=1000,
    chunk_overlap=100,
    retrieval_similarity_threshold=0.35,
)

RUNBOOK = (
    "Incident response runbook. When API latency exceeds the p95 budget of 300 ms, the "
    "on-call engineer should check the database connection pool and the queue depth before "
    "restarting any service. Restarts of the api service require an approved incident "
    "proposal. Rollbacks are performed by the release manager and must reference the "
    "deployment identifier.\n\nRefunds are processed within 14 days. The VPN is configured "
    "with the corporate identity provider; employees are required to enable MFA."
)

ROUTING_CASES: tuple[tuple[str, IntentKind], ...] = (
    ("what is the latency?", IntentKind.METRIC),
    ("what is latency p95?", IntentKind.METRIC),
    ("what metrics are available?", IntentKind.METRIC),
    ("create an incident because latency is high", IntentKind.INCIDENT),
    ("create an incident and restart the API", IntentKind.INCIDENT),
    ("can you restart the API?", IntentKind.ACTION_REQUEST),
    ("show me incident 123", IntentKind.INCIDENT_LOOKUP),
    ("search my resume for React", IntentKind.KNOWLEDGE),
    ("tell me something unrelated", IntentKind.NONE),
)


@dataclass(frozen=True)
class EvaluationCase:
    category: str
    name: str
    expected: str
    actual: str

    @property
    def passed(self) -> bool:
        return self.expected == self.actual


@dataclass(frozen=True)
class EvaluationReport:
    cases: tuple[EvaluationCase, ...]

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(case.passed for case in self.cases)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 1.0

    def text(self) -> str:
        lines = [
            f"total: {self.total}",
            f"passed: {self.passed}",
            f"failed: {self.failed}",
            f"pass_rate: {self.pass_rate:.1%}",
        ]
        lines.extend(
            f"[{'PASS' if case.passed else 'FAIL'}] {case.category}/{case.name}"
            f" expected={case.expected!r} actual={case.actual!r}"
            for case in self.cases
        )
        return "\n".join(lines)


class _MemoryStorage:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def save(self, key: str, data: bytes) -> None:
        self.files[key] = data

    def read(self, key: str) -> bytes:
        return self.files[key]

    def delete(self, key: str) -> None:
        self.files.pop(key, None)


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _user(db: Session, role: UserRole) -> User:
    user = User(
        email=f"evaluation-{role.value}@example.com",
        password_hash="unused",
        name=role.value.title(),
        role=role,
    )
    db.add(user)
    db.commit()
    return user


def _ingest(db: Session, owner: User, text: str) -> None:
    """Run the worker's processing pipeline on an in-memory file."""
    file_storage = _MemoryStorage()
    document = Document(
        id=uuid4(),
        owner_id=owner.id,
        filename="runbook.md",
        content_type="text/markdown",
        file_size=len(text),
        checksum="0" * 64,
        storage_key=f"{owner.id}/runbook",
        status=DocumentStatus.UPLOADED,
    )
    db.add(document)
    db.commit()
    file_storage.save(document.storage_key, text.encode())
    if not process_document(
        db, document.id, file_storage=file_storage, settings=EVALUATION_SETTINGS
    ):
        raise RuntimeError("Evaluation corpus failed to process")


def _grounding(db: Session, user: User, question: str) -> str:
    results = search_owned_chunks(db, user, question, 5, EVALUATION_SETTINGS)
    return "grounded" if results else "no_context"


def _count(db: Session, model: type[Incident] | type[Action]) -> str:
    return str(db.scalar(select(func.count()).select_from(model)))


def _knowledge_cases(db: Session, owner: User, stranger: User) -> list[EvaluationCase]:
    question = "How is the VPN configured?"
    results = search_owned_chunks(db, owner, question, 5, EVALUATION_SETTINGS)
    answer = "".join(LocalChatProvider(EVALUATION_SETTINGS).stream(question, results, []))
    return [
        EvaluationCase("knowledge", "relevant_query", "grounded", _grounding(db, owner, question)),
        EvaluationCase(
            "knowledge",
            "off_topic_stopword_overlap",
            "no_context",
            _grounding(db, owner, "What is the weather today?"),
        ),
        EvaluationCase(
            "knowledge",
            "weak_context",
            "no_context",
            _grounding(db, owner, "tell me something unrelated"),
        ),
        EvaluationCase(
            "knowledge", "owner_isolation", "no_context", _grounding(db, stranger, question)
        ),
        EvaluationCase(
            "knowledge",
            "extractive_answer_quotes_source",
            "quoted",
            "quoted" if "corporate identity provider" in answer else "missing",
        ),
    ]


def _safety_cases(db: Session, viewer: User, analyst: User) -> list[EvaluationCase]:
    try:
        propose(db, viewer, IncidentProposalCreate(title="Denied", summary="Viewer request"))
        viewer_result = "allowed"
    except PermissionError:
        viewer_result = "denied"
    cases = [EvaluationCase("safety", "viewer_incident_denial", "denied", viewer_result)]
    for name, question in (
        ("action_request_creates_nothing", "can you restart the API?"),
        ("rollback_question_creates_nothing", "why did we roll back yesterday?"),
        ("negated_request_creates_nothing", "don't create an incident"),
    ):
        list(stream_agent(question, db, analyst))
        cases.append(EvaluationCase("safety", name, "0", _count(db, Incident)))
    list(stream_agent("create an incident and restart the database", db, analyst))
    cases.append(
        EvaluationCase("safety", "unsupported_target_attaches_no_action", "0", _count(db, Action))
    )
    return cases


def _incident_cases(db: Session, analyst: User, admin: User) -> list[EvaluationCase]:
    proposal = IncidentProposalCreate(
        title="Evaluation incident",
        summary="Proposal for evaluation",
        action_kind="restart_service",
        action_parameters={"service": "api"},
    )
    action = propose(db, analyst, proposal).actions[0]
    cases = [
        EvaluationCase("incident", "analyst_proposal", "pending", action.status.value),
    ]
    approval = approve(db, action, admin, ApprovalDecision.APPROVED, "evaluation-approval")
    cases.append(EvaluationCase("incident", "admin_approval", "approved", approval.decision.value))
    executed = execute(db, action, admin)
    cases.append(
        EvaluationCase("incident", "execution_after_approval", "executed", executed.status.value)
    )
    repeated = execute(db, action, admin)
    cases.append(
        EvaluationCase(
            "incident",
            "repeated_execution_idempotency",
            f"{executed.id}:{ActionStatus.EXECUTED.value}",
            f"{repeated.id}:{repeated.status.value}",
        )
    )
    rejected = propose(db, analyst, proposal).actions[0]
    approve(db, rejected, admin, ApprovalDecision.REJECTED, "evaluation-rejection")
    try:
        execute(db, rejected, admin)
        rejected_result = "executed"
    except ValueError:
        rejected_result = "refused"
    cases.append(
        EvaluationCase("incident", "rejected_action_not_executable", "refused", rejected_result)
    )
    own = propose(db, admin, proposal).actions[0]
    try:
        approve(db, own, admin, ApprovalDecision.APPROVED, "evaluation-self-approval")
        self_approval = "approved"
    except PermissionError:
        self_approval = "denied"
    cases.append(EvaluationCase("incident", "self_approval_denied", "denied", self_approval))
    return cases


def _mcp_cases(db: Session, viewer: User) -> list[EvaluationCase]:
    def call(request_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            request["params"] = params
        return handle_request(request, db, viewer)

    tools_list = call(1, "tools/list")
    metric_call = call(
        2, "tools/call", {"name": "get_metric", "arguments": {"name": "latency_p95"}}
    )
    denied = call(
        3,
        "tools/call",
        {"name": "create_incident", "arguments": {"title": "Denied", "summary": "Viewer"}},
    )
    invalid = call(4, "tools/call", {"name": "search_knowledge", "arguments": {}})
    unknown = call(5, "tools/call", {"name": "unknown_tool", "arguments": {}})
    return [
        EvaluationCase("mcp", "tools_list", "4", str(len(tools_list["result"]["tools"]))),
        EvaluationCase(
            "mcp", "valid_tool_call", "success", "success" if "result" in metric_call else "failure"
        ),
        EvaluationCase("mcp", "rbac_denial", "-32003", str(denied["error"]["code"])),
        EvaluationCase("mcp", "invalid_tool_arguments", "-32602", str(invalid["error"]["code"])),
        EvaluationCase("mcp", "unknown_tool", "-32602", str(unknown["error"]["code"])),
    ]


def run_evaluation() -> EvaluationReport:
    cases: list[EvaluationCase] = []
    with _session() as db:
        viewer = _user(db, UserRole.VIEWER)
        analyst = _user(db, UserRole.ANALYST)
        admin = _user(db, UserRole.ADMIN)
        db.add(
            ServiceMetric(
                service="api",
                name="latency_p95",
                value=240.0,
                unit="ms",
                metric_metadata={"source": "evaluation"},
            )
        )
        db.commit()
        _ingest(db, viewer, RUNBOOK)

        cases.extend(_knowledge_cases(db, owner=viewer, stranger=analyst))
        cases.extend(
            EvaluationCase("routing", question, expected.value, parse_intent(question).kind.value)
            for question, expected in ROUTING_CASES
        )
        latency = get_metric(db, name="latency_p95")
        cases.extend(
            [
                EvaluationCase(
                    "metrics",
                    "allowlisted_metric_reads_recorded_value",
                    "api latency_p95=240ms",
                    "; ".join(f"{m.service} {m.name}={m.value:g}{m.unit}" for m in latency),
                ),
                EvaluationCase(
                    "metrics",
                    "unknown_metric_returns_nothing",
                    "0",
                    str(len(get_metric(db, name="unknown_metric"))),
                ),
            ]
        )
        cases.extend(_safety_cases(db, viewer, analyst))
        cases.extend(_incident_cases(db, analyst, admin))
        cases.extend(_mcp_cases(db, viewer))
    return EvaluationReport(tuple(cases))


if __name__ == "__main__":
    report = run_evaluation()
    print(report.text())
    raise SystemExit(0 if report.failed == 0 else 1)
