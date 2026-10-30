# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Builders for v0.10 idle page and emergency overlay proof states."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from civiccast.cg.models import (
    CgBulletinQueue,
    CgBulletinSubmission,
    CgFeedAdapter,
    CgFeedCatalog,
    CgFeedItem,
    CgHlsRenderPlan,
    CgOverlayContract,
    CgPortalDisplay,
    CgTemplate,
    CgTemplateLibrary,
    CgTemplateZone,
    CgZone,
    EmergencyOverlay,
    IdlePage,
    MultiZoneCgSnapshot,
)


def build_template_library(channel_id: str = "public") -> CgTemplateLibrary:
    """Return configurable CG templates available to a channel."""

    templates = [
        CgTemplate(
            template_id="standard-community-board",
            label="Standard community board",
            regions=[
                CgTemplateZone(region="main", zone_kind="primary", order=0),
                CgTemplateZone(region="lower", zone_kind="ticker", order=1),
                CgTemplateZone(region="side", zone_kind="schedule", order=2),
                CgTemplateZone(region="bug", zone_kind="logo", order=3),
                CgTemplateZone(region="background", zone_kind="audio", order=4),
                CgTemplateZone(region="lower", zone_kind="alert", order=5),
            ],
        ),
        CgTemplate(
            template_id="live-lower-banner",
            label="Live lower banner",
            regions=[
                CgTemplateZone(region="main", zone_kind="primary", order=0),
                CgTemplateZone(region="lower", zone_kind="ticker", order=1),
                CgTemplateZone(region="side", zone_kind="schedule", order=2),
                CgTemplateZone(region="bug", zone_kind="logo", order=3),
                CgTemplateZone(region="background", zone_kind="audio", order=4),
                CgTemplateZone(region="lower", zone_kind="alert", order=5),
            ],
        ),
        CgTemplate(
            template_id="schedule-forward-board",
            label="Schedule forward board",
            regions=[
                CgTemplateZone(region="side", zone_kind="primary", order=0),
                CgTemplateZone(region="main", zone_kind="schedule", order=1),
                CgTemplateZone(region="lower", zone_kind="ticker", order=2),
                CgTemplateZone(region="bug", zone_kind="logo", order=3),
                CgTemplateZone(region="background", zone_kind="audio", order=4),
            ],
        ),
    ]
    return CgTemplateLibrary(
        channel_id=channel_id,
        active_template_id="standard-community-board",
        templates=templates,
        proof_boundary="cg-template-library-to-visual-layout-editor",
    )


def build_idle_page(channel_id: str = "public") -> IdlePage:
    """Return a deterministic between-stream idle page."""

    return IdlePage(
        channel_id=channel_id,
        title="CivicCast is ready",
        message="No meeting is live right now. The next scheduled broadcast will start here.",
        next_broadcast_label="Next broadcast: Public Meetings test broadcast",
        action_label="View published recordings",
        action_url="#/recordings",
    )


def build_emergency_overlay(
    *,
    overlay_id: str = "test-emergency-overlay",
    severity: str = "warning",
) -> EmergencyOverlay:
    """Return a deterministic emergency overlay with cellular fallback enabled."""

    return EmergencyOverlay(
        overlay_id=overlay_id,
        severity=severity,  # type: ignore[arg-type]
        title="Emergency notice",
        message="An emergency notice is active for this broadcast area.",
        instructions="Follow local emergency guidance and check official updates.",
        cellular_fallback_enabled=True,
    )


