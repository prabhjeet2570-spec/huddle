"""Hold-cycling detection.

The exploit: holds expire, so a user who never confirms can occupy a room
indefinitely by placing a fresh hold the moment the last one lapses. Each
individual request is legitimate; only the pattern is not.

The final test here is the one that matters. It seeds abusive and normal
sessions with known ground truth and measures precision and recall against it,
rather than asserting that one handcrafted abuser is caught.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import select

from app.config import settings
from app.domain.enums import AlertKind
from app.domain.exceptions import HoldRateLimited
from app.domain.time_range import TimeRange
from app.infrastructure.models import AlertModel, HoldRateLimitModel
from app.infrastructure.repositories.abuse_repository import (
    AbuseRepository,
    HoldWindow,
)
from app.infrastructure.repositories.reservation_repository import (
    ReservationRepository,
)
from app.infrastructure.repositories.telemetry_repository import TelemetryRepository
from app.services.abuse_detection import HoldCyclingDetector, classify
from app.services.booking_saga import BookingSaga
from app.services.booking_service import BookingService
from app.services.calendar_sync import CalendarService
from app.services.notifications import NotificationService
from tests.conftest import BASE_TIME, at, requires_postgres

pytestmark = [requires_postgres, pytest.mark.postgres]


def _window(created: int, confirmed: int, rooms: int = 1) -> HoldWindow:
    return HoldWindow(
        window_start=BASE_TIME,
        window_end=BASE_TIME,
        created=created,
        confirmed=confirmed,
        expired=created - confirmed,
        released=0,
        distinct_rooms=rooms,
    )


# --- The decision function -------------------------------------------------


def test_a_user_below_the_minimum_sample_is_never_flagged():
    """Two holds and one abandoned is ordinary indecision, not abuse."""
    detection = classify(_window(created=2, confirmed=0))

    assert detection.abusive is False
    assert "minimum" in detection.reason


def test_a_low_confirm_ratio_over_enough_holds_is_flagged():
    detection = classify(_window(created=8, confirmed=1))

    assert detection.abusive is True
    assert "confirm ratio" in detection.reason
    assert detection.evidence["holds_created"] == 8


def test_a_healthy_confirm_ratio_is_not_flagged():
    detection = classify(_window(created=8, confirmed=6))

    assert detection.abusive is False


def test_sheer_hold_frequency_is_flagged_even_with_a_good_ratio():
    """Catches a fast cycler whose ratio has not dropped yet."""
    created = settings.abuse_max_holds_per_window + 2
    detection = classify(_window(created=created, confirmed=created - 1))

    assert detection.abusive is True
    assert "frequency" in detection.reason


def test_the_boundary_of_the_confirm_ratio_is_inclusive():
    # 2/6 = 0.333, at or below the 0.34 threshold.
    assert classify(_window(created=6, confirmed=2)).abusive is True
    # 3/6 = 0.5, above it.
    assert classify(_window(created=6, confirmed=3)).abusive is False


# --- End to end ------------------------------------------------------------


def _services(session, clock):
    repository = ReservationRepository(session)
    telemetry = TelemetryRepository(session)
    detector = HoldCyclingDetector(AbuseRepository(session), telemetry)
    booking = BookingService(repository, detector, clock)
    saga = BookingSaga(
        booking, NotificationService(session), CalendarService(session), telemetry
    )
    return booking, saga


async def _cycle_hold(booking, tenant, clock, user_id, hour: int, confirm=None):
    """Place a hold, then let it lapse - the exploit, one iteration."""
    reservation = await booking.place_hold(
        org_id=tenant.org_id,
        user_id=user_id,
        room_name="A",
        title="squatting",
        attendees=2,
        time_range=TimeRange(at(hour, 0), at(hour + 1, 0)),
    )
    if confirm is not None:
        await confirm.confirm_booking(
            reservation_id=reservation.id, org_id=tenant.org_id, recipient="x"
        )
    clock.advance(settings.hold_ttl_seconds + 1)
    return reservation


async def test_cycling_holds_eventually_rate_limits_the_user(session, tenant, clock):
    booking, _ = _services(session, clock)

    with pytest.raises(HoldRateLimited) as raised:
        for index in range(settings.abuse_min_holds + 4):
            await _cycle_hold(booking, tenant, clock, tenant.user_id, 10 + index)

    assert "unconfirmed holds" in str(raised.value)

    limit = await session.scalar(
        select(HoldRateLimitModel).where(HoldRateLimitModel.user_id == tenant.user_id)
    )
    assert limit is not None
    # The evidence is stored, not just the verdict.
    assert limit.evidence["holds_created"] >= settings.abuse_min_holds
    assert limit.evidence["confirm_ratio"] <= settings.abuse_max_confirm_ratio

    alert = await session.scalar(
        select(AlertModel).where(AlertModel.kind == AlertKind.HOLD_CYCLING_DETECTED)
    )
    assert alert is not None


async def test_a_rate_limited_user_stays_limited_for_the_whole_cooldown(
    session, tenant, clock
):
    """Waiting out the detection window must not lift the limit early.

    Without the short-circuit on an active limit, a cycler could simply stop
    for one window, watch their ratio reset, and resume.
    """
    booking, _ = _services(session, clock)

    with pytest.raises(HoldRateLimited):
        for index in range(settings.abuse_min_holds + 4):
            await _cycle_hold(booking, tenant, clock, tenant.user_id, 10 + index)

    # Long enough for the rolling window to have emptied, but inside the
    # cooldown. The limit must still hold.
    clock.advance(settings.abuse_rate_limit_seconds - 60)

    with pytest.raises(HoldRateLimited):
        await booking.place_hold(
            org_id=tenant.org_id,
            user_id=tenant.user_id,
            room_name="B",
            title="still blocked",
            attendees=2,
            time_range=TimeRange(at(15, 0), at(16, 0)),
        )

    # Recovery needs both conditions: the cooldown elapsed *and* the offending
    # holds aged out of the rolling window. Until then the evidence still
    # stands and the user is re-limited immediately. The limit is a brake
    # rather than a ban, but it does not lift just because time passed.
    clock.advance(120)
    with pytest.raises(HoldRateLimited):
        await booking.place_hold(
            org_id=tenant.org_id,
            user_id=tenant.user_id,
            room_name="B",
            title="cooldown over, evidence not yet stale",
            attendees=2,
            time_range=TimeRange(at(15, 0), at(16, 0)),
        )

    clock.advance(settings.abuse_window_seconds + settings.abuse_rate_limit_seconds)
    reservation = await booking.place_hold(
        org_id=tenant.org_id,
        user_id=tenant.user_id,
        room_name="B",
        title="reformed",
        attendees=2,
        time_range=TimeRange(at(15, 0), at(16, 0)),
    )
    assert reservation.reference


async def test_a_normal_user_is_never_rate_limited(session, tenant, clock):
    """The false-positive case: hold, confirm, repeat."""
    booking, saga = _services(session, clock)

    for index in range(8):
        await _cycle_hold(
            booking, tenant, clock, tenant.user_id, 10 + index, confirm=saga
        )

    limits = await session.scalars(select(HoldRateLimitModel))
    assert list(limits) == []


async def test_rate_limiting_is_scoped_to_the_offending_user(session, tenant, clock):
    booking, _ = _services(session, clock)

    with pytest.raises(HoldRateLimited):
        for index in range(settings.abuse_min_holds + 4):
            await _cycle_hold(booking, tenant, clock, tenant.user_id, 10 + index)

    # A different user is unaffected.
    reservation = await booking.place_hold(
        org_id=tenant.org_id,
        user_id=tenant.other_user_id,
        room_name="C",
        title="innocent bystander",
        attendees=2,
        time_range=TimeRange(at(14, 0), at(15, 0)),
    )
    assert reservation.reference


# --- Measured precision and recall ----------------------------------------


@dataclass
class Session_:
    label: str  # ground truth: "abusive" or "normal"
    holds: int
    confirms: int


#: Ground truth. Abusive sessions cycle holds without confirming; normal
#: sessions confirm most of what they hold. The borderline normals are
#: deliberately unflattering: users who abandon a third of their holds.
GROUND_TRUTH = [
    Session_("abusive", 6, 0),
    Session_("abusive", 8, 1),
    Session_("abusive", 12, 3),
    Session_("abusive", 5, 1),
    Session_("abusive", 15, 4),
    Session_("abusive", 7, 2),
    Session_("normal", 6, 6),
    Session_("normal", 4, 4),
    Session_("normal", 8, 7),
    Session_("normal", 5, 4),
    Session_("normal", 9, 6),
    Session_("normal", 6, 4),
    Session_("normal", 2, 0),  # too few holds to judge
    Session_("normal", 3, 1),  # sparse and indecisive, but not squatting
]


async def test_detection_precision_and_recall_against_seeded_ground_truth(
    capsys,
):
    """Measure the detector against labelled sessions, and report the numbers."""
    true_positive = false_positive = true_negative = false_negative = 0

    for scenario in GROUND_TRUTH:
        predicted = classify(
            _window(created=scenario.holds, confirmed=scenario.confirms)
        ).abusive
        actual = scenario.label == "abusive"

        if predicted and actual:
            true_positive += 1
        elif predicted and not actual:
            false_positive += 1
        elif not predicted and actual:
            false_negative += 1
        else:
            true_negative += 1

    precision = true_positive / (true_positive + false_positive or 1)
    recall = true_positive / (true_positive + false_negative or 1)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    with capsys.disabled():
        print(
            f"\n  hold-cycling detection over {len(GROUND_TRUTH)} labelled "
            f"sessions: TP={true_positive} FP={false_positive} "
            f"TN={true_negative} FN={false_negative} | "
            f"precision={precision:.2f} recall={recall:.2f} f1={f1:.2f}"
        )

    # A false positive rate-limits a legitimate user, so precision is the
    # metric held to 1.0; recall is allowed to lag because a missed cycler is
    # caught on the next window.
    assert precision == 1.0, f"false positives: {false_positive}"
    assert recall >= 0.8, f"missed {false_negative} abusive sessions"
