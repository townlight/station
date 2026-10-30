# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""EAS (public-safety) ingest + display models (S11c).

Three durable entities on the single global Alembic chain (migration ``0051``):

* ``EasCapSource`` — a configured CAP feed (IPAWS-OPEN COG endpoint, NWS api.weather.gov,
  a state AMBER feed, or operator-entered manual alerts) with a geocode + severity filter.
* ``EasCapAlert`` — a normalized CAP 1.2 alert. Deduped on ``(sender, identifier)``;
  supersession is tracked via the CAP ``references`` field (an Update/Cancel supersedes
  the alerts it references).
* ``EasDisplayDecision`` — the resolved on-channel display action for an alert on a
  channel. Carries the mandatory ``eas_claim="not_eas"`` stamp (master §7 honesty line);
  the operator confirms a forced slate — CivicCast never auto-preempts.

Pydantic domain models pair with ``*Db`` SQLAlchemy ORM twins (no ``schema=`` in
``__table_args__``; the migration applies the schema). Literal enums are enforced in the
DB by ``CheckConstraint``s created in the migration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
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

# --- shared literals ---------------------------------------------------------

# Where alerts come from. ``manual`` = an operator-entered alert (no live feed).
EasSourceKind = Literal["ipaws-cap", "nws-cap", "amber-cap", "manual"]

# CAP 1.2 severity (Common Alerting Protocol §3.2.1). ``unknown`` sorts lowest.
EasSeverity = Literal["unknown", "minor", "moderate", "severe", "extreme"]

# CAP message type — an Update/Cancel can supersede earlier alerts via ``references``.
EasMsgType = Literal["alert", "update", "cancel", "ack", "error"]

# A normalized alert's lifecycle in CivicCast.
EasAlertStatus = Literal["active", "superseded", "expired", "cancelled"]

# How an alert is shown on a channel. ``forced_slate`` ALWAYS needs an operator
# confirmation (decision 3 — no auto forced-takeover); crawl/overlay can auto-surface.
EasDisplayMode = Literal["crawl", "overlay", "forced_slate"]

# A display decision's lifecycle.
EasDisplayState = Literal["pending", "displayed", "cleared", "expired"]

# Severity ordering for the per-source ``severity_floor`` filter (higher = worse).
SEVERITY_RANK: dict[str, int] = {
    "unknown": 0,
    "minor": 1,
    "moderate": 2,
    "severe": 3,
    "extreme": 4,
}


def severity_at_or_above(severity: str, floor: str) -> bool:
    """True if ``severity`` meets or exceeds ``floor`` (unknown never meets a real floor)."""
    return SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK.get(floor, 0)


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Pydantic domain models
# ---------------------------------------------------------------------------


class EasCapSource(BaseModel):
    """A configured public-safety alert feed CivicCast polls."""

    model_config = ConfigDict(extra="forbid")

    source_id: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    kind: EasSourceKind
    # The feed endpoint. IPAWS-OPEN is a COG-credentialed URL the operator supplies;
    # NWS = an api.weather.gov alerts URL; AMBER = a state CAP feed. ``manual`` needs none.
    endpoint_url: Annotated[str | None, Field(default=None, max_length=1000)] = None
    # SAME/FIPS or NWS UGC codes this source is scoped to (empty = no geo filter).
    geocode_filter: list[str] = Field(default_factory=list)
    severity_floor: EasSeverity = "severe"
    poll_seconds: Annotated[int, Field(ge=15, le=3600)] = 60
    enabled: bool = True
    # IPAWS COG credentials are an opaque keyring handle, never cleartext (FEMA gates
    # live IPAWS behind COG credentialing — a federal access fact, not a corner cut).
    credential_ref: Annotated[str | None, Field(default=None, max_length=200)] = None
    notes: Annotated[str | None, Field(default=None, max_length=2000)] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class EasCapAlert(BaseModel):
    """A normalized CAP 1.2 alert. Deduped on ``(sender, identifier)``."""

    model_config = ConfigDict(extra="forbid")

    alert_id: Annotated[str, Field(min_length=1, max_length=160)]
    source_id: Annotated[str, Field(min_length=1, max_length=120)]
    sender: Annotated[str, Field(min_length=1, max_length=255)]
    identifier: Annotated[str, Field(min_length=1, max_length=255)]
    sent: datetime
    msg_type: EasMsgType
    status: EasAlertStatus = "active"
    event: Annotated[str, Field(min_length=1, max_length=255)]
    severity: EasSeverity
    urgency: Annotated[str, Field(default="unknown", max_length=40)] = "unknown"
    certainty: Annotated[str, Field(default="unknown", max_length=40)] = "unknown"
    headline: Annotated[str | None, Field(default=None, max_length=500)] = None
    description: Annotated[str | None, Field(default=None)] = None
    instruction: Annotated[str | None, Field(default=None)] = None
    areas: list[str] = Field(default_factory=list)
    # CAP ``references`` (space-separated sender,identifier,sent triples) for supersession.
    references: Annotated[str | None, Field(default=None, max_length=4000)] = None
    effective: datetime | None = None
    onset: datetime | None = None
    expires: datetime | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @property
    def dedup_key(self) -> tuple[str, str]:
        return (self.sender, self.identifier)


