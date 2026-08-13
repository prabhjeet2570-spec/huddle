"""Populate local sample profiles and verify booking/calendar combinations.

Only touches records with this script's idempotency keys. Nothing is truncated.
Pause the dev worker while running, so controlled failure trials own their jobs.
Set HUDDLE_DEMO_PROFILES=true locally. API/calendar must already be running.
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def seed_history():
    """Explicit historical fixtures; new user bookings still reject past dates."""
    from uuid import NAMESPACE_URL, uuid5

    from app.bookings import event
    from app.db import connect
    from app.rooms import ROOMS
    from app.session import DEMO_NAMES
    from app.worker import run_once

    result = []
    for index, name in enumerate(DEMO_NAMES):
        identity = uuid5(NAMESPACE_URL, "huddle-history-v1/" + name)
        owner = uuid5(NAMESPACE_URL, "huddle-local-demo/" + name)
        start = datetime.now(ZoneInfo("America/New_York")).replace(
            hour=14, minute=0, second=0, microsecond=0
        ) - timedelta(days=5 + index)
        with connect() as conn:
            inserted = conn.execute(
                """INSERT INTO bookings(id,owner_id,room_id,title,starts_at,ends_at,attendees)
                VALUES (%s,%s,%s,'Last week’s study session',%s,%s,2) ON CONFLICT(id) DO NOTHING RETURNING id""",
                (identity, owner, ROOMS[index * 6].id, start, start + timedelta(hours=1)),
            ).fetchone()
            if inserted:
                event(
                    conn,
                    identity,
                    "confirmed",
                    "Explicit historical sample fixture, not a past-date API booking.",
                )
        while run_once(identity):
            pass
        result.append({"name": name, "booking_id": str(identity), "historical_fixture": True})
    return result


def main():
    from app.db import connect
    from app.rooms import ROOMS
    from app.session import DEMO_NAMES
    from app.worker import run_once

    base = os.getenv("HUDDLE_URL", "http://127.0.0.1:8010")
    if not base.startswith("http://127.0.0.1:"):
        raise SystemExit("Use a local Huddle instance for sample data")
    day = datetime.now(ZoneInfo("America/New_York")).replace(
        hour=9, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    calendar = os.getenv("CALENDAR_URL", "http://127.0.0.1:8001")
    authorization = {
        "Authorization": "Bearer " + os.getenv("CALENDAR_TOKEN", "local-demo-calendar-token")
    }
    checks, profiles = {}, {}
    titles = [
        "Algorithms study group",
        "Portfolio review",
        "Research reading circle",
        "Interview practice",
        "Statistics problem set",
        "Thesis planning",
        "Design critique",
        "Exam revision",
        "Student club planning",
        "Pair programming",
        "Presentation rehearsal",
        "Quiet writing hour",
    ]
    clients = {name: httpx.Client(base_url=base, timeout=30) for name in DEMO_NAMES}
    try:
        for index, (name, client) in enumerate(clients.items()):
            response = client.post("/demo/profiles/" + name)
            response.raise_for_status()
            bookings = []
            for n, title in enumerate(titles):
                room = ROOMS[(index * 6 + n) % len(ROOMS)]
                start = day + timedelta(days=n // 6, hours=(n // 3) % 6)
                payload = {
                    "room_id": room.id,
                    "title": title,
                    "attendees": min(room.capacity, [2, 4, 6, 8, 12, 20][n % 6]),
                    "starts_at": start.isoformat(),
                    "ends_at": (start + timedelta(minutes=[30, 60, 90][n % 3])).isoformat(),
                }
                response = client.post(
                    "/bookings",
                    json=payload,
                    headers={"Idempotency-Key": f"profile-population-v1-{day.date()}-{name}-{n}"},
                )
                response.raise_for_status()
                bookings.append(response.json())
            # Populate real edit/cancel history through the same APIs as the UI.
            b = client.get("/bookings/" + bookings[2]["id"]).json()
            if b["version"] == 1:
                edited = client.put(
                    "/bookings/" + b["id"],
                    headers={"If-Match": "1"},
                    json={
                        k: ("Research planning" if k == "title" else b[k])
                        for k in ("title", "room_id", "starts_at", "ends_at", "attendees")
                    },
                )
                edited.raise_for_status()
            for b in bookings[3:5]:
                current = client.get("/bookings/" + b["id"]).json()
                if current["status"] != "cancelled":
                    client.post(
                        "/bookings/" + b["id"] + "/cancel",
                        headers={"If-Match": str(current["version"])},
                    ).raise_for_status()
            for b in bookings:
                while run_once(b["id"]):
                    pass
            # Six HTTP 503 responses exhaust the real worker; accelerate only backoff clock.
            outage = bookings[6]
            with connect() as conn:
                conn.execute(
                    "UPDATE outbox SET state='pending',attempts=0,available_at=now(),completed_at=NULL WHERE booking_id=%s AND version=1",
                    (outage["id"],),
                )
                conn.execute(
                    "UPDATE bookings SET calendar_status='pending' WHERE id=%s", (outage["id"],)
                )
            httpx.post(
                calendar + "/failures/" + outage["id"], headers=authorization, json={"attempts": 6}
            ).raise_for_status()
            for _ in range(6):
                with connect() as conn:
                    conn.execute(
                        "UPDATE outbox SET available_at=now() WHERE booking_id=%s", (outage["id"],)
                    )
                assert run_once(outage["id"])
            current = client.get("/bookings/" + outage["id"]).json()
            assert current["calendar_status"] == "needs_review" and current["status"] == "confirmed"
            checks[name + "_six_failures_preserve_room"] = True
            # Exercise owner retry to completion, then leave a second honest failure example.
            client.post("/bookings/" + outage["id"] + "/retry-sync").raise_for_status()
            assert run_once(outage["id"])
            assert client.get("/bookings/" + outage["id"]).json()["calendar_status"] == "synced"
            checks[name + "_manual_retry_recovers"] = True
            failed = bookings[7]
            with connect() as conn:
                conn.execute(
                    "UPDATE outbox SET state='pending',attempts=0,available_at=now(),completed_at=NULL WHERE booking_id=%s AND version=1",
                    (failed["id"],),
                )
                conn.execute(
                    "UPDATE bookings SET calendar_status='pending' WHERE id=%s", (failed["id"],)
                )
            httpx.post(
                calendar + "/failures/" + failed["id"], headers=authorization, json={"attempts": 6}
            ).raise_for_status()
            for _ in range(6):
                with connect() as conn:
                    conn.execute(
                        "UPDATE outbox SET available_at=now() WHERE booking_id=%s", (failed["id"],)
                    )
                assert run_once(failed["id"])
            # Leave one delayed task for a pending state. Worker will reconcile it in 30 minutes.
            delayed = bookings[8]
            with connect() as conn:
                conn.execute(
                    "UPDATE outbox SET state='pending',available_at=now()+interval '30 minutes',completed_at=NULL WHERE booking_id=%s AND version=1",
                    (delayed["id"],),
                )
                conn.execute(
                    "UPDATE bookings SET calendar_status='pending' WHERE id=%s", (delayed["id"],)
                )
            profiles[name] = {
                "created": len(bookings),
                "metrics": client.get("/metrics").json(),
                "booking_ids": [b["id"] for b in bookings],
            }
        historical = seed_history()
        blake, morgan = clients["Blake"], clients["Morgan"]
        first = blake.get("/bookings/" + profiles["Blake"]["booking_ids"][0]).json()
        assert morgan.get("/bookings/" + first["id"]).status_code == 404
        checks["cross_profile_read_denied"] = True
        payload = {k: first[k] for k in ("title", "room_id", "starts_at", "ends_at", "attendees")}
        assert morgan.post("/bookings", json=payload).status_code == 409
        checks["cross_profile_overlap_denied"] = True
        assert blake.post("/bookings", json={**payload, "attendees": 31}).status_code == 422
        checks["over_capacity_denied"] = True
        assert (
            blake.put(
                "/bookings/" + first["id"], headers={"If-Match": "99"}, json=payload
            ).status_code
            == 409
        )
        checks["stale_edit_denied"] = True
        for capacity in [1, 2, 4, 6, 8, 12, 16, 24, 30, 31]:
            for hour in [8, 9, 12, 16]:
                start = day.replace(hour=hour)
                response = blake.get(
                    "/availability",
                    params={
                        "starts_at": start.isoformat(),
                        "ends_at": (start + timedelta(hours=1)).isoformat(),
                        "min_capacity": capacity,
                    },
                )
                response.raise_for_status()
                assert all(r["capacity"] >= capacity for r in response.json()["rooms"])
        checks["forty_capacity_time_searches"] = True
        result = {
            "recorded_at": datetime.now(ZoneInfo("America/New_York")).isoformat(),
            "fixture_note": "48 local sample bookings; real HTTP 503 faults; backoff clock accelerated; one pending task per profile held for 30 minutes. Not production data or latency benchmarks.",
            "historical_fixtures": historical,
            "checks": checks,
            "profiles": profiles,
        }
        (ROOT / "artifacts" / "profile-scenarios.json").write_text(
            json.dumps(result, indent=2, default=str) + "\n"
        )
        print(
            json.dumps(
                {"checks": checks, "profiles": {n: p["created"] for n, p in profiles.items()}},
                indent=2,
            )
        )
    finally:
        for client in clients.values():
            client.close()


if __name__ == "__main__":
    main()
