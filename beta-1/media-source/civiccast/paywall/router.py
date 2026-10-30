# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S26 paywall API surface (slice 3).

Three surface families over the ``PaywallStore`` + ``PaywallService`` DI
seams:

* **Staff CRUD** (``/api/staff/paywall/config[/{config_id}]``,
  ``/api/staff/paywall/grants[/{grant_id}]``) — ``setup_admin`` is the
  spec §4 config role. Comp grants are setup-admin issued (no separate
  "comp grant operator" role); the spec mentions "comp-access grants" in
  §5 alongside the config UI.
* **Public** (``/api/public/paywall/access``,
  ``/api/public/paywall/magic-link``, ``/api/public/paywall/verify``) —
  no auth; rate-limited at the deployment layer. The access endpoint
  takes ``asset_id`` + ``email`` query params and returns the
  :class:`~civiccast.paywall.models.PublicAccessDecision` projection.
* **Webhook** (``/api/webhooks/stripe``) — raw bytes in, signed by
  Stripe, verified by ``PaywallService.handle_stripe_webhook``. A bad
  signature is a 401, never a silent 200 — Stripe will retry on 401s
  but never on 200, so we'd lose a legitimate event if we 200'd a
  rejected one.

DI seams (``get_paywall_store`` / ``get_paywall_service``) return
``None`` at import so the module opens no database; the app factory
overrides them in ``_wire_durable_stores``. An unwired surface is a
503 — never a silent 200 against storage that is not there.
``x-required-roles`` is mirrored into the generated OpenAPI so the
published contract cannot drift from the runtime role gate (same
convention as S23 / S24 / S25).
"""

from __future__ import annotations

import functools
import ipaddress
import logging
import os
import time
from collections import deque
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from civiccast.auth.roles import require_any_role
from civiccast.paywall.models import (
    AccessGrant,
    AccessGrantInput,
    PaywallConfig,
    PaywallConfigInput,
    PaywallConfigPublic,
    PaywallConfigUpdate,
    PublicAccessDecision,
)
from civiccast.paywall.service import (
    MagicLinkNotConfiguredError,
    MagicLinkVerificationError,
    PaywallService,
    PaywallServiceError,
    WebhookSignatureError,
)
from civiccast.paywall.store import (
    AccessGrantNotFoundError,
    PaywallConfigNotFoundError,
    PaywallStationConfigConflictError,
    PaywallStore,
)
from civiccast.platform.providers import PROVIDER_KIND_MAIL, default_registry
from civiccast.subscribe.smtp import SmtpMailbox

logger = logging.getLogger(__name__)

# Q-4 fix: cap the Stripe webhook body at 1 MiB. Real Stripe events top
# out around ~256 KiB; nothing legitimate hits this cap. The cap forecloses
# the cheapest-to-mount DoS (PUT a gigabyte body, signature verifier rejects
# it AFTER materializing the full body in memory).
_WEBHOOK_MAX_CONTENT_LENGTH = 1 * 1024 * 1024  # 1 MiB

# Q-5 fix: in-process token-bucket rate limit on public paywall routes.
# This is a best-effort process-local floor; a production deployment
# should also rate-limit at the edge proxy / CDN layer (documented at the
# module level). The buckets are per (route, client IP), expressed as a
# deque of recent request timestamps; new requests are admitted while the
# count within the window is below the limit.
_RATE_LIMIT_LOCK_NOTE = (
    "Best-effort process-local rate limit. The production deployment "
    "must ALSO enforce limits at the edge (proxy / CDN); a multi-worker "
    "app instance does not coordinate across processes."
)

# (route -> (limit, window_seconds, dict[ip -> deque[timestamps]]))
_RATE_LIMITS: dict[str, tuple[int, float, dict[str, deque[float]]]] = {
    "access": (60, 60.0, {}),  # 60 req / min / IP — generous for legit polling
    "magic_link": (5, 60.0, {}),  # 5 req / min / IP — tight; this sends email
    "verify": (10, 60.0, {}),  # 10 req / min / IP — most verifies are one-shot
}


def _enforce_rate_limit(route_key: str, request: Request) -> None:
    """Token-bucket rate limit. See ``_RATE_LIMIT_LOCK_NOTE``.

    Raises HTTP 429 when the per-IP request rate exceeds the limit. The
    bucket window slides — only requests within the last ``window`` seconds
    count against the limit. The IP is resolved through
    :func:`civiccast.common.trusted_proxy.resolve_client_ip`, which walks
    the ``X-Forwarded-For`` chain when the immediate peer is a configured
    trusted proxy (CDN edge or operator-declared LB CIDR). Behind
    BunnyCDN / Cloudflare with ``CIVICCAST_CDN_PROVIDER`` set, this
    automatically uses the published edge CIDR list so the rate limit
    keys on the real client rather than the CDN edge.
    """
    from civiccast.common.trusted_proxy import resolve_client_ip

    limit, window, buckets = _RATE_LIMITS[route_key]
    ip = resolve_client_ip(request)
    now = time.monotonic()
    bucket = buckets.setdefault(ip, deque())
    # Drop entries outside the window.
    while bucket and bucket[0] < now - window:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(f"Too many requests; rate limit is {limit} per {int(window)}s. Retry shortly."),
        )
    bucket.append(now)


def _enforce_webhook_body_size(request: Request) -> None:
    """Q-4 fix: reject Stripe webhook bodies above
    :data:`_WEBHOOK_MAX_CONTENT_LENGTH` via the Content-Length header
    BEFORE the body materializes into memory."""
    raw_length = request.headers.get("content-length")
    if raw_length is None:
        # Most Stripe deliveries send Content-Length. If absent, fall
        # back to reading the body (the verifier needs raw bytes anyway);
        # the read-side cap below catches it.
        return
    try:
        content_length = int(raw_length)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid content length.",
        ) from None
    if content_length > _WEBHOOK_MAX_CONTENT_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Stripe webhook payload exceeds 1 MiB cap.",
        )


_DB_NOT_READY = "Durable storage is not ready yet."

# Spec §4 — staff config role. ``setup_admin`` is the only scope that
# configures the paywall / issues comp grants.
_CONFIG = ("setup_admin",)
_CONFIG_EXTRA = {"x-required-roles": list(_CONFIG)}

_DEFAULT_STATION_ID = "civiccast-station"


@functools.cache
def _station_id() -> str:
    """The active station id (single-station deployment; env-overridable).

    Same cached pattern as :mod:`civiccast.agenda.router._station_id` —
    re-reading ``os.environ`` on every public read was a hot-path nit on
    the agenda audit (Q-5). Tests that need a different station id should
    set the env BEFORE importing this module or call
    ``_station_id.cache_clear()`` in a fixture."""
    return os.environ.get("CIVICCAST_STATION_ID") or _DEFAULT_STATION_ID


def _empty_config_public() -> PaywallConfigPublic:
    now = datetime.now(UTC)
    return PaywallConfigPublic(
        config_id="paywall-default",
        station_id=_station_id(),
        enabled=False,
        provider="stripe",
        tiers=[],
        signing_secret_present=False,
        created_at=now,
        updated_at=now,
    )


# --- DI seams (overridden by the app factory) --------------------------------


def get_paywall_store() -> PaywallStore | None:
    """DI seam — overridden by ``_wire_durable_stores`` in the app factory."""
    return None


def get_paywall_service() -> PaywallService | None:
    """DI seam — overridden by ``_wire_durable_stores`` in the app factory."""
    return None


def _require_store(store: PaywallStore | None) -> PaywallStore:
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return store


def _require_service(svc: PaywallService | None) -> PaywallService:
    if svc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return svc


# --- magic-link email-send hook (B5 SMTP pattern) ----------------------------


class MagicLinkEmailSender(Protocol):
    """Hook for delivering the magic-link to the viewer's inbox.

    Tests may inject a recording stub. Production resolves the existing real
    SMTP provider and refuses to report delivery when it is unavailable.
    """

    def __call__(
        self,
        *,
        email: str,
        token: str,
        station_id: str,
        scope_kind: str,
        scope_id: str,
    ) -> None: ...


def _default_email_sender(
    *,
    email: str,
    token: str,
    station_id: str,
    scope_kind: str,
    scope_id: str,
) -> None:
    raise RuntimeError("Magic-link mail delivery is not configured")


def _configured_magic_link_base() -> str | None:
    for setting in ("CIVICCAST_RESIDENT_PORTAL_URL", "CIVICCAST_PUBLIC_BASE_URL"):
        raw = os.environ.get(setting, "").strip().rstrip("/")
        if not raw or any(character.isspace() for character in raw):
            continue
        try:
            parsed = urlsplit(raw)
        except ValueError:
            continue
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            continue
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            continue
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            continue
        return raw
    return None


def get_magic_link_email_sender() -> MagicLinkEmailSender:
    """Resolve real SMTP and a trusted public URL before minting a token."""
    if os.environ.get("CIVICCAST_PROVIDER_MAIL", "").strip().lower() != "real":
        return _default_email_sender
    base = _configured_magic_link_base()
    if base is None:
        return _default_email_sender
    try:
        mailbox = default_registry().resolve(PROVIDER_KIND_MAIL)
    except (ValueError, RuntimeError):
        logger.warning("paywall.magic_link.mail_configuration_invalid")
        return _default_email_sender
    if not isinstance(mailbox, SmtpMailbox):
        return _default_email_sender

    def send(*, email: str, token: str, station_id: str, scope_kind: str, scope_id: str) -> None:
        route = f"watch/{quote(scope_id, safe='')}" if scope_kind == "asset" else "redeem"
        link = f"{base}/#/{route}?token={quote(token, safe='')}"
        mailbox.send_magic_link(email=email, link_url=link)

    return send


# --- routers -----------------------------------------------------------------


class _PaywallStaffRoute(APIRoute):
    """Keep FastAPI validation errors from echoing signing-secret input."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original_handler(request)
            except RequestValidationError as exc:
                errors = []
                for error in exc.errors():
                    safe_error = dict(error)
                    if "signing_secret" in safe_error.get("loc", ()):
                        safe_error.pop("input", None)
                    elif "input" in safe_error:
                        safe_error["input"] = _redact_signing_secret_values(safe_error["input"])
                    errors.append(safe_error)
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    content={"detail": jsonable_encoder(errors)},
                )

        return handler


