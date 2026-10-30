# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Live-broadcast spine: SA mapped classes + Pydantic peers + constants.

Sprint 0.4 Slice 1 Commit 3. Schema-only commit: no API behavior, no
store behavior, no preflight evaluator, no finalization handler. The
three SA mapped classes (``LiveSession``, ``LiveSource``,
``RecordingTarget``) describe the data spine that later Slice 1 commits
build their behavior on top of.

State-machine semantics (locked at design-note time, see
``docs/research/v04-slice1-broadcast-spine-design.md``):

- ``idle`` -- session created, not yet pre-flighted.
- ``preflight`` -- pre-flight checklist running.
- ``on_air`` -- live broadcast in progress.
- ``ending`` -- operator clicked End; finalization in progress.
- ``recorded`` -- finalization complete; asset row created at
  ``ASSET_STATE_RECORDED`` (see civiccast/schedule/models.py and
  migration ``0006_widen_asset_state_check``).

Source types match the v0.4 release-plan scope (RTMP / RTSP / NDI /
SRT). SDI is post-1.0 cable add-on territory per spec section 8.3.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base
from civiccast.live.readiness import (
    PROBE_STATE_NEVER_PROBED,
    PROBE_STATES,
    ProbeStateValue,
    ReadinessValue,
    next_action_for,
    observation_age_seconds,
    readiness_state,
    readiness_ttl_seconds,
)
from civiccast.live.source_endpoints import (
    credential_support_reason,
    normalize_endpoint,
    supports_credentials,
)

# ---------------------------------------------------------------------------
# State + type constants
# ---------------------------------------------------------------------------

LIVE_SESSION_STATE_IDLE = "idle"
LIVE_SESSION_STATE_PREFLIGHT = "preflight"
LIVE_SESSION_STATE_ON_AIR = "on_air"
LIVE_SESSION_STATE_ENDING = "ending"
LIVE_SESSION_STATE_RECORDED = "recorded"

_LIVE_SESSION_STATES: tuple[str, ...] = (
    LIVE_SESSION_STATE_IDLE,
    LIVE_SESSION_STATE_PREFLIGHT,
    LIVE_SESSION_STATE_ON_AIR,
    LIVE_SESSION_STATE_ENDING,
    LIVE_SESSION_STATE_RECORDED,
)
LiveSessionStateValue = Literal["idle", "preflight", "on_air", "ending", "recorded"]

SOURCE_TYPE_RTMP = "rtmp"
SOURCE_TYPE_RTSP = "rtsp"
SOURCE_TYPE_NDI = "ndi"
SOURCE_TYPE_SRT = "srt"

_SOURCE_TYPES: tuple[str, ...] = (
    SOURCE_TYPE_RTMP,
    SOURCE_TYPE_RTSP,
    SOURCE_TYPE_NDI,
    SOURCE_TYPE_SRT,
)
LiveSourceTypeValue = Literal["rtmp", "rtsp", "ndi", "srt"]

RELAY_MODE_LOCAL_RTMP = "local_rtmp"
RELAY_MODE_CLOUD_RTMP = "cloud_rtmp_relay"
RELAY_MODE_DIRECT_SYNDICATION = "direct_syndication"

_RELAY_MODES: tuple[str, ...] = (
    RELAY_MODE_LOCAL_RTMP,
    RELAY_MODE_CLOUD_RTMP,
    RELAY_MODE_DIRECT_SYNDICATION,
)
LiveRelayModeValue = Literal["local_rtmp", "cloud_rtmp_relay", "direct_syndication"]

RELAY_HEALTH_NOT_CONFIGURED = "not_configured"
RELAY_HEALTH_READY = "ready"
RELAY_HEALTH_DEGRADED = "degraded"
RELAY_HEALTH_OFFLINE = "offline"

_RELAY_HEALTH_STATES: tuple[str, ...] = (
    RELAY_HEALTH_NOT_CONFIGURED,
    RELAY_HEALTH_READY,
    RELAY_HEALTH_DEGRADED,
    RELAY_HEALTH_OFFLINE,
)
LiveRelayHealthValue = Literal["not_configured", "ready", "degraded", "offline"]

# LiveSessionEvent event_type values. Slice 1 Commit 7 only emits
# ``session.finalized``; ``session.started`` and ``session.ended`` are
# defined in the contract (and the DB CHECK constraint) so a future
# commit can emit them at the go_on_air / end_broadcast transitions
# without a schema change.
LIVE_SESSION_EVENT_STARTED = "session.started"
LIVE_SESSION_EVENT_ENDED = "session.ended"
LIVE_SESSION_EVENT_FINALIZED = "session.finalized"

