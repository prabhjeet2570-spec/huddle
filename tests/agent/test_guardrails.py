"""Guardrails, tested through the executor rather than through the prompt.

Everything here holds regardless of what the model was told, which is the
point: the system prompt asks nicely, and these are what happen when asking
is not enough.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.agent.executor import ToolExecutor
from app.agent.tools import HANDLERS, AgentContext
from app.domain.enums import ActionRisk, AlertKind, ReservationState, ToolCallStatus
from app.domain.time_range import TimeRange
from app.infrastructure.models import (
    AlertModel,
    PendingConfirmationModel,
    ReservationModel,
)
from app.infrastructure.repositories.abuse_repository import AbuseRepository
from app.infrastructure.repositories.conversation_repository import (
    ConversationRepository,
)
from app.infrastructure.repositories.reservation_repository import (
    ReservationRepository,
)
from app.infrastructure.repositories.telemetry_repository import TelemetryRepository
from app.reliability.budget import Budget
from app.services.abuse_detection import HoldCyclingDetector
from app.services.booking_saga import BookingSaga
from app.services.booking_service import BookingService
from app.services.calendar_sync import CalendarService
from app.services.notifications import NotificationService
from tests.conftest import at, requires_postgres

pytestmark = [requires_postgres, pytest.mark.postgres]


async def _executor(session, tenant, clock, budget: Budget | None = None):
    repository = ReservationRepository(session)
    telemetry = TelemetryRepository(session)
    conversations = ConversationRepository(session)
    detector = HoldCyclingDetector(AbuseRepository(session), telemetry)
    booking = BookingService(repository, detector, clock)
    saga = BookingSaga(
        booking, NotificationService(session), CalendarService(session), telemetry
    )
    conversation = await conversations.create(tenant.org_id, tenant.user_id)
    context = AgentContext(
        org_id=tenant.org_id,
        user_id=tenant.user_id,
        username="alice",
        conversation_id=conversation.id,
        booking=booking,
        saga=saga,
    )
    executor = ToolExecutor(
        context,
        conversations,
        telemetry,
        budget or Budget.from_settings(),
        HANDLERS,
    )
    return executor, booking, conversation


def _hold_args(hour: int = 10, room: str = "A") -> dict:
    return {
        "room": room,
        "starts_at": at(hour, 0).isoformat(),
        "ends_at": at(hour + 1, 0).isoformat(),
        "title": "Design review",
        "attendees": 3,
    }


# --- Blast radius ----------------------------------------------------------


async def test_reads_execute_without_confirmation(session, tenant, clock):
    executor, _, _ = await _executor(session, tenant, clock)

    result = await executor.execute("list_rooms", {})

    assert result.status is ToolCallStatus.OK
    assert result.risk is ActionRisk.READ
    assert "Room A" in result.content


async def test_holds_execute_without_confirmation(session, tenant, clock):
    """A hold is reversible by construction, so the agent may place one freely."""
    executor, _, _ = await _executor(session, tenant, clock)

    result = await executor.execute("place_hold", _hold_args())

    assert result.status is ToolCallStatus.OK
    assert result.awaiting_confirmation is False
    assert "NOT booked until confirmed" in result.content


async def test_confirmation_is_parked_and_not_executed(session, tenant, clock):
    executor, _booking, conversation = await _executor(session, tenant, clock)
    hold = await executor.execute("place_hold", _hold_args())
    reference = _field(hold.content, "Reference")

    result = await executor.execute("confirm_booking", {"reference": reference})

    assert result.awaiting_confirmation is True
    assert result.status is ToolCallStatus.BLOCKED
    # The decisive assertion: the reservation was NOT confirmed.
    state = await session.scalar(
        select(ReservationModel.state).where(ReservationModel.reference == reference)
    )
    assert state == ReservationState.HELD

    pending = await session.scalar(
        select(PendingConfirmationModel).where(
            PendingConfirmationModel.conversation_id == conversation.id
        )
    )
    assert pending.tool_name == "confirm_booking"
    assert pending.status == "pending"


async def test_cancellation_is_parked_and_not_executed(session, tenant, clock):
    executor, _, _ = await _executor(session, tenant, clock)
    hold = await executor.execute("place_hold", _hold_args())
    reference = _field(hold.content, "Reference")

    result = await executor.execute("cancel_booking", {"reference": reference})

    assert result.awaiting_confirmation is True
    state = await session.scalar(
        select(ReservationModel.state).where(ReservationModel.reference == reference)
    )
    assert state == ReservationState.HELD


async def test_a_preconfirmed_high_risk_call_executes(session, tenant, clock):
    """Confirmation replays the stored arguments; the gate opens exactly once."""
    executor, _, _ = await _executor(session, tenant, clock)
    hold = await executor.execute("place_hold", _hold_args())
    reference = _field(hold.content, "Reference")

    result = await executor.execute(
        "confirm_booking", {"reference": reference}, preconfirmed=True
    )

    assert result.status is ToolCallStatus.OK
    state = await session.scalar(
        select(ReservationModel.state).where(ReservationModel.reference == reference)
    )
    assert state == ReservationState.CONFIRMED


# --- Argument validation ---------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "expected_field"),
    [
        ({**_hold_args(), "attendees": 0}, "attendees"),
        ({**_hold_args(), "starts_at": "not-a-datetime"}, "starts_at"),
        ({**_hold_args(), "title": ""}, "title"),
        ({k: v for k, v in _hold_args().items() if k != "room"}, "room"),
    ],
)
async def test_invalid_arguments_are_rejected_before_the_tool_runs(
    session, tenant, clock, arguments, expected_field
):
    executor, _, _ = await _executor(session, tenant, clock)

    result = await executor.execute("place_hold", arguments)

    assert result.status is ToolCallStatus.INVALID_ARGUMENTS
    assert expected_field in result.content
    # The model is told what to fix, so it can re-prompt itself.
    assert "call the tool again" in result.content
    assert (await session.scalar(select(ReservationModel.id))) is None


async def test_a_misaligned_slot_is_rejected_by_the_schema(session, tenant, clock):
    executor, _, _ = await _executor(session, tenant, clock)

    result = await executor.execute(
        "place_hold", {**_hold_args(), "starts_at": at(10, 15).isoformat()}
    )

    assert result.status is ToolCallStatus.INVALID_ARGUMENTS
    assert "30-minute boundary" in result.content


async def test_a_hallucinated_tool_name_is_rejected(session, tenant, clock):
    executor, _, _ = await _executor(session, tenant, clock)

    result = await executor.execute("delete_all_bookings", {"confirm": True})

    assert result.status is ToolCallStatus.INVALID_ARGUMENTS
    assert "no tool called" in result.content
    assert "place_hold" in result.content  # tells the model what does exist


# --- Budgets ---------------------------------------------------------------


async def test_a_breached_tool_budget_blocks_and_escalates(session, tenant, clock):
    budget = Budget(max_tool_calls=2, max_tokens=10_000)
    executor, _, conversation = await _executor(session, tenant, clock, budget)

    await executor.execute("list_rooms", {})
    await executor.execute("list_rooms", {})
    result = await executor.execute("list_rooms", {})

    assert result.escalated is True
    assert result.status is ToolCallStatus.BLOCKED
    alert = await session.scalar(
        select(AlertModel).where(AlertModel.kind == AlertKind.BUDGET_EXCEEDED)
    )
    assert alert is not None
    assert alert.conversation_id == conversation.id


async def test_invalid_calls_still_consume_budget(session, tenant, clock):
    """Otherwise a model emitting only malformed calls loops for free."""
    budget = Budget(max_tool_calls=3, max_tokens=10_000)
    executor, _, _ = await _executor(session, tenant, clock, budget)

    for _ in range(3):
        await executor.execute("nonexistent_tool", {})

    assert budget.breach() is not None


# --- Domain refusals -------------------------------------------------------


async def test_a_domain_refusal_is_reported_not_retried(session, tenant, clock):
    executor, _, _ = await _executor(session, tenant, clock)

    result = await executor.execute("place_hold", _hold_args(room="Z"))

    assert result.status is ToolCallStatus.DOMAIN_ERROR
    assert result.attempts == 1  # terminal failures are never retried
    assert "Room Z does not exist" in result.content


async def test_a_taken_room_reports_the_alternatives(session, tenant, clock):
    executor, booking, _ = await _executor(session, tenant, clock)
    await booking.place_hold(
        org_id=tenant.org_id,
        user_id=tenant.other_user_id,
        room_name="A",
        title="Someone else",
        attendees=2,
        time_range=TimeRange(at(10, 0), at(11, 0)),
    )

    result = await executor.execute("place_hold", _hold_args())

    assert result.status is ToolCallStatus.DOMAIN_ERROR
    assert "not available" in result.content
    assert "B" in result.content


# --- Tenant isolation ------------------------------------------------------


async def test_one_tenant_cannot_see_or_touch_anothers_booking(
    session, tenant, other_tenant, clock
):
    ours, _, _ = await _executor(session, tenant, clock)
    theirs, _, _ = await _executor(session, other_tenant, clock)

    hold = await theirs.execute("place_hold", _hold_args())
    reference = _field(hold.content, "Reference")

    result = await ours.execute(
        "cancel_booking", {"reference": reference}, preconfirmed=True
    )

    assert result.status is ToolCallStatus.DOMAIN_ERROR
    assert "was not found" in result.content
    state = await session.scalar(
        select(ReservationModel.state).where(ReservationModel.reference == reference)
    )
    assert state == ReservationState.HELD


def _field(content: str, key: str) -> str:
    for line in content.splitlines():
        if line.startswith(f"{key}: "):
            return line.split(": ", 1)[1]
    raise AssertionError(f"{key} not found in:\n{content}")
