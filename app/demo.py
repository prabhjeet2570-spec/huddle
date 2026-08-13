"""Scripted local demonstrations using the real graph and booking transaction.

Fixtures bypass the model, not the approval or database safeguards.
"""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from app.assistant import lock_workspace
from app.assistant_graph import build_graph, config
from app.bookings import BookingRequest, create_in_transaction
from app.db import connect
from app.session import current_user

router = APIRouter(prefix="/assistant", tags=["local demo"])


class DemoInput(BaseModel):
    scenario: Literal["booking", "conflict", "restart"] = "booking"


def pause_proposal(conn, user, arguments, message):
    identity = uuid4()
    row = conn.execute(
        "INSERT INTO proposals(id,owner_id,arguments,workflow_id) VALUES (%s,%s,%s,%s) RETURNING expires_at",
        (identity, user["id"], Jsonb(arguments), identity),
    ).fetchone()
    proposal = {
        "id": str(identity),
        "arguments": arguments,
        "expires_at": row["expires_at"].isoformat(),
    }
    result = build_graph(conn).invoke(
        {
            "owner_id": str(user["id"]),
            "workflow_id": str(identity),
            "proposal": proposal,
            "trace": ["scripted fixture", "proposal"],
            "outcome": "proposed",
            "message": message,
            "model": "scripted-demo",
            "alternatives": [],
        },
        config(identity),
        durability="sync",
    )
    conn.execute(
        "INSERT INTO conversations(owner_id,messages,workflow_id) VALUES (%s,%s,%s) ON CONFLICT(owner_id) DO UPDATE SET messages=excluded.messages,workflow_id=excluded.workflow_id",
        (user["id"], Jsonb([{"role": "assistant", "content": message}]), identity),
    )
    return {"proposal": result["proposal"], "message": message}


@router.post("/demo")
def demo(body: DemoInput, user=Depends(current_user)):
    with connect() as conn:
        lock_workspace(conn, user)
        conn.execute(
            "UPDATE proposals SET state='superseded' WHERE owner_id=%s AND state='pending'",
            (user["id"],),
        )
        # Unique future windows keep repeated demos from interfering with one another.
        start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=2)
        for _ in range(100):
            occupied = conn.execute(
                "SELECT 1 FROM bookings WHERE status='confirmed' AND tstzrange(starts_at,ends_at,'[)') && tstzrange(%s,%s,'[)') LIMIT 1",
                (start, start + timedelta(hours=1)),
            ).fetchone()
            if not occupied:
                break
            start += timedelta(hours=1)
        else:
            raise HTTPException(409, "No clear demo window found. Cancel old demo bookings first.")
        arguments = BookingRequest(
            room_id="cedar",
            title="Study session · " + body.scenario,
            attendees=3,
            starts_at=start,
            ends_at=start + timedelta(hours=1),
        ).model_dump(mode="json")
        messages = {
            "booking": "Sample booking: review the details, then approve to reserve this room.",
            "conflict": "Sample conflict: another student has taken Cedar. Try approving this request to see the available alternatives.",
            "restart": "Saved-request demo: this proposal will still be here after restarting the app. Return in the same browser and approve within five minutes.",
        }
        result = pause_proposal(conn, user, arguments, messages[body.scenario])
        if body.scenario == "conflict":
            other = uuid4()
            conn.execute(
                "INSERT INTO sessions(id,name,token_hash,expires_at) VALUES (%s,'Simulated student',%s,now()+interval '1 day')",
                (other, "demo-" + str(other)),
            )
            create_in_transaction(conn, BookingRequest.model_validate(arguments), other)
        return result


class AlternativeInput(BaseModel):
    room_id: str


@router.post("/proposals/{proposal_id}/alternative")
def alternative(proposal_id: UUID, body: AlternativeInput, user=Depends(current_user)):
    with connect() as conn:
        lock_workspace(conn, user)
        row = conn.execute(
            "SELECT * FROM proposals WHERE id=%s AND owner_id=%s", (proposal_id, user["id"])
        ).fetchone()
        latest = conn.execute(
            "SELECT workflow_id FROM conversations WHERE owner_id=%s", (user["id"],)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Proposal not found")
        if row["state"] != "conflict" or not latest or latest["workflow_id"] != row["workflow_id"]:
            raise HTTPException(409, "Start a new search; this workflow is no longer current")
        from app.availability import AvailabilityQuery, search_availability

        args = {**row["arguments"], "room_id": body.room_id}
        request = BookingRequest.model_validate(args)
        rooms = search_availability(
            AvailabilityQuery(
                starts_at=request.starts_at, ends_at=request.ends_at, min_capacity=request.attendees
            )
        ).rooms
        if body.room_id not in {r.id for r in rooms}:
            raise HTTPException(409, "That alternative is no longer available. Search again.")
        return pause_proposal(
            conn,
            user,
            args,
            "Review this alternative for the same time and group size. It requires a new approval.",
        )
