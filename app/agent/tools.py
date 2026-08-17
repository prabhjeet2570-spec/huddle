"""The agent's tools.

Each tool carries two things the runtime needs and the prompt cannot be
trusted to supply:

* a **Pydantic schema**, validated before the tool body runs;
* a **handler** that is a plain coroutine, so retries and timeouts can wrap
  it uniformly.

Tool results are structured plain text rather than JSON. The model reads them
directly, and short labelled lines cost fewer tokens than nested objects while
staying unambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.agent.schemas import (
    CancelBookingInput,
    ConfirmBookingInput,
    GetRoomScheduleInput,
    ListAvailableRoomsInput,
    ListMyBookingsInput,
    ListRoomsInput,
    PlaceHoldInput,
)
from app.domain.reservation import Reservation
from app.domain.time_range import TimeRange
from app.services.booking_saga import BookingSaga
from app.services.booking_service import BookingService


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    schema: type[BaseModel]
    summarize: Any = None


class AgentContext:
    """Everything a tool needs that the model is not allowed to choose.

    The user and organization come from the authenticated session. Putting
    them here rather than in the tool schema is what stops a model from
    booking a room as somebody else.
    """

    def __init__(
        self,
        *,
        org_id: UUID,
        user_id: UUID,
        username: str,
        conversation_id: UUID,
        booking: BookingService,
        saga: BookingSaga,
    ) -> None:
        self.org_id = org_id
        self.user_id = user_id
        self.username = username
        self.conversation_id = conversation_id
        self.booking = booking
        self.saga = saga


# --- Result formatting -----------------------------------------------------


def ok(*lines: str) -> str:
    return "\n".join(["Status: success", *lines])


def err(message: str) -> str:
    return "\n".join(["Status: error", f"Message: {message}"])


def _fmt_range(time_range: TimeRange) -> str:
    return f"{time_range.starts_at:%Y-%m-%d %H:%M} to {time_range.ends_at:%H:%M}"


def _reservation_lines(reservation: Reservation) -> list[str]:
    lines = [
        f"Reference: {reservation.reference}",
        f"Room: {reservation.room_name}",
        f"Title: {reservation.title}",
        f"Attendees: {reservation.attendees}",
        f"When: {_fmt_range(reservation.time_range)}",
        f"State: {reservation.state}",
    ]
    if reservation.hold_expires_at is not None:
        lines.append(f"Hold expires at: {reservation.hold_expires_at:%H:%M:%S}")
    return lines


# --- Handlers --------------------------------------------------------------


async def list_rooms(context: AgentContext, _args: BaseModel) -> str:
    rooms = await context.booking.list_rooms(context.org_id)
    if not rooms:
        return ok("Result: This organization has no rooms configured.")
    return ok(
        "Result: Rooms",
        *[f"Room {room.name}: capacity {room.capacity}" for room in rooms],
    )


async def list_my_bookings(context: AgentContext, _args: BaseModel) -> str:
    reservations = await context.booking.list_my_reservations(
        context.user_id, context.org_id
    )
    if not reservations:
        return ok("Result: No active bookings or holds.")

    lines: list[str] = ["Result: Active bookings and holds"]
    for reservation in reservations:
        lines.extend(_reservation_lines(reservation))
    return ok(*lines)


async def list_available_rooms(context: AgentContext, args: BaseModel) -> str:
    assert isinstance(args, ListAvailableRoomsInput)
    time_range = TimeRange(args.starts_at, args.ends_at)
    rooms = await context.booking.list_available_rooms(
        context.org_id, time_range, args.attendees
    )
    header = [
        f"Requested time: {_fmt_range(time_range)}",
        f"Attendees: {args.attendees}",
    ]
    if not rooms:
        return ok("Result: No rooms are free for the entire range.", *header)
    return ok(
        "Result: Rooms free for the entire range",
        *header,
        *[f"Room {room.name}: capacity {room.capacity}" for room in rooms],
    )


async def get_room_schedule(context: AgentContext, args: BaseModel) -> str:
    assert isinstance(args, GetRoomScheduleInput)
    room, taken, free = await context.booking.get_room_schedule(
        context.org_id, args.room, args.date
    )
    return ok(
        "Result: Room schedule",
        f"Room: {room.name}",
        f"Date: {args.date.isoformat()}",
        "Taken:",
        *_range_lines(taken),
        "Free:",
        *_range_lines(free),
    )


def _range_lines(ranges: list[TimeRange]) -> list[str]:
    if not ranges:
        return ["- None"]
    return [
        f"- {time_range.starts_at:%H:%M} to {time_range.ends_at:%H:%M}"
        for time_range in ranges
    ]


async def place_hold(context: AgentContext, args: BaseModel) -> str:
    assert isinstance(args, PlaceHoldInput)
    reservation = await context.booking.place_hold(
        org_id=context.org_id,
        user_id=context.user_id,
        room_name=args.room,
        title=args.title,
        attendees=args.attendees,
        time_range=TimeRange(args.starts_at, args.ends_at),
        conversation_id=context.conversation_id,
    )
    return ok(
        "Result: Room held provisionally. It is NOT booked until confirmed.",
        *_reservation_lines(reservation),
        "Next: read these details back to the user and ask them to confirm. "
        "Then call confirm_booking with this reference.",
    )


async def confirm_booking(context: AgentContext, args: BaseModel) -> str:
    assert isinstance(args, ConfirmBookingInput)
    reservation = await context.booking.get_reservation(
        args.reference, context.org_id, context.user_id
    )
    outcome = await context.saga.confirm_booking(
        reservation_id=reservation.id,
        org_id=context.org_id,
        recipient=context.username,
        conversation_id=context.conversation_id,
    )
    return ok(
        "Result: Booking confirmed",
        *_reservation_lines(outcome.reservation),
        f"Attendees notified: {'yes' if outcome.notified else 'no'}",
        f"Calendar entry: {outcome.calendar_id or 'none'}",
    )


async def cancel_booking(context: AgentContext, args: BaseModel) -> str:
    assert isinstance(args, CancelBookingInput)
    reservation = await context.booking.cancel(
        args.reference, context.org_id, context.user_id
    )
    return ok(
        "Result: Booking cancelled",
        f"Reference: {reservation.reference}",
        f"Room: {reservation.room_name}",
        f"When: {_fmt_range(reservation.time_range)}",
    )


# --- Confirmation summaries ------------------------------------------------


def _summarize_confirm(args: BaseModel) -> str:
    assert isinstance(args, ConfirmBookingInput)
    return f"confirm booking {args.reference}"


def _summarize_cancel(args: BaseModel) -> str:
    assert isinstance(args, CancelBookingInput)
    return f"cancel booking {args.reference}"


# --- Registry --------------------------------------------------------------

TOOLS: dict[str, ToolSpec] = {
    "list_rooms": ToolSpec(
        name="list_rooms",
        description="List every meeting room in the organization with capacity.",
        schema=ListRoomsInput,
    ),
    "list_my_bookings": ToolSpec(
        name="list_my_bookings",
        description=(
            "List the caller's own active bookings and holds, with their "
            "references. Call this before cancelling anything."
        ),
        schema=ListMyBookingsInput,
    ),
    "list_available_rooms": ToolSpec(
        name="list_available_rooms",
        description=(
            "List rooms free for an entire time range and large enough for the "
            "attendees. Use when the user has not chosen a room."
        ),
        schema=ListAvailableRoomsInput,
    ),
    "get_room_schedule": ToolSpec(
        name="get_room_schedule",
        description=(
            "Show the taken and free periods of one room on one day. Use when "
            "the user asks what a specific room looks like."
        ),
        schema=GetRoomScheduleInput,
    ),
    "place_hold": ToolSpec(
        name="place_hold",
        description=(
            "Provisionally hold a room. Reversible: the hold releases itself "
            "if it is not confirmed shortly. Call this as soon as the user has "
            "named a room and a time, before asking them to confirm."
        ),
        schema=PlaceHoldInput,
    ),
    "confirm_booking": ToolSpec(
        name="confirm_booking",
        description=(
            "Turn a hold into a real booking, notify attendees and sync the "
            "calendar. Irreversible from the user's point of view."
        ),
        schema=ConfirmBookingInput,
        summarize=_summarize_confirm,
    ),
    "cancel_booking": ToolSpec(
        name="cancel_booking",
        description="Cancel one of the caller's own bookings by reference.",
        schema=CancelBookingInput,
        summarize=_summarize_cancel,
    ),
}

HANDLERS = {
    "list_rooms": list_rooms,
    "list_my_bookings": list_my_bookings,
    "list_available_rooms": list_available_rooms,
    "get_room_schedule": get_room_schedule,
    "place_hold": place_hold,
    "confirm_booking": confirm_booking,
    "cancel_booking": cancel_booking,
}


def tool_definitions() -> list[dict[str, Any]]:
    """OpenAI/Anthropic-style tool definitions for ``bind_tools``."""
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.schema.model_json_schema(),
            },
        }
        for spec in TOOLS.values()
    ]


def summarize(tool_name: str, args: BaseModel) -> str:
    spec = TOOLS[tool_name]
    if spec.summarize is None:
        return tool_name
    return spec.summarize(args)


__all__ = [
    "HANDLERS",
    "TOOLS",
    "AgentContext",
    "ToolSpec",
    "err",
    "ok",
    "summarize",
    "tool_definitions",
]