def _redact_signing_secret_values(value: object) -> object:
    """Preserve validation context while redacting nested secret fields."""

    if isinstance(value, dict):
        return {
            key: "[redacted]" if key == "signing_secret" else _redact_signing_secret_values(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_signing_secret_values(item) for item in value]
    return value


staff_router = APIRouter(
    prefix="/api/staff", tags=["staff", "paywall"], route_class=_PaywallStaffRoute
)
public_router = APIRouter(prefix="/api/public", tags=["public", "paywall"])
webhook_router = APIRouter(prefix="/api/webhooks", tags=["webhook", "paywall"])


# --- staff config ------------------------------------------------------------


@staff_router.get(
    "/paywall/config",
    response_model=PaywallConfigPublic,
    summary="Get the active paywall config for the station",
    dependencies=[Depends(require_any_role(*_CONFIG))],
    openapi_extra=_CONFIG_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def get_config(store: PaywallStore | None = Depends(get_paywall_store)) -> PaywallConfigPublic:
    # First install has no stored paywall row; return a safe disabled default
    # instead of surfacing normal absence as a visible 404 in the operator UI.
    """Return the public projection of the active station's paywall
    config. On first install, when no row exists yet, this returns a safe
    disabled default (200) rather than a 404 — the operator UI shows the
    default config in "edit" mode instead of a scary absence error.

    Q-2 fix: the response is :class:`PaywallConfigPublic`, which omits
    ``signing_secret`` entirely and surfaces ``signing_secret_present:
    bool`` instead. The secret stays write-only on PUT/PATCH — a stolen
    GET response (HAR file, screenshare) leaks zero secret material.
    """
    resolved = _require_store(store)
    config = resolved.get_config_for_station(_station_id())
    if config is None:
        return _empty_config_public()
    return PaywallConfigPublic.from_config(config)


@staff_router.put(
    "/paywall/config",
    response_model=PaywallConfigPublic,
    summary="Create or replace the paywall config for the station",
    dependencies=[Depends(require_any_role(*_CONFIG))],
    openapi_extra=_CONFIG_EXTRA,
    responses={
        409: {
            "description": ("Another config already exists for this station (different config_id).")
        },
        503: {"description": _DB_NOT_READY},
    },
)
def put_config(
    payload: PaywallConfigInput,
    store: PaywallStore | None = Depends(get_paywall_store),
) -> PaywallConfigPublic:
    """Create or replace the paywall config. A PUT against a different
    ``config_id`` for the SAME ``station_id`` is a 409 — the store's
    unique index enforces "one config per station". An absent or null
    signing secret preserves an existing value; ``""`` explicitly clears
    it, while a valid non-empty value rotates it. Response is the public
    projection (Q-2)."""
    resolved = _require_store(store)
    values = payload.model_dump()
    secret_value = payload.signing_secret
    preserve_existing_secret = secret_value is None
    if secret_value is None:
        # The UI keeps secrets write-only. Its ordinary full-form PUT omits
        # the value (or sends null), which must not erase the stored secret.
        # Pass this intent to the store so it skips the secret column inside
        # the same transaction as the other config fields.
        values["signing_secret"] = None
    elif secret_value == "":
        # Match PATCH's explicit clear sentinel. Blank is never an implicit
        # clear; only an explicit empty value asks to remove the secret.
        values["signing_secret"] = None
    elif len(secret_value) < 32:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=("signing_secret must be at least 32 characters; use a random 32+ byte string."),
        )
    config = PaywallConfig(**values)
    try:
        stored = resolved.upsert_config(
            config,
            preserve_signing_secret_if_missing=preserve_existing_secret,
        )
    except PaywallStationConfigConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return PaywallConfigPublic.from_config(stored)


@staff_router.patch(
    "/paywall/config/{config_id}",
    response_model=PaywallConfigPublic,
    summary="Patch the paywall config (absent keys unchanged)",
    dependencies=[Depends(require_any_role(*_CONFIG))],
    openapi_extra=_CONFIG_EXTRA,
    responses={
        404: {"description": "Paywall config not found"},
        409: {"description": "Patch would conflict with another config for the same station"},
        422: {"description": "signing_secret too short or otherwise invalid"},
        503: {"description": _DB_NOT_READY},
    },
)
def patch_config(
    config_id: str,
    payload: PaywallConfigUpdate,
    store: PaywallStore | None = Depends(get_paywall_store),
) -> PaywallConfigPublic:
    """Patch semantics: absent key unchanged.

    Q-2 fix — ``signing_secret`` set-only rules:

    * ABSENT (key not in body) → unchanged.
    * ``null`` → treated as ABSENT (does NOT clear). A naive UI that
      blanks every form field as null can't accidentally rotate the
      secret away.
    * Explicit ``""`` (empty string) → CLEARS the secret (rotates away).
    * Any non-empty string → SETS the new secret. The Q-11 floor
      (``min_length=32``) is enforced on this path; under-length values
      become 422.

    ``config_id`` / ``station_id`` are set at creation; not editable here.
    """
    resolved = _require_store(store)
    current = resolved.get_config(config_id)
    if current is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"Paywall config {config_id!r} not found.",
        )
    updates = payload.model_dump(exclude_unset=True)
    # Q-2 fix: signing_secret set-only semantics. Strip ``signing_secret``
    # from updates when it was explicitly ``null`` so a null does NOT
    # clear; only an explicit empty-string clears. Length floor enforced
    # for non-empty new values.
    if "signing_secret" in updates:
        secret_value = updates["signing_secret"]
        if secret_value is None:
            # Treat null as ABSENT — do not modify the stored secret.
            updates.pop("signing_secret")
        elif secret_value == "":
            # Explicit clear — pass through as None to the model.
            updates["signing_secret"] = None
        else:
            # Set new value — enforce the 32-char floor here (Q-11) so
            # the response surface is a clean 422, not a model
            # validation exception bubbling up as 500.
            if len(secret_value) < 32:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        "signing_secret must be at least 32 characters; "
                        "use a random 32+ byte string."
                    ),
                )
    merged = current.model_copy(update=updates)
    try:
        stored = resolved.upsert_config(merged)
    except PaywallStationConfigConflictError as exc:  # pragma: no cover - identity fields immutable
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return PaywallConfigPublic.from_config(stored)


