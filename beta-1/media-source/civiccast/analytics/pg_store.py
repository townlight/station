# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable Postgres-backed analytics store + rollup worker (S14).

Three pieces, matching the spec's §6.1 measurement spine:

* :class:`PostgresAnalyticsStore` — satisfies the same
  :class:`~civiccast.analytics.store.AnalyticsStoreProtocol` the JSON-backed
  :class:`~civiccast.analytics.store.AnalyticsStore` and the staff/public
  routers already depend on, so wiring this in is a pure DI swap (no router
  or ingest-endpoint signature changes). ``record_event`` persists a durable
  ``ViewershipEventDb`` row instead of writing a JSON file; ``report``
  reconstructs the same ``AnalyticsRetainedEvent`` shape from the DB and
  reuses ``civiccast.analytics.store``'s existing (already-tested)
  aggregation helpers for the geography/device/platform/caption/audio/
  subscription/podcast breakdowns, then layers on the S14 net-new
  ``vod_rollups`` / ``live_rollups`` / ``year_over_year`` fields read from
  ``ViewershipRollupDb``.
* :class:`AnalyticsRollupWorker` — the periodic background pass (S14 §6.1
  step 4) that folds raw ``viewership_events`` rows into pre-aggregated
  ``viewership_rollups`` buckets: VOD 24h (``day``) and Live both 30-min
  (``halfhour``) AND hourly (``hour``) — both live granularities are
  persisted so a dashboard request can pick either cadence (the spec's
  "hourly when a single day is selected" is a *display* choice, not a
  storage-time one) without recomputing. Idempotent upsert keyed on
  ``UNIQUE(stream_type, bucket_kind, subject_id, bucket_start)``.
* :func:`backfill_json_events` — the one-time, idempotent migration of any
  pre-existing ``analytics-events.json`` rows into ``viewership_events``.
  Runs from application code (see the module docstring in migration
  ``0076_analytics_viewership`` for why this is not inside the migration
  itself), guarded on "the events table is empty and a JSON file exists".
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from civiccast.analytics.models import (
    AnalyticsReport,
    AnalyticsReportSnapshotDb,
    AnalyticsRetainedEvent,
    ViewershipEventDb,
    ViewershipRollupDb,
    ViewershipRollupPoint,
    YearOverYearPoint,
)
from civiccast.analytics.store import (
    _RETAINED_FIELDS,
    _analytics_retention_days,
    _asset_views,
    _dimension_counts,
    _int_property,
    _live_concurrent,
    _podcast_downloads,
    _safe_properties,
    _subscription_growth,
)
from civiccast.app_platform.models import AnalyticsEvent

SessionFactory = Callable[[], AbstractContextManager[Session]]

_LOG = logging.getLogger(__name__)

ROLLUP_WORKER_MODE_INLINE = "inline"
ROLLUP_WORKER_MODE_OFF = "off"
_ROLLUP_WORKER_MODES = (ROLLUP_WORKER_MODE_INLINE, ROLLUP_WORKER_MODE_OFF)

__all__ = [
    "AnalyticsRollupSettings",
    "AnalyticsRollupWorker",
    "PostgresAnalyticsStore",
    "backfill_json_events",
]


def _stream_type_for(event: AnalyticsEvent) -> str:
    """Derive stream_type at ingest: content_id => vod; channel-only => live."""

    return "vod" if event.content_id else "live"


def _event_view_seconds(event_name: str, safe_properties: dict[str, Any]) -> int:
    """Per-event watch-time contribution, matching ``store.py``'s convention.

    ``position_seconds`` is a running playback position — only valid as a
    duration contribution at the final ``playback_complete`` event; summing
    it from every heartbeat too would over-count (see
    ``civiccast.analytics.store._asset_views``).
    """

    if event_name == "playback_complete":
        for key in ("view_seconds", "duration_seconds", "position_seconds"):
            value = safe_properties.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int | float):
                return max(0, int(value))
        return 0
    for key in ("view_seconds", "duration_seconds"):
        value = safe_properties.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            return max(0, int(value))
    return 0


