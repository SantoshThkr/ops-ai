from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import CurrentUser, create_access_token, hash_password, require_roles, verify_password
from app.config import get_settings
from app.db import check_database_connection, get_db
from app.models import User, UserRole
from app.schemas import AuthResponse, LoginRequest, UserCreate, UserResponse

settings = get_settings()
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
