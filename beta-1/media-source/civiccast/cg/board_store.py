# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable store for the CG bulletin-board designer (S6 V1 — build step 7).

One cohesive store over the board-designer entities (board, zones, feed
sources, append-only audit, feed-item approvals), mirroring
:class:`~civiccast.schedule.autoschedule_store.AutoScheduleStore` — a single DI
seam for the whole authoring layer. Domain integrity (a feed zone names a feed,
weather feeds are curated, …) is enforced by the
:mod:`~civiccast.cg.board_models` validators; the store persists what the model
already validated.

Invariant: at most one *active* board per channel. ``upsert_board`` with
``active=True`` deactivates the channel's other boards in the same transaction
so :meth:`get_active_board` is unambiguous.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from civiccast.cg.board_models import (
    CgBoard,
    CgBoardAuditDb,
    CgBoardAuditEvent,
    CgBoardDb,
    CgFeedItemApproval,
    CgFeedItemApprovalDb,
    CgFeedSource,
    CgFeedSourceDb,
    CgZoneConfig,
    CgZoneConfigDb,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]

__all__ = ["CgBoardStore"]


class CgBoardStore:
    """SQLAlchemy-backed board-designer store (Postgres or managed SQLite)."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    # -- Board -------------------------------------------------------------

    def upsert_board(self, board: CgBoard) -> CgBoard:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            if board.active:
                # Enforce one-active-board-per-channel before this row lands.
                session.execute(
                    update(CgBoardDb)
                    .where(
                        CgBoardDb.channel_id == board.channel_id,
                        CgBoardDb.board_id != board.board_id,
                        CgBoardDb.active.is_(True),
                    )
                    .values(active=False, updated_at=now)
                )
            row = session.get(CgBoardDb, board.board_id)
            if row is None:
                session.add(
                    CgBoardDb(
                        board_id=board.board_id,
                        channel_id=board.channel_id,
                        template_id=board.template_id,
                        active=board.active,
                        created_by=board.created_by,
                        created_at=board.created_at,
                        updated_at=now,
                    )
                )
                created_at = board.created_at
            else:
                row.channel_id = board.channel_id
                row.template_id = board.template_id
                row.active = board.active
                row.updated_at = now
                created_at = _coerce_required(row.created_at)
            session.commit()
            return board.model_copy(update={"created_at": created_at, "updated_at": now})

    def get_board(self, board_id: str) -> CgBoard | None:
        with self._session_factory() as session:
            row = session.get(CgBoardDb, board_id)
            return None if row is None else _board_from_row(row)

    def get_active_board(self, channel_id: str) -> CgBoard | None:
        with self._session_factory() as session:
            stmt = (
                select(CgBoardDb)
                .where(CgBoardDb.channel_id == channel_id, CgBoardDb.active.is_(True))
                .order_by(CgBoardDb.updated_at.desc(), CgBoardDb.board_id.asc())
                .limit(1)
            )
            row = session.scalars(stmt).first()
            return None if row is None else _board_from_row(row)

    # -- Zones -------------------------------------------------------------

    def upsert_zone(self, zone: CgZoneConfig) -> CgZoneConfig:
        with self._session_factory() as session:
            row = session.get(CgZoneConfigDb, zone.zone_id)
            if row is None:
                session.add(
                    CgZoneConfigDb(
                        zone_id=zone.zone_id,
                        created_at=zone.created_at,
                        **_zone_columns(zone),
                    )
                )
                created_at = zone.created_at
            else:
                for key, value in _zone_columns(zone).items():
                    setattr(row, key, value)
                created_at = _coerce_required(row.created_at)
            session.commit()
            return zone.model_copy(update={"created_at": created_at})

    def get_zone(self, zone_id: str) -> CgZoneConfig | None:
        with self._session_factory() as session:
            row = session.get(CgZoneConfigDb, zone_id)
            return None if row is None else _zone_from_row(row)

    def list_zones(self, board_id: str) -> list[CgZoneConfig]:
        with self._session_factory() as session:
            stmt = (
                select(CgZoneConfigDb)
                .where(CgZoneConfigDb.board_id == board_id)
                .order_by(CgZoneConfigDb.created_at.asc(), CgZoneConfigDb.zone_id.asc())
            )
            return [_zone_from_row(row) for row in session.scalars(stmt).all()]

    def delete_zone(self, zone_id: str) -> bool:
        with self._session_factory() as session:
            row = session.get(CgZoneConfigDb, zone_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    # -- Feed sources ------------------------------------------------------

    def upsert_feed(self, feed: CgFeedSource) -> CgFeedSource:
        with self._session_factory() as session:
            row = session.get(CgFeedSourceDb, feed.feed_source_id)
            if row is None:
                session.add(
                    CgFeedSourceDb(
                        feed_source_id=feed.feed_source_id,
                        created_at=feed.created_at,
                        **_feed_columns(feed),
                    )
                )
                created_at = feed.created_at
            else:
                for key, value in _feed_columns(feed).items():
                    setattr(row, key, value)
                created_at = _coerce_required(row.created_at)
            session.commit()
            return feed.model_copy(update={"created_at": created_at})

    def get_feed(self, feed_source_id: str) -> CgFeedSource | None:
        with self._session_factory() as session:
            row = session.get(CgFeedSourceDb, feed_source_id)
            return None if row is None else _feed_from_row(row)

    def list_feeds(self, channel_id: str, *, enabled_only: bool = False) -> list[CgFeedSource]:
        with self._session_factory() as session:
            stmt = (
                select(CgFeedSourceDb)
                .where(CgFeedSourceDb.channel_id == channel_id)
                .order_by(CgFeedSourceDb.created_at.asc(), CgFeedSourceDb.feed_source_id.asc())
            )
            if enabled_only:
                stmt = stmt.where(CgFeedSourceDb.enabled.is_(True))
            return [_feed_from_row(row) for row in session.scalars(stmt).all()]

    def delete_feed(self, feed_source_id: str) -> bool:
        with self._session_factory() as session:
            row = session.get(CgFeedSourceDb, feed_source_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    def mark_feed_fetch(
        self, feed_source_id: str, *, fetched_at: datetime, error: str | None
    ) -> None:
        """Record a feed-fetch outcome (success clears the prior error)."""

        with self._session_factory() as session:
            row = session.get(CgFeedSourceDb, feed_source_id)
            if row is None:
                return
            row.last_fetched_at = fetched_at
            row.last_fetch_error = error
            session.commit()

    # -- Audit -------------------------------------------------------------

    def append_audit(self, event: CgBoardAuditEvent) -> CgBoardAuditEvent:
        with self._session_factory() as session:
            session.add(
                CgBoardAuditDb(
                    audit_id=event.audit_id,
                    board_id=event.board_id,
                    channel_id=event.channel_id,
                    event_kind=event.event_kind,
                    operator_id=event.operator_id,
                    occurred_at=event.occurred_at,
                    details_json=json.dumps(event.details, sort_keys=True),
                )
            )
            session.commit()
            return event

    def list_audit(
        self, *, board_id: str, limit: int = 50, offset: int = 0
    ) -> list[CgBoardAuditEvent]:
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        with self._session_factory() as session:
            stmt = (
                select(CgBoardAuditDb)
                .where(CgBoardAuditDb.board_id == board_id)
                .order_by(CgBoardAuditDb.occurred_at.desc(), CgBoardAuditDb.audit_id.desc())
                .limit(limit)
                .offset(offset)
            )
            return [_audit_from_row(row) for row in session.scalars(stmt).all()]

    # -- Feed-item approvals ----------------------------------------------

    def _existing_approval(
        self, session: Session, approval: CgFeedItemApproval
    ) -> CgFeedItemApprovalDb | None:
        return session.scalars(
            select(CgFeedItemApprovalDb).where(
                CgFeedItemApprovalDb.channel_id == approval.channel_id,
                CgFeedItemApprovalDb.feed_source_id == approval.feed_source_id,
                CgFeedItemApprovalDb.item_id == approval.item_id,
            )
        ).first()

    def approve_item(self, approval: CgFeedItemApproval) -> CgFeedItemApproval:
        with self._session_factory() as session:
            existing = self._existing_approval(session, approval)
            if existing is not None:
                # Idempotent: re-approving an item is a no-op, return the stored row.
                return _approval_from_row(existing)
            session.add(
                CgFeedItemApprovalDb(
                    approval_id=approval.approval_id,
                    channel_id=approval.channel_id,
                    feed_source_id=approval.feed_source_id,
                    item_id=approval.item_id,
                    approved_by_operator=approval.approved_by_operator,
                    approved_at=approval.approved_at,
                )
            )
            try:
                session.commit()
            except IntegrityError:
                # A concurrent approval won the race on the
                # (channel_id, feed_source_id, item_id) UNIQUE constraint. Roll
                # back and return the row that landed — approval is idempotent,
                # so the loser observes the same result rather than a 500.
                session.rollback()
                winner = self._existing_approval(session, approval)
                if winner is not None:
                    return _approval_from_row(winner)
                raise
            return approval

    def list_approved_item_ids(self, *, channel_id: str, feed_source_id: str) -> set[str]:
        with self._session_factory() as session:
            stmt = select(CgFeedItemApprovalDb.item_id).where(
                CgFeedItemApprovalDb.channel_id == channel_id,
                CgFeedItemApprovalDb.feed_source_id == feed_source_id,
            )
            return set(session.scalars(stmt).all())

    def revoke_item(self, approval_id: str) -> bool:
        with self._session_factory() as session:
            row = session.get(CgFeedItemApprovalDb, approval_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True


# ---------------------------------------------------------------------------
# Row <-> model helpers
# ---------------------------------------------------------------------------


def _coerce(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _coerce_required(value: datetime | None) -> datetime:
    return _coerce(value) or datetime.now(UTC)


def _board_from_row(row: CgBoardDb) -> CgBoard:
    return CgBoard(
        board_id=row.board_id,
        channel_id=row.channel_id,
        template_id=row.template_id,
        active=row.active,
        created_by=row.created_by,
        created_at=_coerce(row.created_at),  # type: ignore[arg-type]
        updated_at=_coerce(row.updated_at),  # type: ignore[arg-type]
    )


def _zone_columns(zone: CgZoneConfig) -> dict[str, object]:
    return {
        "board_id": zone.board_id,
        "region": zone.region,
        "zone_kind": zone.zone_kind,
        "content_source": zone.content_source,
        "feed_source_id": zone.feed_source_id,
        "refresh_seconds": zone.refresh_seconds,
        "approval_required": zone.approval_required,
        "manual_text": zone.manual_text,
        "image_asset_ref": zone.image_asset_ref,
        "live_source_id": zone.live_source_id,
        "allowed_tags": list(zone.allowed_tags),
    }


def _zone_from_row(row: CgZoneConfigDb) -> CgZoneConfig:
    return CgZoneConfig(
        zone_id=row.zone_id,
        board_id=row.board_id,
        region=row.region,  # type: ignore[arg-type]
        zone_kind=row.zone_kind,  # type: ignore[arg-type]
        content_source=row.content_source,  # type: ignore[arg-type]
        feed_source_id=row.feed_source_id,
        refresh_seconds=row.refresh_seconds,
        approval_required=row.approval_required,
        manual_text=row.manual_text,
        image_asset_ref=row.image_asset_ref,
        live_source_id=row.live_source_id,
        allowed_tags=list(row.allowed_tags or []),
        created_at=_coerce(row.created_at),  # type: ignore[arg-type]
    )


def _feed_columns(feed: CgFeedSource) -> dict[str, object]:
    return {
        "channel_id": feed.channel_id,
        "kind": feed.kind,
        "label": feed.label,
        "source_url": feed.source_url,
        "trust_tier": feed.trust_tier,
        "refresh_seconds": feed.refresh_seconds,
        "enabled": feed.enabled,
        "tags": list(feed.tags),
        "created_by": feed.created_by,
        "last_fetched_at": feed.last_fetched_at,
        "last_fetch_error": feed.last_fetch_error,
    }


def _feed_from_row(row: CgFeedSourceDb) -> CgFeedSource:
    return CgFeedSource(
        feed_source_id=row.feed_source_id,
        channel_id=row.channel_id,
        kind=row.kind,  # type: ignore[arg-type]
        label=row.label,
        source_url=row.source_url,
        trust_tier=row.trust_tier,  # type: ignore[arg-type]
        refresh_seconds=row.refresh_seconds,
        enabled=row.enabled,
        tags=list(row.tags or []),
        created_by=row.created_by,
        created_at=_coerce(row.created_at),  # type: ignore[arg-type]
        last_fetched_at=_coerce(row.last_fetched_at),
        last_fetch_error=row.last_fetch_error,
    )


def _audit_from_row(row: CgBoardAuditDb) -> CgBoardAuditEvent:
    try:
        details = json.loads(row.details_json) if row.details_json else {}
    except (ValueError, TypeError):
        details = {}
    if not isinstance(details, dict):
        details = {}
    return CgBoardAuditEvent(
        audit_id=row.audit_id,
        board_id=row.board_id,
        channel_id=row.channel_id,
        event_kind=row.event_kind,
        operator_id=row.operator_id,
        occurred_at=_coerce(row.occurred_at),  # type: ignore[arg-type]
        details=details,
    )


def _approval_from_row(row: CgFeedItemApprovalDb) -> CgFeedItemApproval:
    return CgFeedItemApproval(
        approval_id=row.approval_id,
        channel_id=row.channel_id,
        feed_source_id=row.feed_source_id,
        item_id=row.item_id,
        approved_by_operator=row.approved_by_operator,
        approved_at=_coerce(row.approved_at),  # type: ignore[arg-type]
    )
