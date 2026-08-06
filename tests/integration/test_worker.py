from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import httpx

from app.db import connect
from app.worker import run_once


def booking(api):
    start = datetime.now(UTC) + timedelta(days=4)
    return api(
        "POST",
        "/bookings",
        json={
            "room_id": "cedar",
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=1)).isoformat(),
            "attendees": 2,
        },
    ).json()


def success(url, **kwargs):
    return httpx.Response(200, request=httpx.Request("PUT", url), json=kwargs["json"])


def test_worker_reconciles_and_is_idle_after_completion(api, monkeypatch):
    b = booking(api)
    remote = Mock(side_effect=success)
    monkeypatch.setattr(httpx, "put", remote)
    assert run_once()
    assert not run_once()
    assert remote.call_count == 1
    assert api("GET", "/bookings/" + b["id"]).json()["calendar_status"] == "synced"


def test_timeout_does_not_cancel_allocation(api, monkeypatch):
    b = booking(api)

    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("lost response")

    monkeypatch.setattr(httpx, "put", timeout)
    run_once()
    row = api("GET", "/bookings/" + b["id"]).json()
    assert row["status"] == "confirmed" and row["calendar_status"] == "pending"
    with connect() as conn:
        conn.execute("UPDATE outbox SET available_at=now()")
    monkeypatch.setattr(httpx, "put", success)
    run_once()
    assert api("GET", "/bookings/" + b["id"]).json()["calendar_status"] == "synced"


def test_expired_lease_is_recovered(api, monkeypatch):
    b = booking(api)
    with connect() as conn:
        conn.execute(
            "UPDATE outbox SET state='processing',lease_until=now()-interval '1 second',attempts=1"
        )
    monkeypatch.setattr(httpx, "put", success)
    run_once()
    assert api("GET", "/bookings/" + b["id"]).json()["calendar_status"] == "synced"
    with connect() as conn:
        assert conn.execute("SELECT attempts FROM outbox").fetchone()["attempts"] == 2


def test_superseded_work_does_not_send_old_payload(api, monkeypatch):
    b = booking(api)
    api("POST", "/bookings/" + b["id"] + "/cancel", headers={"If-Match": "1"})
    remote = Mock(side_effect=success)
    monkeypatch.setattr(httpx, "put", remote)
    run_once()
    run_once()
    assert remote.call_count == 1
    assert remote.call_args.kwargs["json"]["status"] == "cancelled"


def test_exhaustion_becomes_visible_review_task(api, monkeypatch):
    b = booking(api)
    with connect() as conn:
        conn.execute("UPDATE outbox SET attempts=5")

    def timeout(*args, **kwargs):
        raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(httpx, "put", timeout)
    run_once()
    row = api("GET", "/bookings/" + b["id"]).json()
    assert row["status"] == "confirmed" and row["calendar_status"] == "needs_review"
    assert api("POST", "/bookings/" + b["id"] + "/retry-sync").status_code == 200
    monkeypatch.setattr(httpx, "put", success)
    run_once()
    assert api("GET", "/bookings/" + b["id"]).json()["calendar_status"] == "synced"
