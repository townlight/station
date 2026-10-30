# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S16 Production & Control Room entities (CivicCast 3.0 — build step 9).

Pydantic domain contracts + their SQLAlchemy ORM peers for the production
control room. Migration ``0047_production_control`` creates the tables.

Entities (S16 §3):

* ``ProductionDevice`` — one externally-controlled device the station owns
  (OBS / vMix / ATEM / HyperDeck / PTZ / OSC / TCP / HTTP / CasparCG, plus the
  S18 gap-8 ``gpi`` / ``serial`` control kinds). Credentials are NEVER stored
  here in cleartext — ``secret_ref`` points at the OS keyring
  (:mod:`civiccast.control_room.secrets`), mirroring how ``live/`` keeps
  credentials out of operator-facing plans.
  **Honest label (do not build a fake driver):** ``gpi`` and ``serial`` are
  network-relay triggers only. ``tsr_service/index.mjs``'s ``DEVICE_TYPE`` map
  routes both kinds through TSR's generic ``TCPSEND`` adapter — there is no
  real GPI contact-closure or serial (RS-232/422) hardware driver in this
  release. A station that needs real hardware GPI/serial fronts it with its
  own TCP-to-GPI or TCP-to-serial relay box; CivicCast's TCP payload path
  already reaches it.
* ``DeviceProfile`` — the TSR mapping/config for a device: which TSR device
  type, non-secret options, the CivicCast capability map, and (S18 gap-8) the
  Take-Delay / Post-Roll transition timing. Versioned for auditability.
* ``ControlSurface`` — a named operator layout; an ordered set of cues grouped
  into banks, gated by ``assigned_role`` (which real role may fire it).
* ``TimelineCue`` — one fireable action resolved against TSR's timeline-state
  model. Cues are planned + validated server-side before any socket opens
  (the same plan-then-fire discipline as ``facility/router_control.py``).
* ``ControlRoomSession`` — audit record of a live production session bound to a
  ``live/`` program feed; parents an append-only ``CueFiredEvent`` log.
* ``CueFiredEvent`` — the who-did-what-when trail (append-only), redacted of
  secrets.
* ``DeviceCommand`` — S18 gap-8 audit of GPI / serial / router-take device
  commands with their transition timing.

No hard foreign keys: ``device_id`` / ``surface_id`` / ``session_id`` / ``cue_id``
are soft string references resolved in the store (matching the cg + schedule
modules). An audit row must outlive the entity it describes; deleting a device
must not cascade into the surfaces/cues that named it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base

# --- shared literals ---------------------------------------------------------

ProductionDeviceKind = Literal[
    "obs",
    "vmix",
    "atem",
    "hyperdeck",
    "ptz",
    "osc",
    "tcp",
    "http",
    "casparcg",
    # S18 gap-8: facility control beyond production switchers.
    "gpi",
    "serial",
]

DeviceTransport = Literal["tcp", "udp", "http", "websocket", "serial", "gpi"]

CueAction = Literal[
    "scene",
    "input",
    "transition",
    "macro",
    "deck_play",
    "deck_cue",
    "ptz_preset",
    "osc",
    "http",
    "overlay_push",
    "overlay_clear",
    # S18 gap-8 facility actions (audited as DeviceCommands when fired).
    "gpi_pulse",
    "serial_send",
    "router_take",
]

# Cue-firing is a meeting_operator act; a surface may be restricted to a single
# real role. The five real roles are auth/roles.py's canonical set.
SurfaceRole = Literal[
    "setup_admin", "meeting_operator", "records_clerk", "publish_operator", "support_admin"
]

SessionState = Literal["open", "closed"]
SessionMode = Literal["test", "on_air"]

CueResult = Literal["planned", "fired", "failed"]
ControlRoomReadinessStatus = Literal["passed", "warning", "blocked", "not_applicable"]
ControlRoomReadinessSeverity = Literal["info", "warning", "blocker"]

# Device health / state-freshness (S16 item 7): a probe result older than this
# is shown as stale rather than trusted indefinitely. Not a live heartbeat —
# CivicCast only knows what the last probe/fire told it.
DEVICE_HEALTH_STALE_AFTER_SECONDS = 300


# ---------------------------------------------------------------------------
# Pydantic domain models
# ---------------------------------------------------------------------------


