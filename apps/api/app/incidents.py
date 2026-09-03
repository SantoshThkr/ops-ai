"""Incident proposals and approval-gated, idempotent local execution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ActionStatus,
    Approval,
    ApprovalDecision,
    AuditLog,
    Incident,
    IncidentStatus,
    User,
    UserRole,
)
from app.schemas import IncidentProposalCreate


def _expired(expires_at: datetime) -> bool:
    current = datetime.now(UTC)
    if expires_at.tzinfo is None:
        current = current.replace(tzinfo=None)
    return expires_at <= current


def audit(
    db: Session,
    actor: User | None,
    event: str,
    resource_type: str,
    resource_id: UUID | str | None,
    details: dict[str, object] | None = None,
) -> None:
    db.add(
        AuditLog(
            actor_id=actor.id if actor else None,
            event=event,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            details=details or {},
        )
    )


def can_manage(user: User, owner_id: UUID) -> bool:
    """Incident data is restricted to its owner and administrators."""
    return user.role == UserRole.ADMIN or user.id == owner_id


def can_operate(user: User) -> bool:
    return user.role in {UserRole.ADMIN, UserRole.ANALYST}


def can_approve(user: User) -> bool:
    return user.role == UserRole.ADMIN


def get_incident(db: Session, user: User, incident_id: UUID) -> Incident | None:
    incident = db.scalar(select(Incident).where(Incident.id == incident_id))
    if incident is None or not can_manage(user, incident.owner_id):
        return None
    return incident


def propose(db: Session, owner: User, payload: IncidentProposalCreate) -> Incident:
    if not can_operate(owner):
        audit(db, owner, "permission.denied", "incident", None, {"operation": "propose"})
        db.commit()
        raise PermissionError("Only analysts and administrators can create incident proposals")
    if payload.conversation_id is not None:
        from app.models import Conversation

        conversation = db.scalar(
            select(Conversation).where(
                Conversation.id == payload.conversation_id,
                Conversation.user_id == owner.id,
            )
        )
        if conversation is None:
            audit(
                db,
                owner,
                "permission.denied",
                "conversation",
                payload.conversation_id,
                {"operation": "incident.propose"},
            )
            db.commit()
            raise PermissionError("Conversation not found")
    if payload.idempotency_key:
        existing_action = db.scalar(
            select(Action).where(
                Action.owner_id == owner.id, Action.idempotency_key == payload.idempotency_key
            )
        )
        if existing_action:
            existing_incident = db.scalar(
                select(Incident).where(Incident.id == existing_action.incident_id)
            )
            if existing_incident:
                return existing_incident
    incident = Incident(
        owner_id=owner.id,
        conversation_id=payload.conversation_id,
        title=payload.title.strip(),
        summary=payload.summary.strip(),
        severity=payload.severity,
        status=IncidentStatus.PROPOSED,
    )
    db.add(incident)
    db.flush()
    action_specs = list(payload.actions)
    if payload.action_kind and not action_specs:
        from app.schemas import ProposedAction

        action_specs = [
            ProposedAction(kind=payload.action_kind, parameters=payload.action_parameters)
        ]
    for index, action_spec in enumerate(action_specs):
        db.add(
            Action(
                incident_id=incident.id,
                owner_id=owner.id,
                kind=action_spec.kind,
                parameters=action_spec.parameters,
                idempotency_key=payload.idempotency_key if index == 0 else None,
                expires_at=datetime.now(UTC) + timedelta(minutes=payload.expires_in_minutes),
            )
        )
    audit(db, owner, "incident.proposed", "incident", incident.id, {"severity": payload.severity})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if payload.idempotency_key:
            existing_action = db.scalar(
                select(Action).where(
                    Action.owner_id == owner.id, Action.idempotency_key == payload.idempotency_key
                )
            )
            if existing_action:
                existing_incident = db.scalar(
                    select(Incident).where(Incident.id == existing_action.incident_id)
                )
                if existing_incident:
                    return existing_incident
        raise
    db.refresh(incident)
    return incident


def approve(
    db: Session,
    action: Action,
    approver: User,
    decision: ApprovalDecision,
    idempotency_key: str | None,
) -> Approval:
    if not can_approve(approver):
        audit(db, approver, "permission.denied", "action", action.id, {"operation": "approve"})
        db.commit()
        raise PermissionError("You do not have permission to approve this action")
    action = db.scalar(select(Action).where(Action.id == action.id).with_for_update()) or action
    existing = db.scalar(
        select(Approval).where(
            Approval.action_id == action.id,
            Approval.approver_id == approver.id,
        )
    )
    if existing is None and idempotency_key:
        existing = db.scalar(
            select(Approval).where(
                Approval.action_id == action.id, Approval.idempotency_key == idempotency_key
            )
        )
    if existing:
        return existing
    if action.status != ActionStatus.PENDING:
        audit(db, approver, "action.approval_failed", "action", action.id)
        db.commit()
        raise ValueError("Action has already reached a terminal status")
    if _expired(action.expires_at):
        action.status = ActionStatus.EXPIRED
        action.incident.status = IncidentStatus.CANCELLED
        audit(db, approver, "action.expired", "action", action.id)
        db.commit()
        raise ValueError("Action approval window has expired")
    approval = Approval(
        action_id=action.id,
        approver_id=approver.id,
        decision=decision,
        idempotency_key=idempotency_key,
    )
    db.add(approval)
    action.status = (
        ActionStatus.APPROVED if decision == ApprovalDecision.APPROVED else ActionStatus.REJECTED
    )
    action.approved_by = approver.id if decision == ApprovalDecision.APPROVED else None
    action.incident.status = (
        IncidentStatus.ACCEPTED
        if decision == ApprovalDecision.APPROVED
        else IncidentStatus.CANCELLED
    )
    audit(db, approver, f"action.{decision.value}", "action", action.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(Approval).where(
                Approval.action_id == action.id, Approval.approver_id == approver.id
            )
        )
        if existing:
            return existing
        raise
    db.refresh(approval)
    return approval


def execute(db: Session, action: Action, actor: User) -> Action:
    if not can_approve(actor):
        audit(db, actor, "permission.denied", "action", action.id, {"operation": "execute"})
        db.commit()
        raise PermissionError("You do not have permission to execute this action")
    action = db.scalar(select(Action).where(Action.id == action.id).with_for_update()) or action
    if action.status == ActionStatus.EXECUTED:
        return action
    if _expired(action.expires_at):
        action.status = ActionStatus.EXPIRED
        action.incident.status = IncidentStatus.CANCELLED
        audit(db, actor, "action.expired", "action", action.id)
        db.commit()
        return action
    if action.status != ActionStatus.APPROVED:
        audit(db, actor, "action.execution_failed", "action", action.id)
        db.commit()
        raise ValueError("Action requires an approval before execution")
    # This is the single local execution boundary. It is persisted atomically and
    # intentionally has no unapproved external side effect.
    action.status = ActionStatus.EXECUTED
    action.executed_at = datetime.now(UTC)
    action.result = {"status": "executed", "kind": action.kind}
    if all(item.status == ActionStatus.EXECUTED for item in action.incident.actions):
        action.incident.status = IncidentStatus.RESOLVED
    audit(db, actor, "action.executed", "action", action.id, action.result)
    db.commit()
    db.refresh(action)
    return action


class IncidentService:
    propose = staticmethod(propose)
    approve = staticmethod(approve)
    execute = staticmethod(execute)
    get_incident = staticmethod(get_incident)
