"""The booking transaction.

Confirming a booking is four steps against three systems:

    hold -> confirm -> notify -> calendar

Only the first two are ours. If the calendar sync fails, a naive
implementation leaves a confirmed room that no external system knows about; if
notify fails, attendees never learn about a meeting that now exists. Neither is
an error the user can act on, and neither heals itself.

So the whole thing runs as a saga. On failure at step *k*, steps *k-1 … 1* are
undone in reverse order and the reservation returns to the state it had before
the user asked. Every compensation is idempotent and every one is logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.domain.enums import AlertKind, SagaStatus, StepPhase, StepStatus
from app.domain.reservation import Reservation
from app.infrastructure.repositories.telemetry_repository import TelemetryRepository
from app.observability.tracing import saga_span
from app.reliability.retry import call_with_retry
from app.reliability.saga import SagaFailed, SagaStep, run_saga
from app.services.booking_service import BookingService
from app.services.calendar_sync import CalendarService
from app.services.notifications import NotificationService

STEP_CONFIRM = "confirm_reservation"
STEP_NOTIFY = "notify_attendees"
STEP_CALENDAR = "sync_calendar"


class _Recorder:
    """Bridges the saga runner to the durable ``saga_events`` table."""

    def __init__(self, telemetry: TelemetryRepository, saga_id: UUID) -> None:
        self._telemetry = telemetry
        self._saga_id = saga_id

    async def step(
        self,
        step: str,
        phase: StepPhase,
        status: StepStatus,
        detail: str | None = None,
    ) -> None:
        await self._telemetry.record_saga_event(
            self._saga_id, step, phase, status, detail
        )


@dataclass(slots=True)
class BookingOutcome:
    reservation: Reservation
    saga_id: UUID
    notified: bool
    calendar_id: str | None


class BookingSaga:
    def __init__(
        self,
        booking: BookingService,
        notifications: NotificationService,
        calendar: CalendarService,
        telemetry: TelemetryRepository,
    ) -> None:
        self._booking = booking
        self._notifications = notifications
        self._calendar = calendar
        self._telemetry = telemetry

    async def confirm_booking(
        self,
        *,
        reservation_id: UUID,
        org_id: UUID,
        recipient: str,
        conversation_id: UUID | None = None,
    ) -> BookingOutcome:
        """Drive hold -> confirm -> notify -> calendar, or unwind trying."""
        saga_id = await self._telemetry.start_saga(
            "confirm_booking",
            conversation_id=conversation_id,
            reservation_id=reservation_id,
        )
        recorder = _Recorder(self._telemetry, saga_id)
        context: dict[str, Any] = {
            "reservation_id": reservation_id,
            "org_id": org_id,
            "recipient": recipient,
        }

        try:
            result = await run_saga(
                "confirm_booking",
                self._steps(),
                context,
                recorder,
            )
        except SagaFailed as failure:
            await self._telemetry.finish_saga(
                saga_id,
                SagaStatus.COMPENSATED
                if failure.compensated
                else SagaStatus.COMPENSATION_FAILED,
                reservation_id=reservation_id,
                failed_step=failure.failed_step,
                error=str(failure.cause),
            )
            if not failure.compensated:
                # State could not be returned to clean. This is the one case a
                # human must look at, so it becomes an alert rather than a log
                # line nobody reads.
                await self._telemetry.raise_alert(
                    AlertKind.COMPENSATION_FAILED,
                    f"Rollback incomplete for reservation {reservation_id} after "
                    f"'{failure.failed_step}' failed",
                    org_id=org_id,
                    conversation_id=conversation_id,
                    severity="critical",
                    details={
                        "reservation_id": str(reservation_id),
                        "failed_step": failure.failed_step,
                        "compensation_errors": failure.compensation_errors,
                    },
                )
            raise

        await self._telemetry.finish_saga(
            saga_id, SagaStatus.COMPLETED, reservation_id=reservation_id
        )
        return BookingOutcome(
            reservation=result.context[f"{STEP_CONFIRM}_result"],
            saga_id=saga_id,
            notified=bool(result.context.get(f"{STEP_NOTIFY}_result")),
            calendar_id=result.context.get(f"{STEP_CALENDAR}_result"),
        )

    # --- Steps -------------------------------------------------------------

    def _steps(self) -> list[SagaStep]:
        return [
            SagaStep(
                name=STEP_CONFIRM,
                execute=self._confirm,
                compensate=self._undo_confirm,
            ),
            SagaStep(
                name=STEP_NOTIFY,
                execute=self._notify,
                compensate=self._undo_notify,
            ),
            SagaStep(
                name=STEP_CALENDAR,
                execute=self._sync_calendar,
                compensate=self._undo_calendar,
            ),
        ]

    async def _confirm(self, context: dict[str, Any]) -> Reservation:
        with saga_span("confirm_booking", STEP_CONFIRM, StepPhase.EXECUTE):
            reservation, _ = await call_with_retry(
                STEP_CONFIRM,
                lambda: self._booking.confirm_hold(
                    context["reservation_id"], context["org_id"]
                ),
            )
            return reservation

    async def _undo_confirm(self, context: dict[str, Any]) -> bool:
        """Release the reservation entirely.

        The user asked for a booking and did not get one, so leaving the room
        held would block it for no reason. Releasing is idempotent: a
        reservation already cancelled reports ``False`` and stays cancelled.
        """
        with saga_span("confirm_booking", STEP_CONFIRM, StepPhase.COMPENSATE):
            return await self._booking.release_hold(
                context["reservation_id"], context["org_id"]
            )

    async def _notify(self, context: dict[str, Any]) -> UUID:
        with saga_span("confirm_booking", STEP_NOTIFY, StepPhase.EXECUTE):
            reservation: Reservation = context[f"{STEP_CONFIRM}_result"]
            notification_id, _ = await call_with_retry(
                STEP_NOTIFY,
                lambda: self._notifications.notify_booking(
                    reservation, context["recipient"]
                ),
            )
            return notification_id

    async def _undo_notify(self, context: dict[str, Any]) -> bool:
        with saga_span("confirm_booking", STEP_NOTIFY, StepPhase.COMPENSATE):
            return await self._notifications.void(context["reservation_id"])

    async def _sync_calendar(self, context: dict[str, Any]) -> str:
        with saga_span("confirm_booking", STEP_CALENDAR, StepPhase.EXECUTE):
            reservation: Reservation = context[f"{STEP_CONFIRM}_result"]
            external_id, _ = await call_with_retry(
                STEP_CALENDAR,
                lambda: self._calendar.create_entry(reservation),
            )
            return external_id

    async def _undo_calendar(self, context: dict[str, Any]) -> bool:
        with saga_span("confirm_booking", STEP_CALENDAR, StepPhase.COMPENSATE):
            return await self._calendar.delete_entry(context["reservation_id"])
