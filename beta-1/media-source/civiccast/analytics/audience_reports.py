# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S14 audience reports — franchise-authority-ready summary surface.

the incumbent PEG workflow ships an audience-measurement report (via cloud telemetry) that a station
hands to its franchise authority as a weekly or monthly deliverable. CivicCast
already collects the underlying aggregate analytics (per-asset views, live
concurrent viewers, geography / device / platform breakdowns) — what was
missing was the packaged report itself: a stable, exportable shape with
per-channel totals, top-asset rows, and a single header summary the operator
can hand off.

This module lives at ``civiccast/analytics/`` (moved from
``civiccast/reporting/audience.py`` — same code, correct home) — it pulls
from the existing ``AnalyticsReport`` and reshapes it into the
franchise-friendly form. Adding new collection plumbing wasn't necessary;
the data is already there.

The report shape:

* ``AudienceReport``  — top-level: period, generated_at, totals, plus a
  per-channel summary list and a top-N asset list. Always present even
  when the station has no analytics data yet (zeros + an empty
  ``channel_summaries`` list — the operator gets a clean "no activity"
  surface rather than a 404).
* ``AudienceChannelSummary`` — totals + peak concurrent per channel, plus
  ``average_view_seconds`` (``total_view_seconds / total_views``).
* ``AudienceAssetSummary``   — totals per asset (top-N by views), plus
  ``average_view_seconds`` per asset.
* ``AudienceDimensionRow``   — generic dimension/key/count rows (geography,
  device, platform).

Exports:

* CSV (UTF-8 with BOM so Excel + LibreOffice open it cleanly)
* XML (one ``<audience-report>`` root with nested sections)
* JSON via the model's existing ``.model_dump_json()``

The XML export uses the same shape S23's ``shows_report`` XML uses (one
section per row family) so a franchise authority's automated importer can
treat the audience report symmetrically with the as-run / shows reports.

