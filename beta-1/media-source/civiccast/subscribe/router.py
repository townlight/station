# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""FastAPI routes for v0.8 resident subscriptions."""

from __future__ import annotations

import logging
import os
import socket
from contextlib import suppress
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from civiccast.auth.roles import require_any_role
from civiccast.installer.station_state import resolve_station_display_name
from civiccast.platform.providers import (
    PROVIDER_KIND_MAIL,
    PROVIDER_KIND_WEBHOOK,
    ProviderRegistry,
    default_registry,
    describe_provider,
)
from civiccast.platform.stores import resolve_app_store
from civiccast.publish.notifications import watch_url_for_asset
from civiccast.publish.targets import (
    ChannelAssociationLookup,
    resolve_public_base_url,
    resolve_publication_targets,
    resolve_station_default_channel_id,
)
from civiccast.schedule.models import StaffAssetRow
from civiccast.subscribe.models import (
    NotificationDispatchResponse,
    NotificationPayload,
    RssItem,
    SubscriptionConfirmResponse,
    SubscriptionPublicResponse,
    SubscriptionSignupRequest,
    SubscriptionTargetType,
    SubscriptionWebhookRequest,
)
from civiccast.subscribe.outcome_store import NotificationDeliveryStore
from civiccast.subscribe.rate_limit import SubscribeRateLimiter
from civiccast.subscribe.secrets import SubscriptionSecrets, load_subscription_secrets
from civiccast.subscribe.service import (
    confirm_subscription,
    create_email_subscription,
    create_webhook_subscription,
    dispatch_notifications,
    subscription_rss,
    unsubscribe,
)
from civiccast.subscribe.store import SubscribeStore

public_router = APIRouter(prefix="/api/public/subscribe", tags=["public", "subscribe"])
staff_router = APIRouter(prefix="/api/staff/subscribe", tags=["staff", "subscribe"])


def get_subscribe_store(request: Request) -> SubscribeStore:
    return cast(
        SubscribeStore, resolve_app_store(request, "subscribe_store", surface="Subscribe store")
    )


def get_notification_delivery_store(request: Request) -> NotificationDeliveryStore | None:
    """Resolve the durable subscriber-notification receipt store, or ``None``.

    ``None`` (an app instance without durable storage) does not disable
    delivery: ``deliver_publication_notifications`` falls back to a
    process-local guard. It does mean receipts do not outlive the process,
    which is why the durable store is wired wherever storage exists.

    Defined here rather than in ``civiccast.publish.router`` because the public
    RSS feed needs the sibling target lookup and ``publish.router`` already
    imports this module -- one definition, and the import direction stays
    publish -> subscribe.
    """

    return cast(
        "NotificationDeliveryStore | None",
        resolve_app_store(
            request, "notification_delivery_store", surface="Notification delivery store"
        ),
    )


def get_publication_target_lookup(request: Request) -> ChannelAssociationLookup | None:
    """Resolve the schedule/live channel association for the target resolver."""

    return cast(
        "ChannelAssociationLookup | None",
        resolve_app_store(
            request, "publication_target_lookup", surface="Publication target lookup"
        ),
    )


def get_provider_registry(request: Request) -> ProviderRegistry:
    """The app's single provider registry (mirrors the publish router's)."""

    return cast(
        ProviderRegistry,
        getattr(request.app.state, "provider_registry", None) or default_registry(),
    )


def get_rss_asset_store(request: Request) -> Any:
    """Resolve the asset store the public RSS feed reads published records from."""

    return resolve_app_store(request, "asset_store", surface="Asset store")


def get_subscribe_secrets(request: Request) -> SubscriptionSecrets:
    configured = getattr(request.app.state, "subscribe_secrets", None)
    return cast(SubscriptionSecrets, configured or load_subscription_secrets())


def get_subscribe_rate_limiter(request: Request) -> SubscribeRateLimiter:
    configured = getattr(request.app.state, "subscribe_rate_limiter", None)
    return cast(SubscribeRateLimiter, configured or SubscribeRateLimiter())


def _enforce_signup_rate_limit(
    request: Request, limiter: SubscribeRateLimiter, *, channel: str
) -> None:
    limit = int(os.environ.get("CIVICCAST_SUBSCRIBE_RATE_LIMIT", "5"))
    window_seconds = int(os.environ.get("CIVICCAST_SUBSCRIBE_RATE_LIMIT_WINDOW_SECONDS", "60"))
    client_host = request.client.host if request.client is not None else "unknown"
    if not limiter.allow(f"{channel}:{client_host}", limit=limit, window_seconds=window_seconds):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "Too many subscription attempts from this address. Wait a minute, then try again."
            ),
        )


