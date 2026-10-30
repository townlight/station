# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Aggregate-only analytics contracts for station reporting.

S14 (durable viewership store): three net-new tables promote the
beacon->store->report chain from a single JSON file to a migrated
Postgres-backed store — ``ViewershipEventDb`` (durable, privacy-filtered
event rows, replacing ``analytics-events.json``), ``ViewershipRollupDb``
(pre-aggregated VOD-24h / Live-30-min buckets the dashboard reads instead of
scanning raw events on every request), and ``AnalyticsReportSnapshotDb`` (a
stored, dated report — drives year-over-year comparison and reproducible
PDF/CSV exports). See migration ``0076_analytics_viewership`` and
``docs/spec/3.0/sections/S14-analytics-audience-measurement.md`` §3.

``ViewershipEventDb`` keeps a ``properties_json`` column beyond the three
scalar columns (``view_seconds``/``concurrent_viewers``/``geo_bucket``) the
spec's sketch names explicitly — dropping every other safe property would
regress the geography/device/platform/caption/audio/subscription/podcast
dimension breakdowns ``AnalyticsStore.report()`` already ships today. The
extra column is additive, not a spec deviation in shape: every field the
spec names is still a first-class column.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base

StreamType = Literal["vod", "live"]
RollupBucketKind = Literal["day", "halfhour", "hour"]
YoYMetric = Literal["viewer_count", "time_viewed_seconds", "peak_concurrent"]

ReportDimension = Literal[
    "asset",
    "live_concurrency",
    "geography",
    "device",
    "platform",
    "caption",
    "audio",
    "subscription",
    "podcast",
]


class AnalyticsRetainedEvent(BaseModel):
    """Stored event after dropping viewer/session identifiers."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=160)
    event_name: str = Field(min_length=1, max_length=80)
    occurred_at: datetime
    app_target: str = Field(min_length=1, max_length=80)
    channel_id: str | None = Field(default=None, max_length=160)
    content_id: str | None = Field(default=None, max_length=160)
    properties: dict[str, str | int | float | bool] = Field(default_factory=dict)


class AssetViewPoint(BaseModel):
    """Per-day aggregate playback totals for one asset."""

    model_config = ConfigDict(extra="forbid")

    content_id: str
    day: date
    views: int = Field(ge=0)
    view_seconds: int = Field(ge=0)


class LiveConcurrentPoint(BaseModel):
    """Per-day concurrent viewer trend for one channel."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    day: date
    peak_concurrent_viewers: int = Field(ge=0)
    average_concurrent_viewers: float = Field(ge=0)
    samples: int = Field(ge=0)


class AnalyticsDimensionCount(BaseModel):
    """Aggregate dimension count with no viewer/session identity."""

    model_config = ConfigDict(extra="forbid")

    dimension: ReportDimension
    key: str
    count: int = Field(ge=0)


class ViewershipRollupPoint(BaseModel):
    """One pre-aggregated rollup bucket (S14 §3/§6.1)."""

    model_config = ConfigDict(extra="forbid")

    stream_type: StreamType
    bucket_kind: RollupBucketKind
    bucket_start: datetime
    subject_id: str
    viewer_count: int = Field(ge=0)
    time_viewed_seconds: int = Field(ge=0)
    peak_concurrent: int | None = Field(default=None, ge=0)
    avg_concurrent: float | None = Field(default=None, ge=0)
    samples: int = Field(ge=0)


class YearOverYearPoint(BaseModel):
    """Current-vs-prior-year comparison for one headline metric (S14 §6.2)."""

    model_config = ConfigDict(extra="forbid")

    metric: YoYMetric
    current_period: int = Field(ge=0)
    prior_period: int = Field(ge=0)
    delta_pct: float | None = None  # None when prior_period == 0 — no fabricated growth.


