# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S14 audience-measurement exports — rollup CSV + board-ready PDF.

CSV is a flat rollup dump (one row per bucket per subject), generated
in-process with no new dependency — matches the incumbent PEG platform's CSV
export (S14 §6.4). PDF uses ``reportlab`` (already a pinned project
dependency — see ``civiccast/underwriting/service.py::export_affidavit_pdf``
for the sibling pattern this follows): a plain, deterministic multi-page
PDF, not a PDF/A-3 conformant signed record (that's ``civiccast/records/
pdfa.py``, a different surface entirely).
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from civiccast.analytics.models import AnalyticsReport, ViewershipRollupPoint

__all__ = ["export_board_pdf", "export_rollups_csv"]


def export_rollups_csv(rollups: list[ViewershipRollupPoint]) -> str:
    """Flat CSV dump: one row per rollup bucket per subject."""

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "stream_type",
            "bucket_kind",
            "bucket_start",
            "subject_id",
            "viewer_count",
            "time_viewed_seconds",
            "peak_concurrent",
            "avg_concurrent",
            "samples",
        ]
    )
    for point in rollups:
        writer.writerow(
            [
                point.stream_type,
                point.bucket_kind,
                point.bucket_start.isoformat(),
                point.subject_id,
                point.viewer_count,
                point.time_viewed_seconds,
                point.peak_concurrent if point.peak_concurrent is not None else "",
                point.avg_concurrent if point.avg_concurrent is not None else "",
                point.samples,
            ]
        )
    return buffer.getvalue()


def export_board_pdf(
    report: AnalyticsReport,
    *,
    station_label: str,
    range_start: datetime,
    range_end: datetime,
    include_totals: bool = True,
    include_top_content: bool = True,
    include_yoy: bool = True,
    include_live_peaks: bool = True,
) -> bytes:
    """Render the board-ready PDF: cover -> totals -> top content -> YoY -> live peaks.

    The returned bytes always start with ``b"%PDF"`` (a fast smoke-test
    contract, matching every other reportlab export in this repo). Default
    OFF nothing that is requested — every section the caller asks for is
    rendered; a section with no data prints an honest "no data for this
    period" line rather than being silently skipped, so a sparse report
    still reads as complete rather than broken.
    """

    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=letter, pageCompression=0, invariant=1)
    pdf.setTitle(f"{station_label} — audience report")
    pdf.setAuthor("CivicCast")
    pdf.setSubject("Board-ready audience measurement report")

    _page_width, page_height = letter
    left_margin = 54.0
    right_margin = _page_width - 54.0
    top_margin = page_height - 54.0
    line_height = 14.0
    body_bottom = 72.0

    y = top_margin

    def _new_page(heading: str) -> None:
        nonlocal y
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(left_margin, top_margin, heading)
        y = top_margin - (line_height + 10)

    def _ensure_room(needed: float = line_height) -> None:
        nonlocal y
        if y - needed < body_bottom:
            pdf.showPage()
            y = top_margin

    def _heading(text: str) -> None:
        nonlocal y
        _ensure_room(line_height * 2)
        pdf.setFont("Helvetica-Bold", 12)
        pdf.drawString(left_margin, y, text)
        y -= line_height + 4
        pdf.setFont("Helvetica", 10)

    def _line(text: str) -> None:
        nonlocal y
        _ensure_room()
        pdf.drawString(left_margin, y, text)
        y -= line_height

    # -- cover -----------------------------------------------------------
    _new_page(f"{station_label} — audience report")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(left_margin, y, f"Reporting period: {range_start.date()} to {range_end.date()}")
    y -= line_height
    pdf.drawString(left_margin, y, f"Generated: {report.generated_at.isoformat()}")
    y -= line_height * 2
    pdf.setFont("Helvetica-Oblique", 8)
    pdf.drawString(
        left_margin,
        y,
        "Streaming-only measurement — does not include linear/cable (QAM) viewership.",
    )
    y -= line_height * 0.8
    pdf.drawString(
        left_margin,
        y,
        "Viewer Count is a play count, not unique humans. Live concurrency is estimated,",
    )
    y -= line_height * 0.8
    pdf.drawString(left_margin, y, "not CDN-authoritative.")
    y -= line_height * 1.5
    pdf.setFont("Helvetica", 10)

    # -- totals ------------------------------------------------------------
    if include_totals:
        _heading("Totals")
        total_views = sum(p.views for p in report.asset_views)
        total_seconds = sum(p.view_seconds for p in report.asset_views)
        total_hours = round(total_seconds / 3600, 1)
        highest_peak = max(
            (p.peak_concurrent_viewers for p in report.live_concurrent_viewers), default=0
        )
        _line(f"Total reach (VOD plays): {total_views}")
        _line(f"Total watch-time: {total_hours} hour(s)")
        _line(f"Highest live peak-concurrent: {highest_peak}")
        y -= line_height * 0.5

    # -- top content ---------------------------------------------------------
    if include_top_content:
        _heading("Top VOD content (by viewer count)")
        by_content: dict[str, int] = {}
        seconds_by_content: dict[str, int] = {}
        for point in report.asset_views:
            by_content[point.content_id] = by_content.get(point.content_id, 0) + point.views
            seconds_by_content[point.content_id] = (
                seconds_by_content.get(point.content_id, 0) + point.view_seconds
            )
        top = sorted(by_content.items(), key=lambda item: item[1], reverse=True)[:10]
        if not top:
            _line("No VOD viewership data for this period.")
        for content_id, views in top:
            seconds = seconds_by_content.get(content_id, 0)
            _line(f"{content_id[:60]:<60}  {views} view(s)  /  {round(seconds / 60)} min")
        y -= line_height * 0.5

    # -- year-over-year ------------------------------------------------------
    if include_yoy:
        _heading("Year-over-year")
        if not report.year_over_year:
            _line("No year-over-year data available yet.")
        for yoy_point in report.year_over_year:
            if yoy_point.delta_pct is None:
                trend = "no prior-year data"
            else:
                sign = "+" if yoy_point.delta_pct >= 0 else ""
                trend = f"{sign}{yoy_point.delta_pct}%"
            _line(
                f"{yoy_point.metric}: {yoy_point.current_period} "
                f"(prior year: {yoy_point.prior_period}, {trend})"
            )
        y -= line_height * 0.5

    # -- live-event peaks ------------------------------------------------------
    if include_live_peaks:
        _heading("Live-event peaks")
        if not report.live_concurrent_viewers:
            _line("No live viewership data for this period.")
        peaks = sorted(
            report.live_concurrent_viewers,
            key=lambda p: p.peak_concurrent_viewers,
            reverse=True,
        )[:10]
        for live_point in peaks:
            _line(
                f"{live_point.channel_id[:40]:<40}  {live_point.day}  "
                f"peak {live_point.peak_concurrent_viewers}  avg {live_point.average_concurrent_viewers}"
            )

    pdf.setFont("Helvetica-Oblique", 7)
    pdf.drawRightString(
        right_margin,
        body_bottom - 24,
        "Generated by CivicCast — self-hosted, no paid-cloud-CDN dependency.",
    )

    pdf.showPage()
    pdf.save()
    return buf.getvalue()
