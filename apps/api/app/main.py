import json
import logging
import math
import time
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import bindparam, func, literal_column, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from app.agent import IntentKind, parse_intent, should_handle
from app.agent import stream as stream_agent
from app.auth import (
    CurrentUser,
    create_access_token,
    get_current_user,
    hash_password,
    require_roles,
    verify_password,
)
from app.chat import canonicalize_sources, get_chat_provider
from app.config import get_settings
from app.db import check_database_connection, get_db
from app.incidents import approve as approve_action
from app.incidents import audit, can_manage, propose
from app.incidents import execute as execute_action
from app.ingestion import embedding_provider, enqueue, storage, validate_upload
from app.limits import check_rate_limit
from app.mcp import handle_request as handle_mcp_request
from app.models import (
    Action,
    Approval,
    AuditLog,
    Conversation,
    Document,
    DocumentChunk,
    DocumentStatus,
    Incident,
    Message,
    User,
    UserRole,
)
from app.observability import request_id_context
from app.schemas import (
    ActionResponse,
    ApprovalCreate,
    ApprovalResponse,
    AuditLogResponse,
    AuthResponse,
    CitationResponse,
    ConversationCreate,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationResponse,
    DocumentListResponse,
    DocumentResponse,
    IncidentProposalCreate,
    IncidentResponse,
    LoginRequest,
    MessageCreate,
    MessageResponse,
    MetricResponse,
    MetricsQuery,
    SearchRequest,
    SearchResult,
    UserCreate,
    UserResponse,
)

settings = get_settings()
logger = logging.getLogger(__name__)
app = FastAPI(title="OpsAI API", version=settings.api_version)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logging(request: Request, call_next: Any) -> Response:
    started = time.monotonic()
    request_id = request.headers.get("x-request-id", str(uuid4()))
    token = request_id_context.set(request_id)
    try:
        response = cast(Response, await call_next(request))
        response.headers["x-request-id"] = request_id
        logger.info(
            "request_complete",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            },
        )
        return response
    finally:
        request_id_context.reset(token)


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        max_age=settings.jwt_expire_minutes * 60,
    )


def _conversation_title_from_message(message: str) -> str:
    cleaned = " ".join(message.strip().split())
    if not cleaned:
        return "New conversation"
    if len(cleaned) <= 40:
        return cleaned
    return f"{cleaned[:37]}..."


def _event(name: str, payload: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, default=str)}\n\n"


def _citation_payload(chunks: list[SearchResult]) -> list[CitationResponse]:
    unique_chunks = canonicalize_sources(chunks)
    return [
        CitationResponse(
            document_id=chunk.document_id,
            filename=chunk.filename,
            chunk_id=chunk.chunk_id,
            page_number=chunk.page_number,
        )
        for chunk in unique_chunks
    ]


@app.get("/health")
def health() -> dict[str, str]:
    try:
        check_database_connection()
    except SQLAlchemyError as error:
        raise HTTPException(status_code=503, detail="Database unavailable") from error
    return {"status": "ok"}


@app.get("/ready")
def readiness() -> dict[str, str]:
    try:
        check_database_connection()
        Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
        ).ping()
    except (SQLAlchemyError, RedisError, OSError, TimeoutError) as error:
        raise HTTPException(status_code=503, detail="Dependencies unavailable") from error
    return {"status": "ready"}


@app.get("/version")
def version() -> dict[str, str]:
    return {"version": settings.api_version}


@app.post("/auth/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def register(
    user_data: UserCreate,
    response: Response,
    db: Session = Depends(get_db),  # noqa: B008
) -> AuthResponse:
    email = str(user_data.email).lower()
    if db.scalar(select(User).where(User.email == email)) is not None:
        audit(db, None, "auth.register_failed", "user", None, {"reason": "duplicate"})
        db.commit()
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    user = User(
        email=email,
        password_hash=hash_password(user_data.password),
        name=user_data.name,
        role=UserRole.VIEWER,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        audit(db, None, "auth.register_failed", "user", None, {"reason": "duplicate"})
        db.commit()
        raise HTTPException(
            status_code=409, detail="An account with this email already exists"
        ) from error
    db.refresh(user)
    token = create_access_token(user)
    _set_auth_cookie(response, token)
    return AuthResponse(user=UserResponse.model_validate(user))


@app.post("/auth/login", response_model=AuthResponse)
def login(
    credentials: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),  # noqa: B008
) -> AuthResponse:
    email = str(credentials.email).lower()
    if not check_rate_limit("login", email, 10, 60):
        audit(db, None, "rate_limit.denied", "auth", None, {"scope": "login"})
        db.commit()
        raise HTTPException(status_code=429, detail="Too many login attempts")
    user = db.scalar(select(User).where(User.email == email))
    password_valid = False
    if user is not None:
        try:
            password_valid = verify_password(credentials.password, user.password_hash)
        except Exception:
            logger.warning("auth_password_verification_failed")
    if user is None or not password_valid or not user.active:
        audit(db, user, "auth.login_failed", "user", user.id if user else None)
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_access_token(user)
    _set_auth_cookie(response, token)
    return AuthResponse(user=UserResponse.model_validate(user))


