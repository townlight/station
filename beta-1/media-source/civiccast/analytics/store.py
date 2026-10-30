# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable aggregate-only analytics store."""

from __future__ import annotations

import json
import os
import stat
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Protocol, cast

from civiccast.analytics.models import (
    AnalyticsDimensionCount,
    AnalyticsReport,
    AnalyticsRetainedEvent,
    AssetViewPoint,
    LiveConcurrentPoint,
    ReportDimension,
    ViewershipRollupPoint,
)
from civiccast.app_platform.models import AnalyticsEvent
from civiccast.installer.storage import default_storage_dir

_STATE_FILE_NAME = "analytics-events.json"
_DEFAULT_RETENTION_DAYS = 366
_MAX_RETAINED_EVENTS = 10000
_RETENTION_PRUNE_WRITE_INTERVAL = 100
_RETENTION_PRUNE_INTERVAL_SECONDS = 300
_RETAINED_FIELDS = [
    "event_id",
    "event_name",
    "occurred_at",
    "app_target",
    "channel_id",
    "content_id",
    "properties",
]
_SAFE_PROPERTY_KEYS = {
    "audio_track",
    "caption_language",
    "concurrent_viewers",
    "country",
    "device",
    "device_type",
    "download_count",
    "duration_seconds",
    "platform",
    "podcast_download",
    "position_seconds",
    "region",
    "state",
    "subscription_action",
    "view_seconds",
}


class AnalyticsStoreProtocol(Protocol):
    """Store contract shared by app-platform ingestion and staff reporting."""

    def record_event(self, event: AnalyticsEvent) -> AnalyticsRetainedEvent: ...

    def report(self, *, range_days: int = 30) -> AnalyticsReport: ...

    def save_snapshot(
        self,
        *,
        snapshot_id: str,
        generated_at: datetime,
        range_start: datetime,
        range_end: datetime,
        report_json: str,
        created_by: str,
    ) -> None: ...

    def rollups(
        self, *, stream_type: str, bucket_kind: str, range_days: int = 30
    ) -> list[ViewershipRollupPoint]: ...


