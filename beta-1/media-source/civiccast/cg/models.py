# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Closed contracts for v0.10 character-generator surfaces."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ZoneKind = Literal["primary", "ticker", "schedule", "logo", "sponsor", "audio", "alert"]
TemplateRegion = Literal["main", "lower", "side", "bug", "background"]
FeedKind = Literal["rss", "ical", "caldav", "weather", "social"]
FeedTrustTier = Literal["operator_curated", "partner_curated", "public_permitted"]
BulletinState = Literal["submitted", "needs_changes", "accepted", "declined", "scheduled"]


class IdlePage(BaseModel):
    """Between-stream resident idle page state."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    title: Annotated[str, Field(min_length=1, max_length=160)]
    message: Annotated[str, Field(min_length=1)]
    next_broadcast_label: Annotated[str, Field(min_length=1)]
    action_label: Annotated[str, Field(min_length=1)]
    action_url: Annotated[str, Field(min_length=1)]


class EmergencyOverlay(BaseModel):
    """Emergency-notification overlay state for the resident player."""

    model_config = ConfigDict(extra="forbid")

    overlay_id: Annotated[str, Field(min_length=1, max_length=120)]
    severity: Literal["watch", "warning", "emergency"]
    title: Annotated[str, Field(min_length=1, max_length=160)]
    message: Annotated[str, Field(min_length=1)]
    instructions: Annotated[str, Field(min_length=1)]
    cellular_fallback_enabled: bool
    aria_live: Literal["polite", "assertive"] = "assertive"


class CgTemplateZone(BaseModel):
    """One named region in a multi-zone CG template."""

    model_config = ConfigDict(extra="forbid")

    region: TemplateRegion
    zone_kind: ZoneKind
    order: Annotated[int, Field(ge=0, le=20)]


class CgTemplate(BaseModel):
    """Reusable visual layout contract for bulletin-board rendering."""

    model_config = ConfigDict(extra="forbid")

    template_id: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    regions: list[CgTemplateZone]

    @model_validator(mode="after")
    def _regions_are_unique(self) -> CgTemplate:
        keys = [(region.region, region.zone_kind) for region in self.regions]
        if len(keys) != len(set(keys)):
            raise ValueError("template regions must be unique")
        return self


class CgTemplateLibrary(BaseModel):
    """Available CG templates and the currently active template for a channel."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    active_template_id: Annotated[str, Field(min_length=1, max_length=120)]
    templates: list[CgTemplate]
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]

    @model_validator(mode="after")
    def _active_template_exists(self) -> CgTemplateLibrary:
        template_ids = {template.template_id for template in self.templates}
        if self.active_template_id not in template_ids:
            raise ValueError("active_template_id must exist in templates")
        return self


class CgZone(BaseModel):
    """One rendered content zone in the bulletin-board snapshot."""

    model_config = ConfigDict(extra="forbid")

    zone_id: Annotated[str, Field(min_length=1, max_length=120)]
    kind: ZoneKind
    title: Annotated[str | None, Field(default=None, max_length=160)] = None
    content: dict[str, Any] = Field(default_factory=dict)
    source: Annotated[str, Field(min_length=1, max_length=120)]
    refresh_seconds: Annotated[int | None, Field(default=None, gt=0, le=86400)] = None
    approved: bool = True


class CgFeedItem(BaseModel):
    """One normalized dynamic content item for CG zones."""

    model_config = ConfigDict(extra="forbid")

    item_id: Annotated[str, Field(min_length=1, max_length=120)]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    summary: Annotated[str | None, Field(default=None, max_length=500)] = None
    starts_at: datetime | None = None
    url: Annotated[str | None, Field(default=None, max_length=500)] = None
    approved: bool = True
    # CG depth (S18 gap 6): tags the feed fetcher assigns to an item; a zone
    # with allowed_tags only renders items carrying one of those tags. Transient
    # (feed items are fetched, not persisted), so no migration.
    tags: list[str] = Field(default_factory=list)


