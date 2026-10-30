# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""As-run / proof-of-performance + EPG export config models (S23).

Two durable entities on the single global Alembic chain (migration ``0055``):

* ``AsRunLogEntry`` — one **as-aired** record: what the playout engine ACTUALLY
  emitted at a source transition, with engine-verified ``actual_start`` /
  ``actual_end`` (and ``verified=True`` backed by a proof-event), distinct from
  what was *scheduled*. The optional ``schedule_item_id`` / ``scheduled_start``
  link back to the planned slot when there is one (``None`` = live / manual /
  filler). ``source_kind`` covers ``program``/``filler``/``live``/``slate`` and
  reserves ``spot`` so S24 underwriting can populate it later. As-run is an
  append-only franchise-compliance ledger (NOT the trimmed egress proof-event
  ring buffer) — proof-of-performance is a permanent record.
* ``EpgExportConfig`` — one EPG/TV-guide export profile: the ``format``
  (X-List / XMLTV / CSV), the look-ahead ``horizon_days``, an optional push
  ``endpoint`` (``None`` = download-only), and a ``field_map`` mapping CivicCast
  fields → aggregator columns.

Pydantic domain models pair with ``*Db`` SQLAlchemy ORM twins (no ``schema=`` in
``__table_args__``; the migration applies the schema). The ``source_kind`` and
``format`` literals are enforced in the DB by ``CheckConstraint``s created in the
migration, matching the S22 metadata-module convention.

``station_id`` / ``channel_id`` / ``asset_id`` / ``schedule_item_id`` are loose
string columns (no SQLAlchemy ``relationship``), matching the codebase's loose
``asset_id`` convention and keeping the engine write path import-clean.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base

# A stable id/slug: lowercase machine token (matches the metadata-module slug).
Slug = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$")]

# The kinds of source an as-run entry can record (spec §3). Scheduled programs
# matching an exact underwriting placement are recorded as ``spot``.
SourceKind = Literal["program", "filler", "live", "slate", "spot"]
SOURCE_KINDS: tuple[str, ...] = ("program", "filler", "live", "slate", "spot")

# The EPG/TV-guide export formats (spec §3).
EpgFormat = Literal["xlist", "xmltv", "csv"]
EPG_FORMATS: tuple[str, ...] = ("xlist", "xmltv", "csv")


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Pydantic domain models
# ---------------------------------------------------------------------------


class AsRunLogEntry(BaseModel):
    """One as-aired record (what the engine actually emitted, engine-verified)."""

    model_config = ConfigDict(extra="forbid")

    entry_id: Slug
    station_id: Slug
    channel_id: Slug
    # The planned slot, if any (None = live / manual / filler).
    schedule_item_id: Slug | None = None
    asset_id: Slug | None = None
    # Scheduled time (intent), present only when a planned slot links this entry.
    scheduled_start: datetime | None = None
    # What the engine ACTUALLY emitted (from the proof-event ``observed_at``).
    actual_start: datetime
    actual_end: datetime
    duration_s: int
    source_kind: SourceKind
    # Backed by an engine proof-event (not just scheduled intent).
    verified: bool = True
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class EpgExportConfig(BaseModel):
    """One EPG/TV-guide export profile (format + horizon + endpoint + field map)."""

    model_config = ConfigDict(extra="forbid")

    config_id: Slug
    station_id: Slug
    channel_id: Slug
    format: EpgFormat
    horizon_days: int = 14
    # Push target (aggregator) or None = download-only.
    endpoint: Annotated[str | None, Field(default=None, max_length=500)] = None
    # Map CivicCast fields → aggregator columns.
    field_map: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# API request / response shapes (router slice; defined here for a single import)
# ---------------------------------------------------------------------------


class EpgExportConfigInput(BaseModel):
    """Create-an-EPG-config request body (POST /api/staff/epg/configs)."""

    model_config = ConfigDict(extra="forbid")

    config_id: Slug
    station_id: Slug
    channel_id: Slug
    format: EpgFormat
    horizon_days: int = 14
    endpoint: Annotated[str | None, Field(default=None, max_length=500)] = None
    field_map: dict[str, str] = Field(default_factory=dict)


class EpgExportConfigUpdate(BaseModel):
    """Patch-an-EPG-config request body (PATCH /api/staff/epg/configs/{id}).

    Patch semantics (E-6 clarification): an absent key leaves the field
    unchanged; an explicit ``null`` clears the field (e.g.
    ``{"endpoint": null}`` switches the config from push to download-only).
    ``config_id`` / ``station_id`` are set at creation and not editable here.
    """

    model_config = ConfigDict(extra="forbid")

    channel_id: Slug | None = None
    format: EpgFormat | None = None
    horizon_days: int | None = None
    endpoint: Annotated[str | None, Field(default=None, max_length=500)] = None
    field_map: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# SQLAlchemy ORM twins (schema applied by migration 0055, not here)
# ---------------------------------------------------------------------------


class AsRunLogEntryDb(Base):
    """Durable as-aired ledger row. Append-only franchise-compliance record.

    Indexed for the report queries: ``(channel_id, actual_start)`` and
    ``(station_id, actual_start)`` serve the date-range/channel as-run + shows
    scans; ``asset_id`` serves the hours-by-category join to
    ``custom_field_values.asset_id``.
    """

    __tablename__ = "as_run_log"
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('program', 'filler', 'live', 'slate', 'spot')",
            name="as_run_log_source_kind_check",
        ),
        Index("ix_as_run_log_channel_actual_start", "channel_id", "actual_start"),
        Index("ix_as_run_log_station_actual_start", "station_id", "actual_start"),
        Index("ix_as_run_log_asset", "asset_id"),
        # E-5: source-kind-prefixed composite for the affidavit per-period scan.
        Index("ix_as_run_log_source_kind_actual_start", "source_kind", "actual_start"),
    )

    entry_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(120), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(80), nullable=False)
    schedule_item_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    asset_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    scheduled_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actual_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_s: Mapped[int] = mapped_column(Integer, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class EpgExportConfigDb(Base):
    """Durable EPG export profile row."""

    __tablename__ = "epg_export_configs"
    __table_args__ = (
        CheckConstraint(
            "format IN ('xlist', 'xmltv', 'csv')",
            name="epg_export_configs_format_check",
        ),
        Index("ix_epg_export_configs_station", "station_id"),
    )

    config_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(120), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(80), nullable=False)
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    endpoint: Mapped[str | None] = mapped_column(String(500), nullable=True)
    field_map: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


__all__ = [
    "EPG_FORMATS",
    "SOURCE_KINDS",
    "AsRunLogEntry",
    "AsRunLogEntryDb",
    "EpgExportConfig",
    "EpgExportConfigDb",
    "EpgExportConfigInput",
    "EpgExportConfigUpdate",
    "EpgFormat",
    "Slug",
    "SourceKind",
]
