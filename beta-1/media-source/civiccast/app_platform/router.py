# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Public and staff app-platform routes for v1.8 contracts."""

from __future__ import annotations

import hmac
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status

from civiccast.analytics.store import (
    AnalyticsStoreProtocol,
    cast_analytics_store,
    retained_analytics_fields,
)
from civiccast.app_platform.models import (
    AnalyticsEvent,
    AnalyticsIngestResponse,
    AudioTrack,
    CaptionTrack,
    CatalogSort,
    ChannelBrandingUpdate,
    ChannelPublicConfig,
    ChapterMarker,
    EpgScheduleResponse,
    LiveState,
    NowNextState,
    PlaybackPolicy,
    PrerollPolicy,
    ScheduleFeedItem,
    ScheduleFeedKind,
    SmartPlaylistDefinition,
    SmartPlaylistRule,
    StationAppConfig,
    StationAppConfigUpdate,
    VodCatalogItem,
    VodCatalogResponse,
)
from civiccast.app_platform.store import (
    AppPlatformConfigStore,
    AppPlatformConfigStoreError,
    default_app_platform_config_path,
)
from civiccast.auth.roles import require_any_role
from civiccast.cable.channel import (
    ChannelNowNext,
    ChannelProfile,
    PlayoutBlock,
    build_sample_channel_now_next,
    default_channel_profiles,
    public_live_manifest_path,
)
from civiccast.platform.stores import resolve_app_store
from civiccast.playback_policy.models import PlaybackPolicyConfig
from civiccast.playback_policy.router import get_playback_policy_store
from civiccast.playback_policy.store import PlaybackPolicyStore

public_router = APIRouter(prefix="/api/public/app", tags=["public", "app-platform"])
staff_router = APIRouter(prefix="/api/staff/app", tags=["staff", "app-platform"])

_ANALYTICS_MAX_CONTENT_LENGTH = 16_384
_ANALYTICS_DEFAULT_RATE_LIMIT_PER_MINUTE = 60
_ANALYTICS_DEFAULT_TRUSTED_RATE_LIMIT_PER_MINUTE = 600
_ANALYTICS_DEFAULT_MAX_RATE_LIMIT_BUCKETS = 4096
_ANALYTICS_RATE_LIMIT_WINDOW_SECONDS = 60
_ANALYTICS_RATE_LIMIT_PRUNE_INTERVAL_SECONDS = 30


@dataclass
class AnalyticsIngestAccess:
    trusted: bool
    # F-RC3-3: analytics is opt-in, privacy-safe, best-effort telemetry. On a
    # default station it is not configured, and the public portal fires an
    # event on every page load. `configured=False` means "accept and drop"
    # (a clean 202 no-op) instead of a 503 that noises up every page load.
    configured: bool = True


@dataclass
class AnalyticsRateLimitBucket:
    requests: list[float]
    touched_at: float = 0.0


def get_app_platform_config_store(request: Request) -> AppPlatformConfigStore:
    store = getattr(request.app.state, "app_platform_config_store", None)
    if isinstance(store, AppPlatformConfigStore):
        return store
    try:
        store = AppPlatformConfigStore(default_app_platform_config_path())
    except AppPlatformConfigStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    request.app.state.app_platform_config_store = store
    return store


@public_router.get(
    "/config",
    response_model=StationAppConfig,
    summary="Read shared app-platform station config",
)
def read_station_app_config(
    station_name: str | None = Query(default=None, min_length=1, max_length=160),
    store: AppPlatformConfigStore = Depends(get_app_platform_config_store),
) -> StationAppConfig:
    """Return the station app config consumed by all app shells."""

    return store.read_config(station_name_override=station_name)


@public_router.get(
    "/channels/{channel_id}",
    response_model=ChannelPublicConfig,
    summary="Read one public app-platform channel config",
    responses={404: {"description": "Channel not found"}},
)
def read_channel_app_config(
    channel_id: str,
    store: AppPlatformConfigStore = Depends(get_app_platform_config_store),
) -> ChannelPublicConfig:
    channel = store.read_channel(channel_id)
    if channel is None:
        raise _channel_not_found(channel_id)
    return channel


@staff_router.get(
    "/config",
    response_model=StationAppConfig,
    summary="Read operator app-platform station config",
)
def read_staff_station_app_config(
    store: AppPlatformConfigStore = Depends(get_app_platform_config_store),
) -> StationAppConfig:
    return store.read_config()


