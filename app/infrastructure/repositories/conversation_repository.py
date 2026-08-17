"""Conversation state, held server-side.

The client sends a conversation id and nothing else; the server replays what it
recorded, tool traffic included. Having the client post the transcript back on
every turn would be cheaper, but it makes tool calls invisible to the model on
later turns and lets a caller forge what the assistant supposedly said.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.enums import ConversationStatus
from app.infrastructure.models import (
    ConversationModel,
    MessageModel,
    PendingConfirmationModel,
)


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, org_id: UUID, user_id: UUID) -> ConversationModel:
        model = ConversationModel(
            id=uuid4(),
            org_id=org_id,
            user_id=user_id,
            status=ConversationStatus.ACTIVE,
        )
        self._session.add(model)
        await self._session.commit()
        return model

    async def get(
        self,
        conversation_id: UUID,
        org_id: UUID,
        user_id: UUID,
    ) -> ConversationModel | None:
        """Scoped by org *and* user: a conversation id is not a capability."""
        return await self._session.scalar(
            select(ConversationModel).where(
                ConversationModel.id == conversation_id,
                ConversationModel.org_id == org_id,
                ConversationModel.user_id == user_id,
            )
        )

    async def get_or_create(
        self,
        conversation_id: UUID | None,
        org_id: UUID,
        user_id: UUID,
    ) -> ConversationModel:
        if conversation_id is not None:
            existing = await self.get(conversation_id, org_id, user_id)
            if existing is not None:
                return existing
        return await self.create(org_id, user_id)

    # --- History -----------------------------------------------------------

    async def load_messages(self, conversation_id: UUID) -> list[BaseMessage]:
        """Replay the transcript as LangChain messages, tool traffic included."""
        rows = await self._session.scalars(
            select(MessageModel)
            .where(MessageModel.conversation_id == conversation_id)
            .order_by(MessageModel.seq.asc())
        )
        messages: list[BaseMessage] = []
        for row in rows:
            if row.role == "user":
                messages.append(HumanMessage(content=row.content))
            elif row.role == "assistant":
                messages.append(
                    AIMessage(content=row.content, tool_calls=row.tool_calls or [])
                )
            elif row.role == "tool":
                messages.append(
                    ToolMessage(
                        content=row.content,
                        tool_call_id=row.tool_call_id or "",
                        name=row.name,
                    )
                )
        return messages

    async def next_seq(self, conversation_id: UUID) -> int:
        value = await self._session.scalar(
            select(func.coalesce(func.max(MessageModel.seq), 0)).where(
                MessageModel.conversation_id == conversation_id
            )
        )
        return int(value or 0) + 1

    async def append_messages(
        self,
        conversation_id: UUID,
        messages: list[BaseMessage],
    ) -> None:
        seq = await self.next_seq(conversation_id)
        for message in messages:
            row = self._to_row(conversation_id, seq, message)
            if row is not None:
                self._session.add(row)
                seq += 1
        await self._session.commit()

    @staticmethod
    def _to_row(
        conversation_id: UUID,
        seq: int,
        message: BaseMessage,
    ) -> MessageModel | None:
        content = message.content
        if not isinstance(content, str):
            content = json.dumps(content, default=str)

        if isinstance(message, HumanMessage):
            return MessageModel(
                conversation_id=conversation_id, seq=seq, role="user", content=content
            )
        if isinstance(message, ToolMessage):
            return MessageModel(
                conversation_id=conversation_id,
                seq=seq,
                role="tool",
                content=content,
                tool_call_id=message.tool_call_id,
                name=message.name,
            )
        if isinstance(message, AIMessage):
            return MessageModel(
                conversation_id=conversation_id,
                seq=seq,
                role="assistant",
                content=content,
                tool_calls=[
                    {
                        "id": call.get("id"),
                        "name": call.get("name"),
                        "args": call.get("args", {}),
                    }
                    for call in (message.tool_calls or [])
                ]
                or None,
            )
        return None

    # --- Budget and status -------------------------------------------------

    async def record_usage(
        self,
        conversation_id: UUID,
        tool_calls: int,
        tokens: int,
        turns: int = 1,
    ) -> None:
        await self._session.execute(
            update(ConversationModel)
            .where(ConversationModel.id == conversation_id)
            .values(
                tool_calls_used=ConversationModel.tool_calls_used + tool_calls,
                tokens_used=ConversationModel.tokens_used + tokens,
                turns=ConversationModel.turns + turns,
            )
        )
        await self._session.commit()

    async def set_status(
        self,
        conversation_id: UUID,
        status: ConversationStatus,
        reason: str | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status}
        if status in ConversationStatus.terminal():
            values["outcome"] = status
        if reason is not None:
            values["escalation_reason"] = reason
        await self._session.execute(
            update(ConversationModel)
            .where(ConversationModel.id == conversation_id)
            .values(**values)
        )
        await self._session.commit()

    # --- Confirmation gate -------------------------------------------------

    async def create_pending_confirmation(
        self,
        conversation_id: UUID,
        tool_name: str,
        arguments: dict[str, Any],
        summary: str,
        now: datetime,
    ) -> PendingConfirmationModel:
        """Park a high blast-radius call and supersede any earlier one."""
        await self._session.execute(
            update(PendingConfirmationModel)
            .where(
                PendingConfirmationModel.conversation_id == conversation_id,
                PendingConfirmationModel.status == "pending",
            )
            .values(status="superseded")
        )
        model = PendingConfirmationModel(
            id=uuid4(),
            conversation_id=conversation_id,
            tool_name=tool_name,
            arguments=json.loads(json.dumps(arguments, default=str)),
            summary=summary,
            status="pending",
            expires_at=now + timedelta(seconds=settings.confirmation_ttl_seconds),
        )
        self._session.add(model)
        await self._session.commit()
        return model

    async def get_pending_confirmation(
        self,
        conversation_id: UUID,
        now: datetime,
    ) -> PendingConfirmationModel | None:
        return await self._session.scalar(
            select(PendingConfirmationModel)
            .where(
                PendingConfirmationModel.conversation_id == conversation_id,
                PendingConfirmationModel.status == "pending",
                PendingConfirmationModel.expires_at > now,
            )
            .order_by(PendingConfirmationModel.created_at.desc())
            .limit(1)
        )

    async def resolve_confirmation(
        self,
        confirmation_id: UUID,
        status: str,
    ) -> None:
        await self._session.execute(
            update(PendingConfirmationModel)
            .where(PendingConfirmationModel.id == confirmation_id)
            .values(status=status)
        )
        await self._session.commit()
