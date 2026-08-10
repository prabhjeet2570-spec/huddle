"""Verify persisted approval across fresh Python processes. No model key needed.

Uses only huddle_test; leaves uniquely scoped synthetic records for inspection.
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def child(action, payload):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        if action == "prepare":
            client.post("/session", json={"name": "Restart demonstration"}).raise_for_status()
            proposal = client.post("/assistant/demo", json={"scenario": "restart"})
            proposal.raise_for_status()
            return {
                "cookie": client.cookies.get("huddle_session"),
                "proposal_id": proposal.json()["proposal"]["id"],
            }
        client.cookies.set("huddle_session", payload["cookie"])
        before = client.get("/assistant/workflow").json()["workflow"]
        first = client.post(f"/assistant/proposals/{payload['proposal_id']}/approve")
        first.raise_for_status()
        second = client.post(f"/assistant/proposals/{payload['proposal_id']}/approve")
        second.raise_for_status()
        bookings = client.get("/bookings").json()
        return {
            "restored_status": before["status"],
            "restored_next": before["next"],
            "same_result_on_repeated_approval": first.json() == second.json(),
            "booking_count": len(bookings),
            "status": client.get("/assistant/workflow").json()["workflow"]["status"],
        }


def main():
    url = os.getenv("TEST_DATABASE_URL", "")
    if not url.endswith("/huddle_test"):
        raise SystemExit("Set TEST_DATABASE_URL to a dedicated huddle_test database")
    os.environ["DATABASE_URL"] = url
    if len(sys.argv) > 1:
        print(json.dumps(child(sys.argv[1], json.load(sys.stdin))))
        return
    from app.db import migrate

    migrate()

    def process(action, payload):
        result = subprocess.run(
            [sys.executable, __file__, action],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=True,
        )
        return json.loads(result.stdout)

    # The first process exits completely. No in-memory graph survives into the second.
    prepared = process("prepare", {})
    evidence = process("resume", prepared)
    assert evidence == {
        "restored_status": "pending",
        "restored_next": ["approval"],
        "same_result_on_repeated_approval": True,
        "booking_count": 1,
        "status": "approved",
    }, evidence
    artifact = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "scenario": "Approval checkpoint restored by a fresh Python process",
        "model_calls": 0,
        "checks": evidence,
    }
    Path("artifacts/workflow-restart.json").write_text(json.dumps(artifact, indent=2) + "\n")
    print(json.dumps(artifact, indent=2))


if __name__ == "__main__":
    main()
