import logging
import math
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, HTTPException, Query, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import bindparam, func, literal_column, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import (
    CurrentUser,
    create_access_token,
    get_current_user,
    hash_password,
    require_roles,
    verify_password,
)
from app.config import get_settings
from app.db import check_database_connection, get_db
from app.ingestion import embedding_provider, enqueue, storage, validate_upload
from app.models import Document, DocumentChunk, DocumentStatus, User, UserRole
from app.schemas import (
    AuthResponse,
    DocumentListResponse,
    DocumentResponse,
    LoginRequest,
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


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        max_age=settings.jwt_expire_minutes * 60,
    )


@app.get("/health")
def health() -> dict[str, str]:
    try:
        check_database_connection()
    except SQLAlchemyError as error:
        raise HTTPException(status_code=503, detail="Database unavailable") from error
    return {"status": "ok"}


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
    user = db.scalar(select(User).where(User.email == email))
    if (
        user is None
        or not verify_password(credentials.password, user.password_hash)
        or not user.active
    ):
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


@app.post("/documents", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),  # noqa: B008
    user: User = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> Document:
    data = await file.read(settings.max_file_size + 1)
    try:
        suffix = validate_upload(file, data, settings)
    except OverflowError as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    except ValueError as error:
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
        logger.exception("Could not persist document metadata")
        raise HTTPException(status_code=500, detail="Could not save document") from error
    enqueue(document.id, settings)
    logger.info("Accepted document %s for user %s (%s)", document.id, user.id, suffix)
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
    query_vector = embedding_provider(settings).embed([request.query])[0]
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
        return [
            SearchResult(
                document_id=chunk.document_id,
                filename=filename,
                content=chunk.content,
                page_number=chunk.page_number,
                similarity=round(float(score), 6),
            )
            for chunk, filename, score in vector_rows
        ]
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
            filename=filename,
            content=chunk.content,
            page_number=chunk.page_number,
            similarity=round(score, 6),
        )
        for score, chunk, filename in scored[:top_k]
    ]