def build_multi_zone_snapshot(
    channel_id: str = "public",
    *,
    template_id: str | None = None,
) -> MultiZoneCgSnapshot:
    """Return a deterministic 24/7 multi-zone bulletin-board snapshot."""

    generated_at = datetime.now(UTC).replace(microsecond=0)
    template_library = build_template_library(channel_id)
    active_template_id = template_id or template_library.active_template_id
    template = next(
        template
        for template in template_library.templates
        if template.template_id == active_template_id
    )
    return MultiZoneCgSnapshot(
        snapshot_id=f"{channel_id}-community-board",
        generated_at=generated_at,
        channel_id=channel_id,
        template=template,
        zones=[
            CgZone(
                zone_id="primary-program",
                kind="primary",
                title="Now showing",
                source="schedule",
                content={
                    "headline": "Community programming",
                    "body": "Watch live meetings, public announcements, and recent replays.",
                },
                refresh_seconds=300,
            ),
            CgZone(
                zone_id="news-ticker",
                kind="ticker",
                title="Community updates",
                source="rss",
                content={"items": ["Library board meets tonight", "Trail work begins Monday"]},
                refresh_seconds=600,
            ),
            CgZone(
                zone_id="coming-up",
                kind="schedule",
                title="Coming up next",
                source="schedule",
                content={
                    "items": [
                        {"time": "18:00", "title": "City Council"},
                        {"time": "20:00", "title": "Planning Board"},
                    ]
                },
                refresh_seconds=300,
            ),
            CgZone(
                zone_id="station-logo",
                kind="logo",
                title="Station identity",
                source="station-branding",
                content={"logo_text": "PUBLIC", "color": "#2458A6"},
            ),
            CgZone(
                zone_id="background-audio",
                kind="audio",
                title="Background audio",
                source="operator",
                content={"track": "community-calendar-bed", "duck_under_alerts": True},
            ),
            CgZone(
                zone_id="emergency-alert",
                kind="alert",
                title="Emergency overlay",
                source="emergency",
                content={"active": False, "aria_live": "assertive"},
                refresh_seconds=60,
            ),
        ],
        hls_render_path=None,
        portal_render_path=f"/api/public/cg/channels/{channel_id}/snapshot",
        proof_boundary="software-cg-snapshot-to-portal-layout",
    )


def build_feed_catalog(channel_id: str = "public") -> CgFeedCatalog:
    """Return deterministic dynamic feed adapters for CG zones."""

    generated_at = datetime.now(UTC).replace(microsecond=0)
    return CgFeedCatalog(
        generated_at=generated_at,
        channel_id=channel_id,
        adapters=[
            CgFeedAdapter(
                adapter_id="community-news-rss",
                kind="rss",
                label="Community news RSS",
                source_url="https://example.invalid/community-news.xml",
                trust_tier="partner_curated",
                refresh_seconds=900,
                target_zone_kinds=["ticker"],
                items=[
                    CgFeedItem(
                        item_id="library-board",
                        title="Library board meets tonight",
                        summary="Public meeting coverage begins at 6 PM.",
                        url="https://example.invalid/library-board",
                    )
                ],
            ),
            CgFeedAdapter(
                adapter_id="community-calendar-ical",
                kind="ical",
                label="Community calendar",
                source_url="https://example.invalid/calendar.ics",
                trust_tier="partner_curated",
                refresh_seconds=1800,
                target_zone_kinds=["schedule"],
                items=[
                    CgFeedItem(
                        item_id="planning-board",
                        title="Planning Board",
                        summary="Upcoming meeting",
                        starts_at=generated_at,
                    )
                ],
            ),
            CgFeedAdapter(
                adapter_id="weather-alerts",
                kind="weather",
                label="Weather alerts",
                source_url="https://example.invalid/weather.json",
                trust_tier="operator_curated",
                refresh_seconds=300,
                target_zone_kinds=["alert", "ticker"],
                items=[
                    CgFeedItem(
                        item_id="clear-weather",
                        title="No active weather alerts",
                        summary="Normal community bulletin rotation is active.",
                    )
                ],
            ),
            CgFeedAdapter(
                adapter_id="permitted-social",
                kind="social",
                label="Permitted civic social feed",
                source_url="https://example.invalid/social.json",
                trust_tier="public_permitted",
                refresh_seconds=1200,
                target_zone_kinds=["ticker"],
                items=[
                    CgFeedItem(
                        item_id="parks-update",
                        title="Parks department posted a trail update",
                        summary="Imported only when platform terms permit reuse.",
                    )
                ],
            ),
        ],
        proof_boundary="configured-feed-adapters-to-approved-cg-zone-items",
    )


