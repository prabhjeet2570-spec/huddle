"""Idempotent seed data.

Two organizations exist by default so that tenant isolation is demonstrable
rather than merely claimed: the same room names exist in both, and neither can
see the other's bookings.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import hash_password
from app.config import settings
from app.infrastructure.database import session_scope
from app.infrastructure.models import OrganizationModel, RoomModel, UserModel

logger = logging.getLogger(__name__)

DEFAULT_ROOMS: dict[str, int] = {"A": 4, "B": 6, "C": 8, "D": 12, "E": 20}

ORGANIZATIONS: list[dict] = [
    {
        "slug": "acme",
        "name": "Acme Corp",
        "rooms": DEFAULT_ROOMS,
        "users": [("alice", "admin"), ("bob", "member")],
    },
    {
        "slug": "globex",
        "name": "Globex Inc",
        "rooms": {"A": 6, "B": 10},
        "users": [("carol", "admin"), ("dave", "member")],
    },
]


async def seed(session: AsyncSession | None = None) -> None:
    if session is not None:
        await _seed(session)
        return
    async with session_scope() as owned:
        await _seed(owned)


async def _seed(session: AsyncSession) -> None:
    password_hash = hash_password(settings.seed_user_password.get_secret_value())

    for spec in ORGANIZATIONS:
        org = await session.scalar(
            select(OrganizationModel).where(OrganizationModel.slug == spec["slug"])
        )
        if org is None:
            org = OrganizationModel(slug=spec["slug"], name=spec["name"])
            session.add(org)
            await session.flush()

        await _seed_rooms(session, org.id, spec["rooms"])
        await _seed_users(session, org.id, spec["users"], password_hash)

    await session.commit()
    logger.info("seed complete: %d organizations", len(ORGANIZATIONS))


async def _seed_rooms(
    session: AsyncSession,
    org_id: UUID,
    rooms: dict[str, int],
) -> None:
    existing = set(
        (
            await session.scalars(
                select(RoomModel.name).where(RoomModel.org_id == org_id)
            )
        ).all()
    )
    for name, capacity in rooms.items():
        if name not in existing:
            session.add(RoomModel(org_id=org_id, name=name, capacity=capacity))


async def _seed_users(
    session: AsyncSession,
    org_id: UUID,
    users: list[tuple[str, str]],
    password_hash: str,
) -> None:
    existing = set(
        (
            await session.scalars(
                select(UserModel.username).where(UserModel.org_id == org_id)
            )
        ).all()
    )
    for username, role in users:
        if username not in existing:
            session.add(
                UserModel(
                    org_id=org_id,
                    username=username,
                    password_hash=password_hash,
                    role=role,
                )
            )
