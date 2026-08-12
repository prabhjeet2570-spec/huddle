"""The reservation aggregate.

One table models both holds and confirmed bookings. A hold is a reservation in
state ``held`` with a ``hold_expires_at`` deadline; confirming it is a state
transition, not a second row. That is what makes "the slot was taken between
hold and confirm" impossible: the row that occupies the room is the same row
throughout.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.enums import ReservationState
from app.domain.time_range import TimeRange


@dataclass(frozen=True, slots=True)
class Reservation:
    id: UUID
    reference: str
    org_id: UUID
    room_id: UUID
    room_name: str
    user_id: UUID
    title: str
    attendees: int
    time_range: TimeRange
    state: ReservationState
    hold_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_blocking(self) -> bool:
        return self.state in ReservationState.blocking()

    def is_expired_at(self, now: datetime) -> bool:
        return (
            self.state is ReservationState.HELD
            and self.hold_expires_at is not None
            and self.hold_expires_at <= now
        )

    def seconds_until_expiry(self, now: datetime) -> float:
        if self.hold_expires_at is None:
            return 0.0
        return max(0.0, (self.hold_expires_at - now).total_seconds())
