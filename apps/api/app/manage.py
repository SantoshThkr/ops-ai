"""Operator commands for local and demo environments.

    python -m app.manage set-role user@example.com analyst
    python -m app.manage seed-demo-metrics

Role changes are deliberately not exposed over HTTP: they require shell access to the
API environment and are written to the audit log.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.incidents import audit
from app.models import ServiceMetric, User, UserRole

DEMO_SOURCE = "demo-seed"
# Illustrative values so metric questions have something to read in a fresh stack.
DEMO_METRICS: tuple[tuple[str, str, float, str], ...] = (
    ("api", "latency_p95", 240.0, "ms"),
    ("api", "error_rate", 0.012, "ratio"),
    ("api", "daily_error_rate", 0.009, "ratio"),
    ("worker", "queue_depth", 3.0, "jobs"),
    ("payments", "payment_failure_rate", 0.004, "ratio"),
    ("payments", "transaction_count", 18234.0, "count"),
)


def set_role(db: Session, email: str, role: UserRole) -> str:
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None:
        raise LookupError(f"No user registered with email {email!r}")
    previous = user.role
    user.role = role
    audit(
        db,
        None,
        "user.role_changed",
        "user",
        user.id,
        {"from": previous.value, "to": role.value, "source": "cli"},
    )
    db.commit()
    return f"{user.email}: {previous.value} -> {role.value}"


def seed_demo_metrics(db: Session) -> int:
    """Insert demo metrics that are missing; existing rows are left untouched."""
    existing = {
        (service, name)
        for service, name in db.execute(select(ServiceMetric.service, ServiceMetric.name))
    }
    created = 0
    for service, name, value, unit in DEMO_METRICS:
        if (service, name) in existing:
            continue
        db.add(
            ServiceMetric(
                service=service,
                name=name,
                value=value,
                unit=unit,
                metric_metadata={
                    "source": DEMO_SOURCE,
                    "note": "Illustrative demo value, not live telemetry.",
                },
            )
        )
        created += 1
    db.commit()
    return created


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.manage")
    commands = parser.add_subparsers(dest="command", required=True)
    role_parser = commands.add_parser("set-role", help="change an existing user's role")
    role_parser.add_argument("email")
    role_parser.add_argument("role", choices=[role.value for role in UserRole])
    commands.add_parser("seed-demo-metrics", help="insert labelled demo metric values")
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        if args.command == "set-role":
            try:
                print(set_role(db, args.email, UserRole(args.role)))
            except LookupError as error:
                print(error, file=sys.stderr)
                return 1
        else:
            print(f"Inserted {seed_demo_metrics(db)} demo metric(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
