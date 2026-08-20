#!/usr/bin/env python
"""Measure the double-booking guarantee under contention.

Fires N concurrent hold attempts at one room and time window and reports how
many won. The correct answer is always exactly one; anything else is a
double booking. Run it against a real database to produce the number quoted in
the README rather than repeating one from memory.

    python -m scripts.benchmark_concurrency --attempts 50
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections import Counter
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select, text

from app.api.auth import hash_password
from app.domain.enums import ReservationState
from app.domain.exceptions import RoomNotAvailable
from app.domain.time_range import TimeRange
from app.infrastructure.database import dispose_engine, session_scope
from app.infrastructure.models import (
    OrganizationModel,
    ReservationModel,
    RoomModel,
    UserModel,
)
from app.infrastructure.repositories.reservation_repository import (
    ReservationRepository,
)
from app.services.booking_service import BookingService, office_now


async def _prepare():
    slug = f"bench-{uuid4().hex[:8]}"
    async with session_scope() as session:
        org = OrganizationModel(id=uuid4(), slug=slug, name="Benchmark")
        user = UserModel(
            id=uuid4(),
            org_id=org.id,
            username="bench",
            password_hash=hash_password("x"),
        )
        room = RoomModel(id=uuid4(), org_id=org.id, name="A", capacity=10)
        session.add_all([org, user, room])
        await session.commit()
        return org.id, user.id


async def _attempt(org_id, user_id, window, index: int) -> str:
    async with session_scope() as session:
        service = BookingService(ReservationRepository(session))
        try:
            await service.place_hold(
                org_id=org_id,
                user_id=user_id,
                room_name="A",
                title=f"attempt-{index}",
                attendees=2,
                time_range=window,
            )
        except RoomNotAvailable:
            return "rejected"
        except Exception as error:
            return f"error:{type(error).__name__}"
        return "won"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts", type=int, default=50)
    arguments = parser.parse_args()

    org_id, user_id = await _prepare()

    # Next weekday at 10:00, so the booking rules always accept it.
    start = (office_now() + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    while start.weekday() >= 5:
        start += timedelta(days=1)
    window = TimeRange(start, start + timedelta(hours=1))

    began = time.perf_counter()
    results = await asyncio.gather(
        *[
            _attempt(org_id, user_id, window, index)
            for index in range(arguments.attempts)
        ]
    )
    elapsed = time.perf_counter() - began
    tally = Counter(results)

    async with session_scope() as session:
        blocking = await session.scalar(
            select(func.count())
            .select_from(ReservationModel)
            .where(
                ReservationModel.org_id == org_id,
                ReservationModel.state.in_(ReservationState.blocking()),
            )
        )
        await session.execute(
            text("DELETE FROM organizations WHERE id = :id"), {"id": org_id}
        )
        await session.commit()

    await dispose_engine()

    print("=" * 62)
    print("CONCURRENCY BENCHMARK - one room, one window")
    print("=" * 62)
    print(f"concurrent attempts     {arguments.attempts}")
    print(f"succeeded               {tally['won']}")
    print(f"rejected by constraint  {tally['rejected']}")
    other = sum(v for k, v in tally.items() if k not in ("won", "rejected"))
    print(f"unexpected errors       {other}")
    print(f"rows occupying the room {blocking}")
    print(f"wall clock              {elapsed * 1000:.0f} ms")
    print("-" * 62)
    verdict = tally["won"] == 1 and blocking == 1 and other == 0
    print("RESULT: PASS - exactly one winner" if verdict else "RESULT: FAIL")
    print("=" * 62)
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
