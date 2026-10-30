# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S21 scheduled-recording pydantic + SQLAlchemy models.

Two durable entities (migration ``0056_scheduled_recording``, the long-
reserved sibling slot off ``0055_asrun_and_epg``):

* ``RecordingSchedule`` — a forward-scheduled capture: source descriptor,
  recurrence, time window, encoder profile, ingest loudness regime,
  optional target series + custom-field stamps. Disabled schedules
  produce no jobs (DC-1-like default-safe behavior at the schedule
  layer).
* ``RecordingJob`` — one row per planned / running / completed capture
  with a CHECK-pinned ``state`` enum. The produced ``asset_id`` is set
  on transition to ``done`` so the asset + readiness pipeline (S7) is
  unchanged from a watch-folder ingest.

Source kinds match the spec §3 vocabulary: ``sdi`` / ``hdmi`` / ``ndi``
for live inputs, ``rtsp`` / ``srt`` / ``hls`` / ``rtmp`` / ``mpegts``
for network streams.

Pydantic shapes pair with ``*Db`` SQLAlchemy twins via ``from civiccast.db
import Base``. ``station_id`` / ``target_series`` are loose string columns
(no SQLAlchemy ``relationship``), matching the eas / ai_models / metadata
/ reporting / underwriting / agenda / paywall convention.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base

Slug = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$")]

# Source kinds for `RecordingSource`. Live inputs are SDI/HDMI/NDI;
# network streams are RTSP/SRT/HLS/RTMP/MPEG-TS. The router accepts any
# of these; the engine seam (slice 2 / S15) decides how to open each.
SourceKind = Literal["sdi", "hdmi", "ndi", "rtsp", "srt", "hls", "rtmp", "mpegts"]
SOURCE_KIND_VALUES: tuple[str, ...] = (
    "sdi",
    "hdmi",
    "ndi",
    "rtsp",
    "srt",
    "hls",
    "rtmp",
    "mpegts",
)

# Lifecycle states for ``RecordingJob``. The CHECK constraint on the DB
# column pins these so a typo'd state from the service layer would surface
# at write time rather than as a corrupted row.
JobState = Literal[
    "scheduled",
    "arming",
    "recording",
    "finalizing",
    "done",
    "failed",
    "skipped",
]
JOB_STATE_VALUES: tuple[str, ...] = (
    "scheduled",
    "arming",
    "recording",
    "finalizing",
    "done",
    "failed",
    "skipped",
)
JOB_STATE_TERMINAL: frozenset[str] = frozenset({"done", "failed", "skipped"})
JOB_STATE_ACTIVE: frozenset[str] = frozenset({"arming", "recording", "finalizing"})

# Recurrence kinds. ``one_shot`` carries a single ``start``; ``weekly``
# carries weekday list + time. The shape mirrors S19's auto-schedule
# rule shape (intentionally simple — full RRULE is an explicit
# follow-up; this covers the common PEG cases: one-off + weekly).
RecurrenceKind = Literal["one_shot", "weekly"]
RECURRENCE_KIND_VALUES: tuple[str, ...] = ("one_shot", "weekly")

# Weekday integers per ``datetime.weekday()`` semantics: Monday=0, Sunday=6.
WEEKDAY_VALUES: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)


def _now() -> datetime:
    return datetime.now(UTC)


# Per-kind scheme allowlist for network-stream sources. The scheme on the
# operator-supplied URI MUST match the declared ``kind``; everything else
# is rejected at the model layer so the S15 engine seam can trust the
# URI it receives. Mirrors the paywall ``source_doc_url`` posture.
#
# Q-1 / E-2 fix: pre-fix the validator was a falsiness check
# (``not self.uri``) and accepted ``file:`` / ``javascript:`` /
# ``http://169.254.169.254/...`` / ``gopher:`` on every network kind,
# which would have surfaced as SSRF / LFI / RCE the moment the engine
# wired up. Now the trust boundary is the pydantic model, not the engine.
_ALLOWED_NETWORK_SCHEMES: dict[str, tuple[str, ...]] = {
    "rtsp": ("rtsp", "rtsps"),
    "srt": ("srt",),
    "hls": ("http", "https"),
    "rtmp": ("rtmp", "rtmps"),
    "mpegts": ("udp", "rtp"),
}

