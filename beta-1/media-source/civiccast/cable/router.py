# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""FastAPI routes for software channel and CTV beta contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import RedirectResponse

from civiccast.auth.roles import ALL_OPERATOR_ROLES, require_any_role
from civiccast.cable.channel import (
    HLS_OUTPUT_NOT_ENABLED_NEXT_STEP,
    ChannelNowNext,
    ChannelPlayoutPlan,
    ChannelProfile,
    ChannelProofLog,
    CtvFeed,
    build_channel_now_next,
    build_channel_playout_plan,
    build_channel_proof_log,
    build_ctv_feed,
    channel_has_hls_sink,
    default_channel_profiles,
    get_channel_profile,
    local_live_manifest_path,
    resolve_live_outputs,
)
from civiccast.captions.live_sidecar import active_caption_sidecar
from civiccast.captions.tap import build_audio_tap_plan
from civiccast.captions.webvtt import render_webvtt
from civiccast.egress.automation import default_egress_work_dir
from civiccast.egress.caption_embed import load_caption_cues_from_timed_text
from civiccast.egress.gst.audio_tap import read_program_boundary
from civiccast.egress.models import EgressStateRow
from civiccast.egress.router import get_egress_store
from civiccast.egress.store import EgressStore
from civiccast.schedule.models import SCHEDULE_STATE_SCHEDULED, ScheduleItemResponse
from civiccast.schedule.router import get_schedule_store

public_router = APIRouter(prefix="/api/public/channels", tags=["public", "channels"])
staff_router = APIRouter(prefix="/api/staff/cable/channels", tags=["staff", "cable"])


@public_router.get(
    "",
    response_model=list[ChannelProfile],
    summary="List public linear channel profiles",
)
def list_public_channels(egress_store: Any = Depends(get_egress_store)) -> list[ChannelProfile]:
    return resolve_live_outputs(default_channel_profiles(), egress_store)


@public_router.get(
    "/{channel_id}/live.m3u8",
    response_class=RedirectResponse,
    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    summary="Redirect to the channel's live HLS manifest",
    responses={
        307: {"description": "Redirects to /media/live/{channel_id}/playlist.m3u8"},
        404: {"description": "Unknown channel, or HLS web output is not enabled for it"},
    },
)
def public_channel_live_manifest(
    channel_id: str,
    egress_store: Any = Depends(get_egress_store),
) -> RedirectResponse:
    """Make the advertised ``/api/public/channels/{id}/live.m3u8`` URL true.

    The Channels screen, the CTV feed and the app-platform schedule feed all
    print this URL. It used to be a bare string with no route behind it (404
    for every channel, on air or not — beta.5 walkthrough). Now it redirects
    to the manifest ``civiccast.stream.media_router`` really serves when the
    channel has an ``hls`` egress sink, and otherwise answers 404 with a
    ``detail`` that names the fix.
    """
    if get_channel_profile(channel_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel profile not found: {channel_id}",
        )
    if not channel_has_hls_sink(channel_id, egress_store):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{HLS_OUTPUT_NOT_ENABLED_NEXT_STEP} (channel: {channel_id})",
        )
    return RedirectResponse(
        url=local_live_manifest_path(channel_id),
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )


@public_router.get(
    "/ctv/feed",
    response_model=CtvFeed,
    summary="Read the reference connected-TV feed",
)
def public_ctv_feed(station_name: str = "CivicCast Test Station") -> CtvFeed:
    return build_ctv_feed(station_name=station_name)


@public_router.get(
    "/{channel_id}/captions.vtt",
    response_class=Response,
    summary="Read the current public WebVTT captions for a channel",
    responses={
        200: {
            "description": "Current live WebVTT captions; Cache-Control: no-store",
            "content": {"text/vtt": {"schema": {"type": "string"}}},
        },
        404: {"description": "Channel or live caption feed not found"},
    },
)
def public_channel_captions_vtt(
    channel_id: str,
    work_dir: Path = Depends(default_egress_work_dir),
) -> Response:
    _require_channel_profile(channel_id)
    root = work_dir.expanduser().resolve()
    sidecar = active_caption_sidecar(root, channel_id).resolve()
    try:
        sidecar.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found") from exc
    try:
        content = sidecar.read_bytes()
    except (FileNotFoundError, IsADirectoryError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Live captions for channel {channel_id!r} are not available.",
        ) from exc
    plan = build_audio_tap_plan(channel_id)
    if plan is not None:
        token, _first_segment = read_program_boundary(plan.tap_dir)
        if token:
            cues = load_caption_cues_from_timed_text(sidecar, source_id=channel_id)
            cues = [cue for cue in cues if cue.cue_id.startswith(f"{channel_id}-{token}-")]
            if read_program_boundary(plan.tap_dir)[0] != token:
                cues = []
            content = render_webvtt(cues).encode("utf-8")
    return Response(
        content=content,
        media_type="text/vtt",
        headers={"Cache-Control": "no-store"},
    )


@public_router.get(
    "/{channel_id}/now-next",
    response_model=ChannelNowNext,
    summary="Read public now/next state for a channel",
    responses={404: {"description": "Channel profile not found"}},
)
def public_channel_now_next(
    channel_id: str,
    egress_store: EgressStore | None = Depends(get_egress_store),
    schedule_store: Any = Depends(get_schedule_store),
) -> ChannelNowNext:
    """Resident-safe now/next: no daemon ``last_error`` or free-text source label.

    This route is unauthenticated. The daemon's ``last_error`` is raw
    ``str(exc)`` / child stderr (file paths, NAS mounts, headend host:port)
    and ``current_source_label`` is operator free text; this route withholds
    both and serves the scheduled program title (already public via the
    schedule routes) or the channel display name instead. The sibling
    ``GET /api/public/egress/channels/{id}/now`` (``civiccast/egress/router.py``)
    still serves ``current_source_label`` unauthenticated; that is outside this
    builder and tracked as a follow-up.
    """
    return _channel_now_next_or_404(
        channel_id, egress_store, schedule_store, include_operator_detail=False
    )


