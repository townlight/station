# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Pydantic contracts for channel egress."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import PureWindowsPath
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base
from civiccast.egress.schema_currency import (
    EGRESS_SCHEMA_VERSION,
    current_schema_version,
    is_schema_current,
)


def reject_control_chars(value: str, *, field_name: str) -> str:
    """Reject C0 + DEL control characters in a free-text operator field.

    Shared by ``EgressConfig.graphics_overlay_lower_third_text`` (below) AND
    ``civiccast.egress.router.GraphicsOverlayUpdateRequest`` (the request body the
    graphics-overlay PUT endpoint validates BEFORE ``EgressConfig.model_copy``
    applies it — ``model_copy`` does not re-run field validators, so the request
    model needs this same check or a poisoned string would sail through the
    endpoint's actual write path untouched by the config-level validator below).
    Unlike ``clean_relay_identifier``, blank/whitespace-only stays valid here (it
    means "no overlay" for the lower-third text) and padding is preserved, not
    stripped -- callers that want that behavior pass the raw value through."""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{field_name} cannot contain control characters")
    return value


def clean_relay_identifier(value: str | None, *, field_name: str) -> str | None:
    """Normalize a BYO relay identifier (NDI name / SDI device name).

    One shared rule set for the API boundary AND the relay runtime so the
    two layers cannot drift (audit Critical TEST-001/QA-001): padding is
    stripped; control characters (C0 + DEL) are rejected; whitespace-only
    collapses to a rejection naming the field.
    """

    if value is None:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(
            f"{field_name} cannot contain control characters; use the exact "
            "printable name the relay tool reports."
        )
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} cannot be blank or whitespace-only; omit it instead.")
    return stripped


EgressSinkKind = Literal["srt", "rtmp", "local-ts", "udp-ts", "file", "sdi", "hls"]
# S5 (Force Matrix) adds ``takeover`` / ``handback`` — the daemon consumes them
# to invoke the proven supervisor.request_live_takeover / request_live_handback.
EgressCommandAction = Literal["start", "stop", "reload", "drain", "takeover", "handback"]
EgressSourceKind = Literal["program", "slate", "live", "cg"]
CaptionStatus = Literal["not-verified", "on"]
# S11b per-sink loudness regimes (parity decision 1): each egress sink can
# normalize to its destination's standard. ``inherit`` falls back to the
# channel-level ``EgressConfig.loudness_target_lufs`` so an un-configured sink
# behaves exactly as it did before S11.
LoudnessRegime = Literal["streaming", "atsc-a85", "ebu-r128", "inherit"]
EgressState = Literal[
    "STOPPED",
    "STARTING",
    "ON_AIR",
    "TRANSITIONING",
    "FALLBACK_SLATE",
    "DRAINING",
    "STOPPING",
    "ERROR",
]

_ALLOWED_EXTRA_OUTPUT_FLAGS = {
    "-mpegts_flags",
    "-muxrate",
    "-pkt_size",
    "-flush_packets",
    "-max_delay",
}
_SHELL_META = {";", "&", "|", "`", "$", "<", ">"}
_SECRET_QUERY_KEYS = {
    "passphrase",
    "streamkey",
    "stream_key",
    "key",
    "token",
    "password",
    "secret",
}
_SECRET_STREAMID_WORDS = ("pass", "secret", "token", "key", "credential", "cred")


class CanonicalProfile(BaseModel):
    """Stable encode profile used before sources reach the persistent encoder."""

    model_config = ConfigDict(extra="forbid")

    width: Annotated[int, Field(gt=0)] = 1280
    height: Annotated[int, Field(gt=0)] = 720
    fps: Annotated[int, Field(gt=0)] = 30
    # Declarative H.264 request. The FFmpeg wrapper resolves a concrete encoder
    # against the exact binary at execution time; model construction never probes.
    video_codec: Annotated[str, Field(min_length=1, max_length=80)] = "h264"
    video_bitrate_kbps: Annotated[int, Field(gt=0)] = 6000
    gop_size: Annotated[int, Field(gt=0)] = 60
    audio_codec: Annotated[str, Field(min_length=1, max_length=80)] = "aac"
    audio_bitrate_kbps: Annotated[int, Field(gt=0)] = 192
    audio_sample_rate: Annotated[int, Field(gt=0)] = 48_000
    audio_channels: Annotated[int, Field(ge=1, le=8)] = 2
    container: Literal["mpegts"] = "mpegts"