class PostgresAnalyticsStore:
    """SQLAlchemy-backed analytics store (Postgres or managed SQLite)."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    # -- ingest -----------------------------------------------------------

    def record_event(self, event: AnalyticsEvent) -> AnalyticsRetainedEvent:
        safe_properties = _safe_properties(event.properties)
        stream_type = _stream_type_for(event)
        view_seconds = _event_view_seconds(event.event_name, safe_properties)
        concurrent_viewers = _int_property(
            AnalyticsRetainedEvent(
                event_id=event.event_id,
                event_name=event.event_name,
                occurred_at=event.occurred_at,
                app_target=event.app_target,
                channel_id=event.channel_id,
                content_id=event.content_id,
                properties=safe_properties,
            ),
            "concurrent_viewers",
        )
        with self._session_factory() as session:
            row = session.get(ViewershipEventDb, event.event_id)
            if row is None:
                row = ViewershipEventDb(event_id=event.event_id)
                session.add(row)
            row.event_name = event.event_name
            row.occurred_at = event.occurred_at
            row.app_target = event.app_target
            row.stream_type = stream_type
            row.channel_id = event.channel_id
            row.content_id = event.content_id
            row.view_seconds = view_seconds
            row.concurrent_viewers = concurrent_viewers or None
            row.geo_bucket = None  # opt-in coarse geo — off by default, S14 §10 open decision
            row.properties_json = json.dumps(safe_properties, sort_keys=True)
            session.commit()
        return AnalyticsRetainedEvent(
            event_id=event.event_id,
            event_name=event.event_name,
            occurred_at=event.occurred_at,
            app_target=event.app_target,
            channel_id=event.channel_id,
            content_id=event.content_id,
            properties=safe_properties,
        )

    # -- report -------------------------------------------------------------

    def report(self, *, range_days: int = 30) -> AnalyticsReport:
        if range_days < 1 or range_days > 366:
            raise ValueError("range_days must be between 1 and 366")
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=range_days)
        with self._session_factory() as session:
            self._prune_retention(session)
            rows = session.scalars(
                select(ViewershipEventDb).where(ViewershipEventDb.occurred_at >= cutoff)
            ).all()
            events = [_row_to_retained(row) for row in rows]
            vod_rollups = _rollup_points(session, stream_type="vod", start=cutoff, end=now)
            # Live: default the report's embedded rollups to the 30-min
            # cadence; the dedicated /rollups endpoint lets a caller pick
            # ``bucket=hour`` explicitly for a single-day view (S14 §6.1).
            live_rollups = _rollup_points(
                session, stream_type="live", bucket_kind="halfhour", start=cutoff, end=now
            )
            year_over_year = _year_over_year(session, start=cutoff, end=now)
        # Local import: civiccast.app_platform.router imports FROM
        # civiccast.analytics.store (the public ingest endpoint's store DI
        # seam) -- importing it at module scope here would be circular.
        from civiccast.app_platform.router import public_analytics_ingest_configured

        return AnalyticsReport(
            generated_at=now,
            range_days=range_days,
            asset_views=_asset_views(events),
            live_concurrent_viewers=_live_concurrent(events),
            geography=_dimension_counts(events, "geography", ("country", "state", "region")),
            device_breakdown=_dimension_counts(events, "device", ("device_type", "device")),
            platform_breakdown=_dimension_counts(events, "platform", ("platform",)),
            caption_usage=_dimension_counts(events, "caption", ("caption_language",)),
            audio_usage=_dimension_counts(events, "audio", ("audio_track",)),
            subscription_growth=_subscription_growth(events),
            podcast_downloads=_podcast_downloads(events),
            retained_fields=list(_RETAINED_FIELDS),
            privacy_boundary="aggregate-only-no-session-ip-or-viewer-identity",
            vod_rollups=vod_rollups,
            live_rollups=live_rollups,
            year_over_year=year_over_year,
            ingest_configured=public_analytics_ingest_configured(),
        )

    def rollups(
        self, *, stream_type: str, bucket_kind: str, range_days: int = 30
    ) -> list[ViewershipRollupPoint]:
        if range_days < 1 or range_days > 366:
            raise ValueError("range_days must be between 1 and 366")
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=range_days)
        with self._session_factory() as session:
            return _rollup_points(
                session, stream_type=stream_type, bucket_kind=bucket_kind, start=cutoff, end=now
            )

    def save_snapshot(
        self,
        *,
        snapshot_id: str,
        generated_at: datetime,
        range_start: datetime,
        range_end: datetime,
        report_json: str,
        created_by: str,
    ) -> None:
        with self._session_factory() as session:
            row = session.get(AnalyticsReportSnapshotDb, snapshot_id)
            if row is None:
                row = AnalyticsReportSnapshotDb(snapshot_id=snapshot_id)
                session.add(row)
            row.generated_at = generated_at
            row.range_start = range_start
            row.range_end = range_end
            row.report_json = report_json
            row.created_by = created_by
            session.commit()

    def _prune_retention(self, session: Session) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=_analytics_retention_days())
        stale = session.scalars(
            select(ViewershipEventDb).where(ViewershipEventDb.occurred_at < cutoff)
        ).all()
        for row in stale:
            session.delete(row)
        if stale:
            session.commit()


def _row_to_retained(row: ViewershipEventDb) -> AnalyticsRetainedEvent:
    try:
        properties: dict[str, Any] = json.loads(row.properties_json or "{}")
    except (TypeError, ValueError):
        properties = {}
    return AnalyticsRetainedEvent(
        event_id=row.event_id,
        event_name=row.event_name,
        occurred_at=_as_utc(row.occurred_at),
        app_target=row.app_target,
        channel_id=row.channel_id,
        content_id=row.content_id,
        properties=properties,
    )


def _rollup_points(
    session: Session,
    *,
    stream_type: str,
    start: datetime,
    end: datetime,
    bucket_kind: str | None = None,
) -> list[ViewershipRollupPoint]:
    stmt = select(ViewershipRollupDb).where(
        ViewershipRollupDb.stream_type == stream_type,
        ViewershipRollupDb.bucket_start >= start,
        ViewershipRollupDb.bucket_start <= end,
    )
    if bucket_kind is not None:
        stmt = stmt.where(ViewershipRollupDb.bucket_kind == bucket_kind)
    stmt = stmt.order_by(ViewershipRollupDb.bucket_start.asc(), ViewershipRollupDb.subject_id.asc())
    rows = session.scalars(stmt).all()
    return [
        ViewershipRollupPoint(
            stream_type=row.stream_type,  # type: ignore[arg-type]
            bucket_kind=row.bucket_kind,  # type: ignore[arg-type]
            bucket_start=_as_utc(row.bucket_start),
            subject_id=row.subject_id,
            viewer_count=row.viewer_count,
            time_viewed_seconds=row.time_viewed_seconds,
            peak_concurrent=row.peak_concurrent,
            avg_concurrent=row.avg_concurrent,
            samples=row.samples,
        )
        for row in rows
    ]


def _year_over_year(session: Session, *, start: datetime, end: datetime) -> list[YearOverYearPoint]:
    """Compare the current range to the same calendar range one year prior.

    ``delta_pct`` is ``None`` when the prior period has zero — never a
    fabricated or infinite growth number (S14 §6.2).
    """

    prior_start = start - timedelta(days=365)
    prior_end = end - timedelta(days=365)

    def _sum(field: str, range_start: datetime, range_end: datetime) -> int:
        rows = session.scalars(
            select(ViewershipRollupDb).where(
                ViewershipRollupDb.bucket_start >= range_start,
                ViewershipRollupDb.bucket_start <= range_end,
                ViewershipRollupDb.bucket_kind.in_(("day", "halfhour")),
            )
        ).all()
        return sum(getattr(row, field) or 0 for row in rows)

    def _max_peak(range_start: datetime, range_end: datetime) -> int:
        rows = session.scalars(
            select(ViewershipRollupDb).where(
                ViewershipRollupDb.stream_type == "live",
                ViewershipRollupDb.bucket_start >= range_start,
                ViewershipRollupDb.bucket_start <= range_end,
            )
        ).all()
        peaks = [row.peak_concurrent for row in rows if row.peak_concurrent is not None]
        return max(peaks) if peaks else 0

    points: list[YearOverYearPoint] = []
    for metric, current, prior in (
        (
            "viewer_count",
            _sum("viewer_count", start, end),
            _sum("viewer_count", prior_start, prior_end),
        ),
        (
            "time_viewed_seconds",
            _sum("time_viewed_seconds", start, end),
            _sum("time_viewed_seconds", prior_start, prior_end),
        ),
        (
            "peak_concurrent",
            _max_peak(start, end),
            _max_peak(prior_start, prior_end),
        ),
    ):
        delta_pct = round((current - prior) / prior * 100, 1) if prior else None
        points.append(
            YearOverYearPoint(
                metric=metric,  # type: ignore[arg-type]
                current_period=current,
                prior_period=prior,
                delta_pct=delta_pct,
            )
        )
    return points


# --- rollup worker -----------------------------------------------------------


@dataclass(frozen=True)
class AnalyticsRollupSettings:
    """Deployment configuration for the analytics rollup worker."""

    mode: str = ROLLUP_WORKER_MODE_INLINE
    poll_seconds: float = 300.0  # spec §10 recommendation: "every 5 min"
    lookback_days: int = 3  # bounded recompute window per poll; late data + today

    @classmethod
    def from_env(cls) -> AnalyticsRollupSettings:
        mode = os.environ.get("CIVICCAST_ANALYTICS_ROLLUP_WORKER", ROLLUP_WORKER_MODE_INLINE)
        mode = mode.strip().lower()
        if mode not in _ROLLUP_WORKER_MODES:
            raise ValueError(
                f"CIVICCAST_ANALYTICS_ROLLUP_WORKER must be one of "
                f"{', '.join(_ROLLUP_WORKER_MODES)}; got {mode!r}."
            )
        defaults = cls()
        poll = _float_env("CIVICCAST_ANALYTICS_ROLLUP_POLL_SECONDS", defaults.poll_seconds)
        lookback = _int_env("CIVICCAST_ANALYTICS_ROLLUP_LOOKBACK_DAYS", defaults.lookback_days)
        return cls(mode=mode, poll_seconds=poll, lookback_days=lookback)


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number; got {raw!r}.") from exc


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer; got {raw!r}.") from exc


class AnalyticsRollupWorker:
    """Folds raw ``viewership_events`` into pre-aggregated ``viewership_rollups``.

    On the very first run ever (no rollup rows exist yet), the window widens
    to the full retention horizon so a station upgrading onto S14 gets
    immediate historical rollups rather than waiting ``lookback_days`` worth
    of new traffic to backfill them. Every subsequent run recomputes only the
    bounded recent window (default 3 days), which is enough to absorb
    late-arriving beacons while staying cheap on a busy station.
    """

    def __init__(
        self, session_factory: SessionFactory, *, settings: AnalyticsRollupSettings
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    def run_forever(
        self, *, poll_seconds: float | None = None, stop_event: threading.Event | None = None
    ) -> None:
        interval = poll_seconds if poll_seconds is not None else self._settings.poll_seconds
        while stop_event is None or not stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                _LOG.exception("Analytics rollup pass failed; retrying on the next poll interval.")
            if stop_event is not None:
                stop_event.wait(interval)
            else:
                time.sleep(interval)

    def run_once(self, *, now: datetime | None = None) -> int:
        """Recompute rollups for the active window; return bucket rows touched."""

        resolved_now = now or datetime.now(UTC)
        with self._session_factory() as session:
            has_rollups = session.execute(select(ViewershipRollupDb.rollup_id).limit(1)).first()
            if has_rollups is None:
                window_start = resolved_now - timedelta(days=_analytics_retention_days())
            else:
                window_start = resolved_now - timedelta(days=self._settings.lookback_days)
            touched = _recompute_window(session, window_start=window_start, now=resolved_now)
            session.commit()
        return touched


def _recompute_window(session: Session, *, window_start: datetime, now: datetime) -> int:
    rows = session.scalars(
        select(ViewershipEventDb).where(
            ViewershipEventDb.occurred_at >= window_start,
            ViewershipEventDb.occurred_at <= now,
        )
    ).all()

    vod_buckets: dict[tuple[str, date], dict[str, int]] = {}
    live_half: dict[tuple[str, datetime], dict[str, Any]] = {}
    live_hour: dict[tuple[str, datetime], dict[str, Any]] = {}

    for row in rows:
        if row.stream_type == "vod" and row.content_id:
            key = (row.content_id, _as_utc(row.occurred_at).date())
            bucket = vod_buckets.setdefault(key, {"views": 0, "seconds": 0})
            if row.event_name == "playback_start":
                bucket["views"] += 1
            bucket["seconds"] += row.view_seconds
        elif row.stream_type == "live" and row.channel_id:
            occurred = _as_utc(row.occurred_at)
            half_start = _floor_halfhour(occurred)
            hour_start = _floor_hour(occurred)
            for bucket_map, start in ((live_half, half_start), (live_hour, hour_start)):
                key = (row.channel_id, start)
                entry = bucket_map.setdefault(
                    key, {"views": 0, "seconds": 0, "concurrent_samples": []}
                )
                if row.event_name == "playback_start":
                    entry["views"] += 1
                entry["seconds"] += row.view_seconds
                if row.concurrent_viewers is not None and row.concurrent_viewers > 0:
                    entry["concurrent_samples"].append(row.concurrent_viewers)

    touched = 0
    for (content_id, day), values in vod_buckets.items():
        bucket_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        touched += _upsert_rollup(
            session,
            stream_type="vod",
            bucket_kind="day",
            bucket_start=bucket_start,
            subject_id=content_id,
            viewer_count=values["views"],
            time_viewed_seconds=values["seconds"],
            peak_concurrent=None,
            avg_concurrent=None,
            samples=0,
        )
    for bucket_kind, bucket_map in (("halfhour", live_half), ("hour", live_hour)):
        for (channel_id, bucket_start), live_values in bucket_map.items():
            concurrent_samples: list[int] = live_values["concurrent_samples"]
            touched += _upsert_rollup(
                session,
                stream_type="live",
                bucket_kind=bucket_kind,
                bucket_start=bucket_start,
                subject_id=channel_id,
                viewer_count=live_values["views"],
                time_viewed_seconds=live_values["seconds"],
                peak_concurrent=max(concurrent_samples) if concurrent_samples else None,
                avg_concurrent=(
                    round(sum(concurrent_samples) / len(concurrent_samples), 2)
                    if concurrent_samples
                    else None
                ),
                samples=len(concurrent_samples),
            )
    return touched


def _as_utc(moment: datetime) -> datetime:
    """Coerce a possibly-naive datetime to aware UTC without shifting it.

    SQLite hands back naive datetimes even for ``DateTime(timezone=True)``
    columns; the stored values are always UTC (every write path in this
    module writes tz-aware UTC datetimes). ``.astimezone(UTC)`` on a naive
    value would instead interpret it as *local* wall-clock time and shift
    it -- the same gotcha documented in
    ``civiccast.schedule.retention_worker.DispositionReviewResponse``.
    """

    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _floor_halfhour(moment: datetime) -> datetime:
    minute = 0 if moment.minute < 30 else 30
    return moment.replace(minute=minute, second=0, microsecond=0)


def _floor_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def _upsert_rollup(
    session: Session,
    *,
    stream_type: str,
    bucket_kind: str,
    bucket_start: datetime,
    subject_id: str,
    viewer_count: int,
    time_viewed_seconds: int,
    peak_concurrent: int | None,
    avg_concurrent: float | None,
    samples: int,
) -> int:
    rollup_id = f"{stream_type}:{bucket_kind}:{subject_id}:{bucket_start.isoformat()}"
    row = session.get(ViewershipRollupDb, rollup_id)
    if row is None:
        row = ViewershipRollupDb(
            rollup_id=rollup_id,
            stream_type=stream_type,
            bucket_kind=bucket_kind,
            bucket_start=bucket_start,
            subject_id=subject_id,
        )
        session.add(row)
    row.viewer_count = viewer_count
    row.time_viewed_seconds = time_viewed_seconds
    row.peak_concurrent = peak_concurrent
    row.avg_concurrent = avg_concurrent
    row.samples = samples
    row.updated_at = datetime.now(UTC)
    return 1


# --- one-time JSON backfill ---------------------------------------------------


def backfill_json_events(session_factory: SessionFactory, json_path: Path) -> int:
    """Idempotently migrate ``analytics-events.json`` rows into the DB.

    Guarded on "the events table is empty" so it only ever runs meaningfully
    once per station; every subsequent app start is a fast no-op. Returns the
    number of events migrated (0 when nothing to do).
    """

    if not json_path.exists():
        return 0
    with session_factory() as session:
        already_migrated = session.execute(select(ViewershipEventDb.event_id).limit(1)).first()
        if already_migrated is not None:
            return 0
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _LOG.warning(
                "Could not read legacy analytics JSON at %s; skipping backfill.", json_path
            )
            return 0
        items = payload.get("events", []) if isinstance(payload, dict) else []
        migrated = 0
        for item in items:
            try:
                retained = AnalyticsRetainedEvent.model_validate(item)
            except Exception:
                _LOG.warning(
                    "Skipping one malformed legacy analytics event during backfill from %s.",
                    json_path,
                )
                continue
            stream_type = "vod" if retained.content_id else "live"
            view_seconds = _event_view_seconds(retained.event_name, retained.properties)
            concurrent = _int_property(retained, "concurrent_viewers")
            if session.get(ViewershipEventDb, retained.event_id) is not None:
                continue
            session.add(
                ViewershipEventDb(
                    event_id=retained.event_id,
                    event_name=retained.event_name,
                    occurred_at=retained.occurred_at,
                    app_target=retained.app_target,
                    stream_type=stream_type,
                    channel_id=retained.channel_id,
                    content_id=retained.content_id,
                    view_seconds=view_seconds,
                    concurrent_viewers=concurrent or None,
                    geo_bucket=None,
                    properties_json=json.dumps(retained.properties, sort_keys=True),
                )
            )
            migrated += 1
        session.commit()
    if migrated:
        _LOG.info(
            "Backfilled %d legacy analytics event(s) from %s into viewership_events.",
            migrated,
            json_path,
        )
    return migrated
