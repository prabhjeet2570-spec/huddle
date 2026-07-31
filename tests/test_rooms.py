import asyncio

import httpx
import pytest

from app.main import app


def get_rooms(suffix=""):
    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get(f"/rooms{suffix}")

    return asyncio.run(request())


def test_room_catalog():
    response = get_rooms()

    assert response.status_code == 200
    assert response.json() == [
        {"id": "cedar", "name": "Cedar", "capacity": 4},
        {"id": "maple", "name": "Maple", "capacity": 8},
        {"id": "birch", "name": "Birch", "capacity": 12},
    ]


@pytest.mark.parametrize(
    ("capacity", "expected_ids"),
    [
        (4, ["cedar", "maple", "birch"]),
        (5, ["maple", "birch"]),
        (12, ["birch"]),
        (13, []),
    ],
)
def test_minimum_capacity_filter(capacity, expected_ids):
    response = get_rooms(f"?min_capacity={capacity}")

    assert response.status_code == 200
    assert [room["id"] for room in response.json()] == expected_ids


@pytest.mark.parametrize("capacity", ["0", "-1", "many", "2.5"])
def test_invalid_capacity_is_rejected(capacity):
    response = get_rooms(f"?min_capacity={capacity}")

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "min_capacity"]


def test_each_catalog_room_can_be_looked_up():
    catalog = get_rooms().json()

    for room in catalog:
        response = get_rooms(f"/{room['id']}")

        assert response.status_code == 200
        assert response.json() == room


@pytest.mark.parametrize("room_id", ["missing", "Cedar", "ced"])
def test_unknown_room_returns_not_found(room_id):
    response = get_rooms(f"/{room_id}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Room not found"}
