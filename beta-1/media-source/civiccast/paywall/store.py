# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable store for paywall configs + access grants + subscriptions (S26).

Per-request store over the single global session factory (same lazy posture as
eas / ai_models / metadata / reporting / underwriting / agenda). All
comparisons bind through parameters (no string interpolation) and ride the
indexes defined in migration ``0059_paywall_access``.

* ``upsert_config`` / ``get_config`` / ``get_config_for_station`` /
  ``delete_config`` — config CRUD; one config per station.
* ``upsert_grant`` / ``get_grant`` / ``list_grants_for_email`` /
  ``has_grant_for`` / ``delete_grant`` — grant CRUD + the hot-path
  "does this email have access to this scope?" lookup.
* ``upsert_subscription`` / ``get_subscription`` / ``revoke_subscription``
  — subscription CRUD + cascade-revoke of associated grants when a
  subscription is canceled (so the public gate stops allowing access in
  the same transaction the webhook commits).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import delete, or_, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from civiccast.paywall.models import (
    AccessGrant,
    AccessGrantDb,
    GrantedVia,
    PaywallConfig,
    PaywallConfigDb,
    PaywallProvider,
    PaywallTier,
    ScopeKind,
    StripeEventSeenDb,
    Subscription,
    SubscriptionDb,
    SubscriptionStatus,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]


class PaywallStoreError(RuntimeError):
    """Base error for paywall persistence failures."""


class PaywallConfigNotFoundError(PaywallStoreError):
    """Raised when a ``config_id`` does not resolve."""


class PaywallStationConfigConflictError(PaywallStoreError):
    """Raised when an upsert would create a second config for the same
    station (``ix_paywall_configs_station_unique`` collision).

    The router translates this to a 409 so two operators racing to create
    a paywall config see a controlled conflict, not a 500.
    """


class AccessGrantNotFoundError(PaywallStoreError):
    """Raised when a ``grant_id`` does not resolve."""


