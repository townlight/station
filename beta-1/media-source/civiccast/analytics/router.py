# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Staff reporting routes for aggregate analytics."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from civiccast.analytics.audience_reports import (
    AudienceReport,
    _period_bounds,
    build_audience_report,
    export_audience_csv,
    export_audience_xml,
)
from civiccast.analytics.exports import export_board_pdf, export_rollups_csv
from civiccast.analytics.models import AnalyticsReport, ViewershipRollupPoint
from civiccast.analytics.store import AnalyticsStoreProtocol, cast_analytics_store
from civiccast.auth.roles import ALL_OPERATOR_ROLES, require_any_role
from civiccast.platform.stores import resolve_app_store

staff_router = APIRouter(prefix="/api/staff/analytics", tags=["staff", "analytics"])

# S14 spec §4: analytics is read-only diagnostic + publishing-impact data.
# support_admin reads/diagnoses; publish_operator owns published content and
# needs the numbers that justify it. No new role is invented.
_ANALYTICS_READ = ("support_admin", "publish_operator")
_ANALYTICS_READ_EXTRA = {"x-required-roles": list(_ANALYTICS_READ)}


def get_analytics_store(request: Request) -> AnalyticsStoreProtocol:
    return cast_analytics_store(
        resolve_app_store(request, "analytics_store", surface="Analytics store")
    )


@staff_router.get(
    "/reports/overview",
    response_model=AnalyticsReport,
    summary="Read aggregate-only station analytics report",
    dependencies=[Depends(require_any_role(*_ANALYTICS_READ))],
    openapi_extra=_ANALYTICS_READ_EXTRA,
)
def read_analytics_report(
    range_days: Annotated[int, Query(ge=1, le=366)] = 30,
    stream_type: Annotated[Literal["vod", "live", "all"], Query()] = "all",
    metric: Annotated[
        Literal["viewer_count", "time_viewed", "peak_concurrent"], Query()
    ] = "viewer_count",
    store: AnalyticsStoreProtocol = Depends(get_analytics_store),
) -> AnalyticsReport:
    """``stream_type``/``metric`` steer the dashboard's toolbar (S14 §5 panel 1).

    ``metric`` does not change which fields are present (every rollup point
    already carries viewer_count/time_viewed_seconds/peak_concurrent); it is
    accepted here so the toolbar's full query contract round-trips, and the
    ``/rollups`` endpoint is where it actually orders/limits results.
    """

    report = store.report(range_days=range_days)
    if stream_type == "vod":
        report = report.model_copy(update={"live_rollups": []})
    elif stream_type == "live":
        report = report.model_copy(update={"vod_rollups": []})
    return report