@staff_router.patch(
    "/config",
    response_model=StationAppConfig,
    summary="Update operator app-platform station config",
    dependencies=[Depends(require_any_role("setup_admin", "publish_operator"))],
)
def update_staff_station_app_config(
    patch: StationAppConfigUpdate,
    store: AppPlatformConfigStore = Depends(get_app_platform_config_store),
) -> StationAppConfig:
    try:
        return store.update_station(patch)
    except AppPlatformConfigStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


@staff_router.get(
    "/channels/{channel_id}",
    response_model=ChannelPublicConfig,
    summary="Read operator app-platform channel config",
    responses={404: {"description": "Channel not found"}},
)
def read_staff_channel_app_config(
    channel_id: str,
    store: AppPlatformConfigStore = Depends(get_app_platform_config_store),
) -> ChannelPublicConfig:
    channel = store.read_channel(channel_id)
    if channel is None:
        raise _channel_not_found(channel_id)
    return channel


@staff_router.patch(
    "/channels/{channel_id}/branding",
    response_model=ChannelPublicConfig,
    summary="Update operator app-platform channel branding",
    responses={404: {"description": "Channel not found"}},
    dependencies=[Depends(require_any_role("setup_admin", "publish_operator"))],
)
def update_staff_channel_branding(
    channel_id: str,
    patch: ChannelBrandingUpdate,
    store: AppPlatformConfigStore = Depends(get_app_platform_config_store),
) -> ChannelPublicConfig:
    try:
        return store.update_channel_branding(channel_id, patch)
    except AppPlatformConfigStoreError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise _channel_not_found(channel_id) from exc


@public_router.get(
    "/channels/{channel_id}/live",
    response_model=LiveState,
    summary="Read app-platform live playback state",
    responses={404: {"description": "Channel not found"}},
)
def read_channel_live_state(channel_id: str) -> LiveState:
    profile = _profile_or_404(channel_id)
    # Seeded/sample feed by contract (proof_boundary says so below); the
    # operator console's now/next uses the honest egress-state builder instead.
    now_next = build_sample_channel_now_next(profile.channel_id)
    return _live_state_from_block(profile, _seeded_current_block(now_next))


@public_router.get(
    "/channels/{channel_id}/schedule",
    response_model=list[ScheduleFeedItem],
    summary="Read app-platform schedule feed",
    responses={404: {"description": "Channel not found"}},
)
def read_channel_schedule_feed(channel_id: str) -> list[ScheduleFeedItem]:
    profile = _profile_or_404(channel_id)
    now_next = build_sample_channel_now_next(profile.channel_id)
    blocks = [_seeded_current_block(now_next)]
    if now_next.next is not None:
        blocks.append(now_next.next)
    return [_schedule_item_from_block(profile, block) for block in blocks]


@public_router.get(
    "/channels/{channel_id}/schedule/epg",
    response_model=EpgScheduleResponse,
    summary="Read app-platform EPG schedule export",
    responses={404: {"description": "Channel not found"}},
)
def read_channel_epg_schedule(channel_id: str) -> EpgScheduleResponse:
    profile = _profile_or_404(channel_id)
    return EpgScheduleResponse(
        generated_at=datetime.now(UTC).replace(microsecond=0),
        channel_id=profile.channel_id,
        items=read_channel_schedule_feed(profile.channel_id),
        export_targets=[
            "web_pwa",
            "roku",
            "tvos",
            "fire_tv",
            "android_tv",
            "android_mobile",
            "ios_ipados",
            "cg",
            "epg",
        ],
        export_formats=["json", "tvguide_xlist"],
        proof_boundary="playout-plan-to-public-epg-export",
    )


@public_router.get(
    "/channels/{channel_id}/schedule/epg/xlist",
    summary="Read TV Guide X-List style EPG export",
    responses={404: {"description": "Channel not found"}},
)
def read_channel_epg_xlist(channel_id: str) -> Response:
    profile = _profile_or_404(channel_id)
    items = read_channel_schedule_feed(profile.channel_id)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<tvguide-xlist generated-by="CivicCast">',
        f'  <channel id="{escape(profile.channel_id)}">',
        f"    <display-name>{escape(profile.branding.display_name)}</display-name>",
        "  </channel>",
    ]
    for item in items:
        if item.ends_at is None:
            continue
        start = item.starts_at.astimezone(UTC).strftime("%Y%m%d%H%M%S +0000")
        stop = item.ends_at.astimezone(UTC).strftime("%Y%m%d%H%M%S +0000")
        lines.extend(
            [
                f'  <programme start="{start}" stop="{stop}" channel="{escape(item.channel_id)}">',
                f"    <title>{escape(item.title)}</title>",
                f"    <category>{escape(item.kind)}</category>",
                f'    <episode-num system="civiccast">{escape(item.item_id)}</episode-num>',
                "  </programme>",
            ]
        )
    lines.append("</tvguide-xlist>")
    return Response("\n".join(lines) + "\n", media_type="application/xml")


