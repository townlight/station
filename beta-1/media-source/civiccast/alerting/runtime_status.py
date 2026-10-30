# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S8-5 runtime safe-to-air computation (spec §3.6/§3.7/§6.5).

The install-time ``SafeToBroadcastContract`` answers "can I start a meeting?";
this answers "is the box on-air and healthy *right now*?" every few seconds.

Per ``auto_start`` channel we build a ``ChannelRuntimeStatus`` from its
``EgressStateRow`` + the post-QA-004 sink health carried on the latest
``EgressHealthSample`` (the corrected write path already stored state-aware
``sink_connected`` values). Overall color = worst channel color, escalated to
**red** if any ``critical`` alert is firing. This reuses the existing
``SafeToAirColor`` vocabulary so the runtime banner shares the install-time
color semantics.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

from civiccast.alerting.models import (
    CaptionAudioSignal,
    CaptionProcessingState,
    CaptionProviderState,
    ChannelCaptionProcessingStatus,
    ChannelRuntimeStatus,
    RuntimeSafeToAirStatus,
    SafeToAirColor,
)

if TYPE_CHECKING:
    from civiccast.alerting.models import AlertEvent
    from civiccast.egress.models import EgressConfig, EgressHealthSample, EgressStateRow
    from civiccast.egress.store import EgressStore

_LOG = logging.getLogger(__name__)

# Loudness target (ATSC A/85): -24 LUFS, ±2 LU healthy band.
_LOUDNESS_TARGET_LUFS = -24.0
_LOUDNESS_TOLERANCE_LU = 2.0

# States in which an auto_start channel is genuinely off-air (dark) — the
# operator promised 24/7 and the channel is not delivering it.
_DARK_STATES = {"STOPPED", "ERROR", "DRAINING", "STOPPING"}
# Transient states: on the way up / switching — not steady-green, not dark.
_TRANSIENT_STATES = {"STARTING", "TRANSITIONING"}

_COLOR_RANK: dict[SafeToAirColor, int] = {"green": 0, "yellow": 1, "red": 2}
_CAPTION_HEARTBEAT_STALE_SECONDS = 90
_CAPTION_PROGRESS_STALE_SECONDS = 120
_CAPTION_STARTUP_GRACE_SECONDS = 180
_CAPTION_FAILURE_CAPACITY_STATES = {"overloaded", "storage-refused", "paused", "disabled"}
_CAPTION_PROVIDER_STATES = {
    "whistle-primary",
    "whisper-primary",
    "whisper-fallback",
    "whistle-recovering",
    "whistle-recovery-cooldown",
    "fallback-cooldown",
    "fallback-retry-ready",
}


