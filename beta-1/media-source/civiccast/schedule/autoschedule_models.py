# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Query-driven auto-scheduling models (CivicCast 3.0 — S18 gaps 1 & 4).

PEG automation coverage gap 1 (query-driven auto-scheduling) and gap 4 (block /
daypart scheduling), specified in ``docs/spec/3.0/sections/
S18 comparative appendix §5. These entities replace the program
log's static ``asset_id``-per-slot binding with *rules* that resolve a saved
asset query into ``schedule_items`` at materialization time, within operator-
defined daypart windows. The rules are a planning layer only: every item they
produce still flows through the S4 Commit-to-Air gate before it can air.

This module is the **data layer (S18 slice 1)** — Pydantic contracts + their
SQLAlchemy row peers + round-trip. The *executable* pieces land later:

* the asset-query → SQLAlchemy translation + pick strategies — slice 2;
* the daypart/block constraint evaluation + repeat-prevention — slice 3;
* the rolling-window materializer that compiles rules into ``schedule_items``
  — a later slice.

Three entities, all backed by migration ``0043_scheduling_automation``:

* :class:`SavedSearch` — a named, declarative query over asset metadata
  (table ``saved_searches``). Carries an :class:`AssetQuery`.
* :class:`ScheduleBlock` — a daypart window on a channel (table
  ``schedule_blocks``): a time-of-day range on selected weekdays, optionally
  bounded by calendar dates. This is gap 4.
* :class:`AutoScheduleRule` — binds a saved search to a daypart block on a
  channel with a pick strategy, rolling window, and repeat-prevention window
  (table ``auto_schedule_rules``). This is gap 1's compile rule.

**Reference integrity is application-layer, not a DB foreign key** — matching
the schedule module's existing convention (``ScheduleItem.asset_id`` and the
``CommitToAirReport`` reference columns carry no FK either; existence is checked
in the store). ``AutoScheduleRule.saved_search_id`` / ``schedule_block_id`` are
soft string references resolved by the store, so deleting a rule never cascades
into the search/block it named and vice-versa.

Vocabulary mirrors :mod:`civiccast.schedule.commit_models`: the SA row is the
persistence object (``...Row``), the Pydantic peer is the serialization view,
and the round-trip lives on the row (``from_*`` / ``to_*``).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base
from civiccast.schedule.models import AssetStateValue

# ---------------------------------------------------------------------------
# Pick strategy — how a rule chooses ONE asset from its saved-search results
# ---------------------------------------------------------------------------
# top_result    — the first row in the saved search's own ordering
# random_result — a uniformly-random eligible row (variety across compiles)
# newest        — the most recently published eligible row
# The executor for these lands in slice 2; slice 1 stores + validates the value.
PickStrategyValue = Literal["top_result", "random_result", "newest"]
PICK_TOP_RESULT: PickStrategyValue = "top_result"
PICK_RANDOM_RESULT: PickStrategyValue = "random_result"
PICK_NEWEST: PickStrategyValue = "newest"
_PICK_STRATEGIES = (PICK_TOP_RESULT, PICK_RANDOM_RESULT, PICK_NEWEST)

# Rolling-window horizon bounds (spec §5 gap 1: "rolling-window days 14-60").
ROLLING_WINDOW_MIN_DAYS = 14
ROLLING_WINDOW_MAX_DAYS = 60

# Daypart minute-of-day bounds. start ∈ [0, 1440), end ∈ (0, 1440]; a block
# whose end <= start denotes a window that wraps past midnight (interpretation
# is the materializer's job in a later slice — slice 1 only stores it).
MINUTES_PER_DAY = 24 * 60

AssetQueryOrderBy = Literal["published_at", "title", "duration_seconds"]

# Custom-field predicate operators (S22 / S19 wiring). ``eq`` matches the canonical
# string ``value``; ``num_range`` matches the denormalized ``value_num``; ``date_range``
# matches the denormalized ``value_date``. The set mirrors the metadata store's
# ``PredicateOp`` so a saved-search predicate maps 1:1 onto a store predicate.
CustomFieldPredicateOp = Literal["eq", "num_range", "date_range"]


