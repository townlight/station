# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Closed data contracts for the v0.7 three-tier publish workflow."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

PublishDashboardStateValue = Literal[
    "draft",
    "preflight_blocked",
    "publishing",
    "portal_live",
    "reach_degraded",
    "archive_pending",
    "archive_verified",
    "complete",
    "failed_needs_action",
]

PublishSurfaceKindValue = Literal["canonical", "archive", "reach", "record", "audience"]
PublishSurfaceStateValue = Literal[
    "blocked",
    "not_configured",
    # Owner decision 2026-09-02: a surface whose real delivery is deferred to
    # a future release (see civiccast.publish.service's subscriber-notifications
    # handling). Distinct from "not_configured" (an operator COULD set this up
    # today) -- there is nothing to configure yet, and approval never sends
    # anything for a surface in this state.
    "coming_soon",
    "pending",
    "running",
    "succeeded",
    # WP-05 extends the closed vocabulary with two states a fan-out surface
    # genuinely reaches and previously had to lie about:
    #
    # ``partial``    at least one intended delivery succeeded AND at least one
    #                failed or is still in retry. Before WP-05 this was
    #                reported as ``succeeded``.
    # ``unverified`` the run claims success but no delivery receipt exists --
    #                every subscriber-notification row written before WP-05,
    #                because nothing was ever sent or recorded. Never green
    #                evidence; it means "we cannot show this happened".
    "partial",
    "unverified",
    "failed",
    "overridden",
]
PublishSurfaceApprovalValue = Literal["pending", "approved", "overridden"]


class PublishSurfaceOverride(BaseModel):
    """Audit-logged exception for a surface that is intentionally skipped."""

    model_config = ConfigDict(extra="forbid")

    surface_id: Annotated[str, Field(min_length=1, max_length=80)]
    justification: Annotated[str, Field(min_length=12, max_length=1000)]


class YouTubeSchedule(BaseModel):
    """Optional bounded destination schedule; omitting it sends immediately."""

    youtube_scheduled_start: AwareDatetime | None = None
    youtube_scheduled_end: AwareDatetime | None = None

    @model_validator(mode="after")
    def _valid_youtube_window(self) -> YouTubeSchedule:
        start, end = self.youtube_scheduled_start, self.youtube_scheduled_end
        if (start is None) != (end is None) or (start and end and end <= start):
            raise ValueError("YouTube schedule requires a start and a later end.")
        return self


class PublishApprovalRequest(YouTubeSchedule):
    """Operator approval request for a recording publish run."""

    model_config = ConfigDict(extra="forbid")

    operator_id: Annotated[str, Field(min_length=1, max_length=160)]
    operator_display_name: Annotated[str, Field(min_length=1, max_length=200)]
    youtube_channel_id: Annotated[str | None, Field(default=None, min_length=1, max_length=160)] = (
        None
    )
    # Omitted (``None``) means the canonical Portal surface only. It used to
    # mean EVERY surface -- Internet Archive, both NAS archives, YouTube
    # Live/VOD, cable package -- so an API approval with the field left out
    # permanently published a closed session (beta.5 walkthrough F-23,
    # safety). Archive and reach surfaces are opt-in by id.
    approved_surface_ids: list[str] | None = Field(
        default=None,
        description=(
            "Surface ids to publish. Omitted or null means the canonical Portal "
            "surface only; archive and reach surfaces must be listed explicitly. "
            "An empty list is accepted only together with overrides; on its own "
            "it is refused (422) because nothing would be published."
        ),
    )
    overrides: list[PublishSurfaceOverride] = Field(default_factory=list)

    @model_validator(mode="after")
    def _empty_selection_needs_an_override(self) -> PublishApprovalRequest:
        # Hostile review m8: ``null`` means Portal only and ``[]`` used to mean
        # "publish nothing" -- both 200, indistinguishable to a JS client that
        # drops an undefined field. An empty list with nothing else to do is
        # refused so the caller learns which of the two it sent.
        if (
            self.approved_surface_ids is not None
            and not self.approved_surface_ids
            and not self.overrides
        ):
            raise ValueError(
                "approved_surface_ids is an empty list and no overrides were given, "
                "so nothing would be published. Omit the field to publish the Portal "
                "surface only, or list the surface ids to publish."
            )
        return self

    @model_validator(mode="after")
    def _no_duplicate_overrides(self) -> PublishApprovalRequest:
        ids = [override.surface_id for override in self.overrides]
        if len(ids) != len(set(ids)):
            raise ValueError("surface overrides must be unique by surface_id")
        return self


