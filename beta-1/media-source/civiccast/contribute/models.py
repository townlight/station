# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Contributor portal contracts for externally submitted programming."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Slug = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$")]
EmailText = Annotated[
    str, Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
]

ContributorAccountTier = Literal["viewer", "contributor", "operator"]
ContributorSubmissionState = Literal[
    "submitted",
    "under_review",
    "needs_changes",
    "accepted",
    "declined",
    "scheduled",
    "published",
]
NotificationKind = Literal["email", "portal"]
BrokenMediaGateState = Literal["not_run", "passed", "failed", "override_accepted"]
ReviewAction = Literal["mark_under_review", "request_changes", "accept", "decline", "schedule"]


class ContributorAccount(BaseModel):
    """External producer identity below the operator tier."""

    model_config = ConfigDict(extra="forbid")

    account_id: Slug
    tier: ContributorAccountTier = "contributor"
    display_name: Annotated[str, Field(min_length=1, max_length=200)]
    contact_email: EmailText
    organization: Annotated[str | None, Field(default=None, max_length=200)] = None
    active: bool = True

    @model_validator(mode="after")
    def _must_be_contributor_tier(self) -> ContributorAccount:
        if self.tier != "contributor":
            raise ValueError("external producer accounts must use the contributor tier")
        return self


class SubmissionAgreementAcceptance(BaseModel):
    """Terms-of-submission acceptance logged per submission."""

    model_config = ConfigDict(extra="forbid")

    agreement_id: Slug
    version: Annotated[str, Field(min_length=1, max_length=80)]
    accepted_at: datetime
    accepted_by_name: Annotated[str, Field(min_length=1, max_length=200)]
    acceptance_ip_hash: Annotated[str | None, Field(default=None, max_length=160)] = None


class SubmissionNotificationPreference(BaseModel):
    """Status notification destination requested by the contributor."""

    model_config = ConfigDict(extra="forbid")

    kind: NotificationKind
    target: Annotated[str, Field(min_length=1, max_length=320)]
    enabled: bool = True


class SubmissionStatusNotification(BaseModel):
    """Auditable notification queued for a contributor status change."""

    model_config = ConfigDict(extra="forbid")

    kind: NotificationKind
    target: Annotated[str, Field(min_length=1, max_length=320)]
    state: ContributorSubmissionState
    queued_at: datetime
    message: Annotated[str, Field(min_length=1, max_length=500)]


class ContributorNotificationOutbox(BaseModel):
    """Staff-readable queue of contributor status notifications."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    notifications: list[SubmissionStatusNotification]
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]


class SubmissionMediaReference(BaseModel):
    """Reference to uploaded media without granting public publish rights."""

    model_config = ConfigDict(extra="forbid")

    upload_ref: Annotated[
        str,
        Field(
            min_length=1,
            max_length=500,
            description=(
                "Opaque handle for the uploaded file, issued by "
                "POST /api/public/contribute/uploads. NOT a filesystem path -- "
                "resolve it server-side via "
                "civiccast.contribute.store.resolve_contributor_upload_path."
            ),
        ),
    ]
    filename: Annotated[str, Field(min_length=1, max_length=260)]
    content_type: Annotated[str, Field(min_length=1, max_length=120)]
    size_bytes: Annotated[int, Field(gt=0)]
    sha256: Annotated[
        str | None,
        Field(default=None, pattern=r"^[A-Fa-f0-9]{64}$"),
    ] = None


class ContributorSubmissionCreate(BaseModel):
    """Public contributor submission payload."""

    model_config = ConfigDict(extra="forbid")

    contributor: ContributorAccount
    channel_id: Slug
    title: Annotated[str, Field(min_length=1, max_length=240)]
    description: Annotated[str, Field(min_length=1, max_length=4000)]
    tags: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(default_factory=list)
    producer_name: Annotated[str, Field(min_length=1, max_length=200)]
    requested_air_date: datetime | None = None
    media: SubmissionMediaReference
    agreements: list[SubmissionAgreementAcceptance]
    notifications: list[SubmissionNotificationPreference] = Field(default_factory=list)

    @model_validator(mode="after")
    def _submission_contract_is_complete(self) -> ContributorSubmissionCreate:
        if not self.agreements:
            raise ValueError("at least one terms-of-submission agreement must be accepted")
        if len(self.tags) != len({tag.casefold() for tag in self.tags}):
            raise ValueError("tags must be unique")
        enabled_notifications = [item for item in self.notifications if item.enabled]
        if not enabled_notifications:
            raise ValueError("at least one enabled status notification destination is required")
        return self


class BrokenMediaGateResult(BaseModel):
    """Operator-visible media gate result for a submitted file."""

    model_config = ConfigDict(extra="forbid")

    state: BrokenMediaGateState = "not_run"
    checked_at: datetime | None = None
    summary: Annotated[str | None, Field(default=None, max_length=1000)] = None
    blocking_findings: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def _failed_gate_needs_findings(self) -> BrokenMediaGateResult:
        if self.state == "failed" and not self.blocking_findings:
            raise ValueError("failed broken-media gates must include blocking findings")
        if self.state != "not_run" and self.checked_at is None:
            raise ValueError("completed broken-media gates require checked_at")
        return self


class ScheduleHandoff(BaseModel):
    """Schedule handoff requested by the operator after acceptance."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Slug
    requested_start: datetime
    duration_seconds: Annotated[int, Field(gt=0)]
    notes: Annotated[str | None, Field(default=None, max_length=1000)] = None
    schedule_item_id: Slug | None = None