def _as_utc(value: datetime | None) -> datetime | None:
    """Re-attach UTC to a naive datetime (SQLite drops tzinfo on round-trip).

    Mirrors :func:`civiccast.schedule.commit_models._as_utc`: Postgres keeps
    tzinfo, SQLite's ``DateTime(timezone=True)`` returns naive values, so the
    round-trip would otherwise differ by backend. The persistence contract is
    "all timestamps are UTC".
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


# ---------------------------------------------------------------------------
# CustomFieldPredicate — a saved-search clause over a user-defined field (S22)
# ---------------------------------------------------------------------------


class CustomFieldPredicate(BaseModel):
    """One saved-search clause that filters on a custom metadata field (S22).

    The predicate stores the field's **immutable machine ``key``** (not its surrogate
    ``field_id`` or its editable ``label``) so a saved search keeps resolving after an
    operator renames a field's label (spec §6: ``key`` is the stable handle). The slice-5
    executor resolves ``key`` -> ``field_id`` in SQL, joining ``custom_field_defs`` and
    gating on ``searchable`` so a non-searchable field can never be filtered on (spec §3).

    * ``eq``         — exact match on the canonical ``value`` (the ``(field_id, value)`` index).
    * ``num_range``  — ``value_num`` BETWEEN [num_min, num_max] (either bound optional).
    * ``date_range`` — ``value_date`` BETWEEN [date_min, date_max] (either bound optional).

    Predicates compose as AND across an ``AssetQuery.custom_fields`` list, the same way the
    core filters compose. ``extra="forbid"`` keeps a stored predicate from carrying keys the
    executor would ignore, mirroring the :class:`AssetQuery` invariant.
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., min_length=1, max_length=120)
    op: CustomFieldPredicateOp = "eq"
    value: str | None = Field(default=None, max_length=1000)
    num_min: float | None = None
    num_max: float | None = None
    date_min: date | None = None
    date_max: date | None = None

    @model_validator(mode="after")
    def _check(self) -> CustomFieldPredicate:
        if self.num_min is not None and self.num_max is not None and self.num_max < self.num_min:
            raise ValueError("num_max must be >= num_min")
        if (
            self.date_min is not None
            and self.date_max is not None
            and self.date_max < self.date_min
        ):
            raise ValueError("date_max must be on or after date_min")
        return self


# ---------------------------------------------------------------------------
# AssetQuery — the declarative filter a SavedSearch stores (gap 1)
# ---------------------------------------------------------------------------


class AssetQuery(BaseModel):
    """A declarative filter over asset metadata, stored inside a SavedSearch.

    Core fields map to a real column on :class:`civiccast.schedule.models.Asset` so the
    slice-2 executor can translate them to SQLAlchemy without a schema change.
    ``custom_fields`` (S22 / S19) carry predicates over user-defined metadata, resolved by
    the executor against ``custom_field_values``. ``extra="forbid"`` keeps a stored query
    from silently carrying keys the executor will ignore. An empty query (all defaults, no
    filters and no custom-field predicates) matches every asset, ordered newest-published
    first.
    """

    model_config = ConfigDict(extra="forbid")

    # Exact match on Asset.meeting_body (e.g. "City Council").
    meeting_body: str | None = Field(default=None, min_length=1, max_length=120)
    # Case-insensitive substring of Asset.title (executor decides collation).
    title_contains: str | None = Field(default=None, min_length=1, max_length=200)
    # Asset lifecycle states to include; empty list = any state.
    states: list[AssetStateValue] = Field(default_factory=list)
    # Inclusive duration bounds (seconds) against Asset.duration_seconds.
    min_duration_seconds: int | None = Field(default=None, ge=0)
    max_duration_seconds: int | None = Field(default=None, ge=0)
    # Half-open published-at window [after, before) against Asset.published_at.
    published_after: datetime | None = None
    published_before: datetime | None = None
    # S22/S19: custom-field predicates (AND-composed). Empty = no custom-field constraint.
    custom_fields: list[CustomFieldPredicate] = Field(default_factory=list)
    # Result ordering — the meaning of "top_result" / "newest" pick strategies.
    order_by: AssetQueryOrderBy = "published_at"
    order_desc: bool = True

    @model_validator(mode="after")
    def _check_ranges(self) -> AssetQuery:
        if (
            self.min_duration_seconds is not None
            and self.max_duration_seconds is not None
            and self.max_duration_seconds < self.min_duration_seconds
        ):
            raise ValueError("max_duration_seconds must be >= min_duration_seconds")
        if (
            self.published_after is not None
            and self.published_before is not None
            and self.published_before <= self.published_after
        ):
            raise ValueError("published_before must be after published_after")
        return self


# ---------------------------------------------------------------------------
# SavedSearch — a named asset query (table saved_searches)
# ---------------------------------------------------------------------------


