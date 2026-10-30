# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable store for meeting agendas + items (S25 slice 1).

Per-request store over the single global session factory (same lazy posture as
eas / ai_models / metadata / reporting / underwriting). All comparisons bind
through parameters (no string interpolation) and ride the indexes defined in
migration ``0058_meeting_agenda``.

* ``upsert_agenda`` / ``get_agenda`` / ``get_agenda_by_asset`` / ``list_agendas``
  / ``delete_agenda`` — agenda CRUD; ``delete_agenda`` cascades its items
  transactionally (loose-ref convention; no DB foreign key would otherwise
  catch the orphan).
* ``upsert_item`` / ``get_item`` / ``list_items`` / ``delete_item`` — item CRUD;
  ``list_items`` supports an ``order_by`` switch (``"order"`` for the editor /
  agenda sidebar; ``"timecode"`` for the player chapter list — published
  agendas only, filtered upstream).
* ``set_status`` — toggles the agenda's ``draft``/``published`` status under
  ``set_status`` rather than as a generic patch, so the publish gate has its
  own enforceable surface.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from civiccast.agenda.models import (
    AgendaItem,
    AgendaItemDb,
    AgendaStatus,
    MeetingAgenda,
    MeetingAgendaDb,
)
from civiccast.recording.models import RecordingJobDb

SessionFactory = Callable[[], AbstractContextManager[Session]]


class AgendaStoreError(RuntimeError):
    """Base error for agenda persistence failures."""


class AgendaNotFoundError(AgendaStoreError):
    """Raised when an ``agenda_id`` does not resolve."""


class AgendaItemNotFoundError(AgendaStoreError):
    """Raised when an ``item_id`` does not resolve."""


class AgendaItemOrderConflictError(AgendaStoreError):
    """Raised when an item insert/update collides with the
    ``(agenda_id, order)`` unique constraint.

    The router translates this to a 409 with the offending coordinates so
    two operators racing in the editor see a controlled conflict, not a
    raw 500 (E-2 / Q-2 / T-1).
    """


class AgendaUniqueViolationError(AgendaStoreError):
    """Raised when an agenda insert/update collides with the
    ``(station_id, meeting_asset_id)`` unique constraint.

    The router translates this to a 409 so two operators racing to create
    an agenda for the same meeting see a controlled conflict (E-2 follow-up).
    """


class AgendaPublishEmptyError(AgendaStoreError):
    """Raised by :meth:`AgendaStore.publish_if_nonempty` when the agenda
    has zero items at the moment of the atomic flip (E-5).

    The service catches this and re-raises as ``AgendaPublishError`` so the
    router still sees a single failure type for the publish gate.
    """


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


ListItemsOrder = Literal["order", "timecode"]