def build_bulletin_queue(channel_id: str = "public") -> CgBulletinQueue:
    """Return deterministic community bulletin submissions and approved CG items."""

    generated_at = datetime.now(UTC).replace(microsecond=0)
    accepted = CgBulletinSubmission(
        submission_id="arts-fair",
        organization="Community Arts Council",
        submitter_label="Arts Council staff",
        title="Community Arts Fair",
        message="Community Arts Fair runs Saturday from 10 AM to 4 PM downtown.",
        target_zone_kind="ticker",
        state="scheduled",
        requested_start=generated_at,
        requested_end=generated_at + timedelta(hours=2),
        approved_by_operator="operator",
    )
    needs_changes = CgBulletinSubmission(
        submission_id="missing-date",
        organization="Neighborhood Group",
        submitter_label="Neighborhood volunteer",
        title="Neighborhood cleanup",
        message="Join the cleanup.",
        target_zone_kind="primary",
        state="needs_changes",
        moderation_notes="Add date, time, and location before scheduling.",
    )
    return CgBulletinQueue(
        generated_at=generated_at,
        channel_id=channel_id,
        submissions=[accepted, needs_changes],
        approved_zone_items=[
            CgZone(
                zone_id="bulletin-arts-fair",
                kind="ticker",
                title=accepted.title,
                source="community-submission",
                content={
                    "submission_id": accepted.submission_id,
                    "organization": accepted.organization,
                    "message": accepted.message,
                },
                refresh_seconds=900,
                approved=True,
            )
        ],
        proof_boundary="community-submission-queue-to-approved-cg-zone-items",
    )


def build_hls_render_plan(channel_id: str = "public") -> CgHlsRenderPlan:
    """Return the software CG HLS render plan for a channel."""

    return CgHlsRenderPlan(
        channel_id=channel_id,
        snapshot_url=f"/api/public/cg/channels/{channel_id}/snapshot",
        manifest_url=None,
        segment_pattern=None,
        target_duration_seconds=6,
        linear_overlay_contract_url=f"/api/public/cg/channels/{channel_id}/overlay-contract",
        proof_boundary="standalone-cg-video-unavailable",
    )


def build_hls_manifest(channel_id: str = "public") -> str:
    """Refuse to manufacture a manifest when no standalone encoder writes segments."""
    raise RuntimeError(build_hls_render_plan(channel_id).unavailable_reason)


def build_overlay_contract(channel_id: str = "public") -> CgOverlayContract:
    """Return the linear-channel overlay contract for CG renderers."""

    snapshot = build_multi_zone_snapshot(channel_id)
    return CgOverlayContract(
        channel_id=channel_id,
        snapshot_url=snapshot.portal_render_path,
        safe_area_percent=5,
        regions=snapshot.template.regions,
        zone_count=len(snapshot.zones),
        proof_boundary="approved-cg-zones-to-linear-overlay-contract",
    )


def build_portal_display(
    channel_id: str = "public",
    *,
    template_id: str | None = None,
) -> CgPortalDisplay:
    """Return the complete portal display contract for the current CG board."""

    return CgPortalDisplay(
        channel_id=channel_id,
        snapshot=build_multi_zone_snapshot(channel_id, template_id=template_id),
        template_library=build_template_library(channel_id),
        feed_catalog=build_feed_catalog(channel_id),
        approved_bulletins=build_bulletin_queue(channel_id),
        render_plan=build_hls_render_plan(channel_id),
        overlay_contract=build_overlay_contract(channel_id),
        proof_boundary="cg-template-feeds-bulletins-to-portal-display",
    )
