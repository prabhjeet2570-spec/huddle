"""Booking rules.

These are pure functions over values and an injected ``now``. Nothing here
touches a database or a clock of its own, which is what lets the whole rule
surface be tested at its boundaries without fixtures.
"""

from calendar import SATURDAY, SUNDAY
from datetime import datetime, timedelta

from app.config import settings
from app.domain.exceptions import (
    BookingAlreadyStarted,
    BookingHorizonExceeded,
    BookingInThePast,
    BookingTooLong,
    InvalidAttendeeCount,
    MisalignedSlot,
    NonWorkingDay,
    OutsideBusinessHours,
    RoomCapacityExceeded,
)
from app.domain.time_range import TimeRange


def validate_not_in_the_past(time_range: TimeRange, now: datetime) -> None:
    if time_range.starts_at < now:
        raise BookingInThePast(
            f"Choose a start time at or after {now.isoformat(timespec='minutes')}; "
            f"the requested start was "
            f"{time_range.starts_at.isoformat(timespec='minutes')}."
        )


def validate_working_day(time_range: TimeRange) -> None:
    if time_range.starts_at.weekday() in (SATURDAY, SUNDAY):
        raise NonWorkingDay(
            "Bookings are only available Monday through Friday. Choose a weekday."
        )


def validate_business_hours(time_range: TimeRange) -> None:
    crosses_midnight = time_range.starts_at.date() != time_range.ends_at.date()
    if (
        crosses_midnight
        or time_range.starts_at.time() < settings.business_start
        or time_range.ends_at.time() > settings.business_end
    ):
        raise OutsideBusinessHours(
            f"Choose a time between {settings.business_start:%H:%M} and "
            f"{settings.business_end:%H:%M} on the same day."
        )


def validate_slot_alignment(time_range: TimeRange) -> None:
    if not time_range.is_slot_aligned():
        raise MisalignedSlot(
            f"Choose start and end times on {settings.slot_minutes}-minute "
            "boundaries with no seconds or microseconds."
        )


def validate_max_duration(time_range: TimeRange) -> None:
    if time_range.duration > timedelta(hours=settings.max_booking_hours):
        raise BookingTooLong(
            f"Bookings can last at most {settings.max_booking_hours} hours. "
            "Choose a shorter time range."
        )


def validate_minimum_attendees(attendees: int) -> None:
    if attendees < settings.min_attendees:
        raise InvalidAttendeeCount(
            f"A booking needs at least {settings.min_attendees} attendee. "
            f"The requested count was {attendees}."
        )


def validate_room_capacity(attendees: int, capacity: int) -> None:
    if attendees > capacity:
        raise RoomCapacityExceeded(
            f"This room holds {capacity} attendees, but {attendees} were "
            "requested. Choose a larger room or reduce the attendee count."
        )


def validate_booking_horizon(time_range: TimeRange, now: datetime) -> None:
    latest_start = now + timedelta(days=settings.max_booking_horizon_days)
    if time_range.starts_at > latest_start:
        raise BookingHorizonExceeded(
            f"Bookings can start at most {settings.max_booking_horizon_days} "
            f"days ahead. Choose a start at or before "
            f"{latest_start.isoformat(timespec='minutes')}."
        )


def validate_can_be_cancelled(starts_at: datetime, now: datetime) -> None:
    if starts_at <= now:
        raise BookingAlreadyStarted(
            f"This booking started at {starts_at.isoformat(timespec='minutes')} "
            "and can no longer be cancelled. Cancel bookings before they start."
        )


def validate_booking_request(
    time_range: TimeRange,
    attendees: int,
    now: datetime,
) -> None:
    """Run every rule that does not need a room. Ordered cheapest first."""
    validate_slot_alignment(time_range)
    validate_max_duration(time_range)
    validate_working_day(time_range)
    validate_business_hours(time_range)
    validate_not_in_the_past(time_range, now)
    validate_booking_horizon(time_range, now)
    validate_minimum_attendees(attendees)