# Schemes that are NEVER admissible regardless of kind. These are the
# classic URI-class injection vectors (filesystem read, JS scheme,
# data-URI smuggling, protocol-smuggling on libcurl-backed elements).
_FORBIDDEN_URI_SCHEMES: frozenset[str] = frozenset(
    {"file", "javascript", "data", "gopher", "dict", "ftp", "ftps"}
)

# Slug-shaped pattern for live-input identifiers (sdi/hdmi/ndi).
# Q-2 fix: pre-fix this was a length-only bound and accepted shell
# metacharacters / newlines / null bytes — which would have surfaced as
# argument injection the moment S15 built a ``gst_parse_launch`` pipeline
# string from ``input_id``. The pattern allows alphanumerics + ``.`` /
# ``_`` / ``-`` which covers every real SDI/HDMI/NDI identifier shape
# (``sdi-1``, ``hdmi-a``, ``ndi.stage-cam.3``) and forecloses the entire
# command-injection class.
_INPUT_ID_PATTERN = r"^[a-zA-Z0-9._-]{1,120}$"

# Hard cap on the serialized JSON size of ``custom_field_values`` (Q-3 fix).
# The blob is COPIED onto every materialized job; without a cap a single
# 10 MB schedule materialized weekly becomes hundreds of MB of database
# in a month. 64 KiB is large enough for any operator-realistic custom-
# field stamp and small enough that even a many-jobs-per-day cadence
# stays bounded.
_CUSTOM_FIELD_VALUES_MAX_BYTES = 64 * 1024


def _validate_target_series(value: str | None) -> str | None:
    """Reject path-traversal / cross-station shapes on ``target_series``.

    Q-4 fix: pre-fix only the 120-char length was bounded. ``"../../other-
    station/series-foo"`` and ``"station-b:series-x"`` slipped through
    and were forwarded verbatim to ``asset_finalizer.finalize_to_asset``;
    once S7 wired up and used the value as a filesystem path component
    that would have been a cross-station write hole. The Slug shape
    (lowercase alnum + ``_`` / ``-``) covers every real series id and
    forecloses the path-traversal class.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    import re

    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,119}$", stripped):
        raise ValueError(
            f"target_series {stripped!r} must be slug-shaped "
            "(lowercase alphanumeric, '_' or '-', leading alnum, max 120)."
        )
    return stripped


def _validate_custom_field_values(value: dict[str, Any] | None) -> dict[str, Any]:
    """Cap the serialized JSON size of a custom_field_values blob.

    Used by ``RecordingSchedule`` / ``RecordingScheduleInput`` /
    ``RecordingScheduleUpdate`` / ``RecordingJob``. Q-3 fix — without
    this cap, an operator with write permission can amplify storage by
    materializing a multi-MB blob onto every job in a recurring schedule.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("custom_field_values must be a JSON object (dict).")
    try:
        encoded = json.dumps(value, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"custom_field_values must be JSON-serializable: {exc}") from exc
    if len(encoded.encode("utf-8")) > _CUSTOM_FIELD_VALUES_MAX_BYTES:
        raise ValueError(
            f"custom_field_values exceeds the {_CUSTOM_FIELD_VALUES_MAX_BYTES} "
            f"byte serialized cap; got {len(encoded)} bytes."
        )
    return value


# ---------------------------------------------------------------------------
# Pydantic domain models
# ---------------------------------------------------------------------------


