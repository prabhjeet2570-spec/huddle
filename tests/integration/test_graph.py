"""Exercise real graph checkpoints and transactional booking guarantees."""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

from app.assistant import approve
from app.db import connect
from app.rooms import ROOMS


def start(api, scenario="booking"):
    response = api("POST", "/assistant/demo", json={"scenario": scenario})
    assert response.status_code == 200, response.text
    return response.json()["proposal"]


def test_demo_pauses_without_booking(api, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    proposal = start(api)
    assert api("GET", "/bookings").json() == []
    state = api("GET", "/assistant/workflow").json()["workflow"]
    assert state["status"] == "pending" and state["next"] == ["approval"]
    assert state["id"] == proposal["id"] and state["demo"]
    booked = api("POST", f"/assistant/proposals/{proposal['id']}/approve")
    assert booked.status_code == 200
    assert api("GET", "/assistant/workflow").json()["workflow"]["status"] == "approved"


def test_conflict_offers_new_proposal_without_silent_booking(api):
    proposal = start(api, "conflict")
    response = api("POST", f"/assistant/proposals/{proposal['id']}/approve")
    assert response.status_code == 409
    assert api("GET", "/bookings").json() == []
    assert {r["id"] for r in response.json()["alternatives"]} == {
        r.id for r in ROOMS if r.id != "cedar" and r.capacity >= proposal["arguments"]["attendees"]
    }
    state = api("GET", "/assistant/workflow").json()["workflow"]
    assert state["status"] == "conflict" and not state["next"]
    alternative = api(
        "POST", f"/assistant/proposals/{proposal['id']}/alternative", json={"room_id": "maple"}
    )
    assert alternative.status_code == 200
    assert api("GET", "/bookings").json() == []
    new = alternative.json()["proposal"]
    assert new["arguments"]["starts_at"] == proposal["arguments"]["starts_at"]
    booked = api("POST", f"/assistant/proposals/{new['id']}/approve")
    assert booked.status_code == 200 and booked.json()["room_id"] == "maple"


def test_concurrent_approvals_create_one_booking_and_outbox(api):
    proposal = start(api)
    user = api("GET", "/session").json()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: approve(UUID(proposal["id"]), user=user), range(6)))
    assert len({str(r["id"]) for r in results}) == 1
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM bookings").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM outbox").fetchone()["n"] == 1


def test_checkpoint_does_not_bypass_dismissal_or_owner(api):
    proposal = start(api)
    api("DELETE", f"/assistant/proposals/{proposal['id']}")
    assert api("POST", f"/assistant/proposals/{proposal['id']}/approve").status_code == 409
    api("POST", "/session", json={"name": "Other student"})
    assert api("GET", "/assistant/workflow").json() == {"workflow": None}
    assert api("POST", f"/assistant/proposals/{proposal['id']}/approve").status_code == 404


def test_expired_checkpoint_cannot_resume(api):
    proposal = start(api)
    with connect() as conn:
        conn.execute(
            "UPDATE proposals SET expires_at=now()-interval '1 second' WHERE id=%s",
            (proposal["id"],),
        )
    assert api("GET", "/assistant/workflow").json()["workflow"]["status"] == "expired"
    assert api("POST", f"/assistant/proposals/{proposal['id']}/approve").status_code == 409


def test_reset_leaves_no_resumable_action(api):
    proposal = start(api)
    api("DELETE", "/assistant/history")
    assert api("GET", "/assistant/workflow").json() == {"workflow": None}
    assert api("POST", f"/assistant/proposals/{proposal['id']}/approve").status_code == 409


def test_no_alternatives_means_no_fallback_booking(api):
    proposal = start(api, "conflict")
    for room in (
        r.id for r in ROOMS if r.id != "cedar" and r.capacity >= proposal["arguments"]["attendees"]
    ):
        response = api("POST", "/bookings", json={**proposal["arguments"], "room_id": room})
        assert response.status_code == 201
    response = api("POST", f"/assistant/proposals/{proposal['id']}/approve")
    assert response.status_code == 409 and response.json()["alternatives"] == []
    assert len(api("GET", "/bookings").json()) == sum(
        r.id != "cedar" and r.capacity >= proposal["arguments"]["attendees"] for r in ROOMS
    )
    assert (
        api(
            "POST", f"/assistant/proposals/{proposal['id']}/alternative", json={"room_id": "maple"}
        ).status_code
        == 409
    )


def test_model_cannot_propose_an_already_occupied_room(api, monkeypatch):
    import json

    import httpx

    proposed = start(api, "conflict")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-placeholder")

    def model(self, url, **kwargs):
        message = {
            "tool_calls": [
                {
                    "id": "occupied",
                    "type": "function",
                    "function": {
                        "name": "propose_booking",
                        "arguments": json.dumps(proposed["arguments"]),
                    },
                }
            ]
        }
        return httpx.Response(
            200, request=httpx.Request("POST", url), json={"choices": [{"message": message}]}
        )

    monkeypatch.setattr(httpx.Client, "post", model)
    result = api("POST", "/assistant/chat", json={"message": "Cedar only, please"})
    assert result.status_code == 200 and result.json()["proposal"] is None
    assert result.json()["outcome"] == "unavailable"
    assert api("GET", "/bookings").json() == []


def test_approval_posts_confirmation_once(api):
    proposal = start(api)
    path = f"/assistant/proposals/{proposal['id']}/approve"
    assert api("POST", path).status_code == 200
    assert api("POST", path).status_code == 200
    messages = api("GET", "/assistant/history").json()["messages"]
    confirmations = [m for m in messages if "Your room is booked." in m["content"]]
    assert len(confirmations) == 1
    assert "Room 101" in confirmations[0]["content"]