@app.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    response.delete_cookie(key=settings.auth_cookie_name)


@app.get("/me", response_model=UserResponse)
def me(user: CurrentUser) -> User:
    return user


@app.get("/rbac/viewer")
def viewer_probe(user: CurrentUser) -> dict[str, str]:
    return {"message": "Authenticated users can view this resource", "role": user.role.value}


@app.get("/rbac/analyst")
def analyst_probe(
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ANALYST)),  # noqa: B008
) -> dict[str, str]:
    return {"message": "Analyst access granted", "role": user.role.value}


@app.get("/rbac/admin")
def admin_probe(
    user: User = Depends(require_roles(UserRole.ADMIN)),  # noqa: B008
) -> dict[str, str]:
    return {"message": "Administrator access granted", "role": user.role.value}


@app.post(
    "/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_conversation(
    payload: ConversationCreate,
    user: CurrentUser,
    db: Session = Depends(get_db),  # noqa: B008
) -> Conversation:
    conversation = Conversation(
        user_id=user.id,
        title=payload.title or "New conversation",
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


@app.get("/conversations", response_model=ConversationListResponse)
def list_conversations(
    user: CurrentUser,
    db: Session = Depends(get_db),  # noqa: B008
    page: int = Query(default=1, ge=1),  # noqa: B008
    page_size: int = Query(default=20, ge=1, le=50),  # noqa: B008
) -> ConversationListResponse:
    total = (
        db.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.user_id == user.id)
        )
        or 0
    )
    items = db.scalars(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return ConversationListResponse(items=items, page=page, page_size=page_size, total=total)


@app.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
def get_conversation(
    conversation_id: UUID,
    user: CurrentUser,
    db: Session = Depends(get_db),  # noqa: B008
) -> ConversationDetailResponse:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc(), Message.id.asc())
        .limit(200)
    ).all()
    return ConversationDetailResponse(
        id=conversation.id,
        user_id=conversation.user_id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        messages=[
            MessageResponse(
                id=message.id,
                conversation_id=message.conversation_id,
                role=message.role,
                content=message.content,
                created_at=message.created_at,
            )
            for message in messages
        ],
    )


@app.post("/documents", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),  # noqa: B008
    user: User = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> Document:
    if not check_rate_limit("upload", str(user.id), 20, 60):
        audit(db, user, "rate_limit.denied", "user", user.id, {"scope": "upload"})
        db.commit()
        raise HTTPException(status_code=429, detail="Upload rate limit exceeded")
    data = await file.read(settings.max_file_size + 1)
    try:
        suffix = validate_upload(file, data, settings)
    except OverflowError as error:
        audit(db, user, "document.upload_failed", "document", None, {"reason": "too_large"})
        db.commit()
        raise HTTPException(status_code=413, detail=str(error)) from error
    except ValueError as error:
        audit(db, user, "document.upload_failed", "document", None, {"reason": "invalid"})
        db.commit()
        raise HTTPException(status_code=400, detail=str(error)) from error

    document_id = uuid4()
    document = Document(
        id=document_id,
        owner_id=user.id,
        filename=Path(file.filename or "").name,
        content_type=file.content_type or "",
        file_size=len(data),
        checksum=sha256(data).hexdigest(),
        storage_key=f"{user.id}/{document_id}",
        status=DocumentStatus.UPLOADED,
    )
    file_storage = storage(settings)
    try:
        file_storage.save(document.storage_key, data)
        db.add(document)
        db.commit()
        db.refresh(document)
    except Exception as error:
        db.rollback()
        file_storage.delete(document.storage_key)
        audit(db, user, "document.upload_failed", "document", document_id, {"reason": "persist"})
        db.commit()
        logger.exception("Could not persist document metadata")
        raise HTTPException(status_code=500, detail="Could not save document") from error
    enqueue(document.id, settings)
    logger.info(
        "document_accepted",
        extra={"document_id": str(document.id), "user_id": str(user.id), "suffix": suffix},
    )
    return document