class AnalyticsStore:
    """JSON-backed aggregate-only analytics store for compact station reports."""

    def __init__(self, state_path: Path | None = None) -> None:
        self._lock = Lock()
        self._state_path = state_path
        self._events = self._load_events()
        self._writes_since_retention_prune = 0
        self._last_retention_prune_at = time.monotonic()

    def record_event(self, event: AnalyticsEvent) -> AnalyticsRetainedEvent:
        retained = AnalyticsRetainedEvent(
            event_id=event.event_id,
            event_name=event.event_name,
            occurred_at=event.occurred_at,
            app_target=event.app_target,
            channel_id=event.channel_id,
            content_id=event.content_id,
            properties=_safe_properties(event.properties),
        )
        with self._lock:
            self._events[retained.event_id] = retained
            self._writes_since_retention_prune += 1
            if self._retention_prune_due_locked(retained):
                self._prune_retention_locked()
            self._persist_locked()
        return retained

    def report(self, *, range_days: int = 30) -> AnalyticsReport:
        if range_days < 1 or range_days > 366:
            raise ValueError("range_days must be between 1 and 366")
        cutoff = datetime.now(UTC) - timedelta(days=range_days)
        with self._lock:
            events = [
                event.model_copy(deep=True)
                for event in self._events.values()
                if event.occurred_at >= cutoff
            ]
        # Local import: civiccast.app_platform.router already imports FROM
        # civiccast.analytics.store (for the public ingest endpoint's store
        # DI seam) -- importing it at module scope here would be circular.
        from civiccast.app_platform.router import public_analytics_ingest_configured

        return AnalyticsReport(
            generated_at=datetime.now(UTC),
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
            ingest_configured=public_analytics_ingest_configured(),
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
        # The JSON-file-backed store has no durable snapshot table (that's
        # the S14 net-new AnalyticsReportSnapshotDb, Postgres-only). A board
        # PDF still generates and downloads on this store; it just isn't
        # persisted for later reproducibility -- an ephemeral-mode station
        # has no durable storage to persist it into anyway.
        return None

    def rollups(
        self, *, stream_type: str, bucket_kind: str, range_days: int = 30
    ) -> list[ViewershipRollupPoint]:
        # No rollup worker runs against the JSON-file store — S14's
        # background rollup pass is Postgres-only. Returns empty rather than
        # raising, so a station without durable storage sees an honest
        # empty-state dashboard instead of a 500.
        return []

    def _load_events(self) -> dict[str, AnalyticsRetainedEvent]:
        if self._state_path is None:
            return {}
        if not self._state_path.exists():
            return {}
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            items = payload.get("events", [])
            if not isinstance(items, list):
                raise TypeError("events must be a list")
            return {
                event.event_id: event
                for event in (AnalyticsRetainedEvent.model_validate(item) for item in items)
            }
        except (OSError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Cannot load analytics state from {self._state_path}: {exc}"
            ) from exc

    def _persist_locked(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "events": [
                event.model_dump(mode="json")
                for event in sorted(self._events.values(), key=lambda item: item.occurred_at)
            ][-_MAX_RETAINED_EVENTS:],
        }
        tmp_path = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        if os.name != "nt":
            tmp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        tmp_path.replace(self._state_path)

    def _prune_retention_locked(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=_analytics_retention_days())
        retained = {
            event_id: event
            for event_id, event in self._events.items()
            if event.occurred_at >= cutoff
        }
        if len(retained) > _MAX_RETAINED_EVENTS:
            retained = {
                event.event_id: event
                for event in sorted(retained.values(), key=lambda item: item.occurred_at)[
                    -_MAX_RETAINED_EVENTS:
                ]
            }
        self._events = retained
        self._writes_since_retention_prune = 0
        self._last_retention_prune_at = time.monotonic()

    def _retention_prune_due_locked(self, retained: AnalyticsRetainedEvent) -> bool:
        cutoff = datetime.now(UTC) - timedelta(days=_analytics_retention_days())
        if retained.occurred_at < cutoff:
            return True
        if len(self._events) > _MAX_RETAINED_EVENTS:
            return True
        if self._writes_since_retention_prune >= _RETENTION_PRUNE_WRITE_INTERVAL:
            return True
        return time.monotonic() - self._last_retention_prune_at >= _RETENTION_PRUNE_INTERVAL_SECONDS


def default_analytics_state_path() -> Path:
    """Return the managed analytics state path."""

    return default_storage_dir() / _STATE_FILE_NAME


def retained_analytics_fields() -> list[str]:
    """Fields retained after privacy filtering."""

    return list(_RETAINED_FIELDS)


def _analytics_retention_days() -> int:
    raw_days = os.environ.get("CIVICCAST_ANALYTICS_RETENTION_DAYS")
    if raw_days is None:
        return _DEFAULT_RETENTION_DAYS
    try:
        days = int(raw_days)
    except ValueError:
        return _DEFAULT_RETENTION_DAYS
    return min(max(days, 1), _DEFAULT_RETENTION_DAYS)


def _safe_properties(properties: dict[str, Any]) -> dict[str, str | int | float | bool]:
    retained: dict[str, str | int | float | bool] = {}
    for key, value in properties.items():
        normalized = key.strip().lower()
        if normalized not in _SAFE_PROPERTY_KEYS:
            continue
        if isinstance(value, str):
            retained[normalized] = value[:160]
        elif isinstance(value, bool | int | float):
            retained[normalized] = value
    return retained


def _asset_views(events: list[AnalyticsRetainedEvent]) -> list[AssetViewPoint]:
    buckets: dict[tuple[str, Any], dict[str, int]] = defaultdict(lambda: {"views": 0, "seconds": 0})
    for event in events:
        if event.content_id is None:
            continue
        key = (event.content_id, event.occurred_at.date())
        if event.event_name == "playback_start":
            buckets[key]["views"] += 1
        if event.event_name == "playback_complete":
            # position_seconds is a running playback position, only valid as
            # a duration at the final playback_complete event — summing it
            # from heartbeats too would over-count.
            buckets[key]["seconds"] += _int_property(
                event, "view_seconds", "duration_seconds", "position_seconds"
            )
        else:
            buckets[key]["seconds"] += _int_property(event, "view_seconds", "duration_seconds")
    return [
        AssetViewPoint(
            content_id=content_id, day=day, views=values["views"], view_seconds=values["seconds"]
        )
        for (content_id, day), values in sorted(buckets.items())
    ]


def _live_concurrent(events: list[AnalyticsRetainedEvent]) -> list[LiveConcurrentPoint]:
    buckets: dict[tuple[str, Any], list[int]] = defaultdict(list)
    for event in events:
        channel_id = event.channel_id
        if channel_id is None:
            continue
        viewers = _int_property(event, "concurrent_viewers")
        if viewers <= 0:
            continue
        buckets[(channel_id, event.occurred_at.date())].append(viewers)
    return [
        LiveConcurrentPoint(
            channel_id=channel_id,
            day=day,
            peak_concurrent_viewers=max(values),
            average_concurrent_viewers=round(sum(values) / len(values), 2),
            samples=len(values),
        )
        for (channel_id, day), values in sorted(buckets.items())
    ]


def _dimension_counts(
    events: list[AnalyticsRetainedEvent],
    dimension: ReportDimension,
    keys: tuple[str, ...],
) -> list[AnalyticsDimensionCount]:
    counts: dict[str, int] = defaultdict(int)
    for event in events:
        for key in keys:
            value = event.properties.get(key)
            if isinstance(value, str) and value.strip():
                counts[value.strip()] += 1
                break
    return [
        AnalyticsDimensionCount(dimension=dimension, key=key, count=count)
        for key, count in sorted(counts.items())
    ]


def _subscription_growth(events: list[AnalyticsRetainedEvent]) -> list[AnalyticsDimensionCount]:
    counts: dict[str, int] = defaultdict(int)
    for event in events:
        if event.event_name != "subscription_action":
            continue
        action = event.properties.get("subscription_action")
        if isinstance(action, str) and action.strip():
            channel = event.channel_id or "all"
            counts[f"{channel}:{action.strip()}"] += 1
    return [
        AnalyticsDimensionCount(dimension="subscription", key=key, count=count)
        for key, count in sorted(counts.items())
    ]


def _podcast_downloads(events: list[AnalyticsRetainedEvent]) -> list[AnalyticsDimensionCount]:
    counts: dict[str, int] = defaultdict(int)
    for event in events:
        if (
            event.event_name != "podcast_download"
            and event.properties.get("podcast_download") is not True
        ):
            continue
        key = event.content_id or event.channel_id or "unknown-podcast"
        count = _int_property(event, "download_count") or 1
        counts[key] += count
    return [
        AnalyticsDimensionCount(dimension="podcast", key=key, count=count)
        for key, count in sorted(counts.items())
    ]


def _int_property(event: AnalyticsRetainedEvent, *keys: str) -> int:
    for key in keys:
        value = event.properties.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            return max(0, int(value))
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return 0


def cast_analytics_store(store: object) -> AnalyticsStoreProtocol:
    """Runtime helper for router dependency type narrowing."""

    return cast(AnalyticsStoreProtocol, store)
