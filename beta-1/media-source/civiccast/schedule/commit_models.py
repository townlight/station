# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Commit-to-Air models for the S4 playout-core build (CivicCast 3.0).

The Commit-to-Air gate is the operator's explicit "air this" approval step.
An operator prepares a *dry-run* of a materialized program occurrence
(conflict / missing-media / gap detection), and on approval the commit is
persisted as an append-and-update audit record and dispatched to the egress
automation layer.

This module holds the data model only (S4 slice 1):

* :class:`ScheduleConflict` — a detected time-range overlap (model-only).
* :class:`PlayoutEventPlan` — one segment in a channel's planned playout
  timeline; slice 1 uses it to carry detected dead-air gaps.
* :class:`CommitToAirPlan` — the ephemeral dry-run result (never persisted;
  built in-memory by the commit service in slice 2).
* :class:`CommitToAirReport` — the Pydantic view of the persisted record.
* :class:`CommitToAirReportRow` — the SQLAlchemy row backing
  ``civiccast.commit_to_air_reports`` (migration ``0040``).

Reference vocabulary mirrors :mod:`civiccast.schedule.models`: the SA row is
the persistence object (``...Row``), the Pydantic ``CommitToAirReport`` is the
serialization/response peer, and the round-trip lives on the row
(:meth:`CommitToAirReportRow.from_report` / :meth:`~CommitToAirReportRow.to_report`).

**Reference integrity is application-layer, not a DB FK** — deliberately, and
consistent with the rest of the schedule module (e.g. ``ScheduleItem.asset_id``
carries no ``ForeignKey``; existence is checked in the store, see
``PostgresScheduleStore.create`` QA-004). Three further reasons specific to
this table:

1. ``schedule_item_id`` stores the ``schedule_items.id`` value, which is a
   ``UUID`` primary key. A ``VARCHAR`` FK to a ``UUID`` column is a type
   mismatch Postgres rejects outright, so the spec's "VARCHAR(64) FK" cannot
   be a real FK as written.
2. The reports live in the ``civiccast`` schema but reference the program-log
   ``program_slot_occurrences`` table; cross-table FKs add migration-ordering
   and SQLite-enforcement friction the codebase has consistently avoided.
3. This is an **audit record**. It must survive the later cancellation or
   deletion of the schedule item / occurrence it refers to — a hard FK with
   cascade would erase history, and one without cascade would block routine
   cleanup. Soft string references keep the provenance regardless.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base

# ---------------------------------------------------------------------------
# Dispatch status state machine
# ---------------------------------------------------------------------------
# The egress dispatch lifecycle of a committed report:
#   pending      — report persisted, dispatch not yet attempted
#   queued       — dispatch enqueued to the egress automation layer (slice 3)
#   acknowledged — the egress daemon confirmed it applied the committed source
#   error        — dispatch failed; dispatch_error_detail carries the reason
#   cancelled    — operator rolled the commit back (POST .../rollback)
#
# NOTE (spec reconciliation): S4 §3 wrote the CHECK as
# IN ('pending','queued','acknowledged','error') but §4's rollback endpoint
# sets dispatch_status='cancelled'. The four-value CHECK would reject the
# documented rollback write, so 'cancelled' is included here and in the
# migration's CHECK constraint. Caught pre-merge; the spec text is stale.
DispatchStatusValue = Literal["pending", "queued", "acknowledged", "error", "cancelled"]
DISPATCH_STATUS_PENDING: DispatchStatusValue = "pending"
DISPATCH_STATUS_QUEUED: DispatchStatusValue = "queued"
DISPATCH_STATUS_ACKNOWLEDGED: DispatchStatusValue = "acknowledged"
DISPATCH_STATUS_ERROR: DispatchStatusValue = "error"
DISPATCH_STATUS_CANCELLED: DispatchStatusValue = "cancelled"
_DISPATCH_STATUSES = (
    DISPATCH_STATUS_PENDING,
    DISPATCH_STATUS_QUEUED,
    DISPATCH_STATUS_ACKNOWLEDGED,
    DISPATCH_STATUS_ERROR,
    DISPATCH_STATUS_CANCELLED,
)
PlayoutSegmentKind = Literal["program", "gap"]


