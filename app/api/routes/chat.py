from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.agent.runner import ConversationRunner
from app.api.deps import CurrentUser, DbSession

router = APIRouter(tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    #: Omit to start a new conversation. History lives on the server, so the
    #: client never replays the transcript and cannot forge it.
    conversation_id: UUID | None = None


class ChatResponse(BaseModel):
    conversation_id: UUID
    response: str
    status: str
    awaiting_confirmation: bool
    tool_calls: int
    latency_ms: float
    trace_id: str | None = None


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest, user: CurrentUser, session: DbSession
) -> ChatResponse:
    runner = ConversationRunner(session)
    result = await runner.run_turn(
        org_id=user.org_id,
        user_id=user.id,
        username=user.username,
        message=request.message,
        conversation_id=request.conversation_id,
    )
    return ChatResponse(
        conversation_id=result.conversation_id,
        response=result.reply,
        status=result.status,
        awaiting_confirmation=result.awaiting_confirmation,
        tool_calls=result.tool_calls,
        latency_ms=round(result.latency_ms, 1),
        trace_id=result.trace_id,
    )