HONEST GAP (documented, not hidden): this module does NOT produce a true
watch-*duration distribution* (a histogram of individual view lengths).
``AnalyticsStore.report()`` (``analytics/store.py``) only exposes
per-(asset, day) aggregate totals (``AssetViewPoint.views`` /
``.view_seconds``) — the per-event granularity needed for a real
distribution (bucketing individual playback durations into e.g.
<1min/1-5min/5-15min/15-30min/30+min) is deliberately dropped before it
reaches this layer, because ``AnalyticsStore`` never retains per-viewer
event-level rows in its report output (only aggregates — the privacy/
scale posture, see ``store.py`` module docstring). ``average_view_seconds``
below is the honestly-answerable proxy (mean, not a distribution); a real
histogram would require ``AnalyticsStore`` to expose a new per-event-bucket
aggregate, which is out of scope for this pass — recorded as an open gap,
not faked with synthetic buckets.
"""

from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from civiccast.analytics.models import (
    AnalyticsDimensionCount,
    AnalyticsReport,
    AssetViewPoint,
    LiveConcurrentPoint,
)
from civiccast.common.csv_safety import csv_safe

AudiencePeriodKind = Literal["weekly", "monthly", "custom"]


class _ChannelBucket(TypedDict):
    total_views: int
    total_view_seconds: int
    peak_concurrent_viewers: int
    average_concurrent_sum: float
    samples: int
    unique_assets: set[str]


class _AssetBucket(TypedDict):
    total_views: int
    total_view_seconds: int
    first_seen_day: date | None
    last_seen_day: date | None


class AudienceChannelSummary(BaseModel):
    """Per-channel audience totals across the report period.

    ``unique_assets`` counts distinct asset ids with at least one view —
    a useful "did the channel actually broadcast distinct content?" metric.
    """

    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1, max_length=160)
    total_views: int = Field(ge=0)
    total_view_seconds: int = Field(ge=0)
    average_view_seconds: float = Field(
        ge=0,
        description=(
            "total_view_seconds / total_views — an honest PROXY metric, not a "
            "distribution (see module docstring HONEST GAP: no per-event "
            "granularity is available to bucket a real histogram)."
        ),
    )
    peak_concurrent_viewers: int = Field(ge=0)
    average_concurrent_viewers: float = Field(ge=0)
    unique_assets: int = Field(ge=0)
    samples: int = Field(ge=0)


class AudienceAssetSummary(BaseModel):
    """Per-asset totals — typically rendered as a top-N list."""

    model_config = ConfigDict(extra="forbid")

    content_id: str = Field(min_length=1, max_length=160)
    channel_id: str | None = Field(default=None, max_length=160)
    total_views: int = Field(ge=0)
    total_view_seconds: int = Field(ge=0)
    average_view_seconds: float = Field(
        ge=0, description="total_view_seconds / total_views — mean, not a distribution."
    )
    first_seen_day: date | None = None
    last_seen_day: date | None = None


class AudienceDimensionRow(BaseModel):
    """Generic aggregate row for geography / device / platform / caption /
    audio breakdowns. Mirrors ``AnalyticsDimensionCount`` but allows the
    consumer to render the dimension and key in stable column order."""

    model_config = ConfigDict(extra="forbid")

    dimension: str = Field(min_length=1, max_length=40)
    key: str = Field(min_length=1, max_length=160)
    count: int = Field(ge=0)


class AudienceReport(BaseModel):
    """Top-level audience report — the franchise-deliverable shape.

    A franchise authority typically wants: "in the period from X to Y, how
    many people watched, on which channels, of which assets, from which
    geographies, on which devices." That's exactly what this surface answers,
    in a single document the operator can hand off.
    """

    model_config = ConfigDict(extra="forbid")

    period_kind: AudiencePeriodKind
    period_start: date
    period_end: date
    generated_at: datetime
    range_days: int = Field(ge=1, le=366)
    total_views: int = Field(ge=0)
    total_view_seconds: int = Field(ge=0)
    channel_summaries: list[AudienceChannelSummary] = Field(default_factory=list)
    top_assets: list[AudienceAssetSummary] = Field(default_factory=list)
    geography: list[AudienceDimensionRow] = Field(default_factory=list)
    device_breakdown: list[AudienceDimensionRow] = Field(default_factory=list)
    platform_breakdown: list[AudienceDimensionRow] = Field(default_factory=list)
    caption_usage: list[AudienceDimensionRow] = Field(default_factory=list)
    audio_usage: list[AudienceDimensionRow] = Field(default_factory=list)
    subscription_growth: list[AudienceDimensionRow] = Field(default_factory=list)
    podcast_downloads: list[AudienceDimensionRow] = Field(default_factory=list)
    privacy_boundary: str = Field(min_length=1, max_length=200)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _period_bounds(
    kind: AudiencePeriodKind,
    *,
    period_start: date | None,
    period_end: date | None,
) -> tuple[date, date]:
    """Resolve the (start, end) bounds.

    For ``weekly`` / ``monthly`` the caller may pass ``period_end`` and we
    derive ``period_start`` (today - 7 / today - 30 by default).
    ``custom`` requires both bounds — raises ``ValueError`` otherwise.
    """
    today = _utc_today()
    if kind == "custom":
        if period_start is None or period_end is None:
            raise ValueError("custom period requires both period_start and period_end.")
        if period_end < period_start:
            raise ValueError("period_end must be on or after period_start.")
        return period_start, period_end
    end = period_end or today
    if kind == "weekly":
        return end - timedelta(days=7), end
    # monthly
    return end - timedelta(days=30), end


def _safe_average(total_seconds: int, total_views: int) -> float:
    """``total_seconds / total_views``, or 0.0 when there are no views.

    A mean, not a distribution — see the module docstring HONEST GAP note.
    """
    return total_seconds / total_views if total_views > 0 else 0.0


def _channel_summaries(
    asset_views: list[AssetViewPoint],
    concurrent_points: list[LiveConcurrentPoint],
    *,
    asset_to_channel: dict[str, str],
) -> list[AudienceChannelSummary]:
    """Aggregate per-channel totals.

    Asset views are joined to their owning channel via ``asset_to_channel``;
    assets without a channel hint are bucketed under ``"unattributed"`` so the
    operator can still see the volume.
    """
    by_channel: dict[str, _ChannelBucket] = {}

    def _slot(channel_id: str) -> _ChannelBucket:
        slot = by_channel.get(channel_id)
        if slot is None:
            slot = {
                "total_views": 0,
                "total_view_seconds": 0,
                "peak_concurrent_viewers": 0,
                "average_concurrent_sum": 0.0,
                "samples": 0,
                "unique_assets": set(),
            }
            by_channel[channel_id] = slot
        return slot

    for asset_point in asset_views:
        channel_id = asset_to_channel.get(asset_point.content_id, "unattributed")
        slot = _slot(channel_id)
        slot["total_views"] = int(slot["total_views"]) + asset_point.views
        slot["total_view_seconds"] = int(slot["total_view_seconds"]) + asset_point.view_seconds
        slot["unique_assets"].add(asset_point.content_id)

    for concurrent_point in concurrent_points:
        slot = _slot(concurrent_point.channel_id)
        slot["peak_concurrent_viewers"] = max(
            int(slot["peak_concurrent_viewers"]), concurrent_point.peak_concurrent_viewers
        )
        slot["average_concurrent_sum"] = float(slot["average_concurrent_sum"]) + (
            concurrent_point.average_concurrent_viewers * concurrent_point.samples
        )
        slot["samples"] = int(slot["samples"]) + concurrent_point.samples

    out: list[AudienceChannelSummary] = []
    for channel_id, slot in by_channel.items():
        samples = int(slot["samples"])
        avg = float(slot["average_concurrent_sum"]) / samples if samples > 0 else 0.0
        out.append(
            AudienceChannelSummary(
                channel_id=channel_id,
                total_views=int(slot["total_views"]),
                total_view_seconds=int(slot["total_view_seconds"]),
                average_view_seconds=_safe_average(
                    int(slot["total_view_seconds"]), int(slot["total_views"])
                ),
                peak_concurrent_viewers=int(slot["peak_concurrent_viewers"]),
                average_concurrent_viewers=avg,
                unique_assets=len(slot["unique_assets"]),
                samples=samples,
            )
        )
    # Sort by total_views desc — the franchise authority reads top channels first.
    out.sort(key=lambda c: c.total_views, reverse=True)
    return out


def _top_assets(
    asset_views: list[AssetViewPoint],
    *,
    asset_to_channel: dict[str, str],
    limit: int,
) -> list[AudienceAssetSummary]:
    by_asset: dict[str, _AssetBucket] = {}
    for point in asset_views:
        slot = by_asset.setdefault(
            point.content_id,
            {
                "total_views": 0,
                "total_view_seconds": 0,
                "first_seen_day": None,
                "last_seen_day": None,
            },
        )
        slot["total_views"] = int(slot["total_views"]) + point.views
        slot["total_view_seconds"] = int(slot["total_view_seconds"]) + point.view_seconds
        first = slot["first_seen_day"]
        last = slot["last_seen_day"]
        slot["first_seen_day"] = point.day if first is None else min(point.day, first)
        slot["last_seen_day"] = point.day if last is None else max(point.day, last)

    rows: list[AudienceAssetSummary] = []
    for content_id, slot in by_asset.items():
        rows.append(
            AudienceAssetSummary(
                content_id=content_id,
                channel_id=asset_to_channel.get(content_id),
                total_views=int(slot["total_views"]),
                total_view_seconds=int(slot["total_view_seconds"]),
                average_view_seconds=_safe_average(
                    int(slot["total_view_seconds"]), int(slot["total_views"])
                ),
                first_seen_day=slot["first_seen_day"],
                last_seen_day=slot["last_seen_day"],
            )
        )
    rows.sort(key=lambda r: r.total_views, reverse=True)
    return rows[:limit]


def _dimension_rows(
    items: list[AnalyticsDimensionCount], dimension_name: str
) -> list[AudienceDimensionRow]:
    return [
        AudienceDimensionRow(dimension=dimension_name, key=item.key, count=item.count)
        for item in items
    ]


def build_audience_report(
    base_report: AnalyticsReport,
    *,
    kind: AudiencePeriodKind = "monthly",
    period_start: date | None = None,
    period_end: date | None = None,
    asset_to_channel: dict[str, str] | None = None,
    top_assets_limit: int = 25,
) -> AudienceReport:
    """Build a packaged audience report from an aggregate ``AnalyticsReport``.

    ``asset_to_channel`` maps content ids to owning channel ids. When a
    content id is absent from the map, its views are bucketed under
    ``"unattributed"`` so the operator still sees the volume — the
    franchise authority can ask about the unattributed bucket if it
    matters to their report cycle.

    The default ``top_assets_limit`` of 25 matches what most franchise
    reports include in their "most-watched programs" section.
    """
    start, end = _period_bounds(kind, period_start=period_start, period_end=period_end)
    a2c = dict(asset_to_channel or {})

    # base_report's per-day points may span a wider window than the resolved
    # (start, end) period (range_days is a store-pull width, not the report's
    # labeled period) — filter to the actual period before aggregating so the
    # report's stated period and its data always agree.
    asset_views = [point for point in base_report.asset_views if start <= point.day <= end]
    concurrent_points = [
        point for point in base_report.live_concurrent_viewers if start <= point.day <= end
    ]
    channel_summaries = _channel_summaries(
        asset_views,
        concurrent_points,
        asset_to_channel=a2c,
    )
    total_views = sum(c.total_views for c in channel_summaries)
    total_view_seconds = sum(c.total_view_seconds for c in channel_summaries)

    return AudienceReport(
        period_kind=kind,
        period_start=start,
        period_end=end,
        generated_at=datetime.now(UTC),
        range_days=base_report.range_days,
        total_views=total_views,
        total_view_seconds=total_view_seconds,
        channel_summaries=channel_summaries,
        top_assets=_top_assets(asset_views, asset_to_channel=a2c, limit=top_assets_limit),
        geography=_dimension_rows(base_report.geography, "geography"),
        device_breakdown=_dimension_rows(base_report.device_breakdown, "device"),
        platform_breakdown=_dimension_rows(base_report.platform_breakdown, "platform"),
        caption_usage=_dimension_rows(base_report.caption_usage, "caption"),
        audio_usage=_dimension_rows(base_report.audio_usage, "audio"),
        subscription_growth=_dimension_rows(base_report.subscription_growth, "subscription"),
        podcast_downloads=_dimension_rows(base_report.podcast_downloads, "podcast"),
        privacy_boundary=base_report.privacy_boundary,
    )


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


def _csv_safe(value: str) -> str:
    """Guard a CSV cell against spreadsheet formula injection.

    Thin wrapper over the shared :func:`civiccast.common.csv_safety.csv_safe`
    so the audience-report and underwriting-affidavit exporters share one
    implementation (SEC-3).
    """
    return csv_safe(value)


def export_audience_csv(report: AudienceReport) -> str:
    """Render the report as franchise-friendly CSV.

    The CSV has a one-row header section (period + totals) then a per-channel
    section + a per-asset top-N section + a per-dimension breakdown section,
    each delineated by a blank row + a section title row. This is the shape
    most franchise-authority spreadsheet importers want.

    Returns a string. Callers add an explicit BOM when writing to a file the
    target operator opens in Excel (``"\\ufeff" + csv``).
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    writer.writerow(["section", "audience-report-header"])
    writer.writerow(["period_kind", report.period_kind])
    writer.writerow(["period_start", report.period_start.isoformat()])
    writer.writerow(["period_end", report.period_end.isoformat()])
    writer.writerow(["generated_at", report.generated_at.isoformat()])
    writer.writerow(["range_days", str(report.range_days)])
    writer.writerow(["total_views", str(report.total_views)])
    writer.writerow(["total_view_seconds", str(report.total_view_seconds)])
    writer.writerow(["privacy_boundary", report.privacy_boundary])
    writer.writerow([])

    writer.writerow(["section", "channel-summaries"])
    writer.writerow(
        [
            "channel_id",
            "total_views",
            "total_view_seconds",
            "average_view_seconds",
            "peak_concurrent_viewers",
            "average_concurrent_viewers",
            "unique_assets",
            "samples",
        ]
    )
    for row in report.channel_summaries:
        writer.writerow(
            [
                _csv_safe(row.channel_id),
                row.total_views,
                row.total_view_seconds,
                f"{row.average_view_seconds:.2f}",
                row.peak_concurrent_viewers,
                f"{row.average_concurrent_viewers:.2f}",
                row.unique_assets,
                row.samples,
            ]
        )
    writer.writerow([])

    writer.writerow(["section", "top-assets"])
    writer.writerow(
        [
            "content_id",
            "channel_id",
            "total_views",
            "total_view_seconds",
            "average_view_seconds",
            "first_seen_day",
            "last_seen_day",
        ]
    )
    for asset_row in report.top_assets:
        writer.writerow(
            [
                _csv_safe(asset_row.content_id),
                _csv_safe(asset_row.channel_id) if asset_row.channel_id else "",
                asset_row.total_views,
                asset_row.total_view_seconds,
                f"{asset_row.average_view_seconds:.2f}",
                asset_row.first_seen_day.isoformat() if asset_row.first_seen_day else "",
                asset_row.last_seen_day.isoformat() if asset_row.last_seen_day else "",
            ]
        )
    writer.writerow([])

    writer.writerow(["section", "dimensions"])
    writer.writerow(["dimension", "key", "count"])
    for dim_list in (
        report.geography,
        report.device_breakdown,
        report.platform_breakdown,
        report.caption_usage,
        report.audio_usage,
        report.subscription_growth,
        report.podcast_downloads,
    ):
        for dimension_row in dim_list:
            writer.writerow(
                [dimension_row.dimension, _csv_safe(dimension_row.key), dimension_row.count]
            )

    return buf.getvalue()


