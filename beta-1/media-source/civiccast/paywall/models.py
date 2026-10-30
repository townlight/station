# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S26 paywall pydantic + SQLAlchemy models.

Three durable entities (migration ``0059_paywall_access``):

* ``PaywallConfig`` — per-station config. ``enabled=False`` is the safe
  default (DC-1); when False, no gating logic runs at all.
* ``PaywallTier`` — embedded in ``PaywallConfig.tiers`` as a JSON list.
  Each tier maps a CivicCast tier slug to a Stripe price id.
* ``AccessGrant`` — one row per (email, scope). A grant says "this email
  has access to this asset/series/everything" and tracks how access was
  granted.
* ``Subscription`` — mirror of Stripe state. Stripe is the source of
  truth; the signed webhook updates this table.

Pydantic shapes pair with ``*Db`` SQLAlchemy twins via ``from civiccast.db
import Base``. ``station_id`` / ``scope`` are loose string columns (no
SQLAlchemy ``relationship``), matching the eas / ai_models / metadata /
reporting / underwriting / agenda convention.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base

Slug = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$")]

# Lightweight email check — accepts common shapes (user@host.tld) without
# pulling in the email-validator package. We don't need RFC-5322 strictness
# here; the public portal verifies email control via the magic-link round
# trip + the Stripe webhook, both of which are stronger signals than a
# regex.
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email(value: str) -> str:
    trimmed = (value or "").strip()
    if not _EMAIL_PATTERN.match(trimmed):
        raise ValueError(f"email must look like user@host.tld (got {value!r}).")
    return trimmed.lower()


# Public-portal scopes: a specific asset, a series, or the catch-all "all".
# The catch-all unlocks every paywalled item for the holder — comp grants
# for VIPs typically use it; subscription-based grants typically use a
# specific asset/series.
ScopeKind = Literal["asset", "series", "all"]

GrantedVia = Literal["subscription", "comp", "magic_link"]
GRANTED_VIA_VALUES: tuple[str, ...] = ("subscription", "comp", "magic_link")

SubscriptionStatus = Literal["active", "past_due", "canceled", "incomplete"]
SUBSCRIPTION_STATUS_VALUES: tuple[str, ...] = (
    "active",
    "past_due",
    "canceled",
    "incomplete",
)

PaywallProvider = Literal["stripe", "mock"]
PAYWALL_PROVIDER_VALUES: tuple[str, ...] = ("stripe", "mock")


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Pydantic domain models
# ---------------------------------------------------------------------------


class PaywallTier(BaseModel):
    """One subscription tier (mapped to a Stripe price id at config time).

    ``price_id`` is the Stripe price id (``price_...``) the station already
    created in the Stripe dashboard. We never mint prices client-side —
    the operator picks an existing one from Stripe so revenue + tax
    settings stay in the Stripe console.
    """

    model_config = ConfigDict(extra="forbid")

    tier_id: Slug
    name: Annotated[str, Field(min_length=1, max_length=200)]
    price_id: Annotated[str, Field(min_length=1, max_length=200)]
    interval: Literal["month", "year"] = "month"


class PaywallConfig(BaseModel):
    """One paywall config per station. ``enabled=False`` short-circuits all
    gating (DC-1) — the station literally has no paywall behavior unless
    the operator explicitly flips this."""

    model_config = ConfigDict(extra="forbid")

    config_id: Slug
    station_id: Slug
    enabled: bool = False
    provider: PaywallProvider = "stripe"
    tiers: list[PaywallTier] = Field(default_factory=list)
    # The per-station HMAC secret that signs magic-link tokens and verifies
    # Stripe webhook signatures. Stored as a base64-ish opaque string;
    # rotated by patching the config (slice 2 + 3 service + router enforce
    # the rotation rules). Q-11 floor: 32 chars (Stripe webhook secrets are
    # 32+ bytes of entropy; nothing shorter is operationally safe). The
    # empty-string sentinel is rejected; NULL means "no secret yet".
    signing_secret: Annotated[str | None, Field(default=None, min_length=32, max_length=200)] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class PaywallConfigPublic(BaseModel):
    """The :class:`PaywallConfig` projection safe to echo in API responses.

    Q-2 fix (S26 GauntletGate): the staff ``GET /api/staff/paywall/config``
    response NEVER includes ``signing_secret`` in plaintext. The presence
    of a secret is surfaced via ``signing_secret_present: bool`` so the
    operator UI can render a "rotate secret" affordance without learning
    the value (Q-2 + Q-3 reasoning). The secret stays write-only on
    PUT/PATCH.
    """

    model_config = ConfigDict(extra="forbid")

    config_id: Slug
    station_id: Slug
    enabled: bool = False
    provider: PaywallProvider = "stripe"
    tiers: list[PaywallTier] = Field(default_factory=list)
    signing_secret_present: bool = False
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @classmethod
    def from_config(cls, config: PaywallConfig) -> PaywallConfigPublic:
        return cls(
            config_id=config.config_id,
            station_id=config.station_id,
            enabled=config.enabled,
            provider=config.provider,
            tiers=list(config.tiers),
            signing_secret_present=bool(config.signing_secret),
            created_at=config.created_at,
            updated_at=config.updated_at,
        )


