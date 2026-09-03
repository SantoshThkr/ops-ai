from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import AuditLog, User, UserRole

password_hash = PasswordHash.recommended()
bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    return password_hash.verify(password, hashed_password)


def create_access_token(user: User) -> str:
    settings = get_settings()
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": str(user.id), "role": user.role.value, "type": "access", "exp": expires_at}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _token_from_request(
    request: Request, credentials: HTTPAuthorizationCredentials | None
) -> str | None:
    if credentials is not None:
        return credentials.credentials
    return request.cookies.get(get_settings().auth_cookie_name)


def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    token = _token_from_request(request, credentials)
    if not token:
        raise _unauthorized()
    try:
        payload: dict[str, Any] = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
        if payload.get("type") != "access":
            raise _unauthorized()
        user_id = UUID(str(payload["sub"]))
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError) as error:
        raise _unauthorized() from error

    user = db.scalar(select(User).where(User.id == user_id))
    if user is None or not user.active:
        raise _unauthorized()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: UserRole) -> Callable[..., User]:
    def dependency(
        user: CurrentUser,
        db: Annotated[Session, Depends(get_db)],
    ) -> User:
        if user.role not in roles:
            db.add(
                AuditLog(
                    actor_id=user.id,
                    event="permission.denied",
                    resource_type="route",
                    resource_id=None,
                    details={"required_roles": [role.value for role in roles]},
                )
            )
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access this resource",
            )
        return user

    return dependency
