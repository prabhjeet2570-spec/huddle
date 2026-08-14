"""Background sweeper that releases holds nobody confirmed.

A hold that is never confirmed must not block a room forever. The sweeper is
the safety net rather than the primary mechanism: ``place_hold`` already
expires lapsed holds for the room it is about to touch, so correctness does not
depend on how promptly this loop runs. What it adds is that a room stops
*looking* occupied in availability queries shortly after its hold lapses.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime

from app.config import settings
from app.domain.enums import HoldEvent
from app.infrastructure.database import session_scope
from app.infrastructure.repositories.reservation_repository import (
    ReservationRepository,
)
from app.services.booking_service import office_now

logger = logging.getLogger(__name__)


async def sweep_once(now: datetime | None = None, batch: int = 200) -> int:
    """Expire every lapsed hold. Returns how many were released."""
    moment = now or office_now()
    async with session_scope() as session:
        repository = ReservationRepository(session)
        candidates = await repository.expiring_holds(moment, limit=batch)
        if not candidates:
            return 0

        released = await repository.sweep_expired_holds(moment, limit=batch)
        for reservation_id in released:
            reservation = await repository.get_by_id_any_org(reservation_id)
            if reservation is None:
                continue
            await repository.record_hold_event(
                org_id=reservation.org_id,
                user_id=reservation.user_id,
                room_id=reservation.room_id,
                reservation_id=reservation_id,
                event=HoldEvent.EXPIRED,
                created_at=moment,
            )
        await session.commit()
        if released:
            logger.info("hold sweeper released %d expired holds", len(released))
        return len(released)


class HoldSweeper:
    """Owns the sweeper task for the lifetime of the application."""

    def __init__(self, clock: Callable[[], datetime] = office_now) -> None:
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if not settings.hold_sweeper_enabled or self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="hold-sweeper")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        interval = settings.hold_sweeper_interval_seconds
        while not self._stopping.is_set():
            try:
                await sweep_once(self._clock())
            except asyncio.CancelledError:
                raise
            except Exception:
                # A sweeper that dies silently is worse than one that logs and
                # keeps going: the next pass picks up whatever this one missed.
                logger.exception("hold sweeper pass failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except TimeoutError:
                continue


sweeper = HoldSweeper()
