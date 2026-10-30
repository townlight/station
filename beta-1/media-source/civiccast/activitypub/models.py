# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""ActivityPub local persistence models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base

FollowerStatus = Literal["pending", "accepted", "blocked", "rejected", "removed"]


class FollowerRecord(BaseModel):
    """One remote actor that attempted to follow the local station actor."""

    model_config = ConfigDict(extra="forbid")

    actor: Annotated[str, Field(min_length=1, max_length=500)]
    domain: Annotated[str, Field(min_length=1, max_length=253)]
    status: FollowerStatus
    activity_id: Annotated[str, Field(min_length=1, max_length=500)]
    inbox_url: Annotated[str, Field(min_length=1, max_length=500)]
    shared_inbox_url: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    public_key_id: Annotated[str, Field(min_length=1, max_length=500)]
    public_key_pem: Annotated[str, Field(min_length=1)]
    created_at: datetime


class OutboxRecord(BaseModel):
    """One locally generated ActivityPub activity."""

    model_config = ConfigDict(extra="forbid")

    activity_id: Annotated[str, Field(min_length=1, max_length=500)]
    activity: dict[str, object]
    created_at: datetime


class DeliveryRecord(BaseModel):
    """One signed delivery attempt to a remote ActivityPub inbox."""

    model_config = ConfigDict(extra="forbid")

    delivery_id: Annotated[str, Field(min_length=1, max_length=120)]
    activity_id: Annotated[str, Field(min_length=1, max_length=500)]
    inbox_url: Annotated[str, Field(min_length=1, max_length=500)]
    status_code: int
    response_body: str = ""
    created_at: datetime


class ActivityPubFollower(Base):
    """Durable remote actor state for ActivityPub federation."""

    __tablename__ = "activitypub_followers"

    actor: Mapped[str] = mapped_column(String(500), primary_key=True)
    domain: Mapped[str] = mapped_column(String(253), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    activity_id: Mapped[str] = mapped_column(String(500), nullable=False)
    inbox_url: Mapped[str] = mapped_column(String(500), nullable=False)
    shared_inbox_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    public_key_id: Mapped[str] = mapped_column(String(500), nullable=False)
    public_key_pem: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    def to_record(self) -> FollowerRecord:
        created_at = self.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return FollowerRecord(
            actor=self.actor,
            domain=self.domain,
            status=cast(FollowerStatus, self.status),
            activity_id=self.activity_id,
            inbox_url=self.inbox_url,
            shared_inbox_url=self.shared_inbox_url,
            public_key_id=self.public_key_id,
            public_key_pem=self.public_key_pem,
            created_at=created_at,
        )


class ActivityPubOutboxActivity(Base):
    """Durable locally generated ActivityPub activity."""

    __tablename__ = "activitypub_outbox"

    activity_id: Mapped[str] = mapped_column(String(500), primary_key=True)
    activity_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class ActivityPubDeliveryAttempt(Base):
    """Durable signed delivery attempt for an ActivityPub activity."""

    __tablename__ = "activitypub_delivery_attempts"

    delivery_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    activity_id: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    inbox_url: Mapped[str] = mapped_column(String(500), nullable=False)
    status_code: Mapped[int] = mapped_column(nullable=False)
    response_body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


DeliveryRetryState = Literal["pending", "delivered", "dead_letter"]


class DeliveryRetryRecord(BaseModel):
    """A failed delivery queued for retry (Stage F retry worker)."""

    model_config = ConfigDict(extra="forbid")

    retry_id: Annotated[str, Field(min_length=1, max_length=120)]
    activity_id: Annotated[str, Field(min_length=1, max_length=500)]
    inbox_url: Annotated[str, Field(min_length=1, max_length=500)]
    activity: dict[str, object]
    state: DeliveryRetryState
    attempts: int
    next_attempt_at: datetime | None = None
    last_status_code: int
    last_error: str = ""
    created_at: datetime
    updated_at: datetime


class ActivityPubDeliveryRetry(Base):
    """Durable retry-queue row for a failed ActivityPub delivery."""

    __tablename__ = "activitypub_delivery_retries"

    retry_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    activity_id: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    inbox_url: Mapped[str] = mapped_column(String(500), nullable=False)
    activity_json: Mapped[str] = mapped_column(Text, nullable=False)
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
