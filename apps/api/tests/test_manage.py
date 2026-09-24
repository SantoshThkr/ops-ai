from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.manage import DEMO_METRICS, main, seed_demo_metrics, set_role
from app.models import AuditLog, Base, ServiceMetric, User, UserRole
from app.tools import ALLOWED_METRICS


@pytest.fixture()
def factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr("app.manage.SessionLocal", session_factory)
    with session_factory() as db:
        db.add(User(email="ops@example.com", password_hash="x", name="Ops"))
        db.commit()
    yield session_factory


def test_set_role_is_audited(factory: sessionmaker[Session]) -> None:
    with factory() as db:
        assert set_role(db, "OPS@example.com", UserRole.ADMIN) == "ops@example.com: viewer -> admin"
        entry = db.scalars(select(AuditLog).where(AuditLog.event == "user.role_changed")).one()
        assert entry.actor_id is None
        assert entry.details == {"from": "viewer", "to": "admin", "source": "cli"}


def test_cli_reports_unknown_users_without_changing_anything(
    factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["set-role", "missing@example.com", "admin"]) == 1
    assert "No user registered" in capsys.readouterr().err
    assert main(["set-role", "ops@example.com", "analyst"]) == 0
    with factory() as db:
        assert db.scalar(select(User.role)) == UserRole.ANALYST


def test_demo_metrics_are_allowlisted_labelled_and_idempotent(
    factory: sessionmaker[Session],
) -> None:
    assert {name for _, name, _, _ in DEMO_METRICS} <= ALLOWED_METRICS
    with factory() as db:
        assert seed_demo_metrics(db) == len(DEMO_METRICS)
        assert seed_demo_metrics(db) == 0
        assert db.scalar(select(func.count()).select_from(ServiceMetric)) == len(DEMO_METRICS)
        sources = {row.metric_metadata["source"] for row in db.scalars(select(ServiceMetric))}
        assert sources == {"demo-seed"}
