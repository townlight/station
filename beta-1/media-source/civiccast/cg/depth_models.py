# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""CG depth entities (S18 gap 6 / S6 "CG depth" addendum — build step 7, slice 6).

Extends the board/zone/bulletin model with the depth incumbent PEG graphics ships:

* :class:`BulletinMedia` — richer bulletin content: an uploaded image, a
  fullscreen slide asset, or a **live-video** input composited into a zone.
* :class:`BulletinAudio` — a per-bulletin narration or a per-channel background
  bed, mixed under the S11 loudness path so it never clips program audio.
* :class:`ZoneTag` — a channel-scoped tag; a zone's ``allowed_tags`` (added to
  :class:`~civiccast.cg.board_models.CgZoneConfig` in slice 6) restricts the
  zone to tagged content.

These are the authoring/persistence layer. The channel background bed is
rendered into between-program board segments by the FFmpeg compositor; live
video inside a board zone remains separate work. No hard FKs —
``bulletin_id`` / ``target_id`` / ``channel_id`` are soft string references
resolved in the store (matching the rest of the cg module).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import DateTime, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base

BulletinMediaKind = Literal["uploaded_image", "fullscreen_slide", "live_video"]
BulletinAudioScope = Literal["bulletin", "channel"]

# A live-video bulletin sources its picture from a live input; the image/slide
# kinds reference an uploaded asset instead.
_LIVE_KINDS: frozenset[str] = frozenset({"live_video"})

__all__ = [
    "BulletinAudio",
    "BulletinAudioDb",
    "BulletinMedia",
    "BulletinMediaDb",
    "ZoneTag",
    "ZoneTagDb",
]


# ---------------------------------------------------------------------------
# Pydantic domain models
# ---------------------------------------------------------------------------


class BulletinMedia(BaseModel):
    """Richer content for a bulletin: an image, a fullscreen slide, or live video."""

    model_config = ConfigDict(extra="forbid")

    media_id: Annotated[str, Field(min_length=1, max_length=120)]
    bulletin_id: Annotated[str, Field(min_length=1, max_length=120)]
    kind: BulletinMediaKind
    asset_ref: Annotated[str | None, Field(default=None, max_length=120)] = None
    live_source: Annotated[str | None, Field(default=None, max_length=200)] = None
    created_at: datetime

    @model_validator(mode="after")
    def _content_matches_kind(self) -> BulletinMedia:
        if self.kind in _LIVE_KINDS:
            if not self.live_source:
                raise ValueError("live_video media requires a live_source")
            if self.asset_ref is not None:
                raise ValueError("live_video media must not carry an asset_ref")
        else:
            if not self.asset_ref:
                raise ValueError(f"{self.kind} media requires an asset_ref")
            if self.live_source is not None:
                raise ValueError(f"{self.kind} media must not carry a live_source")
        return self


class BulletinAudio(BaseModel):
    """Per-bulletin narration or per-channel background bed (loudness-managed)."""

    model_config = ConfigDict(extra="forbid")

    audio_id: Annotated[str, Field(min_length=1, max_length=120)]
    scope: BulletinAudioScope
    target_id: Annotated[str, Field(min_length=1, max_length=120)]  # bulletin_id or channel_id
    asset_ref: Annotated[str, Field(min_length=1, max_length=120)]
    loudness_regime: Annotated[str, Field(min_length=1, max_length=40)] = "inherit"
    gain_db: Annotated[float, Field(ge=-36.0, le=0.0, allow_inf_nan=False)] = 0.0
    created_at: datetime


class ZoneTag(BaseModel):
    """A channel-scoped tag used to filter content into zones (allowed_tags)."""

    model_config = ConfigDict(extra="forbid")

    tag_id: Annotated[str, Field(min_length=1, max_length=120)]
    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=80)]
    created_at: datetime


# ---------------------------------------------------------------------------
# SQLAlchemy ORM peers (migration 0045 creates these tables)
# ---------------------------------------------------------------------------


class BulletinMediaDb(Base):
    """Durable richer-bulletin-content row."""

    __tablename__ = "bulletin_media"

    media_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    bulletin_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    asset_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    live_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class BulletinAudioDb(Base):
    """Durable bulletin/channel background-audio row."""

    __tablename__ = "bulletin_audio"

    audio_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    asset_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    loudness_regime: Mapped[str] = mapped_column(
        String(40), nullable=False, default="inherit", server_default="inherit"
    )
    gain_db: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class ZoneTagDb(Base):
    """Durable channel-scoped zone tag."""

    __tablename__ = "zone_tags"

    tag_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