class AnalyticsReport(BaseModel):
    """Station reporting snapshot with aggregate-only analytics."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    range_days: int = Field(ge=1, le=366)
    asset_views: list[AssetViewPoint] = Field(default_factory=list)
    live_concurrent_viewers: list[LiveConcurrentPoint] = Field(default_factory=list)
    geography: list[AnalyticsDimensionCount] = Field(default_factory=list)
    device_breakdown: list[AnalyticsDimensionCount] = Field(default_factory=list)
    platform_breakdown: list[AnalyticsDimensionCount] = Field(default_factory=list)
    caption_usage: list[AnalyticsDimensionCount] = Field(default_factory=list)
    audio_usage: list[AnalyticsDimensionCount] = Field(default_factory=list)
    subscription_growth: list[AnalyticsDimensionCount] = Field(default_factory=list)
    podcast_downloads: list[AnalyticsDimensionCount] = Field(default_factory=list)
    retained_fields: list[str]
    privacy_boundary: str
    # S14 net-new — pre-aggregated rollups + year-over-year. Empty lists on
    # the ephemeral JSON-backed store (no rollup worker there); populated
    # once durable storage + PostgresAnalyticsStore are active.
    vod_rollups: list[ViewershipRollupPoint] = Field(default_factory=list)
    live_rollups: list[ViewershipRollupPoint] = Field(default_factory=list)
    year_over_year: list[YearOverYearPoint] = Field(default_factory=list)
    # S14 §5 "load-bearing honesty": whether the deployment collects
    # audience telemetry at all. False when neither
    # CIVICCAST_PUBLIC_ANALYTICS_KEY nor an allowed-origins list is set, in
    # which case every beacon is accepted-and-dropped — the dashboard shows
    # an honest "telemetry is off" state instead of an empty chart that
    # looks broken. As-run / proof-of-performance reports are unaffected.
    ingest_configured: bool = True


# --- S14 durable ORM tables (migration 0076_analytics_viewership) ----------


class ViewershipEventDb(Base):
    """Durable, privacy-filtered viewership event row.

    Replaces ``analytics-events.json`` (``AnalyticsStore``) when durable
    storage is active. Mirrors ``AnalyticsRetainedEvent`` — deliberately
    carries NO ``anonymous_session_id``, NO ``hashed_viewer_id``, NO ``ip``;
    the privacy boundary is enforced before this row is ever constructed
    (see ``PostgresAnalyticsStore.record_event``).
    """

    __tablename__ = "viewership_events"

    event_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    event_name: Mapped[str] = mapped_column(String(80), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    app_target: Mapped[str] = mapped_column(String(80), nullable=False)
    stream_type: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    channel_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    content_id: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    view_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    concurrent_viewers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    geo_bucket: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # JSON-encoded remainder of the safe-property allowlist (device/platform/
    # caption/audio/subscription/podcast/geography keys) — see module
    # docstring. Never contains a key outside ``_SAFE_PROPERTY_KEYS``.
    properties_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class ViewershipRollupDb(Base):
    """Pre-aggregated rollup bucket the dashboard reads (S14 §3/§6.1)."""

    __tablename__ = "viewership_rollups"
    __table_args__ = (
        Index(
            "ix_viewership_rollups_unique_bucket",
            "stream_type",
            "bucket_kind",
            "subject_id",
            "bucket_start",
            unique=True,
        ),
        Index(
            "ix_viewership_rollups_lookup",
            "stream_type",
            "bucket_kind",
            "bucket_start",
        ),
    )

    rollup_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    stream_type: Mapped[str] = mapped_column(String(8), nullable=False)
    bucket_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(160), nullable=False)
    viewer_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    time_viewed_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    peak_concurrent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_concurrent: Mapped[float | None] = mapped_column(Float, nullable=True)
    samples: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )


class AnalyticsReportSnapshotDb(Base):
    """A stored, dated report snapshot — drives YoY + reproducible exports."""

    __tablename__ = "analytics_report_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    range_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    report_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(160), nullable=False)
