# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Canonical app-platform contracts for v1.8 parity architecture."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from civiccast.playback_policy.models import PrerollSequence

AppTarget = Literal[
    "web_pwa",
    "roku",
    "tvos",
    "fire_tv",
    "android_tv",
    "android_mobile",
    "ios_ipados",
    "cg",
    "epg",
]
AppBuildTier = Literal["unbranded", "branded"]
EpgExportFormat = Literal["json", "tvguide_xlist"]
ChannelKind = Literal["public", "education", "government", "community"]


def _default_epg_export_formats() -> list[EpgExportFormat]:
    return ["json"]


OutputKind = Literal[
    "hls",
    "rtmp",
    "srt",
    "ndi_plan",
    "native_app",
    "epg_export",
    "cg_render",
    "facility_control",
]
LiveStateValue = Literal["offline", "on_air", "fallback", "degraded"]
PlaybackAccessTier = Literal["public", "authenticated", "invite_only"]
PrerollKind = Literal["none", "video", "graphic"]
CatalogPublishState = Literal["draft", "scheduled", "published", "archived", "unavailable"]
CatalogSort = Literal["published_at_desc", "published_at_asc", "title_asc"]
SmartPlaylistRuleField = Literal[
    "channel_id",
    "series",
    "topic",
    "publish_state",
    "public_record_required",
]
SmartPlaylistRuleOperator = Literal["equals", "contains"]
TrackKind = Literal["embedded", "sidecar", "generated", "external"]
ChapterSource = Literal["operator", "ai", "imported"]
ScheduleFeedKind = Literal["live", "premiere", "rerun", "bulletin", "fallback"]
AnalyticsEventName = Literal[
    "playback_start",
    "playback_heartbeat",
    "playback_complete",
    "playback_error",
    "podcast_download",
    "search",
    "schedule_browse",
    "subscription_action",
]
CgZoneKind = Literal["primary", "ticker", "schedule", "logo", "sponsor", "audio", "alert"]
ContributorSubmissionState = Literal[
    "draft",
    "submitted",
    "needs_changes",
    "accepted",
    "declined",
    "scheduled",
    "published",
]
FacilityTargetKind = Literal[
    "av_router",
    "caption_encoder",
    "cloud_relay",
    "overlay_renderer",
    "playout_status",
]

Slug = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$")]
UrlString = Annotated[str, Field(min_length=1, max_length=1000)]
HexColor = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]
Seconds = Annotated[float, Field(ge=0)]


class AppBuildProfile(BaseModel):
    """Build metadata shared by unbranded and branded app shells."""

    model_config = ConfigDict(extra="forbid")

    tier: AppBuildTier
    app_name: Annotated[str, Field(min_length=1, max_length=120)]
    platform_targets: list[AppTarget]
    icon_url: UrlString | None = None
    splash_url: UrlString | None = None
    store_ready: bool = False
    store_notes: Annotated[str | None, Field(default=None, max_length=1000)] = None

    @model_validator(mode="after")
    def _targets_required(self) -> AppBuildProfile:
        if not self.platform_targets:
            raise ValueError("platform_targets must include at least one app target")
        if len(self.platform_targets) != len(set(self.platform_targets)):
            raise ValueError("platform_targets must be unique")
        return self


