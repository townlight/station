# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Playback policy contracts for gated access, preroll, and audit proof."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Slug = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$")]
UrlString = Annotated[str, Field(min_length=1, max_length=1000)]
PlaybackSubjectType = Literal["channel", "asset"]
PlaybackAccessTier = Literal["public", "authenticated", "invite_only"]
ViewerAccountTier = Literal["viewer", "contributor", "operator"]
PrerollCreativeKind = Literal["video", "graphic"]
PlaybackDecision = Literal["allowed", "blocked"]


class ViewerAccount(BaseModel):
    """Resident viewer identity, distinct from contributor and operator accounts."""

    model_config = ConfigDict(extra="forbid")

    account_id: Slug
    tier: ViewerAccountTier = "viewer"
    display_name: Annotated[str, Field(min_length=1, max_length=200)]
    invite_groups: list[Slug] = Field(default_factory=list)
    oidc_subject: Annotated[str | None, Field(default=None, max_length=240)] = None

    @model_validator(mode="after")
    def _must_be_viewer_tier(self) -> ViewerAccount:
        if self.tier != "viewer":
            raise ValueError("playback viewer accounts must use the viewer tier")
        return self


class PrerollCreative(BaseModel):
    """One video or graphic card shown before playback."""

    model_config = ConfigDict(extra="forbid")

    creative_id: Slug
    kind: PrerollCreativeKind
    asset_url: UrlString
    duration_seconds: Annotated[int, Field(gt=0, le=600)]
    skippable_after_seconds: Annotated[int | None, Field(default=None, ge=0, le=600)] = None
    accessible_label: Annotated[str, Field(min_length=1, max_length=240)]
    transcript_url: UrlString | None = None

    @model_validator(mode="after")
    def _skip_window_inside_creative(self) -> PrerollCreative:
        if (
            self.skippable_after_seconds is not None
            and self.skippable_after_seconds > self.duration_seconds
        ):
            raise ValueError("skippable_after_seconds cannot exceed duration_seconds")
        return self


class PrerollSequence(BaseModel):
    """Stacked preroll sequence applied at playback time only."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    creatives: list[PrerollCreative] = Field(default_factory=list)
    apply_to_archive_exports: bool = False

    @model_validator(mode="after")
    def _enabled_sequences_need_creatives(self) -> PrerollSequence:
        if self.enabled and not self.creatives:
            raise ValueError("enabled preroll sequences require at least one creative")
        if self.apply_to_archive_exports:
            raise ValueError("prerolls affect playback only, not archival exports")
        creative_ids = [creative.creative_id for creative in self.creatives]
        if len(creative_ids) != len(set(creative_ids)):
            raise ValueError("preroll creative ids must be unique within a sequence")
        return self


class PlaybackPolicyConfig(BaseModel):
    """Policy configured per channel or per asset."""

    model_config = ConfigDict(extra="forbid")

    subject_type: PlaybackSubjectType
    subject_id: Slug
    access_tier: PlaybackAccessTier = "public"
    invite_group_id: Slug | None = None
    oidc_provider_id: Slug | None = None
    authenticated_rss_enabled: bool = False
    public_record_required: bool = False
    public_archive_complete: bool = False
    preroll: PrerollSequence = Field(default_factory=PrerollSequence)
    updated_at: datetime

    @model_validator(mode="after")
    def _guard_public_records_and_tier_fields(self) -> PlaybackPolicyConfig:
        if self.access_tier == "invite_only" and not self.invite_group_id:
            raise ValueError("invite-only playback requires invite_group_id")
        if self.access_tier == "public" and (self.invite_group_id or self.oidc_provider_id):
            raise ValueError("public playback cannot require invite or OIDC gates")
        if (self.public_record_required or self.public_archive_complete) and (
            self.access_tier != "public"
            or self.invite_group_id is not None
            or self.oidc_provider_id is not None
            or self.authenticated_rss_enabled
        ):
            raise ValueError("public-record assets must remain public and ungated")
        return self


class PlaybackPolicyUpdate(BaseModel):
    """Staff mutation payload for one playback policy subject."""

    model_config = ConfigDict(extra="forbid")

    access_tier: PlaybackAccessTier = "public"
    invite_group_id: Slug | None = None
    oidc_provider_id: Slug | None = None
    authenticated_rss_enabled: bool = False
    public_record_required: bool = False
    public_archive_complete: bool = False
    preroll: PrerollSequence = Field(default_factory=PrerollSequence)


class PlaybackPolicyEvaluationRequest(BaseModel):
    """Public playback decision request."""

    model_config = ConfigDict(extra="forbid")

    asset_id: Slug
    channel_id: Slug
    viewer: ViewerAccount | None = None
    invite_group_id: Slug | None = None


class PublicPlaybackPolicyEvaluationRequest(BaseModel):
    """Unauthenticated playback decision request from public clients."""

    model_config = ConfigDict(extra="forbid")

    asset_id: Slug
    channel_id: Slug
    viewer_token: Annotated[str | None, Field(default=None, max_length=5000)] = None


class ViewerTokenRequest(BaseModel):
    """Staff request to issue a server-signed resident playback entitlement."""

    model_config = ConfigDict(extra="forbid")

    account_id: Slug
    display_name: Annotated[str, Field(min_length=1, max_length=200)]
    invite_groups: list[Slug] = Field(default_factory=list)
    oidc_subject: Annotated[str | None, Field(default=None, max_length=240)] = None
    expires_at: datetime | None = None


class ViewerTokenResponse(BaseModel):
    """Server-signed resident playback entitlement."""

    model_config = ConfigDict(extra="forbid")

    viewer: ViewerAccount
    token: Annotated[str, Field(min_length=1)]
    expires_at: datetime | None = None


class PlaybackPolicyAuditEvent(BaseModel):
    """Audit record of the active playback policy at decision time."""

    model_config = ConfigDict(extra="forbid")

    event_id: Slug
    asset_id: Slug
    channel_id: Slug
    viewer_account_id: Slug | None = None
    decision: PlaybackDecision
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    access_tier: PlaybackAccessTier
    preroll_creative_ids: list[Slug]
    occurred_at: datetime


class PlaybackPolicyEvaluation(BaseModel):
    """Public-safe playback decision with active policy proof."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    policy: PlaybackPolicyConfig
    audit_event: PlaybackPolicyAuditEvent
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]


class PlaybackPolicyAuditLog(BaseModel):
    """Staff-readable playback policy audit log."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    events: list[PlaybackPolicyAuditEvent]
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)