@public_router.get(
    "/channels/{channel_id}/catalog",
    response_model=VodCatalogResponse,
    summary="Read app-platform VOD catalog",
    responses={404: {"description": "Channel not found"}},
)
def read_channel_vod_catalog(
    channel_id: str,
    playlist_id: str | None = Query(default=None, max_length=120),
    topic: str | None = Query(default=None, max_length=80),
    series: str | None = Query(default=None, max_length=160),
    publish_state: str | None = Query(default=None, max_length=40),
    sort: CatalogSort = Query(
        default="published_at_desc",
        pattern="^(published_at_desc|published_at_asc|title_asc)$",
    ),
    limit: int = Query(default=20, ge=1, le=100),
    playback_store: PlaybackPolicyStore = Depends(get_playback_policy_store),
) -> VodCatalogResponse:
    profile = _profile_or_404(channel_id)
    now = datetime.now(UTC).replace(microsecond=0)
    playlists = _smart_playlists(profile)
    items = _apply_playback_policies(_catalog_items(profile, now), playback_store)
    selected_playlist = None
    if playlist_id is not None:
        selected_playlist = _playlist_or_404(playlist_id, playlists)
        items = _apply_playlist(items, selected_playlist)
    if topic is not None:
        items = [item for item in items if topic in item.topics]
    if series is not None:
        items = [item for item in items if item.series == series]
    if publish_state is not None:
        items = [item for item in items if item.publish_state == publish_state]
    items = _sort_catalog_items(items, sort)[:limit]
    return VodCatalogResponse(
        generated_at=now,
        channel_id=profile.channel_id,
        items=items,
        playlists=playlists,
        facets=_catalog_facets(items),
    )


@public_router.get(
    "/channels/{channel_id}/catalog/playlists",
    response_model=list[SmartPlaylistDefinition],
    summary="Read app-platform smart playlist definitions",
    responses={404: {"description": "Channel not found"}},
)
def read_channel_smart_playlists(channel_id: str) -> list[SmartPlaylistDefinition]:
    profile = _profile_or_404(channel_id)
    return _smart_playlists(profile)


@public_router.get(
    "/channels/{channel_id}/now-next",
    response_model=NowNextState,
    summary="Read seeded app-platform now/next state",
    responses={404: {"description": "Channel not found"}},
)
def read_channel_now_next(channel_id: str) -> NowNextState:
    profile = _profile_or_404(channel_id)
    items = read_channel_schedule_feed(profile.channel_id)
    return NowNextState(
        generated_at=datetime.now(UTC),
        channel_id=profile.channel_id,
        current=items[0],
        next=items[1],
        fallback_active=False,
        proof_boundary="seeded-app-platform-now-next",
    )


def get_analytics_store(request: Request) -> AnalyticsStoreProtocol:
    return cast_analytics_store(
        resolve_app_store(request, "analytics_store", surface="Analytics store")
    )


def enforce_analytics_body_size(request: Request) -> None:
    raw_length = request.headers.get("content-length")
    if raw_length is None:
        return
    try:
        content_length = int(raw_length)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid content length.",
        ) from None
    if content_length > _ANALYTICS_MAX_CONTENT_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Analytics event payload is too large.",
        )


def require_public_analytics_ingest(
    analytics_key: str | None = Header(default=None, alias="X-CivicCast-Analytics-Key"),
    origin: str | None = Header(default=None, alias="Origin"),
) -> AnalyticsIngestAccess:
    expected = os.environ.get("CIVICCAST_PUBLIC_ANALYTICS_KEY")
    allowed_origins = _allowed_public_analytics_origins()
    if not expected and not allowed_origins:
        # Not configured: accept and drop rather than 503 every page load.
        return AnalyticsIngestAccess(trusted=False, configured=False)
    if analytics_key is not None:
        if expected and hmac.compare_digest(analytics_key, expected):
            return AnalyticsIngestAccess(trusted=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid public analytics ingest key.",
        )
    if origin is not None and origin in allowed_origins:
        return AnalyticsIngestAccess(trusted=False)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Origin is not allowed to submit public analytics events.",
    )