_LIVE_SESSION_EVENT_TYPES: tuple[str, ...] = (
    LIVE_SESSION_EVENT_STARTED,
    LIVE_SESSION_EVENT_ENDED,
    LIVE_SESSION_EVENT_FINALIZED,
)
LiveSessionEventTypeValue = Literal[
    "session.started",
    "session.ended",
    "session.finalized",
]

FINALIZATION_STATE_PENDING = "pending"
FINALIZATION_STATE_RUNNING = "running"
FINALIZATION_STATE_FAILED = "failed"
FINALIZATION_STATE_COMPLETED = "completed"

_FINALIZATION_STATES: tuple[str, ...] = (
    FINALIZATION_STATE_PENDING,
    FINALIZATION_STATE_RUNNING,
    FINALIZATION_STATE_FAILED,
    FINALIZATION_STATE_COMPLETED,
)
FinalizationStateValue = Literal["pending", "running", "failed", "completed"]

# Stable failure-code taxonomy for finalization jobs (Stage B+D audit
# UX-002/UX-003). ``failure_code`` is the machine identifier consumers branch
# on; ``failure_reason`` carries the operator-facing sentence; raw exception
# text is demoted to ``failure_detail``.
FAILURE_CODE_RECORDING_NEVER_APPEARED = "recording.never_appeared"
FAILURE_CODE_RECORDING_NOT_LOCAL = "recording.not_local"
FAILURE_CODE_PROBE_FAILED = "probe.failed"
FAILURE_CODE_INVALID_TRIM = "finalize.invalid_trim"
FAILURE_CODE_PACKAGE_FAILED = "package.failed"
FAILURE_CODE_CDN_UPLOAD_FAILED = "cdn.upload_failed"
FAILURE_CODE_WORKER_INTERRUPTED = "worker.interrupted"
FAILURE_CODE_INTERNAL_ERROR = "internal.error"

FINALIZATION_FAILURE_CODES: tuple[str, ...] = (
    FAILURE_CODE_RECORDING_NEVER_APPEARED,
    FAILURE_CODE_RECORDING_NOT_LOCAL,
    FAILURE_CODE_PROBE_FAILED,
    FAILURE_CODE_INVALID_TRIM,
    FAILURE_CODE_PACKAGE_FAILED,
    FAILURE_CODE_CDN_UPLOAD_FAILED,
    FAILURE_CODE_WORKER_INTERRUPTED,
    FAILURE_CODE_INTERNAL_ERROR,
)


# Slug pattern matches the asset_id / channel_id pattern already used in
# civiccast.schedule.models. Centralizing here would create a cross-module
# dependency; for Slice 1 the pattern is duplicated. A shared validator
# can land in a later cleanup rung if drift becomes a concern.
_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$|^[a-z0-9]$")


# ---------------------------------------------------------------------------
# SQLAlchemy mapped classes
# ---------------------------------------------------------------------------