class PublishRetryRequest(YouTubeSchedule):
    """Operator request to retry one failed or pending publish surface."""

    model_config = ConfigDict(extra="forbid")

    operator_id: Annotated[str, Field(min_length=1, max_length=160)]
    operator_display_name: Annotated[str, Field(min_length=1, max_length=200)]
    youtube_channel_id: Annotated[str | None, Field(default=None, min_length=1, max_length=160)] = (
        None
    )


class PublishPreflightCheck(BaseModel):
    """Readiness check for one publish destination before approval."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, max_length=80)]
    label: Annotated[str, Field(min_length=1, max_length=120)]
    kind: PublishSurfaceKindValue
    required: bool
    health: Literal["ok", "warning", "error", "unknown"]
    credential_reference: str | None = None
    message: Annotated[str, Field(min_length=1)]
    next_step: Annotated[str, Field(min_length=1)]


class PublishPreflightResponse(BaseModel):
    """Pre-approval readiness summary for an asset."""

    model_config = ConfigDict(extra="forbid")

    asset_id: Annotated[str, Field(min_length=1, max_length=160)]
    ready: bool
    checks: list[PublishPreflightCheck]


class PublishNotificationDeliveryRow(BaseModel):
    """One logical subscriber delivery, safe for the dashboard and the API.

    Deliberately carries no subscriber handle: ``subscription_id`` is the
    salted digest the subscription store already exposes, and ``detail`` is
    redacted at the point of failure
    (:func:`civiccast.subscribe.delivery.redact_delivery_detail`). No email
    address, webhook URL, secret or signature reaches this model.
    """

    model_config = ConfigDict(extra="forbid")

    subscription_id: Annotated[str, Field(min_length=1, max_length=160)]
    channel: Literal["email", "webhook"]
    target_type: Literal["channel", "meeting_body"]
    target_id: Annotated[str, Field(min_length=1, max_length=120)]
    outcome: Literal["pending", "sent", "failed", "queued"]
    attempts: Annotated[int, Field(ge=0)]
    error_code: str | None = None
    detail: str = ""
    #: Set when the delivery is in the durable webhook retry queue, so an
    #: operator can follow a queued or dead-lettered notice to its retry row.
    retry_id: str | None = None
    last_attempted_at: datetime | None = None


class PublishNotificationSummary(BaseModel):
    """Per-delivery summary persisted on the publish run (WP-05 plan item 9).

    The aggregate surface state is *derived* from these rows, so the dashboard
    can always explain why a surface is ``partial`` instead of asking the
    operator to trust a single word.
    """

    model_config = ConfigDict(extra="forbid")

    publication_id: Annotated[str, Field(min_length=1, max_length=200)]
    intended: Annotated[int, Field(ge=0)]
    sent: Annotated[int, Field(ge=0)]
    failed: Annotated[int, Field(ge=0)]
    queued: Annotated[int, Field(ge=0)]
    pending: Annotated[int, Field(ge=0)]
    targets: list[str] = Field(default_factory=list)
    #: True when ``deliveries`` lists only the first
    #: ``NOTIFICATION_SUMMARY_MAX_DELIVERIES`` rows. The counts above are always
    #: the full totals; this summary is embedded in the publish run's JSON
    #: column, so a station with thousands of confirmed subscribers must not
    #: grow that row without bound. The complete per-delivery history stays in
    #: ``notification_delivery_outcomes``.
    deliveries_truncated: bool = False
    deliveries: list[PublishNotificationDeliveryRow] = Field(default_factory=list)


class PublishSurfaceStatus(BaseModel):
    """One destination in the three-tier publish workflow."""

    model_config = ConfigDict(extra="forbid")

    delivery_state: str | None = None
    channel_id: str | None = None
    broadcast_id: str | None = None
    stream_id: str | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    id: Annotated[str, Field(min_length=1, max_length=80)]
    label: Annotated[str, Field(min_length=1, max_length=120)]
    kind: PublishSurfaceKindValue
    state: PublishSurfaceStateValue
    required: bool = False
    approval: PublishSurfaceApprovalValue = "pending"
    url: str | None = None
    path: str | None = None
    verification_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    last_attempt_at: datetime | None = None
    completed_at: datetime | None = None
    health: Literal["ok", "warning", "error", "unknown"] = "unknown"
    retry_count: int = Field(default=0, ge=0)
    message: Annotated[str, Field(min_length=1)]
    next_step: Annotated[str, Field(min_length=1)]
    override_justification: str | None = None
    # GauntletGate TW-1: true when this surface was completed by a simulated
    # provider (the default until an admin sets CIVICCAST_PROVIDER_<KIND>=real).
    # The dashboard MUST badge this -- a clerk approving an archive surface has
    # to be able to tell a real archival write from one that never happened.
    simulated: bool = False
    #: WP-05: the safe per-delivery receipt behind a fan-out surface's state.
    #: Present on ``subscriber-notifications`` once a real dispatch has run;
    #: ``None`` on every other surface, and on subscriber-notification rows
    #: written before WP-05 -- which is exactly what makes those rows read as
    #: ``unverified`` instead of green.
    notification_summary: PublishNotificationSummary | None = None

    @model_validator(mode="after")
    def _override_requires_justification(self) -> PublishSurfaceStatus:
        if self.state == "overridden" and not self.override_justification:
            raise ValueError("overridden surfaces require override_justification")
        return self


class PublishAuditEvent(BaseModel):
    """One audit event emitted by approval, publish, retry, or override."""

    model_config = ConfigDict(extra="forbid")

    event_id: Annotated[str, Field(min_length=1, max_length=180)]
    asset_id: Annotated[str, Field(min_length=1, max_length=160)]
    surface_id: Annotated[str, Field(min_length=1, max_length=80)]
    action: Literal["approved", "started", "succeeded", "failed", "retried", "overridden"]
    operator_id: Annotated[str, Field(min_length=1, max_length=160)]
    occurred_at: datetime
    message: Annotated[str, Field(min_length=1)]
    url: str | None = None
    path: str | None = None
    verification_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class PublishRunRecord(BaseModel):
    """Persisted publish attempt for one recording."""

    model_config = ConfigDict(extra="forbid")

    asset_id: Annotated[str, Field(min_length=1, max_length=160)]
    operator_id: Annotated[str, Field(min_length=1, max_length=160)]
    operator_display_name: Annotated[str, Field(min_length=1, max_length=200)]
    approved_at: datetime
    surfaces: list[PublishSurfaceStatus]
    audit_events: list[PublishAuditEvent] = Field(default_factory=list)


class PublishAssetStatus(BaseModel):
    """Dashboard row for one asset's publish state."""

    model_config = ConfigDict(extra="forbid")

    asset_id: Annotated[str, Field(min_length=1, max_length=160)]
    title: Annotated[str, Field(min_length=1)]
    dashboard_state: PublishDashboardStateValue
    dashboard_label: Annotated[str, Field(min_length=1)]
    canonical_public: bool
    archive_verified: bool
    reach_degraded: bool
    needs_operator_action: bool
    public_record_required: bool
    published_at: datetime | None = None
    surfaces: list[PublishSurfaceStatus]


class PublishDashboardSummary(BaseModel):
    """Aggregate counters for the publish dashboard header."""

    total_assets: int
    draft: int
    portal_live: int
    archive_verified: int
    degraded: int
    needs_operator_action: int


class PublishDashboardResponse(BaseModel):
    """Response for ``GET /api/staff/publish/assets``."""

    summary: PublishDashboardSummary
    assets: list[PublishAssetStatus]
