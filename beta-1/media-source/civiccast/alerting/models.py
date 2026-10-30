# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Data contracts for S8 operational alerting.

Pydantic models (API contracts + in-memory use) and SQLAlchemy ORM rows
(persistence). Enums are Literal type aliases, matching the existing
pattern in ``civiccast/egress/models.py``.

OD-9 resource conditions (disk-low / clock-skew / db-unreachable /
service-down) are first-class ``AlertConditionKind`` members so the rule
table is self-documenting and the dashboard can label them precisely.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
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

# ---------------------------------------------------------------------------
# Enums (Literal type aliases — consistent with egress/models.py pattern)
# ---------------------------------------------------------------------------

AlertSeverity = Literal["critical", "warning", "info"]

AlertConditionKind = Literal[
    # S8 self-derived from egress state + shutdown marker
    "off-air",
    "encoder-death",
    "server-crash",
    # S9 — schema_version drift + relay stuck in backoff
    "schema-drift",
    "relay-blocked",
    # S2 — TSDuck TR 101 290 priority-1 drift on a cable sink
    "compliance-probe-fail",
    # S7 — scheduled item's media absent < 5 min before air
    "missing-media",
    # S4 — commit-to-air validation/dispatch failed
    "commit-failure",
    # S5 — live takeover held > 2h without handback
    "takeover-stuck-2h",
    # S13 — Ollama / model runtime unreachable
    "ai-runtime-down",
    # S8 resource conditions (OD-9: first-class for dashboard labelling)
    "disk-low",
    "clock-skew",
    "db-unreachable",
    "service-down",
    # S8-5 self-test: a daily/weekly self-test failed a check (§6.6, warning by default)
    "self-test-fail",
    # S17 remote contribution: VDO.Ninja/coturn co-process down, TURN unreachable
    # (guests behind NAT can't connect), and an on-air guest dropped.
    "remote-contribution-coprocess-down",
    "remote-contribution-turn-unreachable",
    "remote-contribution-guest-drop",
    # S11c public-safety: a CAP/IPAWS/NWS/AMBER source poll is failing (fetch/parse).
    # CivicCast can't display alerts it can't ingest — surface the source as down.
    "eas-source-unavailable",
    # S21 scheduled recording: capture/finalize failed for a scheduled job.
    "scheduled-recording-failure",
    # Item 6 (recording/ingest hardening): a mid-recording source dropout was
    # detected. Distinct from -failure: the job may still be recording (a
    # reconnect succeeded) — this kind exists so the dashboard doesn't label
    # a recovered dropout as a hard failure.
    "scheduled-recording-dropout",
    # BUG C2 fix (S23 §6.1 durable outbox): the as-run ledger's local durable
    # journal (civiccast.reporting.asrun_outbox) failed to drain a batch to
    # the franchise-compliance DB. The event stays journaled (nothing is
    # lost — see the outbox module docstring), but a persistent drain
    # failure needs an operator to know a DB problem is blocking the legal
    # as-aired record from becoming durable. Unseeded (no migration 0039
    # default-rule row), matching every condition kind added after that
    # migration — the operator raises its severity from the "warning"
    # fallback in Alert Settings if a station wants it to page.
    "asrun-outbox-degraded",
    # DEFECT C (found live alongside DEFECT A): a channel-automation poll
    # pass — or one queued command inside it — raised, and the ONLY trace
    # was a log line ("Channel automation pass failed for %s") no operator
    # would ever see. civiccast.egress.automation.ChannelAutomationService
    # raises this (resource_ref=f"egress-channel:{channel_id}") on a failed
    # pass or command, and resolves it on the channel's next clean pass —
    # same fire/dedupe/auto-clear contract as every other condition here.
    # Unseeded, same posture as asrun-outbox-degraded above.
    "channel-automation-failure",
    # Field finding (2026-08-30, PR #80 follow-up): a station whose optional
    # large caption tier was orphaned by an uninstall/reinstall upgrade
    # degrades to the proven floor tier and starts -- but the ONLY trace was
    # a supervisor-process log line no operator would ever see. The control
    # plane raises this at startup from the CIVICCAST_CAPTION_TIER_EVENT
    # environment (station_runtime's fallback record) and resolves it once a
    # start comes up on the requested tier again. Unseeded, same "warning"
    # fallback posture as asrun-outbox-degraded above.
    "caption-tier-degraded",
    # An enabled on-air channel whose live-caption worker has failed or stalled.
    "live-caption-failure",
]

