"""Booking requests and a temporary, single-process booking store.

Bookings are lost on restart. The lock only coordinates threads in one process;
PostgreSQL will replace this store before multiple workers are supported.
"""

from datetime import UTC, datetime
from threading import Lock
from typing import Self
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.rooms import get_room


class BookingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    room_id: str = Field(min_length=1)
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    attendees: int = Field(gt=0, strict=True)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def normalize_to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_time_order(self) -> Self:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class Booking(BookingRequest):
    model_config = ConfigDict(frozen=True)

    id: UUID


_bookings: list[Booking] = []
_booking_lock = Lock()
router = APIRouter(prefix="/bookings", tags=["bookings"])


@router.post("", response_model=Booking, status_code=201)
def create_booking(request: BookingRequest) -> Booking:
    """Reserve a room in this process's temporary booking store."""
    room = get_room(request.room_id)
    if request.attendees > room.capacity:
        raise HTTPException(status_code=422, detail="Attendees exceed room capacity")

    # Keep the overlap check and insertion together so competing threads
    # cannot both observe the same slot as free.
    with _booking_lock:
        if request.starts_at <= datetime.now(UTC):
            raise HTTPException(status_code=422, detail="Booking must start in the future")

        for booking in _bookings:
            if (
                booking.room_id == request.room_id
                and request.starts_at < booking.ends_at
                and booking.starts_at < request.ends_at
            ):
                raise HTTPException(status_code=409, detail="Room is already booked")

        booking = Booking(id=uuid4(), **request.model_dump())
        _bookings.append(booking)

    return booking
