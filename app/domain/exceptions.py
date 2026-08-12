"""Domain errors.

Every error carries a ``retryable`` flag. That flag is what the retry policy
in :mod:`app.reliability.retry` reads to decide whether another attempt could
plausibly succeed. Domain errors are terminal by definition: a booking outside
business hours will still be outside business hours on the third attempt.
"""

from datetime import datetime


class HuddleError(Exception):
    """Base class for every error the application raises deliberately."""

    #: Whether retrying the same call could plausibly produce a different
    #: outcome. Read by the retry policy; never guessed at the call site.
    retryable: bool = False

    @property
    def code(self) -> str:
        return type(self).__name__


class DomainError(HuddleError):
    """A business rule rejected the request. Retrying will not help."""

    retryable = False


class TransientError(HuddleError):
    """An infrastructure failure that another attempt might survive."""

    retryable = True


class ToolTimeout(TransientError):
    """A tool call exceeded its deadline."""


class UpstreamUnavailable(TransientError):
    """A dependency answered with a 5xx or refused the connection."""


class MalformedToolResponse(TransientError):
    """A dependency answered with a payload that could not be parsed."""


# --- Booking rules ---------------------------------------------------------


class InvalidTimeRange(DomainError):
    """Raised when a time range does not end after it starts."""


class BookingInThePast(DomainError):
    """Raised when a booking starts before the current time."""


class NonWorkingDay(DomainError):
    """Raised when a booking falls outside Monday through Friday."""


class OutsideBusinessHours(DomainError):
    """Raised when a booking falls outside office opening hours."""


class MisalignedSlot(DomainError):
    """Raised when booking boundaries do not align with the slot grid."""


class BookingTooLong(DomainError):
    """Raised when a booking exceeds the maximum duration."""


class InvalidAttendeeCount(DomainError):
    """Raised when a booking has fewer than the minimum attendees."""


class RoomCapacityExceeded(DomainError):
    """Raised when a room cannot hold all requested attendees."""


class BookingHorizonExceeded(DomainError):
    """Raised when a booking is requested too far in advance."""


class BookingAlreadyStarted(DomainError):
    """Raised when cancellation is attempted at or after the start time."""


class ReservationNotFound(DomainError):
    """Raised when a reservation is missing or belongs to somebody else."""

    def __init__(self, reservation_id: object) -> None:
        self.reservation_id = reservation_id
        super().__init__(
            f"Reservation {reservation_id} was not found among your "
            "reservations. Check the reference and try again."
        )


class RoomNotFound(DomainError):
    """Raised when a requested room does not exist in this organization."""

    def __init__(self, room_name: str) -> None:
        self.room_name = room_name
        super().__init__(
            f"Room {room_name} does not exist. Choose one of the listed rooms."
        )


class RoomNotAvailable(DomainError):
    """Raised when the room is held or booked for part of the requested range.

    This is the error the PostgreSQL exclusion constraint produces. It is the
    single source of truth for contention; nothing in the application decides
    availability by reading first.
    """

    def __init__(
        self,
        room_name: str | None = None,
        conflict_start: datetime | None = None,
        conflict_end: datetime | None = None,
        alternative_rooms: list[str] | None = None,
    ) -> None:
        self.room_name = room_name
        self.conflict_start = conflict_start
        self.conflict_end = conflict_end
        self.alternative_rooms = list(alternative_rooms or [])

        label = f"Room {room_name}" if room_name else "The selected room"
        message = f"{label} is not available for the full time range."

        if conflict_start and conflict_end:
            message += (
                f" It is taken from {conflict_start.isoformat(timespec='minutes')}"
                f" to {conflict_end.isoformat(timespec='minutes')}."
            )

        if self.alternative_rooms:
            message += (
                f" Rooms free for the whole range: {', '.join(self.alternative_rooms)}."
            )
        elif alternative_rooms is not None:
            message += " No other room with enough capacity is free for the full range."

        super().__init__(message)


class HoldExpired(DomainError):
    """Raised when confirming a hold whose TTL already elapsed."""

    def __init__(self, reservation_id: object) -> None:
        self.reservation_id = reservation_id
        super().__init__(
            f"The hold {reservation_id} expired before it was confirmed. "
            "The room was released. Place a new hold to try again."
        )


# --- Guardrails ------------------------------------------------------------


class GuardrailError(HuddleError):
    """Base class for refusals produced by the guardrail layer."""

    retryable = False


class BudgetExceeded(GuardrailError):
    """Raised when a conversation runs past its tool-call or token budget."""

    def __init__(self, resource: str, used: int, limit: int) -> None:
        self.resource = resource
        self.used = used
        self.limit = limit
        super().__init__(
            f"This conversation reached its {resource} budget "
            f"({used}/{limit}). A human needs to take over."
        )


class ConfirmationRequired(GuardrailError):
    """Raised when a high blast-radius action was attempted unconfirmed."""

    def __init__(self, tool_name: str, summary: str) -> None:
        self.tool_name = tool_name
        self.summary = summary
        super().__init__(
            f"{tool_name} changes shared state and needs explicit confirmation. "
            f"Pending action: {summary}"
        )


class HoldRateLimited(GuardrailError):
    """Raised when hold-cycling detection rate-limits a user."""

    def __init__(self, retry_at: datetime, evidence: str) -> None:
        self.retry_at = retry_at
        self.evidence = evidence
        super().__init__(
            "Too many unconfirmed holds were placed recently, so new holds are "
            f"paused until {retry_at.isoformat(timespec='seconds')}. {evidence}"
        )


class PermissionDenied(GuardrailError):
    """Raised when a caller reaches for another organization's data."""


class ModelNotConfigured(HuddleError):
    """Raised when no language model credentials are configured.

    Terminal by definition: retrying cannot conjure an API key. Separated from
    a provider outage so the message can tell the operator what to actually do.
    """

    retryable = False

    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(
            f"No API key is configured for the '{provider}' provider. Set "
            "HUDDLE_LLM_API_KEY to enable the chat agent. Everything else - "
            "the REST API, the booking rules and the whole test suite - works "
            "without one."
        )
