"""Attendee notification.

Stands in for an email or chat integration. It writes to an outbox table so
the notify step has something real to compensate: voiding the row is the undo,
and it is idempotent because voiding an already-voided row changes nothing.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.reservation import Reservation
from app.infrastructure.models import NotificationModel
from app.reliability.failure_injection import injector


class NotificationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def notify_booking(self, reservation: Reservation, recipient: str) -> UUID:
        injector.maybe_fail("notification.send")

        model = NotificationModel(
            id=uuid4(),
            reservation_id=reservation.id,
            channel="email",
            recipient=recipient,
            body=(
                f"{reservation.title} in room {reservation.room_name} on "
                f"{reservation.time_range.starts_at:%Y-%m-%d} from "
                f"{reservation.time_range.starts_at:%H:%M} to "
                f"{reservation.time_range.ends_at:%H:%M}. "
                f"Reference {reservation.reference}."
            ),
            status="sent",
        )
        self._session.add(model)
        await self._session.commit()
        return model.id

    async def void(self, reservation_id: UUID) -> bool:
        """Compensation. Returns whether anything was actually voided."""
        injector.maybe_fail("notification.void")

        result = await self._session.execute(
            update(NotificationModel)
            .where(
                NotificationModel.reservation_id == reservation_id,
                NotificationModel.status == "sent",
            )
            .values(status="voided")
            .returning(NotificationModel.id)
        )
        voided = list(result.scalars().all())
        await self._session.commit()
        return bool(voided)

    async def count_sent(self, reservation_id: UUID) -> int:
        rows = await self._session.scalars(
            select(NotificationModel.id).where(
                NotificationModel.reservation_id == reservation_id,
                NotificationModel.status == "sent",
            )
        )
        return len(list(rows))
