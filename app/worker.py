"""Lease durable calendar work; retry ambiguous outcomes using the booking ID."""

import logging
import os
import time
from uuid import uuid4

import httpx

from app.bookings import event
from app.db import connect

log = logging.getLogger(__name__)


def run_once(booking_id=None):
    token = uuid4()
    with connect() as conn:
        conn.execute(
            "INSERT INTO worker_heartbeats VALUES ('calendar',now()) ON CONFLICT(name) DO UPDATE SET seen_at=now()"
        )
        job = conn.execute(
            """SELECT * FROM outbox WHERE
            ((state='pending' AND available_at<=now()) OR (state='processing' AND lease_until<now()))
            AND (%s::uuid IS NULL OR booking_id=%s)
            ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1""",
            (booking_id, booking_id),
        ).fetchone()
        if not job:
            return False
        booking = conn.execute(
            "SELECT * FROM bookings WHERE id=%s", (job["booking_id"],)
        ).fetchone()
        if booking["version"] != job["version"]:
            conn.execute(
                "UPDATE outbox SET state='superseded',completed_at=now() WHERE id=%s", (job["id"],)
            )
            return True
        conn.execute(
            """UPDATE outbox SET state='processing',attempts=attempts+1,lease_token=%s,
            lease_until=now()+interval '30 seconds' WHERE id=%s""",
            (token, job["id"]),
        )
        event(
            conn,
            booking["id"],
            "calendar_attempt",
            f"Calendar attempt {job['attempts'] + 1} for revision {job['version']}.",
        )
    payload = {
        key: str(booking[key]) if key in ("id", "starts_at", "ends_at") else booking[key]
        for key in (
            "id",
            "title",
            "room_id",
            "starts_at",
            "ends_at",
            "attendees",
            "status",
            "version",
        )
    }
    try:
        response = httpx.put(
            os.getenv("CALENDAR_URL", "http://127.0.0.1:8001") + "/events/" + str(booking["id"]),
            json=payload,
            timeout=5,
            headers={
                "Authorization": "Bearer "
                + os.getenv("CALENDAR_TOKEN", "local-demo-calendar-token")
            },
        )
        response.raise_for_status()
        result = response.json()
        if result.get("id") != str(booking["id"]) or result.get("version", 0) < booking["version"]:
            raise ValueError("Calendar returned a mismatched acknowledgment")
    except (httpx.HTTPError, ValueError):
        attempts = job["attempts"] + 1
        state = "needs_review" if attempts >= 6 else "pending"
        with connect() as conn:
            changed = conn.execute(
                """UPDATE outbox SET state=%s,available_at=now()+(%s * interval '1 second'),
                lease_until=NULL,last_error='Calendar unavailable or acknowledgment missing'
                WHERE id=%s AND lease_token=%s AND state='processing' RETURNING id""",
                (state, min(60, 2**attempts), job["id"], token),
            ).fetchone()
            if changed:
                conn.execute(
                    "UPDATE bookings SET calendar_status=%s WHERE id=%s AND version=%s",
                    (
                        "needs_review" if state == "needs_review" else "pending",
                        booking["id"],
                        booking["version"],
                    ),
                )
                event(
                    conn,
                    booking["id"],
                    "calendar_retry" if state == "pending" else "needs_review",
                    "Calendar response uncertain. Room allocation retained; reconciliation "
                    + ("queued." if state == "pending" else "requires review."),
                )
        return True
    with connect() as conn:
        changed = conn.execute(
            """UPDATE outbox SET state='done',completed_at=now(),lease_until=NULL,last_error=NULL
            WHERE id=%s AND lease_token=%s AND state='processing' RETURNING id""",
            (job["id"], token),
        ).fetchone()
        if changed:
            conn.execute(
                "UPDATE bookings SET calendar_status='synced' WHERE id=%s AND version=%s",
                (booking["id"], booking["version"]),
            )
            event(
                conn,
                booking["id"],
                "calendar_synced",
                f"Calendar revision {booking['version']} reconciled without a duplicate event.",
            )
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            worked = run_once()
        except Exception:
            log.exception("Worker iteration failed; durable lease will expire")
            worked = False
        time.sleep(0.2 if worked else 2)