class LiveSession(Base):
    """A live broadcast session.

    Created at state ``idle`` when the operator opens the live room.
    Advances through ``preflight`` (checklist runs), ``on_air`` (live
    feed flowing), ``ending`` (operator clicked End; finalization in
    progress), to ``recorded`` (finalization complete; an asset row
    exists at state ``recorded``).
    """

    __tablename__ = "live_sessions"
    __table_args__ = (
        CheckConstraint(
            "state IN ('idle', 'preflight', 'on_air', 'ending', 'recorded')",
            name="live_sessions_state_check",
        ),
    )

    live_session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default=LIVE_SESSION_STATE_IDLE)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Provenance (Beta sprint B1, decision #5): the recording target resolved
    # at go-on-air. The finalization worker uses this instead of guessing
    # from the global target list; NULL for sessions that predate the stamp.
    recording_target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recording_target_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class LiveSource(Base):
    """A configured live input source.

    Operator-managed descriptor pointing at an RTMP, RTSP, NDI, or SRT
    feed. Credentials (if any) are referenced by handle into the OS
    credential store per spec section 15; the handle is opaque at the
    schema layer.
    """

    __tablename__ = "live_sources"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('rtmp', 'rtsp', 'ndi', 'srt')",
            name="live_sources_source_type_check",
        ),
        CheckConstraint(
            "probe_state IN ('never_probed', 'ready', 'failed')",
            name="live_sources_probe_state_check",
        ),
    )

    live_source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(String(8), nullable=False)
    endpoint_url: Mapped[str] = mapped_column(Text, nullable=False)
    credentials_handle: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    # --- Observed readiness (WP-07, migration 0086_live_source_probe_state) ---
    # The row used to BE the readiness claim: civiccast.live.relay._source_path
    # stamped health_state='ready' on every configured source, and that value is
    # the only gate live takeover applies before changing air. These five columns
    # replace "it exists" with "somebody looked, and here is when and what they
    # saw". ``probe_state`` is deliberately one of three durable values --
    # staleness is derived from ``probe_observed_at`` against the readiness TTL
    # (civiccast.live.readiness), never written down, because a persisted
    # "stale" would outlive the successful probe that should have cleared it.
    probe_state: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'never_probed'"),
    )
    probe_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    #: Operator-safe, truncated, secret-redacted reason for the last observation.
    probe_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Stable machine code for the last observation (see
    #: ``civiccast.live.source_probe.PROBE_ERROR_CODES``). Kept separate from
    #: ``probe_detail`` so the UI can branch on the cause without parsing prose.
    probe_error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    #: When this source last actually delivered media. Survives a later failure
    #: so the operator can tell "never worked" from "worked until 09:41".
    probe_last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    #: Optimistic-concurrency token. Incremented on every operator edit; a PATCH
    #: carrying a stale value is rejected rather than silently overwriting the
    #: edit another operator made from the second Live Room window.
    row_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )


class RecordingTarget(Base):
    """Where finalized live recordings land.

    A local location the finalization worker can read: a ``file://`` URI, a
    Windows drive path, or an absolute POSIX path. Shape is enforced at the
    Pydantic surface (``RecordingTargetCreate.target_uri``), not at the DB
    layer; object-store schemes are rejected until object-store recording
    support exists (QA-007).
    """

    __tablename__ = "recording_targets"
    # No CHECK constraints at this stage; URI shape is enforced at the
    # Pydantic surface, not at the DB layer.

    recording_target_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    target_uri: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class LiveRelayConfig(Base):
    """Optional outbound relay or direct-syndication target.

    Local RTMP remains the default free path. A row in this table means the
    station intentionally configured either an outbound cloud relay path back
    to CivicCast live packaging, or a direct-to-platform RTMP target used when
    local station hardware is offline.
    """

    __tablename__ = "live_relay_configs"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('local_rtmp', 'cloud_rtmp_relay', 'direct_syndication')",
            name="live_relay_configs_mode_check",
        ),
        CheckConstraint(
            "health_state IN ('not_configured', 'ready', 'degraded', 'offline')",
            name="live_relay_configs_health_state_check",
        ),
    )

    relay_config_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    endpoint_url: Mapped[str] = mapped_column(Text, nullable=False)
    return_playback_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    credentials_handle: Mapped[str | None] = mapped_column(String(200), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    health_state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=RELAY_HEALTH_NOT_CONFIGURED,
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class LiveSessionEvent(Base):
    """Typed audit row per live-session lifecycle event.

    Slice 1 Commit 7. The composite primary key
    ``(live_session_id, event_type, event_seq)`` is the idempotency
    gate for finalization: a duplicate ``session.finalized`` event
    collides on the PK and the finalization transaction rolls back,
    so the caller's retry returns the pre-existing asset row rather
    than creating a second one. ``event_seq`` is monotonic per session
    (starts at 1); Slice 1 Commit 7 emits ``session.finalized`` with
    ``event_seq=1`` -- future commits emit ``session.started`` and
    ``session.ended`` with higher seq values at the matching state
    transitions without a schema change.

    ``payload_json`` is a free-form serialized payload. Stored as Text
    (not JSONB) so SQLite + Postgres both work without dragging in a
    Postgres-specific column type. Slice 1 Commit 7 writes a finalization
    payload of ``{"recording_uri", "duration_seconds", "finalized_at"}``;
    later commits can extend the shape per event type.
    """

    __tablename__ = "live_session_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('session.started', 'session.ended', 'session.finalized')",
            name="live_session_events_event_type_check",
        ),
        CheckConstraint("event_seq >= 1", name="live_session_events_event_seq_positive"),
    )

    live_session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    event_seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class LiveFinalizationJob(Base):
    """Persisted worker status for an ending live-session recording.

    The worker creates one row per live session when it first observes the
    session in ``ending``. The row is the operator-visible state surface and
    stores retry, settle-detection, and local package-output facts that do not
    belong in the public ``assets.manifest_url`` field.
    """

    __tablename__ = "live_finalization_jobs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'running', 'failed', 'completed')",
            name="live_finalization_jobs_state_check",
        ),
        CheckConstraint("attempts >= 0", name="live_finalization_jobs_attempts_nonneg"),
        CheckConstraint("max_attempts >= 1", name="live_finalization_jobs_max_attempts_positive"),
    )

    live_session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=FINALIZATION_STATE_PENDING,
        server_default=FINALIZATION_STATE_PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    recording_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    # BigInteger, not Integer: Postgres INTEGER caps at ~2 GiB, a routine
    # council-meeting recording size (ENG-003). SQLite cannot catch the width
    # mistake — its integers are 64-bit — so a guard test pins the type
    # (tests/db/test_migration_graph_guards.py).
    recording_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_observed_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    asset_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    local_package_manifest_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    package_manifest_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    trim_in_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    trim_out_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The trim the on-disk package was actually rendered with (Beta B3).
    # Divergence from the asset's trim re-queues the job for repackaging.
    packaged_trim_in_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    packaged_trim_out_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Pydantic peers
