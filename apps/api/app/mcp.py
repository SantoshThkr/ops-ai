"""Authenticated JSON-RPC adapter over the same API services."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.incidents import audit, get_incident, propose
from app.models import User, UserRole
from app.schemas import IncidentProposalCreate
from app.tools import get_metric, search_knowledge, tool_catalog


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _failed(
    request_id: Any,
    db: Session,
    user: User,
    message: str,
    *,
    code: int = -32602,
    tool: str | None = None,
) -> dict[str, Any]:
    audit(
        db,
        user,
        "mcp.request_failed",
        "tool",
        tool[:120] if tool else None,
        {"message": message},
    )
    db.commit()
    return _error(request_id, code, message)


def handle_request(request: dict[str, Any], db: Session, user: User) -> dict[str, Any]:
    request_id = request.get("id")
    if request.get("jsonrpc") != "2.0":
        return _failed(request_id, db, user, "Invalid JSON-RPC request", code=-32600)
    method = request.get("method")
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tool_catalog()}}
    if method != "tools/call":
        return _failed(request_id, db, user, "Method not found", code=-32601)

    params = request.get("params")
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return _failed(request_id, db, user, "Tool name is required")
    name = params["name"]
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _failed(request_id, db, user, "Tool arguments must be an object", tool=name)

    result: dict[str, Any]
    try:
        if name == "search_knowledge":
            query = str(arguments.get("query", "")).strip()
            if not query:
                return _failed(request_id, db, user, "query is required", tool=name)
            top_k = int(arguments.get("top_k", 5))
            if not 1 <= top_k <= 50:
                return _failed(request_id, db, user, "top_k must be between 1 and 50", tool=name)
            result = {
                "results": [
                    item.model_dump(mode="json")
                    for item in search_knowledge(db, user, query, top_k)
                ]
            }
        elif name == "get_metric":
            metrics = get_metric(db, arguments.get("service"), arguments.get("name"))
            result = {
                "metrics": [item.model_dump(mode="json") for item in metrics],
                "metric": metrics[0].model_dump(mode="json") if metrics else None,
            }
        elif name == "get_incident":
            incident_id = UUID(str(arguments.get("incident_id")))
            incident = get_incident(db, user, incident_id)
            if incident is None:
                audit(db, user, "permission.denied", "incident", incident_id, {"operation": "read"})
                db.commit()
                return _error(request_id, -32003, "Incident not found")
            result = {
                "incident": {
                    "id": str(incident.id),
                    "owner_id": str(incident.owner_id),
                    "title": incident.title,
                    "summary": incident.summary,
                    "severity": incident.severity,
                    "status": incident.status.value,
                    "actions": [
                        {
                            "id": str(action.id),
                            "kind": action.kind,
                            "status": action.status.value,
                            "expires_at": action.expires_at.isoformat(),
                        }
                        for action in incident.actions
                    ],
                }
            }
        elif name == "create_incident":
            if user.role not in {UserRole.ADMIN, UserRole.ANALYST}:
                audit(db, user, "permission.denied", "incident", None, {"operation": "propose"})
                db.commit()
                return _error(
                    request_id,
                    -32003,
                    "Only analysts and administrators may propose incidents",
                )
            incident = propose(db, user, IncidentProposalCreate.model_validate(arguments))
            result = {
                "incident_id": str(incident.id),
                "proposal_only": True,
                "approval_required": True,
            }
        else:
            return _failed(request_id, db, user, "Unknown tool", tool=name)
    except (TypeError, ValueError):
        audit(db, user, "mcp.tool_failed", "tool", name)
        db.commit()
        return _error(request_id, -32602, "Invalid tool arguments")
    except Exception:
        db.rollback()
        audit(db, user, "mcp.tool_failed", "tool", name)
        db.commit()
        return _error(request_id, -32000, "Tool execution failed")
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "json", "json": result}]},
    }