class RecordingSource(BaseModel):
    """A capture source descriptor.

    For SDI / HDMI / NDI inputs, ``input_id`` references a station-side
    input registry (e.g. ``"sdi-1"``); ``uri`` is empty.

    For network streams, ``uri`` is the full URL (``rtsp://...``); the
    optional ``input_id`` lets the operator label a recurring stream.
    """

    model_config = ConfigDict(extra="forbid")

    kind: SourceKind
    input_id: Annotated[str, Field(default="", max_length=120)] = ""
    uri: Annotated[str, Field(default="", max_length=2000)] = ""

    @model_validator(mode="after")
    def _validate_kind_shape(self) -> RecordingSource:
        # Network-stream kinds require a uri; live inputs require an input_id.
        # We refuse the cross-product so a misconfigured schedule fails fast
        # instead of arming and producing no usable bytes.
        is_network = self.kind in ("rtsp", "srt", "hls", "rtmp", "mpegts")
        if is_network:
            # Q-1 / E-2 fix: validate scheme against the per-kind allowlist
            # so an attacker cannot post kind="rtsp" uri="file:///etc/passwd"
            # and have S15 open the local file as if it were an RTSP stream.
            stripped = self.uri.strip()
            if not stripped:
                raise ValueError(f"Network-stream source ({self.kind!r}) requires a non-empty uri.")
            # Detect schemeless URIs (urlparse silently accepts them and
            # returns scheme="" — which we want to reject as fail-closed).
            if "://" not in stripped:
                raise ValueError(
                    f"Network-stream source ({self.kind!r}) uri must include a "
                    f"scheme (e.g. {self.kind}://...)."
                )
            try:
                parsed = urlparse(stripped)
            except ValueError as exc:
                raise ValueError(f"Network-stream source uri is not a valid URL: {exc}") from exc
            scheme = (parsed.scheme or "").lower()
            if scheme in _FORBIDDEN_URI_SCHEMES:
                raise ValueError(
                    f"Source uri scheme {scheme!r} is not admissible "
                    f"(file/javascript/data/gopher/dict/ftp are blocked)."
                )
            allowed = _ALLOWED_NETWORK_SCHEMES.get(self.kind, ())
            if scheme not in allowed:
                raise ValueError(
                    f"Source uri scheme {scheme!r} does not match the "
                    f"declared kind {self.kind!r}; allowed: {list(allowed)}."
                )
            # Persist the stripped form so downstream comparisons (overlap,
            # JSON serialization) see a normalized value.
            if stripped != self.uri:
                self.uri = stripped
        if not is_network:
            stripped_id = self.input_id.strip()
            if not stripped_id:
                raise ValueError(
                    f"Live-input source ({self.kind!r}) requires a non-empty input_id."
                )
            # Q-2 fix: Slug-shaped pattern forecloses shell metacharacters,
            # newlines, null bytes, and other argument-injection vectors.
            import re

            if not re.match(_INPUT_ID_PATTERN, stripped_id):
                raise ValueError(
                    f"Live-input input_id {stripped_id!r} contains characters "
                    f"outside [a-zA-Z0-9._-] or exceeds 120 chars."
                )
            if stripped_id != self.input_id:
                self.input_id = stripped_id
        return self