@app.get("/documents", response_model=DocumentListResponse)
def list_documents(
    user: CurrentUser,
    db: Session = Depends(get_db),  # noqa: B008
    page: int = Query(default=1, ge=1),  # noqa: B008
    page_size: int = Query(default=20, ge=1, le=100),  # noqa: B008
) -> DocumentListResponse:
    owned = Document.owner_id == user.id
    total = db.scalar(select(func.count()).select_from(Document).where(owned)) or 0
    items = db.scalars(
        select(Document)
        .where(owned)
        .order_by(Document.created_at.desc(), Document.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return DocumentListResponse(items=items, page=page, page_size=page_size, total=total)


@app.get("/documents/{document_id}", response_model=DocumentResponse)
def get_document(
    document_id: UUID,
    user: CurrentUser,
    db: Session = Depends(get_db),  # noqa: B008
) -> Document:
    document = db.scalar(
        select(Document).where(Document.id == document_id, Document.owner_id == user.id)
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return document


def _search_owned_chunks(db: Session, user: User, query: str, top_k: int) -> list[SearchResult]:
    started = time.monotonic()
    query_vector = embedding_provider(settings).embed([query])[0]
    base_query = (
        select(DocumentChunk, Document.filename)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.owner_id == user.id, Document.status == DocumentStatus.COMPLETED)
    )
    dialect_name = db.bind.dialect.name if db.bind is not None else "sqlite"
    if dialect_name == "postgresql":
        from pgvector.sqlalchemy import Vector

        query_param = bindparam(
            "query_embedding", value=query_vector, type_=Vector(settings.embedding_dimension)
        )
        distance = DocumentChunk.embedding.op("<=>")(query_param)
        similarity: Any = (literal_column("1.0") - distance).label("similarity")
        vector_rows = db.execute(
            select(DocumentChunk, Document.filename, similarity)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(Document.owner_id == user.id, Document.status == DocumentStatus.COMPLETED)
            .order_by(similarity.desc(), DocumentChunk.id.asc())
            .limit(top_k)
        ).all()
        results = [
            SearchResult(
                document_id=chunk.document_id,
                chunk_id=chunk.id,
                filename=filename,
                content=chunk.content,
                page_number=chunk.page_number,
                similarity=round(float(score), 6),
            )
            for chunk, filename, score in vector_rows
            if float(score) >= settings.retrieval_similarity_threshold
        ]
        logger.info(
            "rag_retrieval_completed",
            extra={
                "operation": "rag_retrieval",
                "user_id": str(user.id),
                "status": "completed",
                "result_count": len(results),
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            },
        )
        return results
    rows = db.execute(base_query).all()
    scored = [
        (
            _cosine(query_vector, list(chunk.embedding)),
            chunk,
            filename,
        )
        for chunk, filename in rows
    ]
    scored.sort(key=lambda item: (-item[0], str(item[1].id)))
    return [
        SearchResult(
            document_id=chunk.document_id,
            chunk_id=chunk.id,
            filename=filename,
            content=chunk.content,
            page_number=chunk.page_number,
            similarity=round(score, 6),
        )
        for score, chunk, filename in scored[:top_k]
        if score >= settings.retrieval_similarity_threshold
    ]


def _cosine(left: list[float], right: list[float]) -> float:
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
        sum(value * value for value in right)
    )
    return (
        sum(a * b for a, b in zip(left, right, strict=False)) / denominator if denominator else 0.0
    )


@app.post("/documents/search", response_model=list[SearchResult])
def search_documents(
    request: SearchRequest,
    user: CurrentUser,
    db: Session = Depends(get_db),  # noqa: B008
) -> list[SearchResult]:
    top_k = request.top_k or settings.retrieval_top_k
    return _search_owned_chunks(db, user, request.query, top_k)


@app.post("/metrics/query", response_model=list[MetricResponse])
def query_metrics(
    request: MetricsQuery,
    user: CurrentUser,
    db: Session = Depends(get_db),  # noqa: B008
) -> list[MetricResponse]:
    del user
    from app.tools import get_metric

    return get_metric(db, request.service, request.name)


@app.get("/metrics", response_model=list[MetricResponse])
def list_metrics(
    user: CurrentUser,
    db: Session = Depends(get_db),  # noqa: B008
    service: str | None = Query(default=None, max_length=120),  # noqa: B008
    name: str | None = Query(default=None, max_length=120),  # noqa: B008
) -> list[MetricResponse]:
    del user
    from app.tools import get_metric

    return get_metric(db, service, name)


@app.post(
    "/incidents/proposals", response_model=IncidentResponse, status_code=status.HTTP_201_CREATED
)
@app.post(
    "/incidents/propose",
    response_model=IncidentResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
def create_incident_proposal(
    payload: IncidentProposalCreate,
    user: User = Depends(require_roles(UserRole.ADMIN)),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> Incident:
    if (
        payload.conversation_id is not None
        and db.scalar(
            select(Conversation).where(
                Conversation.id == payload.conversation_id, Conversation.user_id == user.id
            )
        )
        is None
    ):
        audit(
            db,
            user,
            "permission.denied",
            "conversation",
            payload.conversation_id,
            {"operation": "incident.propose"},
        )
        db.commit()
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        return propose(db, user, payload)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


@app.post("/incidents", response_model=IncidentResponse, status_code=status.HTTP_201_CREATED)
def create_incident(
    payload: IncidentProposalCreate,
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ANALYST)),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> Incident:
    return create_incident_proposal(payload, user, db)


@app.get("/incidents", response_model=list[IncidentResponse])
def list_incidents(
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ANALYST)),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> list[Incident]:
    statement = select(Incident)
    if user.role != UserRole.ADMIN:
        statement = statement.where(Incident.owner_id == user.id)
    return list(
        db.scalars(statement.order_by(Incident.created_at.desc()).limit(100)).unique().all()
    )


@app.get("/incidents/{incident_id}", response_model=IncidentResponse)
def get_incident(
    incident_id: UUID,
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ANALYST)),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> Incident:
    incident = db.scalar(select(Incident).where(Incident.id == incident_id))
    if incident is None or not can_manage(user, incident.owner_id):
        audit(db, user, "permission.denied", "incident", incident_id, {"operation": "read"})
        db.commit()
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