def _as_utc(value: datetime | None) -> datetime | None:
    """Re-attach UTC to a naive datetime (SQLite drops tzinfo on round-trip).

    The persistence contract is "all timestamps are UTC". Postgres keeps the
    tzinfo; SQLite's ``DateTime(timezone=True)`` silently returns naive values,
    so the round-trip would otherwise present aware datetimes on Postgres and
    naive ones on SQLite. Mirrors ``Asset.to_metadata`` / ``ScheduleItem.to_response``.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


# ---------------------------------------------------------------------------
# Model-only entities (never persisted as their own rows)
# ---------------------------------------------------------------------------


class ScheduleConflict(BaseModel):
    """A detected time-range overlap between a proposed commit and an
    existing premiere item on the same channel.

    Built by the dry-run (slice 2). The counts are denormalized onto the
    persisted report (``conflicts_found``); the structured detail lives only
    in the ephemeral :class:`CommitToAirPlan` returned to the operator.
    """

    existing_schedule_item_id: str
    existing_asset_id: str
    existing_asset_title: str
    existing_scheduled_at: datetime
    existing_duration_seconds: int = Field(..., ge=0)
    proposed_scheduled_at: datetime
    proposed_duration_seconds: int = Field(..., ge=0)
    overlap_seconds: int = Field(..., ge=0)


class PlayoutEventPlan(BaseModel):
    """One segment in a channel's planned playout timeline.

    Slice 1 uses ``kind="gap"`` entries to represent detected dead-air gaps
    between adjacent scheduled items (the dry-run flags any gap > 1.0s as
    informational — a gap does not fail the dry-run, per S4 §6). The
    ``kind="program"`` value is reserved for the dispatcher's committed-segment
    timeline in a later slice; modelling both now keeps the gap list and the
    program timeline one shape.
    """

    kind: PlayoutSegmentKind
    starts_at: datetime
    ends_at: datetime
    duration_seconds: float = Field(..., ge=0)
    label: str | None = None


class CommitToAirPlan(BaseModel):
    """Ephemeral dry-run result — the operator's intent to air an occurrence.

    Built in-memory by the commit service (slice 2) and returned from the
    prepare-commit endpoint; never persisted. On approval, the durable subset
    is written as a :class:`CommitToAirReport`.
    """

    plan_id: str = Field(..., min_length=1, description='"ctap_" + url-safe token')
    channel_id: str
    occurrence_id: str
    schedule_item_id: str
    asset_id: str
    title: str
    scheduled_at: datetime
    duration_seconds: int = Field(..., ge=0)
    dry_run_passed: bool
    conflicts_detected: list[ScheduleConflict] = Field(default_factory=list)
    missing_media_detail: str | None = None
    gaps_detected: list[PlayoutEventPlan] = Field(default_factory=list)
    created_at: datetime
    operator_id: str | None = None


# ---------------------------------------------------------------------------
# Persisted entity — CommitToAirReport
# ---------------------------------------------------------------------------


class CommitToAirReport(BaseModel):
    """Pydantic view of a persisted commit-to-air record.

    Returned by the commit / list / detail / rollback endpoints. The SA peer
    is :class:`CommitToAirReportRow`.
    """

    report_id: str = Field(..., min_length=1, description='"ctar_" + url-safe token')
    channel_id: str
    occurrence_id: str
    schedule_item_id: str
    asset_id: str
    title: str
    scheduled_at: datetime
    duration_seconds: int = Field(..., ge=0)
    approved_by_operator_id: str
    approved_at: datetime
    conflicts_found: int = Field(default=0, ge=0)
    gaps_found: int = Field(default=0, ge=0)
    dispatch_status: DispatchStatusValue = DISPATCH_STATUS_PENDING
    dispatch_error_detail: str | None = None
    dispatch_timestamp: datetime | None = None
    # Operator's free-text reason supplied at commit time. Not in S4 §3's
    # column list, but the commit request (§4) carries ``operator_notes`` and
    # an audit record should retain the operator's stated intent. Shipping the
    # nullable column now avoids a second migration when the commit endpoint
    # lands in slice 4.
    operator_notes: str | None = None
    # Set only when the commit is rolled back (dispatch_status="cancelled").
    # The operator's stated reason + when the rollback happened — durable audit
    # of the undo, distinct from the commit-time operator_notes.
    rollback_reason: str | None = None
    rolled_back_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CommitToAirReportRow(Base):
    """SQLAlchemy row for ``civiccast.commit_to_air_reports`` (migration 0040).

    Append-and-update audit record: one row per operator commit, mutated
    in place only as its dispatch status advances (pending → queued →
    acknowledged | error) or on rollback (→ cancelled). See the module
    docstring for why the reference columns carry no DB foreign keys.
    """

    __tablename__ = "commit_to_air_reports"
    __table_args__ = (
        CheckConstraint(
            "dispatch_status IN ('pending', 'queued', 'acknowledged', 'error', 'cancelled')",
            name="commit_to_air_reports_dispatch_status_check",
        ),
        # Covers the list query: filter by channel_id, order/range by
        # approved_at (the commit-action timeline that backs the operator's
        # "recent commits" panel). A superset of the spec's bare channel_id
        # index.
        Index(
            "commit_to_air_reports_channel_approved_idx",
            "channel_id",
            "approved_at",
        ),
    )

    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(80), nullable=False)
    occurrence_id: Mapped[str] = mapped_column(String(120), nullable=False)
    schedule_item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_by_operator_id: Mapped[str] = mapped_column(String(80), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    conflicts_found: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    gaps_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    dispatch_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=DISPATCH_STATUS_PENDING,
        server_default=DISPATCH_STATUS_PENDING,
    )
    dispatch_error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    dispatch_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    operator_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Rollback audit (migration 0041): populated only when the commit is
    # undone — the operator's reason + the rollback instant.
    rollback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    @classmethod
    def from_report(cls, report: CommitToAirReport) -> CommitToAirReportRow:
        """Build a row from the Pydantic model, normalizing datetimes to UTC.

        ``created_at`` / ``updated_at`` come from the report when set (an
        update of an existing record); the store decides whether to preserve
        the original ``created_at`` on upsert.
        """
        return cls(
            report_id=report.report_id,
            channel_id=report.channel_id,
            occurrence_id=report.occurrence_id,
            schedule_item_id=report.schedule_item_id,
            asset_id=report.asset_id,
            title=report.title,
            scheduled_at=report.scheduled_at.astimezone(UTC),
            duration_seconds=report.duration_seconds,
            approved_by_operator_id=report.approved_by_operator_id,
            approved_at=report.approved_at.astimezone(UTC),
            conflicts_found=report.conflicts_found,
            gaps_found=report.gaps_found,
            dispatch_status=report.dispatch_status,
            dispatch_error_detail=report.dispatch_error_detail,
            dispatch_timestamp=(
                report.dispatch_timestamp.astimezone(UTC)
                if report.dispatch_timestamp is not None
                else None
            ),
            operator_notes=report.operator_notes,
            rollback_reason=report.rollback_reason,
            rolled_back_at=(
                report.rolled_back_at.astimezone(UTC) if report.rolled_back_at is not None else None
            ),
            created_at=report.created_at.astimezone(UTC),
            updated_at=report.updated_at.astimezone(UTC),
        )

    def to_report(self) -> CommitToAirReport:
        """Round-trip back to the Pydantic model, re-attaching UTC on naive
        datetimes so the contract is byte-equal on Postgres and SQLite."""
        return CommitToAirReport(
            report_id=self.report_id,
            channel_id=self.channel_id,
            occurrence_id=self.occurrence_id,
            schedule_item_id=self.schedule_item_id,
            asset_id=self.asset_id,
            title=self.title,
            scheduled_at=_as_utc(self.scheduled_at),  # type: ignore[arg-type]
            duration_seconds=self.duration_seconds,
            approved_by_operator_id=self.approved_by_operator_id,
            approved_at=_as_utc(self.approved_at),  # type: ignore[arg-type]
            conflicts_found=self.conflicts_found,
            gaps_found=self.gaps_found,
            dispatch_status=self.dispatch_status,  # type: ignore[arg-type]
            dispatch_error_detail=self.dispatch_error_detail,
            dispatch_timestamp=_as_utc(self.dispatch_timestamp),
            operator_notes=self.operator_notes,
            rollback_reason=self.rollback_reason,
            rolled_back_at=_as_utc(self.rolled_back_at),
            created_at=_as_utc(self.created_at),  # type: ignore[arg-type]
            updated_at=_as_utc(self.updated_at),  # type: ignore[arg-type]
        )