class _CaptionStatusFields(TypedDict):
    worker_heartbeat_at: datetime | None
    last_input_at: datetime | None
    last_processed_at: datetime | None
    audio_signal: CaptionAudioSignal
    provider_state: CaptionProviderState
    provider_retry_in_seconds: int | None
    backlog_segments: int


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def derive_live_caption_processing_status(
    payload: Mapping[str, Any] | None,
    *,
    egress_state: str,
    captions_expected: bool,
    now: datetime,
    on_air_since: datetime | None = None,
) -> ChannelCaptionProcessingStatus:
    """Interpret worker metadata without inferring speech from VTT recency."""

    if not captions_expected:
        return ChannelCaptionProcessingStatus(processing_state="disabled")

    snapshot = payload or {}
    heartbeat = _timestamp(snapshot.get("worker_heartbeat_at"))
    last_input = _timestamp(snapshot.get("last_input_at"))
    last_processed = _timestamp(snapshot.get("last_processed_at"))
    pending_since = _timestamp(snapshot.get("pending_since_at"))
    inference_started = _timestamp(snapshot.get("inference_started_at"))
    audio_signal_value = snapshot.get("audio_signal")
    audio_signal: CaptionAudioSignal = (
        cast(CaptionAudioSignal, audio_signal_value)
        if audio_signal_value in {"digital-silence", "audio-present", "unknown"}
        else "unknown"
    )
    provider_state_value = snapshot.get("provider_state")
    provider_state: CaptionProviderState = (
        cast(CaptionProviderState, provider_state_value)
        if provider_state_value in _CAPTION_PROVIDER_STATES
        else "unknown"
    )
    retry = snapshot.get("provider_retry_in_seconds")
    if not isinstance(retry, int) or isinstance(retry, bool) or retry < 0:
        retry = None
    backlog = snapshot.get("backlog_segments", 0)
    if not isinstance(backlog, int) or isinstance(backlog, bool) or backlog < 0:
        backlog = 0
    inflight = snapshot.get("inference_inflight") is True
    capacity_state = snapshot.get("state")
    current = _as_utc(now) or datetime.now(UTC)

    common: _CaptionStatusFields = {
        "worker_heartbeat_at": heartbeat,
        "last_input_at": last_input,
        "last_processed_at": last_processed,
        "audio_signal": audio_signal,
        "provider_state": provider_state,
        "provider_retry_in_seconds": retry,
        "backlog_segments": backlog,
    }
    if egress_state != "ON_AIR":
        return ChannelCaptionProcessingStatus(processing_state="inactive", **common)
    if capacity_state == "overloaded":
        return ChannelCaptionProcessingStatus(processing_state="overloaded", **common)
    if capacity_state in _CAPTION_FAILURE_CAPACITY_STATES:
        return ChannelCaptionProcessingStatus(processing_state="failed", **common)

    pending = backlog > 0 or inflight
    heartbeat_age = (current - heartbeat).total_seconds() if heartbeat else None
    on_air_age = (current - (_as_utc(on_air_since) or current)).total_seconds()
    if heartbeat_age is not None and heartbeat_age > _CAPTION_HEARTBEAT_STALE_SECONDS:
        return ChannelCaptionProcessingStatus(
            processing_state=(
                "stalled" if on_air_age > _CAPTION_STARTUP_GRACE_SECONDS else "unknown"
            ),
            **common,
        )
    if heartbeat is None and on_air_age > _CAPTION_STARTUP_GRACE_SECONDS:
        return ChannelCaptionProcessingStatus(processing_state="stalled", **common)
    if pending:
        # A live source can keep delivering chunks while one ASR call is hung.
        # Use completed progress or the first still-pending input. Starting
        # another batch after failure is not progress and must not reset the
        # deadline. The batch start is only a fallback for older snapshots
        # that do not carry either authoritative timestamp.
        progress_anchor = max(
            (stamp for stamp in (last_processed, pending_since) if stamp),
            default=inference_started or last_input,
        )
        if (
            progress_anchor is not None
            and (current - progress_anchor).total_seconds() > _CAPTION_PROGRESS_STALE_SECONDS
        ):
            return ChannelCaptionProcessingStatus(processing_state="stalled", **common)
        if heartbeat_age is None or heartbeat_age > _CAPTION_HEARTBEAT_STALE_SECONDS:
            return ChannelCaptionProcessingStatus(processing_state="stalled", **common)

    if heartbeat is None:
        processing_state: CaptionProcessingState = "unknown"
    elif (
        audio_signal == "digital-silence"
        and not pending
        and (
            last_processed is not None
            and (current - last_processed).total_seconds() <= _CAPTION_PROGRESS_STALE_SECONDS
            and (last_input is None or last_processed >= last_input)
        )
    ):
        processing_state = "silent"
    elif capacity_state == "delayed":
        processing_state = "delayed"
    elif pending:
        processing_state = "processing"
    elif (
        audio_signal == "audio-present"
        and last_input is not None
        and last_processed is not None
        and last_processed >= last_input
        and (current - last_processed).total_seconds() <= _CAPTION_PROGRESS_STALE_SECONDS
    ):
        processing_state = "caught-up"
    else:
        processing_state = "waiting"
    return ChannelCaptionProcessingStatus(processing_state=processing_state, **common)


