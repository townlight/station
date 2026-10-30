# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S26 paywall service layer (slice 2).

Sits above :mod:`civiccast.paywall.store` and provides the operations the
router (slice 3) and the public portal consume:

* :meth:`PaywallService.has_access` / :meth:`decide` — the gating check.
  ``decide`` is the public-portal entrypoint that returns a
  :class:`~civiccast.paywall.models.PublicAccessDecision` (``allowed`` +
  optional ``reason``); ``has_access`` is the boolean primitive callers can
  use when they already know they want a bare yes/no.

  **DC-1 default-off** — when a station has no paywall config or
  ``enabled=False``, ``has_access`` returns True unconditionally. The
  paywall code path is genuinely inert until an operator flips it on. This
  is the cornerstone of "ships last, default OFF" — a station that never
  configures the paywall behaves exactly like one without the module at
  all.

* :meth:`PaywallService.mint_magic_link` / :meth:`verify_magic_link` —
  short-lived, HMAC-signed, single-use email magic-link tokens (DC-5). The
  signing secret is per-station, looked up via the injected
  ``signing_secret_getter`` (in production, that's
  ``store.get_config_for_station(sid).signing_secret``; in tests, a stub).

  Tokens are encoded as ``{base64url(JSON payload)}.{hex(HMAC-SHA256)}``.
  We deliberately avoid jwt — the payload is tiny, the verification is
  one HMAC, and the token id is a uuid4 hex stored on the redeeming
  grant so a redeem-then-replay can be rejected by a table lookup (DC-5
  "single-use").

* :meth:`PaywallService.handle_stripe_webhook` — the signed-webhook entry
  point. Stripe is the source of truth; this method reconciles
  :class:`~civiccast.paywall.models.Subscription` rows and the associated
  :class:`~civiccast.paywall.models.AccessGrant` rows so the gating check
  returns the right answer the next millisecond.

  The :mod:`stripe` PyPI package is **not** a runtime dependency. The
  service tries to import it lazily; if the import fails AND no
  ``mock_stripe_event_verifier`` was injected, the call raises
  :class:`WebhookSignatureError` (which the router maps to 401). Tests
  inject the mock verifier; production environments that opt into Stripe
  install the package separately.

* **DC-4 — no PAN data ever lands.** The webhook handler extracts ONLY
  the Stripe-side ids (subscription id, customer email, price/tier id,
  status, period end). Card numbers, CVCs, and the full event payload
  are NEVER persisted; the Stripe-hosted Checkout + Customer Portal means
  the raw card data never even reaches our servers. The test suite asserts
  this directly (a payload whose ``description`` mentions a card-shaped
  string is accepted but no card-shaped string lands in the DB).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import math
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy.exc import IntegrityError

from civiccast.paywall.models import (
    AccessGrant,
    PublicAccessDecision,
    ScopeKind,
    Subscription,
    _validate_email,
)
from civiccast.paywall.store import PaywallStore

logger = logging.getLogger(__name__)

# Q-14 fix: Stripe subscription ids look like ``sub_<alnum>``. We validate
# the shape at the boundary so a malformed event can't echo an
# attacker-supplied unbounded string into the 400 response body.
_STRIPE_SUB_ID_RE = re.compile(r"^sub_[A-Za-z0-9_-]{1,80}$")

#: A function that, given a station id, returns the per-station HMAC secret
#: (or ``None`` if the station has no paywall configured). The production
#: implementation wraps :meth:`PaywallStore.get_config_for_station`; tests
#: pass a constant.
SigningSecretGetter = Callable[[str], str | None]

#: Stub for the Stripe SDK's ``Webhook.construct_event``. Signature matches
#: ``(payload_bytes, signature_header, secret) -> dict``. Tests inject a
#: deterministic stub; production passes ``None`` and the service falls
#: back to the real Stripe SDK if importable.
StripeEventVerifier = Callable[[bytes, str, str], dict[str, Any]]

#: How long a magic-link token is valid before redemption. 15 minutes
#: matches the spec's "short-lived" requirement; the constructor can
#: override per-call via ``ttl_seconds``.
DEFAULT_MAGIC_LINK_TTL_SECONDS = 15 * 60