@staff_router.get(
    "/reports/audience",
    response_model=AudienceReport,
    summary=(
        "Build a packaged audience report — the franchise-authority deliverable. "
        "Use ?format=csv or ?format=xml for export-ready downloads."
    ),
    dependencies=[Depends(require_any_role(*ALL_OPERATOR_ROLES))],
)
def read_audience_report(
    kind: Annotated[
        Literal["weekly", "monthly", "custom"], Query(description="period preset")
    ] = "monthly",
    period_start: Annotated[
        date | None, Query(description="ISO date; required for kind=custom")
    ] = None,
    period_end: Annotated[date | None, Query(description="ISO date; defaults to today UTC")] = None,
    range_days: Annotated[int, Query(ge=1, le=366)] = 30,
    top_assets_limit: Annotated[int, Query(ge=1, le=200)] = 25,
    fmt: Annotated[Literal["json", "csv", "xml"], Query(alias="format")] = "json",
    store: AnalyticsStoreProtocol = Depends(get_analytics_store),
) -> AudienceReport | Response:
    """Build + serve the franchise-deliverable audience report.

    The ``json`` response is the default and returns the full ``AudienceReport``
    pydantic model. ``csv`` and ``xml`` return a download-shaped Response with
    the appropriate Content-Type + Content-Disposition so the operator's
    browser saves the file with a sensible name.
    """
    try:
        period_start_bound, _period_end_bound = _period_bounds(
            kind, period_start=period_start, period_end=period_end
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # range_days is a store-pull *width*; the resolved period can reach
    # further back than that (e.g. a historical kind=custom range), so widen
    # the store pull to cover it — build_audience_report then filters the
    # per-day points down to the exact resolved period.
    today = datetime.now(UTC).date()
    lookback_days = min(max(range_days, (today - period_start_bound).days, 1), 366)

    try:
        base = store.report(range_days=lookback_days)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        report = build_audience_report(
            base,
            kind=kind,
            period_start=period_start,
            period_end=period_end,
            top_assets_limit=top_assets_limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if fmt == "json":
        return report

    period = f"{report.period_start.isoformat()}-to-{report.period_end.isoformat()}"
    if fmt == "csv":
        body = "﻿" + export_audience_csv(report)  # BOM for Excel
        return Response(
            content=body,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (f'attachment; filename="audience-report-{period}.csv"')
            },
        )
    # xml
    body = export_audience_xml(report)
    return Response(
        content=body,
        media_type="application/xml",
        headers={"Content-Disposition": (f'attachment; filename="audience-report-{period}.xml"')},
    )


class RollupStats(BaseModel):
    """Summary stats accompanying a ``/rollups`` read (S14 §4)."""

    model_config = ConfigDict(extra="forbid")

    total_viewer_count: int = Field(ge=0)
    total_time_viewed_seconds: int = Field(ge=0)
    peak_concurrent: int | None = None


class RollupsResponse(BaseModel):
    """The dashboard's bar-chart + time-series + stats panels read this."""

    model_config = ConfigDict(extra="forbid")

    rollups: list[ViewershipRollupPoint]
    stats: RollupStats


def _default_bucket_for(stream_type: Literal["vod", "live"]) -> Literal["day", "halfhour"]:
    return "day" if stream_type == "vod" else "halfhour"


@staff_router.get(
    "/rollups",
    response_model=RollupsResponse,
    summary="Read pre-aggregated viewership rollups (bar chart + time-series + stats)",
    dependencies=[Depends(require_any_role(*_ANALYTICS_READ))],
    openapi_extra=_ANALYTICS_READ_EXTRA,
)
def read_rollups(
    stream_type: Annotated[Literal["vod", "live"], Query()],
    bucket: Annotated[Literal["day", "halfhour", "hour"] | None, Query()] = None,
    range_days: Annotated[int, Query(ge=1, le=366, alias="range_days")] = 30,
    top_n: Annotated[int, Query(ge=1, le=100)] = 10,
    store: AnalyticsStoreProtocol = Depends(get_analytics_store),
) -> RollupsResponse:
    """VOD is always ``bucket=day``; Live defaults to ``halfhour`` (hourly for
    a single-day range is the dashboard's own display choice — pass
    ``bucket=hour`` explicitly to get it; both granularities are persisted).
    """

    resolved_bucket = bucket or _default_bucket_for(stream_type)
    if stream_type == "vod" and resolved_bucket != "day":
        raise HTTPException(status_code=422, detail="stream_type=vod only supports bucket=day.")
    if stream_type == "live" and resolved_bucket == "day":
        raise HTTPException(
            status_code=422, detail="stream_type=live supports bucket=halfhour or bucket=hour."
        )

    points = store.rollups(
        stream_type=stream_type, bucket_kind=resolved_bucket, range_days=range_days
    )

    total_viewer_count = sum(p.viewer_count for p in points)
    total_time_viewed_seconds = sum(p.time_viewed_seconds for p in points)
    peaks = [p.peak_concurrent for p in points if p.peak_concurrent is not None]

    # Top-N by viewer_count for the bar-chart panel; the caller still gets
    # every bucket for the time-series panel when top_n >= len(points).
    top_subjects = {
        subject
        for subject, _ in sorted(
            _aggregate_by_subject(points).items(), key=lambda item: item[1], reverse=True
        )[:top_n]
    }
    filtered = [p for p in points if p.subject_id in top_subjects] if top_subjects else points

    return RollupsResponse(
        rollups=sorted(filtered, key=lambda p: (p.bucket_start, p.subject_id)),
        stats=RollupStats(
            total_viewer_count=total_viewer_count,
            total_time_viewed_seconds=total_time_viewed_seconds,
            peak_concurrent=max(peaks) if peaks else None,
        ),
    )


def _aggregate_by_subject(points: list[ViewershipRollupPoint]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for point in points:
        totals[point.subject_id] = totals.get(point.subject_id, 0) + point.viewer_count
    return totals


@staff_router.get(
    "/export.csv",
    summary="Download a flat rollup CSV (PEG automation coverage floor)",
    dependencies=[Depends(require_any_role(*_ANALYTICS_READ))],
    openapi_extra=_ANALYTICS_READ_EXTRA,
    responses={200: {"content": {"text/csv": {}}}},
)
def export_rollups(
    stream_type: Annotated[Literal["vod", "live"], Query()],
    bucket: Annotated[Literal["day", "halfhour", "hour"] | None, Query()] = None,
    range_days: Annotated[int, Query(ge=1, le=366)] = 30,
    store: AnalyticsStoreProtocol = Depends(get_analytics_store),
) -> Response:
    rollups_response = read_rollups(
        stream_type=stream_type, bucket=bucket, range_days=range_days, top_n=1000, store=store
    )
    body = "﻿" + export_rollups_csv(rollups_response.rollups)  # BOM for Excel
    filename = f"analytics-rollups-{stream_type}-{range_days}d.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class BoardPdfInclude(BaseModel):
    """Which sections the board PDF renders (all default on — S14 §5)."""

    model_config = ConfigDict(extra="forbid")

    totals: bool = True
    top_content: bool = True
    yoy: bool = True
    live_peaks: bool = True


class BoardPdfRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    range_start: datetime
    range_end: datetime
    include: BoardPdfInclude = Field(default_factory=BoardPdfInclude)
    station_label: Annotated[str, Field(min_length=1, max_length=160)] = "CivicCast station"


@staff_router.post(
    "/reports/board-pdf",
    summary="Generate the one-click board-ready PDF (totals / top content / YoY / live peaks)",
    dependencies=[Depends(require_any_role(*_ANALYTICS_READ))],
    openapi_extra=_ANALYTICS_READ_EXTRA,
    responses={200: {"content": {"application/pdf": {}}}},
)
def generate_board_pdf(
    request: Request,
    body: Annotated[BoardPdfRequest, Body()],
    store: AnalyticsStoreProtocol = Depends(get_analytics_store),
) -> Response:
    """Renders the PDF and persists an ``AnalyticsReportSnapshot`` (durable
    storage only — the JSON-backed store's ``save_snapshot`` is a no-op, so
    the PDF still downloads on an ephemeral/JSON-mode station; it just is
    not reproducible from a stored snapshot later).
    """

    if body.range_end <= body.range_start:
        raise HTTPException(status_code=422, detail="range_end must be after range_start.")
    range_days = min(max((body.range_end - body.range_start).days, 1), 366)
    report = store.report(range_days=range_days)

    pdf_bytes = export_board_pdf(
        report,
        station_label=body.station_label,
        range_start=body.range_start,
        range_end=body.range_end,
        include_totals=body.include.totals,
        include_top_content=body.include.top_content,
        include_yoy=body.include.yoy,
        include_live_peaks=body.include.live_peaks,
    )

    identity = getattr(request.state, "operator_identity", None)
    created_by = getattr(identity, "operator_id", None) or "unknown"
    snapshot_id = f"board-pdf-{uuid.uuid4().hex}"
    store.save_snapshot(
        snapshot_id=snapshot_id,
        generated_at=report.generated_at,
        range_start=body.range_start,
        range_end=body.range_end,
        report_json=report.model_dump_json(),
        created_by=str(created_by),
    )

    filename = f"audience-report-{body.range_start.date()}-to-{body.range_end.date()}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
