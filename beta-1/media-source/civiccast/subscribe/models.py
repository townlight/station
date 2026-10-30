# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Closed data contracts for v0.8 subscriptions and notifications."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base

SubscriptionChannel = Literal["email", "webhook"]
SubscriptionTargetType = Literal["channel", "meeting_body"]
SubscriptionStatus = Literal["pending_confirmation", "confirmed", "unsubscribed"]


class SubscriptionSignupRequest(BaseModel):
    """Resident email double-opt-in signup request."""

    model_config = ConfigDict(extra="forbid")

    email: Annotated[str, Field(min_length=3, max_length=320)]
    target_type: SubscriptionTargetType
    target_id: Annotated[str, Field(min_length=1, max_length=120)]

    @field_validator("email")
    @classmethod
    def _email_shape(cls, value: str) -> str:
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("email must include a local part and domain")
        return value.lower()


class SubscriptionWebhookRequest(BaseModel):
    """Webhook registration; ownership proof is delivered only to the HTTPS endpoint."""

    model_config = ConfigDict(extra="forbid")

    webhook_url: HttpUrl
    target_type: SubscriptionTargetType
    target_id: Annotated[str, Field(min_length=1, max_length=120)]


class SubscriptionRecord(BaseModel):
    """Persisted subscription row without plaintext subscriber PII."""

    model_config = ConfigDict(extra="forbid")

    subscription_id: Annotated[str, Field(min_length=1, max_length=160)]
    channel: SubscriptionChannel
    encrypted_subscriber_handle: Annotated[str, Field(min_length=1)]
    target_type: SubscriptionTargetType
    target_id: Annotated[str, Field(min_length=1, max_length=120)]
    status: SubscriptionStatus
    confirmation_token: Annotated[str, Field(min_length=1)]
    unsubscribe_token: Annotated[str, Field(min_length=1)]
    encrypted_webhook_secret: str | None = None
    created_at: datetime
    confirmed_at: datetime | None = None
    unsubscribed_at: datetime | None = None

    @model_validator(mode="after")
    def _webhook_secret_matches_channel(self) -> SubscriptionRecord:
        if self.channel == "webhook" and not self.encrypted_webhook_secret:
            raise ValueError("webhook subscriptions require encrypted_webhook_secret")
        return self


class SubscriptionPublicResponse(BaseModel):
    """Safe resident-facing subscription state."""

    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    channel: SubscriptionChannel
    target_type: SubscriptionTargetType
    target_id: str
    status: SubscriptionStatus
    message: str
    next_step: str
    confirmation_token: str | None = None
    unsubscribe_token: str | None = None


class SubscriptionConfirmResponse(BaseModel):
    """Result of confirm or unsubscribe link action."""

    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    status: SubscriptionStatus
    message: str
    next_step: str


class NotificationPayload(BaseModel):
    """Payload dispatched when a recording publishes."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str
    title: str
    portal_url: str
    podcast_url: str | None = None
    summary: str | None = None
    published_at: datetime
    #: Per-recipient one-click unsubscribe link, carrying that subscription's
    #: own signed token. Every notice must offer a way out: it goes in the mail
    #: body AND the ``List-Unsubscribe`` header, and rides in the webhook JSON
    #: so a webhook subscriber can stop notices without contacting the station.
    #: ``None`` only when the station has no public web address configured, in
    #: which case there is no link to any station route at all.
    unsubscribe_url: str | None = None


class NotificationDelivery(BaseModel):
    """Proof that a notification was attempted."""

    model_config = ConfigDict(extra="forbid")

    delivery_id: str
    subscription_id: str
    channel: SubscriptionChannel
    status: Literal["sent", "failed"]
    target_type: SubscriptionTargetType
    target_id: str
    dispatched_at: datetime
    message: str
    signature: str | None = None


class NotificationDispatchResponse(BaseModel):
    """Aggregate notification dispatch result for a published asset."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str
    sent: int
    failed: int
    #: Recipients this run did NOT contact because a durable delivery outcome
    #: already showed them as delivered for this publication (WP-05). Counting
    #: them as ``sent`` would let a re-approval that sent nothing look like a
    #: fresh successful fan-out.
    skipped: int = 0
    deliveries: list[NotificationDelivery]


WebhookRetryState = Literal["pending", "delivered", "dead_letter"]


class WebhookRetryRecord(BaseModel):
    """A failed webhook delivery queued for retry (issue #111).

    Carries only the subscription id and the notification payload: the
    webhook URL and per-subscription secret stay sealed in the subscriptions
    table and are reopened at send time, so this queue adds no plaintext PII.
    """

    model_config = ConfigDict(extra="forbid")

    retry_id: Annotated[str, Field(min_length=1, max_length=120)]
    subscription_id: Annotated[str, Field(min_length=1, max_length=160)]
    payload: dict[str, object]
    state: WebhookRetryState
    attempts: int
    next_attempt_at: datetime | None = None
    last_status_code: int
    last_error: str = ""
    created_at: datetime
    updated_at: datetime


