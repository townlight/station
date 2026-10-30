# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Staff + public routes for audio program tracks — SAP / descriptive audio (S11 gap 9).

Staff (`/api/staff/audio-tracks`) configure the tracks; a public read
(`/api/public/channels/{id}/audio-tracks`) exposes the enabled tracks for a channel so
the web/OTT player can render an audio-track toggle (DC-SAP2). Descriptive tracks are
labeled by their `kind` so the player can announce them as audio description (DC-SAP3).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from civiccast.auth.roles import require_any_role
from civiccast.egress.audio_tracks import (
    AudioProgramTrack,
    AudioTrackKind,
    AudioTrackScope,
    AudioTrackStore,
    AudioTrackStoreError,
)

_DB_NOT_READY = "Durable storage is not ready yet."
_READ = ("setup_admin", "support_admin", "meeting_operator")
_WRITE = ("setup_admin",)

staff_router = APIRouter(prefix="/api/staff/audio-tracks", tags=["staff", "audio-tracks"])
public_router = APIRouter(prefix="/api/public/channels", tags=["public", "audio-tracks"])


def get_audio_track_store() -> AudioTrackStore | None:
    return None


def _require_store(store: AudioTrackStore | None) -> AudioTrackStore:
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return store


class AudioTrackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track_id: Annotated[str, Field(min_length=1, max_length=120)]
    scope: AudioTrackScope
    target_id: Annotated[str, Field(min_length=1, max_length=120)]
    kind: AudioTrackKind
    language: Annotated[str, Field(min_length=2, max_length=35)]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    source_uri: Annotated[str | None, Field(default=None, max_length=1000)] = None
    loudness_target_lufs: Annotated[float | None, Field(default=None)] = None
    enabled: bool = True


class PublicAudioTrack(BaseModel):
    """The web/OTT-facing view of an audio track (no source URI / internal fields)."""

    model_config = ConfigDict(extra="forbid")

    track_id: str
    kind: AudioTrackKind
    language: str
    label: str


@staff_router.get(
    "", response_model=list[AudioProgramTrack], dependencies=[Depends(require_any_role(*_READ))]
)
def list_audio_tracks(
    scope: AudioTrackScope | None = None,
    target_id: str | None = None,
    store: AudioTrackStore | None = Depends(get_audio_track_store),
) -> list[AudioProgramTrack]:
    return _require_store(store).list_tracks(scope=scope, target_id=target_id)


@staff_router.put(
    "/{track_id}",
    response_model=AudioProgramTrack,
    dependencies=[Depends(require_any_role(*_WRITE))],
)
def upsert_audio_track(
    track_id: str,
    payload: AudioTrackInput,
    store: AudioTrackStore | None = Depends(get_audio_track_store),
) -> AudioProgramTrack:
    if payload.track_id != track_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="track_id mismatch.")
    return _require_store(store).upsert_track(AudioProgramTrack(**payload.model_dump()))


@staff_router.delete(
    "/{track_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_any_role(*_WRITE))],
)
def delete_audio_track(
    track_id: str, store: AudioTrackStore | None = Depends(get_audio_track_store)
) -> None:
    try:
        _require_store(store).delete_track(track_id)
    except AudioTrackStoreError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@public_router.get("/{channel_id}/audio-tracks", response_model=list[PublicAudioTrack])
def public_audio_tracks(
    channel_id: str, store: AudioTrackStore | None = Depends(get_audio_track_store)
) -> list[PublicAudioTrack]:
    """The enabled audio tracks for a channel, for the web/OTT player's track toggle.

    Secondary audio PIDs are only emitted by the GStreamer engine, so on the legacy
    ffmpeg-concat engine this returns just the implicit primary program (no secondary
    tracks) — the player toggle must never advertise a track the running engine
    cannot emit."""
    from civiccast.egress.engine_select import gstreamer_engine_selected

    if not gstreamer_engine_selected():
        return []
    tracks = _require_store(store).list_tracks(
        scope="channel", target_id=channel_id, enabled_only=True
    )
    return [
        PublicAudioTrack(track_id=t.track_id, kind=t.kind, language=t.language, label=t.label)
        for t in tracks
    ]


__all__ = ["get_audio_track_store", "public_router", "staff_router"]