@public_router.post(
    "/email",
    response_model=SubscriptionPublicResponse,
    summary="Start email double opt-in subscription",
)
def signup_email(
    payload: SubscriptionSignupRequest,
    request: Request,
    store: SubscribeStore = Depends(get_subscribe_store),
    secrets: SubscriptionSecrets = Depends(get_subscribe_secrets),
    limiter: SubscribeRateLimiter = Depends(get_subscribe_rate_limiter),
) -> SubscriptionPublicResponse:
    _enforce_signup_rate_limit(request, limiter, channel="email")
    return create_email_subscription(payload, store=store, secrets=secrets)


@public_router.post(
    "/webhook",
    response_model=SubscriptionPublicResponse,
    summary="Start webhook subscription opt-in",
    responses={
        400: {"description": "A public HTTPS webhook endpoint is required"},
        502: {"description": "Endpoint confirmation could not be delivered"},
    },
)
def signup_webhook(
    payload: SubscriptionWebhookRequest,
    request: Request,
    store: SubscribeStore = Depends(get_subscribe_store),
    secrets: SubscriptionSecrets = Depends(get_subscribe_secrets),
    limiter: SubscribeRateLimiter = Depends(get_subscribe_rate_limiter),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> SubscriptionPublicResponse:
    _enforce_signup_rate_limit(request, limiter, channel="webhook")
    try:
        return create_webhook_subscription(
            payload,
            store=store,
            secrets=secrets,
            webhook_client=registry.resolve(PROVIDER_KIND_WEBHOOK),
        )
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="Use a public HTTPS webhook endpoint."
        ) from exc
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Webhook confirmation delivery failed (%s).", type(exc).__name__
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="The webhook confirmation could not be delivered. Check the endpoint and register again.",
        ) from exc


@public_router.get(
    "/confirm",
    response_model=SubscriptionConfirmResponse,
    summary="Confirm a double opt-in subscription",
)
def confirm(
    token: str = Query(min_length=1),
    store: SubscribeStore = Depends(get_subscribe_store),
    secrets: SubscriptionSecrets = Depends(get_subscribe_secrets),
) -> SubscriptionConfirmResponse:
    try:
        return confirm_subscription(token, store=store, secrets=secrets)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found. Sign up again to receive a fresh confirmation link.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _unsubscribe(
    token: str, store: SubscribeStore, secrets: SubscriptionSecrets
) -> SubscriptionConfirmResponse:
    try:
        return unsubscribe(token, store=store, secrets=secrets)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found. Contact the station if notices continue.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


# Hosts trusted as "this station, running locally" -- matches
# civiccast.activitypub.config._normalize_base_url's own loopback allowlist,
# which faced the same problem (a public link built from an env value or a
# request Host header must never be allowed to point somewhere the operator
# didn't configure).
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "testserver"})


def _resolve_public_base_url(request: Request) -> str:
    """Resolve the station's real public base URL for public feed links.

    Never invents a host. In priority order:

    1. ``CIVICCAST_PUBLIC_BASE_URL`` (the same env var
       ``civiccast.activitypub.config.load_activitypub_config`` falls back to
       for its own ``base_url``) -- the operator's configured real public
       address, when it parses as a valid absolute http(s) URL.
    2. The incoming request's own scheme+host, but ONLY when that host is
       loopback/testserver (local dev/test) or matches the configured
       station host above -- an arbitrary proxied ``Host`` header must never
       be trusted to build a link a public RSS reader will follow.

    Returns ``""`` when neither is available, so the caller renders a
    relative link instead of fabricating one.
    """
    configured = os.environ.get("CIVICCAST_PUBLIC_BASE_URL", "").strip().rstrip("/")
    configured_host: str | None = None
    if configured:
        parsed_configured = urlparse(configured)
        if parsed_configured.scheme in {"http", "https"} and parsed_configured.netloc:
            configured_host = parsed_configured.hostname
        else:
            configured = ""

    if configured:
        return configured

    request_base = str(request.base_url).rstrip("/")
    request_host = urlparse(request_base).hostname
    if request_host and (request_host in _LOOPBACK_HOSTS or request_host == configured_host):
        return request_base

    return ""


@public_router.get(
    "/unsubscribe",
    response_model=SubscriptionConfirmResponse,
    summary="Unsubscribe using a signed one-click link",
)
def unsubscribe_link(
    token: str = Query(min_length=1),
    store: SubscribeStore = Depends(get_subscribe_store),
    secrets: SubscriptionSecrets = Depends(get_subscribe_secrets),
) -> SubscriptionConfirmResponse:
    """A resident clicking the unsubscribe link in a notice."""

    return _unsubscribe(token, store, secrets)


