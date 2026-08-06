"""Reproduce contention, retries and process death using a disposable database.

Run with TEST_DATABASE_URL ending in /huddle_test. This resets ONLY that database.
No model requests are made. The calendar runs as a separate HTTP process.
"""

import asyncio
import json
import os
import platform
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url.endswith("/huddle_test"):
        raise SystemExit(
            "TEST_DATABASE_URL must point to a dedicated /huddle_test database; it will be reset."
        )
    os.environ["DATABASE_URL"] = url
    from app.db import connect, migrate
    from app.main import app
    from app.worker import run_once

    migrate()
    with connect() as conn:
        conn.execute(
            "TRUNCATE sessions,bookings,requests,booking_events,outbox,worker_heartbeats,proposals,conversations,ai_runs CASCADE"
        )
    with tempfile.TemporaryDirectory(prefix="huddle-calendar-") as directory:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        calendar_url = f"http://127.0.0.1:{port}"
        os.environ["CALENDAR_URL"] = calendar_url
        os.environ["CALENDAR_TOKEN"] = "isolated-demo-token"
        env = {**os.environ, "CALENDAR_DB": str(Path(directory) / "events.sqlite")}
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.calendar_simulator:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                try:
                    if httpx.get(calendar_url + "/openapi.json", timeout=0.3).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Calendar simulator did not start")

            async def experiment():
                evidence = {
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "commit": subprocess.check_output(
                        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                    ).strip(),
                    "python": platform.python_version(),
                    "machine": platform.machine(),
                    "database": "PostgreSQL",
                    "calendar": "independent HTTP simulator with SQLite storage",
                    "scope": "synthetic workload; no live model or real calendar provider",
                    "trials": [],
                }
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    await client.post("/session", json={"name": "Recovery evaluation"})
                    base = datetime.now(UTC) + timedelta(days=10)

                    def payload(offset=0, room="cedar"):
                        start = base + timedelta(hours=offset)
                        return {
                            "room_id": room,
                            "starts_at": start.isoformat(),
                            "ends_at": (start + timedelta(hours=1)).isoformat(),
                            "attendees": 2,
                            "title": "Reproducible recovery demo",
                        }

                    async def create(p, headers=None):
                        start = time.perf_counter()
                        response = await client.post("/bookings", json=p, headers=headers)
                        return response, round((time.perf_counter() - start) * 1000, 3)

                    responses = await asyncio.gather(*[create(payload()) for _ in range(24)])
                    codes = [r.status_code for r, _ in responses]
                    assert codes.count(201) == 1 and codes.count(409) == 23, codes
                    evidence["contention"] = {
                        "attempts": 24,
                        "created": codes.count(201),
                        "conflicts": codes.count(409),
                        "unexpected_errors": sum(c not in (201, 409) for c in codes),
                        "latencies_ms": [ms for _, ms in responses],
                    }
                    responses = await asyncio.gather(
                        *[
                            create(payload(2), {"Idempotency-Key": "same-logical-request"})
                            for _ in range(12)
                        ]
                    )
                    identities = {r.json()["id"] for r, _ in responses}
                    assert len(identities) == 1
                    evidence["idempotency"] = {
                        "attempts": 12,
                        "logical_bookings": len(identities),
                        "latencies_ms": [ms for _, ms in responses],
                    }
                    while run_once():
                        pass
                    for index, phase in enumerate(
                        ["response_lost", "before_remote", "after_remote", "after_ack"]
                    ):
                        created, _ = await create(payload(4 + index * 2))
                        assert created.status_code == 201, created.text
                        booking_id = created.json()["id"]
                        started = time.perf_counter()
                        exit_code = None
                        if phase == "response_lost":
                            armed = httpx.post(
                                calendar_url + "/faults/" + booking_id,
                                headers={"Authorization": "Bearer isolated-demo-token"},
                            )
                            armed.raise_for_status()
                            run_once()
                        else:
                            code = """import os,httpx
from app.worker import run_once
original=httpx.put
phase=os.environ['CRASH_PHASE']
def send(*args,**kwargs):
    if phase=='before_remote': os._exit(71)
    result=original(*args,**kwargs)
    if phase=='after_remote': os._exit(72)
    return result
httpx.put=send
run_once()
os._exit(73)
"""
                            process = subprocess.run(
                                [sys.executable, "-c", code],
                                env={**env, "CRASH_PHASE": phase},
                                cwd=ROOT,
                                timeout=15,
                            )
                            exit_code = process.returncode
                            assert (
                                exit_code
                                == {"before_remote": 71, "after_remote": 72, "after_ack": 73}[phase]
                            )
                        with connect() as conn:
                            interrupted = conn.execute(
                                "SELECT state,attempts FROM outbox WHERE booking_id=%s",
                                (booking_id,),
                            ).fetchone()
                        deadline = time.monotonic() + 40
                        while time.monotonic() < deadline:
                            run_once()
                            result = (await client.get("/bookings/" + booking_id)).json()
                            if result["calendar_status"] == "synced":
                                break
                            await asyncio.sleep(0.25)
                        assert (
                            result["status"] == "confirmed"
                            and result["calendar_status"] == "synced"
                        ), result
                        with sqlite3.connect(env["CALENDAR_DB"]) as db:
                            count = db.execute(
                                "SELECT count(*) FROM events WHERE id=?", (booking_id,)
                            ).fetchone()[0]
                        assert count == 1
                        with connect() as conn:
                            attempts = conn.execute(
                                "SELECT attempts FROM outbox WHERE booking_id=%s", (booking_id,)
                            ).fetchone()["attempts"]
                        evidence["trials"].append(
                            {
                                "fault": phase,
                                "process_exit": exit_code,
                                "interrupted_job": interrupted,
                                "attempts": attempts,
                                "calendar_events": count,
                                "reservation_status": result["status"],
                                "calendar_status": result["calendar_status"],
                                "elapsed_seconds": round(time.perf_counter() - started, 3),
                            }
                        )
                        print(
                            f"{phase}: recovered, {attempts} attempt(s), {count} calendar event",
                            flush=True,
                        )
                    with connect() as conn:
                        evidence["overlapping_active_pairs"] = conn.execute(
                            """SELECT count(*) AS n FROM bookings a JOIN bookings b ON a.id<b.id AND a.room_id=b.room_id AND a.starts_at<b.ends_at AND b.starts_at<a.ends_at WHERE a.status='confirmed' AND b.status='confirmed'"""
                        ).fetchone()["n"]
                        evidence["unresolved_jobs"] = conn.execute(
                            "SELECT count(*) AS n FROM outbox WHERE state NOT IN ('done','superseded')"
                        ).fetchone()["n"]
                    assert (
                        evidence["overlapping_active_pairs"] == 0
                        and evidence["unresolved_jobs"] == 0
                    )
                return evidence

            result = asyncio.run(experiment())
            target = ROOT / "artifacts" / "recovery-results.json"
            target.parent.mkdir(exist_ok=True)
            target.write_text(json.dumps(result, indent=2) + "\n")
            print("Saved " + str(target))
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    main()
