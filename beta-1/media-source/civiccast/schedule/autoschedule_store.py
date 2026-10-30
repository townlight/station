# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""AutoScheduleStore — persistence for the S18 auto-scheduling entities.

CRUD for the three S18 slice-1 entities (saved searches, daypart blocks,
auto-schedule rules) backed by SQLAlchemy. Mirrors
:class:`civiccast.schedule.store.PostgresScheduleStore`'s posture exactly:

* construction takes a session-factory callable and does no I/O;
* every method opens one ``with self._session_factory() as session`` block;
* ``upsert_*`` does insert-or-update-in-place by primary key, preserving
  ``created_at`` and refreshing ``updated_at`` to the write instant (so a
  re-upsert can never move ``updated_at`` backwards);
* ``get_*`` returns the Pydantic model or ``None``;
* ``delete_*`` returns ``True`` if a row was removed, ``False`` if absent;
* ``list_*`` clamps its limit so a caller cannot request an unbounded scan.

Reference integrity (a rule's ``saved_search_id`` / ``schedule_block_id``
actually resolving) is the router/service layer's concern in a later slice —
this store does not enforce cross-entity existence, matching the no-FK
convention documented in :mod:`civiccast.schedule.autoschedule_models`.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from civiccast.schedule.autoschedule_models import (
    AutoScheduleRule,
    AutoScheduleRuleRow,
    SavedSearch,
    SavedSearchRow,
    ScheduleBlock,
    ScheduleBlockRow,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]

# A list call can never scan unbounded; mirrors PostgresScheduleStore.
_DEFAULT_LIST_LIMIT = 200
_MAX_LIST_LIMIT = 500


def _bounded(limit: int) -> int:
    return max(1, min(limit, _MAX_LIST_LIMIT))