def read_live_caption_processing_status(
    work_dir: Path,
    channel_id: str,
    *,
    egress_state: str,
    captions_expected: bool,
    now: datetime,
    on_air_since: datetime | None = None,
) -> ChannelCaptionProcessingStatus:
    """Read one atomic worker snapshot; corrupt/missing status remains unknown."""

    payload: Mapping[str, Any] | None = None
    if captions_expected:
        from civiccast.captions.live_sidecar import caption_runtime_status_path

        path = caption_runtime_status_path(work_dir, channel_id)
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, json.JSONDecodeError):
            pass
    return derive_live_caption_processing_status(
        payload,
        egress_state=egress_state,
        captions_expected=captions_expected,
        now=now,
        on_air_since=on_air_since,
    )


def live_caption_readiness(
    store: EgressStore | None,
    *,
    captions_expected: bool,
    work_dir: Path,
    now: datetime | None = None,
) -> str:
    """Return a detail-free station aggregate for the unauthenticated probe."""

    if not captions_expected:
        return "disabled"
    if store is None:
        return "unknown"
    now = now or datetime.now(tz=UTC)
    try:
        active = []
        for config in store.list_configs():
            if not config.enabled:
                continue
            state_row = store.read_state(config.channel_id)
            if state_row is not None and state_row.state == "ON_AIR":
                active.append((config, state_row))
        if not active:
            return "idle"
        statuses = [
            read_live_caption_processing_status(
                work_dir,
                config.channel_id,
                egress_state="ON_AIR",
                captions_expected=True,
                now=now,
                on_air_since=state_row.updated_at,
            )
            for config, state_row in active
        ]
    except Exception:
        _LOG.debug("Could not aggregate live-caption readiness", exc_info=True)
        return "unknown"
    if any(
        status.processing_state in {"failed", "stalled", "overloaded", "delayed"}
        for status in statuses
    ):
        return "degraded"
    if any(status.processing_state in {"unknown", "waiting"} for status in statuses):
        return "unknown"
    return "healthy"


def _worst(colors: list[SafeToAirColor]) -> SafeToAirColor:
    worst: SafeToAirColor = "green"
    for c in colors:
        if _COLOR_RANK[c] > _COLOR_RANK[worst]:
            worst = c
    return worst


def _seconds_in_state(state_row: EgressStateRow | None, now: datetime) -> int:
    if state_row is None:
        return 0
    updated = state_row.updated_at
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    return max(0, int((now - updated).total_seconds()))


def _loudness_out_of_tolerance(lufs: float | None) -> bool:
    if lufs is None:
        return False
    return abs(lufs - _LOUDNESS_TARGET_LUFS) > _LOUDNESS_TOLERANCE_LU