class ChannelBranding(BaseModel):
    """Public channel identity used by every app shell.

    ``configured_at`` is an explicit stored fact, not a value comparison:
    ``AppPlatformConfigStore.update_channel_branding()`` sets it to the
    write timestamp whenever an operator saves branding through Channel
    Ops, regardless of what values they chose -- including values that
    happen to equal the compile-time default profile (a plausible operator
    choice, e.g. keeping the default color). A row seeded from the default
    table (``_default_station_config()``) leaves this unset. Consumers that
    need to tell "an operator configured this" from "nobody has touched
    this yet" must read this field, not compare branding values against
    ``civiccast.cable.channel.default_channel_profiles()`` -- a PR #132
    review caught a value-comparison implementation of exactly that
    distinction silently misclassifying a deliberate default-equal save as
    unconfigured.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: Annotated[str, Field(min_length=1, max_length=120)]
    short_name: Annotated[str, Field(min_length=1, max_length=40)]
    color: HexColor
    logo_text: Annotated[str, Field(min_length=1, max_length=40)]
    logo_url: UrlString | None = None
    configured_at: datetime | None = None


class ChannelOutput(BaseModel):
    """Software-visible public output target for a channel."""

    model_config = ConfigDict(extra="forbid")

    kind: OutputKind
    label: Annotated[str, Field(min_length=1, max_length=120)]
    target: UrlString
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]
    app_targets: list[AppTarget] = Field(default_factory=list)

    @model_validator(mode="after")
    def _app_targets_unique(self) -> ChannelOutput:
        if len(self.app_targets) != len(set(self.app_targets)):
            raise ValueError("app_targets must be unique")
        return self


class CaptionTrack(BaseModel):
    """Caption track advertised to public playback clients."""

    model_config = ConfigDict(extra="forbid")

    track_id: Slug
    label: Annotated[str, Field(min_length=1, max_length=120)]
    language: Annotated[str, Field(min_length=2, max_length=16)]
    url: UrlString
    kind: TrackKind
    default: bool = False
    confidence: Annotated[float | None, Field(default=None, ge=0, le=1)] = None


class AudioTrack(BaseModel):
    """Audio track advertised to public playback clients."""

    model_config = ConfigDict(extra="forbid")

    track_id: Slug
    label: Annotated[str, Field(min_length=1, max_length=120)]
    language: Annotated[str, Field(min_length=2, max_length=16)]
    url: UrlString
    kind: TrackKind
    default: bool = False


class ChapterMarker(BaseModel):
    """Moderated chapter marker shared by VOD, podcast, and apps."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: Slug
    title: Annotated[str, Field(min_length=1, max_length=200)]
    start_seconds: Seconds
    end_seconds: Seconds | None = None
    source: ChapterSource = "operator"
    approved: bool = True

    @model_validator(mode="after")
    def _end_after_start(self) -> ChapterMarker:
        if self.end_seconds is not None and self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class PrerollPolicy(BaseModel):
    """Optional preroll policy for a channel or VOD item."""

    model_config = ConfigDict(extra="forbid")

    kind: PrerollKind = "none"
    asset_url: UrlString | None = None
    duration_seconds: Annotated[int | None, Field(default=None, gt=0, le=600)] = None
    skippable_after_seconds: Annotated[int | None, Field(default=None, ge=0, le=600)] = None

    @model_validator(mode="after")
    def _asset_required_for_preroll(self) -> PrerollPolicy:
        if self.kind == "none":
            if self.asset_url is not None or self.duration_seconds is not None:
                raise ValueError("none preroll cannot carry an asset_url or duration_seconds")
            return self
        if not self.asset_url:
            raise ValueError("video and graphic prerolls require asset_url")
        if self.duration_seconds is None:
            raise ValueError("video and graphic prerolls require duration_seconds")
        if (
            self.skippable_after_seconds is not None
            and self.skippable_after_seconds > self.duration_seconds
        ):
            raise ValueError("skippable_after_seconds cannot exceed duration_seconds")
        return self


class PlaybackPolicy(BaseModel):
    """Server-side playback policy, including public-record guardrails."""

    model_config = ConfigDict(extra="forbid")

    access_tier: PlaybackAccessTier = "public"
    public_record_required: bool = False
    public_archive_complete: bool = False
    entitlement_required: Annotated[str | None, Field(default=None, max_length=120)] = None
    preroll: PrerollPolicy = Field(default_factory=PrerollPolicy)
    preroll_sequence: PrerollSequence = Field(default_factory=PrerollSequence)

    @model_validator(mode="after")
    def _public_records_cannot_be_gated(self) -> PlaybackPolicy:
        if (self.public_record_required or self.public_archive_complete) and (
            self.access_tier != "public" or self.entitlement_required is not None
        ):
            raise ValueError("public-record assets must remain public and ungated")
        return self


class LiveState(BaseModel):
    """Resident-safe live state for all app clients."""

    model_config = ConfigDict(extra="forbid")

    state: LiveStateValue
    channel_id: Slug
    title: Annotated[str | None, Field(default=None, max_length=200)] = None
    live_session_id: Annotated[str | None, Field(default=None, max_length=160)] = None
    playback_url: UrlString | None = None
    source_ref: Annotated[str | None, Field(default=None, max_length=500)] = None
    started_at: datetime | None = None
    caption_tracks: list[CaptionTrack] = Field(default_factory=list)
    audio_tracks: list[AudioTrack] = Field(default_factory=list)
    dvr_window_seconds: Annotated[int | None, Field(default=None, ge=0)] = None
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]
    fallback_reason: Annotated[str | None, Field(default=None, max_length=500)] = None

    @model_validator(mode="after")
    def _playback_required_when_on_air(self) -> LiveState:
        if self.state == "on_air" and not self.playback_url:
            raise ValueError("on_air live state requires playback_url")
        if self.state == "fallback" and not self.fallback_reason:
            raise ValueError("fallback live state requires fallback_reason")
        return self