class CgFeedAdapter(BaseModel):
    """Configured dynamic source feeding one or more CG zones."""

    model_config = ConfigDict(extra="forbid")

    adapter_id: Annotated[str, Field(min_length=1, max_length=120)]
    kind: FeedKind
    label: Annotated[str, Field(min_length=1, max_length=160)]
    source_url: Annotated[str, Field(min_length=1, max_length=500)]
    trust_tier: FeedTrustTier
    refresh_seconds: Annotated[int, Field(gt=0, le=86400)]
    target_zone_kinds: list[ZoneKind]
    items: list[CgFeedItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _targets_and_items_are_safe(self) -> CgFeedAdapter:
        if not self.target_zone_kinds:
            raise ValueError("feed adapters require at least one target zone kind")
        if len(self.target_zone_kinds) != len(set(self.target_zone_kinds)):
            raise ValueError("target_zone_kinds must be unique")
        if any(not item.approved for item in self.items):
            raise ValueError("feed adapters can expose only approved items")
        if self.kind == "weather" and self.trust_tier == "public_permitted":
            raise ValueError("weather feeds must be operator or partner curated")
        return self


class CgFeedCatalog(BaseModel):
    """Dynamic feed catalog available to CG templates and zones."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    adapters: list[CgFeedAdapter]
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]

    @model_validator(mode="after")
    def _adapter_ids_unique(self) -> CgFeedCatalog:
        ids = [adapter.adapter_id for adapter in self.adapters]
        if len(ids) != len(set(ids)):
            raise ValueError("adapter_id values must be unique")
        return self


class CgBulletinSubmission(BaseModel):
    """Community bulletin submission controlled by operator approval."""

    model_config = ConfigDict(extra="forbid")

    submission_id: Annotated[str, Field(min_length=1, max_length=120)]
    organization: Annotated[str, Field(min_length=1, max_length=160)]
    submitter_label: Annotated[str, Field(min_length=1, max_length=160)]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    message: Annotated[str, Field(min_length=1, max_length=500)]
    target_zone_kind: Literal["primary", "ticker", "schedule"]
    state: BulletinState
    requested_start: datetime | None = None
    requested_end: datetime | None = None
    moderation_notes: Annotated[str | None, Field(default=None, max_length=500)] = None
    approved_by_operator: Annotated[str | None, Field(default=None, max_length=120)] = None

    @model_validator(mode="after")
    def _approval_state_is_explicit(self) -> CgBulletinSubmission:
        if (
            self.requested_end is not None
            and self.requested_start is not None
            and self.requested_end <= self.requested_start
        ):
            raise ValueError("requested_end must be greater than requested_start")
        if self.state in {"accepted", "scheduled"} and not self.approved_by_operator:
            raise ValueError("accepted and scheduled bulletins require approved_by_operator")
        if self.state in {"needs_changes", "declined"} and not self.moderation_notes:
            raise ValueError("needs_changes and declined bulletins require moderation_notes")
        return self


class CgBulletinQueue(BaseModel):
    """Operator-controlled queue for community bulletin-board submissions."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    submissions: list[CgBulletinSubmission]
    approved_zone_items: list[CgZone]
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]

    @model_validator(mode="after")
    def _approved_items_match_accepted_submissions(self) -> CgBulletinQueue:
        accepted_ids = {
            submission.submission_id
            for submission in self.submissions
            if submission.state in {"accepted", "scheduled"}
        }
        item_ids = {item.content.get("submission_id") for item in self.approved_zone_items}
        if not item_ids <= accepted_ids:
            raise ValueError("approved zone items must reference accepted or scheduled submissions")
        return self


class CgHlsRenderPlan(BaseModel):
    """Software CG-to-HLS render contract for streaming channel output."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    snapshot_url: Annotated[str, Field(min_length=1, max_length=500)]
    available: bool = False
    unavailable_reason: str = "Standalone CG video preview is unavailable. Preview the board layout here; watch channel graphics in the channel monitor."
    manifest_url: Annotated[str, Field(min_length=1, max_length=500)] | None
    segment_pattern: Annotated[str, Field(min_length=1, max_length=500)] | None
    target_duration_seconds: Annotated[int, Field(gt=0, le=60)]
    linear_overlay_contract_url: Annotated[str, Field(min_length=1, max_length=500)]
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]


class CgOverlayContract(BaseModel):
    """Contract used by linear-channel renderers to map CG zones to video output."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    snapshot_url: Annotated[str, Field(min_length=1, max_length=500)]
    format: Literal["json-overlay-v1"] = "json-overlay-v1"
    safe_area_percent: Annotated[int, Field(ge=0, le=20)]
    regions: list[CgTemplateZone]
    zone_count: Annotated[int, Field(ge=1, le=20)]
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]


class MultiZoneCgSnapshot(BaseModel):
    """24/7 CG bulletin-board state for portal and streaming renderers."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: Annotated[str, Field(min_length=1, max_length=120)]
    generated_at: datetime
    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    template: CgTemplate
    zones: list[CgZone]
    hls_render_path: Annotated[str, Field(min_length=1, max_length=500)] | None
    portal_render_path: Annotated[str, Field(min_length=1, max_length=500)]
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]

    @model_validator(mode="after")
    def _required_zones_present(self) -> MultiZoneCgSnapshot:
        zone_ids = [zone.zone_id for zone in self.zones]
        if len(zone_ids) != len(set(zone_ids)):
            raise ValueError("zone_id values must be unique")
        kinds = {zone.kind for zone in self.zones}
        required = {"primary", "ticker", "schedule", "logo"}
        missing = required - kinds
        if missing:
            raise ValueError(f"multi-zone CG snapshot missing required zones: {sorted(missing)}")
        if any(not zone.approved for zone in self.zones):
            raise ValueError("multi-zone CG snapshots can include only approved zones")
        return self


class CgPortalDisplay(BaseModel):
    """Operator and public portal display contract for the current CG board."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    snapshot: MultiZoneCgSnapshot
    template_library: CgTemplateLibrary
    feed_catalog: CgFeedCatalog
    approved_bulletins: CgBulletinQueue
    render_plan: CgHlsRenderPlan
    overlay_contract: CgOverlayContract
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]