class SavedSearch(BaseModel):
    """Pydantic view of a persisted named asset query. SA peer: SavedSearchRow."""

    saved_search_id: str = Field(..., min_length=1, description='"ss_" + url-safe token')
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    query: AssetQuery = Field(default_factory=AssetQuery)
    created_at: datetime
    updated_at: datetime


class SavedSearchRow(Base):
    """SQLAlchemy row for ``saved_searches`` (migration 0043).

    The query is stored as a JSON document (``query_json``) the same way the
    asset chapter list is (``Asset.chapters_json``) — a Text column holding the
    :class:`AssetQuery` serialization, not a column-per-field schema, so the
    query vocabulary can grow in slice 2 without a migration.
    """

    __tablename__ = "saved_searches"

    saved_search_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    query_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    @classmethod
    def from_search(cls, search: SavedSearch) -> SavedSearchRow:
        return cls(
            saved_search_id=search.saved_search_id,
            name=search.name,
            description=search.description,
            query_json=search.query.model_dump_json(),
            created_at=search.created_at.astimezone(UTC),
            updated_at=search.updated_at.astimezone(UTC),
        )

    def to_search(self) -> SavedSearch:
        return SavedSearch(
            saved_search_id=self.saved_search_id,
            name=self.name,
            description=self.description,
            query=AssetQuery.model_validate_json(self.query_json),
            created_at=_as_utc(self.created_at),  # type: ignore[arg-type]
            updated_at=_as_utc(self.updated_at),  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# ScheduleBlock — a daypart window on a channel (gap 4, table schedule_blocks)
# ---------------------------------------------------------------------------


class ScheduleBlock(BaseModel):
    """Pydantic view of a daypart/block window. SA peer: ScheduleBlockRow.

    A daypart is a time-of-day range from ``start_minute`` to ``end_minute``
    (minutes from local midnight) recurring on selected weekdays
    (``days_of_week``, Monday=0 to Sunday=6), optionally bounded by calendar dates
    (``active_from`` / ``active_until``, inclusive). An auto-schedule rule fills
    one block.
    """

    block_id: str = Field(..., min_length=1, description='"sb_" + url-safe token')
    channel_id: str = Field(..., min_length=1, max_length=80)
    name: str = Field(..., min_length=1, max_length=200)
    start_minute: int = Field(..., ge=0, lt=MINUTES_PER_DAY)
    end_minute: int = Field(..., gt=0, le=MINUTES_PER_DAY)
    days_of_week: list[int] = Field(default_factory=list)
    active_from: date | None = None
    active_until: date | None = None
    enabled: bool = True
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _check(self) -> ScheduleBlock:
        if not self.days_of_week:
            raise ValueError("days_of_week must list at least one weekday (0-6)")
        if any(d < 0 or d > 6 for d in self.days_of_week):
            raise ValueError("days_of_week entries must be 0 (Mon) .. 6 (Sun)")
        # Normalize to sorted-unique so the stored form is canonical.
        self.days_of_week = sorted(set(self.days_of_week))
        if (
            self.active_from is not None
            and self.active_until is not None
            and self.active_until < self.active_from
        ):
            raise ValueError("active_until must be on or after active_from")
        return self


class ScheduleBlockRow(Base):
    """SQLAlchemy row for ``schedule_blocks`` (migration 0043)."""

    __tablename__ = "schedule_blocks"
    __table_args__ = (
        CheckConstraint(
            "start_minute >= 0 AND start_minute < 1440",
            name="schedule_blocks_start_minute_check",
        ),
        CheckConstraint(
            "end_minute > 0 AND end_minute <= 1440",
            name="schedule_blocks_end_minute_check",
        ),
        # The materializer lists a channel's blocks; enabled filters the rest.
        Index("schedule_blocks_channel_enabled_idx", "channel_id", "enabled"),
    )

    block_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    end_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    days_of_week_json: Mapped[str] = mapped_column(Text, nullable=False)
    active_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    active_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    @classmethod
    def from_block(cls, block: ScheduleBlock) -> ScheduleBlockRow:
        return cls(
            block_id=block.block_id,
            channel_id=block.channel_id,
            name=block.name,
            start_minute=block.start_minute,
            end_minute=block.end_minute,
            days_of_week_json=json.dumps(sorted(set(block.days_of_week))),
            active_from=block.active_from,
            active_until=block.active_until,
            enabled=block.enabled,
            created_at=block.created_at.astimezone(UTC),
            updated_at=block.updated_at.astimezone(UTC),
        )

    def to_block(self) -> ScheduleBlock:
        return ScheduleBlock(
            block_id=self.block_id,
            channel_id=self.channel_id,
            name=self.name,
            start_minute=self.start_minute,
            end_minute=self.end_minute,
            days_of_week=json.loads(self.days_of_week_json),
            active_from=self.active_from,
            active_until=self.active_until,
            enabled=self.enabled,
            created_at=_as_utc(self.created_at),  # type: ignore[arg-type]
            updated_at=_as_utc(self.updated_at),  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# AutoScheduleRule — bind a saved search to a daypart block (gap 1)
# ---------------------------------------------------------------------------


class AutoScheduleRule(BaseModel):
    """Pydantic view of an auto-schedule rule. SA peer: AutoScheduleRuleRow.

    Binds a :class:`SavedSearch` to a :class:`ScheduleBlock` on a channel: the
    materializer fills the block by picking one asset from the search per slot
    using ``pick_strategy``, looking ``rolling_window_days`` ahead and avoiding
    re-airing the same asset within ``repeat_prevention_days`` (0 = no repeat
    guard). ``priority`` orders rules whose blocks overlap (lower wins).
    ``last_materialized_at`` is stamped by the materializer; null until first
    compile.
    """

    rule_id: str = Field(..., min_length=1, description='"asr_" + url-safe token')
    name: str = Field(..., min_length=1, max_length=200)
    saved_search_id: str = Field(..., min_length=1, max_length=64)
    channel_id: str = Field(..., min_length=1, max_length=80)
    schedule_block_id: str = Field(..., min_length=1, max_length=64)
    pick_strategy: PickStrategyValue = PICK_NEWEST
    rolling_window_days: int = Field(
        default=30, ge=ROLLING_WINDOW_MIN_DAYS, le=ROLLING_WINDOW_MAX_DAYS
    )
    repeat_prevention_days: int = Field(default=0, ge=0)
    priority: int = Field(default=100, ge=0)
    enabled: bool = True
    last_materialized_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AutoScheduleRuleRow(Base):
    """SQLAlchemy row for ``auto_schedule_rules`` (migration 0043)."""

    __tablename__ = "auto_schedule_rules"
    __table_args__ = (
        CheckConstraint(
            "pick_strategy IN ('top_result', 'random_result', 'newest')",
            name="auto_schedule_rules_pick_strategy_check",
        ),
        CheckConstraint(
            "rolling_window_days BETWEEN 14 AND 60",
            name="auto_schedule_rules_rolling_window_check",
        ),
        CheckConstraint(
            "repeat_prevention_days >= 0",
            name="auto_schedule_rules_repeat_prevention_check",
        ),
        # The materializer lists active rules per channel, priority-ordered.
        Index(
            "auto_schedule_rules_channel_enabled_idx",
            "channel_id",
            "enabled",
            "priority",
        ),
    )

    rule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    saved_search_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(80), nullable=False)
    schedule_block_id: Mapped[str] = mapped_column(String(64), nullable=False)
    pick_strategy: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PICK_NEWEST, server_default=PICK_NEWEST
    )
    rolling_window_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    repeat_prevention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    last_materialized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    @classmethod
    def from_rule(cls, rule: AutoScheduleRule) -> AutoScheduleRuleRow:
        return cls(
            rule_id=rule.rule_id,
            name=rule.name,
            saved_search_id=rule.saved_search_id,
            channel_id=rule.channel_id,
            schedule_block_id=rule.schedule_block_id,
            pick_strategy=rule.pick_strategy,
            rolling_window_days=rule.rolling_window_days,
            repeat_prevention_days=rule.repeat_prevention_days,
            priority=rule.priority,
            enabled=rule.enabled,
            last_materialized_at=(
                rule.last_materialized_at.astimezone(UTC)
                if rule.last_materialized_at is not None
                else None
            ),
            created_at=rule.created_at.astimezone(UTC),
            updated_at=rule.updated_at.astimezone(UTC),
        )

    def to_rule(self) -> AutoScheduleRule:
        return AutoScheduleRule(
            rule_id=self.rule_id,
            name=self.name,
            saved_search_id=self.saved_search_id,
            channel_id=self.channel_id,
            schedule_block_id=self.schedule_block_id,
            pick_strategy=self.pick_strategy,  # type: ignore[arg-type]
            rolling_window_days=self.rolling_window_days,
            repeat_prevention_days=self.repeat_prevention_days,
            priority=self.priority,
            enabled=self.enabled,
            last_materialized_at=_as_utc(self.last_materialized_at),
            created_at=_as_utc(self.created_at),  # type: ignore[arg-type]
            updated_at=_as_utc(self.updated_at),  # type: ignore[arg-type]
        )