class ProductionDevice(BaseModel):
    """One externally-controlled device the station owns. No cleartext secret —
    ``secret_ref`` is an opaque keyring handle resolved at fire time.

    ``last_probed_at`` / ``last_reachable`` are the device-health + state-
    freshness fields (S16 item 7): they record the outcome of the most recent
    probe or fire attempt, not a live/continuous heartbeat. A probe older than
    ``DEVICE_HEALTH_STALE_AFTER_SECONDS`` is stale, not trusted forever."""

    model_config = ConfigDict(extra="forbid")

    device_id: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    kind: Annotated[
        ProductionDeviceKind,
        Field(
            description=(
                "Device kind. 'gpi' and 'serial' are network-relay triggers (TCP) "
                "only -- direct GPI contact-closure / serial (RS-232/422) hardware "
                "is not supported in this release; both route through TSR's generic "
                "TCPSEND adapter. Front real hardware with your own TCP-to-GPI or "
                "TCP-to-serial relay box."
            )
        ),
    ]
    transport: DeviceTransport
    host: Annotated[str | None, Field(default=None, max_length=255)] = None
    port: Annotated[int | None, Field(default=None, ge=1, le=65535)] = None
    enabled: bool = True
    notes: Annotated[str | None, Field(default=None, max_length=2000)] = None
    secret_ref: Annotated[str | None, Field(default=None, max_length=200)] = None
    last_probed_at: datetime | None = None
    last_reachable: bool | None = None
    created_at: datetime
    updated_at: datetime


class DeviceProfile(BaseModel):
    """The TSR mapping/config for a device (non-secret options + capability map
    + S18 gap-8 transition timing). Versioned for audit."""

    model_config = ConfigDict(extra="forbid")

    profile_id: Annotated[str, Field(min_length=1, max_length=120)]
    device_id: Annotated[str, Field(min_length=1, max_length=120)]
    tsr_device_type: Annotated[str, Field(min_length=1, max_length=60)]
    options: dict[str, Any] = Field(default_factory=dict)
    capability_map: dict[str, Any] = Field(default_factory=dict)
    take_delay_ms: Annotated[int, Field(default=0, ge=0, le=600000)] = 0
    post_roll_ms: Annotated[int, Field(default=0, ge=0, le=600000)] = 0
    version: Annotated[int, Field(default=1, ge=1)] = 1
    created_at: datetime
    updated_at: datetime


class TimelineCue(BaseModel):
    """One fireable action resolved against TSR's timeline state.

    ``version`` bumps every time the cue's authored definition changes
    (mirrors ``DeviceProfile.version``); it lets an operator's console detect
    that a cue bank changed under it. Once at least one ``fired`` audit event
    exists for a cue, the cue is immutable — it can no longer be deleted, so
    the fired-cue audit trail always resolves to a real, inspectable cue
    definition instead of one that was edited or removed after the fact."""

    model_config = ConfigDict(extra="forbid")

    cue_id: Annotated[str, Field(min_length=1, max_length=120)]
    surface_id: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    device_id: Annotated[str, Field(min_length=1, max_length=120)]
    action: CueAction
    payload: dict[str, Any] = Field(default_factory=dict)
    confirm_required: bool = False
    bank: Annotated[int, Field(default=0, ge=0, le=99)] = 0
    position: Annotated[int, Field(default=0, ge=0, le=999)] = 0
    version: Annotated[int, Field(default=1, ge=1)] = 1
    proof_boundary: Annotated[str, Field(min_length=1, max_length=300)]
    created_at: datetime


class ControlSurface(BaseModel):
    """A named operator layout: a bank of cues gated by a single real role."""

    model_config = ConfigDict(extra="forbid")

    surface_id: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    assigned_role: SurfaceRole = "meeting_operator"
    created_by: Annotated[str, Field(min_length=1, max_length=120)]
    created_at: datetime
    updated_at: datetime


class ControlRoomSession(BaseModel):
    """Audit record of a live production session bound to a ``live/`` feed."""

    model_config = ConfigDict(extra="forbid")

    session_id: Annotated[str, Field(min_length=1, max_length=120)]
    surface_id: Annotated[str, Field(min_length=1, max_length=120)]
    operator_id: Annotated[str, Field(min_length=1, max_length=120)]
    operator_name: Annotated[str | None, Field(default=None, max_length=200)] = None
    program_feed_source_ref: Annotated[str | None, Field(default=None, max_length=120)] = None
    mode: SessionMode = "test"
    safe_state_cue_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    state: SessionState = "open"
    started_at: datetime
    on_air_expires_at: datetime | None = None
    ended_at: datetime | None = None


class CueFiredEvent(BaseModel):
    """One append-only entry in a session's fired-cue audit (redacted)."""

    model_config = ConfigDict(extra="forbid")

    event_id: Annotated[str, Field(min_length=1, max_length=120)]
    session_id: Annotated[str, Field(min_length=1, max_length=120)]
    cue_id: Annotated[str, Field(min_length=1, max_length=120)]
    operator_id: Annotated[str, Field(min_length=1, max_length=120)]
    device_id: Annotated[str, Field(min_length=1, max_length=120)]
    action: CueAction
    result: CueResult
    fired_at: datetime
    detail: dict[str, Any] = Field(default_factory=dict)