# ---------------------------------------------------------------------------


def _validate_slug(value: str) -> str:
    if not _SLUG_PATTERN.match(value):
        raise ValueError(
            "identifier must match [a-z0-9][a-z0-9-]*[a-z0-9] (lowercase "
            "alphanumeric + hyphens, no leading/trailing hyphen)"
        )
    return value


def _validate_stream_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"rtmp", "rtmps", "srt", "http", "https"}:
        raise ValueError("stream URL must use rtmp, rtmps, srt, http, or https")
    if not parsed.netloc:
        raise ValueError("stream URL must include a host")
    return value


class LiveSessionCreate(BaseModel):
    """Request body for creating a live session.

    The session is created at state ``idle``; state advancement is
    driven by store-layer transitions in a later Slice 1 commit, not
    by client-supplied state on create.
    """

    model_config = ConfigDict(extra="forbid")

    live_session_id: Annotated[str, Field(min_length=1, max_length=64)]
    channel_id: Annotated[str, Field(min_length=1, max_length=64)]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    notes: str | None = None

    @field_validator("live_session_id", "channel_id")
    @classmethod
    def _slug_check(cls, value: str) -> str:
        return _validate_slug(value)


class LiveSessionResponse(BaseModel):
    """Response shape for live session reads."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    live_session_id: str
    channel_id: str
    title: str
    state: LiveSessionStateValue
    started_at: datetime | None
    ended_at: datetime | None
    notes: str | None
    recording_target_id: Annotated[
        str | None,
        Field(
            description=(
                "Recording target resolved when the session went on air "
                "(provenance for the finalization worker); null for sessions "
                "that predate the stamp or stations with no resolvable target."
            )
        ),
    ] = None
    recording_target_uri: Annotated[
        str | None,
        Field(
            description=(
                "Resolved recording-target URI captured at go-on-air. "
                "Diagnostic: a server-local location — render inside a "
                "collapsed technical-details disclosure."
            )
        ),
    ] = None
    created_at: datetime

    @field_validator("state", mode="before")
    @classmethod
    def _state_in_enum(cls, value: str) -> str:
        if value not in _LIVE_SESSION_STATES:
            raise ValueError(f"state must be one of {_LIVE_SESSION_STATES!r}; got {value!r}")
        return value


def check_credential_shape(source_type: str, credentials_handle: str | None) -> None:
    """Reject a stored credential on a source type CivicCast cannot run one for.

    Requirement, not politeness: an rtsp/rtmp row carrying a handle would look
    authenticated in the operator UI while every probe and every playout leg
    silently ran unauthenticated. Rejecting the shape is the only honest
    outcome -- see ``civiccast.live.source_endpoints`` for why only SRT
    qualifies.
    """
    if not credentials_handle or not credentials_handle.strip():
        return
    if not supports_credentials(source_type):
        raise ValueError(credential_support_reason(source_type))


class LiveSourceCreate(BaseModel):
    """Request body for creating a configured live source."""

    model_config = ConfigDict(extra="forbid")

    live_source_id: Annotated[str, Field(min_length=1, max_length=64)]
    channel_id: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    source_type: LiveSourceTypeValue
    endpoint_url: str
    credentials_handle: Annotated[str | None, Field(default=None, max_length=200)] = None

    @field_validator("live_source_id", "channel_id")
    @classmethod
    def _slug_check(cls, value: str) -> str:
        return _validate_slug(value)

    @field_validator("source_type", mode="before")
    @classmethod
    def _source_type_check(cls, value: str) -> str:
        if value not in _SOURCE_TYPES:
            raise ValueError(f"source_type must be one of {_SOURCE_TYPES!r}; got {value!r}")
        return value

    @model_validator(mode="after")
    def _endpoint_matches_type(self) -> LiveSourceCreate:
        # WP-07: one rule, one place. Before this the create surface accepted
        # ``HttpUrl | str`` and never asked whether the address matched the
        # source type at all, so an ``srt`` row could hold an ``http://`` URL
        # that no probe and no playout element could ever open.
        object.__setattr__(
            self, "endpoint_url", normalize_endpoint(self.source_type, str(self.endpoint_url))
        )
        check_credential_shape(self.source_type, self.credentials_handle)
        return self


class LiveSourceUpdate(BaseModel):
    """Request body for ``PATCH /api/staff/live/sources/{live_source_id}``.

    Every field is optional; omitted fields are left alone. ``channel_id`` is
    editable because moving a mis-filed source to the right channel was
    otherwise a delete-and-recreate the store has no delete for.

    ``expected_row_version`` is the optimistic-concurrency token. When present
    it must equal the row's current ``row_version`` or the update is rejected
    409; when absent the update is last-writer-wins, which is what a scripted
    single-operator station wants and what the operator UI never sends.
    """

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str | None, Field(default=None, min_length=1, max_length=64)] = None
    name: Annotated[str | None, Field(default=None, min_length=1, max_length=200)] = None
    source_type: LiveSourceTypeValue | None = None
    endpoint_url: str | None = None
    credentials_handle: Annotated[str | None, Field(default=None, max_length=200)] = None
    #: Sentinel for "clear the stored credential handle" -- distinct from
    #: ``credentials_handle=None`` meaning "leave it alone", which is what
    #: every other omitted field means in this model.
    clear_credentials_handle: bool = False
    expected_row_version: Annotated[int | None, Field(default=None, ge=1)] = None

    @field_validator("channel_id")
    @classmethod
    def _slug_check(cls, value: str | None) -> str | None:
        return None if value is None else _validate_slug(value)

    @field_validator("source_type", mode="before")
    @classmethod
    def _source_type_check(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in _SOURCE_TYPES:
            raise ValueError(f"source_type must be one of {_SOURCE_TYPES!r}; got {value!r}")
        return value

    @model_validator(mode="after")
    def _at_least_one_change(self) -> LiveSourceUpdate:
        if not self.changed_fields():
            raise ValueError(
                "Send at least one field to change (channel_id, name, source_type, "
                "endpoint_url, credentials_handle, or clear_credentials_handle)."
            )
        if self.credentials_handle is not None and self.clear_credentials_handle:
            raise ValueError(
                "Send either credentials_handle or clear_credentials_handle, not both."
            )
        return self

    def changed_fields(self) -> set[str]:
        """Names of the fields this request actually asks to change."""
        fields = {
            name
            for name in ("channel_id", "name", "source_type", "endpoint_url", "credentials_handle")
            if getattr(self, name) is not None
        }
        if self.clear_credentials_handle:
            fields.add("credentials_handle")
        return fields

    def invalidates_readiness(self) -> bool:
        """Whether this edit makes the previous probe observation meaningless.

        Endpoint, source type, channel, and credential reference all change
        *what would be probed*; a name change does not. The store clears
        readiness on the former and leaves it on the latter, so renaming a
        camera thirty seconds before gavel does not force a re-probe.
        """
        return bool(
            self.changed_fields()
            & {"channel_id", "source_type", "endpoint_url", "credentials_handle"}
        )


class LiveSourceResponse(BaseModel):
    """Response shape for live source reads.

    Carries the persisted observation (``probe_*``) AND the derived answer the
    operator actually needs (``readiness``, ``observation_age_seconds``,
    ``next_action``). Deriving it server-side is deliberate: the readiness TTL
    is a station setting, and a client computing staleness from its own clock
    would disagree with the takeover gate that refuses it.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    live_source_id: str
    channel_id: str
    name: str
    source_type: LiveSourceTypeValue
    endpoint_url: str
    credentials_handle: str | None
    created_at: datetime
    probe_state: ProbeStateValue = PROBE_STATE_NEVER_PROBED
    probe_observed_at: datetime | None = None
    probe_detail: str | None = None
    probe_error_code: str | None = None
    probe_last_success_at: datetime | None = None
    row_version: int = 1

    @field_validator("source_type", mode="before")
    @classmethod
    def _source_type_check(cls, value: str) -> str:
        if value not in _SOURCE_TYPES:
            raise ValueError(f"source_type must be one of {_SOURCE_TYPES!r}; got {value!r}")
        return value

    @field_validator("probe_state", mode="before")
    @classmethod
    def _probe_state_check(cls, value: str | None) -> str:
        # Fail closed: a row written by an older build, or one whose column is
        # somehow unrecognized, reads as "nobody has looked" rather than ready.
        return value if value in PROBE_STATES else PROBE_STATE_NEVER_PROBED

    @computed_field  # type: ignore[prop-decorator]
    @property
    def readiness_ttl_seconds(self) -> int:
        """The station's configured readiness window, as actually applied."""
        return readiness_ttl_seconds()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def observation_age_seconds(self) -> float | None:
        """Seconds since the last observation; ``None`` when never probed."""
        return observation_age_seconds(self.probe_observed_at)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def readiness(self) -> ReadinessValue:
        """``ready`` / ``stale`` / ``failed`` / ``never_probed``."""
        return readiness_state(
            self.probe_state,
            self.probe_observed_at,
            ttl_seconds=readiness_ttl_seconds(),
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def credentials_supported(self) -> bool:
        """Whether this source type can carry a stored credential at all."""
        return supports_credentials(self.source_type)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def credentials_unsupported_reason(self) -> str | None:
        """Copy for the disabled credential control; ``None`` when supported."""
        return credential_support_reason(self.source_type)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def next_action(self) -> str:
        """The one thing the operator should do next about this source."""
        return next_action_for(self.readiness, source_name=self.name, detail=self.probe_detail)


class LiveSourceProbeResponse(BaseModel):
    """Result of an explicit operator-triggered probe.

    Returns the whole refreshed source rather than a bare verdict so the Live
    Room can replace the row it just probed without a second round trip and
    without the two surfaces briefly disagreeing about readiness.
    """

    model_config = ConfigDict(extra="forbid")

    source: LiveSourceResponse
    probed_at: datetime
    ok: bool
    error_code: str | None = None
    detail: str | None = None


class RecordingTargetCreate(BaseModel):
    """Request body for creating a recording target."""

    model_config = ConfigDict(extra="forbid")

    recording_target_id: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    target_uri: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Where recordings land: a `file://` URI, a Windows drive path "
                "(`C:\\recordings`), or an absolute POSIX path. Other schemes "
                "and relative paths are rejected — the finalization worker can "
                "only read local files (QA-003/QA-007: unusable values used to "
                "be accepted and silently wedged finalization)."
            ),
        ),
    ]

    @field_validator("recording_target_id")
    @classmethod
    def _slug_check(cls, value: str) -> str:
        return _validate_slug(value)

    @field_validator("target_uri")
    @classmethod
    def _target_uri_resolvable(cls, value: str) -> str:
        from civiccast.live.finalization_worker import _local_recording_path

        if _local_recording_path(value) is None:
            raise ValueError(
                "target_uri must be a local location the finalization worker "
                "can read: a file:// URI (e.g. file:///C:/recordings), a "
                "Windows drive path (C:\\recordings), or an absolute POSIX "
                "path. Relative paths and non-file schemes are not supported."
            )
        return value