def compute_channel_runtime_status(
    config: EgressConfig,
    state_row: EgressStateRow | None,
    latest_sample: EgressHealthSample | None,
    *,
    now: datetime,
    expect_captions: bool = True,
    live_caption_status: ChannelCaptionProcessingStatus | None = None,
) -> ChannelRuntimeStatus:
    """Build the runtime status for one channel from its state + latest sample.

    ``expect_captions`` is the operator's live-captions switch
    (``StationProfile.live_captions_enabled`` via
    ``resolve_live_captions_enabled``). When it is on, an unverified caption
    proof fails the channel closed (red) -- captions are a legal readiness
    requirement, not a health decoration. When the operator has switched live
    captions OFF (the beta.5 default), no embed leg is built and the tap
    blanks every live sidecar, so the decode-back proof can never PASS; the
    caption gate is then not applied and the color comes from the sinks and
    loudness alone. ``captions_expected``/``captions_verified`` on the result
    let the dashboard say "off by operator" rather than "not confirmed".
    """
    state = state_row.state if state_row is not None else "STOPPED"
    sink_health = dict(latest_sample.sink_connected) if latest_sample is not None else {}
    all_sinks_ok = bool(sink_health) and all(sink_health.values())
    a_sink_down = any(not ok for ok in sink_health.values())
    captions_verified = (
        latest_sample is not None
        and getattr(latest_sample, "caption_status", "not-verified") == "on"
    )
    on_air = state == "ON_AIR"
    silence_is_expected = (
        on_air
        and live_caption_status is not None
        and live_caption_status.audio_signal == "digital-silence"
        and live_caption_status.processing_state == "silent"
    )
    captions_ready = captions_verified or not expect_captions or silence_is_expected
    on_healthy_slate = state == "FALLBACK_SLATE" and all_sinks_ok and captions_ready

    fps = latest_sample.encoder_fps if latest_sample is not None else None
    bitrate = latest_sample.encoder_bitrate_kbps if latest_sample is not None else None
    loudness = latest_sample.last_loudness_lufs if latest_sample is not None else None

    degraded = a_sink_down or _loudness_out_of_tolerance(loudness)

    color: SafeToAirColor
    if state in _DARK_STATES:
        color = "red"
    elif state in _TRANSIENT_STATES:
        # Coming up / switching — not steady, not off-air.
        color = "yellow"
    elif state == "ON_AIR":
        color = "yellow" if degraded else "green"
    elif state == "FALLBACK_SLATE":
        # Idling on slate is healthy *iff* the sinks are up; otherwise degraded.
        color = "green" if all_sinks_ok and not _loudness_out_of_tolerance(loudness) else "yellow"
    else:  # pragma: no cover - exhaustive guard for future states
        color = "yellow"

    # Captions are gated only for an ON_AIR channel while the operator expects
    # them. Stopped channels already have their own egress failure; they must
    # not gain a separate caption failure. Proven digital silence is also not
    # a missing-caption fault: no recent VTT text is used to infer speech.
    caption_worker_failed = (
        live_caption_status is not None
        and live_caption_status.processing_state in {"failed", "stalled"}
    )
    if (
        on_air
        and expect_captions
        and (caption_worker_failed or not captions_verified)
        and not silence_is_expected
    ):
        color = "red"

    return ChannelRuntimeStatus(
        channel_id=config.channel_id,
        egress_state=state,
        sink_health=sink_health,
        on_air=on_air,
        on_healthy_slate=on_healthy_slate,
        encoder_fps=fps,
        encoder_bitrate_kbps=bitrate,
        last_loudness_lufs=loudness,
        seconds_in_state=_seconds_in_state(state_row, now),
        last_proof_event_id=(state_row.current_proof_event_id if state_row is not None else None),
        captions_expected=expect_captions,
        captions_verified=captions_verified,
        live_captions=live_caption_status,
        color=color,
    )