@public_router.post(
    "/unsubscribe",
    response_model=SubscriptionConfirmResponse,
    summary="One-click unsubscribe (RFC 8058)",
)
def unsubscribe_one_click(
    token: str = Query(min_length=1),
    store: SubscribeStore = Depends(get_subscribe_store),
    secrets: SubscriptionSecrets = Depends(get_subscribe_secrets),
) -> SubscriptionConfirmResponse:
    """The mail client acting on the reader's behalf.

    RFC 8058 requires a POST for the ``List-Unsubscribe-Post`` header the
    notice advertises: mail clients will not show a one-click Unsubscribe
    control without it, and a GET-only endpoint risks link-prefetchers
    unsubscribing residents who never asked. Same signed token, same effect.
    """

    return _unsubscribe(token, store, secrets)


# Hosts a request can legitimately arrive on for a station serving itself.
# "0.0.0.0" is a bind address, not a Host a browser sends, so it is not here.


def _trusted_hosts() -> frozenset[str]:
    """Hostnames this station will echo into a public feed's links.

    Loopback and the machine's own hostname/addresses are always trusted; an
    operator adds anything else (a reverse-proxy name, a vanity domain) through
    ``CIVICCAST_TRUSTED_HOSTS`` as a comma-separated list.
    """

    trusted = set(_LOOPBACK_HOSTS)
    with suppress(OSError):
        hostname = socket.gethostname()
        trusted.add(hostname.lower())
        trusted.add(socket.getfqdn(hostname).lower())
        trusted.update(address.lower() for address in socket.gethostbyname_ex(hostname)[2])
    for entry in os.environ.get("CIVICCAST_TRUSTED_HOSTS", "").split(","):
        cleaned = entry.strip().lower()
        if cleaned:
            trusted.add(cleaned)
    return frozenset(trusted)


def _request_base_url_if_trusted(request: Request) -> str | None:
    """The request's own origin, but only when its Host is one we trust.

    There is no ``TrustedHostMiddleware`` in this app, so ``request.base_url``
    is derived from a client-supplied ``Host`` header. Echoing that into a
    cached public RSS feed would let any caller mint links to a host they
    control and have the station's own feed vouch for them. When the Host is
    not recognised, the caller gets an actionable 503 instead of a poisoned
    feed -- the fix is an operator setting, not a guess.
    """

    host = (request.url.hostname or "").lower()
    if not host or host not in _trusted_hosts():
        return None
    return str(request.base_url).rstrip("/")


#: Most-recent published recordings one feed carries. A civic station's
#: recording history is unbounded; a reader wants the recent record, and an
#: unbounded feed would grow with every meeting ever held.
RSS_FEED_MAX_ITEMS = 50
#: Published rows scanned before target filtering. Bounds the resolver's work
#: for a station whose recent publications are mostly on other channels.
_RSS_SCAN_LIMIT = 400


def _published_rows(asset_store: Any) -> list[StaffAssetRow]:
    """Published, packaged recordings, newest first -- or none, never examples.

    Bounded at the STORE, not after the fact: this is an unauthenticated public
    route, and a station with a decade of meetings must not materialise every
    asset row per request. ``list_all_page`` is the paginated projection
    (``civiccast.schedule.store.PostgresAssetStore``), ordered
    ``published_at DESC NULLS LAST``, so one page of the newest rows is exactly
    what a feed needs. ``list_all`` is the unpaginated fallback for stores that
    predate it; a store with neither has nothing published to report.
    """

    pager = getattr(asset_store, "list_all_page", None)
    if pager is not None:
        rows, _total = pager(limit=_RSS_SCAN_LIMIT, offset=0)
    else:
        lister = getattr(asset_store, "list_all", None)
        if lister is None:
            # An app instance whose asset store has no operator-side projection
            # (the ephemeral in-memory VOD store). There is nothing published
            # to report, so the feed is empty -- not seeded with a sample item.
            return []
        rows = cast(list[StaffAssetRow], lister())[:_RSS_SCAN_LIMIT]
    return [row for row in rows if row.published_at is not None and row.manifest_url]


