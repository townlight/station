# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""FastAPI routes for v0.8 podcast episodes and feeds."""

from __future__ import annotations

import hashlib
import os
from typing import cast
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse

from civiccast.auth.roles import require_any_role
from civiccast.platform.stores import resolve_app_store
from civiccast.playback_policy.entitlements import ViewerTokenError, viewer_from_token
from civiccast.playback_policy.models import PlaybackPolicyEvaluationRequest
from civiccast.playback_policy.router import get_playback_policy_store
from civiccast.playback_policy.store import PlaybackPolicyStore
from civiccast.podcast.models import PodcastEpisode, PodcastEpisodeCreate
from civiccast.podcast.rss import render_podcast_rss
from civiccast.podcast.service import podcast_media_dir
from civiccast.podcast.store import PodcastStore

public_router = APIRouter(prefix="/api/public/podcast", tags=["public", "podcast"])
staff_router = APIRouter(prefix="/api/staff/podcast", tags=["staff", "podcast"])


def get_podcast_store(request: Request) -> PodcastStore:
    return cast(PodcastStore, resolve_app_store(request, "podcast_store", surface="Podcast store"))


def _require_channel_access(
    channel_id: str,
    token: str | None,
    playback_store: PlaybackPolicyStore,
    *,
    asset_id: str | None = None,
) -> None:
    policy = (
        playback_store.effective_policy(asset_id, channel_id)
        if asset_id is not None
        else playback_store.get_policy("channel", channel_id)
    )
    if policy.access_tier == "public":
        return
    if not policy.authenticated_rss_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authenticated podcast access is not enabled for this channel.",
        )
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A signed viewer token is required for this podcast.",
        )
    try:
        viewer = viewer_from_token(token)
    except (ValueError, ViewerTokenError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    decision = playback_store.evaluate(
        PlaybackPolicyEvaluationRequest(
            asset_id=asset_id or f"{channel_id}-podcast-feed",
            channel_id=channel_id,
            viewer=viewer,
        )
    )
    if not decision.allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=decision.reason)


@public_router.get("/audio/{asset_id}.mp3", summary="Stream a published podcast enclosure")
def podcast_audio(
    asset_id: str,
    token: str | None = Query(default=None, max_length=5000),
    store: PodcastStore = Depends(get_podcast_store),
    playback_store: PlaybackPolicyStore = Depends(get_playback_policy_store),
) -> FileResponse:
    episode = store.get_episode(asset_id)
    if episode is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Podcast audio not found."
        )
    _require_channel_access(episode.channel_id, token, playback_store, asset_id=asset_id)
    private = playback_store.effective_policy(asset_id, episode.channel_id).access_tier != "public"
    filename = f"{hashlib.sha256(asset_id.encode('utf-8')).hexdigest()[:32]}.mp3"
    audio_path = podcast_media_dir() / filename
    if not audio_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Podcast audio not found."
        )
    return FileResponse(
        audio_path,
        media_type="audio/mpeg",
        headers={"Cache-Control": "private, no-store"} if private else None,
    )


@staff_router.post(
    "/episodes",
    response_model=PodcastEpisode,
    summary="Create a podcast episode through Publish approval",
    dependencies=[Depends(require_any_role("publish_operator", "support_admin"))],
)
def create_episode(
    request: PodcastEpisodeCreate,
) -> PodcastEpisode:
    # A caller-supplied local path must never be converted by a public API.
    # Publish approval resolves the asset's own managed recording first.
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Use the Publish dashboard to create podcast audio from this asset's recording.",
    )


@public_router.get(
    "/{channel_id}.xml",
    summary="Read public podcast RSS feed",
)
def podcast_feed(
    request: Request,
    channel_id: str,
    token: str | None = Query(default=None, max_length=5000),
    store: PodcastStore = Depends(get_podcast_store),
    playback_store: PlaybackPolicyStore = Depends(get_playback_policy_store),
) -> Response:
    _require_channel_access(channel_id, token, playback_store)
    visible_episodes: list[PodcastEpisode] = []
    enclosure_bytes: dict[str, int] = {}
    for episode in store.list_for_channel(channel_id):
        filename = f"{hashlib.sha256(episode.asset_id.encode('utf-8')).hexdigest()[:32]}.mp3"
        audio_path = podcast_media_dir() / filename
        if not audio_path.is_file():
            continue
        policy = playback_store.effective_policy(episode.asset_id, channel_id)
        if policy.access_tier != "public":
            try:
                _require_channel_access(
                    channel_id, token, playback_store, asset_id=episode.asset_id
                )
            except HTTPException:
                continue
            episode = episode.model_copy(
                update={"audio_url": f"{episode.audio_url}?token={quote(token or '', safe='')}"}
            )
        visible_episodes.append(episode)
        enclosure_bytes[episode.asset_id] = audio_path.stat().st_size
    public_base = os.environ.get("CIVICCAST_PUBLIC_BASE_URL", str(request.base_url)).rstrip("/")
    xml = render_podcast_rss(
        channel_id=channel_id,
        episodes=visible_episodes,
        enclosure_bytes=enclosure_bytes,
        feed_url=f"{public_base}/api/public/podcast/{quote(channel_id, safe='')}.xml",
    )
    return Response(
        content=xml,
        media_type="application/rss+xml",
        headers={"Cache-Control": "private, no-store"} if token else None,
    )