@public_router.post(
    "/analytics/events",
    response_model=AnalyticsIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest privacy-safe public app analytics",
    dependencies=[Depends(enforce_analytics_body_size)],
)
def ingest_public_app_analytics(
    request: Request,
    event: AnalyticsEvent,
    access: AnalyticsIngestAccess = Depends(require_public_analytics_ingest),
    store: AnalyticsStoreProtocol = Depends(get_analytics_store),
) -> AnalyticsIngestResponse:
    if not access.configured:
        # Best-effort telemetry with analytics disabled: accept the event and
        # drop it (store nothing) so a default station's portal load stays clean.
        return AnalyticsIngestResponse(
            event_id=event.event_id,
            retained_fields=[],
            proof_boundary="analytics-disabled-event-dropped",
        )
    _enforce_analytics_rate_limit(request, access)
    store.record_event(event)
    return AnalyticsIngestResponse(
        event_id=event.event_id,
        retained_fields=retained_analytics_fields(),
        proof_boundary="privacy-safe-contract-no-direct-viewer-identifiers",
    )


def _enforce_analytics_rate_limit(request: Request, access: AnalyticsIngestAccess) -> None:
    limit = _analytics_rate_limit_per_minute(trusted=access.trusted)
    if limit <= 0:
        return
    key = _client_rate_key(request)
    now = time.monotonic()
    buckets = getattr(request.app.state, "public_analytics_rate_limit_buckets", None)
    if not isinstance(buckets, dict):
        buckets = {}
        request.app.state.public_analytics_rate_limit_buckets = buckets
    max_buckets = _analytics_max_rate_limit_buckets()
    if _analytics_rate_limit_prune_due(
        request.app.state,
        now=now,
        bucket_count=len(buckets),
        max_buckets=max_buckets,
    ):
        prune_analytics_rate_limit_buckets(
            buckets,
            now=now,
            window_seconds=_ANALYTICS_RATE_LIMIT_WINDOW_SECONDS,
            max_buckets=max_buckets,
        )
    if key not in buckets and len(buckets) >= _analytics_max_rate_limit_buckets():
        _evict_oldest_rate_limit_bucket(buckets)
    bucket = buckets.setdefault(key, AnalyticsRateLimitBucket(requests=[], touched_at=now))
    bucket.requests = [
        started_at
        for started_at in bucket.requests
        if now - started_at < _ANALYTICS_RATE_LIMIT_WINDOW_SECONDS
    ]
    bucket.touched_at = now
    if len(bucket.requests) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Public analytics ingest rate limit exceeded.",
        )
    bucket.requests.append(now)


def prune_analytics_rate_limit_buckets(
    buckets: dict[str, AnalyticsRateLimitBucket],
    *,
    now: float,
    window_seconds: int,
    max_buckets: int,
) -> None:
    stale_keys: list[str] = []
    for key, bucket in buckets.items():
        bucket.requests = [
            started_at for started_at in bucket.requests if now - started_at < window_seconds
        ]
        if not bucket.requests:
            stale_keys.append(key)
    for key in stale_keys:
        buckets.pop(key, None)
    while len(buckets) > max_buckets:
        _evict_oldest_rate_limit_bucket(buckets)


def _evict_oldest_rate_limit_bucket(buckets: dict[str, AnalyticsRateLimitBucket]) -> None:
    if not buckets:
        return
    oldest_key = min(buckets, key=lambda key: buckets[key].touched_at)
    buckets.pop(oldest_key, None)


def _analytics_rate_limit_prune_due(
    state: Any,
    *,
    now: float,
    bucket_count: int,
    max_buckets: int,
) -> bool:
    last_pruned = getattr(state, "public_analytics_rate_limit_last_pruned_at", None)
    if not isinstance(last_pruned, int | float):
        state.public_analytics_rate_limit_last_pruned_at = now
        return True
    if bucket_count > max_buckets:
        state.public_analytics_rate_limit_last_pruned_at = now
        return True
    if now - last_pruned >= _ANALYTICS_RATE_LIMIT_PRUNE_INTERVAL_SECONDS:
        state.public_analytics_rate_limit_last_pruned_at = now
        return True
    return False