class CuePlan(BaseModel):
    """A server-resolved, inspectable preview of what firing a cue will send —
    the plan half of the plan-then-fire split (mirrors facility RouterTakePlan).
    Building a CuePlan opens NO device socket; ``proof_boundary`` says so."""

    model_config = ConfigDict(extra="forbid")

    cue_id: Annotated[str, Field(min_length=1, max_length=120)]
    surface_id: Annotated[str, Field(min_length=1, max_length=120)]
    device_id: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    action: CueAction
    resolved_payload: dict[str, Any] = Field(default_factory=dict)
    command_preview: Annotated[str, Field(min_length=1, max_length=500)]
    ready_to_send: bool
    confirm_required: bool = False
    material_state_fingerprint: Annotated[str, Field(default="", max_length=128)] = ""
    take_delay_ms: Annotated[int, Field(default=0, ge=0, le=600000)] = 0
    post_roll_ms: Annotated[int, Field(default=0, ge=0, le=600000)] = 0
    operator_action: Annotated[str, Field(min_length=1, max_length=300)]
    proof_boundary: Annotated[str, Field(min_length=1, max_length=300)]


class DeviceCommand(BaseModel):
    """S18 gap-8 audit of a GPI / serial / router-take command + its timing."""

    model_config = ConfigDict(extra="forbid")

    command_id: Annotated[str, Field(min_length=1, max_length=120)]
    device_id: Annotated[str, Field(min_length=1, max_length=120)]
    session_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    command_kind: Annotated[str, Field(min_length=1, max_length=30)]
    command_preview: Annotated[str, Field(min_length=1, max_length=500)]
    take_delay_ms: Annotated[int, Field(default=0, ge=0, le=600000)] = 0
    post_roll_ms: Annotated[int, Field(default=0, ge=0, le=600000)] = 0
    issued_by: Annotated[str, Field(min_length=1, max_length=120)]
    issued_at: datetime
    result: CueResult = "planned"


class ControlRoomReadinessCheck(BaseModel):
    """One operator-visible setup/readiness check for production control."""

    model_config = ConfigDict(extra="forbid")

    check_id: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    status: ControlRoomReadinessStatus
    severity: ControlRoomReadinessSeverity
    detail: Annotated[str, Field(min_length=1, max_length=600)]
    operator_action: Annotated[str, Field(min_length=1, max_length=400)]
    evidence_ref: Annotated[
        str,
        Field(
            min_length=1,
            max_length=200,
            description="Stable code/data reference backing this operator-visible check.",
        ),
    ]


class ControlRoomLpmDeviceCoverage(BaseModel):
    """One LPM device contract row exposed in the readiness report."""

    model_config = ConfigDict(extra="forbid")

    profile_id: Annotated[str, Field(min_length=1, max_length=120)]
    device_contract_id: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    device_class: Annotated[str, Field(min_length=1, max_length=80)]
    integration_surface: Annotated[str, Field(min_length=1, max_length=160)]
    proof_level: Annotated[str, Field(min_length=1, max_length=60)]
    station_device_evidence_required: bool
    required_checks_count: Annotated[int, Field(ge=0)]


class ControlRoomLpmProfileCoverage(BaseModel):
    """One LPM topology profile and its current proof boundary."""

    model_config = ConfigDict(extra="forbid")

    profile_id: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    priority: Annotated[int, Field(ge=1, le=10)]
    proof_status: Annotated[
        str,
        Field(
            min_length=1,
            max_length=80,
            description="Current evidence level for this LPM profile; contract-only values are not station-device evidence.",
        ),
    ]
    devices: list[ControlRoomLpmDeviceCoverage]
    required_absences: list[Annotated[str, Field(min_length=1, max_length=180)]] = Field(
        default_factory=list
    )
    egress_destinations: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(
        default_factory=list
    )
    not_claimed: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(
        default_factory=list,
        description="Proof claims explicitly not made by the current local readiness report.",
    )