class RecurrenceSpec(BaseModel):
    """Recurrence descriptor. ``one_shot`` = single fixed start. ``weekly``
    = weekday-list + local time-of-day."""

    model_config = ConfigDict(extra="forbid")

    kind: RecurrenceKind
    # one_shot uses ``start`` (UTC) verbatim.
    start: datetime | None = None
    # weekly uses ``weekdays`` (set of Mon=0..Sun=6) + ``time_hhmm``. Missing
    # `time_zone` means legacy UTC; new station UI writes the dynamic sentinel.
    weekdays: list[int] = Field(default_factory=list)
    time_hhmm: Annotated[str, Field(default="", max_length=5)] = ""
    # Older API writes stored weekly wall times as UTC. Keep that as the
    # default for old JSON/API clients; new station forms use the "station"
    # sentinel so profile timezone changes apply without rewriting rows.
    time_zone: Literal["UTC", "station"] = "UTC"

    @field_validator("weekdays")
    @classmethod
    def _validate_weekdays(cls, value: list[int]) -> list[int]:
        for d in value:
            if d not in WEEKDAY_VALUES:
                raise ValueError(f"weekdays entries must be 0..6 (Mon..Sun); got {d!r}.")
        # Deduplicate + sort so the materializer's modulo math stays
        # deterministic regardless of operator-input order.
        return sorted(set(value))

    @model_validator(mode="after")
    def _validate_kind_shape(self) -> RecurrenceSpec:
        if self.kind == "one_shot":
            if self.start is None:
                raise ValueError("one_shot recurrence requires a 'start' timestamp.")
            if self.weekdays or self.time_hhmm:
                raise ValueError("one_shot recurrence must NOT carry weekdays or time_hhmm.")
            if self.time_zone != "UTC":
                raise ValueError("one_shot recurrence does not use time_zone.")
        else:  # weekly
            if not self.weekdays:
                raise ValueError("weekly recurrence requires non-empty 'weekdays'.")
            if not self.time_hhmm:
                raise ValueError("weekly recurrence requires 'time_hhmm' (HH:MM local time).")
            # Parse HH:MM to bound-check.
            if not (
                len(self.time_hhmm) == 5
                and self.time_hhmm[2] == ":"
                and self.time_hhmm[:2].isdigit()
                and self.time_hhmm[3:].isdigit()
                and 0 <= int(self.time_hhmm[:2]) <= 23
                and 0 <= int(self.time_hhmm[3:]) <= 59
            ):
                raise ValueError(
                    f"time_hhmm must be 'HH:MM' (24-hour clock); got {self.time_hhmm!r}."
                )
        return self


SUPPORTED_ENCODER_PROFILES: frozenset[str] = frozenset(
    {
        "copy",
        "default",
        "inherit",
        "h264-1080p",
        "h264-720p",
        "hw-h264-1080p",
        "hw-h264-720p",
    }
)
SUPPORTED_LOUDNESS_REGIMES: frozenset[str] = frozenset(
    {"inherit", "copy", "atsc-a85", "ebu-r128", "streaming"}
)


