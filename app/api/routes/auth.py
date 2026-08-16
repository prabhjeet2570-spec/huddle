from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.auth import create_access_token, verify_password
from app.api.deps import CurrentUser, DbSession
from app.infrastructure.models import OrganizationModel, UserModel

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    organization: str = Field(description="Organization slug, e.g. 'acme'.")
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class CurrentUserResponse(BaseModel):
    id: str
    username: str
    role: str
    organization: str


@router.post("/login", response_model=TokenResponse)
async def login(credentials: LoginRequest, session: DbSession) -> TokenResponse:
    user = await session.scalar(
        select(UserModel)
        .join(OrganizationModel, UserModel.org_id == OrganizationModel.id)
        .where(
            OrganizationModel.slug == credentials.organization,
            UserModel.username == credentials.username,
        )
    )
    # One message for every failure mode, so the endpoint cannot be used to
    # enumerate organizations or usernames.
    if user is None or not verify_password(credentials.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid organization, username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenResponse(
        access_token=create_access_token(user.id, user.org_id, user.username)
    )


@router.get("/me", response_model=CurrentUserResponse)
async def me(user: CurrentUser, session: DbSession) -> CurrentUserResponse:
    org = await session.get(OrganizationModel, user.org_id)
    return CurrentUserResponse(
        id=str(user.id),
        username=user.username,
        role=user.role,
        organization=org.slug if org else "",
    )