class ControlRoomReadinessReport(BaseModel):
    """Current production-control setup posture for operators and support."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    ready_for_on_air: Annotated[
        bool,
        Field(
            description=(
                "True only when local CivicCast configuration and pure safety policy checks "
                "are sufficient for a local On-Air control attempt. It is not clean install, "
                "simulator, hardware, or station-device evidence."
            )
        ),
    ]
    station_device_ready: Annotated[
        bool,
        Field(
            default=False,
            description="True only after LPM station-device evidence exists; local contract-lab proof keeps this false.",
        ),
    ]
    summary: Annotated[
        str,
        Field(
            min_length=1,
            max_length=600,
            description="Operator-facing summary of local readiness blockers and proof limits.",
        ),
    ]
    devices_configured: Annotated[int, Field(ge=0)]
    devices_enabled: Annotated[int, Field(ge=0)]
    devices_missing_profile: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(
        default_factory=list
    )
    surfaces_configured: Annotated[int, Field(ge=0)]
    cues_configured: Annotated[int, Field(ge=0)]
    open_sessions: Annotated[int, Field(ge=0)]
    open_on_air_sessions: Annotated[int, Field(ge=0)]
    checks: list[ControlRoomReadinessCheck]
    lpm_profiles: list[ControlRoomLpmProfileCoverage]
    proof_boundary: Annotated[
        str,
        Field(
            min_length=1,
            max_length=600,
            description="Plain-language boundary of what this readiness report does and does not prove.",
        ),
    ]


# ---------------------------------------------------------------------------
# SQLAlchemy ORM peers (single global metadata; migration 0047 creates tables)
# ---------------------------------------------------------------------------


class ProductionDeviceDb(Base):
    """Durable production-device row."""

    __tablename__ = "production_devices"

    device_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    transport: Mapped[str] = mapped_column(String(20), nullable=False)
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reachable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class DeviceProfileDb(Base):
    """Durable device-profile row."""

    __tablename__ = "device_profiles"

    profile_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    tsr_device_type: Mapped[str] = mapped_column(String(60), nullable=False)
    options: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    capability_map: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    take_delay_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    post_roll_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class TimelineCueDb(Base):
    """Durable timeline-cue row."""

    __tablename__ = "timeline_cues"

    cue_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    surface_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    device_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    confirm_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    bank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    proof_boundary: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class ControlSurfaceDb(Base):
    """Durable control-surface row."""

    __tablename__ = "control_surfaces"

    surface_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    assigned_role: Mapped[str] = mapped_column(
        String(40), nullable=False, default="meeting_operator"
    )
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class ControlRoomSessionDb(Base):
    """Durable control-room-session row."""

    __tablename__ = "control_room_sessions"

    session_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    surface_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    operator_id: Mapped[str] = mapped_column(String(120), nullable=False)
    operator_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    program_feed_source_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="test")
    safe_state_cue_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    on_air_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Partial-unique: at most one OPEN session per control surface -- the
# "operator lock" invariant enforced at the DB level (not just the
# app-level check-then-insert in ControlRoomService.open_session), so a
# race between two concurrent open_session calls for the same surface
# cannot both succeed. Declared as a module-level Index (not inside
# __table_args__) so the postgresql_where / sqlite_where dialect kwargs
# can reference the real column object -- mirrors
# civiccast/producer_ops/models.py's equipment_checkouts pattern.
Index(
    "ix_control_room_sessions_one_open_per_surface",
    ControlRoomSessionDb.surface_id,
    unique=True,
    postgresql_where=ControlRoomSessionDb.state == "open",
    sqlite_where=ControlRoomSessionDb.state == "open",
)


class CueFiredEventDb(Base):
    """Append-only fired-cue audit row."""

    __tablename__ = "control_room_cue_events"

    event_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    cue_id: Mapped[str] = mapped_column(String(120), nullable=False)
    operator_id: Mapped[str] = mapped_column(String(120), nullable=False)
    device_id: Mapped[str] = mapped_column(String(120), nullable=False)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )


class DeviceCommandDb(Base):
    """S18 gap-8 device-command audit row."""

    __tablename__ = "control_room_device_commands"

    command_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    command_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    command_preview: Mapped[str] = mapped_column(String(500), nullable=False)
    take_delay_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    post_roll_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    issued_by: Mapped[str] = mapped_column(String(120), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    result: Mapped[str] = mapped_column(String(20), nullable=False, default="planned")


__all__ = [
    "DEVICE_HEALTH_STALE_AFTER_SECONDS",
    "ControlRoomLpmDeviceCoverage",
    "ControlRoomLpmProfileCoverage",
    "ControlRoomReadinessCheck",
    "ControlRoomReadinessReport",
    "ControlRoomReadinessSeverity",
    "ControlRoomReadinessStatus",
    "ControlRoomSession",
    "ControlRoomSessionDb",
    "ControlSurface",
    "ControlSurfaceDb",
    "CueAction",
    "CueFiredEvent",
    "CueFiredEventDb",
    "CuePlan",
    "CueResult",
    "DeviceCommand",
    "DeviceCommandDb",
    "DeviceProfile",
    "DeviceProfileDb",
    "DeviceTransport",
    "ProductionDevice",
    "ProductionDeviceDb",
    "ProductionDeviceKind",
    "SessionMode",
    "SessionState",
    "SurfaceRole",
    "TimelineCue",
    "TimelineCueDb",
]