class RecordingTargetResponse(BaseModel):
    """Response shape for recording target reads."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    recording_target_id: str
    name: str
    target_uri: str
    created_at: datetime


class LiveRelayConfigCreate(BaseModel):
    """Request body for an optional remote ingest/relay target."""

    model_config = ConfigDict(extra="forbid")

    relay_config_id: Annotated[str, Field(min_length=1, max_length=64)]
    channel_id: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    mode: LiveRelayModeValue
    endpoint_url: Annotated[str, Field(min_length=1)]
    return_playback_url: str | None = None
    provider: Annotated[str | None, Field(max_length=64)] = None
    credentials_handle: Annotated[str | None, Field(max_length=200)] = None
    enabled: bool = True
    notes: str | None = None

    @field_validator("relay_config_id", "channel_id")
    @classmethod
    def _slug_check(cls, value: str) -> str:
        return _validate_slug(value)

    @field_validator("mode", mode="before")
    @classmethod
    def _mode_check(cls, value: str) -> str:
        if value not in _RELAY_MODES:
            raise ValueError(f"mode must be one of {_RELAY_MODES!r}; got {value!r}")
        return value

    @field_validator("endpoint_url")
    @classmethod
    def _endpoint_url_check(cls, value: str) -> str:
        return _validate_stream_url(value)

    @field_validator("return_playback_url")
    @classmethod
    def _return_playback_url_check(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_stream_url(value)


class LiveRelayHealthUpdate(BaseModel):
    """Request body for updating relay health from a station probe."""

    model_config = ConfigDict(extra="forbid")

    health_state: LiveRelayHealthValue
    last_heartbeat_at: datetime | None = None
    notes: str | None = None

    @field_validator("health_state", mode="before")
    @classmethod
    def _health_state_check(cls, value: str) -> str:
        if value not in _RELAY_HEALTH_STATES:
            raise ValueError(f"health_state must be one of {_RELAY_HEALTH_STATES!r}; got {value!r}")
        return value


class LiveRelayConfigResponse(BaseModel):
    """Response shape for remote ingest/relay configuration."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    relay_config_id: str
    channel_id: str
    name: str
    mode: LiveRelayModeValue
    endpoint_url: str
    return_playback_url: str | None
    provider: str | None
    credentials_handle: str | None
    enabled: bool
    health_state: LiveRelayHealthValue
    last_heartbeat_at: datetime | None
    notes: str | None
    created_at: datetime

    @field_validator("mode", mode="before")
    @classmethod
    def _mode_check(cls, value: str) -> str:
        if value not in _RELAY_MODES:
            raise ValueError(f"mode must be one of {_RELAY_MODES!r}; got {value!r}")
        return value

    @field_validator("health_state", mode="before")
    @classmethod
    def _health_state_check(cls, value: str) -> str:
        if value not in _RELAY_HEALTH_STATES:
            raise ValueError(f"health_state must be one of {_RELAY_HEALTH_STATES!r}; got {value!r}")
        return value


