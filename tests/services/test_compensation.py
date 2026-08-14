"""Compensating actions.

A booking is hold -> confirm -> notify -> calendar. The contract under test:
if any step after the hold fails, every completed step is undone in reverse
order and the reservation ends up released, with the whole unwind on record.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.domain.enums import (
    AlertKind,
    ReservationState,
    SagaStatus,
    StepPhase,
    StepStatus,
)
from app.domain.time_range import TimeRange
from app.infrastructure.models import (
    AlertModel,
    CalendarEntryModel,
    NotificationModel,
    ReservationModel,
    SagaModel,
)
from app.infrastructure.repositories.abuse_repository import AbuseRepository
from app.infrastructure.repositories.reservation_repository import (
    ReservationRepository,
)
from app.infrastructure.repositories.telemetry_repository import TelemetryRepository
from app.reliability.failure_injection import Scenario, injected
from app.reliability.saga import SagaFailed
from app.services.abuse_detection import HoldCyclingDetector
from app.services.booking_saga import (
    STEP_CALENDAR,
    STEP_CONFIRM,
    STEP_NOTIFY,
    BookingSaga,
)
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
    return booking, saga, telemetry


async def _hold(booking, tenant, hour: int = 10):
    return await booking.place_hold(
        org_id=tenant.org_id,
        user_id=tenant.user_id,
        room_name="A",
        title="Design review",
        attendees=3,
        time_range=TimeRange(at(hour, 0), at(hour + 1, 0)),
    )


async def _state(session, reservation_id) -> str:
    return await session.scalar(
        select(ReservationModel.state).where(ReservationModel.id == reservation_id)
    )


async def _count(session, model, reservation_id) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.reservation_id == reservation_id)
        )
    )


# --- Happy path ------------------------------------------------------------


async def test_successful_booking_completes_every_step(session, tenant, clock):
    booking, saga, _ = _build(session, clock)
    reservation = await _hold(booking, tenant)

    outcome = await saga.confirm_booking(
        reservation_id=reservation.id, org_id=tenant.org_id, recipient="alice"
    )

    assert outcome.reservation.state is ReservationState.CONFIRMED
    assert outcome.notified is True
    assert outcome.calendar_id is not None
    assert await _state(session, reservation.id) == ReservationState.CONFIRMED
    assert await _count(session, NotificationModel, reservation.id) == 1
    assert await _count(session, CalendarEntryModel, reservation.id) == 1

    saga_row = await session.scalar(
        select(SagaModel).where(SagaModel.id == outcome.saga_id)
    )
    assert saga_row.status == SagaStatus.COMPLETED


# --- Failure injected at each step -----------------------------------------


async def test_notification_failure_rolls_back_the_confirmation(session, tenant, clock):
    booking, saga, telemetry = _build(session, clock)
    reservation = await _hold(booking, tenant)

    with (
        injected(Scenario.NOTIFICATION_FAILURE, "notification.send"),
        pytest.raises(SagaFailed) as raised,
    ):
        await saga.confirm_booking(
            reservation_id=reservation.id,
            org_id=tenant.org_id,
            recipient="alice",
        )

    assert raised.value.failed_step == STEP_NOTIFY
    assert raised.value.compensated is True
    # State is clean: the room is released, nothing was sent, no calendar entry.
    assert await _state(session, reservation.id) == ReservationState.CANCELLED
    assert await _count(session, NotificationModel, reservation.id) == 0
    assert await _count(session, CalendarEntryModel, reservation.id) == 0

    events = await telemetry.saga_events(
        await session.scalar(
            select(SagaModel.id).where(SagaModel.reservation_id == reservation.id)
        )
    )
    compensations = [
        event.step
        for event in events
        if event.phase == StepPhase.COMPENSATE
        and event.status in (StepStatus.SUCCEEDED, StepStatus.NOOP)
    ]
    assert compensations == [STEP_CONFIRM]


async def test_calendar_failure_rolls_back_notify_then_confirm_in_reverse(
    session, tenant, clock
):
    booking, saga, telemetry = _build(session, clock)
    reservation = await _hold(booking, tenant)

    with (
        injected(Scenario.CALENDAR_FAILURE, "calendar.create"),
        pytest.raises(SagaFailed) as raised,
    ):
        await saga.confirm_booking(
            reservation_id=reservation.id,
            org_id=tenant.org_id,
            recipient="alice",
        )

    assert raised.value.failed_step == STEP_CALENDAR
    assert raised.value.compensated is True
    assert await _state(session, reservation.id) == ReservationState.CANCELLED
    assert await _count(session, CalendarEntryModel, reservation.id) == 0

    saga_id = await session.scalar(
        select(SagaModel.id).where(SagaModel.reservation_id == reservation.id)
    )
    events = await telemetry.saga_events(saga_id)
    compensated = [
        event.step
        for event in events
        if event.phase == StepPhase.COMPENSATE
        and event.status in (StepStatus.SUCCEEDED, StepStatus.NOOP)
    ]
    # Reverse order is the contract: notify is undone before confirm.
    assert compensated == [STEP_NOTIFY, STEP_CONFIRM]

    saga_row = await session.get(SagaModel, saga_id)
    assert saga_row.status == SagaStatus.COMPENSATED


async def test_confirm_failure_leaves_the_hold_untouched(session, tenant, clock):
    """A failure at the first step has nothing to compensate."""
    booking, saga, _ = _build(session, clock)
    reservation = await _hold(booking, tenant)
    clock.advance(settings.hold_ttl_seconds + 5)  # the hold lapses first

    with pytest.raises(SagaFailed):
        await saga.confirm_booking(
            reservation_id=reservation.id, org_id=tenant.org_id, recipient="alice"
        )

    assert await _state(session, reservation.id) != ReservationState.CONFIRMED
    assert await _count(session, NotificationModel, reservation.id) == 0
    assert await _count(session, CalendarEntryModel, reservation.id) == 0


# --- Idempotency -----------------------------------------------------------


async def test_compensations_are_idempotent(session, tenant, clock):
    """Running each compensation twice must be a no-op the second time."""
    booking, saga, _ = _build(session, clock)
    reservation = await _hold(booking, tenant)
    await saga.confirm_booking(
        reservation_id=reservation.id, org_id=tenant.org_id, recipient="alice"
    )

    notifications = NotificationService(session)
    calendar = CalendarService(session)

    assert await calendar.delete_entry(reservation.id) is True
    assert await calendar.delete_entry(reservation.id) is False

    assert await notifications.void(reservation.id) is True
    assert await notifications.void(reservation.id) is False

    assert await booking.release_hold(reservation.id, tenant.org_id) is True
    assert await booking.release_hold(reservation.id, tenant.org_id) is False

    # Twice-run compensations leave exactly the same clean state as once-run.
    assert await _state(session, reservation.id) == ReservationState.CANCELLED
    assert await _count(session, CalendarEntryModel, reservation.id) == 0


async def test_failed_compensation_raises_a_critical_alert(session, tenant, clock):
    """When rollback itself fails, a human has to know."""
    booking, saga, _ = _build(session, clock)
    reservation = await _hold(booking, tenant)

    with (
        injected(Scenario.CALENDAR_FAILURE, "calendar.create"),
        injected(Scenario.COMPENSATION_FAILURE, "notification.void"),
        pytest.raises(SagaFailed) as raised,
    ):
        await saga.confirm_booking(
            reservation_id=reservation.id,
            org_id=tenant.org_id,
            recipient="alice",
        )

    assert raised.value.compensated is False
    alert = await session.scalar(
        select(AlertModel).where(AlertModel.kind == AlertKind.COMPENSATION_FAILED)
    )
    assert alert is not None
    assert alert.severity == "critical"
    # The reservation is still released even though one compensation failed:
    # the unwind keeps going past a stuck step.
    assert await _state(session, reservation.id) == ReservationState.CANCELLED


async def test_room_is_free_again_after_a_rolled_back_booking(session, tenant, clock):
    """The observable consequence: a failed booking does not sterilise the room."""
    booking, saga, _ = _build(session, clock)
    reservation = await _hold(booking, tenant)
    window = TimeRange(at(10, 0), at(11, 0))

    assert [
        room.name
        for room in await booking.list_available_rooms(tenant.org_id, window, 2)
    ] == ["B", "C", "D", "E"]

    with (
        injected(Scenario.CALENDAR_FAILURE, "calendar.create"),
        pytest.raises(SagaFailed),
    ):
        await saga.confirm_booking(
            reservation_id=reservation.id,
            org_id=tenant.org_id,
            recipient="alice",
        )

    names = [
        room.name
        for room in await booking.list_available_rooms(tenant.org_id, window, 2)
    ]
    assert "A" in names
