"""Half-open interval semantics, `[starts_at, ends_at)`."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain.exceptions import InvalidTimeRange
from app.domain.time_range import TimeRange
from tests.conftest import at


def test_a_range_must_end_after_it_starts():
    with pytest.raises(InvalidTimeRange):
        TimeRange(at(10, 0), at(10, 0))
    with pytest.raises(InvalidTimeRange):
        TimeRange(at(11, 0), at(10, 0))


def test_duration_is_the_difference():
    assert TimeRange(at(10, 0), at(11, 30)).duration == timedelta(minutes=90)


@pytest.mark.parametrize(
    ("start", "end", "aligned"),
    [
        ((10, 0), (11, 0), True),
        ((10, 30), (11, 30), True),
        ((10, 15), (11, 0), False),
        ((10, 0), (11, 15), False),
    ],
)
def test_slot_alignment(start, end, aligned):
    assert TimeRange(at(*start), at(*end)).is_slot_aligned() is aligned


def test_touching_ranges_do_not_overlap():
    """The rule the whole booking model rests on: 11:00 frees the room."""
    morning = TimeRange(at(10, 0), at(11, 0))
    midday = TimeRange(at(11, 0), at(12, 0))

    assert morning.overlaps(midday) is False
    assert midday.overlaps(morning) is False


@pytest.mark.parametrize(
    ("other_start", "other_end"),
    [((10, 30), (11, 30)), ((9, 30), (10, 30)), ((10, 0), (11, 0)), ((9, 0), (12, 0))],
)
def test_genuine_overlaps_are_detected(other_start, other_end):
    base = TimeRange(at(10, 0), at(11, 0))
    assert base.overlaps(TimeRange(at(*other_start), at(*other_end))) is True


def test_containment_excludes_the_end_boundary():
    window = TimeRange(at(10, 0), at(11, 0))

    assert window.contains(at(10, 0)) is True
    assert window.contains(at(10, 30)) is True
    assert window.contains(at(11, 0)) is False


def test_slot_starts_enumerate_the_grid_without_the_end():
    slots = TimeRange(at(10, 0), at(11, 30)).slot_starts()

    assert slots == [at(10, 0), at(10, 30), at(11, 0)]