class ChannelPublicConfig(BaseModel):
    """Public channel contract consumed by app shells and CG."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Slug
    slug: Slug
    kind: ChannelKind
    branding: ChannelBranding
    outputs: list[ChannelOutput] = Field(default_factory=list)
    programming_rules: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    fallback_behavior: Annotated[str, Field(min_length=1)]
    live_state_url: UrlString
    schedule_feed_url: UrlString
    vod_catalog_url: UrlString
    cg_feed_url: UrlString | None = None
    app_targets: list[AppTarget]

    @model_validator(mode="after")
    def _app_targets_unique(self) -> ChannelPublicConfig:
        if not self.app_targets:
            raise ValueError("app_targets must include at least one app target")
        if len(self.app_targets) != len(set(self.app_targets)):
            raise ValueError("app_targets must be unique")
        return self


class ScheduleFeedItem(BaseModel):
    """Public schedule and EPG projection."""

    model_config = ConfigDict(extra="forbid")

    item_id: Slug
    channel_id: Slug
    kind: ScheduleFeedKind
    title: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str | None, Field(default=None, max_length=1000)] = None
    starts_at: datetime
    ends_at: datetime | None = None
    duration_seconds: Annotated[int, Field(gt=0)]
    catalog_item_id: Slug | None = None
    live_state_url: UrlString | None = None
    playback_url: UrlString | None = None
    captions_available: bool = False
    public_record_required: bool = False
    proof_boundary: Annotated[str | None, Field(default=None, max_length=160)] = None

    @model_validator(mode="after")
    def _end_after_start(self) -> ScheduleFeedItem:
        if self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be greater than starts_at")
        return self


class NowNextState(BaseModel):
    """Current and next public playout state for a channel."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    channel_id: Slug
    current: ScheduleFeedItem
    next: ScheduleFeedItem | None = None
    fallback_active: bool = False
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]

    @model_validator(mode="after")
    def _current_channel_matches(self) -> NowNextState:
        if self.current.channel_id != self.channel_id:
            raise ValueError("current item channel_id must match now/next channel_id")
        if self.next is not None and self.next.channel_id != self.channel_id:
            raise ValueError("next item channel_id must match now/next channel_id")
        return self