def _analytics_rate_limit_per_minute(*, trusted: bool) -> int:
    env_name = (
        "CIVICCAST_TRUSTED_ANALYTICS_RATE_LIMIT_PER_MINUTE"
        if trusted
        else "CIVICCAST_PUBLIC_ANALYTICS_RATE_LIMIT_PER_MINUTE"
    )
    default = (
        _ANALYTICS_DEFAULT_TRUSTED_RATE_LIMIT_PER_MINUTE
        if trusted
        else _ANALYTICS_DEFAULT_RATE_LIMIT_PER_MINUTE
    )
    raw_limit = os.environ.get(env_name)
    if raw_limit is None:
        return default
    try:
        return int(raw_limit)
    except ValueError:
        return default


def _analytics_max_rate_limit_buckets() -> int:
    raw_limit = os.environ.get("CIVICCAST_ANALYTICS_RATE_LIMIT_MAX_BUCKETS")
    if raw_limit is None:
        return _ANALYTICS_DEFAULT_MAX_RATE_LIMIT_BUCKETS
    try:
        return max(1, int(raw_limit))
    except ValueError:
        return _ANALYTICS_DEFAULT_MAX_RATE_LIMIT_BUCKETS


def _allowed_public_analytics_origins() -> set[str]:
    raw_origins = os.environ.get("CIVICCAST_PUBLIC_ANALYTICS_ALLOWED_ORIGINS", "")
    return {origin.strip() for origin in raw_origins.split(",") if origin.strip()}


def public_analytics_ingest_configured() -> bool:
    """Whether the deployment collects audience telemetry at all (S14 §5).

    Mirrors ``require_public_analytics_ingest``'s "not configured" branch —
    no ``CIVICCAST_PUBLIC_ANALYTICS_KEY`` and no allowed origins means every
    beacon is accepted-and-dropped, so Viewer Count / Time Viewed never
    populate. The analytics dashboard reads this (via
    ``AnalyticsReport.ingest_configured``) to show an honest "telemetry is
    off" empty state instead of a dashboard that looks broken. As-run /
    proof-of-performance reports (``civiccast/reporting``) are unaffected —
    they read the program log, not the beacon.
    """

    return bool(os.environ.get("CIVICCAST_PUBLIC_ANALYTICS_KEY")) or bool(
        _allowed_public_analytics_origins()
    )


def _client_rate_key(request: Request) -> str:
    if request.client is None:
        return "anonymous-client"
    client_ip = _analytics_client_ip_for_rate_limit(
        peer_host=request.client.host,
        forwarded_for=request.headers.get("x-forwarded-for"),
    )
    return f"client:{client_ip}"


def _analytics_client_ip_for_rate_limit(
    *,
    peer_host: str,
    forwarded_for: str | None,
) -> str:
    try:
        peer_ip = ip_address(peer_host)
    except ValueError:
        return peer_host
    if not _analytics_peer_can_supply_forwarded_for(peer_ip):
        return str(peer_ip)
    nearest_forwarded_ip: IPv4Address | IPv6Address | None = None
    for forwarded_host in reversed(_parse_forwarded_for(forwarded_for)):
        try:
            forwarded_ip = ip_address(forwarded_host)
        except ValueError:
            continue
        if nearest_forwarded_ip is None:
            nearest_forwarded_ip = forwarded_ip
        if not _analytics_peer_can_supply_forwarded_for(forwarded_ip):
            return str(forwarded_ip)
    if nearest_forwarded_ip is not None:
        return str(nearest_forwarded_ip)
    return str(peer_ip)


def _analytics_peer_can_supply_forwarded_for(peer_ip: IPv4Address | IPv6Address) -> bool:
    if (
        peer_ip.is_loopback
        or peer_ip.is_private
        or peer_ip.is_link_local
        or peer_ip.is_reserved
        or peer_ip.is_unspecified
    ):
        return True
    return any(peer_ip in network for network in _analytics_trusted_proxy_networks())


def _parse_forwarded_for(forwarded_for: str | None) -> list[str]:
    if forwarded_for is None:
        return []
    return [entry.strip() for entry in forwarded_for.split(",") if entry.strip()]


