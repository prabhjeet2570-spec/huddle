from datetime import datetime

from app.config import settings
from app.domain.time_range import TimeRange


def merge_contiguous(slots: list[datetime]) -> list[TimeRange]:
    """Merge 30-minute slot starts into contiguous occupied ranges."""
    if not slots:
        return []

    slot_duration = settings.slot_delta
    ordered_slots = sorted(set(slots))
    ranges: list[TimeRange] = []
    current_start = ordered_slots[0]
    current_end = current_start + slot_duration

    for slot_start in ordered_slots[1:]:
        # A slot starting at the current end extends the same half-open range.
        if slot_start == current_end:
            current_end += slot_duration
            continue

        ranges.append(TimeRange(current_start, current_end))
        current_start = slot_start
        current_end = slot_start + slot_duration

    ranges.append(TimeRange(current_start, current_end))
    return ranges


def derive_free_ranges(
    taken_ranges: list[TimeRange],
    business_hours: TimeRange,
) -> list[TimeRange]:
    """Return the gaps left inside business hours by the taken ranges."""
    free_ranges: list[TimeRange] = []
    next_free_start = business_hours.starts_at

    for taken_range in sorted(
        taken_ranges,
        key=lambda time_range: time_range.starts_at,
    ):
        if not taken_range.overlaps(business_hours):
            continue

        taken_start = max(
            taken_range.starts_at,
            business_hours.starts_at,
        )
        taken_end = min(taken_range.ends_at, business_hours.ends_at)

        if next_free_start < taken_start:
            free_ranges.append(TimeRange(next_free_start, taken_start))

        next_free_start = max(next_free_start, taken_end)

        if next_free_start >= business_hours.ends_at:
            break

    if next_free_start < business_hours.ends_at:
        free_ranges.append(TimeRange(next_free_start, business_hours.ends_at))

    return free_ranges