def _validate_encoder_profile(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in SUPPORTED_ENCODER_PROFILES:
        allowed = ", ".join(sorted(SUPPORTED_ENCODER_PROFILES))
        raise ValueError(f"unsupported encoder_profile {value!r}; use one of: {allowed}")
    return normalized


def _validate_loudness_regime(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in SUPPORTED_LOUDNESS_REGIMES:
        allowed = ", ".join(sorted(SUPPORTED_LOUDNESS_REGIMES))
        raise ValueError(f"unsupported loudness_regime {value!r}; use one of: {allowed}")
    return normalized


class RecordingSchedule(BaseModel):
    """A forward-scheduled capture."""

    model_config = ConfigDict(extra="forbid")

    schedule_id: Slug
    station_id: Slug
    name: Annotated[str, Field(min_length=1, max_length=200)]
    source: RecordingSource
    recurrence: RecurrenceSpec
    # Window length in seconds. Combined with `recurrence` to compute each
    # job's start + end. Capped at 12h so a runaway typo doesn't book a
    # week-long capture.
    duration_seconds: int = Field(ge=60, le=12 * 60 * 60)
    encoder_profile: Slug
    loudness_regime: Annotated[str, Field(default="inherit", max_length=40)] = "inherit"
    target_series: Annotated[str | None, Field(default=None, max_length=120)] = None
    custom_field_values: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @field_validator("custom_field_values")
    @classmethod
    def _cap_custom_field_values(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_custom_field_values(value)

    @field_validator("target_series")
    @classmethod
    def _validate_target_series(cls, value: str | None) -> str | None:
        return _validate_target_series(value)

    @field_validator("encoder_profile")
    @classmethod
    def _validate_supported_encoder_profile(cls, value: str) -> str:
        return _validate_encoder_profile(value)

    @field_validator("loudness_regime")
    @classmethod
    def _validate_supported_loudness_regime(cls, value: str) -> str:
        normalized = _validate_loudness_regime(value)
        assert normalized is not None
        return normalized


class RecordingScheduleInput(BaseModel):
    """Create-a-schedule request body."""

    model_config = ConfigDict(extra="forbid")

    schedule_id: Slug
    station_id: Slug
    name: Annotated[str, Field(min_length=1, max_length=200)]
    source: RecordingSource
    recurrence: RecurrenceSpec
    duration_seconds: int = Field(ge=60, le=12 * 60 * 60)
    encoder_profile: Slug
    loudness_regime: Annotated[str, Field(default="inherit", max_length=40)] = "inherit"
    target_series: Annotated[str | None, Field(default=None, max_length=120)] = None
    custom_field_values: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("custom_field_values")
    @classmethod
    def _cap_custom_field_values(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_custom_field_values(value)

    @field_validator("target_series")
    @classmethod
    def _validate_target_series(cls, value: str | None) -> str | None:
        return _validate_target_series(value)

    @field_validator("encoder_profile")
    @classmethod
    def _validate_supported_encoder_profile(cls, value: str) -> str:
        return _validate_encoder_profile(value)

    @field_validator("loudness_regime")
    @classmethod
    def _validate_supported_loudness_regime(cls, value: str) -> str:
        normalized = _validate_loudness_regime(value)
        assert normalized is not None
        return normalized


class RecordingScheduleUpdate(BaseModel):
    """Patch a schedule; absent keys unchanged. ``schedule_id`` /
    ``station_id`` set at creation and not editable here."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str | None, Field(default=None, max_length=200)] = None
    source: RecordingSource | None = None
    recurrence: RecurrenceSpec | None = None
    duration_seconds: int | None = Field(default=None, ge=60, le=12 * 60 * 60)
    encoder_profile: Slug | None = None
    loudness_regime: Annotated[str | None, Field(default=None, max_length=40)] = None
    target_series: Annotated[str | None, Field(default=None, max_length=120)] = None
    custom_field_values: dict[str, Any] | None = None
    enabled: bool | None = None

    @field_validator("custom_field_values")
    @classmethod
    def _cap_custom_field_values(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return _validate_custom_field_values(value)

    @field_validator("target_series")
    @classmethod
    def _validate_target_series(cls, value: str | None) -> str | None:
        return _validate_target_series(value)

    @field_validator("encoder_profile")
    @classmethod
    def _validate_supported_encoder_profile(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_encoder_profile(value)

    @field_validator("loudness_regime")
    @classmethod
    def _validate_supported_loudness_regime(cls, value: str | None) -> str | None:
        return _validate_loudness_regime(value)


class RecordingJob(BaseModel):
    """One planned / running / completed capture."""

    model_config = ConfigDict(extra="forbid")

    job_id: Slug
    station_id: Slug
    schedule_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    # The planned window — the materializer computes these from the schedule's
    # recurrence + duration_seconds and stamps them onto the job at creation.
    planned_start: datetime
    planned_end: datetime
    state: JobState = "scheduled"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    asset_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    bytes_written: int = Field(default=0, ge=0)
    failure_reason: Annotated[str | None, Field(default=None, max_length=500)] = None
    # Item 6 (recording/ingest hardening): a mid-recording source dropout that
    # the capture pipeline detected and reconnected from. Durable on the job so
    # it survives the process and is visible in the job list / support bundle
    # without a separate event table (DC-1-style: reuse what's already here).
    dropout_count: int = Field(default=0, ge=0)
    last_dropout_at: datetime | None = None
    # The source descriptor at job-create time. We snapshot it onto the job
    # so a schedule edit mid-window doesn't retroactively change what was
    # supposed to be captured.
    source_snapshot: RecordingSource
    encoder_profile: Slug
    loudness_regime: Annotated[str, Field(default="inherit", max_length=40)] = "inherit"
    target_series: Annotated[str | None, Field(default=None, max_length=120)] = None
    custom_field_values: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @field_validator("custom_field_values")
    @classmethod
    def _cap_custom_field_values(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_custom_field_values(value)

    @field_validator("encoder_profile")
    @classmethod
    def _validate_supported_encoder_profile(cls, value: str) -> str:
        return _validate_encoder_profile(value)

    @field_validator("loudness_regime")
    @classmethod
    def _validate_supported_loudness_regime(cls, value: str) -> str:
        normalized = _validate_loudness_regime(value)
        assert normalized is not None
        return normalized

    @field_validator("target_series")
    @classmethod
    def _validate_target_series_field(cls, value: str | None) -> str | None:
        return _validate_target_series(value)

    @model_validator(mode="after")
    def _validate_window(self) -> RecordingJob:
        if self.planned_end <= self.planned_start:
            raise ValueError("planned_end must be strictly after planned_start.")
        return self


# ---------------------------------------------------------------------------
# Public projections (operator-portal-visible shapes — there are no public-
# portal endpoints for S21; recordings produce normal Assets which the
# existing publish pipeline gates separately. We still drop a few internal
# fields from the staff projection so the audit-team sees a clean key-set.)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SQLAlchemy ORM twins (schema applied by migration 0056, not here)
# ---------------------------------------------------------------------------


class RecordingScheduleDb(Base):
    """One row per scheduled capture."""

    __tablename__ = "recording_schedules"
    __table_args__ = (
        UniqueConstraint("station_id", "name", name="recording_schedules_station_name_unique"),
        Index("ix_recording_schedules_station", "station_id"),
        Index("ix_recording_schedules_enabled", "enabled"),
    )

    schedule_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    recurrence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    encoder_profile: Mapped[str] = mapped_column(String(120), nullable=False)
    loudness_regime: Mapped[str] = mapped_column(String(40), nullable=False, default="inherit")
    target_series: Mapped[str | None] = mapped_column(String(120), nullable=True)
    custom_field_values: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class RecordingJobDb(Base):
    """One row per planned / running / completed capture."""

    __tablename__ = "recording_jobs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('scheduled', 'arming', 'recording', 'finalizing', "
            "'done', 'failed', 'skipped')",
            name="recording_jobs_state_check",
        ),
        # Hot-path read: "what's running on this station?"
        Index("ix_recording_jobs_station_state", "station_id", "state"),
        # Scheduler read: "what's due in this horizon?"
        Index("ix_recording_jobs_planned_start", "planned_start"),
        # Overlap detection (DC-5): "are there other jobs in this window on
        # this source for this station?"
        Index("ix_recording_jobs_schedule", "schedule_id"),
    )

    job_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(120), nullable=False)
    schedule_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    planned_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    planned_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    asset_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    bytes_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    dropout_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_dropout_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    encoder_profile: Mapped[str] = mapped_column(String(120), nullable=False)
    loudness_regime: Mapped[str] = mapped_column(String(40), nullable=False, default="inherit")
    target_series: Mapped[str | None] = mapped_column(String(120), nullable=True)
    custom_field_values: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


__all__ = [
    "JOB_STATE_ACTIVE",
    "JOB_STATE_TERMINAL",
    "JOB_STATE_VALUES",
    "RECURRENCE_KIND_VALUES",
    "SOURCE_KIND_VALUES",
    "WEEKDAY_VALUES",
    "JobState",
    "RecordingJob",
    "RecordingJobDb",
    "RecordingSchedule",
    "RecordingScheduleDb",
    "RecordingScheduleInput",
    "RecordingScheduleUpdate",
    "RecordingSource",
    "RecurrenceKind",
    "RecurrenceSpec",
    "Slug",
    "SourceKind",
]
