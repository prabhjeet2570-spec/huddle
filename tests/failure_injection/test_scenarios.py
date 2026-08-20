"""The failure-injection suite.

Each scenario is injected deterministically at a named call site and the
assertion is always the same: whatever failed, the database is left in a state
a human would call clean. No orphaned holds, no notifications for bookings
that do not exist, no calendar entries without a reservation.

This is the suite CI runs to prove the reliability machinery is load-bearing
rather than decorative.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.domain.enums import ReservationState, ToolCallStatus
from app.domain.exceptions import ToolTimeout
from app.domain.time_range import TimeRange
from app.infrastructure.models import (
    AlertModel,
    CalendarEntryModel,
    NotificationModel,
    ReservationModel,
)
from app.infrastructure.repositories.abuse_repository import AbuseRepository
from app.infrastructure.repositories.reservation_repository import (
    ReservationRepository,
)
from app.infrastructure.repositories.telemetry_repository import TelemetryRepository
from app.reliability.failure_injection import Scenario, injected, injector
from app.reliability.saga import SagaFailed
from app.services.abuse_detection import HoldCyclingDetector
from app.services.booking_saga import BookingSaga
from app.services.booking_service import BookingService
from app.services.calendar_sync import CalendarService
from app.services.notifications import NotificationService
from tests.conftest import at, requires_postgres

pytestmark = [requires_postgres, pytest.mark.postgres]


def _build(session, clock):
    repository = ReservationRepository(session)
    telemetry = TelemetryRepository(session)
    detector = HoldCyclingDetector(AbuseRepository(session), telemetry)
    booking = BookingService(repository, detector, clock)
    saga = BookingSaga(
        booking, NotificationService(session), CalendarService(session), telemetry
    )
    return booking, saga


async def _hold(booking, tenant, hour: int = 10):
    return await booking.place_hold(
        org_id=tenant.org_id,
        user_id=tenant.user_id,
        room_name="A",
        title="Injected failure test",
        attendees=3,
        time_range=TimeRange(at(hour, 0), at(hour + 1, 0)),
    )


async def _assert_clean(session, reservation_id) -> None:
    """No half-finished booking anywhere in the database."""
    state = await session.scalar(
        select(ReservationModel.state).where(ReservationModel.id == reservation_id)
    )
    assert state in (
        ReservationState.CANCELLED,
        ReservationState.EXPIRED,
    ), f"reservation left in state {state!r}"

    live_notifications = await session.scalar(
        select(func.count())
        .select_from(NotificationModel)
        .where(
            NotificationModel.reservation_id == reservation_id,
            NotificationModel.status == "sent",
        )
    )
    assert live_notifications == 0, "attendees notified about a booking that was undone"

    calendar_entries = await session.scalar(
        select(func.count())
        .select_from(CalendarEntryModel)
        .where(CalendarEntryModel.reservation_id == reservation_id)
    )
    assert calendar_entries == 0, "calendar entry survived the rollback"


# --- Determinism -----------------------------------------------------------


def test_the_injector_is_deterministic_and_targeted():
    """Chaos you cannot reproduce is not a test."""
    with injected(Scenario.TOOL_TIMEOUT, "some.target", on_calls=(2,)):
        injector.maybe_fail("some.target")  # call 1: passes
        with pytest.raises(ToolTimeout):
            injector.maybe_fail("some.target")  # call 2: fails
        injector.maybe_fail("some.target")  # call 3: passes
        injector.maybe_fail("another.target")  # untargeted: always passes


def test_the_injector_is_inert_when_disabled(monkeypatch):
    """It must be impossible to arm this in production."""
    from app.config import settings

    monkeypatch.setattr(settings, "failure_injection_enabled", False)
    with injected(Scenario.TOOL_SERVER_ERROR, "some.target"):
        injector.maybe_fail("some.target")  # no exception


# --- One scenario per step -------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "target"),
    [
        (Scenario.NOTIFICATION_FAILURE, "notification.send"),
        (Scenario.TOOL_TIMEOUT, "notification.send"),
        (Scenario.MALFORMED_RESPONSE, "notification.send"),
        (Scenario.CALENDAR_FAILURE, "calendar.create"),
        (Scenario.TOOL_TIMEOUT, "calendar.create"),
        (Scenario.TOOL_SERVER_ERROR, "calendar.create"),
        (Scenario.MALFORMED_RESPONSE, "calendar.create"),
    ],
)
async def test_every_injected_step_failure_leaves_state_clean(
    session, tenant, clock, scenario, target
):
    booking, saga = _build(session, clock)
    reservation = await _hold(booking, tenant)

    with injected(scenario, target), pytest.raises(SagaFailed):
        await saga.confirm_booking(
            reservation_id=reservation.id, org_id=tenant.org_id, recipient="alice"
        )

    await _assert_clean(session, reservation.id)


async def test_a_transient_failure_that_clears_is_retried_and_succeeds(
    session, tenant, clock
):
    """The other half of the contract: retryable failures do not abort a booking."""
    booking, saga = _build(session, clock)
    reservation = await _hold(booking, tenant)

    # Fail the first attempt only; the retry policy should carry it through.
    with injected(Scenario.TOOL_SERVER_ERROR, "calendar.create", on_calls=(1,)):
        outcome = await saga.confirm_booking(
            reservation_id=reservation.id, org_id=tenant.org_id, recipient="alice"
        )

    assert outcome.reservation.state is ReservationState.CONFIRMED
    assert outcome.calendar_id is not None


# --- Slot taken between hold and confirm -----------------------------------


async def test_a_lapsed_hold_cannot_be_confirmed(session, tenant, clock):
    """The 'slot taken between hold and confirm' scenario.

    It cannot happen while the hold is live, because the hold row is what
    blocks the room. The reachable failure is the hold lapsing first, and that
    must be refused rather than silently double-booking.
    """
    booking, saga = _build(session, clock)
    reservation = await _hold(booking, tenant)

    clock.advance(settings.hold_ttl_seconds + 5)

    with pytest.raises(SagaFailed) as raised:
        await saga.confirm_booking(
            reservation_id=reservation.id, org_id=tenant.org_id, recipient="alice"
        )

    assert "expired" in str(raised.value).lower()
    await _assert_clean(session, reservation.id)


async def test_a_room_freed_by_a_lapsed_hold_can_be_taken_by_someone_else(
    session, tenant, clock
):
    booking, saga = _build(session, clock)
    first = await _hold(booking, tenant)
    clock.advance(settings.hold_ttl_seconds + 5)

    second = await booking.place_hold(
        org_id=tenant.org_id,
        user_id=tenant.other_user_id,
        room_name="A",
        title="Took the lapsed slot",
        attendees=2,
        time_range=TimeRange(at(10, 0), at(11, 0)),
    )
    outcome = await saga.confirm_booking(
        reservation_id=second.id, org_id=tenant.org_id, recipient="bob"
    )

    assert outcome.reservation.state is ReservationState.CONFIRMED
    # The original holder gets a refusal, not a second booking of the room.
    with pytest.raises(SagaFailed):
        await saga.confirm_booking(
            reservation_id=first.id, org_id=tenant.org_id, recipient="alice"
        )

    blocking = await session.scalar(
        select(func.count())
        .select_from(ReservationModel)
        .where(ReservationModel.state.in_(ReservationState.blocking()))
    )
    assert blocking == 1


# --- Escalation ------------------------------------------------------------


async def test_exhausted_retries_escalate_with_an_alert(session, tenant, clock):
    """A persistent failure must produce a human-review record, not a silent loop."""
    from app.agent.executor import ToolExecutor
    from app.agent.tools import HANDLERS, AgentContext
    from app.infrastructure.repositories.conversation_repository import (
        ConversationRepository,
    )
    from app.reliability.budget import Budget

    booking, saga = _build(session, clock)
    conversations = ConversationRepository(session)
    telemetry = TelemetryRepository(session)
    conversation = await conversations.create(tenant.org_id, tenant.user_id)
    reservation = await _hold(booking, tenant)

    executor = ToolExecutor(
        AgentContext(
            org_id=tenant.org_id,
            user_id=tenant.user_id,
            username="alice",
            conversation_id=conversation.id,
            booking=booking,
            saga=saga,
        ),
        conversations,
        telemetry,
        Budget.from_settings(),
        HANDLERS,
    )

    with injected(Scenario.CALENDAR_FAILURE, "calendar.create"):
        result = await executor.execute(
            "confirm_booking",
            {"reference": reservation.reference},
            preconfirmed=True,
        )

    assert result.escalated is True
    assert result.status is ToolCallStatus.ESCALATED
    assert "human review" in result.content

    alert = await session.scalar(
        select(AlertModel).where(AlertModel.conversation_id == conversation.id)
    )
    assert alert is not None
    assert alert.severity == "critical"
    await _assert_clean(session, reservation.id)
