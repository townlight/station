# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable CG bulletin-board *designer* entities (S6 V1 — build step 7).

The contract layer (:mod:`civiccast.cg.models`) and the community-bulletin
persistence (:mod:`civiccast.cg.bulletin_store`) already shipped. This module
adds the **authoring/management layer** the incumbent PEG platform's CG Player provides: a
durable per-channel board that binds a template to a set of zones, each zone
sourcing content from a registered feed, a manual text editor, an image, the
clock, the schedule, or the emergency overlay. Plus an append-only audit log
and a per-item approval gate for untrusted feeds.

Naming reconciliation (S6 §3 vs this code): the spec calls the zone→feed link
``feed_adapter_id`` in one place and the entity ``CgFeedSource`` in another. We
use ``feed_source_id`` consistently — it unambiguously references
``CgFeedSource.feed_source_id``. The spec's §3 ``CgZoneConfig`` table also
omitted a home for the **manual** zone text and the **image** asset that its own
§2/§5/D13 require shipped in V1; we add the nullable ``manual_text`` /
``image_asset_ref`` columns so those content modes have a durable binding.

No hard foreign keys: ``channel_id`` / ``board_id`` / ``feed_source_id`` are
soft string references resolved in the store, matching the cg + schedule
modules' convention (``cg_bulletins`` and ``schedule_items`` have none). A board
audit record must outlive the board it describes, and deleting a feed must not
cascade into the zones that named it (the resolver degrades the zone instead).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.cg.models import FeedKind, FeedTrustTier, TemplateRegion, ZoneKind
from civiccast.db import Base

ZoneContentSource = Literal[
    "feed_adapter", "manual", "schedule", "emergency", "image", "clock", "live_video"
]

# A zone whose content comes from a registered feed needs a feed bound to it;
# the other modes carry their content inline (manual_text / image_asset_ref) or
# resolve it at render time (schedule / emergency / clock).
_FEED_SOURCED: frozenset[str] = frozenset({"feed_adapter"})

__all__ = [
    "CgBoard",
    "CgBoardAuditDb",
    "CgBoardAuditEvent",
    "CgBoardDb",
    "CgFeedItemApproval",
    "CgFeedItemApprovalDb",
    "CgFeedSource",
    "CgFeedSourceDb",
    "CgZoneConfig",
    "CgZoneConfigDb",
    "ZoneContentSource",
]


# ---------------------------------------------------------------------------
# Pydantic domain models
# ---------------------------------------------------------------------------


class CgBoard(BaseModel):
    """Durable board configuration for one channel (binds a template)."""

    model_config = ConfigDict(extra="forbid")

    board_id: Annotated[str, Field(min_length=1, max_length=120)]
    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    template_id: Annotated[str, Field(min_length=1, max_length=120)]
    active: bool = True
    created_by: Annotated[str, Field(min_length=1, max_length=120)]
    created_at: datetime
    updated_at: datetime


class CgZoneConfig(BaseModel):
    """Durable binding of one zone within a board to its content source."""

    model_config = ConfigDict(extra="forbid")

    zone_id: Annotated[str, Field(min_length=1, max_length=120)]
    board_id: Annotated[str, Field(min_length=1, max_length=120)]
    region: TemplateRegion
    zone_kind: ZoneKind
    content_source: ZoneContentSource
    feed_source_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    refresh_seconds: Annotated[int | None, Field(default=None, gt=0, le=86400)] = None
    approval_required: bool = False
    manual_text: Annotated[str | None, Field(default=None, max_length=500)] = None
    image_asset_ref: Annotated[str | None, Field(default=None, max_length=120)] = None
    live_source_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    # CG depth (S18 gap 6): when non-empty, the zone only shows content carrying
    # one of these tag ids (see civiccast.cg.depth_models.ZoneTag).
    allowed_tags: list[str] = Field(default_factory=list)
    created_at: datetime

    @model_validator(mode="after")
    def _feed_zone_names_a_feed(self) -> CgZoneConfig:
        if self.content_source in _FEED_SOURCED and not self.feed_source_id:
            raise ValueError("feed_adapter zones require a feed_source_id")
        if self.content_source not in _FEED_SOURCED and self.feed_source_id is not None:
            raise ValueError("feed_source_id is only valid for feed_adapter zones")
        if self.content_source == "live_video":
            if not self.live_source_id:
                raise ValueError("live_video zones require a live_source_id")
            if self.zone_kind == "audio":
                raise ValueError("audio zones cannot show live video")
        elif self.live_source_id is not None:
            raise ValueError("live_source_id is only valid for live_video zones")
        return self


