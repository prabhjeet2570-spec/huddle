"""Transactional room allocation and booking lifecycle."""

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from psycopg.errors import ExclusionViolation
from psycopg.types.json import Jsonb
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.db import connect
from app.rooms import get_room
from app.session import current_user


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
    title: str = Field(default="Team meeting", min_length=1, max_length=100)


class BookingStatus(StrEnum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class Booking(BookingRequest):
    model_config = ConfigDict(frozen=True)

    id: UUID
    version: int = 1
    calendar_status: str = "pending"
    status: BookingStatus = BookingStatus.CONFIRMED
    created_at: datetime
    updated_at: datetime
    cancelled_at: datetime | None = None


router = APIRouter(prefix="/bookings", tags=["bookings"])


def event(conn, booking_id, kind, detail):
    conn.execute(
        "INSERT INTO booking_events (booking_id,kind,detail) VALUES (%s,%s,%s)",
        (booking_id, kind, detail),
    )


def occupied_bookings(window: TimeWindow, room_id: str | None = None):
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM bookings WHERE status='confirmed'
            AND starts_at<%s AND ends_at>%s AND (%s::text IS NULL OR room_id=%s)
            ORDER BY starts_at,id""",
            (window.ends_at, window.starts_at, room_id, room_id),
        ).fetchall()
    return [
        Booking.model_validate({k: v for k, v in row.items() if k != "owner_id"}) for row in rows
    ]


def validate_request(request):
    room = get_room(request.room_id)
    if request.attendees > room.capacity:
        raise HTTPException(422, "Attendees exceed room capacity")
    if request.starts_at <= datetime.now(UTC):
        raise HTTPException(422, "Booking must start in the future")


def owned(conn, booking_id, user_id):
    row = conn.execute(
        "SELECT * FROM bookings WHERE id=%s AND owner_id=%s FOR UPDATE",
        (booking_id, user_id),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Booking not found")
    return row


def public(row):
    return {k: v for k, v in row.items() if k != "owner_id"}


def create_in_transaction(conn, request, user_id):
    validate_request(request)
    row = conn.execute(
        """INSERT INTO bookings (id,owner_id,room_id,title,starts_at,ends_at,attendees)
        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
        (
            uuid4(),
            user_id,
            request.room_id,
            request.title,
            request.starts_at,
            request.ends_at,
            request.attendees,
        ),
    ).fetchone()
    event(conn, row["id"], "confirmed", "Room reserved. Calendar synchronization pending.")
    return public(row)


@router.get("", response_model=list[Booking])
def list_bookings(
    room_id: str | None = None,
    status: BookingStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    user=Depends(current_user),
):
    if room_id is not None:
        get_room(room_id)
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM bookings WHERE owner_id=%s
            AND (%s::text IS NULL OR room_id=%s) AND (%s::text IS NULL OR status=%s)
            ORDER BY starts_at,id LIMIT %s OFFSET %s""",
            (user["id"], room_id, room_id, status, status, limit, offset),
        ).fetchall()
    return [public(row) for row in rows]


@router.post("", response_model=Booking, status_code=201)
def create_booking(
    request: BookingRequest,
    user=Depends(current_user),
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
):
    fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    try:
        with connect() as conn:
            if idempotency_key:
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (str(user["id"]) + ":" + idempotency_key,),
                )
                prior = conn.execute(
                    "SELECT * FROM requests WHERE owner_id=%s AND key=%s",
                    (user["id"], idempotency_key),
                ).fetchone()
                if prior:
                    if prior["fingerprint"] != fingerprint:
                        raise HTTPException(
                            409,
                            "Idempotency key was already used for a different booking",
                        )
                    return prior["response"]
            result = create_in_transaction(conn, request, user["id"])
            if idempotency_key:
                conn.execute(
                    "INSERT INTO requests VALUES (%s,%s,%s,%s)",
                    (
                        user["id"],
                        idempotency_key,
                        fingerprint,
                        Jsonb(json.loads(Booking.model_validate(result).model_dump_json())),
                    ),
                )
            return result
    except ExclusionViolation:
        raise HTTPException(409, "Room is already booked") from None


@router.get("/{booking_id}", response_model=Booking)
def get_booking(booking_id: UUID, user=Depends(current_user)):
    with connect() as conn:
        return public(owned(conn, booking_id, user["id"]))


@router.get("/{booking_id}/events")
def booking_events(booking_id: UUID, user=Depends(current_user)):
    with connect() as conn:
        owned(conn, booking_id, user["id"])
        return conn.execute(
            "SELECT kind,detail,created_at FROM booking_events WHERE booking_id=%s ORDER BY id",
            (booking_id,),
        ).fetchall()


@router.put("/{booking_id}", response_model=Booking)
def reschedule_booking(
    booking_id: UUID,
    request: BookingRequest,
    user=Depends(current_user),
    if_match: Annotated[int, Header(ge=1)] = ...,
):
    try:
        with connect() as conn:
            previous = owned(conn, booking_id, user["id"])
            if previous["version"] != if_match:
                raise HTTPException(409, "Booking changed. Refresh before editing.")
            if previous["status"] == "cancelled" or previous["starts_at"] <= datetime.now(UTC):
                raise HTTPException(409, "Only future confirmed bookings can be changed")
            validate_request(request)
            row = conn.execute(
                """UPDATE bookings SET room_id=%s,title=%s,starts_at=%s,ends_at=%s,attendees=%s,
                version=version+1,updated_at=now(),calendar_status='pending' WHERE id=%s RETURNING *""",
                (
                    request.room_id,
                    request.title,
                    request.starts_at,
                    request.ends_at,
                    request.attendees,
                    booking_id,
                ),
            ).fetchone()
            event(
                conn,
                booking_id,
                "rescheduled",
                "Booking changed; previous slot released.",
            )
            return public(row)
    except ExclusionViolation:
        raise HTTPException(409, "Room is already booked. Original booking retained.") from None


@router.post("/{booking_id}/cancel", response_model=Booking)
def cancel_booking(
    booking_id: UUID,
    user=Depends(current_user),
    if_match: Annotated[int, Header(ge=1)] = ...,
):
    with connect() as conn:
        row = owned(conn, booking_id, user["id"])
        if row["status"] == "cancelled":
            return public(row)
        if row["version"] != if_match:
            raise HTTPException(409, "Booking changed. Refresh before cancelling.")
        if row["starts_at"] <= datetime.now(UTC):
            raise HTTPException(409, "A booking that has started cannot be cancelled")
        row = conn.execute(
            """UPDATE bookings SET status='cancelled',cancelled_at=now(),updated_at=now(),
            version=version+1,calendar_status='pending' WHERE id=%s RETURNING *""",
            (booking_id,),
        ).fetchone()
        event(conn, booking_id, "cancelled", "Room released. Calendar removal pending.")
        return public(row)