@staff_router.delete(
    "/paywall/config/{config_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete the paywall config",
    dependencies=[Depends(require_any_role(*_CONFIG))],
    openapi_extra=_CONFIG_EXTRA,
    responses={
        404: {"description": "Paywall config not found"},
        503: {"description": _DB_NOT_READY},
    },
)
def delete_config(
    config_id: str,
    store: PaywallStore | None = Depends(get_paywall_store),
) -> Response:
    resolved = _require_store(store)
    try:
        resolved.delete_config(config_id)
    except PaywallConfigNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- staff grants (operator-issued comps) ------------------------------------


@staff_router.post(
    "/paywall/grants",
    response_model=AccessGrant,
    status_code=status.HTTP_201_CREATED,
    summary="Issue a comp access grant",
    dependencies=[Depends(require_any_role(*_CONFIG))],
    openapi_extra=_CONFIG_EXTRA,
    responses={
        409: {"description": "Grant with this grant_id already exists"},
        503: {"description": _DB_NOT_READY},
    },
)
def create_grant(
    payload: AccessGrantInput,
    store: PaywallStore | None = Depends(get_paywall_store),
) -> AccessGrant:
    """Issue a comp grant (spec §5 "comp-access grants"). The store's
    upsert is idempotent but POST against an existing ``grant_id`` is a
    409 so the operator UI can't accidentally overwrite a grant by
    typoing the id."""
    resolved = _require_store(store)
    if resolved.get_grant(payload.grant_id) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Grant {payload.grant_id!r} already exists.",
        )
    return resolved.upsert_grant(AccessGrant(**payload.model_dump()))


