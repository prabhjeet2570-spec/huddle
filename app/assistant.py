"""HTTP ownership boundary for the persistent LangGraph assistant."""

import os
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from langgraph.types import Command
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from app.assistant_graph import build_graph, config
from app.assistant_model import system_prompt
from app.bookings import Booking
from app.db import connect
from app.session import current_user

router = APIRouter(prefix="/assistant", tags=["assistant"])


class ChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    timezone: str = "America/New_York"


@router.get("/status")
def status():
    return {
        "configured": bool(os.getenv("OPENROUTER_API_KEY")),
        "model": os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
        "orchestrator": "LangGraph",
    }


@router.get("/history")
def history(user=Depends(current_user)):
    with connect() as conn:
        row = conn.execute(
            "SELECT messages FROM conversations WHERE owner_id=%s", (user["id"],)
        ).fetchone()
        proposal = conn.execute(
            "SELECT id,arguments,expires_at FROM proposals WHERE owner_id=%s AND state='pending' AND expires_at>now() ORDER BY created_at DESC LIMIT 1",
            (user["id"],),
        ).fetchone()
    return {"messages": row["messages"] if row else [], "proposal": proposal}


@router.get("/workflow")
def workflow(user=Depends(current_user)):
    with connect() as conn:
        row = conn.execute(
            "SELECT workflow_id FROM conversations WHERE owner_id=%s", (user["id"],)
        ).fetchone()
        if not row or not row["workflow_id"]:
            return {"workflow": None}
        snapshot = build_graph(conn).get_state(config(row["workflow_id"]))
        values = snapshot.values
        proposal = conn.execute(
            "SELECT state,expires_at FROM proposals WHERE id=%s AND owner_id=%s",
            (row["workflow_id"], user["id"]),
        ).fetchone()
        outcome = values.get("outcome", "unknown")
        if proposal:
            outcome = proposal["state"]
            if outcome == "pending" and proposal["expires_at"] <= datetime.now(UTC):
                outcome = "expired"
        return {
            "workflow": {
                "id": str(row["workflow_id"]),
                "status": outcome,
                "next": list(snapshot.next),
                "trace": values.get("trace", []),
                "alternatives": values.get("alternatives", []),
                "message": values.get("message"),
                "demo": values.get("model") == "scripted-demo",
            }
        }


def lock_workspace(conn, user):
    conn.execute("SELECT id FROM sessions WHERE id=%s FOR UPDATE", (user["id"],))


@router.post("/chat")
def chat(body: ChatInput, user=Depends(current_user)):
    if not os.getenv("OPENROUTER_API_KEY"):
        raise HTTPException(
            503,
            "Assistant is not connected. Add OPENROUTER_API_KEY to the server .env; guided demos work without it.",
        )
    try:
        ZoneInfo(body.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(422, "Unknown timezone") from None
    started = time.monotonic()
    model = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    workflow_id = str(uuid4())
    with connect() as conn:
        lock_workspace(conn, user)
        count = conn.execute(
            "SELECT count(*) AS n FROM ai_runs WHERE owner_id=%s AND created_at>now()-interval '1 hour'",
            (user["id"],),
        ).fetchone()["n"]
        if count >= 30:
            raise HTTPException(429, "Assistant budget reached: 30 requests per workspace per hour")
        conn.execute(
            "UPDATE proposals SET state='superseded' WHERE owner_id=%s AND state='pending'",
            (user["id"],),
        )
        saved = conn.execute(
            "SELECT messages FROM conversations WHERE owner_id=%s", (user["id"],)
        ).fetchone()
        messages = (saved["messages"] if saved else [])[-12:]
        messages.append({"role": "user", "content": body.message})
        result = build_graph(conn).invoke(
            {
                "owner_id": str(user["id"]),
                "workflow_id": workflow_id,
                "timezone": body.timezone,
                "model": model,
                "wire": [{"role": "system", "content": system_prompt(body.timezone)}] + messages,
                "rounds": 0,
                "tokens": 0,
                "deadline": time.time() + 45,
                "trace": [],
                "proposal": None,
                "result": None,
                "alternatives": [],
            },
            config(workflow_id),
            durability="sync",
        )
        messages.append({"role": "assistant", "content": result["message"]})
        conn.execute(
            "INSERT INTO conversations(owner_id,messages,workflow_id) VALUES (%s,%s,%s) ON CONFLICT(owner_id) DO UPDATE SET messages=excluded.messages,workflow_id=excluded.workflow_id",
            (user["id"], Jsonb(messages[-14:]), workflow_id),
        )
        conn.execute(
            "INSERT INTO ai_runs(owner_id,model,tokens,elapsed_ms,outcome) VALUES (%s,%s,%s,%s,%s)",
            (
                user["id"],
                model,
                result["tokens"],
                int((time.monotonic() - started) * 1000),
                result["outcome"],
            ),
        )
    return {
        "message": result["message"],
        "proposal": result.get("proposal"),
        "usage": {"tokens": result["tokens"], "model": model},
        "outcome": result["outcome"],
    }


@router.post("/proposals/{proposal_id}/approve", response_model=Booking)
def approve(proposal_id: UUID, user=Depends(current_user)):
    with connect() as conn:
        lock_workspace(conn, user)
        proposal = conn.execute(
            "SELECT * FROM proposals WHERE id=%s AND owner_id=%s FOR UPDATE",
            (proposal_id, user["id"]),
        ).fetchone()
        if not proposal:
            raise HTTPException(404, "Proposal not found")
        if proposal["state"] == "approved":
            return proposal["result"]
        if (
            proposal["state"] != "pending"
            or proposal["expires_at"] <= datetime.now(UTC)
            or not proposal["workflow_id"]
        ):
            raise HTTPException(409, "This proposal expired or was replaced. Ask for a new one.")
        graph = build_graph(conn)
        checkpoint = graph.get_state(config(proposal["workflow_id"]))
        if checkpoint.values.get("owner_id") != str(user["id"]) or checkpoint.next != ("approval",):
            raise HTTPException(409, "No matching workflow is awaiting approval")
        result = graph.invoke(
            Command(resume={"approved": True, "proposal_id": str(proposal_id)}),
            config(proposal["workflow_id"]),
            durability="sync",
        )
        if result["outcome"] == "conflict":
            # Return after the transaction commits the conflict checkpoint.
            response = JSONResponse(
                {"detail": result["message"], "alternatives": result["alternatives"]},
                status_code=409,
            )
        else:
            response = result["result"]
    return response


@router.delete("/proposals/{proposal_id}")
def dismiss(proposal_id: UUID, user=Depends(current_user)):
    with connect() as conn:
        lock_workspace(conn, user)
        conn.execute(
            "UPDATE proposals SET state='dismissed' WHERE id=%s AND owner_id=%s AND state='pending'",
            (proposal_id, user["id"]),
        )
    return {"dismissed": True}


@router.delete("/history")
def reset_history(user=Depends(current_user)):
    with connect() as conn:
        lock_workspace(conn, user)
        conn.execute(
            "UPDATE proposals SET state='superseded' WHERE owner_id=%s AND state='pending'",
            (user["id"],),
        )
        conn.execute("DELETE FROM conversations WHERE owner_id=%s", (user["id"],))
    return {"reset": True}
