"""Evidence store for hold-cycling detection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import HoldEvent
from app.infrastructure.models import HoldEventModel, HoldRateLimitModel


@dataclass(frozen=True, slots=True)
class HoldWindow:
    """What one user did with holds over a rolling window."""

    window_start: datetime
    window_end: datetime
    created: int
    confirmed: int
    expired: int
    released: int
    distinct_rooms: int

    @property
    def confirm_ratio(self) -> float:
        # Undefined with no holds; treated as fully compliant so a user with
        # no history is never flagged.
        return self.confirmed / self.created if self.created else 1.0

    def as_evidence(self) -> dict[str, Any]:
        return {
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "holds_created": self.created,
            "holds_confirmed": self.confirmed,
            "holds_expired": self.expired,
            "holds_released": self.released,
            "confirm_ratio": round(self.confirm_ratio, 4),
            "distinct_rooms": self.distinct_rooms,
        }


class AbuseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def hold_window(
        self,
        user_id: UUID,
        now: datetime,
        window_seconds: int,
    ) -> HoldWindow:
        start = now - timedelta(seconds=window_seconds)
        rows = await self._session.execute(
            select(HoldEventModel.event, func.count())
            .where(
                HoldEventModel.user_id == user_id,
                HoldEventModel.created_at >= start,
                HoldEventModel.created_at <= now,
            )
            .group_by(HoldEventModel.event)
        )
        counts = dict(rows.all())
        distinct_rooms = await self._session.scalar(
            select(func.count(func.distinct(HoldEventModel.room_id))).where(
                HoldEventModel.user_id == user_id,
                HoldEventModel.created_at >= start,
                HoldEventModel.created_at <= now,
                HoldEventModel.event == HoldEvent.CREATED,
            )
        )
        return HoldWindow(
            window_start=start,
            window_end=now,
            created=int(counts.get(HoldEvent.CREATED, 0)),
            confirmed=int(counts.get(HoldEvent.CONFIRMED, 0)),
            expired=int(counts.get(HoldEvent.EXPIRED, 0)),
            released=int(counts.get(HoldEvent.RELEASED, 0)),
            distinct_rooms=int(distinct_rooms or 0),
        )

    async def active_rate_limit(
        self,
        user_id: UUID,
        now: datetime,
    ) -> HoldRateLimitModel | None:
        return await self._session.scalar(
            select(HoldRateLimitModel)
            .where(
                HoldRateLimitModel.user_id == user_id,
                HoldRateLimitModel.until > now,
            )
            .order_by(HoldRateLimitModel.until.desc())
            .limit(1)
        )

    async def apply_rate_limit(
        self,
        *,
        org_id: UUID,
        user_id: UUID,
        until: datetime,
        reason: str,
        evidence: dict[str, Any],
    ) -> HoldRateLimitModel:
        model = HoldRateLimitModel(
            id=uuid4(),
            org_id=org_id,
            user_id=user_id,
            until=until,
            reason=reason,
            evidence=evidence,
        )
        self._session.add(model)
        await self._session.commit()
        return model

    async def list_rate_limits(self, limit: int = 200) -> list[HoldRateLimitModel]:
        return list(
            (
                await self._session.scalars(
                    select(HoldRateLimitModel)
                    .order_by(HoldRateLimitModel.created_at.desc())
                    .limit(limit)
                )
            ).all()
        )