class EgressSinkSpec(BaseModel):
    """One configured egress output target.

    `uri` never carries secrets. Passphrases and stream keys use `secret_ref`.
    """

    model_config = ConfigDict(extra="forbid")

    kind: EgressSinkKind
    label: Annotated[str, Field(min_length=1, max_length=80)]
    uri: Annotated[str, Field(min_length=1, max_length=500)]
    secret_ref: Annotated[str | None, Field(default=None, min_length=1, max_length=160)] = None
    latency_ms: Annotated[int, Field(ge=0, le=60_000)] = 2000
    extra_output_args: list[str] = Field(default_factory=list)
    # S11b per-sink loudness (parity decision 1 — The incumbent PEG workflow normalizes per
    # destination: cable -24 LKFS / streaming -16 LUFS from one show). The
    # regime selects a standard target; an explicit ``loudness_target_lufs``
    # overrides it. ``inherit`` (the default) uses the channel target, so an
    # un-migrated / unconfigured sink resolves to today's behaviour.
    loudness_regime: LoudnessRegime = "inherit"
    loudness_target_lufs: Annotated[float | None, Field(default=None)] = None
    loudness_tolerance_lufs: Annotated[float | None, Field(default=None, gt=0, le=10)] = None
    # S11 gap-B: strip the EAS attention tone (853/960 Hz) on OTT egress so we
    # never rebroadcast an alert signal we are not the certified originator of
    # (FCC Sec. 11.31). Applied only to OTT sinks (slice 3); cable passes it
    # through untouched to the certified headend.
    eas_tone_strip_enabled: bool = True

    @field_validator("uri")
    @classmethod
    def _uri_must_not_embed_secret(cls, value: str) -> str:
        if uri_looks_secret_bearing(value):
            raise ValueError("egress sink uri must not include credentials or secrets")
        return value

    @field_validator("extra_output_args")
    @classmethod
    def _extra_output_args_are_safe(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item:
                raise ValueError("extra_output_args cannot include empty arguments")
            if any(char in item for char in _SHELL_META):
                raise ValueError("extra_output_args cannot include shell metacharacters")
            if item == "-i" or item.startswith("-i:"):
                raise ValueError("extra_output_args cannot add FFmpeg inputs")
            if item.startswith("-") and item not in _ALLOWED_EXTRA_OUTPUT_FLAGS:
                raise ValueError(f"unsupported FFmpeg output flag: {item}")
        return value

    @model_validator(mode="after")
    def _kind_matches_uri(self) -> EgressSinkSpec:
        scheme = urlsplit(self.uri).scheme.lower()
        if self.kind == "srt" and scheme != "srt":
            raise ValueError("srt egress sinks require an srt:// uri")
        if self.kind == "rtmp" and scheme not in {"rtmp", "rtmps"}:
            raise ValueError("rtmp egress sinks require an rtmp:// or rtmps:// uri")
        if self.kind == "local-ts" and scheme not in {"udp", "file"}:
            raise ValueError("local-ts egress sinks require a udp:// or file:// uri")
        if self.kind == "udp-ts":
            # CA-6 headend SPTS sink: a real network destination, always.
            if scheme != "udp":
                raise ValueError("udp-ts egress sinks require a udp:// uri")
            if urlsplit(self.uri).port is None:
                raise ValueError("udp-ts egress sinks require an explicit destination port")
        if self.kind == "file" and scheme not in {"", "file"} and not _is_windows_path(self.uri):
            raise ValueError("file egress sinks require a filesystem path or file:// uri")
        if self.kind == "hls" and scheme not in {"", "file"} and not _is_windows_path(self.uri):
            raise ValueError(
                "hls egress sinks require a local directory path or file:// uri "
                "(the ffmpeg hls muxer writes a manifest + segments there for "
                "civiccast.stream.media_router to serve — not a network destination)"
            )
        return self


class EgressConfigDb(Base):
    """Durable egress config row for one channel."""

    __tablename__ = "egress_configs"

    channel_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auto_start: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    allow_software_fallback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    fill_policy: Mapped[str] = mapped_column(
        String(20), nullable=False, default="slate", server_default="slate"
    )
    ndi_relay_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sdi_relay_device: Mapped[str | None] = mapped_column(String(200), nullable=True)
    slate_message: Mapped[str] = mapped_column(Text, nullable=False)
    # S15 graphics-overlay operator control: off by default (empty string / False),
    # so an unconfigured channel's graph stays byte-identical to before this
    # existed. See EgressConfig.graphics_overlay_enabled / _lower_third_text.
    graphics_overlay_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    graphics_overlay_lower_third_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    loudness_target_lufs: Mapped[float] = mapped_column(Float, nullable=False, default=-16.0)
    loudness_tolerance_lufs: Mapped[float] = mapped_column(Float, nullable=False, default=2.0)
    canonical_profile_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EgressSinkDb(Base):
    """Durable egress sink row owned by an egress channel config."""

    __tablename__ = "egress_sinks"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('srt', 'rtmp', 'local-ts', 'udp-ts', 'file', 'sdi', 'hls')",
            name="egress_sinks_kind_check",
        ),
        CheckConstraint(
            "loudness_regime IN ('streaming', 'atsc-a85', 'ebu-r128', 'inherit')",
            name="egress_sinks_loudness_regime_check",
        ),
    )

    channel_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    label: Mapped[str] = mapped_column(String(80), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    secret_ref: Mapped[str | None] = mapped_column(String(160), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=2000)
    extra_output_args_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # S11b per-sink loudness (migration 0049). Nullable target/tolerance and a
    # server_default of ``inherit``/true mirror the model defaults so pre-0049
    # rows read back exactly as they would have before this column existed.
    loudness_regime: Mapped[str] = mapped_column(
        String(16), nullable=False, default="inherit", server_default="inherit"
    )
    loudness_target_lufs: Mapped[float | None] = mapped_column(Float, nullable=True)
    loudness_tolerance_lufs: Mapped[float | None] = mapped_column(Float, nullable=True)
    eas_tone_strip_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class EgressCommandDb(Base):
    """Durable command queue row consumed by the egress daemon."""

    __tablename__ = "egress_commands"
    __table_args__ = (
        Index(
            "ix_egress_commands_channel_consumed_issued",
            "channel_id",
            "consumed_at",
            "issued_at",
            "command_id",
        ),
        CheckConstraint(
            "action IN ('start', 'stop', 'reload', 'drain', 'takeover', 'handback')",
            name="egress_commands_action_check",
        ),
    )

    command_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    issued_by: Mapped[str] = mapped_column(String(120), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EgressStateDb(Base):
    """Last-known daemon state row for one egress channel."""

    __tablename__ = "egress_states"
    __table_args__ = (
        CheckConstraint(
            "state IN ('STOPPED', 'STARTING', 'ON_AIR', 'TRANSITIONING', "
            "'FALLBACK_SLATE', 'DRAINING', 'STOPPING', 'ERROR')",
            name="egress_states_state_check",
        ),
    )

    channel_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    current_source_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    current_proof_event_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class EgressHealthSampleDb(Base):
    """Append-only health sample row for egress proof and operator visibility."""

    __tablename__ = "egress_health_samples"
    __table_args__ = (
        CheckConstraint(
            "state IN ('STOPPED', 'STARTING', 'ON_AIR', 'TRANSITIONING', "
            "'FALLBACK_SLATE', 'DRAINING', 'STOPPING', 'ERROR')",
            name="egress_health_samples_state_check",
        ),
    )

    sample_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(String(80), nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    sink_connected_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    encoder_fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    encoder_bitrate_kbps: Mapped[float | None] = mapped_column(Float, nullable=True)
    dropped_frames: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seconds_on_air: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_loudness_lufs: Mapped[float | None] = mapped_column(Float, nullable=True)
    caption_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not-verified")
    # S9: schema-currency + proof-event churn rate (migration 0038).
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    proof_events_appended: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class EgressCaptionProofSampleDb(Base):
    """Append-only CEA-608/708 caption decode-back proof sample (S11a, migration 0050).

    One row per live decode-back check: the emitted stream is decoded and its captions
    compared to the expected cues. ``caption_status`` is ``on`` only on a PASS, which the
    daemon's caption_status_provider reads (within a freshness window) so the health
    sample's caption_status reflects PROVEN captions instead of a hardcoded posture.
    Rolling/capped per channel (S9 churn discipline).
    """

    __tablename__ = "egress_caption_proof_samples"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PASS', 'FAIL')",
            name="egress_caption_proof_samples_status_check",
        ),
        CheckConstraint(
            "caption_status IN ('not-verified', 'on')",
            name="egress_caption_proof_samples_caption_status_check",
        ),
        Index(
            "ix_egress_caption_proof_samples_channel_sampled",
            "channel_id",
            "sampled_at",
        ),
    )

    sample_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(String(80), nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False)
    caption_status: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    decoder_name: Mapped[str] = mapped_column(String(120), nullable=False)
    expected_cue_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    decoded_cue_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched_cue_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_timing_delta_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    proof_boundary: Mapped[str] = mapped_column(String(160), nullable=False)
    blocker: Mapped[str | None] = mapped_column(String(200), nullable=True)


class EgressProofEventDb(Base):
    """Append-only as-aired proof event at CivicCast's egress handoff boundary."""

    __tablename__ = "egress_proof_events"
    __table_args__ = (
        CheckConstraint(
            "state IN ('STOPPED', 'STARTING', 'ON_AIR', 'TRANSITIONING', "
            "'FALLBACK_SLATE', 'DRAINING', 'STOPPING', 'ERROR')",
            name="egress_proof_events_state_check",
        ),
        # S9 (audit perf): the churn cap + count_proof_events_since + recent_proof_events
        # all filter by channel_id and order/range on observed_at, every health tick.
        # Without this composite index those are full scans of an append-only table.
        Index("ix_egress_proof_events_channel_observed", "channel_id", "observed_at"),
    )

    event_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(80), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    source_label: Mapped[str] = mapped_column(String(200), nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    proof_boundary: Mapped[str] = mapped_column(String(160), nullable=False)
    machine_summary: Mapped[str] = mapped_column(Text, nullable=False)


class EgressConfig(BaseModel):
    """Operator configuration for one egress channel."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=80)]
    enabled: bool
    # CA-2 channel automation: "this channel runs 24/7" — the lifespan
    # automation driver re-issues a start after app/machine restarts.
    auto_start: bool = False
    # CPU fallback for an explicit hardware encoder request. Default False
    # refuses unavailable explicit hardware instead of silently switching to
    # CPU. Declarative h264 keeps automatic hardware-first/CPU selection.
    allow_software_fallback: bool = False
    # CA-3: what fills gaps between scheduled programs — the plain slate or
    # the rotating approved community bulletin board.
    fill_policy: Literal["slate", "bulletins"] = "slate"
    # Issue #116 (BYO-NDI): when set, the automation driver supervises an
    # NDI relay publishing this channel's output under this name through
    # the station's own NDI-capable FFmpeg build. NULL = no NDI output.
    ndi_relay_name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    # Issue #117 (BYO-SDI): when set, the automation driver supervises an
    # SDI relay feeding this channel's output to this DeckLink device
    # through the station's own DeckLink-capable FFmpeg build. NULL = no
    # SDI output.
    sdi_relay_device: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    sinks: Annotated[list[EgressSinkSpec], Field(min_length=1)]

    @field_validator("ndi_relay_name", "sdi_relay_device")
    @classmethod
    def _relay_identifier_is_runtime_safe(cls, value: str | None, info: Any) -> str | None:
        # Audit Critical (TEST-001/QA-001): the relay runtime rejects control
        # characters and blank names when it builds ffmpeg args - inside the
        # automation pass, at air time. Reject at save time instead, with the
        # same rules, so a stored config can never poison the pass. Padding
        # is stripped rather than rejected.
        return clean_relay_identifier(value, field_name=info.field_name)

    loudness_target_lufs: float = -16.0
    loudness_tolerance_lufs: Annotated[float, Field(gt=0, le=10)] = 2.0
    slate_message: Annotated[str, Field(min_length=1, max_length=240)]
    canonical_profile: CanonicalProfile = Field(default_factory=CanonicalProfile)
    # S15 graphics-overlay operator control (station bug + lower-third leg, PR #93):
    # the operator-facing on/off switch and lower-third banner text. Both default
    # off/blank so an unconfigured channel's playout graph is unaffected. Only the
    # NEXT pipeline build (start / content-reload) picks up a change here -- see
    # civiccast.egress.gst.bridge.graphics_overlay_leg_from_config for the wiring
    # and its documented "not hot" limitation.
    graphics_overlay_enabled: bool = False
    graphics_overlay_lower_third_text: Annotated[str, Field(default="", max_length=240)] = ""

    @field_validator("graphics_overlay_lower_third_text")
    @classmethod
    def _graphics_overlay_lower_third_text_is_control_char_free(cls, value: str) -> str:
        # MINOR fix (2026-08-30 audit): mirror clean_relay_identifier's control-char
        # rule (ndi_relay_name / sdi_relay_device above) for consistency -- a control
        # character here would ride into graphics_overlay_leg_from_config's rendered
        # banner PNG (civiccast.egress.gst.graphics_overlay.render_lower_third_png),
        # same class of poisoned-string-at-render-time risk those fields guard
        # against.
        return reject_control_chars(value, field_name="graphics_overlay_lower_third_text")

    @model_validator(mode="after")
    def _sink_labels_are_unique(self) -> EgressConfig:
        labels = [sink.label for sink in self.sinks]
        if len(labels) != len(set(labels)):
            raise ValueError("egress sink labels must be unique per channel")
        return self


class EgressCommand(BaseModel):
    """Durable control command from the web app to the daemon."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=80)]
    action: EgressCommandAction
    issued_at: datetime
    issued_by: Annotated[str, Field(min_length=1, max_length=120)]
    command_id: Annotated[str, Field(min_length=1, max_length=120)]


# Command-id prefix of the ``stop`` + ``start`` pair the headend-preset route
# queues to restart a channel that is standing by on its fallback slate
# (``civiccast.egress.router._enqueue_restart``). The pair is GUARDED: the
# route decided to restart because it read ``FALLBACK_SLATE``, but that read
# and the enqueue are not one transaction, and the daemon drains the pair one
# poll later. If the channel committed a program to air in between, draining
# the ``stop`` would cut that program -- the one thing the route promises it
# never does (review round 3 delta, MINOR 2). So the daemon runs each half
# only while the persisted state is still the one the half assumes:
# ``FALLBACK_SLATE`` for the ``stop``, ``STOPPED`` (the state the ``stop``
# half itself writes) for the ``start``. A mismatch skips the half and logs
# it; a skipped ``stop`` therefore also skips its ``start``. The guard rides
# on the command id rather than a new column because the egress tables are
# created by ``Base.metadata.create_all`` with no migration path for a
# column added to an installed station.
SLATE_RESTART_COMMAND_PREFIX = "headend-profile-restart-"


def slate_restart_guard_state(command: EgressCommand) -> EgressState | None:
    """The persisted state a slate-restart half requires, or ``None`` if unguarded.

    ``None`` means "an ordinary command: run it unconditionally". Only the
    two actions the route queues are guarded; any other action carrying the
    prefix is treated as unguarded rather than refused.
    """
    if not command.command_id.startswith(SLATE_RESTART_COMMAND_PREFIX):
        return None
    if command.action == "stop":
        return "FALLBACK_SLATE"
    if command.action == "start":
        return "STOPPED"
    return None


# Command-id prefix of the recovery ``start`` the daemon's own startup/per-pass
# sweeps queue after clearing a stale on-air claim
# (``EgressDaemon._reconcile_stale_claims``). The sweep reads the persisted row
# to DECIDE to recover and then writes ``STOPPED`` over it in the same
# transaction, so the state it cleared exists only in that transaction -- and
# the start it leaves behind is drained a poll later, by which time the row
# reads STOPPED. U53 needs that state at dispatch: the channel WAS on air, so
# the start owes the same "air the slate before conforming the program" rule
# every other start of an active channel owes (``_SLATE_FIRST_ACTIVE_STATES``),
# and a recovery start that resolves the program first is dark for the whole
# conform -- measured live at 6 m 55 s (2026-09-26 18:54:20 -> 19:01:23,
# government, after the C1 install restarted the service).
#
# The state rides on the command id rather than a new column for the same
# reason as ``SLATE_RESTART_COMMAND_PREFIX`` above: the egress tables are
# created by ``Base.metadata.create_all`` with no migration path for a column
# added to an installed station. The id stays well inside the 120-char cap --
# the prefix plus a state name plus ``uuid4().hex`` is at most 32 chars.
RESTART_RECOVERY_COMMAND_PREFIX = "restart-recovery-"


def restart_recovery_previous_state(command: EgressCommand) -> str | None:
    """The state a recovery start's stale claim was cleared FROM, or ``None``.

    ``None`` means "not a recovery start: dispatch it with no previous state".
    The value is the persisted state the sweep read before it wrote STOPPED
    (``ON_AIR`` / ``STARTING`` / ``TRANSITIONING`` -- see
    ``EgressDaemon._STALE_RECONCILE_STATES``), returned as the raw token so the
    daemon's own gate stays the single place that decides what a state implies.

    An unparseable tail (no ``STATE-`` segment, or a token that is not a known
    state) is treated as ``None`` rather than refused: the command is still a
    recovery start and must run, only without the hint.
    """
    if not command.command_id.startswith(RESTART_RECOVERY_COMMAND_PREFIX):
        return None
    tail = command.command_id[len(RESTART_RECOVERY_COMMAND_PREFIX) :]
    token, _, attempt = tail.rpartition("-")
    if not attempt or token not in _STALE_CLAIM_STATES:
        return None
    return token


#: The persisted states a recovery command may carry. Mirrors
#: ``EgressDaemon._STALE_RECONCILE_STATES`` -- the states the sweeps are allowed
#: to clear. A recovery command naming anything else is not one this daemon
#: wrote, so ``restart_recovery_previous_state`` declines to vouch for it.
_STALE_CLAIM_STATES: frozenset[str] = frozenset({"ON_AIR", "STARTING", "TRANSITIONING"})


class EgressStateRow(BaseModel):
    """Last-known daemon state for one channel."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=80)]
    state: EgressState
    current_source_label: Annotated[str | None, Field(default=None, max_length=200)] = None
    current_proof_event_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    updated_at: datetime
    pid: Annotated[int | None, Field(default=None, ge=0)] = None
    last_error: Annotated[str | None, Field(default=None, max_length=1000)] = None


class EgressHealthSample(BaseModel):
    """Periodic health sample for operator System Health and proof review."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=80)]
    sampled_at: datetime
    state: EgressState
    sink_connected: dict[str, bool] = Field(default_factory=dict)
    encoder_fps: Annotated[float | None, Field(default=None, ge=0)] = None
    encoder_bitrate_kbps: Annotated[float | None, Field(default=None, ge=0)] = None
    dropped_frames: Annotated[int, Field(ge=0)] = 0
    seconds_on_air: Annotated[int, Field(ge=0)] = 0
    last_loudness_lufs: float | None = None
    caption_status: CaptionStatus = "not-verified"
    # S9: schema-currency stamp + proof-event churn rate (operator visibility).
    schema_version: int = EGRESS_SCHEMA_VERSION
    proof_events_appended_since_last_sample: Annotated[int, Field(ge=0)] = 0


class EgressSchemaCurrency(BaseModel):
    """Whether a channel's persisted egress data matches the running code's schema
    (S9-6 operator visibility). A drift means the latest health sample was written by
    code at a different ``EGRESS_SCHEMA_VERSION`` — surfaced as a badge on System Health
    so a skew (which can silently corrupt data) is visible before it bites."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=80)]
    current_schema_version: int
    sample_schema_version: int | None = None
    is_current: bool
    proof_events_appended_since_last_sample: Annotated[int, Field(ge=0)] = 0
    latest_sampled_at: datetime | None = None

    @classmethod
    def from_latest_sample(
        cls, channel_id: str, sample: EgressHealthSample | None
    ) -> EgressSchemaCurrency:
        if sample is None:
            # No sample yet — nothing to drift against; report current.
            return cls(
                channel_id=channel_id,
                current_schema_version=current_schema_version(),
                sample_schema_version=None,
                is_current=True,
                proof_events_appended_since_last_sample=0,
                latest_sampled_at=None,
            )
        return cls(
            channel_id=channel_id,
            current_schema_version=current_schema_version(),
            sample_schema_version=sample.schema_version,
            is_current=is_schema_current(sample),
            proof_events_appended_since_last_sample=(
                sample.proof_events_appended_since_last_sample
            ),
            latest_sampled_at=sample.sampled_at,
        )


class EgressProofEvent(BaseModel):
    """Machine-readable record of what CivicCast put on air at handoff."""

    model_config = ConfigDict(extra="forbid")

    event_id: Annotated[str, Field(min_length=1, max_length=120)]
    observed_at: datetime
    channel_id: Annotated[str, Field(min_length=1, max_length=80)]
    state: EgressState
    source_label: Annotated[str, Field(min_length=1, max_length=200)]
    source_path: Annotated[str, Field(min_length=1, max_length=1000)]
    source_ref: Annotated[str | None, Field(default=None, max_length=200)] = None
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]
    machine_summary: Annotated[str, Field(min_length=1, max_length=500)]


class EgressCaptionProofSample(BaseModel):
    """One persisted CEA-608/708 caption decode-back proof sample (S11a).

    Persisted form of an ``EgressCaptionDecodeBackProof`` stamped with the time it
    was taken and the embedding ``mode`` that produced the stream. ``caption_status``
    is ``on`` only on a PASS; the daemon's caption_status_provider reads the latest
    sample within a freshness window to fill the health sample's caption_status.
    """

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=80)]
    sampled_at: datetime
    status: Literal["PASS", "FAIL"]
    caption_status: CaptionStatus
    mode: Literal["passthrough", "cea-708", "sidecar"]
    decoder_name: Annotated[str, Field(min_length=1, max_length=120)]
    expected_cue_count: Annotated[int, Field(ge=0)] = 0
    decoded_cue_count: Annotated[int, Field(ge=0)] = 0
    matched_cue_count: Annotated[int, Field(ge=0)] = 0
    max_timing_delta_seconds: Annotated[float, Field(ge=0)] = 0.0
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]
    blocker: Annotated[str | None, Field(default=None, max_length=200)] = None
    sample_id: Annotated[int | None, Field(default=None, ge=0)] = None


class ChannelAutomationRollup(BaseModel):
    """At-a-glance state of every auto_start channel (CA-4 System Health)."""

    model_config = ConfigDict(extra="forbid")

    automated: Annotated[int, Field(ge=0)]
    on_air: Annotated[int, Field(ge=0)]
    on_slate: Annotated[int, Field(ge=0)]
    dark: list[str] = Field(default_factory=list)


#: D45 fix (2026-09-05): hard cap on decoder sub-chains one egress pipeline
#: will ever be asked to build for a single ``EgressSourcePlan``. Each
#: segment becomes its own ``filesrc -> decodebin -> videoconvert ->
#: videoscale -> videorate`` sub-chain, built and set to PLAYING together
#: (``civiccast.egress.gst.bridge.graph_from_config`` /
#: ``civiccast.egress.gst.engine.GstPlayoutEngine._build_playlist``) --
#: avdec_h264's default max-threads=0 spins up ~20 threads per sub-chain.
#: Measured on real hardware: a 60-segment plan produced ~1200 threads and
#: ~3.5 GB RSS on one worker, with no TS output landing inside the engine's
#: 10s stall watchdog, so the worker relaunched roughly every 30s.
#:
#: Defined here (rather than in ``source_plan.py`` or ``gst/bridge.py``
#: separately) so the SCHEDULE-derived plan's producer and consumers import
#: the exact same value and can never disagree about it:
#: ``source_plan.build_source_plan_from_schedule`` clamps the segment count
#: it will ever return to this value (logging a WARNING if it has to), so
#: the plan every other consumer -- ``automation.py``'s rollover-horizon
#: tracking, ``daemon.py``'s dispatched-plan bookkeeping, ``continuity.py``,
#: ``preparer.py`` -- reads is already the same plan the pipeline will
#: actually play. ``gst/bridge.graph_from_config`` treats a "program"-kind
#: plan exceeding it as a "the clamp above was bypassed" bug signal and
#: FAILS CLOSED (``errors.PlaylistCapBypassedError``) rather than silently
#: playing a truncated slice of a plan the rest of the system still
#: believes is the full size.
#:
#: Filler producers also respect this limit: slate repeats cached media
#: within the cap, and large bulletin rotations concatenate groups before
#: planning. Automation refreshes their finite horizons before EOS. The
#: bridge retains a warning/truncation compatibility path for oversized
#: legacy slate/cg callers, not a shape the built-in producers emit.
MAX_PLAYLIST_SUBCHAINS = 12


class EgressSourceSegment(BaseModel):
    """One pre-conformed source segment available to the egress encoder."""

    model_config = ConfigDict(extra="forbid")

    label: Annotated[str, Field(min_length=1, max_length=200)]
    path: Annotated[str, Field(min_length=1, max_length=1000)]
    duration_seconds: Annotated[float, Field(gt=0)]
    #: Full playable media window after asset trims, before join-in-progress,
    #: schedule-slot, or preparation-horizon clipping. Unknown for plans that
    #: do not carry measured/recorded media provenance.
    playable_duration_seconds: Annotated[float | None, Field(default=None, gt=0)] = None
    kind: EgressSourceKind = "program"
    #: Board bed level already applied while rendering; per-sink loudness
    #: normalization needs this level restored after its gain pass.
    board_audio_gain_db: Annotated[float | None, Field(default=None, ge=-36, le=0)] = None
    source_ref: Annotated[str | None, Field(default=None, max_length=200)] = None
    #: Stable schedule-row identity for scheduled program occurrences. Multiple
    #: occurrences may air the same asset, so this cannot be inferred from
    #: ``source_ref`` alone.
    schedule_occurrence_id: Annotated[str | None, Field(default=None, max_length=80)] = None
    #: Opaque credential HANDLE for a live segment whose protocol supports one
    #: (WP-07: SRT only). Never the secret itself -- this model is serialized
    #: into ``TakeoverSession.source_plan_json`` (a durable audit row) and into
    #: the engine's on-disk graph file, so a secret here would be persisted
    #: plaintext in two places. The worker resolves the handle through the
    #: station credential store at element-construction time.
    secret_ref: Annotated[str | None, Field(default=None, max_length=200)] = None
    inpoint_seconds: Annotated[float | None, Field(default=None, ge=0)] = None
    outpoint_seconds: Annotated[float | None, Field(default=None, gt=0)] = None

    @model_validator(mode="after")
    def _trim_points_are_ordered(self) -> EgressSourceSegment:
        if (
            self.inpoint_seconds is not None
            and self.outpoint_seconds is not None
            and self.inpoint_seconds >= self.outpoint_seconds
        ):
            raise ValueError("source segment inpoint_seconds must be less than outpoint_seconds")
        return self


class EgressSourcePlan(BaseModel):
    """Ordered source plan for one egress encoder run."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=80)]
    segments: Annotated[list[EgressSourceSegment], Field(min_length=1)]


# ---------------------------------------------------------------------------
# S5 Force Matrix — live-takeover audit + runtime state
# ---------------------------------------------------------------------------


def _takeover_as_utc(value: datetime | None) -> datetime | None:
    """Re-attach UTC to a naive datetime (SQLite drops tzinfo on round-trip)."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class TakeoverSession(BaseModel):
    """Audit record for one live-takeover → handback cycle.

    ``returned_at`` is NULL while the channel is still under manual takeover and
    set at handback. ``source_plan_json`` is the immutable serialized
    EgressSourcePlan that was forced live, kept for the as-aired record.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1, max_length=120)
    channel_id: str = Field(..., min_length=1, max_length=80)
    source_ref: str = Field(..., min_length=1, max_length=160)
    source_label: str = Field(..., min_length=1, max_length=160)
    operator_id: str = Field(..., min_length=1, max_length=120)
    operator_name: str | None = Field(default=None, max_length=160)
    reason: str | None = None
    took_over_at: datetime
    returned_at: datetime | None = None
    source_plan_json: str
    notes: str | None = None


class ManualRouteState(BaseModel):
    """Current takeover state for one channel (runtime only — not persisted)."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(..., min_length=1, max_length=80)
    active_session: TakeoverSession | None = None
    can_takeover: bool
    can_return: bool


class TakeoverAuditRecordDb(Base):
    """Durable audit row for ``civiccast.takeover_audit`` (migration 0042).

    One row per takeover. The *active* session for a channel is the row with
    ``returned_at IS NULL``. Mirrors the egress ``...Db`` naming convention
    (peer of the Pydantic :class:`TakeoverSession`).
    """

    __tablename__ = "takeover_audit"
    __table_args__ = (
        # Audit queries filter by channel and order by took_over_at.
        Index("ix_takeover_audit_channel_took_over", "channel_id", "took_over_at"),
    )

    session_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(80), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(160), nullable=False)
    source_label: Mapped[str] = mapped_column(String(160), nullable=False)
    operator_id: Mapped[str] = mapped_column(String(120), nullable=False)
    operator_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # timezone=True (the spec snippet used a naive DateTime) to match the
    # codebase's UTC-aware timestamp convention everywhere else.
    took_over_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_plan_json: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    @classmethod
    def from_session(cls, session: TakeoverSession) -> TakeoverAuditRecordDb:
        return cls(
            session_id=session.session_id,
            channel_id=session.channel_id,
            source_ref=session.source_ref,
            source_label=session.source_label,
            operator_id=session.operator_id,
            operator_name=session.operator_name,
            reason=session.reason,
            took_over_at=session.took_over_at.astimezone(UTC),
            returned_at=(
                session.returned_at.astimezone(UTC) if session.returned_at is not None else None
            ),
            source_plan_json=session.source_plan_json,
            notes=session.notes,
        )

    def to_session(self) -> TakeoverSession:
        return TakeoverSession(
            session_id=self.session_id,
            channel_id=self.channel_id,
            source_ref=self.source_ref,
            source_label=self.source_label,
            operator_id=self.operator_id,
            operator_name=self.operator_name,
            reason=self.reason,
            took_over_at=_takeover_as_utc(self.took_over_at),  # type: ignore[arg-type]
            returned_at=_takeover_as_utc(self.returned_at),
            source_plan_json=self.source_plan_json,
            notes=self.notes,
        )


def uri_looks_secret_bearing(uri: str) -> bool:
    """Return true when an egress URI appears to contain a credential."""

    parsed = urlsplit(uri)
    if parsed.username or parsed.password:
        return True
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered_key = key.lower()
        lowered_value = value.lower()
        if lowered_key in _SECRET_QUERY_KEYS:
            return True
        if lowered_key in {"streamid", "srt_streamid"} and any(
            word in lowered_value for word in _SECRET_STREAMID_WORDS
        ):
            return True
    return False


def redact_source_uri(uri: str) -> str:
    """Redact credentials from a source URI for logging / durable proof storage.

    Strips userinfo (``rtsp://user:pass@host`` → ``rtsp://host``) and replaces known
    secret-bearing query values (``passphrase``/``streamkey``/``token``/…) with
    ``<redacted>``. Anything without a network authority — a local file path (incl.
    Windows ``C:\\...``), a bare path, ``file:///...`` — is returned unchanged. Used so a
    live-source URI (which can carry an SRT passphrase, an RTMP stream key, or RTSP
    credentials) never lands cleartext in the egress proof chain.
    """
    parsed = urlsplit(uri)
    if not parsed.netloc:
        return uri  # no network authority (local path / file URI) — nothing to redact
    netloc = parsed.hostname or ""
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    query = [
        (key, "<redacted>" if key.lower() in _SECRET_QUERY_KEYS else value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(query), parsed.fragment))


#: Schemes whose URIs can carry a credential in userinfo or a query value. ``file``
#: and bare paths are deliberately absent -- they have no authority to redact.
_REDACTABLE_URI_SCHEMES = ("srt", "rtmps", "rtmp", "rtsps", "rtsp", "https", "http", "udp")
#: A ``scheme://...`` token embedded ANYWHERE in a line of text. Terminated by
#: whitespace, quotes, or the punctuation that typically closes a URI in prose/log
#: output. Trailing sentence punctuation is trimmed inside the substitution.
_EMBEDDED_URI_RE = re.compile(
    r"\b(?:" + "|".join(_REDACTABLE_URI_SCHEMES) + r")://[^\s\"'<>|\\]+",
    re.IGNORECASE,
)
#: Punctuation a log line commonly puts immediately AFTER a URI, which is not part of it.
_URI_TRAILING_PUNCTUATION = ".,;:!?)]}'\""


def redact_uris_in_text(text: str) -> str:
    """Redact credentials from every ``scheme://...`` URI embedded in free text.

    ``redact_source_uri`` handles a URI that IS the string (``urlsplit`` finds the
    authority fine, even at position 0). It does nothing for a URI that merely appears
    inside a longer line -- ``ERROR failed to open srt://host?passphrase=x`` came back
    unchanged, cleartext. That is exactly the shape a child process writes to stderr,
    and child stderr now reaches the operator-facing ``last_error`` (egress daemon), so
    a mid-line scanner is required rather than optional.

    Each matched URI is rewritten through ``redact_source_uri``, so the two share one
    definition of what a credential is (userinfo, ``_SECRET_QUERY_KEYS`` query values).
    Non-URI text is untouched. ``redact_source_uri`` remains the right call for a
    whole-URI value; use this one for anything that merely CONTAINS URIs.
    """

    def _replace(match: re.Match[str]) -> str:
        candidate = match.group(0)
        trailing = ""
        while candidate and candidate[-1] in _URI_TRAILING_PUNCTUATION:
            trailing = candidate[-1] + trailing
            candidate = candidate[:-1]
        if not candidate:
            return match.group(0)
        cleaned = redact_source_uri(candidate)
        parsed = urlsplit(candidate)
        if parsed.username or parsed.password:
            # ``redact_source_uri`` DROPS userinfo silently (``rtsp://user:pass@h`` ->
            # ``rtsp://h``), which is right for the durable proof chain but reads as
            # "there was never a credential here" in an operator-facing error string.
            # In free text, leave a marker so the reader knows something was removed.
            cleaned = cleaned.replace("://", "://<redacted>@", 1)
        return cleaned + trailing

    return _EMBEDDED_URI_RE.sub(_replace, text)


def _is_windows_path(value: str) -> bool:
    path = PureWindowsPath(value)
    return bool(path.drive and path.root)