class SubmissionMetadataPatch(BaseModel):
    """Operator-safe metadata edits before scheduling or publishing."""

    model_config = ConfigDict(extra="forbid")

    title: Annotated[str | None, Field(default=None, min_length=1, max_length=240)] = None
    description: Annotated[str | None, Field(default=None, min_length=1, max_length=4000)] = None
    tags: list[Annotated[str, Field(min_length=1, max_length=80)]] | None = None
    producer_name: Annotated[str | None, Field(default=None, min_length=1, max_length=200)] = None

    @model_validator(mode="after")
    def _tags_unique_when_present(self) -> SubmissionMetadataPatch:
        if self.tags is not None and len(self.tags) != len({tag.casefold() for tag in self.tags}):
            raise ValueError("tags must be unique")
        return self


class ContributorSubmission(BaseModel):
    """Full submission state visible to operators."""

    model_config = ConfigDict(extra="forbid")

    submission_id: Slug
    receipt_token: Annotated[str, Field(min_length=24, max_length=160)]
    contributor: ContributorAccount
    channel_id: Slug
    title: Annotated[str, Field(min_length=1, max_length=240)]
    description: Annotated[str, Field(min_length=1, max_length=4000)]
    tags: list[Annotated[str, Field(min_length=1, max_length=80)]]
    producer_name: Annotated[str, Field(min_length=1, max_length=200)]
    requested_air_date: datetime | None = None
    media: SubmissionMediaReference
    agreements: list[SubmissionAgreementAcceptance]
    notifications: list[SubmissionNotificationPreference]
    state: ContributorSubmissionState
    broken_media_gate: BrokenMediaGateResult = Field(default_factory=BrokenMediaGateResult)
    asset_id: Annotated[
        str | None,
        Field(
            default=None,
            max_length=64,
            pattern=r"^[a-z0-9][a-z0-9-]{2,63}$",
            description=(
                "Library asset created by ffprobe ingest when this submission was "
                "accepted. None until acceptance completes a real ingest; a submission "
                "cannot be scheduled while this is None."
            ),
        ),
    ] = None
    operator_notes: Annotated[str | None, Field(default=None, max_length=2000)] = None
    decline_reason: Annotated[str | None, Field(default=None, max_length=1000)] = None
    schedule_handoff: ScheduleHandoff | None = None
    created_at: datetime
    updated_at: datetime
    status_history: list[Annotated[str, Field(min_length=1, max_length=300)]]
    notifications_sent: list[SubmissionStatusNotification] = Field(default_factory=list)


class ContributorSubmissionReceipt(BaseModel):
    """Public acknowledgement returned after a contributor submission."""

    model_config = ConfigDict(extra="forbid")

    submission_id: Slug
    receipt_token: Annotated[str, Field(min_length=24, max_length=160)]
    state: ContributorSubmissionState
    status_url: Annotated[str, Field(min_length=1, max_length=500)]
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]


class PublicSubmissionStatus(BaseModel):
    """Contributor-safe status payload without operator-only internals."""

    model_config = ConfigDict(extra="forbid")

    submission_id: Slug
    title: Annotated[str, Field(min_length=1, max_length=240)]
    state: ContributorSubmissionState
    producer_name: Annotated[str, Field(min_length=1, max_length=200)]
    updated_at: datetime
    status_message: Annotated[str, Field(min_length=1, max_length=500)]
    decline_reason: Annotated[str | None, Field(default=None, max_length=1000)] = None
    schedule_handoff: ScheduleHandoff | None = None


class ContributorReviewRequest(BaseModel):
    """Operator review action for a contributor submission."""

    model_config = ConfigDict(extra="forbid")

    action: ReviewAction
    metadata_patch: SubmissionMetadataPatch | None = None
    broken_media_gate: BrokenMediaGateResult | None = None
    operator_notes: Annotated[str | None, Field(default=None, max_length=2000)] = None
    decline_reason: Annotated[str | None, Field(default=None, max_length=1000)] = None
    schedule_handoff: ScheduleHandoff | None = None

    @model_validator(mode="after")
    def _action_specific_fields(self) -> ContributorReviewRequest:
        if self.action == "decline" and not self.decline_reason:
            raise ValueError("decline actions require decline_reason")
        if self.action == "schedule" and self.schedule_handoff is None:
            raise ValueError("schedule actions require schedule_handoff")
        return self


class ContributorReviewQueue(BaseModel):
    """Operator queue for external producer submissions."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    submissions: list[ContributorSubmission]
    needs_operator_action: int
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]


class ProducerActivityReportRow(BaseModel):
    """Grant/franchise reporting row for one producer account."""

    model_config = ConfigDict(extra="forbid")

    contributor_id: Slug
    producer_name: Annotated[str, Field(min_length=1, max_length=200)]
    submitted_count: Annotated[int, Field(ge=0)]
    accepted_count: Annotated[int, Field(ge=0)]
    scheduled_count: Annotated[int, Field(ge=0)]
    declined_count: Annotated[int, Field(ge=0)]
    latest_submission_at: datetime


class ProducerActivityReport(BaseModel):
    """Contributor activity report for station reporting obligations."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    rows: list[ProducerActivityReportRow]
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]


class SubmissionAgreementCatalog(BaseModel):
    """Current public terms-of-submission catalog."""

    model_config = ConfigDict(extra="forbid")

    agreement_id: Slug
    version: Annotated[str, Field(min_length=1, max_length=80)]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    summary: Annotated[str, Field(min_length=1, max_length=1000)]
    effective_at: datetime


def utc_now() -> datetime:
    """Return a normalized UTC timestamp for deterministic contracts."""

    return datetime.now(UTC).replace(microsecond=0)
