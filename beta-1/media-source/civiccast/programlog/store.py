# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable store for program slots and their materialized occurrences (CA-1)."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from civiccast.programlog.models import (
    ChannelProgramSlotDb,
    ProgramSlot,
    ProgramSlotOccurrenceDb,
    SlotOccurrence,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]

__all__ = ["PostgresProgramLogStore"]


class PostgresProgramLogStore:
    """SQLAlchemy-backed program-log store (Postgres or managed SQLite)."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    # -- slots ----------------------------------------------------------

    def create_slot(self, slot: ProgramSlot) -> ProgramSlot:
        with self._session_factory() as session:
            session.add(
                ChannelProgramSlotDb(
                    slot_id=slot.slot_id,
                    channel_id=slot.channel_id,
                    asset_id=slot.asset_id,
                    title_override=slot.title_override,
                    recurrence=slot.recurrence,
                    first_start_at=slot.first_start_at,
                    recurrence_anchor_local=slot.recurrence_anchor_local,
                    duration_seconds=slot.duration_seconds,
                    repeat_until=slot.repeat_until,
                    enabled=slot.enabled,
                    created_at=slot.created_at,
                    updated_at=slot.updated_at,
                )
            )
            session.commit()
            return slot

    def get_slot(self, slot_id: str) -> ProgramSlot | None:
        with self._session_factory() as session:
            row = session.get(ChannelProgramSlotDb, slot_id)
            return _slot_row_to_model(row) if row is not None else None

    def list_slots(self, *, channel_id: str | None = None) -> list[ProgramSlot]:
        with self._session_factory() as session:
            stmt = select(ChannelProgramSlotDb).order_by(
                ChannelProgramSlotDb.first_start_at.asc(),
                ChannelProgramSlotDb.slot_id.asc(),
            )
            if channel_id is not None:
                stmt = stmt.where(ChannelProgramSlotDb.channel_id == channel_id)
            rows = session.scalars(stmt).all()
            return [_slot_row_to_model(row) for row in rows]

    def update_slot(self, slot: ProgramSlot) -> ProgramSlot:
        with self._session_factory() as session:
            row = session.get(ChannelProgramSlotDb, slot.slot_id)
            if row is None:
                return self.create_slot(slot)
            row.channel_id = slot.channel_id
            row.asset_id = slot.asset_id
            row.title_override = slot.title_override
            row.recurrence = slot.recurrence
            row.first_start_at = slot.first_start_at
            row.recurrence_anchor_local = slot.recurrence_anchor_local
            row.duration_seconds = slot.duration_seconds
            row.repeat_until = slot.repeat_until
            row.enabled = slot.enabled
            row.updated_at = slot.updated_at
            session.commit()
            return slot

    def delete_slot(self, slot_id: str) -> None:
        with self._session_factory() as session:
            row = session.get(ChannelProgramSlotDb, slot_id)
            if row is not None:
                session.delete(row)
                session.commit()

    # -- occurrences ----------------------------------------------------

    def record_occurrence(self, occurrence: SlotOccurrence) -> SlotOccurrence:
        with self._session_factory() as session:
            session.add(
                ProgramSlotOccurrenceDb(
                    occurrence_id=occurrence.occurrence_id,
                    slot_id=occurrence.slot_id,
                    occurrence_start=occurrence.occurrence_start,
                    schedule_item_id=occurrence.schedule_item_id,
                    status=occurrence.status,
                    detail=occurrence.detail,
                    created_at=occurrence.created_at,
                )
            )
            session.commit()
            return occurrence

    def update_occurrence(self, occurrence: SlotOccurrence) -> SlotOccurrence:
        with self._session_factory() as session:
            row = session.get(ProgramSlotOccurrenceDb, occurrence.occurrence_id)
            if row is None:
                return self.record_occurrence(occurrence)
            row.schedule_item_id = occurrence.schedule_item_id
            row.status = occurrence.status
            row.detail = occurrence.detail
            session.commit()
            return occurrence

    def list_occurrences(
        self,
        *,
        slot_id: str | None = None,
        slot_ids: set[str] | None = None,
        start_from: datetime | None = None,
        limit: int | None = None,
    ) -> list[SlotOccurrence]:
        with self._session_factory() as session:
            stmt = select(ProgramSlotOccurrenceDb).order_by(
                ProgramSlotOccurrenceDb.occurrence_start.asc(),
                ProgramSlotOccurrenceDb.occurrence_id.asc(),
            )
            if slot_id is not None:
                stmt = stmt.where(ProgramSlotOccurrenceDb.slot_id == slot_id)
            if slot_ids is not None:
                # Scope to a known set of slots (e.g. one channel's slots) so the
                # read does not materialize every future occurrence across all
                # channels. An empty set correctly yields no rows.
                stmt = stmt.where(ProgramSlotOccurrenceDb.slot_id.in_(slot_ids))
            if start_from is not None:
                stmt = stmt.where(ProgramSlotOccurrenceDb.occurrence_start >= start_from)
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = session.scalars(stmt).all()
            return [_occurrence_row_to_model(row) for row in rows]


def _coerce_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"Unsupported datetime value: {value!r}")


def _slot_row_to_model(row: ChannelProgramSlotDb) -> ProgramSlot:
    return ProgramSlot(
        slot_id=row.slot_id,
        channel_id=row.channel_id,
        asset_id=row.asset_id,
        title_override=row.title_override,
        recurrence=row.recurrence,  # type: ignore[arg-type]
        first_start_at=_coerce_datetime(row.first_start_at),
        recurrence_anchor_local=row.recurrence_anchor_local,
        duration_seconds=row.duration_seconds,
        repeat_until=(_coerce_datetime(row.repeat_until) if row.repeat_until is not None else None),
        enabled=row.enabled,
        created_at=_coerce_datetime(row.created_at),
        updated_at=_coerce_datetime(row.updated_at),
    )


def _occurrence_row_to_model(row: ProgramSlotOccurrenceDb) -> SlotOccurrence:
    return SlotOccurrence(
        occurrence_id=row.occurrence_id,
        slot_id=row.slot_id,
        occurrence_start=_coerce_datetime(row.occurrence_start),
        schedule_item_id=row.schedule_item_id,
        status=row.status,  # type: ignore[arg-type]
        detail=row.detail,
        created_at=_coerce_datetime(row.created_at),
    )
