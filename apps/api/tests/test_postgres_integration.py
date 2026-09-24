"""Postgres-only behavior: row locking under real concurrency and pgvector retrieval.

Skipped unless TEST_DATABASE_URL points at a pgvector-enabled database, for example:

    TEST_DATABASE_URL=postgresql+psycopg://opsai:<password>@localhost:5432/opsai pytest
"""

import os
import threading
from collections.abc import Callable, Iterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.incidents import approve, execute, propose
from app.models import Action, ActionStatus, ApprovalDecision, AuditLog, Base, User, UserRole
from app.retrieval import search_owned_chunks
from app.schemas import IncidentProposalCreate

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not set")

RUNBOOK = (
    "Incident response runbook. Restarts of the api service require an approved incident "
    "proposal. The VPN is configured with the corporate identity provider."
)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    assert DATABASE_URL is not None
    engine = create_engine(DATABASE_URL, pool_size=10)
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)  # no-op when migrations have already run
    yield engine
    engine.dispose()


@pytest.fixture()
def factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine)


def _user(db: Session, role: UserRole) -> User:
    user = User(email=f"{uuid4()}@example.com", password_hash="x", name="PG", role=role)
    db.add(user)
    db.commit()
    return user


def _approved_action(factory: sessionmaker[Session]) -> tuple[UUID, list[UUID]]:
    with factory() as db:
        analyst = _user(db, UserRole.ANALYST)
        admins = [_user(db, UserRole.ADMIN) for _ in range(2)]
        incident = propose(
            db,
            analyst,
            IncidentProposalCreate(
                title="Postgres race",
                summary="Concurrent requests",
                action_kind="restart_service",
                action_parameters={"service": "api"},
            ),
        )
        return incident.actions[0].id, [admin.id for admin in admins]


def _run_concurrently(workers: list[Callable[[threading.Barrier], None]]) -> list[BaseException]:
    """Run workers in threads; each calls barrier.wait() after loading its rows."""
    barrier = threading.Barrier(len(workers), timeout=30)
    errors: list[BaseException] = []

    def wrap(work: Callable[[threading.Barrier], None]) -> None:
        try:
            work(barrier)
        except BaseException as error:  # collected and asserted by the caller
            errors.append(error)

    threads = [threading.Thread(target=wrap, args=(work,)) for work in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return errors


def test_concurrent_execution_runs_exactly_once(factory: sessionmaker[Session]) -> None:
    action_id, (admin_id, _) = _approved_action(factory)
    with factory() as db:
        admin = db.get(User, admin_id)
        approve(db, db.get(Action, action_id), admin, ApprovalDecision.APPROVED, None)

    def request(barrier: threading.Barrier) -> None:
        with factory() as db:
            # The route loads the action before the service locks it.
            action = db.get(Action, action_id)
            user = db.get(User, admin_id)
            assert action is not None and action.status == ActionStatus.APPROVED
            barrier.wait()
            execute(db, action, user)

    assert _run_concurrently([request for _ in range(5)]) == []
    with factory() as db:
        executed = db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.event == "action.executed", AuditLog.resource_id == str(action_id))
        )
        assert executed == 1
        assert db.get(Action, action_id).status == ActionStatus.EXECUTED


def test_concurrent_conflicting_decisions_record_exactly_one(
    factory: sessionmaker[Session],
) -> None:
    action_id, admin_ids = _approved_action(factory)
    decisions = [ApprovalDecision.APPROVED, ApprovalDecision.REJECTED]

    def decide(admin_id: UUID, decision: ApprovalDecision) -> Callable[[threading.Barrier], None]:
        def work(barrier: threading.Barrier) -> None:
            with factory() as db:
                action = db.get(Action, action_id)
                user = db.get(User, admin_id)
                assert action is not None and action.status == ActionStatus.PENDING
                barrier.wait()
                approve(db, action, user, decision, None)

        return work

    errors = _run_concurrently(
        [decide(admin, decision) for admin, decision in zip(admin_ids, decisions, strict=True)]
    )
    assert len(errors) == 1 and isinstance(errors[0], ValueError)
    with factory() as db:
        action = db.get(Action, action_id)
        assert len(action.approvals) == 1
        expected = (
            ActionStatus.APPROVED
            if action.approvals[0].decision == ApprovalDecision.APPROVED
            else ActionStatus.REJECTED
        )
        assert action.status == expected


def test_pgvector_search_is_owner_scoped_and_thresholded(
    factory: sessionmaker[Session], add_document: Callable[..., UUID]
) -> None:
    with factory() as db:
        owner = _user(db, UserRole.VIEWER)
        stranger = _user(db, UserRole.VIEWER)
        document_id = add_document(db, owner, RUNBOOK)

        results = search_owned_chunks(db, owner, "How is the VPN configured?", 5)
        assert [result.document_id for result in results] == [document_id]
        assert 0.35 <= results[0].similarity <= 1.0
        assert search_owned_chunks(db, stranger, "How is the VPN configured?", 5) == []
        assert search_owned_chunks(db, owner, "What is the weather today?", 5) == []
