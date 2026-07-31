"""Room search and schedule views over the current booking store."""

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import Field

from app.bookings import Booking, TimeWindow, occupied_bookings
from app.rooms import ROOMS, Room, get_room

router = APIRouter(tags=["availability"])


class AvailabilityQuery(TimeWindow):
    min_capacity: int = Field(default=1, ge=1)


class AvailabilityResult(TimeWindow):
    rooms: list[Room]


class RoomSchedule(TimeWindow):
    room: Room
    bookings: list[Booking]
    free_windows: list[TimeWindow]


@router.get("/availability", response_model=AvailabilityResult)
def search_availability(
    query: Annotated[AvailabilityQuery, Query()],
) -> AvailabilityResult:
    """Find rooms free for the entire window. Results do not reserve a slot."""
    occupied = {booking.room_id for booking in occupied_bookings(query)}
    return AvailabilityResult(
        starts_at=query.starts_at,
        ends_at=query.ends_at,
        rooms=[
            room
            for room in ROOMS
            if room.capacity >= query.min_capacity and room.id not in occupied
        ],
    )


@router.get("/rooms/{room_id}/schedule", response_model=RoomSchedule)
def room_schedule(
    room_id: str, window: Annotated[TimeWindow, Query()]
) -> RoomSchedule:
    """Return intersecting bookings and free intervals within the requested window."""
    room = get_room(room_id)
    bookings = occupied_bookings(window, room_id)
    free_windows = []
    cursor = window.starts_at
    for booking in bookings:
        occupied_start = max(booking.starts_at, window.starts_at)
        if cursor < occupied_start:
            free_windows.append(TimeWindow(starts_at=cursor, ends_at=occupied_start))
        cursor = max(cursor, min(booking.ends_at, window.ends_at))
    if cursor < window.ends_at:
        free_windows.append(TimeWindow(starts_at=cursor, ends_at=window.ends_at))
    return RoomSchedule(
        **window.model_dump(), room=room, bookings=bookings, free_windows=free_windows
    )