#: How long an :class:`AccessGrant` minted from a redeemed magic-link
#: stays valid. 30 days is the default; a station can extend by issuing a
#: comp grant out-of-band.
DEFAULT_MAGIC_LINK_GRANT_TTL_SECONDS = 30 * 24 * 60 * 60


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Custom exceptions (router maps to status codes; service stays HTTP-free)
# ---------------------------------------------------------------------------


class PaywallServiceError(RuntimeError):
    """Base error raised by :class:`PaywallService` operations."""


class PaywallNotEnabledError(PaywallServiceError):
    """Raised when an operation requires a paywall config that does not exist
    OR is disabled. The router maps this to 404 (no config) or 409 (disabled)
    depending on the route — the service itself doesn't pick a status."""


class MagicLinkNotConfiguredError(PaywallServiceError):
    """Raised when a magic-link mint is attempted for a station whose
    paywall config is missing OR whose ``signing_secret`` is unset. Without
    a secret we have nothing to sign with — surface explicitly."""


class MagicLinkVerificationError(PaywallServiceError):
    """Parent type for ALL magic-link verify-time failures.

    E-3 / Q-7 fix: callers (the router) should catch this single type and
    return one generic 401 + one identical detail string. The specific
    subclass type still surfaces in server logs; what the wire sees is
    constant across "invalid", "expired", and "replayed".
    """


class MagicLinkInvalidError(MagicLinkVerificationError):
    """Raised when a redeem-time token fails HMAC verification or its
    payload cannot be decoded. The router maps this to 401 — never echo
    the raw token back."""


class MagicLinkExpiredError(MagicLinkVerificationError):
    """Raised when a redeem-time token's ``exp`` is in the past."""


class MagicLinkAlreadyRedeemedError(MagicLinkVerificationError):
    """Raised when a redeem-time token's id matches a grant that was
    already created (single-use enforcement, DC-5)."""