def _analytics_trusted_proxy_networks() -> list[IPv4Network | IPv6Network]:
    raw_networks = os.environ.get("CIVICCAST_ANALYTICS_TRUSTED_PROXY_CIDRS", "")
    networks: list[IPv4Network | IPv6Network] = []
    for raw_network in raw_networks.split(","):
        network = raw_network.strip()
        if not network:
            continue
        try:
            networks.append(ip_network(network, strict=False))
        except ValueError:
            continue
    return networks


def _profile_or_404(channel_id: str) -> ChannelProfile:
    for profile in default_channel_profiles():
        if profile.channel_id == channel_id:
            return profile
    raise _channel_not_found(channel_id)


def _channel_not_found(channel_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Channel {channel_id!r} not found.",
    )


def _seeded_current_block(now_next: ChannelNowNext) -> PlayoutBlock:
    """The sample contract always seeds a current block; say so loudly if not.

    An explicit raise, not ``assert``: ``python -O`` strips asserts, and this
    guards a public endpoint.
    """
    if now_next.current is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Seeded channel feed has no current block.",
        )
    return now_next.current


def _live_state_from_block(profile: ChannelProfile, block: PlayoutBlock) -> LiveState:
    if block.status == "fallback" or block.kind == "fallback":
        return LiveState(
            state="fallback",
            channel_id=profile.channel_id,
            title=block.title,
            live_session_id=block.block_id,
            playback_url=_playback_url_for_block(profile, block),
            source_ref=block.source_ref,
            started_at=block.starts_at,
            proof_boundary="playout-plan-to-public-live-state",
            fallback_reason=block.failover_reason or "channel fallback is active",
        )
    return LiveState(
        state="on_air",
        channel_id=profile.channel_id,
        title=block.title,
        live_session_id=block.block_id,
        playback_url=_playback_url_for_block(profile, block),
        source_ref=block.source_ref,
        started_at=block.starts_at,
        caption_tracks=[
            CaptionTrack(
                track_id="live-captions",
                label="Live captions",
                language="en",
                url=f"/api/public/channels/{profile.channel_id}/captions.vtt",
                kind="generated",
                default=True,
            )
        ]
        if block.caption_refs
        else [],
        audio_tracks=[
            AudioTrack(
                track_id="program-audio",
                label="Program audio",
                language="en",
                url=f"/api/public/channels/{profile.channel_id}/audio.m3u8",
                kind="embedded",
                default=True,
            )
        ],
        dvr_window_seconds=1800,
        proof_boundary="playout-plan-to-public-live-state",
    )


def _schedule_item_from_block(profile: ChannelProfile, block: PlayoutBlock) -> ScheduleFeedItem:
    kind = _schedule_kind_for_block(block)
    starts_at = block.starts_at.replace(microsecond=0)
    return ScheduleFeedItem(
        item_id=block.block_id,
        channel_id=profile.channel_id,
        kind=kind,
        title=block.title,
        description=f"{profile.branding.display_name} {kind} schedule item.",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(seconds=block.duration_seconds),
        duration_seconds=block.duration_seconds,
        catalog_item_id=_catalog_item_id_for_block(block),
        live_state_url=f"/api/public/app/channels/{profile.channel_id}/live",
        playback_url=_playback_url_for_block(profile, block),
        captions_available=bool(block.caption_refs),
        public_record_required=kind in {"live", "premiere", "rerun"},
        proof_boundary="playout-plan-to-public-schedule-feed",
    )


def _schedule_kind_for_block(block: PlayoutBlock) -> ScheduleFeedKind:
    if block.kind == "file":
        return "premiere"
    if block.kind == "slate":
        return "fallback"
    if block.kind in {"live", "rerun", "bulletin", "fallback"}:
        return block.kind
    return "fallback"


def _catalog_item_id_for_block(block: PlayoutBlock) -> str | None:
    if block.source_ref.startswith("asset-"):
        return block.source_ref.removeprefix("asset-")
    return None


def _playback_url_for_block(profile: ChannelProfile, block: PlayoutBlock) -> str:
    if block.kind == "live":
        return public_live_manifest_path(profile.channel_id)
    return f"/api/public/assets/{block.source_ref}/embed.m3u8"


