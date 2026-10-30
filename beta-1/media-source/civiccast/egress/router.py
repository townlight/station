# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Staff API for channel egress configuration and daemon control."""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from civiccast.alerting.resource_sampler import ResourceThresholds
from civiccast.auth.models import OperatorIdentity
from civiccast.auth.roles import require_any_role
from civiccast.egress.caption_proof import build_caption_status_provider
from civiccast.egress.compliance import (
    ComplianceProbeResult,
    DeviceProbeResult,
    TsduckStatus,
    locate_tsduck,
    probe_device,
    read_last_probe,
    run_compliance_probe,
)
from civiccast.egress.dispatcher import RUNNING_STATES
from civiccast.egress.engine_select import gstreamer_engine_selected
from civiccast.egress.gst.bridge import SUPPORTED_SINK_KINDS as GST_SUPPORTED_SINK_KINDS
from civiccast.egress.headend import (
    HeadendProfile,
    apply_headend_profile,
    get_headend_profile,
    list_headend_profiles,
    resolve_local_hls_directory,
)
from civiccast.egress.loudness_plan import ChannelLoudnessPlan, build_loudness_plan
from civiccast.egress.models import (
    SLATE_RESTART_COMMAND_PREFIX,
    CaptionStatus,
    EgressCaptionProofSample,
    EgressCommand,
    EgressCommandAction,
    EgressConfig,
    EgressHealthSample,
    EgressProofEvent,
    EgressSchemaCurrency,
    EgressSinkSpec,
    EgressState,
    EgressStateRow,
    ManualRouteState,
    TakeoverSession,
    reject_control_chars,
)
from civiccast.egress.operator_state import operator_state_projection
from civiccast.egress.store import EgressStore
from civiccast.egress.takeover_service import (
    AlreadyLiveError,
    NotInTakeoverError,
    TakeoverNotReadyError,
    TakeoverService,
)


def get_egress_store() -> EgressStore | None:
    """FastAPI dependency for the active egress store.

    The app factory overrides this when durable storage is ready. The default
    import-time value is ``None`` so router import does not open a database.
    """


def get_takeover_service() -> TakeoverService | None:
    """FastAPI dependency for the live-takeover service (S5).

    The app factory overrides this when durable storage is ready; the default
    import-time value is ``None`` so router import does not open a database. The
    handlers translate None into HTTP 503.
    """


def get_egress_work_dir() -> Path:
    """DI seam: where per-channel egress artifacts (incl. compliance results) live."""

    from civiccast.egress.automation import default_egress_work_dir

    return default_egress_work_dir()


def get_compliance_prober() -> Any:
    """DI seam: callable(config, seconds) -> ComplianceProbeResult.

    The default runs the real BYO-TSDuck probe; tests override this so the
    API contract needs neither TSDuck nor a live stream.
    """

    def _prober(config: EgressConfig, seconds: int) -> ComplianceProbeResult:
        return run_compliance_probe(config, seconds=seconds, work_dir=get_egress_work_dir())

    return _prober


def get_device_prober() -> Any:
    """DI seam: callable(host, ports) -> DeviceProbeResult."""

    def _prober(host: str, ports: list[int]) -> DeviceProbeResult:
        return probe_device(host, ports=ports)

    return _prober


public_router = APIRouter(prefix="/api/public/egress", tags=["public", "egress"])
staff_router = APIRouter(prefix="/api/staff/egress", tags=["staff", "egress"])
_DB_NOT_READY_DESCRIPTION = "Durable storage not ready -- run Setup storage or set DATABASE_URL"
_DB_NOT_READY_DETAIL = (
    "Durable storage is not ready. Open Setup and choose Prepare storage, "
    "or set DATABASE_URL for a technical deployment."
)


class EgressCommandRequest(BaseModel):
    """Operator command request; server fills id, time, and actor."""

    model_config = ConfigDict(extra="forbid")

    action: EgressCommandAction


class EgressCommandResponse(BaseModel):
    """Acknowledgement for a queued egress command."""

    model_config = ConfigDict(extra="forbid")

    command: EgressCommand
    queued: bool


class TakeoverRequest(BaseModel):
    """Operator request to take a channel live (S5). Actor comes from auth."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=2000)
    path_id: str | None = Field(default=None, max_length=160)
    duration_seconds: float = Field(default=3600.0, gt=0)


class HandbackRequest(BaseModel):
    """Operator request to return a channel from takeover to its schedule."""

    model_config = ConfigDict(extra="forbid")

    notes: str | None = Field(default=None, max_length=2000)


class PublicEgressNowResponse(BaseModel):
    """Viewer-safe current egress status for one channel."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    state: EgressState
    current_source_label: str | None = None
    updated_at: datetime
    caption_status: CaptionStatus = "not-verified"
    seconds_on_air: int = 0


