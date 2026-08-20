"""The agent loop, driven end to end with a scripted model.

Using a scripted model rather than a live one is what makes these assertions
possible: the tool calls are exactly the ones the test chose, including the
malformed and looping ones a real model produces only occasionally.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import func, select

from app.agent.llm import FakeChatModel
from app.agent.runner import ConversationRunner
from app.domain.enums import ConversationStatus, ReservationState
from app.infrastructure.models import (
    AlertModel,
    ConversationModel,
    MessageModel,
    ReservationModel,
    TurnModel,
)
from tests.conftest import at, requires_postgres

pytestmark = [requires_postgres, pytest.mark.postgres]


def _call(name: str, args: dict, call_id: str = "c1") -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"id": call_id, "name": name, "args": args}]
    )


def _hold_args(hour: int = 10, room: str = "A") -> dict:
    return {
        "room": room,
        "starts_at": at(hour, 0).isoformat(),
        "ends_at": at(hour + 1, 0).isoformat(),
        "title": "Design review",
        "attendees": 3,
    }


async def _run(session, tenant, clock, script, message, conversation_id=None):
    runner = ConversationRunner(session, model=FakeChatModel(script), clock=clock)
    return await runner.run_turn(
        org_id=tenant.org_id,
        user_id=tenant.user_id,
        username="alice",
        message=message,
        conversation_id=conversation_id,
    )


# --- The loop --------------------------------------------------------------


async def test_a_read_only_turn_completes(session, tenant, clock):
    result = await _run(
        session,
        tenant,
        clock,
        [_call("list_rooms", {}), AIMessage(content="There are five rooms.")],
        "what rooms are there?",
    )

    assert result.status is ConversationStatus.COMPLETED
    assert result.reply == "There are five rooms."
    assert result.tool_calls == 1
    assert result.llm_calls == 2


async def test_the_reasoning_tool_observation_loop_runs_more_than_once(
    session, tenant, clock
):
    result = await _run(
        session,
        tenant,
        clock,
        [
            _call(
                "list_available_rooms",
                {
                    "starts_at": at(10, 0).isoformat(),
                    "ends_at": at(11, 0).isoformat(),
                    "attendees": 3,
                },
            ),
            _call("place_hold", _hold_args(), call_id="c2"),
            AIMessage(content="Held room A. Shall I confirm?"),
        ],
        "book a room for 3 at 10",
    )

    assert result.tool_calls == 2
    assert result.llm_calls == 3
    reservation = await session.scalar(select(ReservationModel))
    assert reservation.state == ReservationState.HELD


# --- Server-side history ---------------------------------------------------


async def test_history_is_persisted_and_replayed_including_tool_traffic(
    session, tenant, clock
):
    first = await _run(
        session,
        tenant,
        clock,
        [_call("list_rooms", {}), AIMessage(content="Five rooms.")],
        "what rooms are there?",
    )

    model = FakeChatModel([AIMessage(content="As I said, five.")])
    runner = ConversationRunner(session, model=model, clock=clock)
    await runner.run_turn(
        org_id=tenant.org_id,
        user_id=tenant.user_id,
        username="alice",
        message="how many again?",
        conversation_id=first.conversation_id,
    )

    # The second turn's prompt contains the first turn's tool call and result.
    # A client-supplied transcript would have dropped both.
    replayed = model.calls[0]
    roles = [type(message).__name__ for message in replayed]
    assert "ToolMessage" in roles
    assert sum(1 for name in roles if name == "HumanMessage") == 2

    stored = await session.scalar(
        select(func.count())
        .select_from(MessageModel)
        .where(MessageModel.conversation_id == first.conversation_id)
    )
    assert stored == 6  # user, ai+toolcall, tool, ai, user, ai


async def test_a_conversation_id_from_another_user_is_not_honoured(
    session, tenant, other_tenant, clock
):
    theirs = await _run(
        session, other_tenant, clock, [AIMessage(content="hello")], "hi"
    )

    ours = await _run(
        session,
        tenant,
        clock,
        [AIMessage(content="hello")],
        "hi",
        conversation_id=theirs.conversation_id,
    )

    # A fresh conversation is started rather than the other tenant's resumed.
    assert ours.conversation_id != theirs.conversation_id


# --- Confirmation flow -----------------------------------------------------


async def test_confirmation_requires_a_second_user_message(session, tenant, clock):
    held = await _run(
        session,
        tenant,
        clock,
        [_call("place_hold", _hold_args()), AIMessage(content="Confirm room A at 10?")],
        "book room A at 10 for 3",
    )
    reference = await session.scalar(select(ReservationModel.reference))

    # The model tries to confirm on its own. The gate stops it.
    attempted = await _run(
        session,
        tenant,
        clock,
        [
            _call("confirm_booking", {"reference": reference}),
            AIMessage(content="I need you to confirm."),
        ],
        "sounds fine I guess",
        conversation_id=held.conversation_id,
    )
    assert attempted.awaiting_confirmation is True
    state = await session.scalar(select(ReservationModel.state))
    assert state == ReservationState.HELD

    # The user says yes. No model call is needed to act on it.
    confirmed = await _run(
        session,
        tenant,
        clock,
        [],
        "yes",
        conversation_id=held.conversation_id,
    )
    assert confirmed.llm_calls == 0
    assert "Booked" in confirmed.reply
    state = await session.scalar(select(ReservationModel.state))
    assert state == ReservationState.CONFIRMED


async def test_declining_a_confirmation_leaves_the_hold_alone(session, tenant, clock):
    held = await _run(
        session,
        tenant,
        clock,
        [_call("place_hold", _hold_args()), AIMessage(content="Confirm?")],
        "book room A at 10 for 3",
    )
    reference = await session.scalar(select(ReservationModel.reference))

    await _run(
        session,
        tenant,
        clock,
        [_call("confirm_booking", {"reference": reference}), AIMessage(content="ok")],
        "book it",
        conversation_id=held.conversation_id,
    )
    declined = await _run(
        session, tenant, clock, [], "no", conversation_id=held.conversation_id
    )

    assert "not gone ahead" in declined.reply
    state = await session.scalar(select(ReservationModel.state))
    assert state == ReservationState.HELD


async def test_an_ambiguous_reply_asks_again_and_changes_nothing(
    session, tenant, clock
):
    held = await _run(
        session,
        tenant,
        clock,
        [_call("place_hold", _hold_args()), AIMessage(content="Confirm?")],
        "book room A at 10 for 3",
    )
    reference = await session.scalar(select(ReservationModel.reference))
    await _run(
        session,
        tenant,
        clock,
        [_call("confirm_booking", {"reference": reference}), AIMessage(content="ok")],
        "book it",
        conversation_id=held.conversation_id,
    )

    unclear = await _run(
        session,
        tenant,
        clock,
        [],
        "yes but change the room to B and start an hour later",
        conversation_id=held.conversation_id,
    )

    assert unclear.awaiting_confirmation is True
    assert "clear yes or no" in unclear.reply
    state = await session.scalar(select(ReservationModel.state))
    assert state == ReservationState.HELD


# --- Non-convergence -------------------------------------------------------


async def test_a_looping_model_is_halted_by_the_budget(
    session, tenant, clock, monkeypatch
):
    """The model that never converges is bounded, not left running."""
    from app.config import settings

    monkeypatch.setattr(settings, "max_tool_calls_per_conversation", 4)

    script = [_call("list_rooms", {}, call_id=f"c{i}") for i in range(12)]
    result = await _run(session, tenant, clock, script, "go in circles")

    assert result.status is ConversationStatus.ESCALATED
    assert result.tool_calls <= 6
    alert = await session.scalar(
        select(AlertModel).where(AlertModel.conversation_id == result.conversation_id)
    )
    assert alert is not None


async def test_a_conversation_already_over_budget_never_calls_the_model(
    session, tenant, clock, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "max_tool_calls_per_conversation", 2)

    first = await _run(
        session,
        tenant,
        clock,
        [
            _call("list_rooms", {}),
            _call("list_rooms", {}, "c2"),
            AIMessage(content="hi"),
        ],
        "list rooms twice",
    )

    model = FakeChatModel([AIMessage(content="should never be reached")])
    runner = ConversationRunner(session, model=model, clock=clock)
    result = await runner.run_turn(
        org_id=tenant.org_id,
        user_id=tenant.user_id,
        username="alice",
        message="and again",
        conversation_id=first.conversation_id,
    )

    assert result.status is ConversationStatus.ESCALATED
    assert model.calls == []  # the guard node ended the turn before the LLM
    assert "safety budget" in result.reply


# --- Telemetry -------------------------------------------------------------


async def test_every_turn_is_recorded_with_its_latency_and_outcome(
    session, tenant, clock
):
    result = await _run(
        session,
        tenant,
        clock,
        [_call("list_rooms", {}), AIMessage(content="Five rooms.")],
        "what rooms are there?",
    )

    turn = await session.scalar(
        select(TurnModel).where(TurnModel.conversation_id == result.conversation_id)
    )
    assert turn.seq == 1
    assert turn.latency_ms > 0
    assert turn.tool_calls == 1
    assert turn.outcome == ConversationStatus.COMPLETED

    conversation = await session.get(ConversationModel, result.conversation_id)
    assert conversation.turns == 1
    assert conversation.outcome == ConversationStatus.COMPLETED


# --- Provider failure ------------------------------------------------------


class _FailingModel(FakeChatModel):
    """A model whose provider is down."""

    def __init__(self, error: Exception) -> None:
        super().__init__([])
        object.__setattr__(self, "_error", error)
        object.__setattr__(self, "attempts", 0)

    async def ainvoke(self, *args, **kwargs):
        object.__setattr__(self, "attempts", self.attempts + 1)
        raise self._error


class _ProviderDown(Exception):
    status_code = 503


class _BadApiKey(Exception):
    status_code = 401


async def test_a_provider_outage_escalates_instead_of_raising(session, tenant, clock):
    """A 500 from the model must not become a 500 from our API."""
    model = _FailingModel(_ProviderDown("service unavailable"))
    runner = ConversationRunner(session, model=model, clock=clock)

    result = await runner.run_turn(
        org_id=tenant.org_id,
        user_id=tenant.user_id,
        username="alice",
        message="book me a room",
    )

    assert result.status is ConversationStatus.ESCALATED
    assert "cannot reach the assistant service" in result.reply
    assert "ProviderDown" in (result.escalation_reason or "")
    # Retryable, so the policy tried more than once before giving up.
    assert model.attempts > 1


async def test_a_bad_api_key_is_not_retried(session, tenant, clock):
    """A 401 is terminal; hammering it three times helps nobody."""
    model = _FailingModel(_BadApiKey("invalid api key"))
    runner = ConversationRunner(session, model=model, clock=clock)

    result = await runner.run_turn(
        org_id=tenant.org_id,
        user_id=tenant.user_id,
        username="alice",
        message="book me a room",
    )

    assert result.status is ConversationStatus.ESCALATED
    assert model.attempts == 1


async def test_a_provider_outage_leaves_the_turn_recorded(session, tenant, clock):
    """The failure is still telemetry, not a hole in the record."""
    model = _FailingModel(_ProviderDown("boom"))
    runner = ConversationRunner(session, model=model, clock=clock)
    result = await runner.run_turn(
        org_id=tenant.org_id,
        user_id=tenant.user_id,
        username="alice",
        message="book me a room",
    )

    turn = await session.scalar(
        select(TurnModel).where(TurnModel.conversation_id == result.conversation_id)
    )
    assert turn is not None
    assert turn.outcome == ConversationStatus.ESCALATED


async def test_a_missing_api_key_reports_configuration_not_failure(
    session, tenant, clock, monkeypatch
):
    """What someone sees on a fresh clone with no key: a clear instruction."""
    from pydantic import SecretStr

    from app.config import settings

    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_api_key", SecretStr(""))

    runner = ConversationRunner(session, clock=clock)  # no model override
    result = await runner.run_turn(
        org_id=tenant.org_id,
        user_id=tenant.user_id,
        username="alice",
        message="book me a room",
    )

    assert result.status is ConversationStatus.BLOCKED
    assert "HUDDLE_LLM_API_KEY" in result.reply
    # Blocked, not escalated: nothing failed, it was never configured.
    assert result.llm_calls == 0
    assert result.tool_calls == 0