class SubscriptionNotFoundError(PaywallStoreError):
    """Raised when a ``sub_id`` does not resolve."""


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(dt: datetime | None) -> datetime | None:
    """E-11 fix: promote a naive datetime to UTC-aware.

    SQLite returns ``DateTime(timezone=True)`` columns as tz-naive
    datetimes even though the column is typed timezone=True. Postgres
    returns aware datetimes. This helper centralizes the
    "make-aware-as-UTC" rule so every grant/subscription scan agrees on
    semantics — and a future contributor scanning expiry doesn't have to
    re-derive the SQLite vs PG pitfall.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _tier_to_dict(tier: PaywallTier | dict[str, Any]) -> dict[str, Any]:
    """JSON column serializer. Accepts the pydantic model or a raw dict."""
    if isinstance(tier, PaywallTier):
        return tier.model_dump()
    return dict(tier)


def _config_db_to_model(row: PaywallConfigDb) -> PaywallConfig:
    """Promote an ORM row to its pydantic projection."""
    return PaywallConfig(
        config_id=row.config_id,
        station_id=row.station_id,
        enabled=row.enabled,
        provider=cast(PaywallProvider, row.provider),
        # The DB column is a JSON list of tier dicts; rehydrate to PaywallTier
        # objects. Pydantic will validate scheme + bounds on the way back in.
        tiers=[PaywallTier(**t) for t in (row.tiers or [])],
        signing_secret=row.signing_secret,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _grant_db_to_model(row: AccessGrantDb) -> AccessGrant:
    return AccessGrant(
        grant_id=row.grant_id,
        station_id=row.station_id,
        email=row.email,
        scope_kind=cast(ScopeKind, row.scope_kind),
        scope_id=row.scope_id,
        granted_via=cast(GrantedVia, row.granted_via),
        subscription_id=row.subscription_id,
        magic_link_token_id=row.magic_link_token_id,
        expires_at=row.expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _subscription_db_to_model(row: SubscriptionDb) -> Subscription:
    return Subscription(
        sub_id=row.sub_id,
        station_id=row.station_id,
        email=row.email,
        tier_id=row.tier_id,
        status=cast(SubscriptionStatus, row.status),
        current_period_end=row.current_period_end,
        grant_id=row.grant_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class PaywallStore:
    """Per-request CRUD over the three paywall tables."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # PaywallConfig
    # ------------------------------------------------------------------

    def upsert_config(
        self,
        config: PaywallConfig,
        *,
        preserve_signing_secret_if_missing: bool = False,
    ) -> PaywallConfig:
        """Insert or update a paywall config.

        Raises ``PaywallStationConfigConflictError`` if a different
        ``config_id`` already exists for ``station_id`` (the unique index
        on ``station_id`` enforces "one config per station"). When
        ``preserve_signing_secret_if_missing`` is true, an existing row's
        secret column is left untouched in this same transaction. This
        avoids a stale read in the API layer overwriting a concurrent
        rotation during an ordinary settings save.
        """
        now = _now()
        with self._session_factory() as session:
            existing = session.get(PaywallConfigDb, config.config_id)
            if existing is None:
                # Pre-check the station uniqueness so we can return a typed
                # error rather than relying on IntegrityError (which we still
                # catch as belt-and-suspenders below).
                station_row = session.execute(
                    select(PaywallConfigDb).where(PaywallConfigDb.station_id == config.station_id)
                ).scalar_one_or_none()
                if station_row is not None and station_row.config_id != config.config_id:
                    raise PaywallStationConfigConflictError(
                        f"A different paywall config already exists for station "
                        f"{config.station_id!r} (config_id={station_row.config_id!r})."
                    )
                row = PaywallConfigDb(
                    config_id=config.config_id,
                    station_id=config.station_id,
                    enabled=config.enabled,
                    provider=config.provider,
                    tiers=[_tier_to_dict(t) for t in config.tiers],
                    signing_secret=config.signing_secret,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                existing.station_id = config.station_id
                existing.enabled = config.enabled
                existing.provider = config.provider
                existing.tiers = [_tier_to_dict(t) for t in config.tiers]
                if not preserve_signing_secret_if_missing:
                    existing.signing_secret = config.signing_secret
                existing.updated_at = now
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                # The pre-check above usually catches this — the catch is a
                # safety net in case of a true race.
                raise PaywallStationConfigConflictError(
                    f"Paywall config write violated the station-unique constraint "
                    f"for {config.station_id!r}."
                ) from exc

            stored = session.get(PaywallConfigDb, config.config_id)
            assert stored is not None  # we just wrote it
            return _config_db_to_model(stored)

    def get_config(self, config_id: str) -> PaywallConfig | None:
        with self._session_factory() as session:
            row = session.get(PaywallConfigDb, config_id)
            return _config_db_to_model(row) if row else None

    def get_config_for_station(self, station_id: str) -> PaywallConfig | None:
        """Return the (single) config for a station, or None if none exists.

        Hot path for the gating check (``has_access``) — when there's no
        config, the answer is "paywall not configured → access allowed
        (DC-1: default off)".
        """
        with self._session_factory() as session:
            row = session.execute(
                select(PaywallConfigDb).where(PaywallConfigDb.station_id == station_id)
            ).scalar_one_or_none()
            return _config_db_to_model(row) if row else None

    def delete_config(self, config_id: str) -> None:
        with self._session_factory() as session:
            row = session.get(PaywallConfigDb, config_id)
            if row is None:
                raise PaywallConfigNotFoundError(f"Paywall config {config_id!r} not found.")
            session.delete(row)
            session.commit()

    # ------------------------------------------------------------------
    # AccessGrant
    # ------------------------------------------------------------------

    def upsert_grant(self, grant: AccessGrant) -> AccessGrant:
        """Insert or update a grant. No unique constraint on
        (station, email, scope) — multiple grants for the same scope are
        allowed (e.g. a comp grant plus a subscription grant); the gating
        check accepts ANY matching non-expired grant.

        Q-10 fix: ``access_grants.magic_link_token_id`` IS uniquely
        indexed (migration 0059's
        ``ix_access_grants_magic_link_token_unique``). A concurrent
        magic-link redemption that races to insert a second grant with
        the same token id raises ``IntegrityError`` — the service
        translates that to the same single-use signal as the read-side
        replay check.
        """
        now = _now()
        with self._session_factory() as session:
            existing = session.get(AccessGrantDb, grant.grant_id)
            if existing is None:
                row = AccessGrantDb(
                    grant_id=grant.grant_id,
                    station_id=grant.station_id,
                    email=grant.email,
                    scope_kind=grant.scope_kind,
                    scope_id=grant.scope_id,
                    granted_via=grant.granted_via,
                    subscription_id=grant.subscription_id,
                    magic_link_token_id=grant.magic_link_token_id,
                    expires_at=grant.expires_at,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                existing.station_id = grant.station_id
                existing.email = grant.email
                existing.scope_kind = grant.scope_kind
                existing.scope_id = grant.scope_id
                existing.granted_via = grant.granted_via
                existing.subscription_id = grant.subscription_id
                existing.magic_link_token_id = grant.magic_link_token_id
                existing.expires_at = grant.expires_at
                existing.updated_at = now
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                # Re-raise so callers (the service layer) can map this to
                # the single-use replay signal. We deliberately do NOT
                # introduce a typed wrapper here — IntegrityError is the
                # canonical SQLAlchemy signal and service.py catches it
                # by type.
                raise

            stored = session.get(AccessGrantDb, grant.grant_id)
            assert stored is not None
            return _grant_db_to_model(stored)

    def get_grant(self, grant_id: str) -> AccessGrant | None:
        with self._session_factory() as session:
            row = session.get(AccessGrantDb, grant_id)
            return _grant_db_to_model(row) if row else None

    def list_grants_for_email(
        self,
        station_id: str,
        email: str,
        *,
        include_expired: bool = True,
        now: datetime | None = None,
    ) -> list[AccessGrant]:
        """Return all grants for an email at a station, regardless of scope.

        E-10 fix: when ``include_expired=False``, push the expiry filter
        into SQL (``expires_at IS NULL OR expires_at > :now``) so callers
        do not pay for a linear Python-side scan on the hot path. Default
        is ``True`` for backward compatibility — replay-detection in
        ``verify_magic_link`` needs to see expired grants to recognize a
        replay attempt.
        """
        comparison_now = _as_utc(now) or _now()
        with self._session_factory() as session:
            stmt = (
                select(AccessGrantDb)
                .where(AccessGrantDb.station_id == station_id)
                .where(AccessGrantDb.email == email)
            )
            if not include_expired:
                stmt = stmt.where(
                    or_(
                        AccessGrantDb.expires_at.is_(None),
                        AccessGrantDb.expires_at > comparison_now,
                    )
                )
            rows = session.execute(stmt).scalars().all()
            return [_grant_db_to_model(r) for r in rows]

    def has_grant_for(
        self,
        station_id: str,
        email: str,
        scope_kind: ScopeKind,
        scope_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Hot-path gate check. Returns True if a non-expired grant exists
        for (email, scope_kind, scope_id) OR for (email, "all", ""). The
        catch-all check lets a comp grant for "all" unlock a specific asset
        without a separate row.

        E-10 fix: the expiry filter rides SQL (``expires_at IS NULL OR
        expires_at > :now``) so a user with a long history doesn't pay
        for a Python-side linear scan on every gate check.
        E-11 fix: any naive ``expires_at`` returned by SQLite is
        promoted to UTC-aware via :func:`_as_utc` for a safe compare.
        """
        comparison_now = _as_utc(now) or _now()
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(AccessGrantDb)
                    .where(AccessGrantDb.station_id == station_id)
                    .where(AccessGrantDb.email == email)
                    .where(
                        or_(
                            AccessGrantDb.expires_at.is_(None),
                            AccessGrantDb.expires_at > comparison_now,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                # Defensive backstop: even though SQL filtered, SQLite can
                # round-trip a tz-naive value the comparison-time
                # promotion missed. Re-check via :func:`_as_utc` in Python.
                if row.expires_at is not None:
                    expires = _as_utc(row.expires_at)
                    if expires is not None and expires < comparison_now:
                        continue
                if row.scope_kind == "all":
                    return True
                if row.scope_kind == scope_kind and row.scope_id == scope_id:
                    return True
            return False

    def delete_grant(self, grant_id: str) -> None:
        with self._session_factory() as session:
            row = session.get(AccessGrantDb, grant_id)
            if row is None:
                raise AccessGrantNotFoundError(f"Access grant {grant_id!r} not found.")
            session.delete(row)
            session.commit()

    def revoke_grants_for_subscription(self, sub_id: str) -> int:
        """Cascade-delete all grants tied to a canceled subscription.

        Called by the webhook handler when a Stripe subscription
        transitions to ``canceled``. Returns the number of grants
        removed (useful for the webhook's response + auditing)."""
        with self._session_factory() as session:
            result = session.execute(
                delete(AccessGrantDb).where(AccessGrantDb.subscription_id == sub_id)
            )
            session.commit()
            return int(cast(CursorResult[object], result).rowcount or 0)

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    def upsert_subscription(self, subscription: Subscription) -> Subscription:
        """Insert or update a Stripe-sourced subscription row. Called by
        the webhook handler with verified-signature payloads only."""
        now = _now()
        with self._session_factory() as session:
            existing = session.get(SubscriptionDb, subscription.sub_id)
            if existing is None:
                row = SubscriptionDb(
                    sub_id=subscription.sub_id,
                    station_id=subscription.station_id,
                    email=subscription.email,
                    tier_id=subscription.tier_id,
                    status=subscription.status,
                    current_period_end=subscription.current_period_end,
                    grant_id=subscription.grant_id,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                existing.station_id = subscription.station_id
                existing.email = subscription.email
                existing.tier_id = subscription.tier_id
                existing.status = subscription.status
                existing.current_period_end = subscription.current_period_end
                existing.grant_id = subscription.grant_id
                existing.updated_at = now
            session.commit()

            stored = session.get(SubscriptionDb, subscription.sub_id)
            assert stored is not None
            return _subscription_db_to_model(stored)

    def get_subscription(self, sub_id: str) -> Subscription | None:
        with self._session_factory() as session:
            row = session.get(SubscriptionDb, sub_id)
            return _subscription_db_to_model(row) if row else None

    # ------------------------------------------------------------------
    # Stripe event idempotency (Q-1 fix)
    # ------------------------------------------------------------------

    def record_stripe_event_seen(
        self,
        event_id: str,
        station_id: str,
        event_type: str | None,
    ) -> bool:
        """Record that we processed a Stripe webhook event.

        Returns ``True`` if this is a new event (INSERT succeeded), ``False``
        if it was already recorded (PK collision = duplicate replay).

        Q-1 fix: the row is INSERTed BEFORE any side-effect dispatch in
        :meth:`PaywallService.handle_stripe_webhook`. A duplicate signal
        short-circuits the handler and returns a 2xx duplicate-ack so
        Stripe doesn't retry.
        """
        with self._session_factory() as session:
            # Fast path: pre-check + commit. The PK gives us a true unique
            # constraint at the DB level, so we ALSO catch IntegrityError
            # as belt-and-suspenders against the genuine race window.
            existing = session.get(StripeEventSeenDb, event_id)
            if existing is not None:
                return False
            session.add(
                StripeEventSeenDb(
                    event_id=event_id,
                    station_id=station_id,
                    event_type=event_type,
                    received_at=_now(),
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return False
            return True

    # ------------------------------------------------------------------
    # Single-transaction reconcile (E-2 fix)
    # ------------------------------------------------------------------

    def reconcile_subscription_event(
        self,
        subscription: Subscription,
        grant: AccessGrant | None,
        *,
        revoke_sub_id: str | None = None,
    ) -> tuple[Subscription, AccessGrant | None, int]:
        """Upsert a Subscription + (optionally) an AccessGrant in a single
        transaction (E-2 fix).

        Before this method existed, the webhook handler upserted the
        grant and the subscription in two separate ``self._session_factory()``
        contexts; if the second crashed, the gate could report stale
        access. This method opens one session and commits once so either
        BOTH writes land or NEITHER does.

        ``revoke_sub_id`` is the subscription id whose grants should be
        cascade-deleted in the same transaction (used by the deleted
        path). Returns ``(stored_subscription, stored_grant_or_None,
        grants_revoked_count)``.
        """
        now = _now()
        with self._session_factory() as session:
            # 1. Upsert grant (if any).
            stored_grant: AccessGrant | None = None
            if grant is not None:
                grant_existing = session.get(AccessGrantDb, grant.grant_id)
                if grant_existing is None:
                    grant_row = AccessGrantDb(
                        grant_id=grant.grant_id,
                        station_id=grant.station_id,
                        email=grant.email,
                        scope_kind=grant.scope_kind,
                        scope_id=grant.scope_id,
                        granted_via=grant.granted_via,
                        subscription_id=grant.subscription_id,
                        magic_link_token_id=grant.magic_link_token_id,
                        expires_at=grant.expires_at,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(grant_row)
                else:
                    grant_existing.station_id = grant.station_id
                    grant_existing.email = grant.email
                    grant_existing.scope_kind = grant.scope_kind
                    grant_existing.scope_id = grant.scope_id
                    grant_existing.granted_via = grant.granted_via
                    grant_existing.subscription_id = grant.subscription_id
                    grant_existing.magic_link_token_id = grant.magic_link_token_id
                    grant_existing.expires_at = grant.expires_at
                    grant_existing.updated_at = now

            # 2. Upsert subscription.
            sub_existing = session.get(SubscriptionDb, subscription.sub_id)
            if sub_existing is None:
                sub_row = SubscriptionDb(
                    sub_id=subscription.sub_id,
                    station_id=subscription.station_id,
                    email=subscription.email,
                    tier_id=subscription.tier_id,
                    status=subscription.status,
                    current_period_end=subscription.current_period_end,
                    grant_id=subscription.grant_id,
                    created_at=now,
                    updated_at=now,
                )
                session.add(sub_row)
            else:
                sub_existing.station_id = subscription.station_id
                sub_existing.email = subscription.email
                sub_existing.tier_id = subscription.tier_id
                sub_existing.status = subscription.status
                sub_existing.current_period_end = subscription.current_period_end
                sub_existing.grant_id = subscription.grant_id
                sub_existing.updated_at = now

            # 3. Cascade-revoke grants for a canceled subscription, same tx.
            revoked = 0
            if revoke_sub_id is not None:
                result = session.execute(
                    delete(AccessGrantDb).where(AccessGrantDb.subscription_id == revoke_sub_id)
                )
                revoked = int(cast(CursorResult[object], result).rowcount or 0)

            session.commit()

            stored_sub_row = session.get(SubscriptionDb, subscription.sub_id)
            assert stored_sub_row is not None
            stored_sub = _subscription_db_to_model(stored_sub_row)
            if grant is not None:
                stored_grant_row = session.get(AccessGrantDb, grant.grant_id)
                if stored_grant_row is not None:
                    stored_grant = _grant_db_to_model(stored_grant_row)
            return stored_sub, stored_grant, revoked


__all__ = [
    "AccessGrantNotFoundError",
    "PaywallConfigNotFoundError",
    "PaywallStationConfigConflictError",
    "PaywallStore",
    "PaywallStoreError",
    "SessionFactory",
    "SubscriptionNotFoundError",
]