def export_audience_xml(report: AudienceReport) -> str:
    """Render the report as XML for franchise-authority importers."""
    root = ET.Element("audience-report")
    ET.SubElement(root, "period-kind").text = report.period_kind
    ET.SubElement(root, "period-start").text = report.period_start.isoformat()
    ET.SubElement(root, "period-end").text = report.period_end.isoformat()
    ET.SubElement(root, "generated-at").text = report.generated_at.isoformat()
    ET.SubElement(root, "range-days").text = str(report.range_days)
    ET.SubElement(root, "total-views").text = str(report.total_views)
    ET.SubElement(root, "total-view-seconds").text = str(report.total_view_seconds)
    ET.SubElement(root, "privacy-boundary").text = report.privacy_boundary

    channels = ET.SubElement(root, "channel-summaries")
    for row in report.channel_summaries:
        c = ET.SubElement(channels, "channel")
        ET.SubElement(c, "channel-id").text = row.channel_id
        ET.SubElement(c, "total-views").text = str(row.total_views)
        ET.SubElement(c, "total-view-seconds").text = str(row.total_view_seconds)
        ET.SubElement(c, "average-view-seconds").text = f"{row.average_view_seconds:.2f}"
        ET.SubElement(c, "peak-concurrent-viewers").text = str(row.peak_concurrent_viewers)
        ET.SubElement(
            c, "average-concurrent-viewers"
        ).text = f"{row.average_concurrent_viewers:.2f}"
        ET.SubElement(c, "unique-assets").text = str(row.unique_assets)
        ET.SubElement(c, "samples").text = str(row.samples)

    assets = ET.SubElement(root, "top-assets")
    for asset_row in report.top_assets:
        a = ET.SubElement(assets, "asset")
        ET.SubElement(a, "content-id").text = asset_row.content_id
        if asset_row.channel_id:
            ET.SubElement(a, "channel-id").text = asset_row.channel_id
        ET.SubElement(a, "total-views").text = str(asset_row.total_views)
        ET.SubElement(a, "total-view-seconds").text = str(asset_row.total_view_seconds)
        ET.SubElement(a, "average-view-seconds").text = f"{asset_row.average_view_seconds:.2f}"
        if asset_row.first_seen_day:
            ET.SubElement(a, "first-seen-day").text = asset_row.first_seen_day.isoformat()
        if asset_row.last_seen_day:
            ET.SubElement(a, "last-seen-day").text = asset_row.last_seen_day.isoformat()

    for dim_list, parent_tag in (
        (report.geography, "geography"),
        (report.device_breakdown, "device-breakdown"),
        (report.platform_breakdown, "platform-breakdown"),
        (report.caption_usage, "caption-usage"),
        (report.audio_usage, "audio-usage"),
        (report.subscription_growth, "subscription-growth"),
        (report.podcast_downloads, "podcast-downloads"),
    ):
        if not dim_list:
            continue
        section = ET.SubElement(root, parent_tag)
        for dimension_row in dim_list:
            r = ET.SubElement(section, "row")
            ET.SubElement(r, "dimension").text = dimension_row.dimension
            ET.SubElement(r, "key").text = dimension_row.key
            ET.SubElement(r, "count").text = str(dimension_row.count)

    return ET.tostring(root, encoding="unicode")


__all__ = [
    "AudienceAssetSummary",
    "AudienceChannelSummary",
    "AudienceDimensionRow",
    "AudiencePeriodKind",
    "AudienceReport",
    "build_audience_report",
    "export_audience_csv",
    "export_audience_xml",
]
