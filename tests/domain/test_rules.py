"""Business rules, at their boundaries."""

from __future__ import annotations

from datetime import timedelta

import pytest

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
from app.domain.rules import (
    validate_booking_horizon,
    validate_business_hours,
    validate_can_be_cancelled,
    validate_max_duration,
    validate_minimum_attendees,
    validate_not_in_the_past,
    validate_room_capacity,
    validate_slot_alignment,
    validate_working_day,
)
from app.domain.time_range import TimeRange
from tests.conftest import BASE_TIME, at

NOW = BASE_TIME  # Monday 2026-10-05 09:00 -03:00


# R1 - not in the past
def test_a_future_booking_is_accepted():
    validate_not_in_the_past(TimeRange(at(10, 0), at(11, 0)), NOW)


def test_a_booking_starting_exactly_now_is_accepted():
    validate_not_in_the_past(TimeRange(NOW, at(10, 0)), NOW)


def test_a_past_booking_is_rejected():
    with pytest.raises(BookingInThePast):
        validate_not_in_the_past(TimeRange(at(8, 0), at(8, 30)), NOW)


# R2 - working days
def test_weekdays_are_accepted():
    validate_working_day(TimeRange(at(10, 0, day=9), at(11, 0, day=9)))  # Friday


@pytest.mark.parametrize("day", [10, 11])  # Saturday, Sunday
def test_weekends_are_rejected(day):
    with pytest.raises(NonWorkingDay):
        validate_working_day(TimeRange(at(10, 0, day=day), at(11, 0, day=day)))


# R3 - business hours
def test_the_exact_opening_and_closing_boundaries_are_accepted():
    validate_business_hours(TimeRange(at(8, 0), at(9, 0)))
    validate_business_hours(TimeRange(at(19, 0), at(20, 0)))


@pytest.mark.parametrize(
    ("start", "end"), [((7, 30), (8, 30)), ((19, 30), (20, 30)), ((23, 0), (23, 30))]
)
def test_times_outside_business_hours_are_rejected(start, end):
    with pytest.raises(OutsideBusinessHours):
        validate_business_hours(TimeRange(at(*start), at(*end)))


def test_a_booking_crossing_midnight_is_rejected():
    with pytest.raises(OutsideBusinessHours):
        validate_business_hours(TimeRange(at(19, 0), at(9, 0, day=6)))


# R4 - slot alignment
def test_aligned_boundaries_are_accepted():
    validate_slot_alignment(TimeRange(at(10, 0), at(10, 30)))


def test_a_misaligned_start_is_rejected():
    with pytest.raises(MisalignedSlot):
        validate_slot_alignment(TimeRange(at(10, 15), at(11, 0)))


# R5 - maximum duration
def test_the_maximum_duration_is_accepted_exactly():
    validate_max_duration(TimeRange(at(10, 0), at(10 + settings.max_booking_hours, 0)))


def test_one_slot_over_the_maximum_is_rejected():
    with pytest.raises(BookingTooLong):
        validate_max_duration(
            TimeRange(at(10, 0), at(10 + settings.max_booking_hours, 30))
        )


# R6 / R7 - attendees and capacity
def test_one_attendee_is_accepted_and_zero_is_not():
    validate_minimum_attendees(1)
    with pytest.raises(InvalidAttendeeCount):
        validate_minimum_attendees(0)


def test_attendees_equal_to_capacity_are_accepted():
    validate_room_capacity(4, 4)


def test_attendees_above_capacity_are_rejected_with_the_numbers():
    with pytest.raises(RoomCapacityExceeded) as raised:
        validate_room_capacity(5, 4)
    assert "holds 4" in str(raised.value)


# R12 - booking horizon
def test_the_horizon_boundary_is_accepted():
    latest = NOW.replace(hour=10) + timedelta(
        days=settings.max_booking_horizon_days - 1
    )
    validate_booking_horizon(TimeRange(latest, latest + timedelta(hours=1)), NOW)


def test_beyond_the_horizon_is_rejected():
    far = NOW + timedelta(days=settings.max_booking_horizon_days + 1)
    with pytest.raises(BookingHorizonExceeded):
        validate_booking_horizon(TimeRange(far, far + timedelta(hours=1)), NOW)


# R10 - cancellation cutoff
def test_cancelling_before_the_start_is_accepted():
    validate_can_be_cancelled(at(10, 0), NOW)


def test_cancelling_at_or_after_the_start_is_rejected():
    with pytest.raises(BookingAlreadyStarted):
        validate_can_be_cancelled(NOW, NOW)
    with pytest.raises(BookingAlreadyStarted):
        validate_can_be_cancelled(at(8, 0), NOW)
