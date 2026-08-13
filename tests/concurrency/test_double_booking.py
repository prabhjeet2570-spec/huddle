"""The property this whole project exists to guarantee.

Fifty coroutines, fifty independent database sessions, one room, one time
window, released simultaneously. Exactly one may win.

This is not a check-then-act race that happens to be narrow. There is no read
before the write anywhere in the hold path: every attempt inserts, and
PostgreSQL's exclusion constraint rejects forty-nine of them.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest
from sqlalchemy import func, select

from app.domain.enums import ReservationState
from app.domain.exceptions import RoomNotAvailable
from app.domain.time_range import TimeRange
from app.infrastructure.models import ReservationModel
from app.infrastructure.repositories.reservation_repository import (
    ReservationRepository,
)
from app.services.booking_service import BookingService
from tests.conftest import at, requires_postgres

pytestmark = [requires_postgres, pytest.mark.postgres]

CONTENDERS = 50


async def _attempt(session_factory, tenant, time_range, clock, index: int) -> str:
    """One agent's booking attempt, on its own connection."""
    async with session_factory() as session:
        service = BookingService(ReservationRepository(session), clock=clock)
        try:
            await service.place_hold(
                org_id=tenant.org_id,
                user_id=tenant.user_id,
                room_name="A",
                title=f"attempt-{index}",
                attendees=2,
                time_range=time_range,
            )
        except RoomNotAvailable:
            return "rejected"
        except Exception as error:  # surfaced, never swallowed
            return f"error:{type(error).__name__}"
        return "won"


async def test_fifty_concurrent_holds_on_one_slot_produce_exactly_one_winner(
    session_factory, tenant, clock, capsys
):
    time_range = TimeRange(at(10, 0), at(11, 0))

    results = await asyncio.gather(
        *[
            _attempt(session_factory, tenant, time_range, clock, index)
            for index in range(CONTENDERS)
        ]
    )
    tally = Counter(results)

    async with session_factory() as session:
        blocking = await session.scalar(
            select(func.count())
            .select_from(ReservationModel)
            .where(ReservationModel.state.in_(ReservationState.blocking()))
        )

    with capsys.disabled():
        print(
            f"\n  {CONTENDERS} concurrent attempts on one slot -> "
            f"won={tally['won']} rejected={tally['rejected']} "
            f"other={sum(v for k, v in tally.items() if k not in ('won', 'rejected'))}"
            f" | blocking rows in db={blocking}"
        )

    assert tally["won"] == 1, f"expected exactly one winner, got {tally}"
    assert tally["rejected"] == CONTENDERS - 1, f"unexpected failures: {tally}"
    # The decisive assertion: the database itself holds exactly one row that
    # occupies this room. Anything else is a double booking.
    assert blocking == 1


async def test_concurrent_holds_on_adjacent_slots_all_succeed(
    session_factory, tenant, clock
):
    """Half-open ranges must not be treated as overlapping when they touch."""
    ranges = [TimeRange(at(h, 0), at(h + 1, 0)) for h in (10, 11, 12, 13)]

    results = await asyncio.gather(
        *[
            _attempt(session_factory, tenant, time_range, clock, index)
            for index, time_range in enumerate(ranges)
        ]
    )

    assert results == ["won"] * len(ranges)


async def test_concurrent_holds_on_different_rooms_all_succeed(
    session_factory, tenant, clock
):
    time_range = TimeRange(at(10, 0), at(11, 0))

    async def attempt(room: str) -> str:
        async with session_factory() as session:
            service = BookingService(ReservationRepository(session), clock=clock)
            try:
                await service.place_hold(
                    org_id=tenant.org_id,
                    user_id=tenant.user_id,
                    room_name=room,
                    title=f"room-{room}",
                    attendees=2,
                    time_range=time_range,
                )
            except RoomNotAvailable:
                return "rejected"
            return "won"

    results = await asyncio.gather(*[attempt(room) for room in "ABCDE"])
    assert results == ["won"] * 5


async def test_partial_overlap_is_rejected(session_factory, tenant, clock):
    """10:00-11:00 must block 10:30-11:30, not just an identical range."""
    async with session_factory() as session:
        service = BookingService(ReservationRepository(session), clock=clock)
        await service.place_hold(
            org_id=tenant.org_id,
            user_id=tenant.user_id,
            room_name="A",
            title="first",
            attendees=2,
            time_range=TimeRange(at(10, 0), at(11, 0)),
        )

    async with session_factory() as session:
        service = BookingService(ReservationRepository(session), clock=clock)
        with pytest.raises(RoomNotAvailable) as raised:
            await service.place_hold(
                org_id=tenant.org_id,
                user_id=tenant.user_id,
                room_name="A",
                title="overlapping",
                attendees=2,
                time_range=TimeRange(at(10, 30), at(11, 30)),
            )
    # The error is actionable: it names the conflict and the alternatives.
    assert "10:00" in str(raised.value)
    assert "B" in str(raised.value)


async def test_two_tenants_may_hold_their_own_room_a_simultaneously(
    session_factory, tenant, other_tenant, clock
):
    """Room 'A' in Acme and room 'A' in Globex are different rooms."""
    time_range = TimeRange(at(10, 0), at(11, 0))

    async def attempt(current) -> str:
        async with session_factory() as session:
            service = BookingService(ReservationRepository(session), clock=clock)
            try:
                await service.place_hold(
                    org_id=current.org_id,
                    user_id=current.user_id,
                    room_name="A",
                    title=f"{current.slug}-meeting",
                    attendees=2,
                    time_range=time_range,
                )
            except RoomNotAvailable:
                return "rejected"
            return "won"

    assert await asyncio.gather(attempt(tenant), attempt(other_tenant)) == [
        "won",
        "won",
    ]
