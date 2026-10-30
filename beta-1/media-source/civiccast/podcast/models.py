# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Closed contracts for v0.8 podcast episodes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class PodcastChapter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t: float = Field(ge=0)
    title: Annotated[str, Field(min_length=1, max_length=200)]


class PodcastEpisodeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: Annotated[str, Field(min_length=1, max_length=160)]
    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    title: Annotated[str, Field(min_length=1, max_length=240)]
    portal_url: str
    source_media_url: str
    signed_transcript_url: str | None = None
    summary: str | None = None
    chapters: list[PodcastChapter] = Field(default_factory=list)


class PodcastEpisode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_id: Annotated[str, Field(min_length=1, max_length=160)]
    asset_id: Annotated[str, Field(min_length=1, max_length=160)]
    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    title: Annotated[str, Field(min_length=1, max_length=240)]
    audio_url: str
    rss_guid: Annotated[str, Field(min_length=1, max_length=240)]
    duration_seconds: int | None = Field(default=None, ge=0)
    loudness_lufs: float
    chapters: list[PodcastChapter] = Field(default_factory=list)
    transcript_url: str | None = None
    summary: str | None = None
    published_at: datetime


class PodcastFeed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str
    title: str
    feed_url: str
    episodes: list[PodcastEpisode]
