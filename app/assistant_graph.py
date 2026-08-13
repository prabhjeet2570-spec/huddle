"""Persistent booking workflow. Only the authenticated resume path can reserve."""

import json
import time
from datetime import UTC, datetime
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from psycopg.errors import ExclusionViolation
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from app.assistant_model import model_response
from app.availability import AvailabilityQuery, search_availability
from app.bookings import (
    Booking,
    BookingRequest,
    create_in_transaction,
    list_bookings,
    validate_request,
)
from app.rooms import get_room


class WorkflowState(TypedDict, total=False):
    owner_id: str
    workflow_id: str
    timezone: str
    model: str
    wire: list[dict[str, Any]]
    calls: list[dict[str, Any]]
    rounds: int
    tokens: int
    deadline: float
    message: str
    outcome: str
    proposal: dict[str, Any] | None
    result: dict[str, Any] | None
    alternatives: list[dict[str, Any]]
    trace: list[str]


def config(workflow_id):
    return {"configurable": {"thread_id": str(workflow_id)}, "recursion_limit": 16}


def build_graph(conn):
    def model(state):
        update = {"rounds": state["rounds"] + 1, "trace": state["trace"] + ["model"], "calls": []}
        if state["rounds"] >= 3 or time.time() > state["deadline"]:
            return {
                **update,
                "outcome": "budget",
                "message": "The assistant reached its tool budget. Please narrow your request.",
            }
        try:
            message, tokens = model_response(state["wire"], state["model"])
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list) or len(calls) > 4:
                raise ValueError("Invalid tool call batch")
            update.update(tokens=state["tokens"] + tokens)
            if not calls:
                return {
                    **update,
                    "outcome": "answered",
                    "message": message.get("content")
                    or "Please provide a time and attendee count.",
                }
            return {
                **update,
                "wire": state["wire"]
                + [{"role": "assistant", "content": message.get("content"), "tool_calls": calls}],
                "calls": calls,
            }
        except (httpx.HTTPError, KeyError, ValueError, TypeError, AttributeError):
            return {
                **update,
                "outcome": "provider_error",
                "message": "The assistant could not reach a valid model response. No booking was made. You can still book through Find a room.",
            }

    def tools(state):
        wire = list(state["wire"])
        trace = state["trace"] + ["tools"]
        for call in state["calls"]:
            try:
                args = json.loads(call["function"]["arguments"])
                name = call["function"]["name"]
                if name == "search_rooms":
                    query = AvailabilityQuery.model_validate(args)
                    result = search_availability(query).model_dump(mode="json")
                    zone = ZoneInfo(state["timezone"])
                    result.update(
                        local_start=query.starts_at.astimezone(zone).isoformat(),
                        local_end=query.ends_at.astimezone(zone).isoformat(),
                    )
                elif name == "list_bookings":
                    result = [
                        Booking.model_validate(b).model_dump(mode="json")
                        for b in list_bookings(user={"id": state["owner_id"]})
                    ]
                elif name == "propose_booking":
                    # A catalog display name differs only in case from its ID.
                    # Canonicalize known IDs before validation; never guess unknown rooms.
                    room_id = args.get("room_id") if isinstance(args, dict) else None
                    if isinstance(room_id, str) and room_id.strip().lower() in {
                        "cedar",
                        "maple",
                        "birch",
                    }:
                        args["room_id"] = room_id.strip().lower()
                    booking = BookingRequest.model_validate(args)
                    validate_request(booking)
                    available = search_availability(
                        AvailabilityQuery(
                            starts_at=booking.starts_at,
                            ends_at=booking.ends_at,
                            min_capacity=booking.attendees,
                        )
                    ).rooms
                    if booking.room_id not in {room.id for room in available}:
                        names = ", ".join(room.name for room in available)
                        message = f"{get_room(booking.room_id).name} is unavailable for that time. "
                        message += (
                            f"Available alternatives: {names}. Ask for one of these rooms to review a new proposal."
                            if names
                            else "No rooms matching your group size are available. Try another time."
                        )
                        # Finish deterministically rather than spending model calls
                        # on repeated attempts to propose the same occupied room.
                        return {
                            "outcome": "unavailable",
                            "message": message,
                            "alternatives": [room.model_dump() for room in available],
                            "trace": trace + ["unavailable"],
                        }
                    row = conn.execute(
                        "INSERT INTO proposals(id,owner_id,arguments,workflow_id) VALUES (%s,%s,%s,%s) RETURNING id,arguments,expires_at",
                        (
                            state["workflow_id"],
                            state["owner_id"],
                            Jsonb(booking.model_dump(mode="json")),
                            state["workflow_id"],
                        ),
                    ).fetchone()
                    proposal = {
                        **row,
                        "id": str(row["id"]),
                        "expires_at": row["expires_at"].isoformat(),
                    }
                    return {
                        "proposal": proposal,
                        "trace": trace + ["proposal"],
                        "outcome": "proposed",
                        "message": "Review the exact details below, then approve to reserve the room. Availability is checked again when you approve.",
                    }
                else:
                    result = {"error": "Unknown tool"}
            except (ValidationError, ValueError, HTTPException, KeyError, TypeError) as error:
                result = {
                    "error": error.detail
                    if isinstance(error, HTTPException)
                    else "Invalid tool arguments. Include valid room, attendee count, and timezone-aware times."
                }
            wire.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", "invalid"),
                    "content": json.dumps(result),
                }
            )
        return {"wire": wire, "trace": trace}

    def approval(state):
        # No side effects before interrupt: LangGraph re-enters this node on resume.
        decision = interrupt({"proposal": state["proposal"], "action": "approve_booking"})
        if decision != {"approved": True, "proposal_id": state["proposal"]["id"]}:
            raise HTTPException(409, "Approval does not match this proposal")
        return {"trace": state["trace"] + ["approval"], "outcome": "approved"}

    def reserve(state):
        proposal = conn.execute(
            "SELECT * FROM proposals WHERE id=%s AND owner_id=%s FOR UPDATE",
            (state["proposal"]["id"], state["owner_id"]),
        ).fetchone()
        if (
            not proposal
            or proposal["state"] != "pending"
            or proposal["expires_at"] <= datetime.now(UTC)
        ):
            raise HTTPException(409, "This proposal expired or was replaced. Ask for a new one.")
        request = BookingRequest.model_validate(proposal["arguments"])
        try:
            # Savepoint keeps the outer transaction usable after an exclusion violation.
            with conn.transaction():
                result = Booking.model_validate(
                    create_in_transaction(conn, request, state["owner_id"])
                ).model_dump(mode="json")
        except ExclusionViolation:
            alternatives = search_availability(
                AvailabilityQuery(
                    starts_at=request.starts_at,
                    ends_at=request.ends_at,
                    min_capacity=request.attendees,
                )
            ).model_dump(mode="json")["rooms"]
            conn.execute("UPDATE proposals SET state='conflict' WHERE id=%s", (proposal["id"],))
            return {
                "outcome": "conflict",
                "alternatives": alternatives,
                "message": "That room was taken while approval was pending. No booking was made. Choose an alternative and approve a new proposal.",
                "trace": state["trace"] + ["conflict"],
            }
        conn.execute(
            "UPDATE proposals SET state='approved',result=%s WHERE id=%s",
            (Jsonb(result), proposal["id"]),
        )
        return {
            "result": result,
            "outcome": "booked",
            "message": f"Your room is booked. {get_room(result['room_id']).name} is reserved for {result['attendees']} people. You can find the time and reservation details in My bookings.",
            "trace": state["trace"] + ["reserved"],
        }

    graph = StateGraph(WorkflowState)
    for name, node in [
        ("model", model),
        ("tools", tools),
        ("approval", approval),
        ("reserve", reserve),
    ]:
        graph.add_node(name, node)
    graph.add_conditional_edges(
        START,
        lambda s: (
            END
            if s.get("outcome") == "unavailable"
            else "approval"
            if s.get("proposal")
            else "model"
        ),
    )
    graph.add_conditional_edges("model", lambda s: "tools" if s.get("calls") else END)
    graph.add_conditional_edges(
        "tools",
        lambda s: (
            END
            if s.get("outcome") == "unavailable"
            else "approval"
            if s.get("proposal")
            else "model"
        ),
    )
    graph.add_edge("approval", "reserve")
    graph.add_edge("reserve", END)
    return graph.compile(checkpointer=PostgresSaver(conn))