@public_router.get(
    "/rss/{target_type}/{target_id}.xml",
    summary="Read a public no-PII subscription RSS feed",
)
def rss_feed(
    target_type: str,
    target_id: str,
    request: Request,
    asset_store: Any = Depends(get_rss_asset_store),
    target_lookup: ChannelAssociationLookup | None = Depends(get_publication_target_lookup),
) -> Response:
    # WP-05 plan item 12. NOTE for future editors: this function's docstring
    # becomes the public OpenAPI description, and
    # `tests/policy/test_lan_only_station_external_dependencies.py` fails the
    # build if /openapi.json names an external host -- so the history of the
    # placeholder link this route used to emit lives here in a comment and in
    # the CHANGELOG, never in the docstring.
    #
    # What was removed: one fabricated item ("Example CivicCast recording")
    # pointing at a placeholder hostname, served for every target on every
    # station in production. What replaced it: real published records,
    # filtered by the same canonical resolver publish delivery uses
    # (civiccast.publish.targets.resolve_publication_targets), so the feed and
    # the notices agree about which recordings belong to a target.
    """Render this target's real published recordings as RSS.

    Items are the station's actual published, packaged recordings for this
    channel or meeting body, newest first, capped at
    :data:`RSS_FEED_MAX_ITEMS`. Links are built from the station's configured
    public base URL and the portal's ``#/watch/{asset_id}`` route. A station
    with nothing published yet gets a valid, configured, empty feed.

    When no public base URL is configured, this request's own origin is used
    only if its Host is one the station recognises; an unrecognised Host gets
    a 503 telling the operator to set the public web address, because a public
    feed must never echo a caller-supplied hostname into its links.
    """
    if target_type not in {"channel", "meeting_body"}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RSS feed not found. Use channel or meeting_body feed paths.",
        )
    # Configured base first. The request's own origin is a fallback ONLY when
    # its Host is trusted -- see _request_base_url_if_trusted.
    base_url = resolve_public_base_url() or _request_base_url_if_trusted(request)
    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "This feed is not available yet because the station has no public web "
                "address set. Open Setup and set the station's public web address "
                "(or add this hostname to CIVICCAST_TRUSTED_HOSTS), then reload."
            ),
        )
    rows = _published_rows(asset_store)
    channel_ids: dict[str, str] = {}
    if rows and target_lookup is not None:
        channel_ids = target_lookup.channel_ids_for_assets(rows)
    default_channel_id = resolve_station_default_channel_id()

    items: list[RssItem] = []
    for row in rows:
        targets = resolve_publication_targets(
            row,
            channel_id=channel_ids.get(row.asset_id),
            default_channel_id=default_channel_id,
        )
        if not any(
            target.target_type == target_type and target.target_id == target_id
            for target in targets
        ):
            continue
        link = watch_url_for_asset(row.asset_id, public_base_url=base_url)
        if link is None:  # pragma: no cover - base_url is non-empty by construction
            continue
        items.append(
            RssItem(
                title=row.title,
                link=link,
                guid=f"civiccast:asset:{row.asset_id}",
                published_at=cast(datetime, row.published_at),
                description=(row.description or "").strip()
                or f"Public recording published by {resolve_station_display_name()}.",
            )
        )
        if len(items) >= RSS_FEED_MAX_ITEMS:
            break

    xml = subscription_rss(
        target_type,
        target_id,
        items,
        public_base_url=base_url,
        station_name=resolve_station_display_name(),
    )
    return Response(
        content=xml,
        media_type="application/rss+xml",
        # Readers poll feeds on a timer. A short shared cache window keeps a
        # popular meeting from turning every reader's poll into a database
        # page scan, while staying far below the interval at which a station
        # publishes a new recording.
        headers={"Cache-Control": "public, max-age=300"},
    )


@staff_router.post(
    "/dispatch-test",
    response_model=NotificationDispatchResponse,
    summary="Dispatch a simulated subscription notification for local proof",
    dependencies=[Depends(require_any_role("publish_operator", "support_admin"))],
    responses={409: {"description": "A real mail or webhook provider is configured"}},
)
def dispatch_test(
    payload: NotificationPayload,
    target_type: SubscriptionTargetType,
    target_id: str = Query(min_length=1, max_length=120),
    store: SubscribeStore = Depends(get_subscribe_store),
    secrets: SubscriptionSecrets = Depends(get_subscribe_secrets),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> NotificationDispatchResponse:
    """Fan out a notice to one target's confirmed subscribers, mocks only.

    Two WP-05 changes. First, ``target_type``/``target_id`` are required: the
    old ``channel``/``government`` defaults meant a mistyped call silently
    tested a target the caller never named. Second, this route refuses with 409
    once a real mail or webhook provider is configured -- a route named
    "dispatch-test" must not put a message in a resident's inbox, and it writes
    no delivery receipt, so a real send here would also be invisible to the
    Publish dashboard's idempotency guard. Real fan-out belongs to publish
    approval, which does both.
    """

    real = [
        name
        for kind, name in (
            (PROVIDER_KIND_MAIL, "mail"),
            (PROVIDER_KIND_WEBHOOK, "webhook"),
        )
        if not describe_provider(kind, registry).simulated
    ]
    if real:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This test route only runs against simulated providers, and this "
                f"station has a real {' and '.join(real)} provider configured. "
                "Approve the recording on the Publish screen to send real notices; "
                "they are recorded and cannot be sent twice."
            ),
        )
    return dispatch_notifications(
        payload,
        store=store,
        target_type=target_type,
        target_id=target_id,
        secrets=secrets,
    )
