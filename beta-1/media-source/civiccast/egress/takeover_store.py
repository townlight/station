# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Persistence for live-takeover audit sessions (CivicCast 3.0 — S5).

A durable append-and-close log of manual live-takeovers. One row per takeover;
the *active* session on a channel is the row whose ``returned_at`` is NULL.
Mirrors the schedule/egress stores' session-factory posture (no I/O at import).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from civiccast.egress.models import TakeoverAuditRecordDb, TakeoverSession

SessionFactory = Callable[[], AbstractContextManager[Session]]


class PostgresTakeoverAuditStore:
    """SQLAlchemy-backed store for ``civiccast.takeover_audit``."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def append(self, session: TakeoverSession) -> TakeoverSession:
        """Persist a new takeover session (``returned_at`` typically NULL)."""
        with self._session_factory() as db:
            row = TakeoverAuditRecordDb.from_session(session)
            db.add(row)
            db.commit()
            db.refresh(row)
            return row.to_session()

    def close(
        self,
        session_id: str,
        *,
        returned_at: datetime,
        notes: str | None = None,
    ) -> TakeoverSession | None:
        """Record the handback: set ``returned_at`` (+ optional notes). Returns
        the updated session, or None if the session id is unknown."""
        with self._session_factory() as db:
            row = db.get(TakeoverAuditRecordDb, session_id)
            if row is None:
                return None
            row.returned_at = returned_at.astimezone(UTC)
            if notes is not None:
                row.notes = notes
            db.commit()
            db.refresh(row)
            return row.to_session()

    def get_active(self, channel_id: str) -> TakeoverSession | None:
        """Return the channel's open takeover (``returned_at IS NULL``), if any.

        Defensive against more than one open row (should not happen): returns
        the most recently started.
        """
        with self._session_factory() as db:
            row = (
                db.execute(
                    select(TakeoverAuditRecordDb)
                    .where(
                        TakeoverAuditRecordDb.channel_id == channel_id,
                        TakeoverAuditRecordDb.returned_at.is_(None),
                    )
                    .order_by(TakeoverAuditRecordDb.took_over_at.desc())
                )
                .scalars()
                .first()
            )
            return row.to_session() if row is not None else None

    def list_by_channel(self, channel_id: str, *, limit: int = 50) -> list[TakeoverSession]:
        """Return a channel's takeover sessions, most recent first.

        ``limit`` is clamped to ``[1, 500]`` so a caller cannot request an
        unbounded scan.
        """
        bounded = max(1, min(limit, 500))
        with self._session_factory() as db:
            rows = (
                db.execute(
                    select(TakeoverAuditRecordDb)
                    .where(TakeoverAuditRecordDb.channel_id == channel_id)
                    .order_by(TakeoverAuditRecordDb.took_over_at.desc())
                    .limit(bounded)
                )
                .scalars()
                .all()
            )
            return [row.to_session() for row in rows]