class SubscriptionWebhookRetry(Base):
    """Durable retry-queue row for a failed subscriber webhook delivery."""

    __tablename__ = "subscription_webhook_retries"

    retry_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    subscription_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status_code: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


# ---------------------------------------------------------------------------
# WP-05: durable per-delivery notification outcomes.
#
# Publish used to report the subscriber-notifications surface "succeeded" as
# soon as it had BUILT a payload -- nothing was ever sent, and nothing was ever
# recorded. These rows are the receipt that makes the surface state an
# observation instead of an assertion.
#
# PII rule for both tables: stable ids only. The subscription id is a salted
# digest (``civiccast.subscribe.service._subscription_id``); the email address
# and webhook URL stay sealed in ``subscriptions.encrypted_subscriber_handle``
# and are never copied here, nor is any secret or signature. ``detail`` is a
# short redacted operator sentence, never an exception string that could carry
# a recipient (see ``civiccast.publish.notifications.redact_delivery_detail``).
# ---------------------------------------------------------------------------

NotificationDeliveryOutcomeValue = Literal["pending", "sent", "failed", "queued"]

NotificationTransport = SubscriptionChannel


class NotificationDeliveryOutcomeRecord(BaseModel):
    """One logical delivery: publication x subscription x target x transport.

    ``delivery_key`` deliberately does NOT include the attempt number -- it is
    the duplicate-send guard, so every retry of the same recipient must land on
    the same row (WP-05 plan item 4). Numbered attempts live beneath it in
    :class:`NotificationDeliveryAttemptRecord`.
    """

    model_config = ConfigDict(extra="forbid")

    delivery_key: Annotated[str, Field(min_length=1, max_length=80)]
    publication_id: Annotated[str, Field(min_length=1, max_length=200)]
    asset_id: Annotated[str, Field(min_length=1, max_length=160)]
    subscription_id: Annotated[str, Field(min_length=1, max_length=160)]
    target_type: SubscriptionTargetType
    target_id: Annotated[str, Field(min_length=1, max_length=120)]
    transport: NotificationTransport
    outcome: NotificationDeliveryOutcomeValue
    attempts: Annotated[int, Field(ge=0)] = 0
    error_code: Annotated[str, Field(max_length=80)] | None = None
    detail: Annotated[str, Field(max_length=500)] = ""
    retry_id: Annotated[str, Field(max_length=120)] | None = None
    #: While set and in the future, one caller owns this in-flight delivery and
    #: no other may send it. Cleared when an attempt records its result. A row
    #: whose lease has expired without a result is recoverable -- the sending
    #: process died mid-send -- and the next caller may take it over.
    lease_expires_at: datetime | None = None
    first_attempted_at: datetime | None = None
    last_attempted_at: datetime | None = None
    succeeded_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class NotificationDeliveryAttemptRecord(BaseModel):
    """One numbered attempt beneath a logical delivery."""

    model_config = ConfigDict(extra="forbid")

    delivery_key: Annotated[str, Field(min_length=1, max_length=80)]
    attempt_number: Annotated[int, Field(ge=1)]
    attempted_at: datetime
    outcome: NotificationDeliveryOutcomeValue
    error_code: Annotated[str, Field(max_length=80)] | None = None
    detail: Annotated[str, Field(max_length=500)] = ""
    retry_id: Annotated[str, Field(max_length=120)] | None = None


class NotificationDeliveryOutcome(Base):
    """Durable logical-delivery row (migration ``0085``).

    The UNIQUE constraint over the logical identity -- not the primary key
    alone -- is the concurrency/idempotency guard the plan requires: two
    approvals racing on the same recording cannot create two rows for the same
    recipient, so the second one observes the first's outcome instead of
    sending a duplicate notice.
    """

    __tablename__ = "notification_delivery_outcomes"
    __table_args__ = (
        UniqueConstraint(
            "publication_id",
            "subscription_id",
            "target_type",
            "target_id",
            "transport",
            name="notification_delivery_outcomes_logical_key",
        ),
    )

    delivery_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    publication_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    asset_id: Mapped[str] = mapped_column(String(160), nullable=False)
    subscription_id: Mapped[str] = mapped_column(String(160), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(120), nullable=False)
    transport: Mapped[str] = mapped_column(String(20), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # Links a queued webhook delivery back to its retry-queue row so the
    # dead-letter/backoff outcome stays visible from the publish run.
    retry_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    #: In-flight lease. See NotificationDeliveryOutcomeRecord.lease_expires_at.
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class NotificationDeliveryAttempt(Base):
    """One numbered attempt beneath a :class:`NotificationDeliveryOutcome`."""

    __tablename__ = "notification_delivery_attempts"

    delivery_key: Mapped[str] = mapped_column(
        String(80),
        ForeignKey("civiccast.notification_delivery_outcomes.delivery_key", ondelete="CASCADE"),
        primary_key=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    retry_id: Mapped[str | None] = mapped_column(String(120), nullable=True)


class RssItem(BaseModel):
    """One public RSS feed item."""

    title: str
    link: str
    guid: str
    published_at: datetime
    description: str
