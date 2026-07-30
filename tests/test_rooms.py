import asyncio

import httpx
import pytest

from app.main import app


def get_rooms(query=""):
    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get(f"/rooms{query}")

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
