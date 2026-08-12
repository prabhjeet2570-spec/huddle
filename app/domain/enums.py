"""Enumerations shared by the domain, the persistence layer and the agent."""

from enum import StrEnum


class ReservationState(StrEnum):
    """Lifecycle of a reservation row.

    ``HELD`` and ``CONFIRMED`` are the two states that occupy a room. The
    PostgreSQL exclusion constraint is defined over exactly that pair, so
    releasing a room is always a state transition and never a delete.
    """

    HELD = "held"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @classmethod
    def blocking(cls) -> tuple["ReservationState", ...]:
        return (cls.HELD, cls.CONFIRMED)


class ActionRisk(StrEnum):
    """Blast radius of a tool call.

    ``READ`` and ``HOLD`` are reversible: a read changes nothing and a hold
    releases itself when its TTL elapses. ``HIGH`` covers everything that a
    human would have to undo by hand, so it is gated behind explicit user
    confirmation.
    """

    READ = "read"
    HOLD = "hold"
    HIGH = "high"


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    ESCALATED = "escalated"
    FAILED = "failed"
    BLOCKED = "blocked"

    @classmethod
    def terminal(cls) -> tuple["ConversationStatus", ...]:
        return (cls.COMPLETED, cls.ESCALATED, cls.FAILED, cls.BLOCKED)


class SagaStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    COMPENSATED = "compensated"
    COMPENSATION_FAILED = "compensation_failed"


class StepPhase(StrEnum):
    EXECUTE = "execute"
    COMPENSATE = "compensate"


class StepStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: A compensation that found nothing to undo. Proof of idempotency.
    NOOP = "noop"


class ToolCallStatus(StrEnum):
    OK = "ok"
    #: The tool ran and the domain refused the request (terminal failure).
    DOMAIN_ERROR = "domain_error"
    #: Arguments failed Pydantic validation before the tool ran.
    INVALID_ARGUMENTS = "invalid_arguments"
    #: A guardrail stopped the call: budget breach or missing confirmation.
    BLOCKED = "blocked"
    #: Retries were exhausted against a retryable failure.
    ESCALATED = "escalated"


class HoldEvent(StrEnum):
    CREATED = "created"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    RELEASED = "released"


class AlertKind(StrEnum):
    TOOL_RETRIES_EXHAUSTED = "tool_retries_exhausted"
    BUDGET_EXCEEDED = "budget_exceeded"
    COMPENSATION_FAILED = "compensation_failed"
    HOLD_CYCLING_DETECTED = "hold_cycling_detected"
    AGENT_LOOP_DETECTED = "agent_loop_detected"