@staff_router.delete(
    "/paywall/grants/{grant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a grant",
    dependencies=[Depends(require_any_role(*_CONFIG))],
    openapi_extra=_CONFIG_EXTRA,
    responses={
        404: {"description": "Grant not found"},
        503: {"description": _DB_NOT_READY},
    },
)
def delete_grant(
    grant_id: str,
    store: PaywallStore | None = Depends(get_paywall_store),
) -> Response:
    resolved = _require_store(store)
    try:
        resolved.delete_grant(grant_id)
    except AccessGrantNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- public surface ----------------------------------------------------------


_PublicScope = Literal["asset", "series"]


@public_router.get(
    "/paywall/access",
    response_model=PublicAccessDecision,
    summary="Public read: does this email have access to this asset/series?",
    responses={429: {"description": "Rate limit exceeded"}, 503: {"description": _DB_NOT_READY}},
)
def public_access(
    request: Request,
    asset_id: str | None = Query(
        None,
        description="Asset id to check access for. Mutually exclusive with series_id.",
    ),
    series_id: str | None = Query(
        None,
        description="Series id to check access for. Mutually exclusive with asset_id.",
    ),
    email: str | None = Query(
        None,
        description="The viewer's email (from their magic-link session). Anonymous if absent.",
    ),
    svc: PaywallService | None = Depends(get_paywall_service),
) -> PublicAccessDecision:
    """Returns ``allowed`` + an optional ``reason``. DC-1 default-off
    means a station without a config returns ``allowed=True`` regardless
    of email.

    Q-5 fix: in-process rate limiting (best-effort, deployment edge
    should ALSO enforce limits — see module-level note).
    """
    _enforce_rate_limit("access", request)
    resolved = _require_service(svc)
    if asset_id and series_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Pass either asset_id or series_id, not both.",
        )
    if not asset_id and not series_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Pass either asset_id or series_id.",
        )
    scope_kind: _PublicScope = "asset" if asset_id else "series"
    scope_id = asset_id or series_id or ""
    return resolved.decide(_station_id(), email, scope_kind, scope_id)


