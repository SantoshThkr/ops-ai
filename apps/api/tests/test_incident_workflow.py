"""Approval and execution invariants that must hold across sessions (concurrent requests)."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.incidents import approve, execute, propose
from app.models import Action, ActionStatus, ApprovalDecision, AuditLog, Base, User, UserRole
from app.schemas import IncidentProposalCreate


@pytest.fixture()
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    # A file database gives each session its own connection, like separate API requests.
    engine = create_engine(f"sqlite:///{tmp_path / 'workflow.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def _setup(factory: sessionmaker[Session]) -> tuple[UUID, UUID, UUID]:
    with factory() as db:
        analyst = User(email="a@example.com", password_hash="x", name="A", role=UserRole.ANALYST)
        admin = User(email="b@example.com", password_hash="x", name="B", role=UserRole.ADMIN)
        second_admin = User(email="c@example.com", password_hash="x", name="C", role=UserRole.ADMIN)
        db.add_all([analyst, admin, second_admin])
        db.commit()
        incident = propose(
            db,
            analyst,
            IncidentProposalCreate(
                title="API errors",
                summary="Restart after approval",
                action_kind="restart_service",
                action_parameters={"service": "api"},
            ),
        )
        return incident.actions[0].id, admin.id, second_admin.id


def _audit_count(factory: sessionmaker[Session], event: str, action_id: UUID) -> int:
    with factory() as db:
        return (
            db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.event == event, AuditLog.resource_id == str(action_id))
            )
            or 0
        )


def test_execution_rereads_state_so_a_stale_request_cannot_execute_twice(
    sessions: sessionmaker[Session],
) -> None:
    action_id, admin_id, _ = _setup(sessions)
    with sessions() as db:
        approve(
            db, db.get(Action, action_id), db.get(User, admin_id), ApprovalDecision.APPROVED, "k"
        )

    first, second = sessions(), sessions()
    try:
        # Both requests load the approved action before either executes it.
        stale_first = first.get(Action, action_id)
        stale_second = second.get(Action, action_id)
        assert stale_first is not None and stale_second is not None
        assert stale_second.status == ActionStatus.APPROVED

        execute(first, stale_first, first.get(User, admin_id))
        result = execute(second, stale_second, second.get(User, admin_id))
    finally:
        first.close()
        second.close()

    assert result.status == ActionStatus.EXECUTED
    assert _audit_count(sessions, "action.executed", action_id) == 1


def test_a_stale_second_approver_cannot_flip_a_decided_action(
    sessions: sessionmaker[Session],
) -> None:
    action_id, admin_id, second_admin_id = _setup(sessions)
    first, second = sessions(), sessions()
    try:
        stale_first = first.get(Action, action_id)
        stale_second = second.get(Action, action_id)
        assert stale_first is not None and stale_second is not None

        approve(first, stale_first, first.get(User, admin_id), ApprovalDecision.APPROVED, "a")
        with pytest.raises(ValueError, match="terminal"):
            approve(
                second,
                stale_second,
                second.get(User, second_admin_id),
                ApprovalDecision.REJECTED,
                "b",
            )
    finally:
        first.close()
        second.close()

    with sessions() as db:
        action = db.get(Action, action_id)
        assert action is not None
        assert action.status == ActionStatus.APPROVED
        assert len(action.approvals) == 1


def test_rejected_action_keeps_its_status_after_expiry(sessions: sessionmaker[Session]) -> None:
    action_id, admin_id, _ = _setup(sessions)
    with sessions() as db:
        action = db.get(Action, action_id)
        admin = db.get(User, admin_id)
        assert action is not None and admin is not None
        approve(db, action, admin, ApprovalDecision.REJECTED, "reject")
        action.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()

        with pytest.raises(ValueError, match="rejected"):
            execute(db, action, admin)
        db.refresh(action)
        assert action.status == ActionStatus.REJECTED
        assert action.executed_at is None


def test_expired_approval_is_reported_as_a_failure_not_a_success(
    sessions: sessionmaker[Session],
) -> None:
    action_id, admin_id, _ = _setup(sessions)
    with sessions() as db:
        action = db.get(Action, action_id)
        admin = db.get(User, admin_id)
        assert action is not None and admin is not None
        approve(db, action, admin, ApprovalDecision.APPROVED, "approve")
        action.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()

        with pytest.raises(ValueError, match="expired"):
            execute(db, action, admin)
        db.refresh(action)
        assert action.status == ActionStatus.EXPIRED
        assert action.executed_at is None
    assert _audit_count(sessions, "action.executed", action_id) == 0


def test_pending_action_cannot_be_executed(sessions: sessionmaker[Session]) -> None:
    action_id, admin_id, _ = _setup(sessions)
    with sessions() as db:
        action = db.get(Action, action_id)
        admin = db.get(User, admin_id)
        assert action is not None and admin is not None
        with pytest.raises(ValueError, match="pending"):
            execute(db, action, admin)
        assert action.status == ActionStatus.PENDING
