"""Booking use cases.

The service validates business rules and then delegates contention entirely to
the database. There is no availability check standing between a decision and a
write: :meth:`place_hold` inserts and lets the exclusion constraint rule. Reads
like :meth:`list_available_rooms` exist to help the user choose, never to
authorize a write.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from uuid import UUID

from app.config import OFFICE_TZ, settings
from app.domain.enums import HoldEvent, ReservationState
from app.domain.exceptions import (
    HoldExpired,
    ReservationNotFound,
    RoomCapacityExceeded,
    RoomNotAvailable,
    RoomNotFound,
)
from app.domain.reservation import Reservation
from app.domain.room import Room
from app.domain.rules import (
    validate_booking_horizon,
    validate_booking_request,
    validate_can_be_cancelled,
    validate_room_capacity,
    validate_working_day,
)
from app.domain.schedule import derive_free_ranges
from app.domain.time_range import TimeRange
from app.infrastructure.repositories.reservation_repository import (
    ReservationRepository,
)
from app.services.abuse_detection import HoldCyclingDetector


def office_now() -> datetime:
    return datetime.now(OFFICE_TZ)


class BookingService:
    def __init__(
        self,
        repository: ReservationRepository,
        detector: HoldCyclingDetector | None = None,
        clock: Callable[[], datetime] = office_now,
    ) -> None:
        self._repository = repository
        self._detector = detector
        self._clock = clock

    @property
    def now(self) -> datetime:
        return self._clock()

    # --- Reads -------------------------------------------------------------

    async def list_my_reservations(
        self,
        user_id: UUID,
        org_id: UUID,
    ) -> list[Reservation]:
        return await self._repository.list_for_user(user_id, org_id)

    async def list_rooms(self, org_id: UUID) -> list[Room]:
        return await self._repository.list_rooms(org_id)

    async def list_available_rooms(
        self,
        org_id: UUID,
        time_range: TimeRange,
        attendees: int,
    ) -> list[Room]:
        now = self.now
        validate_booking_request(time_range, attendees, now)

        max_capacity = await self._repository.max_capacity(org_id)
        if attendees > max_capacity:
            raise RoomCapacityExceeded(
                f"The largest room holds {max_capacity} attendees, but "
                f"{attendees} were requested. Reduce the attendee count."
            )
        return await self._repository.find_available_rooms(
            org_id, time_range, attendees, now
        )

    async def get_room_schedule(
        self,
        org_id: UUID,
        room_name: str,
        day: date,
    ) -> tuple[Room, list[TimeRange], list[TimeRange]]:
        room = await self._repository.get_room_by_name(org_id, room_name)
        if room is None:
            raise RoomNotFound(room_name)

        business_hours = TimeRange(
            starts_at=datetime.combine(day, settings.business_start, tzinfo=OFFICE_TZ),
            ends_at=datetime.combine(day, settings.business_end, tzinfo=OFFICE_TZ),
        )
        validate_working_day(business_hours)
        validate_booking_horizon(business_hours, self.now)

        taken = await self._repository.find_day_reservations(room.id, day, self.now)
        free = derive_free_ranges(taken, business_hours)
        return room, taken, free

    async def get_reservation(
        self,
        reference: str,
        org_id: UUID,
        user_id: UUID | None = None,
    ) -> Reservation:
        reservation = await self._repository.get_by_reference(
            reference, org_id, user_id
        )
        if reservation is None:
            raise ReservationNotFound(reference)
        return reservation

    # --- Holds -------------------------------------------------------------

    async def place_hold(
        self,
        *,
        org_id: UUID,
        user_id: UUID,
        room_name: str,
        title: str,
        attendees: int,
        time_range: TimeRange,
        conversation_id: UUID | None = None,
        ttl_seconds: int | None = None,
    ) -> Reservation:
        """Reserve a room provisionally.

        A hold is low blast radius by construction: it releases itself when
        the TTL elapses, so the agent may place one without asking. That same
        property is what makes hold-cycling possible, which is why the
        detector runs first.
        """
        now = self.now
        validate_booking_request(time_range, attendees, now)

        room = await self._repository.get_room_by_name(org_id, room_name)
        if room is None:
            raise RoomNotFound(room_name)
        validate_room_capacity(attendees, room.capacity)

        if self._detector is not None:
            # Raises HoldRateLimited when this user is cycling holds.
            await self._detector.check(org_id=org_id, user_id=user_id, now=now)

        try:
            return await self._repository.place_hold(
                org_id=org_id,
                room=room,
                user_id=user_id,
                conversation_id=conversation_id,
                title=title,
                attendees=attendees,
                time_range=time_range,
                now=now,
                ttl_seconds=ttl_seconds,
            )
        except RoomNotAvailable as error:
            raise await self._enrich_unavailable(
                org_id, room, time_range, attendees, now
            ) from error

    async def confirm_hold(
        self,
        reservation_id: UUID,
        org_id: UUID,
    ) -> Reservation:
        """Promote a live hold to a confirmed booking.

        Returns the confirmed reservation, or raises :class:`HoldExpired` if
        the TTL elapsed first. The room can never have been taken by somebody
        else in between, because the hold row itself was blocking it.
        """
        now = self.now
        reservation = await self._repository.confirm_hold(reservation_id, org_id, now)
        if reservation is not None:
            return reservation

        existing = await self._repository.get_by_id(reservation_id, org_id)
        if existing is not None and existing.state is ReservationState.CONFIRMED:
            return existing  # already confirmed; confirmation is idempotent

        if existing is not None and existing.state is ReservationState.HELD:
            # The TTL lapsed before we got here. Retire the row now rather
            # than leaving a stale hold for the sweeper: we already know it
            # is dead, and a row in the wrong state is how audits get hard.
            await self._repository.release(
                reservation_id, org_id, ReservationState.EXPIRED
            )
            await self._repository.record_hold_event(
                org_id=org_id,
                user_id=existing.user_id,
                room_id=existing.room_id,
                reservation_id=reservation_id,
                event=HoldEvent.EXPIRED,
                created_at=now,
            )
        raise HoldExpired(reservation_id)

    async def release_hold(self, reservation_id: UUID, org_id: UUID) -> bool:
        """Compensation for :meth:`place_hold`. Idempotent."""
        now = self.now
        released = await self._repository.release(
            reservation_id, org_id, ReservationState.CANCELLED
        )
        if released:
            reservation = await self._repository.get_by_id(reservation_id, org_id)
            if reservation is not None:
                await self._repository.record_hold_event(
                    org_id=org_id,
                    user_id=reservation.user_id,
                    room_id=reservation.room_id,
                    reservation_id=reservation_id,
                    event=HoldEvent.RELEASED,
                    created_at=now,
                )
        return released

    async def revert_confirmation(self, reservation_id: UUID, org_id: UUID) -> bool:
        """Compensation for :meth:`confirm_hold`. Idempotent."""
        return await self._repository.revert_to_hold(reservation_id, org_id, self.now)

    # --- Cancellation ------------------------------------------------------

    async def cancel(
        self,
        reference: str,
        org_id: UUID,
        user_id: UUID,
    ) -> Reservation:
        reservation = await self._repository.get_by_reference(
            reference, org_id, user_id
        )
        # A reservation belonging to somebody else looks exactly like one that
        # does not exist, so the error cannot be used to probe for other
        # people's bookings.
        if reservation is None or not reservation.is_blocking:
            raise ReservationNotFound(reference)

        validate_can_be_cancelled(reservation.time_range.starts_at, self.now)
        await self._repository.release(
            reservation.id, org_id, ReservationState.CANCELLED
        )
        return reservation

    # --- Helpers -----------------------------------------------------------

    async def _enrich_unavailable(
        self,
        org_id: UUID,
        room: Room,
        time_range: TimeRange,
        attendees: int,
        now: datetime,
    ) -> RoomNotAvailable:
        """Turn a bare constraint violation into an actionable message."""
        conflict = await self._repository.find_conflict(room.id, time_range, now)
        alternatives = await self._repository.find_available_rooms(
            org_id, time_range, attendees, now
        )
        return RoomNotAvailable(
            room_name=room.name,
            conflict_start=conflict[0] if conflict else None,
            conflict_end=conflict[1] if conflict else None,
            alternative_rooms=[room.name for room in alternatives],
        )
