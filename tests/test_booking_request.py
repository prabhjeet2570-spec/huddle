from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.bookings import BookingRequest


def booking_payload(**changes):
    return {
        "room_id": "cedar",
        "starts_at": "2026-10-05T09:00:00-04:00",
        "ends_at": "2026-10-05T10:00:00-04:00",
        "attendees": 3,
        **changes,
    }


def test_booking_times_are_normalized_to_utc():
    booking = BookingRequest.model_validate(booking_payload())

    assert booking.starts_at == datetime(2026, 10, 5, 13, tzinfo=UTC)
    assert booking.ends_at == datetime(2026, 10, 5, 14, tzinfo=UTC)
    assert booking.starts_at.tzinfo is UTC
    assert booking.ends_at.tzinfo is UTC


@pytest.mark.parametrize("field", ["starts_at", "ends_at"])
def test_booking_times_require_timezone(field):
    with pytest.raises(ValidationError, match="timezone"):
        BookingRequest.model_validate(booking_payload(**{field: "2026-10-05T09:00:00"}))


@pytest.mark.parametrize(
    "ends_at",
    [
        "2026-10-05T09:00:00-04:00",
        "2026-10-05T08:00:00-04:00",
        "2026-10-05T13:00:00Z",  # Same instant, different offset.
        "2026-10-05T10:00:00Z",  # Later wall time, earlier instant.
    ],
)
def test_end_must_be_after_start(ends_at):
    with pytest.raises(ValidationError, match="ends_at must be after starts_at"):
        BookingRequest.model_validate(booking_payload(ends_at=ends_at))


@pytest.mark.parametrize("attendees", [0, -1, 1.5, True, "3"])
def test_attendees_must_be_a_positive_integer(attendees):
    with pytest.raises(ValidationError) as error:
        BookingRequest.model_validate(booking_payload(attendees=attendees))

    assert error.value.errors()[0]["loc"] == ("attendees",)


def test_blank_room_id_is_rejected():
    with pytest.raises(ValidationError) as error:
        BookingRequest.model_validate(booking_payload(room_id="  "))

    assert error.value.errors()[0]["loc"] == ("room_id",)


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError) as error:
        BookingRequest.model_validate(booking_payload(confirmed=True))

    assert error.value.errors()[0]["type"] == "extra_forbidden"
