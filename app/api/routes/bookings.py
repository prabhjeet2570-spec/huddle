from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.domain.exceptions import DomainError, GuardrailError
from app.domain.reservation import Reservation
from app.domain.time_range import TimeRange
from app.infrastructure.repositories.abuse_repository import AbuseRepository
from app.infrastructure.repositories.reservation_repository import (
    ReservationRepository,
)
from app.infrastructure.repositories.telemetry_repository import TelemetryRepository
from app.services.abuse_detection import HoldCyclingDetector
from app.services.booking_saga import BookingSaga
from app.services.booking_service import BookingService
from app.services.calendar_sync import CalendarService
from app.services.notifications import NotificationService

router = APIRouter(prefix="/bookings", tags=["bookings"])


class ReservationResponse(BaseModel):
    reference: str
    room: str
    title: str
    attendees: int
    starts_at: datetime
    ends_at: datetime
    state: str
    hold_expires_at: datetime | None = None

    @classmethod
    def of(cls, reservation: Reservation) -> ReservationResponse:
        return cls(
            reference=reservation.reference,
            room=reservation.room_name,
            title=reservation.title,
            attendees=reservation.attendees,
            starts_at=reservation.time_range.starts_at,
            ends_at=reservation.time_range.ends_at,
            state=reservation.state,
            hold_expires_at=reservation.hold_expires_at,
        )


class RoomResponse(BaseModel):
    name: str
    capacity: int


class HoldRequest(BaseModel):
    room: str
    title: str = Field(min_length=1, max_length=200)
    attendees: int = Field(ge=1)
    starts_at: datetime
    ends_at: datetime


def _services(session, user):
    repository = ReservationRepository(session)
    telemetry = TelemetryRepository(session)
    detector = HoldCyclingDetector(AbuseRepository(session), telemetry)
    booking = BookingService(repository, detector)
    saga = BookingSaga(
        booking, NotificationService(session), CalendarService(session), telemetry
    )
    return booking, saga


def _http(error: Exception) -> HTTPException:
    code = (
        status.HTTP_429_TOO_MANY_REQUESTS
        if isinstance(error, GuardrailError)
        else status.HTTP_409_CONFLICT
    )
    return HTTPException(status_code=code, detail=str(error))


@router.get("/rooms", response_model=list[RoomResponse])
async def list_rooms(user: CurrentUser, session: DbSession) -> list[RoomResponse]:
    booking, _ = _services(session, user)
    rooms = await booking.list_rooms(user.org_id)
    return [RoomResponse(name=room.name, capacity=room.capacity) for room in rooms]


@router.get("/available", response_model=list[RoomResponse])
async def available_rooms(
    user: CurrentUser,
    session: DbSession,
    starts_at: Annotated[datetime, Query()],
    ends_at: Annotated[datetime, Query()],
    attendees: Annotated[int, Query(ge=1)] = 1,
) -> list[RoomResponse]:
    booking, _ = _services(session, user)
    try:
        rooms = await booking.list_available_rooms(
            user.org_id, TimeRange(starts_at, ends_at), attendees
        )
    except DomainError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    return [RoomResponse(name=room.name, capacity=room.capacity) for room in rooms]


@router.get("/me", response_model=list[ReservationResponse])
async def my_bookings(
    user: CurrentUser, session: DbSession
) -> list[ReservationResponse]:
    booking, _ = _services(session, user)
    reservations = await booking.list_my_reservations(user.id, user.org_id)
    return [ReservationResponse.of(reservation) for reservation in reservations]


@router.post(
    "/holds", response_model=ReservationResponse, status_code=status.HTTP_201_CREATED
)
async def place_hold(
    request: HoldRequest, user: CurrentUser, session: DbSession
) -> ReservationResponse:
    booking, _ = _services(session, user)
    try:
        reservation = await booking.place_hold(
            org_id=user.org_id,
            user_id=user.id,
            room_name=request.room,
            title=request.title,
            attendees=request.attendees,
            time_range=TimeRange(request.starts_at, request.ends_at),
        )
    except (DomainError, GuardrailError) as error:
        raise _http(error) from error
    return ReservationResponse.of(reservation)


@router.post("/{reference}/confirm", response_model=ReservationResponse)
async def confirm(
    reference: str, user: CurrentUser, session: DbSession
) -> ReservationResponse:
    booking, saga = _services(session, user)
    try:
        reservation = await booking.get_reservation(reference, user.org_id, user.id)
        outcome = await saga.confirm_booking(
            reservation_id=reservation.id,
            org_id=user.org_id,
            recipient=user.username,
        )
    except DomainError as error:
        raise _http(error) from error
    return ReservationResponse.of(outcome.reservation)


@router.delete("/{reference}", response_model=ReservationResponse)
async def cancel(
    reference: str, user: CurrentUser, session: DbSession
) -> ReservationResponse:
    booking, _ = _services(session, user)
    try:
        reservation = await booking.cancel(reference, user.org_id, user.id)
    except DomainError as error:
        raise _http(error) from error
    return ReservationResponse.of(reservation)