class AgendaStore:
    """CRUD over the two S25 tables. Loose-ref convention; no DB FKs."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def _session(self) -> AbstractContextManager[Session]:
        return self._session_factory()

    # --- agendas --------------------------------------------------------

    def upsert_agenda(self, agenda: MeetingAgenda) -> MeetingAgenda:
        """Create or update an agenda (keyed by ``agenda_id``). Idempotent.

        Raises :class:`AgendaUniqueViolationError` if the write collides with
        the ``(station_id, meeting_asset_id)`` unique constraint (two
        operators racing to create an agenda for the same meeting).
        """
        with self._session() as session:
            row = session.get(MeetingAgendaDb, agenda.agenda_id)
            if row is None:
                row = MeetingAgendaDb(agenda_id=agenda.agenda_id, created_at=agenda.created_at)
                session.add(row)
            row.station_id = agenda.station_id
            row.meeting_asset_id = agenda.meeting_asset_id
            row.source_recording_job_id = agenda.source_recording_job_id
            row.source_doc_url = agenda.source_doc_url
            row.status = agenda.status
            row.updated_at = _now()
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AgendaUniqueViolationError(
                    f"An agenda already exists for "
                    f"(station_id={agenda.station_id!r}, meeting_asset_id={agenda.meeting_asset_id!r})."
                ) from exc
            return _agenda_to_model(row, session)

    def get_agenda(self, agenda_id: str) -> MeetingAgenda | None:
        with self._session() as session:
            row = session.get(MeetingAgendaDb, agenda_id)
            return _agenda_to_model(row, session) if row is not None else None

    def get_agenda_by_asset(self, station_id: str, meeting_asset_id: str) -> MeetingAgenda | None:
        """One-shot resolver for the public endpoint."""
        with self._session() as session:
            stmt = (
                select(MeetingAgendaDb)
                .outerjoin(
                    RecordingJobDb, MeetingAgendaDb.source_recording_job_id == RecordingJobDb.job_id
                )
                .where(MeetingAgendaDb.station_id == station_id)
                .where(
                    or_(
                        MeetingAgendaDb.meeting_asset_id == meeting_asset_id,
                        RecordingJobDb.asset_id == meeting_asset_id,
                    )
                )
            )
            row = session.execute(stmt).scalar_one_or_none()
            return _agenda_to_model(row, session) if row is not None else None

    def get_agenda_by_recording_job(self, station_id: str, job_id: str) -> MeetingAgenda | None:
        with self._session() as session:
            row = session.execute(
                select(MeetingAgendaDb).where(
                    MeetingAgendaDb.station_id == station_id,
                    MeetingAgendaDb.source_recording_job_id == job_id,
                )
            ).scalar_one_or_none()
            return _agenda_to_model(row, session) if row is not None else None

    def list_agendas(
        self,
        station_id: str,
        *,
        status: AgendaStatus | None = None,
    ) -> list[MeetingAgenda]:
        """All agendas for a station, optionally narrowed to one status."""
        with self._session() as session:
            stmt = (
                select(MeetingAgendaDb)
                .where(MeetingAgendaDb.station_id == station_id)
                .order_by(MeetingAgendaDb.meeting_asset_id, MeetingAgendaDb.agenda_id)
            )
            if status is not None:
                stmt = stmt.where(MeetingAgendaDb.status == status)
            return [_agenda_to_model(r, session) for r in session.execute(stmt).scalars().all()]

    def set_status(self, agenda_id: str, status: AgendaStatus) -> MeetingAgenda:
        """Toggle the publish gate. Dedicated surface so a generic patch
        cannot inadvertently flip publish status (DC-6)."""
        with self._session() as session:
            row = session.get(MeetingAgendaDb, agenda_id)
            if row is None:
                raise AgendaNotFoundError(f"Meeting agenda {agenda_id!r} not found.")
            row.status = status
            row.updated_at = _now()
            session.commit()
            return _agenda_to_model(row, session)

    def publish_if_nonempty(self, agenda_id: str) -> MeetingAgenda:
        """Atomic publish gate: items-exist check + status flip in ONE
        transaction.

        Closes the TOCTOU race where a concurrent ``delete_item`` between
        the service's read and write could publish an empty agenda
        (E-5). Raises :class:`AgendaNotFoundError` if the agenda is gone;
        raises :class:`AgendaPublishEmptyError` if the agenda has no items
        at the moment of the flip — the service catches and re-raises as
        ``AgendaPublishError`` for the router.
        """
        with self._session() as session:
            row = session.get(MeetingAgendaDb, agenda_id)
            if row is None:
                raise AgendaNotFoundError(f"Meeting agenda {agenda_id!r} not found.")
            # Existence-only probe (LIMIT 1) inside the same transaction as
            # the UPDATE. Reads a single row at most so it scales with the
            # agenda count, not the item count.
            first_item_id = session.execute(
                select(AgendaItemDb.item_id).where(AgendaItemDb.agenda_id == agenda_id).limit(1)
            ).scalar_one_or_none()
            if first_item_id is None:
                raise AgendaPublishEmptyError(
                    f"Cannot publish agenda {agenda_id!r}: it has zero items."
                )
            row.status = "published"
            row.updated_at = _now()
            session.commit()
            return _agenda_to_model(row, session)

    def delete_agenda(self, agenda_id: str) -> None:
        """Delete an agenda AND every item that referenced it.

        Loose-ref convention has no DB cascade — the store performs the
        cascade transactionally so an orphan item never survives.
        """
        with self._session() as session:
            row = session.get(MeetingAgendaDb, agenda_id)
            if row is None:
                raise AgendaNotFoundError(f"Meeting agenda {agenda_id!r} not found.")
            session.execute(delete(AgendaItemDb).where(AgendaItemDb.agenda_id == agenda_id))
            session.delete(row)
            session.commit()

    # --- items ----------------------------------------------------------

    def upsert_item(self, item: AgendaItem) -> AgendaItem:
        """Create or update an item (keyed by ``item_id``).

        Raises :class:`AgendaItemOrderConflictError` if the write collides
        with the ``(agenda_id, order)`` unique constraint — the router
        translates that into a 409 so two operators dragging items around
        see a controlled conflict, not a raw 500 (E-2 / Q-2 / T-1).
        """
        with self._session() as session:
            row = session.get(AgendaItemDb, item.item_id)
            if row is None:
                row = AgendaItemDb(item_id=item.item_id, created_at=item.created_at)
                session.add(row)
            row.agenda_id = item.agenda_id
            row.order = item.order
            row.number = item.number
            row.title = item.title
            row.video_timecode_s = item.video_timecode_s
            row.doc_anchor = item.doc_anchor
            row.notes = item.notes
            row.confidence = item.confidence
            row.updated_at = _now()
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AgendaItemOrderConflictError(
                    f"Another agenda item already occupies "
                    f"(agenda_id={item.agenda_id!r}, order={item.order})."
                ) from exc
            return _item_to_model(row)

    def get_item(self, item_id: str) -> AgendaItem | None:
        with self._session() as session:
            row = session.get(AgendaItemDb, item_id)
            return _item_to_model(row) if row is not None else None

    def list_items(
        self,
        agenda_id: str,
        *,
        order_by: ListItemsOrder = "order",
    ) -> list[AgendaItem]:
        """Items for an agenda. ``order_by="order"`` for the agenda sidebar,
        ``order_by="timecode"`` for the player chapter list (NULL timecodes
        last in both — the sidebar still shows them; the player skips them)."""
        with self._session() as session:
            stmt = select(AgendaItemDb).where(AgendaItemDb.agenda_id == agenda_id)
            if order_by == "timecode":
                # SQLite + Postgres both honor NULLS LAST when written
                # explicitly via the `is_(None)` trick — keeps the query
                # cross-dialect.
                stmt = stmt.order_by(
                    AgendaItemDb.video_timecode_s.is_(None),
                    AgendaItemDb.video_timecode_s,
                    AgendaItemDb.order,
                )
            else:
                stmt = stmt.order_by(AgendaItemDb.order, AgendaItemDb.item_id)
            return [_item_to_model(r) for r in session.execute(stmt).scalars().all()]

    def delete_item(self, item_id: str) -> None:
        with self._session() as session:
            row = session.get(AgendaItemDb, item_id)
            if row is None:
                raise AgendaItemNotFoundError(f"Agenda item {item_id!r} not found.")
            session.delete(row)
            session.commit()


# --- row → model converters --------------------------------------------------


def _agenda_to_model(row: MeetingAgendaDb, session: Session) -> MeetingAgenda:
    asset_id = row.meeting_asset_id
    if row.source_recording_job_id:
        job = session.get(RecordingJobDb, row.source_recording_job_id)
        if job is not None and job.station_id == row.station_id and job.asset_id:
            asset_id = job.asset_id
    return MeetingAgenda(
        agenda_id=row.agenda_id,
        station_id=row.station_id,
        meeting_asset_id=asset_id,
        source_recording_job_id=row.source_recording_job_id,
        source_doc_url=row.source_doc_url,
        status=row.status,  # type: ignore[arg-type]
        created_at=_as_utc(row.created_at),  # type: ignore[arg-type]
        updated_at=_as_utc(row.updated_at),  # type: ignore[arg-type]
    )


def _item_to_model(row: AgendaItemDb) -> AgendaItem:
    return AgendaItem(
        item_id=row.item_id,
        agenda_id=row.agenda_id,
        order=row.order,
        number=row.number,
        title=row.title,
        video_timecode_s=row.video_timecode_s,
        doc_anchor=row.doc_anchor,
        notes=row.notes,
        confidence=row.confidence,
        created_at=_as_utc(row.created_at),  # type: ignore[arg-type]
        updated_at=_as_utc(row.updated_at),  # type: ignore[arg-type]
    )


__all__ = [
    "AgendaItemNotFoundError",
    "AgendaItemOrderConflictError",
    "AgendaNotFoundError",
    "AgendaPublishEmptyError",
    "AgendaStore",
    "AgendaStoreError",
    "AgendaUniqueViolationError",
    "ListItemsOrder",
    "SessionFactory",
]
