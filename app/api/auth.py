"""Token issuing and password hashing.

The token carries the organization as well as the user. Every request-scoped
query is filtered by that org id, so a tenant boundary is enforced on the way
in rather than remembered by each query author.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings

password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


@dataclass(frozen=True, slots=True)
class TokenClaims:
    user_id: UUID
    org_id: UUID
    username: str


def create_access_token(user_id: UUID, org_id: UUID, username: str) -> str:
    expires_at = datetime.now(UTC) + timedelta(hours=settings.jwt_expiration_hours)
    payload = {
        "sub": str(user_id),
        "org": str(org_id),
        "username": username,
        "exp": expires_at,
    }
    return jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> TokenClaims:
    payload = jwt.decode(
        token,
        settings.jwt_secret.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
    )
    subject, org, username = (
        payload.get("sub"),
        payload.get("org"),
        payload.get("username"),
    )
    if not subject or not org:
        raise JWTError("Token is missing a subject or organization")
    try:
        return TokenClaims(UUID(subject), UUID(org), username or "")
    except (TypeError, ValueError) as error:
        raise JWTError("Token identifiers are not valid UUIDs") from error


def hash_password(plain: str) -> str:
    return password_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return password_context.verify(plain, hashed)
