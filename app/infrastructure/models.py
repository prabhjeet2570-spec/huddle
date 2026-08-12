"""SQLAlchemy models.

The load-bearing declaration in this file is the exclusion constraint on
``reservations``. It is the only thing standing between two concurrent agents
and a double booking, and it is enforced by PostgreSQL rather than by any
check the application performs.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSTZRANGE, ExcludeConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.enums import ReservationState


class Base(DeclarativeBase):
    """Base class for all ORM models."""


def _pk() -> Mapped[UUID]:
    return mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --- Tenancy ---------------------------------------------------------------


class OrganizationModel(Base):
    __tablename__ = "organizations"

    id: Mapped[UUID] = _pk()
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class UserModel(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = _pk()
    org_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="member")
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (UniqueConstraint("org_id", "username", name="uq_users_org_name"),)


class RoomModel(Base):
    __tablename__ = "rooms"

    id: Mapped[UUID] = _pk()
    org_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_rooms_org_name"),
        CheckConstraint("capacity > 0", name="ck_rooms_capacity_positive"),
    )


# --- Reservations ----------------------------------------------------------


class ReservationModel(Base):
    """A hold or a confirmed booking. See :class:`ReservationState`."""

    __tablename__ = "reservations"

    id: Mapped[UUID] = _pk()
    #: Short human-quotable handle, e.g. ``HDL-7Q2K``. The model quotes this
    #: back to the user; the UUID never leaves the server.
    reference: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    org_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    room_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("rooms.id", ondelete="CASCADE")
    )
    user_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    attendees: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The half-open ``[starts_at, ends_at)`` interval, stored as a native
    #: range so the exclusion constraint can index it with GiST.
    period: Mapped[object] = mapped_column(TSTZRANGE, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    hold_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # The whole concurrency story in one declaration: no two reservations
        # that currently occupy a room may have overlapping periods. The WHERE
        # clause is what lets cancelled and expired rows stay in the table as
        # an audit trail without blocking anybody.
        ExcludeConstraint(
            ("room_id", "="),
            ("period", "&&"),
            name="ex_reservations_no_overlap",
            using="gist",
            where=(
                f"state IN ('{ReservationState.HELD}', '{ReservationState.CONFIRMED}')"
            ),
        ),
        CheckConstraint(
            "state IN ('held', 'confirmed', 'cancelled', 'expired')",
            name="ck_reservations_state",
        ),
        CheckConstraint(
            "(state <> 'held') OR (hold_expires_at IS NOT NULL)",
            name="ck_reservations_hold_has_deadline",
        ),
        CheckConstraint("attendees > 0", name="ck_reservations_attendees_positive"),
        Index("ix_reservations_user_state", "user_id", "state"),
        Index("ix_reservations_org_state", "org_id", "state"),
        # Drives the hold sweeper, which scans only live holds.
        Index(
            "ix_reservations_hold_expiry",
            "hold_expires_at",
            postgresql_where=(f"state = '{ReservationState.HELD}'"),
        ),
    )


class HoldEventModel(Base):
    """Append-only audit of hold lifecycle transitions.

    This is the evidence base for hold-cycling detection. It is written on
    every transition rather than derived from ``reservations`` so that a user
    who cycles holds cannot erase the trail by letting rows expire.
    """

    __tablename__ = "hold_events"

    id: Mapped[UUID] = _pk()
    org_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    room_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    reservation_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    event: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (Index("ix_hold_events_user_time", "user_id", "created_at"),)


class HoldRateLimitModel(Base):
    """An active rate limit produced by hold-cycling detection."""

    __tablename__ = "hold_rate_limits"

    id: Mapped[UUID] = _pk()
    org_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (Index("ix_hold_rate_limits_user", "user_id", "until"),)


# --- Conversations ---------------------------------------------------------


class ConversationModel(Base):
    __tablename__ = "conversations"

    id: Mapped[UUID] = _pk()
    org_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    user_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    #: Set once the conversation reaches a terminal status. Tagged onto every
    #: trace so a span tree can be filtered by how the conversation ended.
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    escalation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    turns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_conversations_user", "user_id", "created_at"),)


class MessageModel(Base):
    """Server-side conversation history.

    The client never supplies history. It supplies a conversation id, and the
    server replays what it recorded, including tool calls and tool results, so
    the model can see what it already did.
    """

    __tablename__ = "conversation_messages"

    id: Mapped[UUID] = _pk()
    conversation_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tool_calls: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_messages_seq"),
        Index("ix_messages_conversation", "conversation_id", "seq"),
    )


class PendingConfirmationModel(Base):
    """A high blast-radius tool call parked until the user confirms it.

    The agent cannot execute one of these. The arguments were already
    validated and are replayed verbatim on confirmation, so the model gets no
    second chance to change what it asked for.
    """

    __tablename__ = "pending_confirmations"

    id: Mapped[UUID] = _pk()
    conversation_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict] = mapped_column(JSONB, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        Index("ix_pending_confirmations_conv", "conversation_id", "status"),
    )


# --- Reliability telemetry -------------------------------------------------


class TurnModel(Base):
    """One user message and everything the agent did in response."""

    __tablename__ = "turns"

    id: Mapped[UUID] = _pk()
    conversation_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (UniqueConstraint("conversation_id", "seq", name="uq_turns_seq"),)


class ToolInvocationModel(Base):
    """One tool call, including the ones that never ran."""

    __tablename__ = "tool_invocations"

    id: Mapped[UUID] = _pk()
    conversation_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    turn_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    risk: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    arguments: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        Index("ix_tool_invocations_conv", "conversation_id"),
        Index("ix_tool_invocations_name_status", "tool_name", "status"),
    )


class SagaModel(Base):
    """One multi-step booking transaction."""

    __tablename__ = "sagas"

    id: Mapped[UUID] = _pk()
    conversation_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    reservation_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    failed_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SagaEventModel(Base):
    """Every forward step and every compensation, in the order they ran."""

    __tablename__ = "saga_events"

    id: Mapped[UUID] = _pk()
    saga_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sagas.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    step: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (Index("ix_saga_events_saga", "saga_id", "seq"),)


class AlertModel(Base):
    """An operator-facing record that something needs a human."""

    __tablename__ = "alerts"

    id: Mapped[UUID] = _pk()
    org_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    conversation_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="warning")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (Index("ix_alerts_kind_time", "kind", "created_at"),)


class NotificationModel(Base):
    """Outbox row written by the notify step and voided by its compensation."""

    __tablename__ = "notifications"

    id: Mapped[UUID] = _pk()
    reservation_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    recipient: Mapped[str] = mapped_column(String(128), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="sent")
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (Index("ix_notifications_reservation", "reservation_id"),)


class CalendarEntryModel(Base):
    """Simulated external calendar. Deleting a row is the compensation."""

    __tablename__ = "calendar_entries"

    id: Mapped[UUID] = _pk()
    reservation_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, unique=True
    )
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()