@app.get("/actions/{action_id}", response_model=ActionResponse)
def get_action(
    action_id: UUID,
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ANALYST)),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> Action:
    action = db.scalar(select(Action).where(Action.id == action_id))
    if action is None or not can_manage(user, action.owner_id):
        audit(db, user, "permission.denied", "action", action_id, {"operation": "read"})
        db.commit()
        raise HTTPException(status_code=404, detail="Action not found")
    return action


def _require_action(action_id: UUID, user: User, db: Session) -> Action:
    action = db.scalar(select(Action).where(Action.id == action_id))
    if action is None or not can_manage(user, action.owner_id):
        audit(db, user, "permission.denied", "action", action_id, {"operation": "access"})
        db.commit()
        raise HTTPException(status_code=404, detail="Action not found")
    return action


@app.post("/actions/{action_id}/approve", response_model=ApprovalResponse)
@app.post("/actions/{action_id}/approval", response_model=ApprovalResponse, include_in_schema=False)
@app.post("/approvals/{action_id}", response_model=ApprovalResponse, include_in_schema=False)
def approve(
    action_id: UUID,
    payload: ApprovalCreate,
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ANALYST)),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),  # noqa: B008
) -> Approval:
    if not check_rate_limit("approval", str(user.id), 30, 60):
        audit(db, user, "rate_limit.denied", "user", user.id, {"scope": "approval"})
        db.commit()
        raise HTTPException(status_code=429, detail="Approval rate limit exceeded")
    action = _require_action(action_id, user, db)
    assert payload.decision is not None
    try:
        return approve_action(db, action, user, payload.decision, idempotency_key)
    except (PermissionError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/actions/{action_id}/approvals", response_model=list[ApprovalResponse])
def list_approvals(
    action_id: UUID,
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ANALYST)),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> list[Approval]:
    action = _require_action(action_id, user, db)
    return list(
        db.scalars(
            select(Approval)
            .where(Approval.action_id == action.id)
            .order_by(Approval.created_at.asc())
            .limit(50)
        ).all()
    )


