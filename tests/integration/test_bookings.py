import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.db import connect
from app.main import app


def payload(**changes):
    start = datetime.now(UTC) + timedelta(days=3)
    return {
        "room_id": "cedar",
        "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(hours=1)).isoformat(),
        "attendees": 3,
        **changes,
    }


def test_create_edit_cancel_rebook(api):
    p = payload()
    created = api("POST", "/bookings", json=p)
    assert created.status_code == 201
    booking = created.json()
    path = "/bookings/" + booking["id"]
    assert api("GET", path).json() == booking
    edited = api("PUT", path, json={**p, "title": "Changed"}, headers={"If-Match": "1"})
    assert edited.status_code == 200 and edited.json()["version"] == 2
    assert api("POST", path + "/cancel", headers={"If-Match": "1"}).status_code == 409
    cancelled = api("POST", path + "/cancel", headers={"If-Match": "2"})
    assert cancelled.json()["status"] == "cancelled"
    assert api("POST", path + "/cancel", headers={"If-Match": "2"}).json() == cancelled.json()
    assert api("POST", "/bookings", json=p).status_code == 201
    assert len(api("GET", "/bookings?status=cancelled").json()) == 1
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM outbox").fetchone()["n"] == 4


def test_conflicting_edit_retains_original(api):
    p = payload()
    original = api("POST", "/bookings", json=p).json()
    api("POST", "/bookings", json={**p, "room_id": "maple"})
    response = api(
        "PUT",
        "/bookings/" + original["id"],
        json={**p, "room_id": "maple"},
        headers={"If-Match": "1"},
    )
    assert response.status_code == 409
    assert api("GET", "/bookings/" + original["id"]).json() == original


def test_idempotency_key_replays_and_rejects_changed_payload(api):
    p = payload()
    headers = {"Idempotency-Key": "retry-me"}
    first = api("POST", "/bookings", json=p, headers=headers)
    assert api("POST", "/bookings", json=p, headers=headers).json() == first.json()
    assert (
        api("POST", "/bookings", json={**p, "title": "Different"}, headers=headers).status_code
        == 409
    )
    assert len(api("GET", "/bookings").json()) == 1


def test_ownership_is_enforced(api):
    b = api("POST", "/bookings", json=payload()).json()
    api("POST", "/session", json={"name": "Other user"})
    for path in ["/bookings/" + b["id"], "/bookings/" + b["id"] + "/events"]:
        assert api("GET", path).status_code == 404
    assert api("GET", "/bookings").json() == []
    assert (
        api("POST", "/bookings/" + b["id"] + "/cancel", headers={"If-Match": "1"}).status_code
        == 404
    )


def test_parallel_requests_have_one_winner(api):
    p = payload()

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post("/session", json={"name": "Concurrent client"})
            responses = await asyncio.gather(*[client.post("/bookings", json=p) for _ in range(24)])
            return [r.status_code for r in responses]

    codes = asyncio.run(run())
    assert codes.count(201) == 1 and codes.count(409) == 23
    with connect() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM bookings WHERE status='confirmed'").fetchone()[
                "n"
            ]
            == 1
        )


def test_database_itself_rejects_overlap(api):
    from psycopg.errors import ExclusionViolation

    b = api("POST", "/bookings", json=payload()).json()
    with pytest.raises(ExclusionViolation), connect() as conn:
        conn.execute(
            """INSERT INTO bookings(id,owner_id,room_id,title,starts_at,ends_at,attendees)
            SELECT %s,owner_id,room_id,title,starts_at,ends_at,attendees FROM bookings WHERE id=%s""",
            (uuid4(), b["id"]),
        )


def test_availability_schedule_and_adjacency(api):
    p = payload()
    b = api("POST", "/bookings", json=p).json()
    params = {k: p[k] for k in ("starts_at", "ends_at")}
    rooms = api("GET", "/availability", params=params).json()["rooms"]
    assert {r["id"] for r in rooms} == {"maple", "birch"}
    schedule = api("GET", "/rooms/cedar/schedule", params=params).json()
    assert schedule["free_windows"] == []
    assert set(schedule["bookings"][0]) == {"starts_at", "ends_at"}
    adjacent = {
        **p,
        "starts_at": p["ends_at"],
        "ends_at": (datetime.fromisoformat(p["ends_at"]) + timedelta(hours=1)).isoformat(),
    }
    assert api("POST", "/bookings", json=adjacent).status_code == 201
    api("POST", "/bookings/" + b["id"] + "/cancel", headers={"If-Match": "1"})
    assert len(api("GET", "/availability", params=params).json()["rooms"]) == 3


def test_csrf_origin_rejected(api):
    assert (
        api(
            "POST", "/bookings", json=payload(), headers={"Origin": "https://other.test"}
        ).status_code
        == 403
    )


def test_unauthenticated_api():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            assert (await c.get("/bookings")).status_code == 401

    asyncio.run(run())
