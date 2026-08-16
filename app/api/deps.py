"""Request-scoped dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import TokenClaims, decode_access_token
from app.infrastructure.database import get_db_session
from app.infrastructure.models import UserModel

bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: DbSession,
) -> UserModel:
    """Resolve the caller from a bearer token, or reject the request.

    The user row is re-read on every request so a deleted or moved user cannot
    keep acting on a token that has not expired yet.
    """
    if credentials is None:
        raise _unauthorized()

    try:
        claims: TokenClaims = decode_access_token(credentials.credentials)
    except JWTError as error:
        raise _unauthorized() from error

    user = await session.scalar(
        select(UserModel).where(
            UserModel.id == claims.user_id, UserModel.org_id == claims.org_id
        )
    )
    if user is None:
        raise _unauthorized()
    return user


CurrentUser = Annotated[UserModel, Depends(get_current_user)]


async def require_admin(user: CurrentUser) -> UserModel:
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires an administrator account",
        )
    return user


AdminUser = Annotated[UserModel, Depends(require_admin)]


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing authentication token",
        headers={"WWW-Authenticate": "Bearer"},
    )