class EasDisplayDecision(BaseModel):
    """The resolved on-channel display action for an alert. NEVER claims EAS."""

    model_config = ConfigDict(extra="forbid")

    decision_id: Annotated[str, Field(min_length=1, max_length=160)]
    alert_id: Annotated[str, Field(min_length=1, max_length=160)]
    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    mode: EasDisplayMode
    state: EasDisplayState = "pending"
    # "auto" for an auto-surfaced crawl/overlay, else the operator id. A forced_slate
    # is never "auto" (decision 3 — full-screen pre-emption always needs a human).
    decided_by: Annotated[str, Field(min_length=1, max_length=120)]
    auto_surfaced: bool = False
    # The CG overlay id used on the existing render path (build_cg_overlay_egress_proof).
    overlay_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    # Hard honesty stamp: this is on-channel public-safety information, NOT EAS.
    eas_claim: Literal["not_eas"] = "not_eas"
    reason: Annotated[str | None, Field(default=None, max_length=500)] = None
    displayed_at: datetime | None = None
    cleared_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# SQLAlchemy ORM twins (schema applied by the migration, not here)
# ---------------------------------------------------------------------------


class EasCapSourceDb(Base):
    """Durable CAP source row."""

    __tablename__ = "eas_cap_sources"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('ipaws-cap', 'nws-cap', 'amber-cap', 'manual')",
            name="eas_cap_sources_kind_check",
        ),
        CheckConstraint(
            "severity_floor IN ('unknown', 'minor', 'moderate', 'severe', 'extreme')",
            name="eas_cap_sources_severity_floor_check",
        ),
    )

    source_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    endpoint_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    geocode_filter: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    severity_floor: Mapped[str] = mapped_column(String(16), nullable=False, default="severe")
    poll_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    credential_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class EasCapAlertDb(Base):
    """Durable normalized CAP alert row. Unique on ``(sender, identifier)`` (dedup)."""

    __tablename__ = "eas_cap_alerts"
    __table_args__ = (
        UniqueConstraint("sender", "identifier", name="eas_cap_alerts_dedup_key"),
        CheckConstraint(
            "msg_type IN ('alert', 'update', 'cancel', 'ack', 'error')",
            name="eas_cap_alerts_msg_type_check",
        ),
        CheckConstraint(
            "status IN ('active', 'superseded', 'expired', 'cancelled')",
            name="eas_cap_alerts_status_check",
        ),
        CheckConstraint(
            "severity IN ('unknown', 'minor', 'moderate', 'severe', 'extreme')",
            name="eas_cap_alerts_severity_check",
        ),
        Index("ix_eas_cap_alerts_status_expires", "status", "expires"),
        Index("ix_eas_cap_alerts_source", "source_id"),
    )

    alert_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(120), nullable=False)
    sender: Mapped[str] = mapped_column(String(255), nullable=False)
    identifier: Mapped[str] = mapped_column(String(255), nullable=False)
    sent: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    msg_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    event: Mapped[str] = mapped_column(String(255), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    urgency: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    certainty: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    headline: Mapped[str | None] = mapped_column(String(500), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    areas: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    references: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    onset: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class EasDisplayDecisionDb(Base):
    """Durable display-decision row (carries the not_eas honesty stamp)."""

    __tablename__ = "eas_display_decisions"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('crawl', 'overlay', 'forced_slate')",
            name="eas_display_decisions_mode_check",
        ),
        CheckConstraint(
            "state IN ('pending', 'displayed', 'cleared', 'expired')",
            name="eas_display_decisions_state_check",
        ),
        CheckConstraint(
            "eas_claim = 'not_eas'",
            name="eas_display_decisions_not_eas_check",
        ),
        Index("ix_eas_display_decisions_channel_state", "channel_id", "state"),
        Index("ix_eas_display_decisions_alert", "alert_id"),
    )

    decision_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    alert_id: Mapped[str] = mapped_column(String(160), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(120), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    decided_by: Mapped[str] = mapped_column(String(120), nullable=False)
    auto_surfaced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    overlay_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    eas_claim: Mapped[str] = mapped_column(String(16), nullable=False, default="not_eas")
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    displayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


def _as_dict(value: Any) -> list[str]:
    return list(value) if value else []