class CgFeedSource(BaseModel):
    """Durable registration of a dynamic feed for a channel's board zones."""

    model_config = ConfigDict(extra="forbid")

    feed_source_id: Annotated[str, Field(min_length=1, max_length=120)]
    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    kind: FeedKind
    label: Annotated[str, Field(min_length=1, max_length=160)]
    source_url: Annotated[str, Field(min_length=1, max_length=500)]
    trust_tier: FeedTrustTier
    refresh_seconds: Annotated[int, Field(gt=0, le=86400)] = 900
    enabled: bool = True
    # CG depth (S18 gap 6): tags stamped onto this feed's items so a zone with
    # allowed_tags can include them (see civiccast.cg.depth_models.ZoneTag).
    tags: list[str] = Field(default_factory=list)
    created_by: Annotated[str, Field(min_length=1, max_length=120)]
    created_at: datetime
    last_fetched_at: datetime | None = None
    last_fetch_error: Annotated[str | None, Field(default=None, max_length=500)] = None

    @field_validator("source_url")
    @classmethod
    def _http_scheme_only(cls, value: str) -> str:
        # Reject non-http(s) schemes (file:// / ftp:// / gopher://) before the URL
        # is ever persisted. The fetch-time guard (feed_fetcher._assert_safe_feed_url)
        # additionally blocks private / loopback / cloud-metadata hosts, which need
        # DNS resolution to detect and so cannot be checked at model-construction time.
        if urlsplit(value).scheme.lower() not in ("http", "https"):
            raise ValueError("source_url must be an http(s) URL")
        return value

    @model_validator(mode="after")
    def _weather_must_be_curated(self) -> CgFeedSource:
        # Parity with CgFeedAdapter: a public-permitted weather feed could inject
        # spoofed alerts onto the channel, so weather must be operator/partner curated.
        if self.kind == "weather" and self.trust_tier == "public_permitted":
            raise ValueError("weather feeds must be operator or partner curated")
        return self


class CgBoardAuditEvent(BaseModel):
    """Append-only board-lifecycle event (who changed what, when)."""

    model_config = ConfigDict(extra="forbid")

    audit_id: Annotated[str, Field(min_length=1, max_length=120)]
    board_id: Annotated[str, Field(min_length=1, max_length=120)]
    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    event_kind: Annotated[str, Field(min_length=1, max_length=50)]
    operator_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    occurred_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class CgFeedItemApproval(BaseModel):
    """One operator approval of a single feed item for an approval-gated zone."""

    model_config = ConfigDict(extra="forbid")

    approval_id: Annotated[str, Field(min_length=1, max_length=120)]
    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    feed_source_id: Annotated[str, Field(min_length=1, max_length=120)]
    item_id: Annotated[str, Field(min_length=1, max_length=120)]
    approved_by_operator: Annotated[str, Field(min_length=1, max_length=120)]
    approved_at: datetime


# ---------------------------------------------------------------------------
# SQLAlchemy ORM peers (single global metadata; migration 0044 creates tables)
# ---------------------------------------------------------------------------


class CgBoardDb(Base):
    """Durable board row."""

    __tablename__ = "cg_boards"

    board_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    template_id: Mapped[str] = mapped_column(String(120), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class CgZoneConfigDb(Base):
    """Durable zone-binding row within a board."""

    __tablename__ = "cg_zone_configs"

    zone_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    board_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    region: Mapped[str] = mapped_column(String(50), nullable=False)
    zone_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    content_source: Mapped[str] = mapped_column(String(20), nullable=False)
    feed_source_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    refresh_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    manual_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_asset_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    live_source_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    allowed_tags: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class CgFeedSourceDb(Base):
    """Durable feed-source registration row."""

    __tablename__ = "cg_feed_sources"

    feed_source_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    source_url: Mapped[str] = mapped_column(String(500), nullable=False)
    trust_tier: Mapped[str] = mapped_column(String(30), nullable=False)
    refresh_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=900)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    tags: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fetch_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class CgBoardAuditDb(Base):
    """Append-only board audit row."""

    __tablename__ = "cg_board_audit"

    audit_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    board_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    channel_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    event_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    operator_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    details_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}"
    )


class CgFeedItemApprovalDb(Base):
    """One approved feed item for an approval-gated zone."""

    __tablename__ = "cg_feed_item_approvals"
    # Mirror the DB-level UNIQUE from migration 0044 in the ORM (same name) so
    # the model is the source of truth and a concurrent double-approve hits a
    # DB constraint rather than silently inserting two rows.
    __table_args__ = (
        UniqueConstraint(
            "channel_id",
            "feed_source_id",
            "item_id",
            name="uq_cg_feed_item_approvals_item",
        ),
    )

    approval_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    feed_source_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    item_id: Mapped[str] = mapped_column(String(120), nullable=False)
    approved_by_operator: Mapped[str] = mapped_column(String(120), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