def compute_runtime_safe_to_air(
    store: EgressStore,
    firing_alerts: list[AlertEvent],
    *,
    now: datetime | None = None,
    expect_captions: bool | None = None,
    caption_work_dir: Path | None = None,
) -> RuntimeSafeToAirStatus:
    """Compute runtime status for auto-start and currently active channels.

    ``firing_alerts`` is the current set of ``state="firing"`` alert events
    (the caller reads them once from the alert store). Overall color = worst
    channel color, escalated to red if any critical alert is firing.
    ``expect_captions`` defaults to the operator's live-captions switch, read
    ONCE here (not per channel) via ``resolve_live_captions_enabled_or_default``.
    """
    now = now or datetime.now(tz=UTC)
    if expect_captions is None:
        # Never fatal: a momentarily locked or unreadable station-state.json
        # must not take the on-air banner down; the read failure lands on the
        # shipped default, the same value the egress strategy and the caption
        # workers use, so the banner and the pipeline agree.
        from civiccast.installer.station_state import resolve_live_captions_enabled_or_default

        expect_captions = resolve_live_captions_enabled_or_default()
    if caption_work_dir is None:
        from civiccast.egress.automation import default_egress_work_dir

        caption_work_dir = default_egress_work_dir()

    channels: list[ChannelRuntimeStatus] = []
    for config in store.list_configs():
        if not config.enabled:
            continue
        state_row = store.read_state(config.channel_id)
        # Keep watching promised 24/7 channels and include channels already on
        # air after a manual start. Stopped manual channels remain out of scope.
        if not config.auto_start and (state_row is None or state_row.state != "ON_AIR"):
            continue
        recent = store.recent_health(config.channel_id, 1)
        latest_sample = recent[0] if recent else None
        egress_state = state_row.state if state_row is not None else "STOPPED"
        live_caption_status = read_live_caption_processing_status(
            caption_work_dir,
            config.channel_id,
            egress_state=egress_state,
            captions_expected=expect_captions,
            now=now,
            on_air_since=(state_row.updated_at if egress_state == "ON_AIR" and state_row else None),
        )
        channels.append(
            compute_channel_runtime_status(
                config,
                state_row,
                latest_sample,
                now=now,
                expect_captions=expect_captions,
                live_caption_status=live_caption_status,
            )
        )

    active_critical = sum(1 for a in firing_alerts if a.severity == "critical")
    active_warning = sum(1 for a in firing_alerts if a.severity == "warning")

    channel_worst = _worst([c.color for c in channels]) if channels else "green"
    color: SafeToAirColor = "red" if active_critical > 0 else channel_worst

    label, message = _summarize(color, channels, active_critical, active_warning)

    return RuntimeSafeToAirStatus(
        generated_at=now,
        color=color,
        label=label,
        operator_message=message,
        channels=channels,
        active_critical_alerts=active_critical,
        active_warning_alerts=active_warning,
    )


def _fmt_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def _summarize(
    color: SafeToAirColor,
    channels: list[ChannelRuntimeStatus],
    active_critical: int,
    active_warning: int,
) -> tuple[str, str]:
    if not channels:
        return ("Idle", "No channels are configured for 24/7 automation.")
    if color == "green":
        return ("On air", f"On air — all {len(channels)} channel(s) healthy.")
    if color == "red":
        caption_red = [
            channel
            for channel in channels
            if channel.color == "red"
            and channel.on_air
            and channel.captions_expected
            and (
                (
                    channel.live_captions is not None
                    and channel.live_captions.processing_state in {"failed", "stalled"}
                )
                or (
                    not channel.captions_verified
                    and not (
                        channel.live_captions is not None
                        and channel.live_captions.processing_state == "silent"
                        and channel.live_captions.audio_signal == "digital-silence"
                    )
                )
            )
        ]
        if caption_red:
            worst_caption = caption_red[0]
            issue = (
                worst_caption.live_captions.processing_state
                if worst_caption.live_captions is not None
                and worst_caption.live_captions.processing_state in {"failed", "stalled"}
                else "not verified"
            )
            return (
                "Captions degraded",
                f"Live captions are {issue} on {worst_caption.channel_id}; "
                "the channel remains on air.",
            )
        red = [c for c in channels if c.color == "red"]
        if red:
            worst = red[0]
            return (
                "OFF AIR",
                f"OFF AIR — {worst.channel_id} {worst.egress_state} "
                f"for {_fmt_duration(worst.seconds_in_state)}.",
            )
        # Red only because a critical alert is firing (channels themselves not red).
        return ("OFF AIR", f"{active_critical} critical alert(s) firing.")
    # yellow
    degraded = [c.channel_id for c in channels if c.color == "yellow"]
    suffix = f" ({active_warning} warning alert(s))" if active_warning else ""
    return ("Degraded", f"Degraded — {', '.join(degraded)} not fully healthy.{suffix}")
