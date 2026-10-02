"""Offline deterministic orchestration with typed intent and allowlisted actions.

Routing rules, in priority order:

1. An explicit, non-negated request to create/open/raise an incident becomes an
   incident *proposal*. An operational action is attached only when the message
   names a supported action and an allowlisted target; nothing is inferred.
2. A message referencing an incident identifier is a read-only incident lookup.
3. An operational command without an incident request ("restart the api") is
   declined with guidance; no tool runs.
4. Document markers route to knowledge retrieval, metric phrases to metrics.
5. Everything else falls back to read-only knowledge retrieval in the chat layer.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.incidents import audit, can_operate, get_incident, propose
from app.models import User, UserRole
from app.observability import log_event, timed_operation
from app.schemas import RESTARTABLE_SERVICES, IncidentProposalCreate
from app.tools import get_metric

logger = logging.getLogger(__name__)


class IntentKind(StrEnum):
    NONE = "none"
    KNOWLEDGE = "knowledge"
    METRIC = "metric"
    INCIDENT = "incident"
    INCIDENT_LOOKUP = "incident_lookup"
    ACTION_REQUEST = "action_request"


@dataclass(frozen=True)
class AgentIntent:
    kind: IntentKind
    query: str
    metric_name: str | None = None
    action_kind: str | None = None
    service: str | None = None
    incident_id: UUID | None = None


# Intents answered by the agent's tools; knowledge and fallback stay on the RAG path.
AGENT_INTENTS = frozenset(
    {IntentKind.METRIC, IntentKind.INCIDENT, IntentKind.INCIDENT_LOOKUP, IntentKind.ACTION_REQUEST}
)

_METRIC_PHRASES: tuple[tuple[str, str], ...] = (
    ("payment failure rate", "payment_failure_rate"),
    ("payment failure", "payment_failure_rate"),
    ("daily error rate", "daily_error_rate"),
    ("transaction count", "transaction_count"),
    ("transactions", "transaction_count"),
    ("latency", "latency_p95"),
    ("p95", "latency_p95"),
    ("queue depth", "queue_depth"),
    ("error rate", "error_rate"),
)
_GENERIC_METRIC = re.compile(r"\b(?:metrics?|service health)\b")
_INCIDENT_REQUEST = re.compile(
    r"\b(?:create|open|raise|file|propose|declare|log|report)\s+(?:an?\s+)?(?:new\s+)?incident\b"
)
_NEGATION_BEFORE = re.compile(r"\b(?:don[’']?t|do not|never|no need to)\s+(?:\w+\s+){0,2}$")
_RESTART = re.compile(r"\b(?:restart|reboot)\b")
_RESTART_TARGET = re.compile(r"\b(?:restart|reboot)\s+(?:the\s+)?(?:service\s+)?([a-z]+)\b")
_ROLLBACK = re.compile(r"\b(?:roll[\s-]?back|redeploy)\b")
_QUESTION_START = re.compile(
    r"^(?:why|when|what|how|who|whom|which|where|did|was|were|has|have|had|is|are|does)\b"
)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_INCIDENT_NUMBER = re.compile(r"\bincident\s+#?\d+\b")
_STRONG_KNOWLEDGE = re.compile(
    r"\b(?:documents?|resume|uploaded|files?|polic(?:y|ies)|runbooks?|handbook|according to)\b"
)
_WEAK_KNOWLEDGE = ("what does", "what is in")


def _requested_restart_target(normalized: str) -> str | None:
    match = _RESTART_TARGET.search(normalized)
    if match is None or match.group(1) not in RESTARTABLE_SERVICES:
        return None
    return match.group(1)


def _incident_requested(normalized: str) -> bool:
    for match in _INCIDENT_REQUEST.finditer(normalized):
        if not _NEGATION_BEFORE.search(normalized[: match.start()]):
            return True
    return False


def parse_intent(question: str) -> AgentIntent:
    """Map text to a closed set of typed intents; no model or arbitrary tool names."""
    normalized = " ".join(question.casefold().split())
    restart = _RESTART.search(normalized) is not None
    rollback = _ROLLBACK.search(normalized) is not None
    action_kind = "restart_service" if restart else "rollback_deployment" if rollback else None
    service = _requested_restart_target(normalized) if restart else None

    if _incident_requested(normalized):
        return AgentIntent(IntentKind.INCIDENT, question, action_kind=action_kind, service=service)
    if "incident" in normalized and (
        _UUID.search(normalized) or _INCIDENT_NUMBER.search(normalized)
    ):
        uuid_match = _UUID.search(normalized)
        incident_id = UUID(uuid_match.group(0)) if uuid_match else None
        return AgentIntent(IntentKind.INCIDENT_LOOKUP, question, incident_id=incident_id)
    if action_kind is not None and not _QUESTION_START.search(normalized):
        return AgentIntent(
            IntentKind.ACTION_REQUEST, question, action_kind=action_kind, service=service
        )
    if _STRONG_KNOWLEDGE.search(normalized):
        return AgentIntent(IntentKind.KNOWLEDGE, question)
    metric_name = next((name for phrase, name in _METRIC_PHRASES if phrase in normalized), None)
    if metric_name is not None or _GENERIC_METRIC.search(normalized):
        return AgentIntent(IntentKind.METRIC, question, metric_name=metric_name)
    if any(marker in normalized for marker in _WEAK_KNOWLEDGE):
        return AgentIntent(IntentKind.KNOWLEDGE, question)
    return AgentIntent(IntentKind.NONE, question)


def should_handle(question: str) -> bool:
    return parse_intent(question).kind in AGENT_INTENTS


def _incident_title(question: str) -> str:
    cleaned = " ".join(question.split())
    return cleaned if len(cleaned) <= 120 else f"{cleaned[:117]}..."


def _stream_metric(
    intent: AgentIntent, db: Session, user: User
) -> Iterator[tuple[str, dict[str, Any]]]:
    arguments: dict[str, Any] = {}
    if intent.metric_name:
        arguments["name"] = intent.metric_name
    yield "tool_call", {"name": "get_metric", "arguments": arguments}
    with timed_operation(logger, "agent.tool_execution", tool="get_metric", user_id=str(user.id)):
        metrics = get_metric(db, name=intent.metric_name)
    yield (
        "tool_result",
        {
            "name": "get_metric",
            "result": [item.model_dump(mode="json") for item in metrics],
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


def _stream_incident_proposal(
    intent: AgentIntent, db: Session, user: User, conversation_id: Any
) -> Iterator[tuple[str, dict[str, Any]]]:
    arguments: dict[str, Any] = {"summary": intent.query}
    if intent.action_kind == "restart_service" and intent.service:
        arguments.update(action_kind="restart_service", service=intent.service)
    yield "tool_call", {"name": "create_incident", "arguments": arguments}
    if not can_operate(user):
        log_event(
            logger,
            "agent.permission_denied",
            level=logging.WARNING,
            operation="incident.propose",
            tool="create_incident",
            user_id=str(user.id),
            status="denied",
        )
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

    # Only an explicitly requested action on an allowlisted target is attached.
    note = ""
    if intent.action_kind == "restart_service" and intent.service is None:
        allowed = ", ".join(sorted(RESTARTABLE_SERVICES))
        note = f" No restart was attached because the target is not one of: {allowed}."
    elif intent.action_kind == "rollback_deployment":
        note = (
            " Rollbacks need a deployment identifier, so no rollback was attached;"
            " propose it through the incidents API."
        )
    with timed_operation(
        logger, "agent.tool_execution", tool="create_incident", user_id=str(user.id)
    ):
        incident = propose(
            db,
            user,
            IncidentProposalCreate(
                title=_incident_title(intent.query),
                summary=intent.query.strip()[:5000],
                severity="high" if "outage" in intent.query.casefold() else "medium",
                action_kind="restart_service" if "service" in arguments else None,
                action_parameters={"service": intent.service} if "service" in arguments else {},
                conversation_id=conversation_id,
            ),
        )
    action = incident.actions[0] if incident.actions else None
    yield (
        "tool_result",
        {
            "name": "create_incident",
            "result": {
                "incident_id": str(incident.id),
                "action_id": str(action.id) if action else None,
            },
            "read_only": False,
        },
    )
    if action:
        yield (
            "approval_required",
            {
                "action_id": str(action.id),
                "incident_id": str(incident.id),
                "kind": action.kind,
                "parameters": action.parameters,
                "expires_at": action.expires_at.isoformat(),
            },
        )
        approver = "another administrator" if user.role == UserRole.ADMIN else "an administrator"
        text = (
            f"I created an incident proposal with a {action.kind} action for the "
            f"{intent.service} service. Nothing runs until {approver} approves it."
        )
    else:
        text = "I created an incident proposal with no operational action attached." + note
    yield "token", {"text": text}
    yield "done", {"status": "completed", "incident_id": str(incident.id)}


def _stream_incident_lookup(
    intent: AgentIntent, db: Session, user: User
) -> Iterator[tuple[str, dict[str, Any]]]:
    incident_id = str(intent.incident_id) if intent.incident_id else None
    yield "tool_call", {"name": "get_incident", "arguments": {"incident_id": incident_id}}
    if intent.incident_id is None:
        yield (
            "tool_result",
            {"name": "get_incident", "result": {"error": "invalid_id"}, "read_only": True},
        )
        yield "token", {"text": "Incident IDs are UUIDs; I couldn't find one in that message."}
        yield "done", {"status": "completed", "tool": "get_incident"}
        return
    with timed_operation(logger, "agent.tool_execution", tool="get_incident", user_id=str(user.id)):
        incident = get_incident(db, user, intent.incident_id)
    if incident is None:
        audit(
            db,
            user,
            "permission.denied",
            "incident",
            intent.incident_id,
            {"operation": "read", "source": "agent"},
        )
        db.commit()
        yield (
            "tool_result",
            {"name": "get_incident", "result": {"error": "not_found"}, "read_only": True},
        )
        yield "token", {"text": "I couldn't find an incident with that ID that you can access."}
        yield "done", {"status": "completed", "tool": "get_incident"}
        return
    actions = "; ".join(f"{item.kind} ({item.status.value})" for item in incident.actions)
    yield (
        "tool_result",
        {
            "name": "get_incident",
            "result": {"incident_id": str(incident.id), "status": incident.status.value},
            "read_only": True,
        },
    )
    yield (
        "token",
        {
            "text": (
                f"Incident “{incident.title}” is {incident.status.value} "
                f"(severity {incident.severity}). "
                + (f"Actions: {actions}." if actions else "No actions are attached.")
            )
        },
    )
    yield "done", {"status": "completed", "tool": "get_incident"}


def _stream_action_request(user: User) -> Iterator[tuple[str, dict[str, Any]]]:
    log_event(
        logger,
        "agent.action_request_declined",
        operation="action_request",
        user_id=str(user.id),
        status="declined",
    )
    text = (
        "I can't run operational actions directly. Ask me to "
        "“create an incident and restart the api service” to record a proposal; "
        "an administrator must approve it before anything runs."
    )
    if not can_operate(user):
        text += " Only analysts and administrators can create incident proposals."
    yield "token", {"text": text}
    yield "done", {"status": "completed"}


def stream(
    question: str,
    db: Session,
    user: User,
    *,
    conversation_id: Any = None,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Stream agent events for tool intents; knowledge and fallback intents yield nothing."""
    intent = parse_intent(question)
    log_event(
        logger,
        "agent.intent_classified",
        operation="intent_classification",
        user_id=str(user.id),
        status="completed",
        intent=intent.kind.value,
    )
    if intent.kind == IntentKind.METRIC:
        yield from _stream_metric(intent, db, user)
    elif intent.kind == IntentKind.INCIDENT:
        yield from _stream_incident_proposal(intent, db, user, conversation_id)
    elif intent.kind == IntentKind.INCIDENT_LOOKUP:
        yield from _stream_incident_lookup(intent, db, user)
    elif intent.kind == IntentKind.ACTION_REQUEST:
        yield from _stream_action_request(user)


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