@app.post("/actions/{action_id}/execute", response_model=ActionResponse)
@app.post("/actions/{action_id}/run", response_model=ActionResponse, include_in_schema=False)
def execute(
    action_id: UUID,
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ANALYST)),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),  # noqa: B008
) -> Action:
    del idempotency_key  # execution is idempotent by persisted status
    action = _require_action(action_id, user, db)
    try:
        return execute_action(db, action, user)
    except (PermissionError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/audit-logs", response_model=list[AuditLogResponse])
def list_audit_logs(
    user: User = Depends(require_roles(UserRole.ADMIN, UserRole.ANALYST)),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> list[AuditLog]:
    return list(db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(200)).all())


@app.post("/mcp")
def mcp(
    request: dict[str, Any],
    user: CurrentUser,
    db: Session = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    return handle_mcp_request(request, db, user)


@app.get("/agent/tools")
def agent_tools(user: CurrentUser) -> list[dict[str, Any]]:
    del user
    from app.tools import tool_catalog

    return tool_catalog()


@app.post("/conversations/{conversation_id}/messages")
def send_message(
    conversation_id: UUID,
    payload: MessageCreate,
    user: CurrentUser,
    db: Session = Depends(get_db),  # noqa: B008
) -> StreamingResponse:
    if not check_rate_limit("chat", str(user.id), 30, 60):
        audit(db, user, "rate_limit.denied", "user", user.id, {"scope": "chat"})
        db.commit()
        raise HTTPException(status_code=429, detail="Chat rate limit exceeded")
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    user_message = Message(
        conversation_id=conversation.id,
        role="user",
        content=payload.content.strip(),
    )
    db.add(user_message)
    db.commit()
    db.refresh(user_message)
    if conversation.title == "New conversation":
        conversation.title = _conversation_title_from_message(payload.content)
    conversation.updated_at = func.now()
    db.commit()

    recent_history = list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(10)
        ).all()
    )
    recent_history.reverse()

    retrieved_chunks = _search_owned_chunks(db, user, payload.content, settings.retrieval_top_k)
    max_similarity = max((chunk.similarity for chunk in retrieved_chunks), default=0.0)
    grounded_chunks = (
        [] if max_similarity < settings.retrieval_similarity_threshold else retrieved_chunks
    )
    citations = _citation_payload(grounded_chunks)
    provider = get_chat_provider(settings)

    def stream_response() -> Any:
        collected: list[str] = []
        try:
            intent = parse_intent(payload.content)
            if should_handle(payload.content):
                for event_name, event_payload in stream_agent(
                    payload.content, db, user, conversation_id=conversation.id
                ):
                    if event_name == "token":
                        collected.append(str(event_payload.get("text", "")))
                    yield _event(event_name, event_payload)
                assistant_message = Message(
                    conversation_id=conversation.id,
                    role="assistant",
                    content="".join(collected).strip(),
                )
                db.add(assistant_message)
                db.commit()
                return
            if intent.kind == IntentKind.NONE:
                response_text = "Hi! Ask me about your uploaded documents or service metrics."
                for token in response_text.split():
                    collected.append(f"{token} ")
                    yield _event("token", {"text": f"{token} "})
                assistant_message = Message(
                    conversation_id=conversation.id,
                    role="assistant",
                    content=response_text,
                )
                db.add(assistant_message)
                db.commit()
                yield _event("done", {"status": "completed"})
                return
            yield _event(
                "tool_call",
                {"name": "search_knowledge", "arguments": {"query": payload.content}},
            )
            yield _event(
                "tool_result",
                {
                    "name": "search_knowledge",
                    "result": {"count": len(retrieved_chunks)},
                    "read_only": True,
                },
            )
            if not retrieved_chunks or max_similarity < settings.retrieval_similarity_threshold:
                response_text = (
                    "I couldn't find enough information in your uploaded documents to answer that."
                )
                for token in response_text.split():
                    collected.append(f"{token} ")
                    yield _event("token", {"text": f"{token} "})
                assistant_message = Message(
                    conversation_id=conversation.id,
                    role="assistant",
                    content=response_text,
                )
                db.add(assistant_message)
                db.commit()
                yield _event("done", {"status": "completed"})
                return

            history_lines = [
                f"{message.role}: {message.content}" for message in recent_history[-4:]
            ]
            for token in provider.stream(payload.content, grounded_chunks, history_lines):
                collected.append(token)
                yield _event("token", {"text": token})
            assistant_message = Message(
                conversation_id=conversation.id,
                role="assistant",
                content="".join(collected).strip(),
            )
            db.add(assistant_message)
            db.commit()
            citations_payload = [item.model_dump(mode="json") for item in citations]
            yield _event("citation", {"citations": citations_payload})
            yield _event("done", {"status": "completed", "citations": citations_payload})
        except Exception as error:  # pragma: no cover - defensive path
            audit(db, user, "chat.failed", "conversation", conversation.id)
            db.commit()
            logger.exception("Chat generation failed for conversation %s", conversation.id)
            yield _event("error", {"message": "I couldn't complete that response right now."})
            raise HTTPException(status_code=500, detail="Chat generation failed") from error

    return StreamingResponse(stream_response(), media_type="text/event-stream")
