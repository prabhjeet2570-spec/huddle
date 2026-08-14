"""Test fixtures.

The suite runs against a real PostgreSQL instance, because the property under
test - that two agents cannot double-book a room - is enforced by a PostgreSQL
exclusion constraint and has no SQLite equivalent. Testing it against a
different engine would be testing something else.

``docker compose up -d db`` provides one; CI provides one as a service
container. Without a reachable database the postgres-marked tests skip rather
than fail, so ``pytest`` still runs the pure-domain tests anywhere.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

# Tests get their own database. Sharing one with the development stack lets
# leftover fixture rows shadow the seed, which is a confusing failure to debug.
os.environ.setdefault(
    "HUDDLE_DATABASE_URL",
    "postgresql+asyncpg://huddle:huddle@localhost:5433/huddle_test",
)
os.environ.setdefault("HUDDLE_JWT_SECRET", "test-only-secret-not-for-production")
os.environ.setdefault("HUDDLE_LLM_PROVIDER", "fake")
os.environ.setdefault("HUDDLE_FAILURE_INJECTION_ENABLED", "true")
os.environ.setdefault("HUDDLE_HOLD_SWEEPER_ENABLED", "false")
os.environ.setdefault("HUDDLE_SEED_ON_STARTUP", "false")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.auth import hash_password
from app.config import OFFICE_TZ, settings
from app.infrastructure import database
from app.infrastructure.models import (
    Base,
    OrganizationModel,
    RoomModel,
    UserModel,
)
from app.reliability.failure_injection import injector

TABLES = [
    "saga_events",
    "sagas",
    "tool_invocations",
    "turns",
    "pending_confirmations",
    "conversation_messages",
    "conversations",
    "alerts",
    "notifications",
    "calendar_entries",
    "hold_events",
    "hold_rate_limits",
    "reservations",
    "rooms",
    "users",
    "organizations",
]


def _ensure_test_database() -> None:
    """Create the test database if the server is up but the database is not."""
    import re

    url = settings.database_url
    match = re.match(r"postgresql\+asyncpg://([^:]+):([^@]+)@([^:/]+):(\d+)/(.+)", url)
    if match is None:
        return
    user, password, host, port, name = match.groups()

    try:
        import psycopg

        with psycopg.connect(
            f"postgresql://{user}:{password}@{host}:{port}/postgres",
            autocommit=True,
            connect_timeout=3,
        ) as connection:
            exists = connection.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
            ).fetchone()
            if not exists:
                connection.execute(f'CREATE DATABASE "{name}"')
    except Exception:
        # The probe below reports unavailability; nothing to do here.
        pass


def _database_available() -> bool:
    _ensure_test_database()

    async def probe() -> bool:
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    try:
        return asyncio.run(probe())
    except Exception:
        return False


DATABASE_AVAILABLE = _database_available()
requires_postgres = pytest.mark.skipif(
    not DATABASE_AVAILABLE,
    reason="PostgreSQL not reachable; run `docker compose up -d db`",
)


@pytest_asyncio.fixture(scope="session")
async def engine():
    if not DATABASE_AVAILABLE:
        pytest.skip("PostgreSQL not reachable")
    created = create_async_engine(settings.database_url, pool_size=30, max_overflow=30)
    async with created.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
        await connection.run_sync(Base.metadata.create_all)
    yield created
    await created.dispose()


@pytest_asyncio.fixture
async def session_factory(engine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A clean database per test, wired into the application's session factory."""
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        for table in TABLES:
            await session.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
        await session.commit()

    database.set_session_factory(factory)
    injector.disarm()
    try:
        yield factory
    finally:
        injector.disarm()
        database.set_session_factory(None)


@pytest_asyncio.fixture
async def session(session_factory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as active:
        yield active


class Tenant:
    """A seeded organization with users and rooms, for readable tests."""

    def __init__(
        self,
        org_id: UUID,
        slug: str,
        users: dict[str, UUID],
        rooms: dict[str, UUID],
    ) -> None:
        self.org_id = org_id
        self.slug = slug
        self.users = users
        self.rooms = rooms

    @property
    def user_id(self) -> UUID:
        return self.users["alice"]

    @property
    def other_user_id(self) -> UUID:
        return self.users["bob"]


async def _make_tenant(
    session: AsyncSession,
    slug: str,
    rooms: dict[str, int],
) -> Tenant:
    org = OrganizationModel(id=uuid4(), slug=slug, name=slug.title())
    session.add(org)
    await session.flush()

    password = hash_password("test-password")
    users: dict[str, UUID] = {}
    for name, role in (("alice", "admin"), ("bob", "member")):
        user = UserModel(
            id=uuid4(),
            org_id=org.id,
            username=name,
            password_hash=password,
            role=role,
        )
        session.add(user)
        users[name] = user.id

    room_ids: dict[str, UUID] = {}
    for name, capacity in rooms.items():
        room = RoomModel(id=uuid4(), org_id=org.id, name=name, capacity=capacity)
        session.add(room)
        room_ids[name] = room.id

    await session.commit()
    return Tenant(org.id, slug, users, room_ids)


@pytest_asyncio.fixture
async def tenant(session) -> Tenant:
    return await _make_tenant(
        session, "acme", {"A": 4, "B": 6, "C": 8, "D": 12, "E": 20}
    )


@pytest_asyncio.fixture
async def other_tenant(session) -> Tenant:
    return await _make_tenant(session, "globex", {"A": 6, "B": 10})


# --- Deterministic time ----------------------------------------------------

#: A Monday, inside business hours, comfortably inside the booking horizon.
BASE_TIME = datetime(2026, 10, 5, 9, 0, tzinfo=OFFICE_TZ)


class FrozenClock:
    """A clock the tests move by hand, so TTLs are exact rather than slept for."""

    def __init__(self, start: datetime = BASE_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


def at(hour: int, minute: int = 0, day: int = 5) -> datetime:
    """A datetime on the reference Monday (2026-10-05) in office time."""
    return datetime(2026, 10, day, hour, minute, tzinfo=OFFICE_TZ)
