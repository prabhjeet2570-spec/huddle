"""Half-open time ranges, ``[starts_at, ends_at)``.

The end boundary is excluded, so a booking that ends at 11:30 leaves the room
free from 11:30. This matches the semantics of the PostgreSQL ``tstzrange``
literal ``'[)'`` used by the exclusion constraint, which is why the two never
disagree about whether two meetings touch or overlap.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config import settings
from app.domain.exceptions import InvalidTimeRange


@dataclass(frozen=True, slots=True)
class TimeRange:
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        if self.ends_at <= self.starts_at:
            raise InvalidTimeRange("ends_at must be after starts_at")
        if (self.starts_at.tzinfo is None) != (self.ends_at.tzinfo is None):
            raise InvalidTimeRange("both boundaries must share awareness")

    @property
    def duration(self) -> timedelta:
        return self.ends_at - self.starts_at

    def is_slot_aligned(self) -> bool:
        return self._is_boundary_aligned(self.starts_at) and self._is_boundary_aligned(
            self.ends_at
        )

    def overlaps(self, other: "TimeRange") -> bool:
        # Half-open ranges may touch at a boundary without overlapping.
        return self.starts_at < other.ends_at and other.starts_at < self.ends_at

    def contains(self, moment: datetime) -> bool:
        return self.starts_at <= moment < self.ends_at

    def slot_starts(self) -> list[datetime]:
        slots: list[datetime] = []
        current = self.starts_at
        while current < self.ends_at:
            slots.append(current)
            current += settings.slot_delta
        return slots

    @staticmethod
    def _is_boundary_aligned(boundary: datetime) -> bool:
        return (
            boundary.minute % settings.slot_minutes == 0
            and boundary.second == 0
            and boundary.microsecond == 0
        )