class EpgScheduleResponse(BaseModel):
    """Machine-readable schedule export for apps, CG, EPG, and portal consumers."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    channel_id: Slug
    items: list[ScheduleFeedItem]
    export_targets: list[AppTarget]
    export_formats: list[EpgExportFormat] = Field(default_factory=_default_epg_export_formats)
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]

    @model_validator(mode="after")
    def _items_match_channel(self) -> EpgScheduleResponse:
        if not self.items:
            raise ValueError("EPG exports require at least one schedule item")
        mismatched = [item.item_id for item in self.items if item.channel_id != self.channel_id]
        if mismatched:
            raise ValueError("EPG schedule items must match channel_id")
        if len(self.export_targets) != len(set(self.export_targets)):
            raise ValueError("export_targets must be unique")
        if len(self.export_formats) != len(set(self.export_formats)):
            raise ValueError("export_formats must be unique")
        return self


class VodCatalogItem(BaseModel):
    """App-safe VOD catalog item."""

    model_config = ConfigDict(extra="forbid")

    item_id: Slug
    asset_id: Slug
    channel_id: Slug
    title: Annotated[str, Field(min_length=1, max_length=240)]
    description: Annotated[str | None, Field(default=None, max_length=2000)] = None
    series: Annotated[str | None, Field(default=None, max_length=160)] = None
    topics: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(default_factory=list)
    playlist_ids: list[Slug] = Field(default_factory=list)
    playback_url: UrlString | None = None
    poster_url: UrlString | None = None
    thumbnail_url: UrlString | None = None
    duration_seconds: Annotated[int | None, Field(default=None, ge=0)] = None
    published_at: datetime | None = None
    publish_state: CatalogPublishState
    captions: list[CaptionTrack] = Field(default_factory=list)
    audio_tracks: list[AudioTrack] = Field(default_factory=list)
    chapters: list[ChapterMarker] = Field(default_factory=list)
    playback_policy: PlaybackPolicy = Field(default_factory=PlaybackPolicy)

    @model_validator(mode="after")
    def _published_items_need_playback(self) -> VodCatalogItem:
        if self.publish_state == "published" and not self.playback_url:
            raise ValueError("published catalog items require playback_url")
        return self


class SmartPlaylistRule(BaseModel):
    """One deterministic rule for app-visible smart playlists."""

    model_config = ConfigDict(extra="forbid")

    field: SmartPlaylistRuleField
    operator: SmartPlaylistRuleOperator = "equals"
    value: Annotated[str | bool, Field()]

    @model_validator(mode="after")
    def _operator_matches_field(self) -> SmartPlaylistRule:
        if self.operator == "contains" and self.field != "topic":
            raise ValueError("contains smart-playlist rules are only supported for topic")
        if self.field == "public_record_required" and not isinstance(self.value, bool):
            raise ValueError("public_record_required rules require a boolean value")
        if self.field != "public_record_required" and not isinstance(self.value, str):
            raise ValueError(f"{self.field} rules require a string value")
        return self


class SmartPlaylistDefinition(BaseModel):
    """Public smart-playlist definition consumed by apps and CG."""

    model_config = ConfigDict(extra="forbid")

    playlist_id: Slug
    label: Annotated[str, Field(min_length=1, max_length=120)]
    description: Annotated[str | None, Field(default=None, max_length=500)] = None
    channel_id: Slug
    rules: list[SmartPlaylistRule]
    sort: CatalogSort = "published_at_desc"
    limit: Annotated[int, Field(gt=0, le=100)] = 20

    @model_validator(mode="after")
    def _rules_required(self) -> SmartPlaylistDefinition:
        if not self.rules:
            raise ValueError("smart playlists require at least one rule")
        return self


class VodCatalogResponse(BaseModel):
    """Facet-ready public VOD catalog response."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    channel_id: Slug | None = None
    items: list[VodCatalogItem]
    playlists: list[SmartPlaylistDefinition] = Field(default_factory=list)
    facets: dict[str, list[str]] = Field(default_factory=dict)
    next_cursor: Annotated[str | None, Field(default=None, max_length=500)] = None


class AnalyticsEvent(BaseModel):
    """Privacy-safe public app analytics event."""

    model_config = ConfigDict(extra="forbid")

    event_id: Slug
    event_name: AnalyticsEventName
    occurred_at: datetime
    app_target: AppTarget
    channel_id: Slug | None = None
    content_id: Annotated[str | None, Field(default=None, max_length=160)] = None
    anonymous_session_id: Annotated[
        str | None, Field(default=None, min_length=8, max_length=160)
    ] = None
    hashed_viewer_id: Annotated[str | None, Field(default=None, max_length=160)] = None
    properties: dict[str, Any] = Field(default_factory=dict, max_length=32)

    @model_validator(mode="after")
    def _properties_must_stay_privacy_safe(self) -> AnalyticsEvent:
        sensitive_keys = {
            "address",
            "display_name",
            "email",
            "first_name",
            "full_name",
            "ip",
            "ip_address",
            "last_name",
            "name",
            "phone",
            "token",
        }
        sensitive_suffixes = (
            "_address",
            "_display_name",
            "_email",
            "_first_name",
            "_full_name",
            "_ip",
            "_ip_address",
            "_last_name",
            "_phone",
            "_token",
        )
        for key in self.properties:
            normalized = key.strip().lower()
            if normalized in sensitive_keys or normalized.endswith(sensitive_suffixes):
                raise ValueError("analytics properties cannot include direct viewer identifiers")
            if len(normalized) > 80:
                raise ValueError("analytics property keys must be 80 characters or fewer")
        for value in self.properties.values():
            if isinstance(value, str) and len(value) > 500:
                raise ValueError("analytics property string values must be 500 characters or fewer")
            if isinstance(value, list | dict):
                raise ValueError("analytics properties cannot include nested values")
        return self


class AnalyticsIngestResponse(BaseModel):
    """Acknowledgement for privacy-safe public app analytics ingestion."""

    model_config = ConfigDict(extra="forbid")

    event_id: Slug
    accepted: bool = True
    retained_fields: list[Annotated[str, Field(min_length=1)]]
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]


class CgZone(BaseModel):
    """One zone in a multi-zone CG bulletin-board snapshot."""

    model_config = ConfigDict(extra="forbid")

    zone_id: Slug
    kind: CgZoneKind
    title: Annotated[str | None, Field(default=None, max_length=160)] = None
    content: dict[str, Any] = Field(default_factory=dict)
    refresh_seconds: Annotated[int | None, Field(default=None, gt=0, le=86400)] = None