class AutoScheduleStore:
    """SQLAlchemy-backed CRUD for saved searches, daypart blocks, and rules."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # SavedSearch
    # ------------------------------------------------------------------
    def upsert_saved_search(self, search: SavedSearch) -> SavedSearch:
        """Insert a new saved search or update an existing one by id.

        ``saved_search_id`` is immutable; on update the original ``created_at``
        is preserved and ``updated_at`` is stamped to now.
        """
        with self._session_factory() as session:
            now = datetime.now(UTC)
            existing = session.get(SavedSearchRow, search.saved_search_id)
            if existing is None:
                row = SavedSearchRow.from_search(search)
                row.updated_at = now
                session.add(row)
            else:
                existing.name = search.name
                existing.description = search.description
                existing.query_json = search.query.model_dump_json()
                existing.updated_at = now
                row = existing
            session.commit()
            session.refresh(row)
            return row.to_search()

    def get_saved_search(self, saved_search_id: str) -> SavedSearch | None:
        """Return one saved search by id, or None if absent."""
        with self._session_factory() as session:
            row = session.get(SavedSearchRow, saved_search_id)
            return row.to_search() if row is not None else None

    def list_saved_searches(self, *, limit: int = _DEFAULT_LIST_LIMIT) -> list[SavedSearch]:
        """Return saved searches ordered by name (case-sensitive ASC)."""
        with self._session_factory() as session:
            stmt = select(SavedSearchRow).order_by(SavedSearchRow.name.asc()).limit(_bounded(limit))
            return [row.to_search() for row in session.scalars(stmt)]

    def delete_saved_search(self, saved_search_id: str) -> bool:
        """Delete a saved search by id. True if a row was removed."""
        with self._session_factory() as session:
            row = session.get(SavedSearchRow, saved_search_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    # ------------------------------------------------------------------
    # ScheduleBlock (daypart window)
    # ------------------------------------------------------------------
    def upsert_schedule_block(self, block: ScheduleBlock) -> ScheduleBlock:
        """Insert a new daypart block or update an existing one by id."""
        with self._session_factory() as session:
            now = datetime.now(UTC)
            existing = session.get(ScheduleBlockRow, block.block_id)
            if existing is None:
                row = ScheduleBlockRow.from_block(block)
                row.updated_at = now
                session.add(row)
            else:
                fresh = ScheduleBlockRow.from_block(block)
                existing.channel_id = fresh.channel_id
                existing.name = fresh.name
                existing.start_minute = fresh.start_minute
                existing.end_minute = fresh.end_minute
                existing.days_of_week_json = fresh.days_of_week_json
                existing.active_from = fresh.active_from
                existing.active_until = fresh.active_until
                existing.enabled = fresh.enabled
                existing.updated_at = now
                row = existing
            session.commit()
            session.refresh(row)
            return row.to_block()

    def get_schedule_block(self, block_id: str) -> ScheduleBlock | None:
        """Return one daypart block by id, or None if absent."""
        with self._session_factory() as session:
            row = session.get(ScheduleBlockRow, block_id)
            return row.to_block() if row is not None else None

    def list_schedule_blocks(
        self,
        *,
        channel_id: str | None = None,
        enabled_only: bool = False,
        limit: int = _DEFAULT_LIST_LIMIT,
    ) -> list[ScheduleBlock]:
        """Return daypart blocks, optionally filtered by channel / enabled.

        Ordered by ``(channel_id, start_minute, block_id)`` for a stable,
        operator-readable daypart listing.
        """
        with self._session_factory() as session:
            stmt = select(ScheduleBlockRow)
            if channel_id is not None:
                stmt = stmt.where(ScheduleBlockRow.channel_id == channel_id)
            if enabled_only:
                stmt = stmt.where(ScheduleBlockRow.enabled.is_(True))
            stmt = stmt.order_by(
                ScheduleBlockRow.channel_id.asc(),
                ScheduleBlockRow.start_minute.asc(),
                ScheduleBlockRow.block_id.asc(),
            ).limit(_bounded(limit))
            return [row.to_block() for row in session.scalars(stmt)]

    def delete_schedule_block(self, block_id: str) -> bool:
        """Delete a daypart block by id. True if a row was removed."""
        with self._session_factory() as session:
            row = session.get(ScheduleBlockRow, block_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    # ------------------------------------------------------------------
    # AutoScheduleRule
    # ------------------------------------------------------------------
    def upsert_auto_schedule_rule(self, rule: AutoScheduleRule) -> AutoScheduleRule:
        """Insert a new auto-schedule rule or update an existing one by id."""
        with self._session_factory() as session:
            now = datetime.now(UTC)
            existing = session.get(AutoScheduleRuleRow, rule.rule_id)
            if existing is None:
                row = AutoScheduleRuleRow.from_rule(rule)
                row.updated_at = now
                session.add(row)
            else:
                fresh = AutoScheduleRuleRow.from_rule(rule)
                existing.name = fresh.name
                existing.saved_search_id = fresh.saved_search_id
                existing.channel_id = fresh.channel_id
                existing.schedule_block_id = fresh.schedule_block_id
                existing.pick_strategy = fresh.pick_strategy
                existing.rolling_window_days = fresh.rolling_window_days
                existing.repeat_prevention_days = fresh.repeat_prevention_days
                existing.priority = fresh.priority
                existing.enabled = fresh.enabled
                existing.last_materialized_at = fresh.last_materialized_at
                existing.updated_at = now
                row = existing
            session.commit()
            session.refresh(row)
            return row.to_rule()

    def get_auto_schedule_rule(self, rule_id: str) -> AutoScheduleRule | None:
        """Return one auto-schedule rule by id, or None if absent."""
        with self._session_factory() as session:
            row = session.get(AutoScheduleRuleRow, rule_id)
            return row.to_rule() if row is not None else None

    def list_auto_schedule_rules(
        self,
        *,
        channel_id: str | None = None,
        enabled_only: bool = False,
        limit: int = _DEFAULT_LIST_LIMIT,
    ) -> list[AutoScheduleRule]:
        """Return auto-schedule rules, optionally filtered by channel / enabled.

        Ordered by ``(channel_id, priority, rule_id)`` — the order the
        materializer applies overlapping rules (lower priority wins first).
        """
        with self._session_factory() as session:
            stmt = select(AutoScheduleRuleRow)
            if channel_id is not None:
                stmt = stmt.where(AutoScheduleRuleRow.channel_id == channel_id)
            if enabled_only:
                stmt = stmt.where(AutoScheduleRuleRow.enabled.is_(True))
            stmt = stmt.order_by(
                AutoScheduleRuleRow.channel_id.asc(),
                AutoScheduleRuleRow.priority.asc(),
                AutoScheduleRuleRow.rule_id.asc(),
            ).limit(_bounded(limit))
            return [row.to_rule() for row in session.scalars(stmt)]

    def delete_auto_schedule_rule(self, rule_id: str) -> bool:
        """Delete an auto-schedule rule by id. True if a row was removed."""
        with self._session_factory() as session:
            row = session.get(AutoScheduleRuleRow, rule_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True
