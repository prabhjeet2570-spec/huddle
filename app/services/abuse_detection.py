"""Hold-cycling detection.

Expiring holds fix one problem and create another. A hold blocks a room and
releases itself, so a user who never confirms can keep a room indefinitely by
placing a new hold the instant the previous one lapses. No single request
looks abusive; the pattern is only visible over time.

Detection is therefore a rolling-window statistic on the ``hold_events``
audit trail, on two independent signals:

* **Confirm ratio** - of the holds placed in the window, how many became
  bookings. A user working normally confirms most of what they hold.
* **Hold frequency** - sheer volume of holds in the window, which catches a
  fast cycler whose ratio has not yet dropped.

Both need a minimum sample (``abuse_min_holds``) before they can fire, so a
user who places two holds and abandons one is never flagged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from app.config import settings
from app.domain.enums import AlertKind
from app.domain.exceptions import HoldRateLimited
from app.infrastructure.repositories.abuse_repository import (
    AbuseRepository,
    HoldWindow,
)
from app.infrastructure.repositories.telemetry_repository import TelemetryRepository


@dataclass(frozen=True, slots=True)
class Detection:
    """The verdict for one user at one moment, with the evidence behind it."""

    abusive: bool
    reason: str
    window: HoldWindow

    @property
    def evidence(self) -> dict:
        return {**self.window.as_evidence(), "reason": self.reason}


def classify(window: HoldWindow) -> Detection:
    """Pure decision function. Separated so it can be tested without a database."""
    if window.created < settings.abuse_min_holds:
        return Detection(
            abusive=False,
            reason=(
                f"only {window.created} holds in window "
                f"(minimum {settings.abuse_min_holds} to assess)"
            ),
            window=window,
        )

    if window.created >= settings.abuse_max_holds_per_window:
        return Detection(
            abusive=True,
            reason=(
                f"hold frequency {window.created} in "
                f"{settings.abuse_window_seconds}s exceeds "
                f"{settings.abuse_max_holds_per_window}"
            ),
            window=window,
        )

    if window.confirm_ratio <= settings.abuse_max_confirm_ratio:
        return Detection(
            abusive=True,
            reason=(
                f"confirm ratio {window.confirm_ratio:.2f} "
                f"({window.confirmed}/{window.created}) at or below "
                f"{settings.abuse_max_confirm_ratio}"
            ),
            window=window,
        )

    return Detection(
        abusive=False,
        reason=(
            f"confirm ratio {window.confirm_ratio:.2f} "
            f"({window.confirmed}/{window.created}) within tolerance"
        ),
        window=window,
    )


class HoldCyclingDetector:
    def __init__(
        self,
        repository: AbuseRepository,
        telemetry: TelemetryRepository | None = None,
    ) -> None:
        self._repository = repository
        self._telemetry = telemetry

    async def evaluate(self, user_id: UUID, now: datetime) -> Detection:
        window = await self._repository.hold_window(
            user_id, now, settings.abuse_window_seconds
        )
        return classify(window)

    async def check(self, *, org_id: UUID, user_id: UUID, now: datetime) -> Detection:
        """Gate a hold request. Raises :class:`HoldRateLimited` when limited.

        An existing rate limit short-circuits: once a user is limited they stay
        limited for the cooldown, otherwise letting holds lapse would restore
        their ratio and unlock them immediately.
        """
        active = await self._repository.active_rate_limit(user_id, now)
        if active is not None:
            raise HoldRateLimited(active.until, active.reason)

        detection = await self.evaluate(user_id, now)
        if not detection.abusive:
            return detection

        until = now + timedelta(seconds=settings.abuse_rate_limit_seconds)
        await self._repository.apply_rate_limit(
            org_id=org_id,
            user_id=user_id,
            until=until,
            reason=detection.reason,
            evidence=detection.evidence,
        )
        if self._telemetry is not None:
            await self._telemetry.raise_alert(
                AlertKind.HOLD_CYCLING_DETECTED,
                f"Hold cycling detected for user {user_id}: {detection.reason}",
                org_id=org_id,
                severity="warning",
                details={"user_id": str(user_id), **detection.evidence},
            )
        raise HoldRateLimited(until, detection.reason)
