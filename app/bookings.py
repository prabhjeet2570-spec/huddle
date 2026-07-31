"""Booking requests and a temporary, single-process booking store.

Bookings are lost on restart. The lock only coordinates threads in one process;
PostgreSQL will replace this store before multiple workers are supported.
"""

from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock
from typing import Annotated, Self
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.rooms import get_room


class TimeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    starts_at: AwareDatetime
    ends_at: AwareDatetime

    @field_validator("starts_at", "ends_at")
    @classmethod
    def normalize_to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_time_order(self) -> Self:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self

    def overlaps(self, other: "TimeWindow") -> bool:
        return self.starts_at < other.ends_at and other.starts_at < self.ends_at


class BookingRequest(TimeWindow):
    room_id: str = Field(min_length=1)
    attendees: int = Field(gt=0, strict=True)


class BookingStatus(StrEnum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class Booking(BookingRequest):
    model_config = ConfigDict(frozen=True)

    id: UUID
    status: BookingStatus = BookingStatus.CONFIRMED
    created_at: datetime
    updated_at: datetime
    cancelled_at: datetime | None = None


_bookings: dict[UUID, Booking] = {}
_booking_lock = Lock()
router = APIRouter(prefix="/bookings", tags=["bookings"])


def occupied_bookings(window: TimeWindow, room_id: str | None = None) -> list[Booking]:
    with _booking_lock:
        matches = [
            booking
            for booking in _bookings.values()
            if booking.status == BookingStatus.CONFIRMED
            and (room_id is None or booking.room_id == room_id)
            and window.overlaps(booking)
        ]
    return sorted(matches, key=lambda booking: (booking.starts_at, booking.id))


def _validate_slot(
    request: BookingRequest, now: datetime, exclude_id: UUID | None = None
) -> None:
    # The caller must hold _booking_lock through validation and storage.
    room = get_room(request.room_id)
    if request.attendees > room.capacity:
        raise HTTPException(status_code=422, detail="Attendees exceed room capacity")
    if request.starts_at <= now:
        raise HTTPException(status_code=422, detail="Booking must start in the future")
    if any(
        booking.id != exclude_id
        and booking.status == BookingStatus.CONFIRMED
        and booking.room_id == request.room_id
        and request.overlaps(booking)
        for booking in _bookings.values()
    ):
        raise HTTPException(status_code=409, detail="Room is already booked")


@router.get("", response_model=list[Booking])
def list_bookings(
    room_id: str | None = None,
    status: BookingStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Booking]:
    """List reservations, including cancellations, ordered by start time."""
    if room_id is not None:
        get_room(room_id)

    with _booking_lock:
        bookings = [
            booking
            for booking in _bookings.values()
            if (room_id is None or booking.room_id == room_id)
            and (status is None or booking.status == status)
        ]

    bookings.sort(key=lambda booking: (booking.starts_at, booking.id))
    return bookings[offset : offset + limit]


@router.post("", response_model=Booking, status_code=201)
def create_booking(request: BookingRequest) -> Booking:
    """Reserve a room in this process's temporary booking store."""
    # Keep the overlap check and insertion together so competing threads
    # cannot both observe the same slot as free.
    with _booking_lock:
        now = datetime.now(UTC)
        _validate_slot(request, now)
        booking = Booking(
            id=uuid4(), created_at=now, updated_at=now, **request.model_dump()
        )
        _bookings[booking.id] = booking

    return booking


@router.get("/{booking_id}", response_model=Booking)
def get_booking(booking_id: UUID) -> Booking:
    """Retrieve a reservation by the reference returned when it was created."""
    with _booking_lock:
        booking = _bookings.get(booking_id)
        if booking is not None:
            return booking

    raise HTTPException(status_code=404, detail="Booking not found")


@router.put("/{booking_id}", response_model=Booking)
def reschedule_booking(booking_id: UUID, request: BookingRequest) -> Booking:
    """Replace room, times, and attendees together, retaining the booking ID."""
    with _booking_lock:
        booking = _bookings.get(booking_id)
        if booking is None:
            raise HTTPException(status_code=404, detail="Booking not found")
        if booking.status == BookingStatus.CANCELLED:
            raise HTTPException(
                status_code=409, detail="Cancelled bookings cannot be changed"
            )

        now = datetime.now(UTC)
        if booking.starts_at <= now:
            raise HTTPException(
                status_code=409, detail="A booking that has started cannot be changed"
            )

        _validate_slot(request, now, exclude_id=booking_id)
        changes = request.model_dump()
        if all(getattr(booking, key) == value for key, value in changes.items()):
            return booking

        # Only replace the record once every check passes. Failed moves leave
        # the original slot reserved, and successful moves release it atomically.
        updated = booking.model_copy(update={**changes, "updated_at": now})
        _bookings[booking_id] = updated

    return updated


@router.post("/{booking_id}/cancel", response_model=Booking)
def cancel_booking(booking_id: UUID) -> Booking:
    """Release a future reservation, retaining its record and cancellation time."""
    with _booking_lock:
        booking = _bookings.get(booking_id)
        if booking is None:
            raise HTTPException(status_code=404, detail="Booking not found")
        if booking.status == BookingStatus.CANCELLED:
            return booking

        now = datetime.now(UTC)
        if booking.starts_at <= now:
            raise HTTPException(
                status_code=409, detail="A booking that has started cannot be cancelled"
            )

        cancelled = booking.model_copy(
            update={
                "status": BookingStatus.CANCELLED,
                "cancelled_at": now,
                "updated_at": now,
            }
        )
        _bookings[booking_id] = cancelled

    return cancelled