AlertChannelKind = Literal["email", "sms", "webhook"]
AlertDeliveryStatus = Literal["sent", "failed", "suppressed", "dead_letter"]
AlertEventState = Literal["firing", "resolved"]
SelfTestKind = Literal["daily", "weekly"]
SelfTestStatus = Literal["pass", "warn", "fail"]
SafeToAirColor = Literal["green", "yellow", "red"]
CaptionProcessingState = Literal[
    "disabled",
    "inactive",
    "waiting",
    "processing",
    "delayed",
    "overloaded",
    "caught-up",
    "silent",
    "stalled",
    "failed",
    "unknown",
]
CaptionAudioSignal = Literal["digital-silence", "audio-present", "unknown"]
CaptionProviderState = Literal[
    "whistle-primary",
    "whisper-primary",
    "whisper-fallback",
    "whistle-recovering",
    "whistle-recovery-cooldown",
    "fallback-cooldown",
    "fallback-retry-ready",
    "unknown",
]

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class AlertRule(BaseModel):
    """Operator-tunable policy mapping one condition to severity + channels.

    Seeded with §6.2 defaults; operator can tune severity, channels, dedupe
    windows, and quiet-hours scope. Secrets are never in this contract — they
    live in the credential store referenced by AlertChannel.credential_handle.
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: Annotated[str, Field(min_length=1, max_length=120)]
    condition: AlertConditionKind
    enabled: bool = True
    severity: AlertSeverity
    channel_ids: Annotated[list[str], Field(min_length=0)] = Field(default_factory=list)
    dedupe_window_seconds: Annotated[int, Field(ge=0, le=86_400)] = 900
    re_alert_after_seconds: Annotated[int, Field(ge=0, le=604_800)] = 3600
    # None = all channels; non-None = limit to one egress channel_id's conditions
    scope_channel_id: Annotated[str | None, Field(default=None, max_length=80)] = None
    notify_on_resolve: bool = True
    updated_at: datetime
    updated_by: Annotated[str, Field(min_length=1, max_length=120)]


class AlertChannel(BaseModel):
    """A push-destination for operator alerts.

    Secrets (SMTP password, SMS API key, webhook secret) are NOT stored
    here — they are kept in the same local credential store the installer
    uses (``credential_handle`` is the key). Reads return ``target_redacted``
    (e.g. last-4 of a phone number, masked email domain) — never raw PII.
    """

    model_config = ConfigDict(extra="forbid")

    # Note: ``channel_id`` is the alert-channel ID, NOT an egress channel_id.
    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    kind: AlertChannelKind
    label: Annotated[str, Field(min_length=1, max_length=120)]
    enabled: bool = True
    target_redacted: Annotated[str, Field(min_length=1, max_length=200)]
    credential_handle: Annotated[str | None, Field(default=None, max_length=200)] = None
    # critical alerts always send; warning/info held during the quiet window (OD-2)
    quiet_hours_start_utc: Annotated[str | None, Field(default=None, max_length=5)] = None
    quiet_hours_end_utc: Annotated[str | None, Field(default=None, max_length=5)] = None
    last_delivery_status: AlertDeliveryStatus | None = None
    last_delivery_at: datetime | None = None
    created_at: datetime

    @field_validator("quiet_hours_start_utc", "quiet_hours_end_utc", mode="before")
    @classmethod
    def _quiet_hours_format(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) != 5 or value[2] != ":" or not value[:2].isdigit() or not value[3:].isdigit():
            raise ValueError("quiet_hours must be HH:MM format (e.g. '22:00')")
        h, m = int(value[:2]), int(value[3:])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("quiet_hours hours must be 0-23, minutes 0-59")
        return value


class AlertEvent(BaseModel):
    """One fired alert for a (rule, resource) pair — append-only lifecycle.

    ``dedupe_key = f"{condition}:{resource_ref}"`` is the unit of
    notify-on-first-failure. A single ``AlertEvent`` per dedupe_key stays
    open (state="firing") while the condition persists; ``occurrence_count``
    absorbs suppressed repeats.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: Annotated[str, Field(min_length=1, max_length=120)]
    rule_id: Annotated[str, Field(min_length=0, max_length=120)]
    condition: AlertConditionKind
    severity: AlertSeverity
    state: AlertEventState
    resource_ref: Annotated[str, Field(min_length=1, max_length=200)]
    summary: Annotated[str, Field(min_length=1, max_length=300)]
    detail: Annotated[str, Field(default="", max_length=2000)] = ""
    source_section: Annotated[str, Field(min_length=2, max_length=8)]
    first_observed_at: datetime
    last_observed_at: datetime
    resolved_at: datetime | None = None
    occurrence_count: Annotated[int, Field(ge=1)] = 1
    acknowledged_at: datetime | None = None
    acknowledged_by: Annotated[str | None, Field(default=None, max_length=120)] = None


