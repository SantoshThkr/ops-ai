"""Typed, allowlisted tools shared by the API, agent, and MCP adapter."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ServiceMetric, User
from app.schemas import MetricResponse, SearchResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    read_only: bool


TOOL_DEFINITIONS = (
    ToolDefinition("search_knowledge", "Search the user's uploaded documents.", True),
    ToolDefinition("get_metric", "Read an explicitly recorded service metric.", True),
    ToolDefinition("get_incident", "Read an authorized incident.", True),
    ToolDefinition("create_incident", "Create an incident proposal; approval is required.", False),
)
TOOLS = {definition.name: definition for definition in TOOL_DEFINITIONS}

# Metrics are data contracts, not values invented by the assistant.
ALLOWED_METRICS = frozenset(
    {
        "payment_failure_rate",
        "daily_error_rate",
        "transaction_count",
        "error_rate",
        "latency_p95",
        "queue_depth",
    }
)


def search_knowledge(
    db: Session,
    user: User,
    query: str,
    top_k: int | None = None,
) -> list[SearchResult]:
    from app.main import _search_owned_chunks

    settings = get_settings()
    requested = top_k or settings.retrieval_top_k
    started = time.monotonic()
    try:
        rows = _search_owned_chunks(db, user, query, max(1, min(requested, 50)))
        results = [row for row in rows if row.similarity >= settings.retrieval_similarity_threshold]
    except Exception:
        logger.exception(
            "tool_failed",
            extra={
                "operation": "tool_execution",
                "tool": "search_knowledge",
                "user_id": str(user.id),
                "status": "failed",
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            },
        )
        raise
    logger.info(
        "tool_completed",
        extra={
            "operation": "tool_execution",
            "tool": "search_knowledge",
            "user_id": str(user.id),
            "status": "completed",
            "result_count": len(results),
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
        },
    )
    return results


def get_metric(
    db: Session,
    service: str | None = None,
    name: str | None = None,
) -> list[MetricResponse]:
    logger.info(
        "tool_call",
        extra={
            "operation": "tool_execution",
            "tool": "get_metric",
            "status": "requested",
            "service": service,
            "metric": name,
        },
    )
    if name is not None and name not in ALLOWED_METRICS:
        logger.warning(
            "tool_failed",
            extra={
                "operation": "tool_execution",
                "tool": "get_metric",
                "status": "invalid_metric",
                "metric": name,
            },
        )
        return []
    statement = select(ServiceMetric).order_by(ServiceMetric.service, ServiceMetric.name).limit(100)
    if service:
        statement = statement.where(ServiceMetric.service == service)
    if name:
        statement = statement.where(ServiceMetric.name == name)
    return [MetricResponse.model_validate(row) for row in db.scalars(statement).all()]


def tool_catalog() -> list[dict[str, Any]]:
    return [
        {
            "name": definition.name,
            "description": definition.description,
            "read_only": definition.read_only,
        }
        for definition in TOOL_DEFINITIONS
    ]


# Kept as a typed alias for integrations that used the Day 5 name.
read_metrics = get_metric
