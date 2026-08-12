"""Merging occupied slots and deriving the gaps between them."""

from __future__ import annotations

from app.domain.schedule import derive_free_ranges, merge_contiguous
from app.domain.time_range import TimeRange
from tests.conftest import at

BUSINESS = TimeRange(at(8, 0), at(20, 0))


def test_no_slots_merge_to_nothing():
    assert merge_contiguous([]) == []


def test_contiguous_slots_merge_into_one_range():
    merged = merge_contiguous([at(10, 0), at(10, 30), at(11, 0)])

    assert merged == [TimeRange(at(10, 0), at(11, 30))]


def test_separated_slots_stay_separate():
    merged = merge_contiguous([at(10, 0), at(14, 0)])

    assert merged == [
        TimeRange(at(10, 0), at(10, 30)),
        TimeRange(at(14, 0), at(14, 30)),
    ]


def test_duplicate_and_unordered_slots_are_normalised():
    merged = merge_contiguous([at(10, 30), at(10, 0), at(10, 30)])

    assert merged == [TimeRange(at(10, 0), at(11, 0))]


def test_an_empty_day_is_free_all_day():
    assert derive_free_ranges([], BUSINESS) == [BUSINESS]


def test_gaps_are_derived_around_a_booking():
    free = derive_free_ranges([TimeRange(at(10, 0), at(11, 30))], BUSINESS)

    assert free == [TimeRange(at(8, 0), at(10, 0)), TimeRange(at(11, 30), at(20, 0))]


def test_a_booking_at_the_edge_produces_one_gap():
    free = derive_free_ranges([TimeRange(at(8, 0), at(9, 0))], BUSINESS)

    assert free == [TimeRange(at(9, 0), at(20, 0))]


def test_a_fully_booked_day_has_no_gaps():
    assert derive_free_ranges([BUSINESS], BUSINESS) == []


def test_adjacent_bookings_do_not_produce_a_zero_length_gap():
    free = derive_free_ranges(
        [TimeRange(at(10, 0), at(11, 0)), TimeRange(at(11, 0), at(12, 0))], BUSINESS
    )

    assert free == [TimeRange(at(8, 0), at(10, 0)), TimeRange(at(12, 0), at(20, 0))]
