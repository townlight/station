# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Deterministic YouTube syndication adapter models for v0.7."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class YouTubePublishProof(BaseModel):
    """Result from a YouTube Live or VOD mock publish."""

    model_config = ConfigDict(extra="forbid")

    target_type: Literal["youtube_live", "youtube_vod"]
    url: Annotated[str, Field(min_length=1)]
    credential_key: Annotated[str, Field(min_length=1)]
    delivery_state: Literal[
        "simulated", "scheduled", "configured", "starting", "live", "failed", "stopped"
    ] = "simulated"
    confirmed_live: bool = False
    channel_id: str | None = None
    broadcast_id: str | None = None
    stream_id: str | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None


class MockYouTubeClient:
    """Deterministic YouTube reach client for CI and local proof."""

    def publish_live(
        self,
        *,
        asset_id: str,
        title: str | None = None,
        channel_id: str | None = None,
        scheduled_start: datetime | None = None,
        scheduled_end: datetime | None = None,
        broadcast_id: str | None = None,
        stream_id: str | None = None,
    ) -> YouTubePublishProof:
        return YouTubePublishProof(
            target_type="youtube_live",
            url=f"https://youtube.example/watch?v={asset_id}",
            credential_key="os-credential://civiccast/youtube/live/mock",
        )

    def upload_vod(self, *, asset_id: str) -> YouTubePublishProof:
        return YouTubePublishProof(
            target_type="youtube_vod",
            url=f"https://youtube.example/watch?v={asset_id}",
            credential_key="os-credential://civiccast/youtube/vod/mock",
        )
