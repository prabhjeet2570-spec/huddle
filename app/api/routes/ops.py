"""Operational endpoints: health, metrics and the alert queue."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.deps import AdminUser, CurrentUser, DbSession
from app.infrastructure.repositories.telemetry_repository import TelemetryRepository
from app.observability.metrics import collect_metrics

router = APIRouter(tags=["ops"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/metrics")
async def metrics(user: CurrentUser, session: DbSession) -> dict[str, Any]:
    """Measured agent-reliability metrics, computed from durable records."""
    report = await collect_metrics(session, org_id=user.org_id)
    return report.as_dict()


@router.get("/alerts")
async def alerts(user: AdminUser, session: DbSession) -> list[dict[str, Any]]:
    """The human-review queue. Anything here needs a person."""
    records = await TelemetryRepository(session).list_alerts(org_id=user.org_id)
    return [
        {
            "id": str(record.id),
            "kind": record.kind,
            "severity": record.severity,
            "message": record.message,
            "details": record.details,
            "conversation_id": (
                str(record.conversation_id) if record.conversation_id else None
            ),
            "created_at": record.created_at.isoformat(),
            "resolved": record.resolved_at is not None,
        }
        for record in records
    ]