class LiveIngestPath(BaseModel):
    """Operator-safe path in the live ingest plan."""

    model_config = ConfigDict(extra="forbid")

    path_id: str
    label: str
    mode: LiveRelayModeValue
    endpoint_url: str
    return_playback_url: str | None = None
    provider: str | None = None
    enabled: bool
    health_state: LiveRelayHealthValue
    outbound_only: bool
    requires_inbound_firewall: bool
    operator_action: str
    risk_note: str | None = None


class LiveIngestPlan(BaseModel):
    """Staff-facing plan for local, cloud-relay, and direct platform ingest."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    generated_at: datetime
    local_default: LiveIngestPath
    relay_paths: list[LiveIngestPath]
    recommended_path_id: str
    degraded_count: int
    direct_syndication_available: bool


class LiveSessionEventResponse(BaseModel):
    """Response shape for ``live_session_events`` rows.

    The finalizer returns this peer wrapped inside the
    :class:`civiccast.live.finalization.FinalizationResult`. The
    ``payload_json`` field is intentionally surfaced verbatim: callers
    that care about typed payload contents do their own ``json.loads``
    (the Slice 1 contract keeps payload schemas per-event-type, so a
    universal Pydantic shape would over-promise).
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    live_session_id: str
    event_type: LiveSessionEventTypeValue
    event_seq: int
    payload_json: str | None
    created_at: datetime

    @field_validator("event_type", mode="before")
    @classmethod
    def _event_type_in_enum(cls, value: str) -> str:
        if value not in _LIVE_SESSION_EVENT_TYPES:
            raise ValueError(
                f"event_type must be one of {_LIVE_SESSION_EVENT_TYPES!r}; got {value!r}"
            )
        return value