class StaffEgressChannelSummary(BaseModel):
    """Staff inventory row for one configured egress channel."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    enabled: bool
    sink_count: int
    state: EgressStateRow | None = None
    latest_health: EgressHealthSample | None = None


class StaffEgressChannelDetail(BaseModel):
    """Staff detail view for one egress channel."""

    model_config = ConfigDict(extra="forbid")

    config: EgressConfig
    state: EgressStateRow | None = None
    latest_health: EgressHealthSample | None = None


class EgressStorageStatusResponse(BaseModel):
    """Bounded egress log/command storage and work-volume headroom."""

    model_config = ConfigDict(extra="forbid")

    worker_log_bytes: int
    worker_log_file_count: int
    command_history_rows: int
    pending_commands: int
    command_table_bytes: int | None = None
    work_volume_free_bytes: int | None = None
    low_disk_warning: bool | None = None
    low_disk_threshold_bytes: int


_EGRESS_LOW_DISK_THRESHOLD_BYTES = int(ResourceThresholds().min_media_free_gb * 1024**3)


START_WITHOUT_CONFIG_DESCRIPTION = (
    "Start refused: the channel has no outgoing-feed configuration (or it is disabled)"
)


def start_without_config_reason(channel_id: str) -> str:
    """Operator-facing reason for refusing ``start`` on an unconfigured channel.

    Mirrors the inline reason the Channels screen shows under its disabled
    Start button so the API and the console never disagree.
    """
    return (
        f"No outgoing-feed configuration for {channel_id}. Apply a headend preset "
        "or the local rehearsal preset first."
    )


def start_disabled_config_reason(channel_id: str) -> str:
    """Operator-facing reason for refusing ``start`` on a disabled configuration.

    Mirrored by ``startDisabledConfigReason`` in the Channels screen.
    """
    return (
        f"Outgoing feed for {channel_id} is disabled in its egress configuration. "
        "Enable it in Outgoing feed configuration, then start."
    )


def _reject_unsupported_sink_kinds(config: EgressConfig) -> None:
    """DEFECT B: refuse an unsupported sink kind AT CONFIG TIME, with a clear
    message naming the supported kinds -- instead of accepting it with 200 OK
    and only discovering the gap deep inside a ``start`` pass (DEFECT A: an
    ``hls`` sink was accepted here and crashed the channel the moment an
    operator started it; the type system advertised it, the API accepted it,
    and the engine could not run it -- an accept-then-crash gap, the same
    shape several other defects this week share).

    Engine-aware: the ffmpeg-concat engine's ``civiccast.egress.sinks``
    already implements every declared ``EgressSinkKind`` (including
    ``rtmp``), so this check only narrows the accepted set when the
    GStreamer engine (the default) is actually selected -- it must never
    reject a kind the SELECTED engine can genuinely deliver.
    """
    if not gstreamer_engine_selected():
        return
    unsupported = sorted(
        {sink.kind for sink in config.sinks if sink.kind not in GST_SUPPORTED_SINK_KINDS}
    )
    if not unsupported:
        return
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=(
            f"Sink kind(s) {unsupported} are not supported by the active GStreamer egress "
            f"engine. Supported kinds: {sorted(GST_SUPPORTED_SINK_KINDS)}. Switch the "
            "channel's sink to a supported kind, or run the ffmpeg-concat engine instead "
            "(CIVICCAST_EGRESS_ENGINE=ffmpeg-concat) if this station needs rtmp output."
        ),
    )


def _require_store(store: EgressStore | None, *, surface: str) -> EgressStore:
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{_DB_NOT_READY_DETAIL} Surface: {surface}.",
        )
    return store


@staff_router.get(
    "/storage",
    response_model=EgressStorageStatusResponse,
    summary="Read egress worker log and command storage use",
    dependencies=[Depends(require_any_role("setup_admin", "support_admin", "meeting_operator"))],
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def get_egress_storage_status(
    egress_store: EgressStore | None = Depends(get_egress_store),
    work_dir: Path = Depends(get_egress_work_dir),
) -> EgressStorageStatusResponse:
    store = _require_store(egress_store, surface="egress storage status")
    usage = store.command_storage_usage()
    worker_log_bytes = 0
    worker_log_file_count = 0
    try:
        for path in work_dir.expanduser().glob("*/logs/gst-worker.*.log*"):
            try:
                if path.is_file():
                    worker_log_bytes += path.stat().st_size
                    worker_log_file_count += 1
            except OSError:
                continue
    except OSError:
        pass

    free_bytes: int | None = None
    probe_path = work_dir.expanduser()
    try:
        while not probe_path.exists() and probe_path.parent != probe_path:
            probe_path = probe_path.parent
        free_bytes = shutil.disk_usage(probe_path).free
    except OSError:
        pass

    return EgressStorageStatusResponse(
        worker_log_bytes=worker_log_bytes,
        worker_log_file_count=worker_log_file_count,
        command_history_rows=usage["row_count"],
        pending_commands=usage["pending_count"],
        command_table_bytes=usage["storage_bytes"],
        work_volume_free_bytes=free_bytes,
        low_disk_warning=(
            free_bytes < _EGRESS_LOW_DISK_THRESHOLD_BYTES if free_bytes is not None else None
        ),
        low_disk_threshold_bytes=_EGRESS_LOW_DISK_THRESHOLD_BYTES,
    )


@public_router.get(
    "/channels/{channel_id}/now",
    response_model=PublicEgressNowResponse,
    summary="Read viewer-safe current egress channel status",
    responses={
        404: {"description": "Egress channel is not on air"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def get_public_now(
    channel_id: str,
    egress_store: EgressStore | None = Depends(get_egress_store),
) -> PublicEgressNowResponse:
    store = _require_store(egress_store, surface="public egress now")
    state = store.read_state(channel_id)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Egress channel is not on air: {channel_id}",
        )
    latest_health = _latest_health(store, channel_id)
    return PublicEgressNowResponse(
        channel_id=channel_id,
        state=state.state,
        current_source_label=state.current_source_label,
        updated_at=state.updated_at,
        caption_status=latest_health.caption_status if latest_health else "not-verified",
        seconds_on_air=latest_health.seconds_on_air if latest_health else 0,
    )


@staff_router.get(
    "/channels",
    response_model=list[StaffEgressChannelSummary],
    summary="List configured egress channels",
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def list_channels(
    egress_store: EgressStore | None = Depends(get_egress_store),
) -> list[StaffEgressChannelSummary]:
    store = _require_store(egress_store, surface="egress channels")
    summaries: list[StaffEgressChannelSummary] = []
    for config in store.list_configs():
        summaries.append(
            StaffEgressChannelSummary(
                channel_id=config.channel_id,
                enabled=config.enabled,
                sink_count=len(config.sinks),
                state=operator_state_projection(store.read_state(config.channel_id)),
                latest_health=_latest_health(store, config.channel_id),
            )
        )
    return summaries


@staff_router.get(
    "/channels/{channel_id}",
    response_model=StaffEgressChannelDetail,
    summary="Read egress channel config, state, and latest health",
    responses={
        404: {"description": "Egress config not found"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def get_channel_detail(
    channel_id: str,
    egress_store: EgressStore | None = Depends(get_egress_store),
) -> StaffEgressChannelDetail:
    store = _require_store(egress_store, surface="egress channel detail")
    config = store.get_config(channel_id)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Egress config not found: {channel_id}",
        )
    return StaffEgressChannelDetail(
        config=config,
        state=operator_state_projection(store.read_state(channel_id)),
        latest_health=_latest_health(store, channel_id),
    )


@staff_router.put(
    "/channels/{channel_id}/config",
    response_model=EgressConfig,
    summary="Create or replace an egress channel configuration",
    dependencies=[Depends(require_any_role("setup_admin"))],
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def upsert_config(
    channel_id: str,
    payload: EgressConfig,
    egress_store: EgressStore | None = Depends(get_egress_store),
) -> EgressConfig:
    """Persist one channel's egress config.

    The path id must match the body id so scripts cannot accidentally write a
    config under one channel while the body describes another.
    """
    store = _require_store(egress_store, surface="egress config")
    if payload.channel_id != channel_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Path channel_id {channel_id!r} does not match body "
                f"channel_id {payload.channel_id!r}."
            ),
        )
    _reject_unsupported_sink_kinds(payload)
    _reject_uncontained_hls_sinks(payload)
    store.upsert_config(payload)
    result = store.get_config(channel_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Egress config was not readable after save.",
        )
    return result


def _reject_uncontained_hls_sinks(config: EgressConfig) -> None:
    """Refuse an ``hls`` sink whose folder the media router would not serve.

    ``/media/live/{channel}/...`` is public and unauthenticated and serves
    whatever folder the channel's ``hls`` sink names. The preset route
    already resolves its destination through ``resolve_local_hls_directory``;
    this PUT takes a whole ``EgressConfig`` body and used to run only the
    model's shape check, which accepted ``C:\\Windows``, a UNC share, ``/etc``
    and ``file:///C:/Windows`` (review round 2 delta, BLOCKER 2 -- executed by
    the reviewer). Every operator-input write path into that sink now goes
    through the same resolver, and ``media_router._live_dir_for_channel``
    re-checks at serve time so no future writer can reopen it.
    """
    for sink in config.sinks:
        if sink.kind != "hls":
            continue
        try:
            resolve_local_hls_directory(sink.uri)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"hls sink {sink.label!r}: {exc}",
            ) from exc


@staff_router.get(
    "/channels/{channel_id}/config",
    response_model=EgressConfig,
    summary="Read an egress channel configuration",
    responses={
        404: {"description": "Egress config not found"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def get_config(
    channel_id: str,
    egress_store: EgressStore | None = Depends(get_egress_store),
) -> EgressConfig:
    store = _require_store(egress_store, surface="egress config")
    result = store.get_config(channel_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Egress config not found: {channel_id}",
        )
    return result


class GraphicsOverlayStateResponse(BaseModel):
    """Current operator-facing S15 graphics-overlay state for one channel."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    graphics_overlay_enabled: bool
    graphics_overlay_lower_third_text: str