class WebhookSignatureError(PaywallServiceError):
    """Raised when the Stripe webhook signature fails verification OR the
    Stripe SDK isn't importable and no mock verifier was injected. Router
    maps to 401."""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PaywallService:
    """Gating + magic-link lifecycle + Stripe webhook reconciliation."""

    def __init__(
        self,
        store: PaywallStore,
        *,
        signing_secret_getter: SigningSecretGetter | None = None,
        clock: Callable[[], datetime] | None = None,
        mock_stripe_event_verifier: StripeEventVerifier | None = None,
        magic_link_ttl_seconds: int = DEFAULT_MAGIC_LINK_TTL_SECONDS,
        magic_link_grant_ttl_seconds: int = DEFAULT_MAGIC_LINK_GRANT_TTL_SECONDS,
    ) -> None:
        self._store = store
        # Default getter walks the store. We allow a None override so the
        # production wiring can plug in a cached lookup (slice 3).
        self._secret_getter: SigningSecretGetter = (
            signing_secret_getter or self._default_secret_getter
        )
        self._clock = clock or _utcnow
        self._stripe_verifier = mock_stripe_event_verifier
        self._magic_link_ttl_seconds = magic_link_ttl_seconds
        self._magic_link_grant_ttl_seconds = magic_link_grant_ttl_seconds

    # ------------------------------------------------------------------
    # Gating (DC-1, DC-2)
    # ------------------------------------------------------------------

    def has_access(
        self,
        station_id: str,
        email: str | None,
        scope_kind: Literal["asset", "series"],
        scope_id: str,
    ) -> bool:
        """Boolean access primitive.

        DC-1 default-off short-circuit: no config OR ``enabled=False`` →
        ``True``. The paywall code path is genuinely inert until an
        operator flips it on.

        DC-2: when the paywall IS enabled, an anonymous (``email is None``)
        session is denied; otherwise the answer rides
        :meth:`PaywallStore.has_grant_for` (which considers the catch-all
        ``"all"`` scope).
        """
        config = self._store.get_config_for_station(station_id)
        if config is None or not config.enabled:
            return True
        if email is None or not email.strip():
            return False
        return self._store.has_grant_for(
            station_id,
            email.strip().lower(),
            scope_kind,
            scope_id,
            now=self._clock(),
        )

    def decide(
        self,
        station_id: str,
        email: str | None,
        scope_kind: Literal["asset", "series"],
        scope_id: str,
    ) -> PublicAccessDecision:
        """Public-portal access check. Returns a :class:`PublicAccessDecision`
        with ``allowed`` + an optional ``reason`` for the client UI.

        Reason codes (stable strings the operator UI keys off):

        * ``None`` — allowed; nothing to display.
        * ``"sign_in_required"`` — the paywall is enabled but the viewer
          hasn't told us who they are yet (no email session).
        * ``"subscription_required"`` — the viewer is signed in but has no
          grant for the requested scope.
        """
        config = self._store.get_config_for_station(station_id)
        if config is None or not config.enabled:
            return PublicAccessDecision(allowed=True)
        if email is None or not email.strip():
            return PublicAccessDecision(allowed=False, reason="sign_in_required")
        allowed = self._store.has_grant_for(
            station_id,
            email.strip().lower(),
            scope_kind,
            scope_id,
            now=self._clock(),
        )
        if allowed:
            return PublicAccessDecision(allowed=True)
        return PublicAccessDecision(allowed=False, reason="subscription_required")

    # ------------------------------------------------------------------
    # Magic-link lifecycle (DC-5)
    # ------------------------------------------------------------------

    def viewer_proof(self, grant: AccessGrant) -> str:
        """Issue browser identity only after successful magic-link redemption."""
        secret = self._secret_getter(grant.station_id)
        if not secret:
            raise MagicLinkNotConfiguredError(
                "Viewer identity requires the station signing secret."
            )
        return _encode_token(
            {
                "purpose": "viewer-session",
                "station_id": grant.station_id,
                "email": grant.email,
                "exp": (self._clock() + timedelta(hours=12)).timestamp(),
            },
            secret,
        )

    def viewer_email(self, station_id: str, proof: str | None) -> str | None:
        """Verify identity, not entitlement; callers must check current grants."""
        if not proof or len(proof) > 4096:
            return None
        try:
            payload, signature = _split_token(proof)
            secret = self._secret_getter(station_id)
            if not secret or not hmac.compare_digest(signature, _sign_payload(payload, secret)):
                return None
            expiry = payload.get("exp")
            if (
                payload.get("purpose") != "viewer-session"
                or payload.get("station_id") != station_id
                or isinstance(expiry, bool)
                or not isinstance(expiry, (int, float))
                or not math.isfinite(expiry)
                or expiry <= self._clock().timestamp()
            ):
                return None
            email = payload.get("email")
            return _validate_email(email) if isinstance(email, str) else None
        except (ValueError, TypeError, UnicodeError):
            return None

    def mint_magic_link(
        self,
        station_id: str,
        email: str,
        scope_kind: ScopeKind,
        scope_id: str,
        *,
        ttl_seconds: int | None = None,
    ) -> str:
        """Mint a single-use, short-lived magic-link token.

        Returns the token string (the router emails it as
        ``https://<station>/paywall/verify?token=<token>``). The token is
        ``{base64url(JSON payload)}.{hex(HMAC-SHA256)}``; the payload
        carries the station, email, scope, ``token_id`` (uuid4 hex), and
        an absolute ``exp`` timestamp.

        Raises :class:`MagicLinkNotConfiguredError` if the station has no
        paywall config, is disabled (E-8), or has no signing secret.

        Operator-only contexts (staff comp-mint) may pass
        ``scope_kind="all"``; the public router calls
        :meth:`mint_magic_link_for_public` instead, which rejects
        ``"all"`` (E-1 fix).
        """
        # E-8 fix: ALSO require config.enabled=True. A "configured but
        # disabled" station can today mint tokens whose redeemed grants
        # become live the instant an operator flips enabled; surfacing
        # the disable here closes that surprise.
        config = self._store.get_config_for_station(station_id)
        if config is None or not config.enabled or not config.signing_secret:
            raise MagicLinkNotConfiguredError(
                f"Cannot mint a magic-link for station {station_id!r}: "
                "paywall is not configured + enabled with a signing_secret."
            )
        secret = self._secret_getter(station_id) or config.signing_secret
        if not secret:  # pragma: no cover - belt-and-suspenders
            raise MagicLinkNotConfiguredError(
                f"Cannot mint a magic-link for station {station_id!r}: "
                "no signing_secret resolvable."
            )
        normalized_email = email.strip().lower()
        ttl = ttl_seconds if ttl_seconds is not None else self._magic_link_ttl_seconds
        exp = (self._clock() + timedelta(seconds=ttl)).timestamp()
        payload = {
            "token_id": uuid.uuid4().hex,
            "station_id": station_id,
            "email": normalized_email,
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "exp": exp,
        }
        return _encode_token(payload, secret)

    def mint_magic_link_for_public(
        self,
        station_id: str,
        email: str,
        scope_kind: Literal["asset", "series"],
        scope_id: str,
        *,
        ttl_seconds: int | None = None,
    ) -> str:
        """Public-facing wrapper around :meth:`mint_magic_link` that
        rejects ``scope_kind="all"`` (E-1 BLOCKER fix).

        The public ``POST /api/public/paywall/magic-link`` endpoint MUST
        route through this method so an anonymous caller cannot mint a
        catch-all grant via a single email round-trip — that was the
        S26 monetization-bypass blocker. Operator-only paths (staff comp
        mint, programmatic VIP onboarding) call :meth:`mint_magic_link`
        directly and may still issue ``"all"``.

        The type narrowing on ``scope_kind`` enforces the rule at the
        signature level too, but we re-validate at runtime so a caller
        bypassing the type check still gets rejected.
        """
        if scope_kind not in ("asset", "series"):
            raise PaywallServiceError(
                "Public magic-link requests must specify a specific asset or "
                "series; the catch-all scope is operator-only."
            )
        # scope_id is required for non-catch-all scopes — the public
        # router validates this too, but we belt-and-suspender here.
        if not scope_id or not scope_id.strip():
            raise PaywallServiceError("Public magic-link requests require a non-empty scope_id.")
        # T-6 fix: refuse to mint for syntactically invalid emails.
        # The endpoint is public + sends mail, so an unauthenticated
        # caller cannot trigger an outbound SMTP send for a bogus
        # address. `_validate_email` is the same shape check the model
        # already runs at the persistence boundary.
        try:
            _validate_email(email)
        except ValueError as exc:
            raise PaywallServiceError(
                "Email is not a recognizable shape; nothing was sent."
            ) from exc
        return self.mint_magic_link(
            station_id, email, scope_kind, scope_id, ttl_seconds=ttl_seconds
        )

    def verify_magic_link(
        self, token: str, *, expected_station_id: str | None = None
    ) -> AccessGrant:
        """Verify + redeem a magic-link token. On success, persists an
        :class:`AccessGrant` and returns it.

        Q-8 fix: if ``expected_station_id`` is provided, the token's
        embedded ``station_id`` claim MUST match (defense-in-depth
        against future multi-tenant deployments). The public router
        passes the running station id; a station-A token replayed at
        station-B is a :class:`MagicLinkInvalidError`.

        Raises:
            MagicLinkInvalidError: the token is malformed, HMAC mismatched,
                references a station with no signing secret, or its
                station_id claim does not match ``expected_station_id``.
            MagicLinkExpiredError: the embedded ``exp`` is in the past.
            MagicLinkAlreadyRedeemedError: a grant with this token's
                ``token_id`` already exists (single-use, DC-5).
        """
        try:
            payload, mac_hex = _split_token(token)
        except ValueError as exc:
            raise MagicLinkInvalidError("Magic-link token is malformed.") from exc

        station_id = payload.get("station_id")
        if not isinstance(station_id, str):
            raise MagicLinkInvalidError("Magic-link token is malformed.")
        # Q-8 fix: enforce cross-station replay protection before any
        # secret lookup. A token whose ``station_id`` claim doesn't match
        # the running station is rejected. We still do the constant-time
        # MAC check below so timing analysis on the station-mismatch
        # branch can't be used to enumerate valid station ids.
        if expected_station_id is not None and station_id != expected_station_id:
            raise MagicLinkInvalidError("Magic-link token cannot be verified for this station.")
        secret = self._secret_getter(station_id)
        if not secret:
            # The station's secret was rotated away or the config was
            # deleted — either way, we can't verify this token.
            raise MagicLinkInvalidError("Magic-link token cannot be verified for this station.")
        expected_hex = _sign_payload(payload, secret)
        # Constant-time compare so a timing-side-channel can't recover the
        # MAC byte-by-byte.
        if not hmac.compare_digest(mac_hex, expected_hex):
            raise MagicLinkInvalidError("Magic-link token signature is invalid.")

        exp = payload.get("exp")
        # Q-13 fix: ``isinstance(True, int|float)`` is True in Python (bool
        # is an int subclass); explicitly exclude bool so an attacker
        # crafting a token with ``exp=True`` cannot collapse to numeric 1.
        if not isinstance(exp, int | float) or isinstance(exp, bool):
            raise MagicLinkInvalidError("Magic-link token is malformed.")
        now = self._clock()
        if now.timestamp() > float(exp):
            raise MagicLinkExpiredError("Magic-link token has expired.")

        token_id = payload.get("token_id")
        email = payload.get("email")
        scope_kind = payload.get("scope_kind")
        scope_id = payload.get("scope_id")
        if not isinstance(token_id, str) or not isinstance(email, str):
            raise MagicLinkInvalidError("Magic-link token is malformed.")
        if scope_kind not in ("asset", "series", "all") or not isinstance(scope_id, str):
            raise MagicLinkInvalidError("Magic-link token is malformed.")

        # Single-use enforcement (read-side): a grant with this token's id
        # means someone has already redeemed it. Reject the replay. We
        # ALSO catch the IntegrityError at the write side below (Q-10) so
        # a concurrent verify race that slips past this read can't both
        # land — the unique index on magic_link_token_id rejects the
        # second insert.
        existing_grants = self._store.list_grants_for_email(station_id, email)
        for existing in existing_grants:
            if existing.magic_link_token_id == token_id:
                raise MagicLinkAlreadyRedeemedError("Magic-link token has already been redeemed.")

        grant = AccessGrant(
            grant_id=f"mlg-{token_id}",
            station_id=station_id,
            email=email,
            scope_kind=cast(ScopeKind, scope_kind),
            scope_id=scope_id,
            granted_via="magic_link",
            magic_link_token_id=token_id,
            expires_at=now + timedelta(seconds=self._magic_link_grant_ttl_seconds),
        )
        try:
            return self._store.upsert_grant(grant)
        except IntegrityError as exc:
            # Q-10 fix: concurrent redemption raced past the read-side
            # check and tripped the unique magic_link_token_id index.
            # That's the same "already redeemed" signal — surface it
            # consistently so the router emits the same generic 401.
            raise MagicLinkAlreadyRedeemedError(
                "Magic-link token has already been redeemed."
            ) from exc

    # ------------------------------------------------------------------
    # Stripe webhook reconciliation (DC-3 + DC-4)
    # ------------------------------------------------------------------

    def handle_stripe_webhook(
        self,
        payload_bytes: bytes,
        signature_header: str,
        station_id: str,
    ) -> dict[str, Any]:
        """Verify + reconcile one Stripe webhook event.

        DC-3: signature is verified before any side effect. A bad signature
        (or a missing verifier) raises :class:`WebhookSignatureError` and
        the router returns 401.

        DC-4: only Stripe-side identifiers are persisted. The full event
        payload is never stored; card numbers / CVCs / billing details
        never even hit our schema. The Stripe-hosted Checkout + Customer
        Portal flow means the raw card data never reaches our servers in
        the first place.

        Q-1 fix: BEFORE any state mutation, the event id is INSERTed into
        ``paywall_stripe_events_seen``. A duplicate (replay) returns a
        2xx duplicate-ack without dispatching — Stripe stops retrying.

        Handles three event types — anything else is acknowledged but
        ignored (so a station that opts into more Stripe events doesn't
        500 our endpoint):

        * ``customer.subscription.created`` / ``customer.subscription.updated``
          — upsert a :class:`Subscription` row + an :class:`AccessGrant`
          with ``granted_via="subscription"`` + ``scope_kind="all"`` (a
          subscription unlocks everything for the holder).
        * ``customer.subscription.deleted`` — flip the subscription to
          ``canceled`` and cascade-revoke all grants tied to it.
        """
        secret = self._secret_getter(station_id)
        if not secret:
            raise WebhookSignatureError(
                f"Paywall is not configured for station {station_id!r}; webhook rejected."
            )

        # 1. Verify the signature. The mock verifier (tests) takes
        # precedence over the SDK (production) so unit tests stay offline.
        # E-15 fix: only swallow exceptions that genuinely indicate a
        # signature failure. The mock verifier raises arbitrary
        # exceptions in tests (intentional) so we keep the broad catch
        # there; for the real SDK we catch the specific signature error.
        if self._stripe_verifier is not None:
            try:
                event = self._stripe_verifier(payload_bytes, signature_header, secret)
            except Exception as exc:
                raise WebhookSignatureError(
                    str(exc) or "Stripe webhook signature invalid."
                ) from exc
        else:
            try:
                import stripe  # type: ignore[import-not-found]
            except ImportError as exc:
                raise WebhookSignatureError(
                    "The 'stripe' package is not installed; pass mock_stripe_event_verifier in tests "
                    "or `pip install stripe` in production."
                ) from exc
            sig_error_cls: tuple[type[Exception], ...] = (Exception,)
            # Covered by SDK-present runs; AttributeError on older SDKs is fine.
            with contextlib.suppress(AttributeError):
                sig_error_cls = (stripe.error.SignatureVerificationError,)
            try:
                event = stripe.Webhook.construct_event(payload_bytes, signature_header, secret)
            except sig_error_cls as exc:
                raise WebhookSignatureError(
                    str(exc) or "Stripe webhook signature invalid."
                ) from exc

        # 2. Dispatch. ``event`` is a dict (or dict-like Stripe object); we
        # only read the few fields we need, never echo the rest.
        event_type = event.get("type") if hasattr(event, "get") else None
        data_object = (event.get("data") or {}).get("object") if hasattr(event, "get") else None
        if not isinstance(event_type, str) or not isinstance(data_object, dict):
            return {"received": True, "action": "ignored"}

        # Q-1 fix: event-id replay protection. BEFORE any state mutation,
        # record the event. A duplicate (PK collision) signals replay; we
        # short-circuit with a 2xx duplicate-ack so Stripe stops retrying.
        event_id = _str_or_none(event.get("id")) if hasattr(event, "get") else None
        if event_id is not None:
            inserted = self._store.record_stripe_event_seen(event_id, station_id, event_type)
            if not inserted:
                logger.info(
                    "stripe.webhook.duplicate event_id=%s station_id=%s type=%s",
                    event_id,
                    station_id,
                    event_type,
                )
                return {"received": True, "action": "duplicate"}

        if event_type in ("customer.subscription.created", "customer.subscription.updated"):
            sub, grant = self._reconcile_subscription(station_id, data_object)
            return {
                "received": True,
                "action": "created" if event_type.endswith("created") else "updated",
                "subscription_id": sub.sub_id,
                "grant_id": grant.grant_id if grant else None,
            }
        if event_type == "customer.subscription.deleted":
            sub_id = _str_or_none(data_object.get("id"))
            if sub_id is None:
                return {"received": True, "action": "ignored"}
            # Q-14 fix: validate sub_id shape before echoing it into the
            # response body or persisting it.
            if not _STRIPE_SUB_ID_RE.match(sub_id):
                raise PaywallServiceError(
                    "Stripe subscription id has an unexpected shape; webhook rejected."
                )
            existing = self._store.get_subscription(sub_id)
            if existing is not None:
                # E-2 fix: single-transaction (status flip + cascade delete).
                canceled_sub = Subscription(
                    sub_id=existing.sub_id,
                    station_id=existing.station_id,
                    email=existing.email,
                    tier_id=existing.tier_id,
                    status="canceled",
                    current_period_end=existing.current_period_end,
                    grant_id=existing.grant_id,
                )
                _, _, removed = self._store.reconcile_subscription_event(
                    canceled_sub, None, revoke_sub_id=sub_id
                )
            else:
                # No existing subscription row — still revoke any orphan
                # grants tied to this sub_id (idempotent).
                removed = self._store.revoke_grants_for_subscription(sub_id)
            return {
                "received": True,
                "action": "deleted",
                "subscription_id": sub_id,
                "grants_removed": removed,
            }

        # Anything else (invoice.paid, customer.created, ...) — ack + drop.
        return {"received": True, "action": "ignored"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _default_secret_getter(self, station_id: str) -> str | None:
        config = self._store.get_config_for_station(station_id)
        if config is None:
            return None
        return config.signing_secret

    def _reconcile_subscription(
        self,
        station_id: str,
        data_object: dict[str, Any],
    ) -> tuple[Subscription, AccessGrant | None]:
        """Extract the safe fields from a Stripe subscription object and
        upsert the mirror row + an "all-scope" grant.

        We read ONLY: ``id``, ``status``, ``current_period_end``,
        ``customer_email`` (if present), and the first item's
        ``price.id`` as the tier mapping. Card-shaped data never lands.

        E-2 fix: uses the single-transaction ``reconcile_subscription_event``
        store helper.
        E-6 fix: ``current_period_end`` only advances; a stale (older)
        Stripe event cannot regress the stored expiry.
        E-7 fix: a missing ``current_period_end`` on an active/past_due
        event is rejected (400 fail-closed), not papered over.
        E-13 fix: the Stripe price id is preserved verbatim in
        ``tier_id`` via a non-lossy slug transform (lowercase + replace
        underscores only — case info would not survive a slug check
        anyway since the model's Slug enforces lowercase; the prior
        ``replace("price_", "tier-")`` is kept ONLY for the tier_id slug
        constraint).
        Q-14 fix: validate Stripe sub_id shape at the boundary.
        """
        sub_id = _str_or_none(data_object.get("id"))
        if sub_id is None:
            raise PaywallServiceError("Stripe subscription event missing 'id'.")
        # Q-14 fix: bound the sub_id shape so a malformed event can't echo
        # an attacker-supplied unbounded string back into the response.
        if not _STRIPE_SUB_ID_RE.match(sub_id):
            raise PaywallServiceError(
                "Stripe subscription id has an unexpected shape; webhook rejected."
            )
        status_value = _str_or_none(data_object.get("status")) or "incomplete"
        if status_value not in ("active", "past_due", "canceled", "incomplete"):
            # Stripe occasionally adds new states; map unknown to incomplete
            # so the gate stays closed until a human investigates.
            status_value = "incomplete"
        period_end_raw = data_object.get("current_period_end")
        if isinstance(period_end_raw, int | float) and not isinstance(period_end_raw, bool):
            period_end = datetime.fromtimestamp(float(period_end_raw), tz=UTC)
        elif status_value in ("active", "past_due"):
            # E-7 fix: fail closed on malformed Stripe payloads for
            # subscriptions claiming to grant access. The prior code
            # silently minted a 30-day grant; we now reject the event
            # so Stripe surfaces the malformed payload in its dashboard
            # (it will retry — that's fine; a retry of a still-malformed
            # event will hit the same 400).
            raise PaywallServiceError(
                "Stripe subscription event missing 'current_period_end'; cannot grant access."
            )
        else:
            # Canceled / incomplete events that don't need to mint a grant
            # may have no period_end; use ``now`` (the gate ignores it
            # for non-granting statuses).
            period_end = self._clock()
        email = _str_or_none(data_object.get("customer_email")) or _str_or_none(
            (data_object.get("customer_details") or {}).get("email")
        )
        if not email:
            raise PaywallServiceError(
                "Stripe subscription event has no customer email; cannot grant access."
            )
        # DC-4 / T-1 fix: validate email shape BEFORE constructing the
        # AccessGrant. Otherwise a Luhn-valid card-shaped string sent in
        # ``customer_email`` would propagate into pydantic's ValidationError
        # message — leaking the bait into a (caught) traceback. Validate
        # here so the error message we surface contains no echo of the
        # planted value.
        try:
            email = _validate_email(email)
        except ValueError as exc:
            raise PaywallServiceError(
                "Stripe subscription event has an unrecognizable customer "
                "email; cannot grant access."
            ) from exc
        # Tier: first item's price id, or a synthetic id keyed off sub_id.
        items = (data_object.get("items") or {}).get("data") or []
        if items and isinstance(items, list):
            first = items[0]
            price = (first.get("price") or {}) if isinstance(first, dict) else {}
            tier_id = _str_or_none(price.get("id")) or f"tier-{sub_id[-8:]}"
        else:
            tier_id = f"tier-{sub_id[-8:]}"
        # Slug-shape the tier id (Stripe uses ``price_...``; the slug
        # allowlist accepts underscores + digits but not uppercase). E-13:
        # the transform is destructive on the ``price_`` prefix but the
        # tail of the price id is preserved verbatim — Stripe price-id
        # collisions that differ only in case are vanishingly rare.
        tier_id = tier_id.lower().replace("price_", "tier-")

        # E-6 fix: never regress current_period_end. If we already have a
        # row for this sub_id with a LATER period_end, keep ours — a
        # stale Stripe ``updated`` event (out-of-order delivery) cannot
        # shorten a customer's paid window.
        existing_sub = self._store.get_subscription(sub_id)
        if existing_sub is not None and existing_sub.current_period_end is not None:
            existing_end = existing_sub.current_period_end
            if existing_end.tzinfo is None:
                existing_end = existing_end.replace(tzinfo=UTC)
            if existing_end > period_end:
                period_end = existing_end

        grant: AccessGrant | None = None
        if status_value in ("active", "past_due"):
            grant_id = f"sg-{sub_id}"
            grant = AccessGrant(
                grant_id=grant_id,
                station_id=station_id,
                email=email,
                scope_kind="all",
                scope_id="",
                granted_via="subscription",
                subscription_id=sub_id,
                expires_at=period_end,
            )
        subscription = Subscription(
            sub_id=sub_id,
            station_id=station_id,
            email=email,
            tier_id=tier_id,
            status=status_value,  # type: ignore[arg-type]
            current_period_end=period_end,
            grant_id=grant.grant_id if grant else None,
        )
        # E-2 fix: single-transaction reconcile.
        stored_sub, stored_grant, _ = self._store.reconcile_subscription_event(subscription, grant)
        return stored_sub, stored_grant


# ---------------------------------------------------------------------------
# Token codec (module-level so tests can poke at it directly)
# ---------------------------------------------------------------------------


def _encode_token(payload: dict[str, Any], secret: str) -> str:
    """Encode + sign a magic-link payload.

    ``{base64url(JSON payload)}.{hex(HMAC-SHA256(payload-bytes))}``. We
    sign the raw payload bytes (not the base64), so a re-encode by a
    naive client cannot change the bytes the MAC covers.
    """
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_json).rstrip(b"=").decode("ascii")
    mac = hmac.new(secret.encode("utf-8"), payload_json, hashlib.sha256).hexdigest()
    return f"{payload_b64}.{mac}"