class CgFeedSnapshot(BaseModel):
    """Multi-zone CG state for portal, render, and overlay adapters."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: Slug
    generated_at: datetime
    channel_id: Slug
    template_id: Slug
    zones: list[CgZone]
    emergency_active: bool = False
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]

    @model_validator(mode="after")
    def _zones_required_and_unique(self) -> CgFeedSnapshot:
        if not self.zones:
            raise ValueError("zones must include at least one zone")
        ids = [zone.zone_id for zone in self.zones]
        if len(ids) != len(set(ids)):
            raise ValueError("zone_id values must be unique")
        return self


class ContributorSubmission(BaseModel):
    """External producer submission state for operator review."""

    model_config = ConfigDict(extra="forbid")

    submission_id: Slug
    contributor_id: Slug
    producer_name: Annotated[str, Field(min_length=1, max_length=200)]
    title: Annotated[str, Field(min_length=1, max_length=240)]
    state: ContributorSubmissionState
    agreement_version: Annotated[str, Field(min_length=1, max_length=80)]
    agreement_accepted_at: datetime
    requested_air_date: datetime | None = None
    asset_id: Slug | None = None
    operator_notes: Annotated[str | None, Field(default=None, max_length=2000)] = None


class FacilityIntegrationTarget(BaseModel):
    """Software-visible output or control target for facility adapters."""

    model_config = ConfigDict(extra="forbid")

    target_id: Slug
    kind: FacilityTargetKind
    label: Annotated[str, Field(min_length=1, max_length=120)]
    endpoint: UrlString
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]
    enabled: bool = False
    operator_setup_required: bool = True


class StationAppConfig(BaseModel):
    """Top-level station app-platform config shared by every shell."""

    model_config = ConfigDict(extra="forbid")

    station_id: Slug
    station_name: Annotated[str, Field(min_length=1, max_length=160)]
    generated_at: datetime
    default_channel_id: Slug
    build_profile: AppBuildProfile
    channels: list[ChannelPublicConfig]
    support_url: UrlString
    privacy_url: UrlString
    analytics_enabled: bool = False
    ga4_measurement_id: Annotated[
        str | None, Field(default=None, pattern=r"^G-[A-Z0-9]{4,20}$")
    ] = None
    analytics_privacy_notice_url: UrlString | None = None
    emergency_status_url: UrlString | None = None

    @model_validator(mode="after")
    def _validate_station_config(self) -> StationAppConfig:
        channel_ids = [channel.channel_id for channel in self.channels]
        if not channel_ids:
            raise ValueError("channels must include at least one channel")
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError("channel_id values must be unique")
        if self.default_channel_id not in channel_ids:
            raise ValueError("default_channel_id must reference a channel")
        if self.ga4_measurement_id and not self.analytics_enabled:
            raise ValueError("GA4 integration requires analytics_enabled")
        if self.ga4_measurement_id and not self.analytics_privacy_notice_url:
            raise ValueError("GA4 integration requires analytics_privacy_notice_url")
        return self


class StationAppConfigUpdate(BaseModel):
    """Operator-editable station app-platform fields."""

    model_config = ConfigDict(extra="forbid")

    station_name: Annotated[str | None, Field(default=None, min_length=1, max_length=160)] = None
    default_channel_id: Slug | None = None
    support_url: UrlString | None = None
    privacy_url: UrlString | None = None
    analytics_enabled: bool | None = None
    ga4_measurement_id: Annotated[
        str | None, Field(default=None, pattern=r"^G-[A-Z0-9]{4,20}$")
    ] = None
    analytics_privacy_notice_url: UrlString | None = None
    emergency_status_url: UrlString | None = None
    app_name: Annotated[str | None, Field(default=None, min_length=1, max_length=120)] = None
    build_tier: AppBuildTier | None = None
    store_ready: bool | None = None
    store_notes: Annotated[str | None, Field(default=None, max_length=1000)] = None


class ChannelBrandingUpdate(BaseModel):
    """Operator-editable channel branding fields."""

    model_config = ConfigDict(extra="forbid")

    display_name: Annotated[str | None, Field(default=None, min_length=1, max_length=120)] = None
    short_name: Annotated[str | None, Field(default=None, min_length=1, max_length=40)] = None
    color: HexColor | None = None
    logo_text: Annotated[str | None, Field(default=None, min_length=1, max_length=40)] = None
    logo_url: UrlString | None = None
