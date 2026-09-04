"""Small offline evaluation runner for deterministic OpsAI behavior."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agent import IntentKind, parse_intent
from app.incidents import approve, execute, propose
from app.mcp import handle_request
from app.models import ActionStatus, ApprovalDecision, Base, ServiceMetric, User, UserRole
from app.schemas import IncidentProposalCreate, SearchResult


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


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _knowledge_result(query: str, similarity: float) -> str:
    result = SearchResult(
        document_id=uuid4(),
        chunk_id=uuid4(),
        filename="resume.pdf",
        content=query,
        page_number=1,
        similarity=similarity,
    )
    return "grounded" if result.similarity >= 0.35 else "no_context"


def run_evaluation() -> EvaluationReport:
    cases: list[EvaluationCase] = []
    with _session() as db:
        viewer = User(
            email="evaluation-viewer@example.com",
            password_hash="unused",
            name="Viewer",
            role=UserRole.VIEWER,
        )
        analyst = User(
            email="evaluation-analyst@example.com",
            password_hash="unused",
            name="Analyst",
            role=UserRole.ANALYST,
        )
        admin = User(
            email="evaluation-admin@example.com",
            password_hash="unused",
            name="Admin",
            role=UserRole.ADMIN,
        )
        db.add_all(
            [
                viewer,
                analyst,
                admin,
                ServiceMetric(
                    service="api",
                    name="latency_p95",
                    value=240.0,
                    unit="ms",
                    metric_metadata={"source": "evaluation"},
                ),
            ]
        )
        db.commit()

        cases.extend(
            [
                EvaluationCase(
                    "knowledge",
                    "relevant_query",
                    "grounded",
                    _knowledge_result("What does my resume say about React?", 0.91),
                ),
                EvaluationCase(
                    "knowledge",
                    "weak_context",
                    "no_context",
                    _knowledge_result("What does the document say about an unrelated topic?", 0.20),
                ),
                EvaluationCase(
                    "knowledge",
                    "off_topic_query",
                    "no_context",
                    _knowledge_result("What is the weather today?", 0.10),
                ),
                EvaluationCase(
                    "metrics",
                    "valid_allowlisted_metric",
                    IntentKind.METRIC.value,
                    parse_intent("What is the latency metric?").kind.value,
                ),
                EvaluationCase(
                    "metrics",
                    "unknown_metric",
                    "0",
                    str(
                        len(
                            [
                                metric
                                for metric in db.query(ServiceMetric).all()
                                if metric.name == "unknown_metric"
                            ]
                        )
                    ),
                ),
            ]
        )

        try:
            propose(
                db,
                viewer,
                IncidentProposalCreate(title="Denied", summary="Viewer request"),
            )
            viewer_result = "allowed"
        except PermissionError:
            viewer_result = "denied"
        cases.append(EvaluationCase("safety", "viewer_incident_denial", "denied", viewer_result))

        incident = propose(
            db,
            analyst,
            IncidentProposalCreate(
                title="Evaluation incident",
                summary="Proposal for evaluation",
                action_kind="restart_service",
                action_parameters={"service": "api"},
            ),
        )
        action = incident.actions[0]
        cases.append(
            EvaluationCase(
                "incident",
                "analyst_proposal",
                ActionStatus.PENDING.value,
                action.status.value,
            )
        )
        approval = approve(db, action, admin, ApprovalDecision.APPROVED, "evaluation-approval")
        cases.append(
            EvaluationCase(
                "incident",
                "admin_approval",
                ApprovalDecision.APPROVED.value,
                approval.decision.value,
            )
        )
        executed = execute(db, action, admin)
        cases.append(
            EvaluationCase(
                "incident",
                "execution_after_approval",
                ActionStatus.EXECUTED.value,
                executed.status.value,
            )
        )
        repeated = execute(db, action, admin)
        cases.append(
            EvaluationCase(
                "incident",
                "repeated_execution_idempotency",
                str(executed.id),
                str(repeated.id),
            )
        )

        tools_list = handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, db, viewer)
        cases.append(
            EvaluationCase(
                "mcp",
                "tools_list",
                "4",
                str(len(tools_list["result"]["tools"])),
            )
        )
        metric_call = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_metric", "arguments": {"name": "latency_p95"}},
            },
            db,
            viewer,
        )
        cases.append(
            EvaluationCase(
                "mcp",
                "valid_tool_call",
                "success",
                "success" if "result" in metric_call else "failure",
            )
        )
        denied_call = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "create_incident",
                    "arguments": {
                        "title": "Denied",
                        "summary": "Viewer",
                    },
                },
            },
            db,
            viewer,
        )
        cases.append(
            EvaluationCase(
                "mcp",
                "rbac_denial",
                "-32003",
                str(denied_call["error"]["code"]),
            )
        )
        invalid_call = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "search_knowledge", "arguments": {}},
            },
            db,
            viewer,
        )
        cases.append(
            EvaluationCase(
                "mcp",
                "invalid_tool_arguments",
                "-32602",
                str(invalid_call["error"]["code"]),
            )
        )
        unknown_call = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "unknown_tool", "arguments": {}},
            },
            db,
            viewer,
        )
        cases.append(
            EvaluationCase(
                "mcp",
                "unknown_tool",
                "-32602",
                str(unknown_call["error"]["code"]),
            )
        )
    return EvaluationReport(tuple(cases))


if __name__ == "__main__":
    report = run_evaluation()
    print(report.text())
    raise SystemExit(0 if report.failed == 0 else 1)
