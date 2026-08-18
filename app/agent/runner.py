"""Conversation orchestration.

Owns everything around one turn that is not the state machine itself:
resolving a pending confirmation before the model gets a say, replaying
server-side history, opening the root trace span, recording the turn's
telemetry, and tagging the conversation's outcome.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.confirmation import ConfirmationVerdict, classify_confirmation
from app.agent.executor import ToolExecutor
from app.agent.graph import build_graph
from app.agent.llm import build_model
from app.agent.prompts import system_prompt
from app.agent.tools import HANDLERS, TOOLS, AgentContext, tool_definitions
from app.config import settings
from app.domain.enums import ConversationStatus
from app.domain.exceptions import ModelNotConfigured
from app.infrastructure.repositories.abuse_repository import AbuseRepository
from app.infrastructure.repositories.conversation_repository import (
    ConversationRepository,
)
from app.infrastructure.repositories.reservation_repository import (
    ReservationRepository,
)
from app.infrastructure.repositories.telemetry_repository import TelemetryRepository
from app.observability.tracing import current_trace_id, tag_outcome, turn_span
from app.reliability.budget import Budget
from app.services.abuse_detection import HoldCyclingDetector
from app.services.booking_saga import BookingSaga
from app.services.booking_service import BookingService
from app.services.calendar_sync import CalendarService
from app.services.notifications import NotificationService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TurnResult:
    conversation_id: UUID
    reply: str
    status: ConversationStatus
    outcome: str
    awaiting_confirmation: bool
    tool_calls: int
    llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    trace_id: str | None
    escalation_reason: str | None = None


class ConversationRunner:
    def __init__(
        self,
        session: AsyncSession,
        *,
        model=None,
        clock=None,
    ) -> None:
        self._session = session
        self._model_override = model
        self._reservations = ReservationRepository(session)
        self._conversations = ConversationRepository(session)
        self._telemetry = TelemetryRepository(session)
        self._abuse = AbuseRepository(session)
        self._clock = clock

        detector = HoldCyclingDetector(self._abuse, self._telemetry)
        self._booking = (
            BookingService(self._reservations, detector, clock)
            if clock is not None
            else BookingService(self._reservations, detector)
        )
        self._saga = BookingSaga(
            self._booking,
            NotificationService(session),
            CalendarService(session),
            self._telemetry,
        )

    async def run_turn(
        self,
        *,
        org_id: UUID,
        user_id: UUID,
        username: str,
        message: str,
        conversation_id: UUID | None = None,
    ) -> TurnResult:
        conversation = await self._conversations.get_or_create(
            conversation_id, org_id, user_id
        )
        started = time.perf_counter()
        turn_number = conversation.turns + 1

        context = AgentContext(
            org_id=org_id,
            user_id=user_id,
            username=username,
            conversation_id=conversation.id,
            booking=self._booking,
            saga=self._saga,
        )
        budget = Budget.from_settings(
            tool_calls_used=conversation.tool_calls_used,
            tokens_used=conversation.tokens_used,
        )
        executor = ToolExecutor(
            context, self._conversations, self._telemetry, budget, HANDLERS
        )

        with turn_span(conversation.id, org_id, user_id, turn_number) as span:
            trace_id = current_trace_id()

            # A pending high-risk action is resolved before the model runs.
            # Otherwise the model would be free to re-decide an action the
            # user was already asked about.
            resolved = await self._resolve_confirmation(
                conversation.id, message, executor
            )
            if resolved is not None:
                result = await self._finish(
                    conversation.id,
                    org_id,
                    message,
                    resolved.messages,
                    resolved.reply,
                    status=resolved.status,
                    awaiting=resolved.awaiting,
                    tool_calls=resolved.tool_calls,
                    llm_calls=0,
                    prompt_tokens=0,
                    completion_tokens=0,
                    started=started,
                    trace_id=trace_id,
                    escalation_reason=resolved.escalation_reason,
                )
                tag_outcome(span, result.outcome)
                return result

            history = await self._conversations.load_messages(conversation.id)
            rooms = await self._booking.list_rooms(org_id)

            try:
                model = build_model(tool_definitions(), self._model_override)
            except ModelNotConfigured as error:
                # A misconfiguration, not a failure. Say so plainly rather than
                # returning a 500 that looks like the application is broken.
                result = await self._finish(
                    conversation.id,
                    org_id,
                    message,
                    [AIMessage(content=str(error))],
                    str(error),
                    status=ConversationStatus.BLOCKED,
                    awaiting=False,
                    tool_calls=0,
                    llm_calls=0,
                    prompt_tokens=0,
                    completion_tokens=0,
                    started=started,
                    trace_id=trace_id,
                    escalation_reason=str(error),
                )
                tag_outcome(span, result.outcome)
                return result

            graph = build_graph(model, executor)

            state = await graph.ainvoke(
                {
                    "messages": [*history, HumanMessage(content=message)],
                    "system_prompt": system_prompt(rooms, self._booking.now),
                    "budget": budget,
                    "llm_calls": 0,
                    "tool_calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                },
                {"recursion_limit": settings.max_llm_calls_per_turn * 2 + 4},
            )

            produced = state["messages"][len(history) :]
            status = (
                ConversationStatus(state["outcome"])
                if state.get("outcome")
                else ConversationStatus.COMPLETED
            )
            result = await self._finish(
                conversation.id,
                org_id,
                None,
                produced,
                _last_text(state["messages"]),
                status=status,
                awaiting=bool(state.get("awaiting_confirmation")),
                tool_calls=int(state.get("tool_calls", 0)),
                llm_calls=int(state.get("llm_calls", 0)),
                prompt_tokens=int(state.get("prompt_tokens", 0)),
                completion_tokens=int(state.get("completion_tokens", 0)),
                started=started,
                trace_id=trace_id,
                escalation_reason=state.get("escalation_reason"),
            )
            tag_outcome(span, result.outcome)
            return result

    # --- Confirmation ------------------------------------------------------

    @dataclass(slots=True)
    class _Resolved:
        messages: list[BaseMessage]
        reply: str
        status: ConversationStatus
        awaiting: bool
        tool_calls: int
        escalation_reason: str | None = None

    async def _resolve_confirmation(
        self,
        conversation_id: UUID,
        message: str,
        executor: ToolExecutor,
    ) -> _Resolved | None:
        pending = await self._conversations.get_pending_confirmation(
            conversation_id, self._booking.now
        )
        if pending is None:
            return None

        verdict = classify_confirmation(message)

        if verdict is ConfirmationVerdict.DECLINE:
            await self._conversations.resolve_confirmation(pending.id, "declined")
            reply = (
                f"Understood, I have not gone ahead with {pending.summary}. "
                "Tell me what you would like to change."
            )
            return self._Resolved(
                messages=[HumanMessage(content=message), AIMessage(content=reply)],
                reply=reply,
                status=ConversationStatus.COMPLETED,
                awaiting=False,
                tool_calls=0,
            )

        if verdict is ConfirmationVerdict.AMBIGUOUS:
            reply = (
                f"Before I go ahead with {pending.summary}, I need a clear yes "
                "or no. Reply 'yes' to proceed or 'no' to stop."
            )
            return self._Resolved(
                messages=[HumanMessage(content=message), AIMessage(content=reply)],
                reply=reply,
                status=ConversationStatus.ACTIVE,
                awaiting=True,
                tool_calls=0,
            )

        # Affirmed. Replay the stored arguments verbatim.
        await self._conversations.resolve_confirmation(pending.id, "confirmed")
        result = await executor.execute(
            pending.tool_name, pending.arguments, preconfirmed=True
        )
        call_id = f"confirmed-{pending.id.hex[:8]}"
        messages: list[BaseMessage] = [
            HumanMessage(content=message),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": call_id,
                        "name": pending.tool_name,
                        "args": pending.arguments,
                    }
                ],
            ),
            ToolMessage(
                content=result.content,
                tool_call_id=call_id,
                name=pending.tool_name,
            ),
        ]

        if result.escalated:
            reply = (
                "I could not complete that reliably, so I stopped and flagged "
                "this conversation for a person to review."
            )
            status = ConversationStatus.ESCALATED
        elif result.status.value == "ok":
            reply = _humanize(pending.tool_name, result.content)
            status = ConversationStatus.COMPLETED
        else:
            reply = result.content.replace("Status: error\nMessage: ", "")
            status = ConversationStatus.COMPLETED

        messages.append(AIMessage(content=reply))
        return self._Resolved(
            messages=messages,
            reply=reply,
            status=status,
            awaiting=False,
            tool_calls=1,
            escalation_reason=result.content if result.escalated else None,
        )

    # --- Persistence -------------------------------------------------------

    async def _finish(
        self,
        conversation_id: UUID,
        org_id: UUID,
        user_message: str | None,
        messages: list[BaseMessage],
        reply: str,
        *,
        status: ConversationStatus,
        awaiting: bool,
        tool_calls: int,
        llm_calls: int,
        prompt_tokens: int,
        completion_tokens: int,
        started: float,
        trace_id: str | None,
        escalation_reason: str | None,
    ) -> TurnResult:
        to_store = list(messages)
        if user_message is not None:
            to_store = [HumanMessage(content=user_message), *to_store]

        await self._conversations.append_messages(conversation_id, to_store)
        await self._conversations.record_usage(
            conversation_id, tool_calls, prompt_tokens + completion_tokens
        )

        latency_ms = (time.perf_counter() - started) * 1000
        seq = await self._telemetry.next_turn_seq(conversation_id)
        await self._telemetry.record_turn(
            conversation_id=conversation_id,
            seq=seq,
            latency_ms=latency_ms,
            llm_calls=llm_calls,
            tool_calls=tool_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            outcome=status,
            trace_id=trace_id,
        )

        # A conversation waiting on a confirmation is not finished; leaving it
        # ACTIVE keeps it out of the completion-rate numerator.
        effective = ConversationStatus.ACTIVE if awaiting else status
        await self._conversations.set_status(
            conversation_id, effective, escalation_reason
        )

        return TurnResult(
            conversation_id=conversation_id,
            reply=reply,
            status=effective,
            outcome=effective,
            awaiting_confirmation=awaiting,
            tool_calls=tool_calls,
            llm_calls=llm_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            trace_id=trace_id,
            escalation_reason=escalation_reason,
        )


def _last_text(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            content = message.content
            if isinstance(content, str) and content.strip():
                return content
            if isinstance(content, list):
                parts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict)
                ]
                joined = "".join(parts).strip()
                if joined:
                    return joined
    return "I do not have a reply for that. Could you rephrase?"


def _humanize(tool_name: str, content: str) -> str:
    """Turn a confirmed tool result into a sentence, without another LLM call."""
    fields = dict(
        line.split(": ", 1)
        for line in content.splitlines()
        if ": " in line and not line.startswith("Status")
    )
    reference = fields.get("Reference", "")
    room = fields.get("Room", "")
    when = fields.get("When", "")

    if tool_name == "confirm_booking":
        return (
            f"Booked. Room {room}, {when}. Your reference is {reference}. "
            "Attendees have been notified and it is on the calendar."
        )
    if tool_name == "cancel_booking":
        return f"Cancelled booking {reference} in room {room} ({when})."
    return content


TOOL_NAMES = tuple(TOOLS)
