"""HTTP surface: auth, tenancy, the booking endpoints and the chat endpoint."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.domain.enums import ReservationState
from tests.conftest import at, requires_postgres

pytestmark = [requires_postgres, pytest.mark.postgres]


@pytest_asyncio.fixture
async def client(session_factory):
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as active:
        yield active


async def _login(client, org: str = "acme", username: str = "alice") -> str:
    response = await client.post(
        "/auth/login",
        json={"organization": org, "username": username, "password": "test-password"},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _booking_body(hour: int = 10, room: str = "A") -> dict:
    return {
        "room": room,
        "title": "Design review",
        "attendees": 3,
        "starts_at": at(hour, 0).isoformat(),
        "ends_at": at(hour + 1, 0).isoformat(),
    }


# --- Health and auth -------------------------------------------------------


async def test_health_needs_no_authentication(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_login_returns_a_bearer_token(client, tenant):
    token = await _login(client)
    response = await client.get("/auth/me", headers=_auth(token))

    assert response.status_code == 200
    assert response.json()["username"] == "alice"
    assert response.json()["organization"] == "acme"


@pytest.mark.parametrize(
    "payload",
    [
        {"organization": "acme", "username": "alice", "password": "wrong"},
        {"organization": "acme", "username": "nobody", "password": "test-password"},
        {"organization": "nosuchorg", "username": "alice", "password": "test-password"},
    ],
)
async def test_bad_credentials_are_rejected_identically(client, tenant, payload):
    """One message for every failure, so the endpoint cannot be used to probe."""
    response = await client.post("/auth/login", json=payload)

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid organization, username or password"


@pytest.mark.parametrize(
    "path", ["/auth/me", "/bookings/me", "/bookings/rooms", "/metrics"]
)
async def test_protected_endpoints_reject_anonymous_callers(client, tenant, path):
    assert (await client.get(path)).status_code == 401


async def test_a_garbage_token_is_rejected(client, tenant):
    response = await client.get("/auth/me", headers=_auth("not-a-real-jwt"))
    assert response.status_code == 401


# --- Bookings --------------------------------------------------------------


async def test_the_full_hold_confirm_cancel_cycle(client, tenant):
    token = await _login(client)

    held = await client.post(
        "/bookings/holds", json=_booking_body(), headers=_auth(token)
    )
    assert held.status_code == 201
    reference = held.json()["reference"]
    assert held.json()["state"] == ReservationState.HELD
    assert held.json()["hold_expires_at"] is not None

    confirmed = await client.post(
        f"/bookings/{reference}/confirm", headers=_auth(token)
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["state"] == ReservationState.CONFIRMED

    listed = await client.get("/bookings/me", headers=_auth(token))
    assert [item["reference"] for item in listed.json()] == [reference]

    cancelled = await client.delete(f"/bookings/{reference}", headers=_auth(token))
    assert cancelled.status_code == 200
    assert (await client.get("/bookings/me", headers=_auth(token))).json() == []


async def test_a_conflicting_hold_returns_409_with_alternatives(client, tenant):
    token = await _login(client)
    await client.post("/bookings/holds", json=_booking_body(), headers=_auth(token))

    clash = await client.post(
        "/bookings/holds", json=_booking_body(), headers=_auth(token)
    )

    assert clash.status_code == 409
    assert "not available" in clash.json()["detail"]


async def test_an_invalid_booking_request_is_rejected(client, tenant):
    token = await _login(client)

    response = await client.post(
        "/bookings/holds",
        json={**_booking_body(), "starts_at": at(10, 15).isoformat()},
        headers=_auth(token),
    )

    assert response.status_code == 409
    assert "30-minute" in response.json()["detail"]


async def test_availability_reflects_a_live_hold(client, tenant):
    token = await _login(client)
    params = {
        "starts_at": at(10, 0).isoformat(),
        "ends_at": at(11, 0).isoformat(),
        "attendees": 3,
    }

    before = await client.get(
        "/bookings/available", params=params, headers=_auth(token)
    )
    assert [room["name"] for room in before.json()] == ["A", "B", "C", "D", "E"]

    await client.post("/bookings/holds", json=_booking_body(), headers=_auth(token))

    after = await client.get("/bookings/available", params=params, headers=_auth(token))
    assert [room["name"] for room in after.json()] == ["B", "C", "D", "E"]


async def test_availability_filters_by_capacity(client, tenant):
    token = await _login(client)

    response = await client.get(
        "/bookings/available",
        params={
            "starts_at": at(10, 0).isoformat(),
            "ends_at": at(11, 0).isoformat(),
            "attendees": 10,
        },
        headers=_auth(token),
    )

    assert [room["name"] for room in response.json()] == ["D", "E"]


# --- Tenancy ---------------------------------------------------------------


async def test_rooms_are_scoped_to_the_callers_organization(
    client, tenant, other_tenant
):
    acme = await _login(client, "acme", "alice")
    globex = await _login(client, "globex", "alice")

    acme_rooms = (await client.get("/bookings/rooms", headers=_auth(acme))).json()
    globex_rooms = (await client.get("/bookings/rooms", headers=_auth(globex))).json()

    assert [room["name"] for room in acme_rooms] == ["A", "B", "C", "D", "E"]
    assert [room["name"] for room in globex_rooms] == ["A", "B"]
    assert acme_rooms[0]["capacity"] != globex_rooms[0]["capacity"]


async def test_both_tenants_can_hold_their_own_room_a_at_the_same_time(
    client, tenant, other_tenant
):
    acme = await _login(client, "acme", "alice")
    globex = await _login(client, "globex", "alice")

    first = await client.post(
        "/bookings/holds", json=_booking_body(), headers=_auth(acme)
    )
    second = await client.post(
        "/bookings/holds", json=_booking_body(), headers=_auth(globex)
    )

    assert first.status_code == 201
    assert second.status_code == 201


async def test_a_tenant_cannot_cancel_another_tenants_booking(
    client, tenant, other_tenant
):
    acme = await _login(client, "acme", "alice")
    globex = await _login(client, "globex", "alice")

    held = await client.post(
        "/bookings/holds", json=_booking_body(), headers=_auth(acme)
    )
    reference = held.json()["reference"]

    response = await client.delete(f"/bookings/{reference}", headers=_auth(globex))

    assert response.status_code == 409
    assert "not found" in response.json()["detail"]


async def test_a_user_cannot_cancel_a_colleagues_booking(client, tenant):
    alice = await _login(client, "acme", "alice")
    bob = await _login(client, "acme", "bob")

    held = await client.post(
        "/bookings/holds", json=_booking_body(), headers=_auth(alice)
    )
    reference = held.json()["reference"]

    assert (
        await client.delete(f"/bookings/{reference}", headers=_auth(bob))
    ).status_code == 409


# --- Chat ------------------------------------------------------------------


async def test_the_chat_endpoint_returns_a_conversation_id(client, tenant, monkeypatch):
    from langchain_core.messages import AIMessage

    from app.agent import runner as runner_module
    from app.agent.llm import FakeChatModel

    original = runner_module.ConversationRunner.__init__

    def patched(self, session, *, model=None, clock=None):
        original(
            self,
            session,
            model=FakeChatModel([AIMessage(content="Rooms A through E.")]),
            clock=clock,
        )

    monkeypatch.setattr(runner_module.ConversationRunner, "__init__", patched)

    token = await _login(client)
    response = await client.post(
        "/chat", json={"message": "what rooms are there?"}, headers=_auth(token)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["response"] == "Rooms A through E."
    assert body["conversation_id"]
    assert body["status"] == "completed"


async def test_chat_rejects_an_empty_message(client, tenant):
    token = await _login(client)
    response = await client.post("/chat", json={"message": ""}, headers=_auth(token))
    assert response.status_code == 422


# --- Ops -------------------------------------------------------------------


async def test_metrics_are_served_and_shaped(client, tenant):
    token = await _login(client)
    response = await client.get("/metrics", headers=_auth(token))

    assert response.status_code == 200
    names = {metric["name"] for metric in response.json()["metrics"]}
    assert {
        "task_completion_rate",
        "escalation_rate",
        "wrong_tool_call_rate",
        "compensation_frequency",
        "turn_latency_p50_ms",
        "turn_latency_p95_ms",
        "tool_calls_per_confirmed_booking",
    } <= names
    # Every metric states how it is computed.
    assert all(metric["definition"] for metric in response.json()["metrics"])


async def test_the_alert_queue_is_admin_only(client, tenant):
    admin = await _login(client, "acme", "alice")
    member = await _login(client, "acme", "bob")

    assert (await client.get("/alerts", headers=_auth(admin))).status_code == 200
    assert (await client.get("/alerts", headers=_auth(member))).status_code == 403
