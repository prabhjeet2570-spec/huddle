import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from psycopg.types.json import Jsonb

from app.db import connect


def proposal(api, **changes):
    user = api("GET", "/session").json()
    start = datetime.now(UTC) + timedelta(days=2)
    arguments = {
        "room_id": "cedar",
        "title": "Exact action",
        "attendees": 2,
        "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(hours=1)).isoformat(),
        **changes,
    }
    identity = uuid4()
    with connect() as conn:
        conn.execute(
            "INSERT INTO proposals(id,owner_id,arguments) VALUES (%s,%s,%s)",
            (identity, user["id"], Jsonb(arguments)),
        )
    return identity, arguments


def test_approval_executes_once_with_exact_arguments(api):
    identity, arguments = proposal(api)
    path = f"/assistant/proposals/{identity}/approve"
    first = api("POST", path)
    assert first.status_code == 200
    assert api("POST", path).json() == first.json()
    assert first.json()["title"] == arguments["title"]
    assert len(api("GET", "/bookings").json()) == 1


@pytest.mark.parametrize("state", ["dismissed", "superseded"])
def test_inactive_proposal_cannot_execute(api, state):
    identity, _ = proposal(api)
    with connect() as conn:
        conn.execute("UPDATE proposals SET state=%s WHERE id=%s", (state, identity))
    assert api("POST", f"/assistant/proposals/{identity}/approve").status_code == 409
    assert api("GET", "/bookings").json() == []


def test_expired_and_wrong_owner_proposals(api):
    identity, _ = proposal(api)
    with connect() as conn:
        conn.execute(
            "UPDATE proposals SET expires_at=now()-interval '1 second' WHERE id=%s", (identity,)
        )
    assert api("POST", f"/assistant/proposals/{identity}/approve").status_code == 409
    api("POST", "/session", json={"name": "Other"})
    assert api("POST", f"/assistant/proposals/{identity}/approve").status_code == 404


def test_changed_instruction_invalidates_old_proposal(api, monkeypatch):
    identity, _ = proposal(api)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only-placeholder")

    def fake(self, url, **kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": "What time would you prefer?"}}]},
        )

    monkeypatch.setattr(httpx.Client, "post", fake)
    response = api("POST", "/assistant/chat", json={"message": "yes but change rooms"})
    assert response.status_code == 200
    assert api("POST", f"/assistant/proposals/{identity}/approve").status_code == 409
    assert api("GET", "/bookings").json() == []


def test_model_can_only_propose(api, monkeypatch):
    _, arguments = proposal(api)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only-placeholder")

    def fake(self, url, **kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "usage": {"total_tokens": 50},
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "propose_booking",
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ],
                        }
                    }
                ],
            },
        )

    monkeypatch.setattr(httpx.Client, "post", fake)
    response = api("POST", "/assistant/chat", json={"message": "Book a room"})
    assert response.status_code == 200 and response.json()["proposal"]
    assert api("GET", "/bookings").json() == []
    assert (
        api(
            "POST", "/assistant/proposals/" + response.json()["proposal"]["id"] + "/approve"
        ).status_code
        == 200
    )


def test_missing_key_is_explicit(api, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert api("POST", "/assistant/chat", json={"message": "hello"}).status_code == 503


def test_reset_invalidates_pending_approval(api):
    identity, _ = proposal(api)
    assert api("DELETE", "/assistant/history").status_code == 200
    assert api("POST", f"/assistant/proposals/{identity}/approve").status_code == 409
    assert api("GET", "/assistant/history").json() == {"messages": [], "proposal": None}