def _catalog_items(profile: ChannelProfile, now: datetime) -> list[VodCatalogItem]:
    return [
        VodCatalogItem(
            item_id=f"{profile.channel_id}-sample-meeting",
            asset_id=f"{profile.channel_id}-sample-meeting",
            channel_id=profile.channel_id,
            title=f"{profile.branding.display_name} sample meeting",
            description="Approved public-record meeting replay for app-platform catalog proof.",
            series=f"{profile.branding.short_name} meetings",
            topics=["meeting", profile.kind, "public-record"],
            playlist_ids=[f"{profile.channel_id}-recent", f"{profile.channel_id}-public-records"],
            playback_url=f"/api/public/assets/{profile.channel_id}-sample-meeting/embed.m3u8",
            poster_url=f"/static/posters/{profile.channel_id}-meeting.png",
            thumbnail_url=f"/static/thumbnails/{profile.channel_id}-meeting.jpg",
            duration_seconds=3600,
            published_at=now - timedelta(days=1),
            publish_state="published",
            captions=[
                CaptionTrack(
                    track_id="english",
                    label="English captions",
                    language="en",
                    url=(f"/api/public/assets/{profile.channel_id}-sample-meeting/captions/en.vtt"),
                    kind="generated",
                    default=True,
                    confidence=0.91,
                )
            ],
            audio_tracks=[
                AudioTrack(
                    track_id="program-audio",
                    label="Program audio",
                    language="en",
                    url=(f"/api/public/assets/{profile.channel_id}-sample-meeting/audio/en.m3u8"),
                    kind="embedded",
                    default=True,
                )
            ],
            chapters=[
                ChapterMarker(
                    chapter_id="call-to-order",
                    title="Call to order",
                    start_seconds=0,
                    end_seconds=180,
                    source="operator",
                    approved=True,
                ),
                ChapterMarker(
                    chapter_id="public-comment",
                    title="Public comment",
                    start_seconds=900,
                    end_seconds=1500,
                    source="operator",
                    approved=True,
                ),
            ],
            playback_policy=PlaybackPolicy(
                public_record_required=True,
                public_archive_complete=True,
            ),
        ),
        VodCatalogItem(
            item_id=f"{profile.channel_id}-bulletin-update",
            asset_id=f"{profile.channel_id}-bulletin-update",
            channel_id=profile.channel_id,
            title=f"{profile.branding.display_name} bulletin update",
            description="Short station bulletin for between-meeting playback.",
            series="Station bulletins",
            topics=["bulletin", profile.kind],
            playlist_ids=[f"{profile.channel_id}-recent", f"{profile.channel_id}-bulletins"],
            playback_url=f"/api/public/assets/{profile.channel_id}-bulletin-update/embed.m3u8",
            poster_url=f"/static/posters/{profile.channel_id}-bulletin.png",
            thumbnail_url=f"/static/thumbnails/{profile.channel_id}-bulletin.jpg",
            duration_seconds=420,
            published_at=now - timedelta(days=3),
            publish_state="published",
            playback_policy=PlaybackPolicy(),
        ),
        VodCatalogItem(
            item_id=f"{profile.channel_id}-producer-magazine",
            asset_id=f"{profile.channel_id}-producer-magazine",
            channel_id=profile.channel_id,
            title=f"{profile.branding.short_name} community magazine",
            description="Submitted producer program awaiting final publish approval.",
            series="Community magazine",
            topics=["producer", "community", profile.kind],
            playlist_ids=[f"{profile.channel_id}-producer"],
            poster_url=f"/static/posters/{profile.channel_id}-producer.png",
            thumbnail_url=f"/static/thumbnails/{profile.channel_id}-producer.jpg",
            duration_seconds=1800,
            published_at=None,
            publish_state="scheduled",
            playback_policy=PlaybackPolicy(),
        ),
    ]


def _apply_playback_policies(
    items: list[VodCatalogItem],
    playback_store: PlaybackPolicyStore,
) -> list[VodCatalogItem]:
    return [
        item.model_copy(
            update={
                "playback_policy": _catalog_playback_policy(
                    item,
                    playback_store.effective_policy(item.asset_id, item.channel_id),
                )
            }
        )
        for item in items
    ]