class AlertEventDelivery(BaseModel):
    """Proof that S8 attempted to notify the operator via one channel.

    Mirrors ``subscribe/``'s ``NotificationDelivery`` structure — delivery
    is attempted, retry is bounded, dead-letter surfaces as a dashboard
    warning. The actual claim is "delivery was attempted", never "received".
    """

    model_config = ConfigDict(extra="forbid")

    delivery_id: Annotated[str, Field(min_length=1, max_length=120)]
    event_id: Annotated[str, Field(min_length=1, max_length=120)]
    alert_channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    kind: AlertChannelKind
    status: AlertDeliveryStatus
    attempts: Annotated[int, Field(ge=0)] = 0
    next_attempt_at: datetime | None = None
    last_error: Annotated[str, Field(default="", max_length=1000)] = ""
    signature: Annotated[str | None, Field(default=None, max_length=200)] = None
    dispatched_at: datetime


class SystemResourceSample(BaseModel):
    """Host metrics snapshot for the System Health dashboard.

    Fields that cannot be sampled on the current platform (e.g. GPU on a
    headless box) are None. An appliance vendor hides these in firmware;
    CivicCast owns them for a commodity PC installation.
    """

    model_config = ConfigDict(extra="forbid")

    sample_id: Annotated[int | None, Field(default=None, ge=1)] = None
    sampled_at: datetime
    cpu_percent: Annotated[float | None, Field(default=None, ge=0, le=100)] = None
    ram_used_gb: Annotated[float | None, Field(default=None, ge=0)] = None
    ram_total_gb: Annotated[float | None, Field(default=None, ge=0)] = None
    gpu_percent: Annotated[float | None, Field(default=None, ge=0, le=100)] = None
    gpu_vram_used_gb: Annotated[float | None, Field(default=None, ge=0)] = None
    media_volume_free_gb: Annotated[float | None, Field(default=None, ge=0)] = None
    backup_volume_free_gb: Annotated[float | None, Field(default=None, ge=0)] = None
    db_reachable: bool = True
    backup_volume_writable: bool = True
    service_running: bool = True
    clock_skew_seconds: float | None = None


class SystemSelfTest(BaseModel):
    """Result of a scheduled or on-demand system self-test.

    Daily: install-time readiness path + FileSink continuity proof +
    backup write/read/delete probe + model-runtime ping.
    Weekly: daily set + restore rehearsal + SRT continuity proof +
    TSDuck compliance probe + alert-channel test send.
    """

    model_config = ConfigDict(extra="forbid")

    self_test_id: Annotated[str, Field(min_length=1, max_length=120)]
    kind: SelfTestKind
    started_at: datetime
    finished_at: datetime | None = None
    status: SelfTestStatus
    checks: dict[str, bool] = Field(default_factory=dict)
    summary: Annotated[str, Field(min_length=1, max_length=600)]
    evidence_path: Annotated[str | None, Field(default=None, max_length=500)] = None


class ChannelCaptionProcessingStatus(BaseModel):
    """Staff-safe view of worker progress, input level, and selected ASR path."""

    model_config = ConfigDict(extra="forbid")

    processing_state: CaptionProcessingState = "unknown"
    worker_heartbeat_at: datetime | None = None
    last_input_at: datetime | None = None
    last_processed_at: datetime | None = None
    audio_signal: CaptionAudioSignal = "unknown"
    provider_state: CaptionProviderState = "unknown"
    provider_retry_in_seconds: Annotated[int | None, Field(ge=0)] = None
    backlog_segments: Annotated[int, Field(ge=0)] = 0


