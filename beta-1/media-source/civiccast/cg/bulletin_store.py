# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable community bulletin store (cable automation CA-3).

Bulletins were contract-only before CA-3 (`build_bulletin_queue` returned
deterministic mock data per request). The bulletin filler can only air REAL
community content if submissions persist, so this store backs the operator
CRUD surface and the per-channel approved rotation. State-transition
integrity (approval requires an operator id, decline/needs-changes require
notes) is enforced by :class:`~civiccast.cg.models.CgBulletinSubmission`'s
validators — the store persists what the model already validated.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import DateTime, String, Text, delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Mapped, Session, mapped_column

from civiccast.cg.models import CgBulletinSubmission
from civiccast.db import Base

# The store's own `list` method shadows the builtin inside the class
# body, so later annotations need this module-level alias.
BulletinList = list[CgBulletinSubmission]

SessionFactory = Callable[[], AbstractContextManager[Session]]

_AIRABLE_STATES = ("accepted", "scheduled")

__all__ = ["CgBulletinDb", "PostgresCgBulletinStore"]


class CgBulletinDb(Base):
    """Durable community bulletin row."""

    __tablename__ = "cg_bulletins"

    submission_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    organization: Mapped[str] = mapped_column(String(160), nullable=False)
    submitter_label: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    target_zone_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="submitted", server_default="submitted"
    )
    requested_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requested_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    moderation_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by_operator: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class PostgresCgBulletinStore:
    """SQLAlchemy-backed bulletin store (Postgres or managed SQLite)."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def create(self, channel_id: str, submission: CgBulletinSubmission) -> CgBulletinSubmission:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            session.add(
                CgBulletinDb(
                    submission_id=submission.submission_id,
                    channel_id=channel_id,
                    created_at=now,
                    updated_at=now,
                    **_submission_columns(submission),
                )
            )
            session.commit()
            return submission

    def get(self, submission_id: str) -> tuple[str, CgBulletinSubmission] | None:
        with self._session_factory() as session:
            row = session.get(CgBulletinDb, submission_id)
            if row is None:
                return None
            return row.channel_id, _row_to_submission(row)

    def update(self, channel_id: str, submission: CgBulletinSubmission) -> CgBulletinSubmission:
        with self._session_factory() as session:
            row = session.get(CgBulletinDb, submission.submission_id)
            if row is None:
                return self.create(channel_id, submission)
            row.channel_id = channel_id
            for key, value in _submission_columns(submission).items():
                setattr(row, key, value)
            row.updated_at = datetime.now(UTC)
            session.commit()
            return submission

    def list(
        self,
        *,
        channel_id: str,
        states: tuple[str, ...] | None = None,
    ) -> list[CgBulletinSubmission]:
        with self._session_factory() as session:
            stmt = (
                select(CgBulletinDb)
                .where(CgBulletinDb.channel_id == channel_id)
                .order_by(CgBulletinDb.created_at.asc(), CgBulletinDb.submission_id.asc())
            )
            if states is not None:
                stmt = stmt.where(CgBulletinDb.state.in_(states))
            rows = session.scalars(stmt).all()
            return [_row_to_submission(row) for row in rows]

    def list_approved(self, channel_id: str) -> BulletinList:
        """The on-air rotation: accepted + scheduled bulletins in creation order."""

        return self.list(channel_id=channel_id, states=_AIRABLE_STATES)

    def delete_expired(self, *, before: datetime, channel_id: str | None = None) -> int:
        """Delete bulletins whose air window ended before ``before`` (housekeeping).

        Only rows with a ``requested_end`` strictly before the cutoff are removed;
        open-ended bulletins (no ``requested_end``) are never purged. The filler's
        time-window filter already stops expired bulletins from airing — this just
        keeps the table from growing without bound. Returns the rows removed.
        """

        with self._session_factory() as session:
            stmt = delete(CgBulletinDb).where(
                CgBulletinDb.requested_end.is_not(None),
                CgBulletinDb.requested_end < before,
            )
            if channel_id is not None:
                stmt = stmt.where(CgBulletinDb.channel_id == channel_id)
            result = session.execute(stmt)
            session.commit()
            return int(cast(CursorResult[object], result).rowcount or 0)


def _submission_columns(submission: CgBulletinSubmission) -> dict[str, object]:
    return {
        "organization": submission.organization,
        "submitter_label": submission.submitter_label,
        "title": submission.title,
        "message": submission.message,
        "target_zone_kind": submission.target_zone_kind,
        "state": submission.state,
        "requested_start": submission.requested_start,
        "requested_end": submission.requested_end,
        "moderation_notes": submission.moderation_notes,
        "approved_by_operator": submission.approved_by_operator,
    }


def _row_to_submission(row: CgBulletinDb) -> CgBulletinSubmission:
    return CgBulletinSubmission(
        submission_id=row.submission_id,
        organization=row.organization,
        submitter_label=row.submitter_label,
        title=row.title,
        message=row.message,
        target_zone_kind=row.target_zone_kind,  # type: ignore[arg-type]
        state=row.state,  # type: ignore[arg-type]
        requested_start=_coerce(row.requested_start),
        requested_end=_coerce(row.requested_end),
        moderation_notes=row.moderation_notes,
        approved_by_operator=row.approved_by_operator,
    )


def _coerce(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