class PaywallConfigInput(BaseModel):
    """Create/replace config request body.

    On PUT, an absent or null ``signing_secret`` preserves the current
    value. The explicit empty-string sentinel clears it; a non-empty value
    rotates it and must meet the 32-character floor.
    """

    model_config = ConfigDict(extra="forbid")

    config_id: Slug
    station_id: Slug
    enabled: bool = False
    provider: PaywallProvider = "stripe"
    tiers: list[PaywallTier] = Field(default_factory=list)
    # Allow the empty-string clear sentinel here; the router normalizes it to
    # None before constructing PaywallConfig and conditionally enforces the
    # minimum for non-empty replacement values.
    signing_secret: Annotated[str | None, Field(default=None, max_length=200)] = None


class PaywallConfigUpdate(BaseModel):
    """Patch config; absent keys unchanged. ``config_id`` / ``station_id``
    set at creation and not editable here.

    Q-2 fix: ``signing_secret`` semantics are SET-ONLY. Absent leaves
    unchanged (default). An explicit empty string ``""`` clears the
    secret. ``null`` is treated as "absent" (does NOT clear) so a UI that
    naively sends every form field as null can't accidentally rotate the
    secret away. To clear, send ``""`` (the empty string).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    provider: PaywallProvider | None = None
    tiers: list[PaywallTier] | None = None
    # NOTE: explicit ``""`` clears; ``null`` is treated as "absent"
    # (does NOT clear). The router strips ``signing_secret: None`` from
    # the update payload to enforce this. min_length is enforced
    # conditionally in router (Q-11 floor only when setting a new value).
    signing_secret: Annotated[str | None, Field(default=None, max_length=200)] = None


class AccessGrant(BaseModel):
    """One grant of paywall access for an email.

    ``scope_kind`` + ``scope_id`` encode WHICH content the email has
    access to. ``"all"`` ignores ``scope_id`` (typically empty). A grant
    via ``subscription`` ties back to a ``Subscription.sub_id`` so a
    canceled Stripe sub can cascade to invalidate the grant.
    """

    model_config = ConfigDict(extra="forbid")

    grant_id: Slug
    station_id: Slug
    email: Annotated[str, Field(min_length=3, max_length=320)]
    scope_kind: ScopeKind
    # Empty string when scope_kind="all"; otherwise the asset_id / series_id slug.
    scope_id: Annotated[str, Field(default="", max_length=120)] = ""
    granted_via: GrantedVia
    # For subscription-backed grants, the Stripe sub_id. None for comp /
    # magic_link.
    subscription_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    # For magic-link grants, the (HMAC-signed) token's id portion; lets the
    # service mark a token redeemed so it cannot be replayed (DC-5).
    magic_link_token_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @field_validator("scope_id")
    @classmethod
    def _scope_id_shape(cls, value: str) -> str:
        # When the kind is "all", scope_id should be empty. We don't enforce
        # that at the field level (it depends on scope_kind which arrives in
        # the same payload), but we DO normalize whitespace.
        return value.strip()

    @field_validator("email", mode="before")
    @classmethod
    def _check_email(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("email must be a string.")
        return _validate_email(value)


class AccessGrantInput(BaseModel):
    """Create/comp request body for staff endpoints."""

    model_config = ConfigDict(extra="forbid")

    grant_id: Slug
    station_id: Slug
    email: Annotated[str, Field(min_length=3, max_length=320)]
    scope_kind: ScopeKind
    scope_id: Annotated[str, Field(default="", max_length=120)] = ""
    granted_via: GrantedVia
    subscription_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    magic_link_token_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    expires_at: datetime | None = None

    @field_validator("email", mode="before")
    @classmethod
    def _check_email(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("email must be a string.")
        return _validate_email(value)


class Subscription(BaseModel):
    """Mirror of one Stripe subscription. The signed webhook reconciles
    this row when Stripe state changes (DC-3). The Stripe ``sub_id`` is the
    primary key — we do not mint subscription ids ourselves."""

    model_config = ConfigDict(extra="forbid")

    sub_id: Annotated[str, Field(min_length=1, max_length=120)]
    station_id: Slug
    email: Annotated[str, Field(min_length=3, max_length=320)]
    tier_id: Slug
    status: SubscriptionStatus
    current_period_end: datetime
    # The grant_id of the AccessGrant this subscription gave rise to; lets
    # us cascade a canceled subscription to revoke its grant.
    grant_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @field_validator("email", mode="before")
    @classmethod
    def _check_email(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("email must be a string.")
        return _validate_email(value)


# ---------------------------------------------------------------------------
# Public projection (the public gate-check endpoint returns this shape)
# ---------------------------------------------------------------------------


class PublicAccessDecision(BaseModel):
    """Public projection of an access check.

    Drops every internal field (email, grant_id, scope, expires_at, etc.)
    and surfaces ONLY ``allowed: bool`` plus an optional ``reason`` for the
    client to display ("subscription required", "session expired", etc.).
    The viewer's email is never echoed; the server only confirms or denies
    access for the session it already knows about."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    reason: str | None = None


