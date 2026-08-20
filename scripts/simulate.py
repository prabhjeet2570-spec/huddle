#!/usr/bin/env python
"""Drive realistic traffic through the agent so the metrics have real data.

Runs a mix of sessions against a scripted model: successful bookings,
conflicts, malformed tool calls, injected step failures that force
compensation, and hold cyclers. Nothing here is a mock of the agent - the real
graph, executor, guardrails and saga run. Only the language model is scripted,
so the run is deterministic and free.

    python -m scripts.simulate --sessions 40
"""

from __future__ import annotations

import argparse
import asyncio
import random
from datetime import timedelta
from uuid import uuid4

from langchain_core.messages import AIMessage
from sqlalchemy import select, text

from app.agent.llm import FakeChatModel
from app.agent.runner import ConversationRunner
from app.api.auth import hash_password
from app.config import settings
from app.infrastructure.database import dispose_engine, session_scope
from app.infrastructure.models import (
    OrganizationModel,
    ReservationModel,
    RoomModel,
    UserModel,
)
from app.reliability.failure_injection import Scenario, injector
from app.services.booking_service import office_now

ROOMS = {"A": 4, "B": 6, "C": 8, "D": 12, "E": 20}


def _call(name: str, args: dict, call_id: str | None = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id or uuid4().hex[:8], "name": name, "args": args}],
        usage_metadata={"input_tokens": 420, "output_tokens": 60, "total_tokens": 480},
    )


def _say(text_: str) -> AIMessage:
    return AIMessage(
        content=text_,
        usage_metadata={"input_tokens": 480, "output_tokens": 45, "total_tokens": 525},
    )


async def _seed():
    slug = f"sim-{uuid4().hex[:6]}"
    async with session_scope() as session:
        org = OrganizationModel(id=uuid4(), slug=slug, name="Simulation")
        session.add(org)
        await session.flush()
        users = []
        for index in range(6):
            user = UserModel(
                id=uuid4(),
                org_id=org.id,
                username=f"user{index}",
                password_hash=hash_password("x"),
            )
            session.add(user)
            users.append(user.id)
        for name, capacity in ROOMS.items():
            session.add(
                RoomModel(id=uuid4(), org_id=org.id, name=name, capacity=capacity)
            )
        await session.commit()
        return org.id, users


def _slot(base, offset_hours: int) -> tuple[str, str]:
    start = base + timedelta(hours=offset_hours)
    return start.isoformat(), (start + timedelta(hours=1)).isoformat()


async def _turn(org_id, user_id, message, script, conversation_id=None):
    async with session_scope() as session:
        runner = ConversationRunner(session, model=FakeChatModel(script))
        return await runner.run_turn(
            org_id=org_id,
            user_id=user_id,
            username="simulated",
            message=message,
            conversation_id=conversation_id,
        )


async def _happy_booking(org_id, user_id, base, room, offset) -> None:
    starts, ends = _slot(base, offset)
    first = await _turn(
        org_id,
        user_id,
        "book me a room tomorrow morning",
        [
            _call(
                "list_available_rooms",
                {"starts_at": starts, "ends_at": ends, "attendees": 3},
            ),
            _call(
                "place_hold",
                {
                    "room": room,
                    "starts_at": starts,
                    "ends_at": ends,
                    "title": "Team sync",
                    "attendees": 3,
                },
            ),
            _say(f"I have held room {room}. Shall I confirm?"),
        ],
    )
    reference = await _latest_reference(org_id, user_id)
    if reference is None:
        return
    await _turn(
        org_id,
        user_id,
        "please book it",
        [_call("confirm_booking", {"reference": reference}), _say("Confirm?")],
        first.conversation_id,
    )
    await _turn(org_id, user_id, "yes", [], first.conversation_id)


async def _conflicting_booking(org_id, user_id, base, room, offset) -> None:
    starts, ends = _slot(base, offset)
    await _turn(
        org_id,
        user_id,
        "book that same slot",
        [
            _call(
                "place_hold",
                {
                    "room": room,
                    "starts_at": starts,
                    "ends_at": ends,
                    "title": "Clash",
                    "attendees": 2,
                },
            ),
            _say("That room is taken. Try another?"),
        ],
    )