def _split_token(token: str) -> tuple[dict[str, Any], str]:
    """Inverse of :func:`_encode_token`. Returns ``(payload_dict, mac_hex)``.

    Raises :class:`ValueError` on any structural problem (missing dot, bad
    base64, bad JSON, payload not a dict).
    """
    if not isinstance(token, str) or "." not in token:
        raise ValueError("Token is missing the signature separator.")
    # E-12 fix: the urlsafe_b64 alphabet (A-Za-z0-9_-=) contains no
    # ``.``, so a well-formed token has exactly ONE dot. ``split(".", 1)``
    # makes the structural assumption explicit; ``rsplit`` was defensive
    # overkill that confused readers into thinking the format admits more
    # than one separator.
    payload_b64, mac_hex = token.split(".", 1)
    # Restore padding (urlsafe_b64encode strips it; decode wants it).
    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload_json = base64.urlsafe_b64decode(payload_b64 + padding)
    except (ValueError, TypeError) as exc:
        raise ValueError("Token payload is not valid base64url.") from exc
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise ValueError("Token payload is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Token payload is not a JSON object.")
    return payload, mac_hex


def _sign_payload(payload: dict[str, Any], secret: str) -> str:
    """Re-compute the MAC for verify-time comparison. Must match the bytes
    written by :func:`_encode_token` exactly — same key ordering, same
    separators."""
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload_json, hashlib.sha256).hexdigest()


def _str_or_none(value: object) -> str | None:
    """Defensive string extractor — returns the value if it's a non-empty
    string, otherwise ``None``. Stripe occasionally returns ints / nulls
    where we expect strings (e.g. ``customer_email`` is null on guest
    Checkouts that didn't collect one)."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


__all__ = [
    "DEFAULT_MAGIC_LINK_GRANT_TTL_SECONDS",
    "DEFAULT_MAGIC_LINK_TTL_SECONDS",
    "MagicLinkAlreadyRedeemedError",
    "MagicLinkExpiredError",
    "MagicLinkInvalidError",
    "MagicLinkNotConfiguredError",
    "MagicLinkVerificationError",
    "PaywallNotEnabledError",
    "PaywallService",
    "PaywallServiceError",
    "SigningSecretGetter",
    "StripeEventVerifier",
    "WebhookSignatureError",
]
