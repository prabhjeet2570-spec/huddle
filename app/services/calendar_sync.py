"""External calendar synchronisation.

Stands in for a Google Calendar or Exchange integration: the step that is
furthest from our database and therefore most likely to fail after everything
else has already succeeded. Its compensation deletes the entry.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.reservation import Reservation
from app.infrastructure.models import CalendarEntryModel
from app.reliability.failure_injection import injector


class CalendarService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_entry(self, reservation: Reservation) -> str:
        injector.maybe_fail("calendar.create")

        existing = await self._session.scalar(
            select(CalendarEntryModel).where(
                CalendarEntryModel.reservation_id == reservation.id
            )
        )
        if existing is not None:
            return existing.external_id  # retry-safe

        external_id = f"cal_{uuid4().hex[:12]}"
        self._session.add(
            CalendarEntryModel(
                id=uuid4(),
                reservation_id=reservation.id,
                external_id=external_id,
                payload={
                    "title": reservation.title,
                    "room": reservation.room_name,
                    "attendees": reservation.attendees,
                    "start": reservation.time_range.starts_at.isoformat(),
                    "end": reservation.time_range.ends_at.isoformat(),
                },
            )
        )
        await self._session.commit()
        return external_id

    async def delete_entry(self, reservation_id: UUID) -> bool:
        """Compensation. Returns whether an entry existed to delete."""
        injector.maybe_fail("calendar.delete")

        result = await self._session.execute(
            delete(CalendarEntryModel)
            .where(CalendarEntryModel.reservation_id == reservation_id)
            .returning(CalendarEntryModel.id)
        )
        deleted = list(result.scalars().all())
        await self._session.commit()
        return bool(deleted)

    async def has_entry(self, reservation_id: UUID) -> bool:
        return (
            await self._session.scalar(
                select(CalendarEntryModel.id).where(
                    CalendarEntryModel.reservation_id == reservation_id
                )
            )
        ) is not None
