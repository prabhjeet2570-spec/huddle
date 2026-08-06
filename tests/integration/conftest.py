import asyncio
import os

import httpx
import pytest

from app.db import connect, migrate
from app.main import app


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a dedicated database ending in huddle_test")
    if not url.endswith("/huddle_test"):
        pytest.fail("Integration tests require a dedicated huddle_test database")
    monkeypatch.setenv("DATABASE_URL", url)
    migrate()
    with connect() as conn:
        conn.execute(
            "TRUNCATE sessions,bookings,requests,booking_events,outbox,worker_heartbeats,proposals,conversations,ai_runs CASCADE"
        )


@pytest.fixture
def api():
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    def request(method, path, **kwargs):
        return asyncio.run(client.request(method, path, **kwargs))

    request("POST", "/session", json={"name": "Integration user"})
    yield request
    asyncio.run(client.aclose())