async def _malformed_call(org_id, user_id, base, offset) -> None:
    """A model that gets its own tool schema wrong, then recovers."""
    starts, ends = _slot(base, offset)
    await _turn(
        org_id,
        user_id,
        "book something at quarter past",
        [
            _call(
                "place_hold",
                {
                    "room": "A",
                    "starts_at": starts.replace(":00:00", ":15:00"),
                    "ends_at": ends,
                    "title": "Misaligned",
                    "attendees": 0,
                },
            ),
            _call(
                "place_hold",
                {
                    "room": "B",
                    "starts_at": starts,
                    "ends_at": ends,
                    "title": "Corrected",
                    "attendees": 2,
                },
            ),
            _say("Fixed the time and held room B."),
        ],
    )


async def _hallucinated_tool(org_id, user_id) -> None:
    await _turn(
        org_id,
        user_id,
        "delete everything",
        [_call("purge_all_bookings", {"confirm": True}), _say("I cannot do that.")],
    )


async def _compensated_booking(org_id, user_id, base, room, offset) -> None:
    """A booking whose calendar sync fails, forcing a full rollback."""
    starts, ends = _slot(base, offset)
    first = await _turn(
        org_id,
        user_id,
        "book the big room",
        [
            _call(
                "place_hold",
                {
                    "room": room,
                    "starts_at": starts,
                    "ends_at": ends,
                    "title": "Doomed",
                    "attendees": 4,
                },
            ),
            _say("Held. Confirm?"),
        ],
    )
    reference = await _latest_reference(org_id, user_id)
    if reference is None:
        return
    await _turn(
        org_id,
        user_id,
        "confirm it",
        [_call("confirm_booking", {"reference": reference}), _say("Confirm?")],
        first.conversation_id,
    )
    injector.arm(Scenario.CALENDAR_FAILURE, "calendar.create")
    try:
        await _turn(org_id, user_id, "yes", [], first.conversation_id)
    finally:
        injector.disarm("calendar.create")


async def _hold_cycler(org_id, user_id, base, room) -> None:
    """The exploit: hold, never confirm, repeat."""
    for offset in range(1, 9):
        starts, ends = _slot(base, offset)
        await _turn(
            org_id,
            user_id,
            "hold that room again",
            [
                _call(
                    "place_hold",
                    {
                        "room": room,
                        "starts_at": starts,
                        "ends_at": ends,
                        "title": "Squatting",
                        "attendees": 2,
                    },
                ),
                _say("Held."),
            ],
        )


async def _latest_reference(org_id, user_id) -> str | None:
    async with session_scope() as session:
        return await session.scalar(
            select(ReservationModel.reference)
            .where(
                ReservationModel.org_id == org_id,
                ReservationModel.user_id == user_id,
                ReservationModel.state == "held",
            )
            .order_by(ReservationModel.created_at.desc())
            .limit(1)
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=40)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--keep", action="store_true", help="keep simulated rows")
    arguments = parser.parse_args()

    random.seed(arguments.seed)
    settings.failure_injection_enabled = True

    org_id, users = await _seed()

    base = (office_now() + timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    while base.weekday() >= 5:
        base += timedelta(days=1)

    offset = 0
    print(f"simulating {arguments.sessions} sessions...", flush=True)

    for index in range(arguments.sessions):
        user = users[index % len(users)]
        room = random.choice(list(ROOMS))
        kind = random.choices(
            ["happy", "conflict", "malformed", "hallucination", "compensated"],
            weights=[52, 14, 14, 8, 12],
        )[0]
        offset = (offset + 1) % 9

        if kind == "happy":
            await _happy_booking(org_id, user, base, room, offset)
        elif kind == "conflict":
            await _conflicting_booking(org_id, user, base, room, offset)
        elif kind == "malformed":
            await _malformed_call(org_id, user, base, offset)
        elif kind == "hallucination":
            await _hallucinated_tool(org_id, user)
        else:
            await _compensated_booking(org_id, user, base, room, offset)

        if (index + 1) % 10 == 0:
            print(f"  {index + 1}/{arguments.sessions}", flush=True)

    # One deliberate abuser, so hold-cycling detection has something to catch.
    await _hold_cycler(org_id, users[-1], base, "E")
    print("  + 1 hold-cycling session", flush=True)

    if not arguments.keep:
        async with session_scope() as session:
            await session.execute(
                text("DELETE FROM organizations WHERE id = :id"), {"id": org_id}
            )
            await session.commit()
        print("simulated rows removed (pass --keep to retain them)")

    await dispose_engine()
    print("done. now run: python -m scripts.report_metrics")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
