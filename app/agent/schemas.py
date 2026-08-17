"""Tool argument schemas.

Every tool argument crosses a trust boundary: it was written by a language
model, not by a caller we control. These schemas are the checkpoint. An
argument that does not validate never reaches the service layer, and the model
is handed the specific validation failure so it can correct itself rather than
guessing.
"""

from __future__ import annotations

from datetime import date as Date
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, Field, field_validator

from app.config import OFFICE_TZ, settings

DATETIME_DESCRIPTION = (
    "ISO 8601 date and time with the office offset, "
    "YYYY-MM-DDTHH:MM:SS-03:00, for example 2026-09-07T10:00:00-03:00. "
    "It must land on a 30-minute boundary (:00 or :30) with zero seconds."
)
ROOM_DESCRIPTION = (
    "Room name exactly as returned by list_rooms or list_available_rooms, "
    "for example 'A'. Never invent a room name."
)
REFERENCE_DESCRIPTION = (
    "Reservation reference in HDL-XXXX form, taken from list_my_bookings or "
    "from the result of place_hold. Never invent or guess this value."
)


class _SlotAligned(BaseModel):
    """Shared validation for the two timestamp-bearing schemas."""

    @field_validator("starts_at", "ends_at", check_fields=False)
    @classmethod
    def _align(cls, value: datetime) -> datetime:
        moment = value.astimezone(OFFICE_TZ)
        if moment.minute % settings.slot_minutes or moment.second or moment.microsecond:
            raise ValueError(
                f"must land on a {settings.slot_minutes}-minute boundary with "
                f"zero seconds; got {moment.isoformat()}"
            )
        return moment


class ListRoomsInput(BaseModel):
    """No arguments: lists every room in the caller's organization."""


class ListMyBookingsInput(BaseModel):
    """No arguments: the user identity comes from the session, never the model."""


class ListAvailableRoomsInput(_SlotAligned):
    starts_at: AwareDatetime = Field(description=DATETIME_DESCRIPTION)
    ends_at: AwareDatetime = Field(description=DATETIME_DESCRIPTION)
    attendees: int = Field(
        ge=1, le=1000, description="Number of people who need space in the room."
    )


class GetRoomScheduleInput(BaseModel):
    room: str = Field(min_length=1, max_length=32, description=ROOM_DESCRIPTION)
    date: Date = Field(description="ISO 8601 date, for example 2026-09-07.")


class PlaceHoldInput(_SlotAligned):
    room: str = Field(min_length=1, max_length=32, description=ROOM_DESCRIPTION)
    starts_at: AwareDatetime = Field(description=DATETIME_DESCRIPTION)
    ends_at: AwareDatetime = Field(description=DATETIME_DESCRIPTION)
    title: str = Field(
        min_length=1, max_length=200, description="Short meeting title from the user."
    )
    attendees: int = Field(
        ge=1, le=1000, description="Total number of people attending."
    )


class ConfirmBookingInput(BaseModel):
    reference: str = Field(
        min_length=4, max_length=16, description=REFERENCE_DESCRIPTION
    )


class CancelBookingInput(BaseModel):
    reference: str = Field(
        min_length=4, max_length=16, description=REFERENCE_DESCRIPTION
    )