class GraphicsOverlayUpdateRequest(BaseModel):
    """Set the lower-third banner text and on/off state for one channel.

    A small, focused body (not the full ``EgressConfig``) so the operator UI's
    on-air toggle never has to round-trip sinks/secrets/branding it doesn't touch
    -- mirrors ``HeadendProfileApplyRequest`` above, which patches a subset of the
    channel's config the same way.
    """

    model_config = ConfigDict(extra="forbid")

    graphics_overlay_enabled: bool
    graphics_overlay_lower_third_text: Annotated[str, Field(default="", max_length=240)] = ""

    @field_validator("graphics_overlay_lower_third_text")
    @classmethod
    def _lower_third_text_is_control_char_free(cls, value: str) -> str:
        # MINOR fix (2026-08-30 audit): ``EgressConfig.model_copy(update=...)`` in
        # ``update_graphics_overlay`` below does NOT re-run EgressConfig's own field
        # validators (pydantic v2 ``model_copy`` never validates) -- so the same
        # control-char rule that guards EgressConfig.graphics_overlay_lower_third_text
        # must also gate THIS request body, or an operator could write a poisoned
        # string straight past it through this endpoint's actual save path.
        return reject_control_chars(value, field_name="graphics_overlay_lower_third_text")


@staff_router.get(
    "/channels/{channel_id}/graphics-overlay",
    response_model=GraphicsOverlayStateResponse,
    summary="Read the S15 graphics-overlay (lower-third) state for a channel",
    dependencies=[Depends(require_any_role("setup_admin", "meeting_operator", "support_admin"))],
    responses={
        404: {"description": "Egress config not found"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def get_graphics_overlay(
    channel_id: str,
    egress_store: EgressStore | None = Depends(get_egress_store),
) -> GraphicsOverlayStateResponse:
    store = _require_store(egress_store, surface="graphics overlay")
    config = store.get_config(channel_id)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Egress config not found: {channel_id}",
        )
    return GraphicsOverlayStateResponse(
        channel_id=channel_id,
        graphics_overlay_enabled=config.graphics_overlay_enabled,
        graphics_overlay_lower_third_text=config.graphics_overlay_lower_third_text,
    )


@staff_router.put(
    "/channels/{channel_id}/graphics-overlay",
    response_model=GraphicsOverlayStateResponse,
    summary="Set the S15 graphics-overlay (lower-third) state for a channel",
    dependencies=[Depends(require_any_role("meeting_operator", "setup_admin"))],
    responses={
        404: {"description": "Egress config not found"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def update_graphics_overlay(
    channel_id: str,
    payload: GraphicsOverlayUpdateRequest,
    egress_store: EgressStore | None = Depends(get_egress_store),
) -> GraphicsOverlayStateResponse:
    """Persist the lower-third toggle + text. Takes effect on the channel's NEXT
    pipeline build (a fresh ``start`` or a content-reload) -- it does not hot-update
    an already-live pipeline's on-screen text; see
    ``civiccast.egress.gst.bridge.graphics_overlay_leg_from_config``.
    """
    store = _require_store(egress_store, surface="graphics overlay")
    config = store.get_config(channel_id)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Egress config not found: {channel_id}",
        )
    updated = config.model_copy(
        update={
            "graphics_overlay_enabled": payload.graphics_overlay_enabled,
            "graphics_overlay_lower_third_text": payload.graphics_overlay_lower_third_text,
        }
    )
    store.upsert_config(updated)
    result = store.get_config(channel_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Egress config was not readable after save.",
        )
    return GraphicsOverlayStateResponse(
        channel_id=channel_id,
        graphics_overlay_enabled=result.graphics_overlay_enabled,
        graphics_overlay_lower_third_text=result.graphics_overlay_lower_third_text,
    )


@staff_router.get(
    "/channels/{channel_id}/loudness-plan",
    response_model=ChannelLoudnessPlan,
    summary="Resolve the per-sink loudness plan for a channel (S11b)",
    dependencies=[Depends(require_any_role("setup_admin", "support_admin"))],
    responses={
        404: {"description": "Egress config not found"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def get_loudness_plan(
    channel_id: str,
    egress_store: EgressStore | None = Depends(get_egress_store),
) -> ChannelLoudnessPlan:
    """Per-destination loudness plan: each sink's resolved target, standard
    label, and whether it diverges from the channel conform baseline."""
    store = _require_store(egress_store, surface="loudness plan")
    config = store.get_config(channel_id)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Egress config not found: {channel_id}",
        )
    return build_loudness_plan(config)


class CaptionStatusResponse(BaseModel):
    """Live caption decode-back status for one channel (S11a)."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    caption_status: CaptionStatus
    latest: EgressCaptionProofSample | None = None


@staff_router.get(
    "/channels/{channel_id}/caption-status",
    response_model=CaptionStatusResponse,
    summary="Live CEA-608/708 caption decode-back status (S11a)",
    dependencies=[Depends(require_any_role("setup_admin", "meeting_operator", "support_admin"))],
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def get_caption_status(
    channel_id: str,
    egress_store: EgressStore | None = Depends(get_egress_store),
) -> CaptionStatusResponse:
    """Operator-facing caption status: ``on`` only when the latest decode-back proof
    is a fresh PASS (fail-closed), plus the latest proof sample for the drawer."""
    store = _require_store(egress_store, surface="caption status")
    caption_status = build_caption_status_provider(store)(channel_id)
    return CaptionStatusResponse(
        channel_id=channel_id,
        caption_status=caption_status,
        latest=store.latest_caption_proof_sample(channel_id),
    )


@staff_router.get(
    "/channels/{channel_id}/caption-proofs",
    response_model=list[EgressCaptionProofSample],
    summary="Recent CEA-608/708 caption decode-back proof samples (S11a)",
    dependencies=[Depends(require_any_role("setup_admin", "meeting_operator", "support_admin"))],
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def get_caption_proofs(
    channel_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    egress_store: EgressStore | None = Depends(get_egress_store),
) -> list[EgressCaptionProofSample]:
    store = _require_store(egress_store, surface="caption proofs")
    return store.recent_caption_proof_samples(channel_id, limit)


class HeadendProfileApplyRequest(BaseModel):
    """Apply one named headend delivery profile to a channel (CA-6)."""

    model_config = ConfigDict(extra="forbid")

    profile_id: str
    # Required for every cable transport (the profile validates it). Optional
    # for ``local-rehearsal-hls``: blank means "the station's egress work
    # folder", see ``civiccast.egress.headend.default_local_hls_directory``.
    destination_uri: str = ""
    muxrate_kbps: int | None = None
    keep_existing_sinks: bool = False


@staff_router.get(
    "/headend-profiles",
    response_model=list[HeadendProfile],
    summary="List named cable-headend delivery profiles (vendor-doc sourced)",
)
def headend_profiles() -> list[HeadendProfile]:
    # Static product surface: works before durable storage is prepared so
    # a station can read the requirements while still setting up.
    return list_headend_profiles()


HeadendProfileOnAirEffect = Literal["restart_queued", "restart_required", "next_start", "unchanged"]


class HeadendProfileApplyResponse(BaseModel):
    """What a preset apply changed, and when it reaches the air.

    ``on_air_effect`` is the honest answer to "is this on air now?":

    - ``restart_queued`` -- the channel was standing by on its fallback slate,
      so a ``stop`` + ``start`` pair was queued (one durable write; the daemon
      runs each half only while the channel is still in the state that half
      assumes, see ``models.slate_restart_guard_state``); the daemon rebuilds
      the pipeline with the new outputs within one poll cycle. No program is
      interrupted -- but the restart terminates the ONE worker that produces
      every sink, so every output, the cable headend feed included, drops for
      the length of a pipeline rebuild (a few seconds). Portal residents were
      watching the slate; cable subscribers were too.
    - ``restart_required`` -- the channel is airing (or starting / handing
      off) a program. A running pipeline never picks up an output change (the
      GStreamer engine's reload re-applies only the program source and the
      graphics overlay -- see ``civiccast/egress/gst/engine.py``), and cutting
      a program is the operator's call, not this route's. The preset lands at
      the next ``start``; the operator can Stop then Start now.
    - ``next_start`` -- the channel is not running (or is going off air); its
      next ``start`` reads the new config.
    - ``unchanged`` -- the stored config already matched AND nothing needs to
      reach the air: the channel is not running, or its running pipeline was
      built with every output this preset asks for (measured from the
      daemon's latest health sample, which is keyed by the sink labels of the
      config the pipeline was BUILT with -- in every appender that writes a
      sample, a content reload's settlement included, because the keying is
      applied inside ``_sink_connected`` and not at each call site). A
      content reload is issued at every program boundary and does not
      rebuild sinks, so an appender keyed off the config row as it stands
      now would report a sink saved after the build as connected for one
      ~2 s poll tick (review round 4 delta, MAJOR 1). A config that matches on disk but
      not on air -- the sequence ``restart_required`` -> program ends ->
      re-apply -- is NOT ``unchanged``; it takes the normal path above
      (review round 3 delta, MAJOR 2).

    ``on_air_detail`` says the same in one operator-readable sentence, and
    the Channels screen shows it verbatim after the apply.
    """

    model_config = ConfigDict(extra="forbid")

    config: EgressConfig
    on_air_effect: HeadendProfileOnAirEffect
    on_air_detail: str


@staff_router.post(
    "/channels/{channel_id}/config/headend-profile",
    response_model=HeadendProfileApplyResponse,
    summary="Apply a headend delivery profile to a channel's egress config",
    dependencies=[Depends(require_any_role("setup_admin"))],
    responses={
        404: {"description": "Unknown headend profile"},
        422: {"description": "Destination does not satisfy the profile's transport"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def apply_headend_profile_to_channel(
    channel_id: str,
    payload: HeadendProfileApplyRequest,
    request: Request,
    egress_store: EgressStore | None = Depends(get_egress_store),
) -> HeadendProfileApplyResponse:
    """Apply a headend preset to the channel's config and say when it airs.

    Persisting the config alone changes nothing on air: the daemon reads a
    channel's sinks, encode profile and loudness target only when it BUILDS a
    pipeline. A ``reload`` does not rebuild them -- on the default GStreamer
    engine it re-applies only the program source and the graphics overlay
    (``gst/engine.py`` / ``gst/worker.py``), while ``daemon.py`` would still
    start the HLS relay child for the new sink, leaving a relay fed nothing
    and a manifest URL that 404s (review round 2 delta, BLOCKER 1). So this
    route never queues a ``reload``. What it does instead depends on the
    channel's state and is reported back in ``on_air_effect``:
    ``FALLBACK_SLATE`` gets a real restart (``stop`` + ``start``) because no
    program is interrupted -- though every output, the cable feed included,
    drops for the rebuild, and the response says so; a program on air is left
    alone and the response says a restart is required; a dark channel just
    gets the config. An identical config is ``unchanged`` only when the
    running channel is measured to already deliver it. See
    ``HeadendProfileApplyResponse``.
    """
    store = _require_store(egress_store, surface="headend profile")
    profile = get_headend_profile(payload.profile_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown headend profile: {payload.profile_id}",
        )
    base = store.get_config(channel_id)
    fresh = base is None
    if base is None:
        # First-time setup path: the applied profile replaces the
        # placeholder sink, so a station can go from nothing to a
        # headend-ready config in one call.
        base = EgressConfig(
            channel_id=channel_id,
            enabled=True,
            slate_message="CivicCast is preparing the channel.",
            sinks=[
                EgressSinkSpec(
                    kind="file",
                    label="Cable headend",
                    uri=f"build/egress/{channel_id}-headend-placeholder.ts",
                )
            ],
        )
    try:
        updated = apply_headend_profile(
            base,
            profile,
            destination_uri=payload.destination_uri,
            muxrate_kbps_override=payload.muxrate_kbps,
            keep_existing_sinks=payload.keep_existing_sinks and not fresh,
        )
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    _reject_unsupported_sink_kinds(updated)
    _reject_uncontained_hls_sinks(updated)
    config_changed = fresh or updated != base
    if config_changed:
        store.upsert_config(updated)
    result = store.get_config(channel_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Egress config was not readable after save.",
        )
    effect, detail = _apply_preset_on_air(
        store,
        result,
        config_changed=config_changed,
        issued_by=_staff_operator_id(request),
    )
    return HeadendProfileApplyResponse(config=result, on_air_effect=effect, on_air_detail=detail)


# The egress state in which a pipeline restart interrupts nothing but the
# fallback slate, so the route may restart the channel to put a new output on
# air without asking. Any other running state is a program (or one starting /
# handing off) and the restart is the operator's decision.
_HEADEND_AUTO_RESTART_STATE = "FALLBACK_SLATE"
# Running states that are not yet (or no longer) a steady program, each with
# its own true sentence (review round 3 delta, MINOR 4: "The channel is on
# air" was shown for STARTING, and "not running" for STOPPING / DRAINING,
# which still have a worker emitting).
_HEADEND_STARTING_STATE = "STARTING"
_HEADEND_GOING_OFF_AIR_STATES = frozenset({"STOPPING", "DRAINING"})

_RESTART_QUEUED_DETAIL = (
    "The channel was standing by on its slate, so it is being restarted with the new "
    "output. Every output, including the cable feed, drops for a few seconds while the "
    "pipeline rebuilds; the channel is back on air right after."
)
_RESTART_REQUIRED_DETAIL = (
    "The channel is on air. A running pipeline does not pick up output changes, so "
    "this preset takes effect when the channel is next started. To put it on air now, "
    "Stop and then Start the channel."
)
_RESTART_REQUIRED_WHILE_STARTING_DETAIL = (
    "The channel is starting up. The pipeline being built does not pick up output "
    "changes, so this preset takes effect when the channel is next started. Once it is "
    "up, Stop and then Start the channel to put it on air now."
)
_NEXT_START_DETAIL = (
    "The channel is not running. The preset takes effect when the channel is next started."
)
_NEXT_START_WHILE_GOING_OFF_AIR_DETAIL = (
    "The channel is going off air. The preset takes effect when the channel is next started."
)
_UNCHANGED_NOT_RUNNING_DETAIL = (
    "The channel's saved outputs already matched this preset and the channel is not "
    "running; nothing was queued. It reads this config when it is next started."
)
_UNCHANGED_ON_AIR_DETAIL = (
    "The channel's outputs already matched this preset and the running channel is "
    "already delivering them; nothing changed on air."
)


def _apply_preset_on_air(
    store: EgressStore, config: EgressConfig, *, config_changed: bool, issued_by: str
) -> tuple[HeadendProfileOnAirEffect, str]:
    """Put a just-saved preset on air where that is safe; say what happened.

    Returns the ``(on_air_effect, on_air_detail)`` pair for
    ``HeadendProfileApplyResponse``. Uses ``dispatcher.RUNNING_STATES`` (the
    commit-to-air nudge's own definition of "running") rather than a copy.

    ``config_changed`` is False when the stored config already matched the
    preset byte for byte. That alone is NOT ``unchanged``: the state is read
    FIRST, and an identical config short-circuits only when nothing needs to
    reach the air -- the channel is not running, or the running pipeline is
    measured to carry every output the preset asks for
    (:func:`_running_pipeline_delivers`). Otherwise the identical apply takes
    the same path a changed one would, which is exactly what an operator
    re-applying a preset after ``restart_required`` needs (review round 3
    delta, MAJOR 2: it used to answer "nothing changed on air" and queue
    nothing while the web preview stayed off air).
    """
    state = store.read_state(config.channel_id)
    if state is None or state.state not in RUNNING_STATES:
        if not config_changed:
            return ("unchanged", _UNCHANGED_NOT_RUNNING_DETAIL)
        if state is not None and state.state in _HEADEND_GOING_OFF_AIR_STATES:
            return ("next_start", _NEXT_START_WHILE_GOING_OFF_AIR_DETAIL)
        return ("next_start", _NEXT_START_DETAIL)
    if not config_changed and _running_pipeline_delivers(store, config):
        return ("unchanged", _UNCHANGED_ON_AIR_DETAIL)
    if state.state == _HEADEND_STARTING_STATE:
        return ("restart_required", _RESTART_REQUIRED_WHILE_STARTING_DETAIL)
    if state.state != _HEADEND_AUTO_RESTART_STATE:
        return ("restart_required", _RESTART_REQUIRED_DETAIL)
    _enqueue_restart(store, config.channel_id, issued_by=issued_by)
    return ("restart_queued", _RESTART_QUEUED_DETAIL)


def _running_pipeline_delivers(store: EgressStore, config: EgressConfig) -> bool:
    """True when the daemon's running pipeline was built with every sink in ``config``.

    The daemon stamps nothing on the state row about which config built the
    pipeline, but every health sample it appends carries ``sink_connected``
    keyed by the sink LABELS of the config the pipeline was built with
    (``EgressDaemon._built_configs`` -> ``_sink_connected`` /
    ``health.build_default_sink_health``; the config row as it stands NOW is
    only the fallback for a process adopted without a recorded build). That
    keying lives inside ``_sink_connected``, so it holds for EVERY appender
    -- the start, the poll tick, the fallback-slate transition and a content
    reload's settlement, which carries the config row it read when it armed
    (review round 4 delta, MAJOR 1). A sample is appended at start and on
    every poll tick. So "the latest sample
    knows every label this preset asks for" is a measurement of what is on
    air, not a guess from the config row. No sample at all (a state row
    written by something other than a daemon) reads as "not delivering", so
    the route errs toward putting the preset on air. Limit: two configs whose
    sinks differ only in URI share labels and are indistinguishable here --
    but an identical stored config is the precondition for this check, so
    the only way to hit that is a config edited to the same labels between
    the build and the apply.
    """
    samples = store.recent_health(config.channel_id, 1)
    if not samples:
        return False
    delivered = samples[0].sink_connected
    return all(sink.label in delivered for sink in config.sinks)


def _enqueue_restart(store: EgressStore, channel_id: str, *, issued_by: str) -> None:
    """Queue ``stop`` then ``start`` so the daemon rebuilds the pipeline.

    There is no ``restart`` command action; the daemon drains a channel's
    pending commands in ``(issued_at, command_id)`` order and runs each in
    turn, so a ``stop`` (terminates the worker, ends the HLS relay session)
    followed by a ``start`` (reads the saved config, starts the relay for the
    new sink, builds the pipeline with it) is one restart. Both the timestamp
    and the id suffix order the pair so neither store can drain them swapped.

    The pair is ONE durable write (``enqueue_commands``): a poll can never
    drain the ``stop`` alone, and a process death here leaves nothing queued
    rather than a ``stop`` with no ``start`` (review round 3 delta, MINOR 1).
    The ids carry :data:`SLATE_RESTART_COMMAND_PREFIX`, so the daemon runs
    the ``stop`` only while the channel is still on its slate and the
    ``start`` only after that ``stop`` ran (MINOR 2).
    """
    now = datetime.now(UTC)
    token = uuid.uuid4().hex
    store.enqueue_commands(
        [
            EgressCommand(
                channel_id=channel_id,
                action=action,  # type: ignore[arg-type]
                issued_at=now + timedelta(microseconds=offset),
                issued_by=issued_by,
                command_id=f"{SLATE_RESTART_COMMAND_PREFIX}{token}-{offset}-{action}",
            )
            for offset, action in enumerate(("stop", "start"))
        ]
    )


class ComplianceProbeRequest(BaseModel):
    """Bounded TSDuck verification run (CA-7)."""

    model_config = ConfigDict(extra="forbid")

    seconds: int = 10


class HeadendChannelReadiness(BaseModel):
    """Readiness summary for one udp-ts channel."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    destination: str
    last_probe: ComplianceProbeResult | None = None


class HeadendReadinessResponse(BaseModel):
    """TSDuck availability + per-channel last probe results."""

    model_config = ConfigDict(extra="forbid")

    tsduck: TsduckStatus
    channels: list[HeadendChannelReadiness]


class DeviceProbeRequest(BaseModel):
    """TCP reachability probe of a headend appliance's management surface."""

    model_config = ConfigDict(extra="forbid")

    host: str
    ports: list[int] = Field(default_factory=lambda: [80, 443])


class NdiReadinessResponse(BaseModel):
    """BYO-NDI posture: the station's ffmpeg wire + live relay statuses."""

    model_config = ConfigDict(extra="forbid")

    byo_ffmpeg_configured: bool
    byo_ffmpeg_path: str | None = None
    next_step: str = ""
    relays: list[Any] = Field(default_factory=list)


@staff_router.get(
    "/ndi-readiness",
    response_model=NdiReadinessResponse,
    summary="BYO-NDI readiness and supervised relay statuses",
)
def ndi_readiness() -> NdiReadinessResponse:
    import os

    from civiccast.egress.ndi_relay import all_relay_statuses

    byo_path = os.environ.get("CIVICCAST_NDI_FFMPEG") or None
    next_step = (
        ""
        if byo_path
        else (
            "Set CIVICCAST_NDI_FFMPEG to the station's NDI-capable FFmpeg "
            "build to enable NDI output. CivicCast's bundled ffmpeg cannot "
            "include the NDI muxer (NewTek license) — see the NDI runbook "
            "section."
        )
    )
    return NdiReadinessResponse(
        byo_ffmpeg_configured=byo_path is not None,
        byo_ffmpeg_path=byo_path,
        next_step=next_step,
        relays=[status.model_dump() for status in all_relay_statuses()],
    )


class SdiReadinessResponse(BaseModel):
    """BYO-SDI posture: the station's ffmpeg wire + live relay statuses."""

    model_config = ConfigDict(extra="forbid")

    byo_ffmpeg_configured: bool
    byo_ffmpeg_path: str | None = None
    next_step: str = ""
    relays: list[Any] = Field(default_factory=list)


@staff_router.get(
    "/sdi-readiness",
    response_model=SdiReadinessResponse,
    summary="BYO-SDI readiness and supervised relay statuses",
)
def sdi_readiness() -> SdiReadinessResponse:
    import os

    from civiccast.egress.sdi_relay import all_relay_statuses

    byo_path = os.environ.get("CIVICCAST_SDI_FFMPEG") or None
    next_step = (
        ""
        if byo_path
        else (
            "Set CIVICCAST_SDI_FFMPEG to the station's DeckLink-capable "
            "FFmpeg build to enable SDI output. CivicCast's bundled ffmpeg "
            "cannot include the decklink muxer (Blackmagic SDK license) — "
            "see the SDI runbook section, or use the OBS bridge."
        )
    )
    return SdiReadinessResponse(
        byo_ffmpeg_configured=byo_path is not None,
        byo_ffmpeg_path=byo_path,
        next_step=next_step,
        relays=[status.model_dump() for status in all_relay_statuses()],
    )


@staff_router.get(
    "/headend-readiness",
    response_model=HeadendReadinessResponse,
    summary="TSDuck availability and last stream-verification results",
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def headend_readiness(
    egress_store: EgressStore | None = Depends(get_egress_store),
    work_dir: Path = Depends(get_egress_work_dir),
) -> HeadendReadinessResponse:
    store = _require_store(egress_store, surface="headend readiness")
    channels: list[HeadendChannelReadiness] = []
    for config in store.list_configs():
        udp_sinks = [sink for sink in config.sinks if sink.kind == "udp-ts"]
        if not udp_sinks:
            continue
        channels.append(
            HeadendChannelReadiness(
                channel_id=config.channel_id,
                destination=udp_sinks[0].uri,
                last_probe=read_last_probe(config.channel_id, work_dir),
            )
        )
    return HeadendReadinessResponse(tsduck=locate_tsduck(), channels=channels)


@staff_router.post(
    "/channels/{channel_id}/compliance-probe",
    response_model=ComplianceProbeResult,
    summary="Run a bounded TSDuck verification of the channel's headend stream",
    dependencies=[Depends(require_any_role("setup_admin"))],
    responses={
        404: {"description": "Egress config not found"},
        422: {"description": "Channel has no udp-ts headend sink"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def run_channel_compliance_probe(
    channel_id: str,
    payload: ComplianceProbeRequest,
    egress_store: EgressStore | None = Depends(get_egress_store),
    prober: Any = Depends(get_compliance_prober),
) -> ComplianceProbeResult:
    store = _require_store(egress_store, surface="compliance probe")
    config = store.get_config(channel_id)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Egress config not found: {channel_id}",
        )
    try:
        return prober(config, payload.seconds)  # type: ignore[no-any-return]
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


@staff_router.post(
    "/headend-device-probe",
    response_model=DeviceProbeResult,
    summary="TCP reachability probe of a headend appliance",
    dependencies=[Depends(require_any_role("setup_admin"))],
)
def run_headend_device_probe(
    payload: DeviceProbeRequest,
    prober: Any = Depends(get_device_prober),
) -> DeviceProbeResult:
    return prober(payload.host, payload.ports)  # type: ignore[no-any-return]


class GstreamerRepairResponse(BaseModel):
    """Outcome of the 'repair GStreamer runtime & restore full egress' recovery
    action (degraded-mode tier 5)."""

    model_config = ConfigDict(extra="forbid")

    triggered: bool = Field(description="A signed re-stage was launched.")
    closure_healthy: bool = Field(description="The closure verifies clean right now.")
    remedy: str = Field(
        description=("already-healthy | restage-launched | installer-missing | launch-failed")
    )
    detail: str
    pid: int | None = None


@staff_router.post(
    "/repair-gstreamer",
    response_model=GstreamerRepairResponse,
    summary="Repair the GStreamer runtime and restore full egress (no reinstall)",
    dependencies=[Depends(require_any_role("setup_admin", "support_admin"))],
)
def repair_gstreamer_runtime() -> GstreamerRepairResponse:
    """Operator recovery for a station degraded onto the FFmpeg egress engine by
    a corrupt GStreamer closure.

    Re-verifies the closure in place. If it is healthy again (the common
    transient AV-quarantine cause), nothing destructive runs and GStreamer
    egress restores on the next control-plane environment re-derivation. If it
    still has missing bytes, launches the installer's signed, scoped
    ``native-app-payload`` re-stage DETACHED; on the service's next start the
    re-derived environment re-verifies the healthy closure and GStreamer egress
    AUTO-RESTORES. Never a reinstall.

    Wired to the operator console's egress health surface
    (``GstreamerRepairPanel`` in ``civiccast/apps/portal-operator/src/
    screens/SystemHealthScreen.tsx``), which POSTs here behind a confirm
    dialog and surfaces ``remedy`` / ``detail`` / ``closure_healthy``.
    """

    # Imported lazily so the egress router module graph stays light and the
    # native install-layout resolution is only touched when a repair is asked
    # for (this endpoint is Windows-native; the resolver reads sys.executable).
    from civiccast.native.gstreamer_repair import trigger_gstreamer_repair

    outcome = trigger_gstreamer_repair()
    return GstreamerRepairResponse(
        triggered=outcome.triggered,
        closure_healthy=outcome.healthy,
        remedy=outcome.remedy,
        detail=outcome.detail,
        pid=outcome.pid,
    )


@staff_router.post(
    "/channels/{channel_id}/commands",
    response_model=EgressCommandResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue an egress daemon command",
    dependencies=[Depends(require_any_role("meeting_operator"))],
    responses={
        409: {"description": START_WITHOUT_CONFIG_DESCRIPTION},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def queue_command(
    channel_id: str,
    payload: EgressCommandRequest,
    request: Request,
    egress_store: EgressStore | None = Depends(get_egress_store),
) -> EgressCommandResponse:
    store = _require_store(egress_store, surface="egress commands")
    if payload.action == "start":
        # beta.5 walkthrough F-29: a start on a channel with no egress config
        # used to be accepted (202) and then dropped by the daemon, which
        # only ever raised ConfigInvalidError into its own log -- the console
        # showed "Stopped" with no reason. Refuse it here with the reason.
        config = store.get_config(channel_id)
        if config is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=start_without_config_reason(channel_id),
            )
        if not config.enabled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=start_disabled_config_reason(channel_id),
            )
    command = EgressCommand(
        channel_id=channel_id,
        action=payload.action,
        issued_at=datetime.now(UTC),
        issued_by=_staff_operator_id(request),
        command_id=f"egress-{uuid.uuid4()}",
    )
    store.enqueue_command(command)
    return EgressCommandResponse(command=command, queued=True)


# --- S5 Force Matrix: live takeover / handback ---


@staff_router.post(
    "/channels/{channel_id}/takeover",
    response_model=TakeoverSession,
    status_code=status.HTTP_201_CREATED,
    summary="Take a channel live (override the schedule)",
    dependencies=[Depends(require_any_role("meeting_operator", "setup_admin"))],
    responses={
        409: {"description": "Channel is already under live takeover"},
        422: {"description": "No ready live source could be prepared"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def begin_takeover(
    channel_id: str,
    payload: TakeoverRequest,
    request: Request,
    service: TakeoverService | None = Depends(get_takeover_service),
) -> TakeoverSession:
    svc = _require_takeover_service(service)
    identity = _staff_operator(request)
    try:
        return svc.take(
            channel_id=channel_id,
            operator_id=identity.operator_id,
            operator_name=identity.operator_display_name,
            reason=payload.reason,
            path_id=payload.path_id,
            duration_seconds=payload.duration_seconds,
        )
    except AlreadyLiveError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except TakeoverNotReadyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


@staff_router.delete(
    "/channels/{channel_id}/takeover",
    response_model=TakeoverSession,
    summary="Return a channel from live takeover to its schedule",
    dependencies=[Depends(require_any_role("meeting_operator", "setup_admin"))],
    responses={
        404: {"description": "Channel is not currently under live takeover"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def end_takeover(
    channel_id: str,
    request: Request,
    service: TakeoverService | None = Depends(get_takeover_service),
    payload: HandbackRequest | None = None,
) -> TakeoverSession:
    svc = _require_takeover_service(service)
    identity = _staff_operator(request)
    try:
        return svc.handback(
            channel_id=channel_id,
            operator_id=identity.operator_id,
            notes=payload.notes if payload is not None else None,
        )
    except NotInTakeoverError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@staff_router.get(
    "/channels/{channel_id}/takeover-state",
    response_model=ManualRouteState,
    summary="Read the channel's manual-route (takeover) state",
    dependencies=[Depends(require_any_role("meeting_operator", "setup_admin"))],
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def get_takeover_state(
    channel_id: str,
    service: TakeoverService | None = Depends(get_takeover_service),
) -> ManualRouteState:
    return _require_takeover_service(service).state(channel_id)


@staff_router.get(
    "/channels/{channel_id}/takeover-audit",
    response_model=list[TakeoverSession],
    summary="Read the channel's live-takeover audit log (admin)",
    dependencies=[Depends(require_any_role("setup_admin"))],
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def get_takeover_audit(
    channel_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    service: TakeoverService | None = Depends(get_takeover_service),
) -> list[TakeoverSession]:
    return _require_takeover_service(service).audit(channel_id, limit=limit)


@staff_router.get(
    "/channels/{channel_id}/state",
    response_model=EgressStateRow | None,
    summary="Read the last-known egress daemon state",
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def get_state(
    channel_id: str,
    egress_store: EgressStore | None = Depends(get_egress_store),
) -> EgressStateRow | None:
    store = _require_store(egress_store, surface="egress state")
    return operator_state_projection(store.read_state(channel_id))


@staff_router.get(
    "/channels/{channel_id}/health",
    response_model=list[EgressHealthSample],
    summary="Read recent egress health samples",
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def get_recent_health(
    channel_id: str,
    limit: int = Query(default=20, ge=1, le=500),
    egress_store: EgressStore | None = Depends(get_egress_store),
) -> list[EgressHealthSample]:
    store = _require_store(egress_store, surface="egress health")
    return store.recent_health(channel_id, limit)


@staff_router.get(
    "/channels/{channel_id}/schema-currency",
    response_model=EgressSchemaCurrency,
    summary="Whether the channel's persisted egress data matches the running schema",
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def get_schema_currency(
    channel_id: str,
    egress_store: EgressStore | None = Depends(get_egress_store),
) -> EgressSchemaCurrency:
    store = _require_store(egress_store, surface="egress schema currency")
    return EgressSchemaCurrency.from_latest_sample(channel_id, _latest_health(store, channel_id))


@staff_router.get(
    "/channels/{channel_id}/proof",
    response_model=list[EgressProofEvent],
    summary="Read recent egress as-aired proof events",
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def get_recent_proof_events(
    channel_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    egress_store: EgressStore | None = Depends(get_egress_store),
) -> list[EgressProofEvent]:
    store = _require_store(egress_store, surface="egress proof")
    return store.recent_proof_events(channel_id, limit)


def _staff_operator(request: Request) -> OperatorIdentity:
    identity = getattr(request.state, "operator_identity", None)
    if not isinstance(identity, OperatorIdentity):
        raise TypeError("Staff auth middleware did not attach an OperatorIdentity.")
    return identity


def _staff_operator_id(request: Request) -> str:
    return _staff_operator(request).operator_id


def _require_takeover_service(service: TakeoverService | None) -> TakeoverService:
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{_DB_NOT_READY_DETAIL} Surface: live takeover.",
        )
    return service


def _latest_health(store: EgressStore, channel_id: str) -> EgressHealthSample | None:
    samples = store.recent_health(channel_id, 1)
    return samples[0] if samples else None
