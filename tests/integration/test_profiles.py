from app.db import connect
from app.rooms import ROOMS


def test_demo_profile_switch_keeps_ownership_separate(api, monkeypatch):
    monkeypatch.setenv("HUDDLE_DEMO_PROFILES", "true")
    assert api("GET", "/demo/profiles").json()["names"] == ["Blake", "Morgan", "Jake", "Ashley"]
    blake = api("POST", "/demo/profiles/Blake").json()
    assert api("POST", "/demo/profiles/Blake").json()["id"] == blake["id"]
    assert api("POST", "/demo/profiles/Jake").json()["id"] != blake["id"]
    assert api("POST", "/demo/profiles/Unknown").status_code == 404


def test_demo_profiles_are_disabled_by_default(api, monkeypatch):
    monkeypatch.delenv("HUDDLE_DEMO_PROFILES", raising=False)
    assert api("GET", "/demo/profiles").json() == {"names": []}
    assert api("POST", "/demo/profiles/Blake").status_code == 404


def test_database_catalog_matches_application_catalog(api):
    with connect() as conn:
        rows = conn.execute("SELECT id,name,capacity FROM room_catalog ORDER BY id").fetchall()
    assert rows == sorted([r.model_dump(exclude={"floor"}) for r in ROOMS], key=lambda r: r["id"])


def test_expanded_room_capacity_is_enforced_in_sql(api):
    from datetime import UTC, datetime, timedelta

    import pytest
    from psycopg.errors import CheckViolation

    start = datetime.now(UTC) + timedelta(days=5)
    response = api(
        "POST",
        "/bookings",
        json={
            "room_id": "forum",
            "attendees": 30,
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 201
    with pytest.raises(CheckViolation), connect() as conn:
        conn.execute("UPDATE bookings SET attendees=31 WHERE id=%s", (response.json()["id"],))