class MagicLinkRequest(BaseModel):
    """Body of ``POST /api/public/paywall/magic-link``.

    E-1 BLOCKER fix: the public mint endpoint refuses ``scope_kind="all"``.
    A catch-all magic-link redemption would create a 30-day grant that
    unlocks every paywalled asset for the holder — defeating the entire
    paywall with one email round-trip. Operator-only paths (staff comp
    mint via the service layer) may still issue ``"all"``; the public
    surface cannot. The default is ``"asset"`` and a non-empty
    ``scope_id`` is required.
    """

    model_config = ConfigDict(extra="forbid")

    email: str = Field(..., min_length=3, max_length=320)
    scope_kind: Literal["asset", "series"] = "asset"
    scope_id: str = Field(..., min_length=1, max_length=120)


class MagicLinkResponse(BaseModel):
    """Confirmation envelope for ``POST /api/public/paywall/magic-link``.

    We deliberately do NOT echo the token here — the viewer receives it
    via email; surfacing it on the response would defeat the channel-
    separation that makes magic-links a defense against credential
    stuffing. The delivery boolean reports whether the configured relay
    accepted the message.
    """

    model_config = ConfigDict(extra="forbid")

    sent: bool


class MagicLinkVerificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: Literal[True] = True
    email: str


@public_router.post(
    "/paywall/magic-link",
    response_model=MagicLinkResponse,
    summary="Email a single-use magic-link to the viewer",
    responses={
        200: {"description": "Magic-link delivery status"},
        422: {"description": "Public mint requires an asset/series scope_id (no catch-all)"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": _DB_NOT_READY},
    },
)
def public_magic_link(
    payload: MagicLinkRequest,
    request: Request,
    svc: PaywallService | None = Depends(get_paywall_service),
    sender: MagicLinkEmailSender = Depends(get_magic_link_email_sender),
) -> MagicLinkResponse:
    """Mint a token + invoke the email sender. The token is never echoed.

    ``sent`` is true only after the configured SMTP relay accepts the message.

    Q-5 fix: per-IP rate limit (5 / 60s) to prevent the endpoint being
    used as an email-spam relay once SMTP is wired.

    The response shape remains identical whether or not the paywall is
    configured. The boolean truthfully reports delivery, without a token.

    E-1 BLOCKER fix: the request model rejects ``scope_kind="all"``; we
    additionally route through :meth:`mint_magic_link_for_public` which
    re-validates the same rule.
    """
    _enforce_rate_limit("magic_link", request)
    resolved = _require_service(svc)
    sid = _station_id()
    try:
        token = resolved.mint_magic_link_for_public(
            sid, payload.email, payload.scope_kind, payload.scope_id
        )
    except MagicLinkNotConfiguredError:
        # Q-9 fix: identical response shape whether configured or not.
        # The mint is silently dropped on the unconfigured path so the
        # response status code can't be used to probe "is paywall on?".
        logger.info(
            "paywall.magic_link.mint_no_config station_id=%s scope_kind=%s",
            sid,
            payload.scope_kind,
        )
        return MagicLinkResponse(sent=False)
    except PaywallServiceError as exc:
        # E-1 belt-and-suspenders: a service-level rejection of
        # ``scope_kind="all"`` (or any other validation) becomes a 422
        # (the request didn't conform to the public contract).
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    try:
        sender(
            email=payload.email,
            token=token,
            station_id=sid,
            scope_kind=payload.scope_kind,
            scope_id=payload.scope_id,
        )
    except Exception as exc:
        logger.warning(
            "paywall.magic_link.delivery_failed station_id=%s error_type=%s",
            sid,
            type(exc).__name__,
        )
        return MagicLinkResponse(sent=False)
    return MagicLinkResponse(sent=True)


_VERIFY_GENERIC_DETAIL = "Magic link could not be verified."


@public_router.get(
    "/paywall/verify",
    response_model=MagicLinkVerificationResponse,
    summary="Redeem a magic-link token (single-use)",
    responses={
        401: {"description": "Token invalid, expired, or already redeemed"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": _DB_NOT_READY},
    },
)
def public_verify_magic_link(
    request: Request,
    response: Response,
    token: str = Query(..., min_length=8, description="The signed token from the email link."),
    svc: PaywallService | None = Depends(get_paywall_service),
) -> MagicLinkVerificationResponse:
    """Verify the token + return a decision.

    E-3 / Q-7 fix: ALL failure modes (malformed / bad signature /
    expired / replayed) return one identical 401 body:
    ``{"detail": "Magic link could not be verified."}``. A probing
    client cannot distinguish sub-checks via the response. The actual
    failure type is logged at INFO level (it's user input, not a server
    error) for ops debugging.

    Q-8 fix: ``expected_station_id`` is passed so a token minted at
    station A can never redeem against station B.

    Q-5 fix: per-IP rate limit (10 / 60s).
    """
    _enforce_rate_limit("verify", request)
    resolved = _require_service(svc)
    sid = _station_id()
    try:
        grant = resolved.verify_magic_link(token, expected_station_id=sid)
    except MagicLinkVerificationError as exc:
        # E-3/Q-7: log specific cause, return identical detail body.
        logger.info(
            "paywall.verify.failed station_id=%s reason=%s",
            sid,
            type(exc).__name__,
        )
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail=_VERIFY_GENERIC_DETAIL,
        ) from exc
    peer = request.client.host if request.client else ""
    try:
        local = ipaddress.ip_address(peer).is_loopback and request.url.hostname in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
    except ValueError:
        local = False
    response.set_cookie(
        "civiccast_viewer",
        resolved.viewer_proof(grant),
        max_age=12 * 3600,
        path="/api/public",
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https" or not local,
    )
    response.headers["Cache-Control"] = "no-store"
    return MagicLinkVerificationResponse(email=grant.email)


# --- webhook -----------------------------------------------------------------


@webhook_router.post(
    "/stripe",
    summary="Stripe webhook (signed)",
    dependencies=[Depends(_enforce_webhook_body_size)],
    responses={
        401: {"description": "Signature invalid or paywall not configured"},
        400: {"description": "Webhook body rejected"},
        413: {"description": "Stripe webhook payload exceeds 1 MiB cap"},
        503: {"description": _DB_NOT_READY},
    },
)
async def stripe_webhook(
    request: Request,
    svc: PaywallService | None = Depends(get_paywall_service),
) -> dict[str, object]:
    """Receive a Stripe-signed webhook event and reconcile state.

    The raw request bytes (not the parsed JSON) are passed to the
    verifier so Stripe's signature still matches — re-serializing JSON
    would shuffle key order + whitespace and break the MAC. The
    ``Stripe-Signature`` header carries the signature; a missing header
    is a 401.

    Q-4 fix: the body is capped at 1 MiB. The Content-Length header is
    checked via ``_enforce_webhook_body_size`` (dependency above) and a
    streaming cap below catches Transfer-Encoding: chunked bypasses.
    """
    resolved = _require_service(svc)
    # Q-4 streaming cap (defense for chunked transfers where
    # Content-Length is absent).
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > _WEBHOOK_MAX_CONTENT_LENGTH:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Stripe webhook payload exceeds 1 MiB cap.",
            )
    signature = request.headers.get("stripe-signature") or request.headers.get("Stripe-Signature")
    if not signature:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Stripe-Signature header is required.",
        )
    sid = _station_id()
    try:
        return resolved.handle_stripe_webhook(body, signature, sid)
    except WebhookSignatureError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except PaywallServiceError as exc:
        # A malformed event (e.g. missing email on a subscription created)
        # is a 400 — Stripe will retry on 5xx so we'd loop forever; a 4xx
        # tells Stripe we've decided not to accept it.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


__all__ = [
    "MagicLinkEmailSender",
    "MagicLinkRequest",
    "MagicLinkResponse",
    "get_magic_link_email_sender",
    "get_paywall_service",
    "get_paywall_store",
    "public_router",
    "staff_router",
    "webhook_router",
]