# ---------------------------------------------------------------------------
# SQLAlchemy ORM twins (schema applied by migration 0059, not here)
# ---------------------------------------------------------------------------


class PaywallConfigDb(Base):
    """One row per station."""

    __tablename__ = "paywall_configs"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('stripe', 'mock')",
            name="paywall_configs_provider_check",
        ),
        # One config per station so the "get by station" lookup is unambiguous.
        Index("ix_paywall_configs_station_unique", "station_id", unique=True),
    )

    config_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False, default="stripe")
    tiers: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    signing_secret: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class AccessGrantDb(Base):
    """One row per (email, scope) grant."""

    __tablename__ = "access_grants"
    __table_args__ = (
        CheckConstraint(
            "scope_kind IN ('asset', 'series', 'all')",
            name="access_grants_scope_kind_check",
        ),
        CheckConstraint(
            "granted_via IN ('subscription', 'comp', 'magic_link')",
            name="access_grants_granted_via_check",
        ),
        # The hot-path read: "does THIS email have access to THIS scope?"
        Index(
            "ix_access_grants_email_scope",
            "station_id",
            "email",
            "scope_kind",
            "scope_id",
        ),
        # For the "revoke by subscription" cascade.
        Index("ix_access_grants_subscription", "subscription_id"),
    )

    grant_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    scope_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    granted_via: Mapped[str] = mapped_column(String(16), nullable=False)
    subscription_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    magic_link_token_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class StripeEventSeenDb(Base):
    """Idempotency ledger for Stripe webhook events (Q-1 fix).

    Stripe documents at-least-once webhook delivery + a 5-minute signature
    tolerance window; both make replay a real concern. This table records
    every signed event id we've already processed so a replay returns a
    duplicate-ack without re-running side effects.

    Schema lives in migration ``0059_paywall_access`` (added in place per
    the audit directive; no chain-split migration). The primary key
    ``event_id`` is what Stripe sets on every event (``evt_...``); a unique
    insert collision IS the duplicate signal.
    """

    __tablename__ = "paywall_stripe_events_seen"

    event_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(120), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class SubscriptionDb(Base):
    """One row per Stripe subscription. ``sub_id`` is the Stripe id."""

    __tablename__ = "paywall_subscriptions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'past_due', 'canceled', 'incomplete')",
            name="paywall_subscriptions_status_check",
        ),
        Index("ix_paywall_subscriptions_station_email", "station_id", "email"),
        Index("ix_paywall_subscriptions_status", "status"),
    )

    sub_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    tier_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    grant_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


__all__ = [
    "GRANTED_VIA_VALUES",
    "PAYWALL_PROVIDER_VALUES",
    "SUBSCRIPTION_STATUS_VALUES",
    "AccessGrant",
    "AccessGrantDb",
    "AccessGrantInput",
    "GrantedVia",
    "PaywallConfig",
    "PaywallConfigDb",
    "PaywallConfigInput",
    "PaywallConfigPublic",
    "PaywallConfigUpdate",
    "PaywallProvider",
    "PaywallTier",
    "PublicAccessDecision",
    "ScopeKind",
    "Slug",
    "StripeEventSeenDb",
    "Subscription",
    "SubscriptionDb",
    "SubscriptionStatus",
]