class LiveFinalizationStatusResponse(BaseModel):
    """Operator-visible status for the recording finalization worker."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    live_session_id: Annotated[
        str, Field(description="Live session this finalization job belongs to.")
    ]
    state: Annotated[
        FinalizationStateValue,
        Field(
            description=(
                "`pending`: waiting for the recording file to appear/settle. "
                "`running`: an attempt is in progress. `failed`: the last "
                "attempt failed — the job retries automatically while "
                "`attempts < max_attempts` (see `next_attempt_at`) and is "
                "terminal once attempts are exhausted (see `terminal`). "
                "`completed`: finalized and packaged."
            )
        ),
    ]
    attempts: Annotated[int, Field(description="Finalization attempts made so far.")]
    max_attempts: Annotated[
        int, Field(description="Attempts before the job becomes terminal `failed`.")
    ]
    recording_uri: Annotated[
        str | None,
        Field(
            description=(
                "Resolved recording location. Diagnostic: a server-local "
                "`file://` URI — render inside a collapsed technical-details "
                "disclosure, not as primary status copy."
            )
        ),
    ]
    recording_size_bytes: Annotated[
        int | None,
        Field(
            description=(
                "Latest observed size of the recording file in bytes (grows "
                "until the file settles; final size once completed)."
            )
        ),
    ]
    next_attempt_at: Annotated[
        datetime | None,
        Field(
            description=(
                "When the next automatic retry is due; null when no retry is "
                "scheduled (not failed, or terminal)."
            )
        ),
    ]
    failure_reason: Annotated[
        str | None,
        Field(
            description=(
                "Operator-facing sentence describing the last failure and "
                "what to do about it. Safe to render verbatim."
            )
        ),
    ]
    failure_code: Annotated[
        str | None,
        Field(
            description=(
                "Stable machine-readable failure identifier: "
                "`recording.never_appeared`, `recording.not_local`, "
                "`probe.failed`, `finalize.invalid_trim`, `package.failed`, "
                "`cdn.upload_failed`, `worker.interrupted`, or `internal.error`."
            )
        ),
    ] = None
    failure_detail: Annotated[
        str | None,
        Field(
            description=(
                "Raw diagnostic detail (exception text) for the last failure. "
                "Diagnostic: may contain server paths — render inside a "
                "collapsed technical-details disclosure."
            )
        ),
    ] = None
    asset_id: Annotated[
        str | None, Field(description="Asset produced by finalization, once completed.")
    ]
    local_package_manifest_path: Annotated[
        str | None,
        Field(
            description=(
                "Filesystem path of the locally packaged HLS manifest; never a "
                "servable URL. Diagnostic: render inside a collapsed "
                "technical-details disclosure."
            )
        ),
    ]
    package_manifest_url: Annotated[
        str | None,
        Field(
            description=(
                "Servable public manifest URL; set only when the worker is "
                "configured with `CIVICCAST_LIVE_MANIFEST_BASE_URL`. Null "
                "means the package is local-only and the asset does not pass "
                "publish readiness."
            )
        ),
    ]
    trim_in_seconds: Annotated[
        float | None,
        Field(
            description=(
                "Trim-in recorded on the finalization job. Operator trims are "
                "applied via the asset trim editor; the worker repackages when "
                "the asset trim diverges from packaged_trim_in_seconds."
            )
        ),
    ]
    trim_out_seconds: Annotated[
        float | None, Field(description="Trim-out recorded on the job (see trim_in_seconds).")
    ]
    packaged_trim_in_seconds: Annotated[
        float | None,
        Field(
            description=(
                "Trim-in the current on-disk package was rendered with; null "
                "until first packaging. When the asset's trim differs, the "
                "worker re-renders the package automatically."
            )
        ),
    ] = None
    packaged_trim_out_seconds: Annotated[
        float | None,
        Field(description="Trim-out the current package was rendered with."),
    ] = None
    started_at: Annotated[
        datetime | None, Field(description="When the current/last attempt started.")
    ]
    completed_at: Annotated[
        datetime | None, Field(description="When finalization completed, if it has.")
    ]
    created_at: Annotated[
        datetime, Field(description="When the worker first observed the ending session.")
    ]
    updated_at: Annotated[
        datetime | None, Field(description="Last time the worker updated this row.")
    ]

    @computed_field(  # type: ignore[prop-decorator]
        description=(
            "True when no further worker transitions will occur: the job is "
            "`completed`, or `failed` with all attempts exhausted (operator "
            "intervention required — see the finalization worker runbook)."
        )
    )
    @property
    def terminal(self) -> bool:
        if self.state == FINALIZATION_STATE_COMPLETED:
            return True
        return self.state == FINALIZATION_STATE_FAILED and self.attempts >= self.max_attempts

    @field_validator("state", mode="before")
    @classmethod
    def _state_in_enum(cls, value: str) -> str:
        if value not in _FINALIZATION_STATES:
            raise ValueError(f"state must be one of {_FINALIZATION_STATES!r}; got {value!r}")
        return value
