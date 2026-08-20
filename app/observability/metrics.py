"""Measured agent metrics.

Every number here is computed from a durable table, never accumulated in
process memory and never estimated. That is the point: a metric you can
recompute from records is one you can audit, and one that survives a restart.

Definitions are stated explicitly because reliability metrics are easy to
report flatteringly by accident.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    ConversationStatus,
    SagaStatus,
    ToolCallStatus,
)
from app.infrastructure.models import (
    ConversationModel,
    HoldRateLimitModel,
    ReservationModel,
    SagaModel,
    ToolInvocationModel,
    TurnModel,
)

#: Statuses that mean the model asked for something it should not have. These
#: are the agent's mistakes, as opposed to the user asking for the impossible.
WRONG_CALL_STATUSES = (
    ToolCallStatus.INVALID_ARGUMENTS,
    ToolCallStatus.DOMAIN_ERROR,
)


@dataclass(slots=True)
class Metric:
    name: str
    value: float
    unit: str
    numerator: int | None = None
    denominator: int | None = None
    definition: str = ""


@dataclass(slots=True)
class MetricsReport:
    metrics: list[Metric] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def add(
        self,
        name: str,
        value: float,
        unit: str,
        definition: str,
        numerator: int | None = None,
        denominator: int | None = None,
    ) -> None:
        self.metrics.append(
            Metric(name, round(value, 4), unit, numerator, denominator, definition)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "metrics": [asdict(metric) for metric in self.metrics],
            "counts": self.counts,
        }

    def get(self, name: str) -> Metric | None:
        return next((m for m in self.metrics if m.name == name), None)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = min(round(fraction * (len(ordered) - 1)), len(ordered) - 1)
    return ordered[index]


async def collect_metrics(
    session: AsyncSession,
    org_id: UUID | None = None,
) -> MetricsReport:
    report = MetricsReport()

    def scope(statement: Select, column) -> Select:
        return statement.where(column == org_id) if org_id is not None else statement

    # --- Conversations -----------------------------------------------------
    rows = await session.execute(
        scope(
            select(ConversationModel.status, func.count()).group_by(
                ConversationModel.status
            ),
            ConversationModel.org_id,
        )
    )
    by_status = {status: int(count) for status, count in rows.all()}
    terminal = sum(by_status.get(status, 0) for status in ConversationStatus.terminal())
    completed = by_status.get(ConversationStatus.COMPLETED, 0)
    escalated = by_status.get(ConversationStatus.ESCALATED, 0)

    report.counts["conversations_total"] = sum(by_status.values())
    report.counts["conversations_terminal"] = terminal
    report.counts.update({f"conversations_{k}": v for k, v in by_status.items()})

    report.add(
        "task_completion_rate",
        _ratio(completed, terminal),
        "ratio",
        "Conversations that ended completed, over conversations that reached "
        "any terminal state. Conversations still awaiting a user reply are "
        "excluded from both sides.",
        completed,
        terminal,
    )
    report.add(
        "escalation_rate",
        _ratio(escalated, terminal),
        "ratio",
        "Conversations handed to a human, over conversations that reached any "
        "terminal state.",
        escalated,
        terminal,
    )

    # --- Tool calls --------------------------------------------------------
    conversation_scope = (
        select(ConversationModel.id).where(ConversationModel.org_id == org_id)
        if org_id is not None
        else None
    )

    def tool_scope(statement: Select) -> Select:
        if conversation_scope is None:
            return statement
        return statement.where(
            ToolInvocationModel.conversation_id.in_(conversation_scope)
        )

    tool_rows = await session.execute(
        tool_scope(
            select(ToolInvocationModel.status, func.count()).group_by(
                ToolInvocationModel.status
            )
        )
    )
    tool_by_status = {status: int(count) for status, count in tool_rows.all()}
    tool_total = sum(tool_by_status.values())
    wrong = sum(tool_by_status.get(status, 0) for status in WRONG_CALL_STATUSES)

    report.counts["tool_calls_total"] = tool_total
    report.counts.update({f"tool_calls_{k}": v for k, v in tool_by_status.items()})

    report.add(
        "wrong_tool_call_rate",
        _ratio(wrong, tool_total),
        "ratio",
        "Tool calls rejected for invalid arguments or refused by a domain "
        "rule, over all tool calls. Counts the model asking for something it "
        "should not have.",
        wrong,
        tool_total,
    )

    retried = await session.scalar(
        tool_scope(select(func.count()).select_from(ToolInvocationModel)).where(
            ToolInvocationModel.attempts > 1
        )
    )
    report.add(
        "tool_retry_rate",
        _ratio(int(retried or 0), tool_total),
        "ratio",
        "Tool calls that needed more than one attempt, over all tool calls.",
        int(retried or 0),
        tool_total,
    )

    # --- Compensations -----------------------------------------------------
    saga_rows = await session.execute(
        select(SagaModel.status, func.count()).group_by(SagaModel.status)
    )
    saga_by_status = {status: int(count) for status, count in saga_rows.all()}
    saga_total = sum(saga_by_status.values())
    compensated = saga_by_status.get(SagaStatus.COMPENSATED, 0) + saga_by_status.get(
        SagaStatus.COMPENSATION_FAILED, 0
    )

    report.counts["sagas_total"] = saga_total
    report.counts.update({f"sagas_{k}": v for k, v in saga_by_status.items()})

    report.add(
        "compensation_frequency",
        _ratio(compensated, saga_total),
        "ratio",
        "Booking transactions that had to be rolled back, over all booking "
        "transactions attempted.",
        compensated,
        saga_total,
    )
    report.add(
        "compensation_success_rate",
        _ratio(saga_by_status.get(SagaStatus.COMPENSATED, 0), compensated),
        "ratio",
        "Rollbacks that returned state to clean, over all rollbacks. Anything "
        "below 1.0 means a booking was left inconsistent.",
        saga_by_status.get(SagaStatus.COMPENSATED, 0),
        compensated,
    )

    # --- Latency -----------------------------------------------------------
    def turn_scope(statement: Select) -> Select:
        if conversation_scope is None:
            return statement
        return statement.where(TurnModel.conversation_id.in_(conversation_scope))

    latencies = [
        float(value)
        for value in (
            await session.scalars(turn_scope(select(TurnModel.latency_ms)))
        ).all()
    ]
    report.counts["turns_total"] = len(latencies)
    report.add(
        "turn_latency_p50_ms",
        _percentile(latencies, 0.50),
        "milliseconds",
        "Median wall-clock time from receiving a user message to returning a "
        "reply, including every tool call and retry.",
        denominator=len(latencies),
    )
    report.add(
        "turn_latency_p95_ms",
        _percentile(latencies, 0.95),
        "milliseconds",
        "95th percentile of the same measurement.",
        denominator=len(latencies),
    )
    report.add(
        "turn_latency_mean_ms",
        statistics.fmean(latencies) if latencies else 0.0,
        "milliseconds",
        "Arithmetic mean turn latency.",
        denominator=len(latencies),
    )

    # --- Efficiency --------------------------------------------------------
    confirmed = await session.scalar(
        scope(
            select(func.count()).select_from(ReservationModel),
            ReservationModel.org_id,
        ).where(ReservationModel.state == "confirmed")
    )
    confirmed = int(confirmed or 0)
    report.counts["bookings_confirmed"] = confirmed
    report.add(
        "tool_calls_per_confirmed_booking",
        _ratio(tool_total, confirmed),
        "calls",
        "All tool calls divided by confirmed bookings. Lower is a more "
        "direct agent; it rises when the model explores or retries.",
        tool_total,
        confirmed,
    )

    tokens = await session.scalar(
        scope(
            select(func.coalesce(func.sum(ConversationModel.tokens_used), 0)),
            ConversationModel.org_id,
        )
    )
    report.counts["tokens_total"] = int(tokens or 0)
    report.add(
        "tokens_per_conversation",
        _ratio(int(tokens or 0), sum(by_status.values())),
        "tokens",
        "Mean prompt plus completion tokens per conversation.",
        int(tokens or 0),
        sum(by_status.values()),
    )

    # --- Hold cycling ------------------------------------------------------
    holds = await session.scalar(
        scope(
            select(func.count()).select_from(ReservationModel),
            ReservationModel.org_id,
        )
    )
    expired = await session.scalar(
        scope(
            select(func.count()).select_from(ReservationModel),
            ReservationModel.org_id,
        ).where(ReservationModel.state == "expired")
    )
    limits = await session.scalar(
        scope(
            select(func.count()).select_from(HoldRateLimitModel),
            HoldRateLimitModel.org_id,
        )
    )
    report.counts["reservations_total"] = int(holds or 0)
    report.counts["holds_expired"] = int(expired or 0)
    report.counts["hold_rate_limits_applied"] = int(limits or 0)
    report.add(
        "hold_expiry_rate",
        _ratio(int(expired or 0), int(holds or 0)),
        "ratio",
        "Reservations that lapsed unconfirmed, over all reservations. The "
        "signal hold-cycling detection watches.",
        int(expired or 0),
        int(holds or 0),
    )

    return report