class ChannelRuntimeStatus(BaseModel):
    """Per-channel runtime snapshot feeding the dashboard + safe-to-air computation."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=80)]
    egress_state: str  # EgressState — imported lazily to avoid circular; validated at use
    sink_health: dict[str, bool] = Field(default_factory=dict)
    on_air: bool
    on_healthy_slate: bool
    encoder_fps: float | None = None
    encoder_bitrate_kbps: float | None = None
    last_loudness_lufs: float | None = None
    seconds_in_state: Annotated[int, Field(ge=0)] = 0
    last_proof_event_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    # Whether the operator's live-captions switch is ON (captions are a
    # readiness requirement and gate ``color``) or OFF (no embed leg is built,
    # the proof cannot PASS, and the caption gate is not applied). Lets the
    # dashboard say "off by operator" instead of "not confirmed".
    captions_expected: bool = True
    # The latest health sample's decode-back proof read ``on``.
    captions_verified: bool = False
    # Live recognition-worker observations are separate from the egress
    # decode-back proof above. They report input/worker progress, not speech
    # completeness or caption latency.
    live_captions: ChannelCaptionProcessingStatus | None = None
    color: SafeToAirColor


class RuntimeSafeToAirStatus(BaseModel):
    """Continuous runtime safe-to-air signal; cached ~5s for dashboard polling.

    Distinct from the install-time ``SafeToBroadcastContract`` (a pre-meeting
    readiness gate). This answers "is the box on-air and healthy right now?"
    every few seconds. Green only if EVERY auto_start channel that should be
    ON_AIR is ON_AIR (or cleanly on healthy slate) AND no critical alert fires.
    """

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    color: SafeToAirColor
    label: Annotated[str, Field(min_length=1, max_length=80)]
    operator_message: Annotated[str, Field(min_length=1)]
    channels: list[ChannelRuntimeStatus] = Field(default_factory=list)
    active_critical_alerts: Annotated[int, Field(ge=0)] = 0
    active_warning_alerts: Annotated[int, Field(ge=0)] = 0


# ---------------------------------------------------------------------------
# SQLAlchemy ORM models
# ---------------------------------------------------------------------------


class AlertRuleDb(Base):
    """Durable alert-rule row — one per condition, operator-tunable."""

    __tablename__ = "alert_rules"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('critical', 'warning', 'info')",
            name="alert_rules_severity_check",
        ),
    )

    rule_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    condition: Mapped[str] = mapped_column(String(40), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    channel_ids_json: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'[]'"))
    dedupe_window_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("900")
    )
    re_alert_after_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3600")
    )
    scope_channel_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    notify_on_resolve: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(120), nullable=False)


class AlertChannelDb(Base):
    """Durable alert-channel destination row — no secret values stored."""

    __tablename__ = "alert_channels"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('email', 'sms', 'webhook')",
            name="alert_channels_kind_check",
        ),
    )

    channel_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    target_redacted: Mapped[str] = mapped_column(String(200), nullable=False)
    credential_handle: Mapped[str | None] = mapped_column(String(200), nullable=True)
    quiet_hours_start_utc: Mapped[str | None] = mapped_column(String(5), nullable=True)
    quiet_hours_end_utc: Mapped[str | None] = mapped_column(String(5), nullable=True)
    last_delivery_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_delivery_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AlertEventDb(Base):
    """Append-only alert event row — one per dedupe_key per firing instance."""

    __tablename__ = "alert_events"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('critical', 'warning', 'info')",
            name="alert_events_severity_check",
        ),
        CheckConstraint(
            "state IN ('firing', 'resolved')",
            name="alert_events_state_check",
        ),
        # Dedupe lookup: find firing event for (condition, resource_ref) per tick.
        Index("ix_alert_events_dedupe", "condition", "resource_ref", "state"),
    )

    event_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    rule_id: Mapped[str] = mapped_column(String(120), nullable=False)
    condition: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(String(300), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    source_section: Mapped[str] = mapped_column(String(8), nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(120), nullable=True)


class AlertEventDeliveryDb(Base):
    """Delivery attempt record for one alert via one channel — proof of attempt."""

    __tablename__ = "alert_event_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('sent', 'failed', 'suppressed', 'dead_letter')",
            name="alert_event_deliveries_status_check",
        ),
        Index("ix_alert_event_deliveries_event_id", "event_id"),
    )

    delivery_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(120), nullable=False)
    alert_channel_id: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    signature: Mapped[str | None] = mapped_column(String(200), nullable=True)
    dispatched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SystemResourceSampleDb(Base):
    """Host-metric snapshot row — written every ~60s by the resource sampler."""

    __tablename__ = "system_resource_samples"
    __table_args__ = (Index("ix_system_resource_samples_sampled_at", "sampled_at"),)

    sample_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cpu_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    ram_used_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    ram_total_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    gpu_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    gpu_vram_used_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    media_volume_free_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    backup_volume_free_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    db_reachable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    backup_volume_writable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    service_running: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    clock_skew_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)


class SystemSelfTestDb(Base):
    """Scheduled or on-demand self-test result row."""

    __tablename__ = "system_self_tests"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('daily', 'weekly')",
            name="system_self_tests_kind_check",
        ),
        CheckConstraint(
            "status IN ('pass', 'warn', 'fail')",
            name="system_self_tests_status_check",
        ),
        Index("ix_system_self_tests_started_at", "started_at"),
    )

    self_test_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(8), nullable=False)
    checks_json: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'{}'"))
    summary: Mapped[str] = mapped_column(String(600), nullable=False)
    evidence_path: Mapped[str | None] = mapped_column(String(500), nullable=True)


# ---------------------------------------------------------------------------
# ORM ↔ pydantic conversion helpers
# ---------------------------------------------------------------------------


def alert_rule_from_db(row: AlertRuleDb) -> AlertRule:
    return AlertRule(
        rule_id=row.rule_id,
        condition=row.condition,  # type: ignore[arg-type]
        enabled=row.enabled,
        severity=row.severity,  # type: ignore[arg-type]
        channel_ids=json.loads(row.channel_ids_json),
        dedupe_window_seconds=row.dedupe_window_seconds,
        re_alert_after_seconds=row.re_alert_after_seconds,
        scope_channel_id=row.scope_channel_id,
        notify_on_resolve=row.notify_on_resolve,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


def alert_channel_from_db(row: AlertChannelDb) -> AlertChannel:
    return AlertChannel(
        channel_id=row.channel_id,
        kind=row.kind,  # type: ignore[arg-type]
        label=row.label,
        enabled=row.enabled,
        target_redacted=row.target_redacted,
        credential_handle=row.credential_handle,
        quiet_hours_start_utc=row.quiet_hours_start_utc,
        quiet_hours_end_utc=row.quiet_hours_end_utc,
        last_delivery_status=row.last_delivery_status,  # type: ignore[arg-type]
        last_delivery_at=row.last_delivery_at,
        created_at=row.created_at,
    )


def alert_event_from_db(row: AlertEventDb) -> AlertEvent:
    return AlertEvent(
        event_id=row.event_id,
        rule_id=row.rule_id,
        condition=row.condition,  # type: ignore[arg-type]
        severity=row.severity,  # type: ignore[arg-type]
        state=row.state,  # type: ignore[arg-type]
        resource_ref=row.resource_ref,
        summary=row.summary,
        detail=row.detail,
        source_section=row.source_section,
        first_observed_at=row.first_observed_at,
        last_observed_at=row.last_observed_at,
        resolved_at=row.resolved_at,
        occurrence_count=row.occurrence_count,
        acknowledged_at=row.acknowledged_at,
        acknowledged_by=row.acknowledged_by,
    )


def alert_event_delivery_from_db(row: AlertEventDeliveryDb) -> AlertEventDelivery:
    return AlertEventDelivery(
        delivery_id=row.delivery_id,
        event_id=row.event_id,
        alert_channel_id=row.alert_channel_id,
        kind=row.kind,  # type: ignore[arg-type]
        status=row.status,  # type: ignore[arg-type]
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        last_error=row.last_error,
        signature=row.signature,
        dispatched_at=row.dispatched_at,
    )


def system_resource_sample_from_db(row: SystemResourceSampleDb) -> SystemResourceSample:
    return SystemResourceSample(
        sample_id=row.sample_id,
        sampled_at=row.sampled_at,
        cpu_percent=row.cpu_percent,
        ram_used_gb=row.ram_used_gb,
        ram_total_gb=row.ram_total_gb,
        gpu_percent=row.gpu_percent,
        gpu_vram_used_gb=row.gpu_vram_used_gb,
        media_volume_free_gb=row.media_volume_free_gb,
        backup_volume_free_gb=row.backup_volume_free_gb,
        db_reachable=row.db_reachable,
        backup_volume_writable=row.backup_volume_writable,
        service_running=row.service_running,
        clock_skew_seconds=row.clock_skew_seconds,
    )


def system_self_test_from_db(row: SystemSelfTestDb) -> SystemSelfTest:
    return SystemSelfTest(
        self_test_id=row.self_test_id,
        kind=row.kind,  # type: ignore[arg-type]
        started_at=row.started_at,
        finished_at=row.finished_at,
        status=row.status,  # type: ignore[arg-type]
        checks=json.loads(row.checks_json),
        summary=row.summary,
        evidence_path=row.evidence_path,
    )