def _catalog_playback_policy(
    item: VodCatalogItem,
    policy: PlaybackPolicyConfig,
) -> PlaybackPolicy:
    base_policy = item.playback_policy
    first_creative = policy.preroll.creatives[0] if policy.preroll.creatives else None
    legacy_preroll = (
        PrerollPolicy()
        if first_creative is None
        else PrerollPolicy(
            kind=first_creative.kind,
            asset_url=first_creative.asset_url,
            duration_seconds=first_creative.duration_seconds,
            skippable_after_seconds=first_creative.skippable_after_seconds,
        )
    )
    if base_policy.public_record_required or base_policy.public_archive_complete:
        return PlaybackPolicy(
            access_tier="public",
            public_record_required=base_policy.public_record_required,
            public_archive_complete=base_policy.public_archive_complete,
            entitlement_required=None,
            preroll=legacy_preroll,
            preroll_sequence=policy.preroll,
        )
    return PlaybackPolicy(
        access_tier=policy.access_tier,
        public_record_required=base_policy.public_record_required or policy.public_record_required,
        public_archive_complete=base_policy.public_archive_complete
        or policy.public_archive_complete,
        entitlement_required=policy.invite_group_id or policy.oidc_provider_id,
        preroll=legacy_preroll,
        preroll_sequence=policy.preroll,
    )


def _smart_playlists(profile: ChannelProfile) -> list[SmartPlaylistDefinition]:
    return [
        SmartPlaylistDefinition(
            playlist_id=f"{profile.channel_id}-recent",
            label="Recent programs",
            description="Published items for this channel, newest first.",
            channel_id=profile.channel_id,
            rules=[
                SmartPlaylistRule(field="channel_id", value=profile.channel_id),
                SmartPlaylistRule(field="publish_state", value="published"),
            ],
            sort="published_at_desc",
            limit=20,
        ),
        SmartPlaylistDefinition(
            playlist_id=f"{profile.channel_id}-public-records",
            label="Public records",
            description="Published meeting records that must remain publicly playable.",
            channel_id=profile.channel_id,
            rules=[
                SmartPlaylistRule(field="channel_id", value=profile.channel_id),
                SmartPlaylistRule(field="public_record_required", value=True),
            ],
            sort="published_at_desc",
            limit=50,
        ),
        SmartPlaylistDefinition(
            playlist_id=f"{profile.channel_id}-bulletins",
            label="Bulletins",
            description="Station bulletin videos for app shelves and between-stream display.",
            channel_id=profile.channel_id,
            rules=[
                SmartPlaylistRule(field="channel_id", value=profile.channel_id),
                SmartPlaylistRule(field="topic", operator="contains", value="bulletin"),
                SmartPlaylistRule(field="publish_state", value="published"),
            ],
            sort="title_asc",
            limit=20,
        ),
    ]


def _playlist_or_404(
    playlist_id: str,
    playlists: list[SmartPlaylistDefinition],
) -> SmartPlaylistDefinition:
    for playlist in playlists:
        if playlist.playlist_id == playlist_id:
            return playlist
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Playlist {playlist_id!r} not found.",
    )


def _apply_playlist(
    items: list[VodCatalogItem],
    playlist: SmartPlaylistDefinition,
) -> list[VodCatalogItem]:
    selected = items
    for rule in playlist.rules:
        if rule.field == "channel_id":
            selected = [item for item in selected if item.channel_id == rule.value]
        elif rule.field == "series":
            selected = [item for item in selected if item.series == rule.value]
        elif rule.field == "topic":
            selected = [item for item in selected if rule.value in item.topics]
        elif rule.field == "publish_state":
            selected = [item for item in selected if item.publish_state == rule.value]
        elif rule.field == "public_record_required":
            selected = [
                item
                for item in selected
                if item.playback_policy.public_record_required == rule.value
            ]
    return _sort_catalog_items(selected, playlist.sort)[: playlist.limit]


def _sort_catalog_items(
    items: list[VodCatalogItem],
    sort: CatalogSort,
) -> list[VodCatalogItem]:
    if sort == "title_asc":
        return sorted(items, key=lambda item: (item.title.casefold(), item.item_id))
    if sort == "published_at_asc":
        return sorted(
            items,
            key=lambda item: (item.published_at or datetime.max.replace(tzinfo=UTC), item.item_id),
        )
    return sorted(
        items,
        key=lambda item: (item.published_at or datetime.min.replace(tzinfo=UTC), item.item_id),
        reverse=True,
    )


def _catalog_facets(items: list[VodCatalogItem]) -> dict[str, list[str]]:
    topics = sorted({topic for item in items for topic in item.topics})
    series = sorted({item.series for item in items if item.series})
    publish_states = sorted({str(item.publish_state) for item in items})
    channels = sorted({item.channel_id for item in items})
    return {
        "channel": channels,
        "topic": topics,
        "series": series,
        "publish_state": publish_states,
    }