@staff_router.get(
    "",
    response_model=list[ChannelProfile],
    summary="List operator channel profiles",
    dependencies=[Depends(require_any_role(*ALL_OPERATOR_ROLES))],
)
def list_staff_channels(egress_store: Any = Depends(get_egress_store)) -> list[ChannelProfile]:
    return resolve_live_outputs(default_channel_profiles(), egress_store)


@staff_router.get(
    "/{channel_id}/now-next",
    response_model=ChannelNowNext,
    summary="Read operator now/next state for a channel",
    responses={404: {"description": "Channel profile not found"}},
    dependencies=[Depends(require_any_role(*ALL_OPERATOR_ROLES))],
)
def staff_channel_now_next(
    channel_id: str,
    egress_store: EgressStore | None = Depends(get_egress_store),
    schedule_store: Any = Depends(get_schedule_store),
) -> ChannelNowNext:
    return _channel_now_next_or_404(channel_id, egress_store, schedule_store)


@staff_router.get(
    "/{channel_id}/proof-log",
    response_model=ChannelProofLog,
    summary="Read channel playout proof log",
    responses={404: {"description": "Channel profile not found"}},
    dependencies=[Depends(require_any_role(*ALL_OPERATOR_ROLES))],
)
def staff_channel_proof_log(
    channel_id: str,
    egress_store: EgressStore | None = Depends(get_egress_store),
) -> ChannelProofLog:
    """Proof log built only from persisted egress daemon proof events.

    Without a durable egress store (or before the daemon has aired anything)
    the log is empty; the console renders "No proof events yet". It never
    falls back to sample-contract rows (beta.5 walkthrough F-27).
    """
    _require_channel_profile(channel_id)
    proof_events = (
        egress_store.recent_proof_events(channel_id, _PROOF_LOG_LIMIT)
        if egress_store is not None
        else None
    )
    caption_samples = (
        egress_store.recent_caption_proof_samples(channel_id, _CAPTION_PROOF_LIMIT)
        if egress_store is not None
        else None
    )
    return build_channel_proof_log(
        channel_id, proof_events=proof_events, caption_proof_samples=caption_samples
    )


@staff_router.get(
    "/{channel_id}/playout-plan",
    response_model=ChannelPlayoutPlan,
    summary="Read channel schedule-to-playout plan",
    responses={404: {"description": "Channel profile not found"}},
    dependencies=[Depends(require_any_role(*ALL_OPERATOR_ROLES))],
)
def staff_channel_playout_plan(
    channel_id: str,
    schedule_store: Any = Depends(get_schedule_store),
) -> ChannelPlayoutPlan:
    _require_channel_profile(channel_id)
    return build_channel_playout_plan(
        channel_id, schedule_items=_scheduled_rows(schedule_store, channel_id)
    )


_PROOF_LOG_LIMIT = 200
# Caption decode-back samples are taken every few seconds while on air; this
# is enough to cover the join window around each of the last 200 proof events.
_CAPTION_PROOF_LIMIT = 2000


def _require_channel_profile(channel_id: str) -> ChannelProfile:
    profile = get_channel_profile(channel_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown channel profile: {channel_id}",
        )
    return profile


def _scheduled_rows(schedule_store: Any, channel_id: str) -> list[ScheduleItemResponse] | None:
    if schedule_store is None:
        return None
    return cast(
        list[ScheduleItemResponse],
        schedule_store.list(channel_id=channel_id, states=(SCHEDULE_STATE_SCHEDULED,)),
    )


# How many recent proof events the now/next join scans for the one the state
# row points at. Every start and transition appends one; a handful covers
# any realistic gap between the row and its event.
_PROOF_EVENT_JOIN_LIMIT = 50


def _current_source_ref(
    egress_store: EgressStore | None, state: EgressStateRow | None
) -> str | None:
    """The asset id the daemon's own proof event recorded for what it handed off.

    Identity comes from the daemon's persisted records, never from a substring
    of the free-text label (hostile review M5): the proof event the state row
    points at when the row still carries ``current_proof_event_id``, else the
    newest proof event whose ``source_label`` equals the row's label (both are
    the same segment label, folded by ``db_safe_text`` at their own choke
    points). ``None`` when there is no such event -- the builder then refuses
    to borrow caption refs from the schedule.
    """
    if egress_store is None or state is None:
        return None
    events = egress_store.recent_proof_events(state.channel_id, _PROOF_EVENT_JOIN_LIMIT)
    if state.current_proof_event_id is not None:
        for event in events:
            if event.event_id == state.current_proof_event_id:
                return event.source_ref
    if state.current_source_label is None:
        return None
    for event in events:
        if event.source_label == state.current_source_label:
            return event.source_ref
    return None


def _channel_now_next_or_404(
    channel_id: str,
    egress_store: EgressStore | None,
    schedule_store: Any,
    *,
    include_operator_detail: bool = True,
) -> ChannelNowNext:
    """Now/next from the daemon state row plus scheduled premieres.

    No egress store or no state row means nothing is on air (``current`` is
    ``None``); the console renders "No program on air" (F-27).
    """
    _require_channel_profile(channel_id)
    egress_state = egress_store.read_state(channel_id) if egress_store is not None else None
    return build_channel_now_next(
        channel_id,
        schedule_items=_scheduled_rows(schedule_store, channel_id),
        egress_state=egress_state,
        include_operator_detail=include_operator_detail,
        current_source_ref=_current_source_ref(egress_store, egress_state),
    )
