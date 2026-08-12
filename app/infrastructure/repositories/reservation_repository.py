"""Reservation persistence.

Nothing in this module decides availability by reading first. Holds and
bookings are inserted, and PostgreSQL's exclusion constraint decides whether
they may exist. An ``ExclusionViolation`` is not an error path to be avoided;
it is the mechanism.
"""

from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4

from asyncpg.exceptions import ExclusionViolationError
from sqlalchemy import Select, and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import OFFICE_TZ, settings
from app.domain.enums import HoldEvent, ReservationState
from app.domain.exceptions import RoomNotAvailable
from app.domain.reservation import Reservation
from app.domain.room import Room
from app.domain.time_range import TimeRange
from app.infrastructure.models import (
    HoldEventModel,
    ReservationModel,
    RoomModel,
)

_REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_reference() -> str:
    """A short, unambiguous handle the user can read aloud."""
    body = "".join(secrets.choice(_REFERENCE_ALPHABET) for _ in range(4))
    return f"HDL-{body}"


def _period(time_range: TimeRange) -> Range[datetime]:
    # '[)' matches the half-open semantics of TimeRange exactly.
    return Range(time_range.starts_at, time_range.ends_at, bounds="[)")


class ReservationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- Rooms -------------------------------------------------------------

    async def get_room_by_name(self, org_id: UUID, name: str) -> Room | None:
        model = await self._session.scalar(
            select(RoomModel).where(
                RoomModel.org_id == org_id, func.upper(RoomModel.name) == name.upper()
            )
        )
        return self._to_room(model) if model else None

    async def list_rooms(self, org_id: UUID) -> list[Room]:
        models = await self._session.scalars(
            select(RoomModel)
            .where(RoomModel.org_id == org_id)
            .order_by(RoomModel.capacity.asc(), RoomModel.name.asc())
        )
        return [self._to_room(model) for model in models]

    async def max_capacity(self, org_id: UUID) -> int:
        value = await self._session.scalar(
            select(func.max(RoomModel.capacity)).where(RoomModel.org_id == org_id)
        )
        return int(value or 0)

    # --- Holds -------------------------------------------------------------

    async def sweep_expired_holds(
        self,
        now: datetime,
        room_id: UUID | None = None,
        limit: int | None = None,
    ) -> list[UUID]:
        """Transition lapsed holds to ``expired`` and return their ids.

        Called both by the background sweeper and inline before placing a
        hold, so a lapsed hold never blocks a legitimate request just because
        the sweeper has not come around yet.
        """
        conditions = [
            ReservationModel.state == ReservationState.HELD,
            ReservationModel.hold_expires_at <= now,
        ]
        if room_id is not None:
            conditions.append(ReservationModel.room_id == room_id)

        selector: Select = select(ReservationModel.id).where(and_(*conditions))
        if limit is not None:
            selector = selector.limit(limit)
        # FOR UPDATE SKIP LOCKED lets several sweeper replicas run at once
        # without fighting over the same rows.
        selector = selector.with_for_update(skip_locked=True)

        ids = list((await self._session.scalars(selector)).all())
        if not ids:
            return []

        await self._session.execute(
            update(ReservationModel)
            .where(ReservationModel.id.in_(ids))
            .values(state=ReservationState.EXPIRED, hold_expires_at=None)
        )
        return ids

    async def place_hold(
        self,
        *,
        org_id: UUID,
        room: Room,
        user_id: UUID,
        conversation_id: UUID | None,
        title: str,
        attendees: int,
        time_range: TimeRange,
        now: datetime,
        ttl_seconds: int | None = None,
    ) -> Reservation:
        """Insert a hold, letting the database arbitrate contention."""
        ttl = ttl_seconds if ttl_seconds is not None else settings.hold_ttl_seconds

        # Clear this room's lapsed holds in the same transaction as the
        # insert, so the check and the write cannot be interleaved.
        expired_ids = await self.sweep_expired_holds(now, room_id=room.id)
        for expired_id in expired_ids:
            await self.record_hold_event(
                org_id=org_id,
                user_id=user_id,
                room_id=room.id,
                reservation_id=expired_id,
                event=HoldEvent.EXPIRED,
                created_at=now,
            )

        model = ReservationModel(
            id=uuid4(),
            reference=new_reference(),
            org_id=org_id,
            room_id=room.id,
            user_id=user_id,
            conversation_id=conversation_id,
            title=title,
            attendees=attendees,
            period=_period(time_range),
            state=ReservationState.HELD,
            hold_expires_at=now + timedelta(seconds=ttl),
        )
        try:
            # SAVEPOINT: a rejected insert must not poison the outer
            # transaction, which still owes the sweep its hold events.
            async with self._session.begin_nested():
                self._session.add(model)
                await self._session.flush()
        except IntegrityError as error:
            if _is_exclusion_violation(error):
                # Somebody else holds or booked an overlapping slice. This is
                # the constraint doing its job, not a failure to handle.
                await self._session.commit()
                raise RoomNotAvailable(room_name=room.name) from error
            await self._session.rollback()
            raise

        await self.record_hold_event(
            org_id=org_id,
            user_id=user_id,
            room_id=room.id,
            reservation_id=model.id,
            event=HoldEvent.CREATED,
            created_at=now,
        )
        await self._session.commit()
        return await self.get_by_id(model.id, org_id=org_id)  # type: ignore[return-value]

    async def confirm_hold(
        self,
        reservation_id: UUID,
        org_id: UUID,
        now: datetime,
    ) -> Reservation | None:
        """Flip a live hold to ``confirmed``.

        Conditional on the row still being a live hold, so a hold that the
        sweeper expired a microsecond earlier cannot be confirmed by a
        straggling request.
        """
        result = await self._session.execute(
            update(ReservationModel)
            .where(
                ReservationModel.id == reservation_id,
                ReservationModel.org_id == org_id,
                ReservationModel.state == ReservationState.HELD,
                ReservationModel.hold_expires_at > now,
            )
            .values(state=ReservationState.CONFIRMED, hold_expires_at=None)
            .returning(ReservationModel.id)
        )
        if result.scalar_one_or_none() is None:
            return None

        reservation = await self.get_by_id(reservation_id, org_id=org_id)
        if reservation is not None:
            await self.record_hold_event(
                org_id=org_id,
                user_id=reservation.user_id,
                room_id=reservation.room_id,
                reservation_id=reservation_id,
                event=HoldEvent.CONFIRMED,
                created_at=now,
            )
        await self._session.commit()
        return reservation

    async def release(
        self,
        reservation_id: UUID,
        org_id: UUID,
        target_state: ReservationState = ReservationState.CANCELLED,
    ) -> bool:
        """Release a blocking reservation. Idempotent: returns whether it acted.

        This is the compensation for :meth:`place_hold` and the implementation
        of cancellation. Running it against an already-released reservation is
        a no-op that reports ``False``.
        """
        result = await self._session.execute(
            update(ReservationModel)
            .where(
                ReservationModel.id == reservation_id,
                ReservationModel.org_id == org_id,
                ReservationModel.state.in_(ReservationState.blocking()),
            )
            .values(state=target_state, hold_expires_at=None)
            .returning(ReservationModel.id)
        )
        released = result.scalar_one_or_none() is not None
        await self._session.commit()
        return released

    async def revert_to_hold(
        self,
        reservation_id: UUID,
        org_id: UUID,
        now: datetime,
        ttl_seconds: int | None = None,
    ) -> bool:
        """Compensation for confirm: put a confirmed reservation back on hold."""
        ttl = ttl_seconds if ttl_seconds is not None else settings.hold_ttl_seconds
        result = await self._session.execute(
            update(ReservationModel)
            .where(
                ReservationModel.id == reservation_id,
                ReservationModel.org_id == org_id,
                ReservationModel.state == ReservationState.CONFIRMED,
            )
            .values(
                state=ReservationState.HELD,
                hold_expires_at=now + timedelta(seconds=ttl),
            )
            .returning(ReservationModel.id)
        )
        reverted = result.scalar_one_or_none() is not None
        await self._session.commit()
        return reverted

    # --- Queries -----------------------------------------------------------

    async def get_by_id(
        self,
        reservation_id: UUID,
        org_id: UUID,
    ) -> Reservation | None:
        row = (
            await self._session.execute(
                select(ReservationModel, RoomModel.name)
                .join(RoomModel, ReservationModel.room_id == RoomModel.id)
                .where(
                    ReservationModel.id == reservation_id,
                    ReservationModel.org_id == org_id,
                )
            )
        ).one_or_none()
        return self._to_domain(*row) if row else None

    async def get_by_id_any_org(self, reservation_id: UUID) -> Reservation | None:
        """Fetch without tenant scoping. Only the background sweeper may use
        this: it runs outside any request and must see every organization."""
        row = (
            await self._session.execute(
                select(ReservationModel, RoomModel.name)
                .join(RoomModel, ReservationModel.room_id == RoomModel.id)
                .where(ReservationModel.id == reservation_id)
            )
        ).one_or_none()
        return self._to_domain(*row) if row else None

    async def get_by_reference(
        self,
        reference: str,
        org_id: UUID,
        user_id: UUID | None = None,
    ) -> Reservation | None:
        conditions = [
            func.upper(ReservationModel.reference) == reference.strip().upper(),
            ReservationModel.org_id == org_id,
        ]
        if user_id is not None:
            conditions.append(ReservationModel.user_id == user_id)

        row = (
            await self._session.execute(
                select(ReservationModel, RoomModel.name)
                .join(RoomModel, ReservationModel.room_id == RoomModel.id)
                .where(and_(*conditions))
            )
        ).one_or_none()
        return self._to_domain(*row) if row else None

    async def list_for_user(
        self,
        user_id: UUID,
        org_id: UUID,
        states: tuple[ReservationState, ...] = ReservationState.blocking(),
    ) -> list[Reservation]:
        rows = await self._session.execute(
            select(ReservationModel, RoomModel.name)
            .join(RoomModel, ReservationModel.room_id == RoomModel.id)
            .where(
                ReservationModel.user_id == user_id,
                ReservationModel.org_id == org_id,
                ReservationModel.state.in_(states),
            )
            .order_by(func.lower(ReservationModel.period).asc())
        )
        return [self._to_domain(model, name) for model, name in rows]

    async def find_available_rooms(
        self,
        org_id: UUID,
        time_range: TimeRange,
        min_capacity: int,
        now: datetime,
    ) -> list[Room]:
        """Rooms with no blocking reservation overlapping the whole range.

        A hold that has already lapsed does not exclude a room, so the result
        matches what :meth:`place_hold` would actually allow.
        """
        blocking = (
            select(ReservationModel.id)
            .where(
                ReservationModel.room_id == RoomModel.id,
                ReservationModel.state.in_(ReservationState.blocking()),
                ReservationModel.period.op("&&")(_period(time_range)),
                or_(
                    ReservationModel.state == ReservationState.CONFIRMED,
                    ReservationModel.hold_expires_at > now,
                ),
            )
            .exists()
        )
        models = await self._session.scalars(
            select(RoomModel)
            .where(
                RoomModel.org_id == org_id,
                RoomModel.capacity >= min_capacity,
                ~blocking,
            )
            .order_by(RoomModel.capacity.asc(), RoomModel.name.asc())
        )
        return [self._to_room(model) for model in models]

    async def find_conflict(
        self,
        room_id: UUID,
        time_range: TimeRange,
        now: datetime,
    ) -> tuple[datetime, datetime] | None:
        """The first blocking interval overlapping ``time_range``, for messaging."""
        row = (
            await self._session.execute(
                select(
                    func.lower(ReservationModel.period),
                    func.upper(ReservationModel.period),
                )
                .where(
                    ReservationModel.room_id == room_id,
                    ReservationModel.state.in_(ReservationState.blocking()),
                    ReservationModel.period.op("&&")(_period(time_range)),
                    or_(
                        ReservationModel.state == ReservationState.CONFIRMED,
                        ReservationModel.hold_expires_at > now,
                    ),
                )
                .order_by(func.lower(ReservationModel.period).asc())
                .limit(1)
            )
        ).one_or_none()
        if row is None:
            return None
        return self._as_office(row[0]), self._as_office(row[1])

    async def find_day_reservations(
        self,
        room_id: UUID,
        day: date,
        now: datetime,
    ) -> list[TimeRange]:
        day_range = TimeRange(
            starts_at=datetime.combine(day, settings.business_start, tzinfo=OFFICE_TZ),
            ends_at=datetime.combine(day, settings.business_end, tzinfo=OFFICE_TZ),
        )
        rows = await self._session.execute(
            select(
                func.lower(ReservationModel.period),
                func.upper(ReservationModel.period),
            )
            .where(
                ReservationModel.room_id == room_id,
                ReservationModel.state.in_(ReservationState.blocking()),
                ReservationModel.period.op("&&")(_period(day_range)),
                or_(
                    ReservationModel.state == ReservationState.CONFIRMED,
                    ReservationModel.hold_expires_at > now,
                ),
            )
            .order_by(func.lower(ReservationModel.period).asc())
        )
        return [
            TimeRange(
                max(self._as_office(start), day_range.starts_at),
                min(self._as_office(end), day_range.ends_at),
            )
            for start, end in rows
        ]

    async def expiring_holds(self, now: datetime, limit: int = 200) -> list[UUID]:
        return list(
            (
                await self._session.scalars(
                    select(ReservationModel.id)
                    .where(
                        ReservationModel.state == ReservationState.HELD,
                        ReservationModel.hold_expires_at <= now,
                    )
                    .limit(limit)
                )
            ).all()
        )

    # --- Hold audit --------------------------------------------------------

    async def record_hold_event(
        self,
        *,
        org_id: UUID,
        user_id: UUID,
        room_id: UUID,
        reservation_id: UUID,
        event: HoldEvent,
        created_at: datetime,
    ) -> None:
        """Append to the hold audit trail.

        ``created_at`` comes from the domain clock rather than the database, so
        the rolling detection window and the events it reads over always agree
        on what time it is.
        """
        self._session.add(
            HoldEventModel(
                org_id=org_id,
                user_id=user_id,
                room_id=room_id,
                reservation_id=reservation_id,
                event=event,
                created_at=created_at,
            )
        )

    async def ensure_btree_gist(self) -> None:
        await self._session.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))

    # --- Mapping -----------------------------------------------------------

    @staticmethod
    def _as_office(value: datetime) -> datetime:
        return value.astimezone(OFFICE_TZ)

    @classmethod
    def _to_domain(cls, model: ReservationModel, room_name: str) -> Reservation:
        period = model.period
        return Reservation(
            id=model.id,
            reference=model.reference,
            org_id=model.org_id,
            room_id=model.room_id,
            room_name=room_name,
            user_id=model.user_id,
            title=model.title,
            attendees=model.attendees,
            time_range=TimeRange(
                cls._as_office(period.lower), cls._as_office(period.upper)
            ),
            state=ReservationState(model.state),
            hold_expires_at=(
                cls._as_office(model.hold_expires_at) if model.hold_expires_at else None
            ),
            created_at=cls._as_office(model.created_at),
            updated_at=cls._as_office(model.updated_at),
        )

    @staticmethod
    def _to_room(model: RoomModel) -> Room:
        return Room(
            id=model.id, org_id=model.org_id, name=model.name, capacity=model.capacity
        )


def _is_exclusion_violation(error: IntegrityError) -> bool:
    original = getattr(error, "orig", None)
    cause = getattr(original, "__cause__", None)
    if isinstance(cause, ExclusionViolationError) or isinstance(
        original, ExclusionViolationError
    ):
        return True
    return "ex_reservations_no_overlap" in str(error)
