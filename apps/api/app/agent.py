"""Offline deterministic orchestration with typed intent and allowlisted actions."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from app.incidents import audit, propose
from app.models import User, UserRole
from app.schemas import IncidentProposalCreate
from app.tools import get_metric, search_knowledge


class IntentKind(StrEnum):
    NONE = "none"
    KNOWLEDGE = "knowledge"
    METRIC = "metric"
    INCIDENT = "incident"


@dataclass(frozen=True)
class AgentIntent:
    kind: IntentKind
    query: str
    metric_name: str | None = None
    action_kind: str | None = None


_METRIC_PHRASES: tuple[tuple[str, str], ...] = (
    ("payment failure rate", "payment_failure_rate"),
    ("payment failure", "payment_failure_rate"),
    ("daily error rate", "daily_error_rate"),
    ("transaction count", "transaction_count"),
    ("transactions", "transaction_count"),
    ("latency", "latency_p95"),
    ("queue depth", "queue_depth"),
    ("error rate", "error_rate"),
)
_INCIDENT_ACTIONS: tuple[tuple[str, str], ...] = (
    ("rollback", "rollback_deployment"),
    ("roll back", "rollback_deployment"),
    ("restart", "restart_service"),
)
_KNOWLEDGE_MARKERS = (
    "document",
    "resume",
    "uploaded",
    "file",
    "policy",
    "what does",
    "what is in",
)


def parse_intent(question: str) -> AgentIntent:
    """Map text to a closed set of typed intents; no model or arbitrary tool names."""
    normalized = " ".join(question.casefold().split())
    metric_name = next((name for phrase, name in _METRIC_PHRASES if phrase in normalized), None)
    incident_marker = any(
        phrase in normalized
        for phrase in (
            "create an incident",
            "create incident",
            "propose an incident",
            "propose incident",
            "please restart",
            "restart service",
            "rollback deployment",
            "roll back",
        )
    )
    if incident_marker:
        action_kind = next(
            (action for phrase, action in _INCIDENT_ACTIONS if phrase in normalized), None
        )
        return AgentIntent(IntentKind.INCIDENT, question, action_kind=action_kind)
    if metric_name is not None or any(
        phrase in normalized for phrase in ("metric", "service health")
    ):
        return AgentIntent(IntentKind.METRIC, question, metric_name=metric_name)
    if any(marker in normalized for marker in _KNOWLEDGE_MARKERS):
        return AgentIntent(IntentKind.KNOWLEDGE, question)
    return AgentIntent(IntentKind.NONE, question)


def should_handle(question: str) -> bool:
    return parse_intent(question).kind in {IntentKind.METRIC, IntentKind.INCIDENT}


def stream(
    question: str,
    db: Session,
    user: User,
    *,
    conversation_id: Any = None,
) -> Iterator[tuple[str, dict[str, Any]]]:
    intent = parse_intent(question)
    if intent.kind == IntentKind.METRIC:
        arguments: dict[str, Any] = {}
        if intent.metric_name:
            arguments["name"] = intent.metric_name
        yield "tool_call", {"name": "get_metric", "arguments": arguments}
        metrics = get_metric(db, name=intent.metric_name)
        metric_result = [item.model_dump(mode="json") for item in metrics]
        yield (
            "tool_result",
            {
                "name": "get_metric",
                "result": metric_result,
                "read_only": True,
            },
        )
        if metrics:
            answer = "Recorded service metrics: " + "; ".join(
                f"{item.service} {item.name}={item.value:g}{item.unit}" for item in metrics
            )
        else:
            answer = "No recorded metric is available for that request."
        yield "token", {"text": answer}
        yield "done", {"status": "completed", "tool": "get_metric"}
        return

    if intent.kind == IntentKind.INCIDENT:
        yield "tool_call", {"name": "create_incident", "arguments": {"summary": question}}
        if user.role not in {UserRole.ADMIN, UserRole.ANALYST}:
            audit(db, user, "permission.denied", "incident", None, {"operation": "propose"})
            db.commit()
            yield (
                "tool_result",
                {
                    "name": "create_incident",
                    "result": {"error": "permission_denied"},
                    "read_only": False,
                },
            )
            yield "error", {"message": "You do not have permission to create incident proposals."}
            yield "done", {"status": "denied"}
            return
        incident = propose(
            db,
            user,
            IncidentProposalCreate(
                title="Agent incident proposal",
                summary=question,
                severity="high" if "outage" in question.casefold() else "medium",
                action_kind=intent.action_kind or "restart_service",
                action_parameters={"service": "api"},
                conversation_id=conversation_id,
            ),
        )
        action = incident.actions[0] if incident.actions else None
        result = {
            "incident_id": str(incident.id),
            "action_id": str(action.id) if action else None,
        }
        yield "tool_result", {"name": "create_incident", "result": result}
        if action:
            yield (
                "approval_required",
                {
                    "action_id": str(action.id),
                    "incident_id": str(incident.id),
                    "expires_at": action.expires_at.isoformat(),
                },
            )
        yield (
            "token",
            {
                "text": (
                    "I created an incident proposal. Approval is required "
                    "before any action can run."
                )
            },
        )
        yield "done", {"status": "completed", "incident_id": str(incident.id)}
        return

    if intent.kind == IntentKind.NONE:
        return

    # Knowledge requests remain on the existing grounded chat path.
    yield "tool_call", {"name": "search_knowledge", "arguments": {"query": question}}
    results = search_knowledge(db, user, question)
    yield (
        "tool_result",
        {
            "name": "search_knowledge",
            "result": {"count": len(results)},
            "read_only": True,
        },
    )


class LocalDeterministicAgent:
    def stream(
        self,
        question: str,
        db: Session,
        user: User,
        *,
        conversation_id: Any = None,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        yield from stream(question, db, user, conversation_id=conversation_id)
