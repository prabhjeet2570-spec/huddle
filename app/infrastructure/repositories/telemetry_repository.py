"""Durable telemetry: turns, tool invocations, sagas, alerts.

Everything the metrics script reports is read back out of these tables. That
is deliberate: a metric computed from a durable record can be recomputed and
audited, while a metric accumulated in process memory cannot.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    AlertKind,
    SagaStatus,
    StepPhase,
    StepStatus,
    ToolCallStatus,
)
from app.infrastructure.models import (
    AlertModel,
    SagaEventModel,
    SagaModel,
    ToolInvocationModel,
    TurnModel,
)


class TelemetryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- Turns -------------------------------------------------------------

    async def record_turn(
        self,
        *,
        conversation_id: UUID,
        seq: int,
        latency_ms: float,
        llm_calls: int,
        tool_calls: int,
        prompt_tokens: int,
        completion_tokens: int,
        outcome: str,
        trace_id: str | None,
    ) -> UUID:
        model = TurnModel(
            id=uuid4(),
            conversation_id=conversation_id,
            seq=seq,
            latency_ms=latency_ms,
            llm_calls=llm_calls,
            tool_calls=tool_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            outcome=outcome,
            trace_id=trace_id,
        )
        self._session.add(model)
        await self._session.commit()
        return model.id

    async def next_turn_seq(self, conversation_id: UUID) -> int:
        value = await self._session.scalar(
            select(func.coalesce(func.max(TurnModel.seq), 0)).where(
                TurnModel.conversation_id == conversation_id
            )
        )
        return int(value or 0) + 1

    # --- Tool invocations --------------------------------------------------

    async def record_tool_invocation(
        self,
        *,
        conversation_id: UUID,
        tool_name: str,
        risk: str,
        status: ToolCallStatus,
        attempts: int = 1,
        latency_ms: float = 0.0,
        error_type: str | None = None,
        arguments: dict[str, Any] | None = None,
        turn_id: UUID | None = None,
    ) -> None:
        self._session.add(
            ToolInvocationModel(
                id=uuid4(),
                conversation_id=conversation_id,
                turn_id=turn_id,
                tool_name=tool_name,
                risk=risk,
                status=status,
                attempts=attempts,
                latency_ms=latency_ms,
                error_type=error_type,
                arguments=arguments,
            )
        )
        await self._session.commit()

    # --- Sagas -------------------------------------------------------------

    async def start_saga(
        self,
        name: str,
        conversation_id: UUID | None = None,
        reservation_id: UUID | None = None,
    ) -> UUID:
        model = SagaModel(
            id=uuid4(),
            name=name,
            conversation_id=conversation_id,
            reservation_id=reservation_id,
            status=SagaStatus.RUNNING,
        )
        self._session.add(model)
        await self._session.commit()
        return model.id

    async def record_saga_event(
        self,
        saga_id: UUID,
        step: str,
        phase: StepPhase,
        status: StepStatus,
        detail: str | None = None,
    ) -> None:
        seq = await self._session.scalar(
            select(func.coalesce(func.max(SagaEventModel.seq), 0)).where(
                SagaEventModel.saga_id == saga_id
            )
        )
        self._session.add(
            SagaEventModel(
                id=uuid4(),
                saga_id=saga_id,
                seq=int(seq or 0) + 1,
                step=step,
                phase=phase,
                status=status,
                detail=detail,
            )
        )
        await self._session.commit()

    async def finish_saga(
        self,
        saga_id: UUID,
        status: SagaStatus,
        reservation_id: UUID | None = None,
        failed_step: str | None = None,
        error: str | None = None,
    ) -> None:
        model = await self._session.get(SagaModel, saga_id)
        if model is None:
            return
        model.status = status
        model.failed_step = failed_step
        model.error = error
        model.completed_at = func.now()
        if reservation_id is not None:
            model.reservation_id = reservation_id
        await self._session.commit()

    async def saga_events(self, saga_id: UUID) -> list[SagaEventModel]:
        return list(
            (
                await self._session.scalars(
                    select(SagaEventModel)
                    .where(SagaEventModel.saga_id == saga_id)
                    .order_by(SagaEventModel.seq.asc())
                )
            ).all()
        )

    # --- Alerts ------------------------------------------------------------

    async def raise_alert(
        self,
        kind: AlertKind,
        message: str,
        *,
        org_id: UUID | None = None,
        conversation_id: UUID | None = None,
        severity: str = "warning",
        details: dict[str, Any] | None = None,
    ) -> UUID:
        model = AlertModel(
            id=uuid4(),
            org_id=org_id,
            conversation_id=conversation_id,
            kind=kind,
            severity=severity,
            message=message,
            details=details or {},
        )
        self._session.add(model)
        await self._session.commit()
        return model.id

    async def list_alerts(
        self,
        org_id: UUID | None = None,
        limit: int = 100,
    ) -> list[AlertModel]:
        statement = select(AlertModel).order_by(AlertModel.created_at.desc())
        if org_id is not None:
            statement = statement.where(AlertModel.org_id == org_id)
        return list((await self._session.scalars(statement.limit(limit))).all())
