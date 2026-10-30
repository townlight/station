# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Public router that serves packaged VOD + live HLS output over HTTP.

``civiccast.stream.packager`` writes a finished HLS package (multivariant
manifest + per-rendition variant playlists + segments) to a local directory
next to the source recording. Nothing served that tree to a browser —
``AssetMetadata.manifest_url`` was only ever non-null when an operator wired
up a CDN (Stage C) or set ``CIVICCAST_LIVE_MANIFEST_BASE_URL`` by hand. The
``/media/vod`` mount closes that gap: it serves the package tree for a
finalized asset at ``/media/vod/{asset_id}/...`` so a stock install has
something to play out of the box (see
``finalization_worker._servable_manifest_url``, which points
``manifest_url`` at this router by default when no CDN is set).

Lookup key is ``asset_id`` (the public, stable identity), resolved to the
package directory via ``LiveFinalizationJob.local_package_manifest_path`` —
the exact absolute path the worker wrote after packaging, so this router
never re-derives the recording-target resolution rules and cannot drift
from where the worker actually wrote the files.

``/media/live`` is the Sprint 0.4 live-HLS sibling: ``civiccast.egress.sinks
.HlsSink`` (a channel egress sink, wired through the persistent ffmpeg
encoder same as any other output) writes a rolling live manifest + segments
to a local directory. Lookup key is ``channel_id``, resolved to that
directory via the channel's configured ``hls`` egress sink URI (the egress
store, not a per-broadcast DB row — the live directory is a fixed,
continuously-overwritten location per channel, not a write-once package).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.orm import Session

from civiccast.common.trusted_proxy import resolve_client_ip
from civiccast.db import get_session
from civiccast.egress.headend import resolve_local_hls_directory
from civiccast.egress.router import get_egress_store
from civiccast.egress.store import EgressStore
from civiccast.live.models import LiveFinalizationJob
from civiccast.live.surge_service import get_surge_switch_service
from civiccast.schedule.models import Asset
from civiccast.schedule.paths import resolve_upload_root, resolve_vod_package_root
from civiccast.stream import live_manifest_lock

_DB_NOT_READY = "Durable storage is not ready yet."
_LOG = logging.getLogger(__name__)


def get_optional_session() -> Iterator[Session | None]:
    """Yield a session, or ``None`` when durable storage is not CONFIGURED.

    GauntletGate QA-1: this route used ``Depends(get_session)`` directly, and
    ``civiccast.db.get_engine`` raises a bare ``RuntimeError`` when DATABASE_URL
    is unset. FastAPI turned that into an unhandled 500 before the handler's own
    documented "404s rather than 500s" promise could run.

    This dependency covers UNCONFIGURED storage only, and cannot cover more.
    Engine construction is lazy and opens no socket, so when DATABASE_URL IS set
    but Postgres is merely UNREACHABLE, ``get_session()`` succeeds here and the
    failure surfaces later, inside the handler, as ``OperationalError`` at the
    first ``session.execute``. That is what :func:`degrade_on_storage_failure`
    is for (GauntletGate rc18 PE-2026-07-22-1) -- the original fix rescued only
    ``RuntimeError`` and therefore missed every real outage, which is the case
    its own rationale claimed to cover.
    """
    try:
        yield from get_session()
    except RuntimeError:
        yield None


@contextmanager
def degrade_on_storage_failure() -> Iterator[None]:
    """Turn a live storage failure into the same 503 an absent store gives.

    To a resident the two are one thing: the recording will not play. They must
    answer identically rather than one degrading cleanly and the other raising a
    stack trace out of a public playback route.

    Deliberately narrow: only the SQLAlchemy connection/operational family is
    converted. A programming error, a bad query, or a constraint violation is a
    real fault and must keep surfacing as one rather than being disguised as
    "storage isn't ready".
    """

    try:
        yield
    except (OperationalError, InterfaceError, DBAPIError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_DB_NOT_READY,
        ) from exc


router = APIRouter(prefix="/media/vod", tags=["media"])
live_router = APIRouter(prefix="/media/live", tags=["media"])

# Segments are content-addressed by rendition dir + sequence number and are
# never rewritten in place once packaged (VOD, not live) — safe to cache
# forever. The manifest is small and equally immutable per asset (a repack
# writes a new file, but the URL for an existing completed asset never
# changes shape), but a short TTL guards against caching a manifest that
# was fetched mid-repackage.
_SEGMENT_CACHE_CONTROL = "public, max-age=31536000, immutable"
_MANIFEST_CACHE_CONTROL = "public, max-age=60"
# Live segments are also written once and never rewritten in place (the
# sliding window works by deleting old files, not mutating them) — same
# long-TTL immutable policy as VOD. The live manifest changes every segment
# interval (HlsSink.segment_seconds = 2s); a resident's player must re-poll
# promptly or drift behind live, so its cache window is a fraction of that.
_LIVE_MANIFEST_CACHE_CONTROL = "public, max-age=1, must-revalidate"

_CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/MP2T",
    ".m4s": "video/mp4",
    ".mp4": "video/mp4",
    # WebVTT caption tracks live inside the same package tree (the offline
    # caption job writes ``captions/<lang>/seg*.vtt`` plus the flat
    # ``captions/captions.vtt``). Serving them as octet-stream is what
    # makes a browser download a caption file instead of rendering it as a
    # subtitle track, so the type is declared, not defaulted.
    ".vtt": "text/vtt",
}


def _serve_from_directory(
    base_dir: Path,
    file_path: str,
    *,
    manifest_cache_control: str,
) -> FileResponse:
    """Serve one manifest/segment file from an HLS output directory.

    Shared by the VOD and live routes: same path-traversal guard, same
    content-type dispatch. Only the manifest's ``Cache-Control`` differs
    (VOD manifests are effectively immutable per asset; live manifests
    change every segment interval — see the module docstring).
    """
    candidate, content_type, cache_control = _resolve_media_file(
        base_dir, file_path, manifest_cache_control=manifest_cache_control
    )
    return FileResponse(
        candidate,
        media_type=content_type,
        headers={"Cache-Control": cache_control},
    )


def _resolve_media_file(
    base_dir: Path,
    file_path: str,
    *,
    manifest_cache_control: str,
) -> tuple[Path, str, str]:
    """Apply the shared containment check and return response metadata."""
    candidate = (base_dir / file_path).resolve()
    if not candidate.is_relative_to(base_dir) or not candidate.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Media file not found: {file_path}",
        )
    content_type = _CONTENT_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
    cache_control = (
        manifest_cache_control if candidate.suffix.lower() == ".m3u8" else _SEGMENT_CACHE_CONTROL
    )
    return candidate, content_type, cache_control


def _serve_live_from_directory(base_dir: Path, file_path: str) -> Response:
    """Snapshot a live manifest under the publisher lock; stream segments as files."""
    candidate, content_type, cache_control = _resolve_media_file(
        base_dir, file_path, manifest_cache_control=_LIVE_MANIFEST_CACHE_CONTROL
    )
    if candidate.suffix.lower() != ".m3u8":
        return FileResponse(
            candidate,
            media_type=content_type,
            headers={"Cache-Control": cache_control},
        )
    try:
        with live_manifest_lock.LIVE_MANIFEST_LOCK:
            body = candidate.read_bytes()
    except FileNotFoundError as exc:
        # The publisher/cleanup can remove the path between containment check
        # and open. Match the route's ordinary missing-file behavior.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Media file not found: {file_path}",
        ) from exc
    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": cache_control},
    )


def _package_dir_for_asset(asset_id: str, session: Session) -> Path:
    """Resolve the on-disk package directory for a finalized asset.

    404s (rather than 500s) for any asset with no completed finalization
    job on record — an unpackaged or live/uploaded-only asset simply has
    nothing under this router yet.
    """
    is_published = session.execute(
        select(Asset.asset_id)
        .where(Asset.asset_id == asset_id)
        .where(Asset.published_at.is_not(None))
        .limit(1)
    ).scalar_one_or_none()
    if is_published is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No published media found for asset: {asset_id}",
        )

    manifest_path_str = session.execute(
        select(LiveFinalizationJob.local_package_manifest_path)
        .where(LiveFinalizationJob.asset_id == asset_id)
        .where(LiveFinalizationJob.local_package_manifest_path.is_not(None))
        .limit(1)
    ).scalar_one_or_none()
    if manifest_path_str:
        return Path(manifest_path_str).resolve().parent

    uploaded = session.execute(
        select(Asset.file_path, Asset.manifest_url).where(Asset.asset_id == asset_id)
    ).one_or_none()
    if uploaded is not None:
        file_path, manifest_url = uploaded
        expected_suffix = f"/media/vod/{asset_id}/playlist.m3u8"
        if file_path and manifest_url and str(manifest_url).endswith(expected_suffix):
            upload_root = resolve_upload_root()
            if upload_root is not None:
                package_root = resolve_vod_package_root(upload_root)
                trusted_package_dir = (package_root / asset_id).resolve()
                if (
                    package_root.is_relative_to(upload_root)
                    and trusted_package_dir.is_relative_to(package_root)
                    and (trusted_package_dir / "playlist.m3u8").is_file()
                ):
                    return trusted_package_dir

            # Compatibility for packages created before the asset-owned rc14
            # package root. New packaging never writes to this shared path.
            package_dir = (Path(file_path).expanduser().resolve().parent / "hls").resolve()
            if (package_dir / "playlist.m3u8").is_file():
                return package_dir

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"No packaged media found for asset: {asset_id}",
    )


@router.get("/{asset_id}/{file_path:path}")
def get_media_file(
    asset_id: str,
    file_path: str,
    session: Session | None = Depends(get_optional_session),
) -> FileResponse:
    """Serve one file from a packaged VOD asset's HLS output tree.

    ``file_path`` is the manifest/segment path relative to the package
    directory (e.g. ``playlist.m3u8``, ``720p/playlist.m3u8``,
    ``720p/seg003.ts``) — exactly the relative URIs the packager already
    writes into its manifests (``civiccast.stream.packager``), so no URL
    rewriting is needed between what ffmpeg wrote and what this serves.
    """
    if session is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    with degrade_on_storage_failure():
        package_dir = _package_dir_for_asset(asset_id, session)
    return _serve_from_directory(
        package_dir, file_path, manifest_cache_control=_MANIFEST_CACHE_CONTROL
    )


def _live_dir_for_channel(channel_id: str, egress_store: EgressStore | None) -> Path:
    """Resolve the on-disk live-HLS output directory for a channel.

    404s (not 500s) when durable storage isn't configured, the channel has
    no egress config, or the channel has no ``hls`` sink — a channel that
    has never been wired for local live-HLS output simply has nothing under
    this router yet (the same "nothing configured -> 404" posture as
    ``_package_dir_for_asset`` for an unpackaged VOD asset).

    Also 404s when the sink names a folder outside the root the station is
    allowed to serve (``resolve_local_hls_directory``: the egress work dir or
    ``CIVICCAST_LIVE_HLS_ROOT``; never UNC, never relative, never a path --
    or a junction/symlink -- that resolves outside). This route is public and
    unauthenticated, so the containment check lives HERE, at serve time, on
    the resolved path (review round 2 delta, BLOCKER 2): the preset route
    and the config PUT both refuse such a sink up front for a readable 422,
    but a third writer -- or a junction swapped in after the config was
    written -- must not be able to re-point this file server anywhere.
    """
    if egress_store is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No live HLS output configured for channel: {channel_id}",
        )
    config = egress_store.get_config(channel_id)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No live HLS output configured for channel: {channel_id}",
        )
    hls_sink = next((sink for sink in config.sinks if sink.kind == "hls"), None)
    if hls_sink is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No live HLS output configured for channel: {channel_id}",
        )
    try:
        return resolve_local_hls_directory(hls_sink.uri)
    except ValueError as exc:
        _LOG.warning(
            "refusing to serve /media/live/%s: the hls sink folder is not servable (%s)",
            channel_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No live HLS output configured for channel: {channel_id}",
        ) from exc


@live_router.get("/{channel_id}/{file_path:path}")
def get_live_media_file(
    channel_id: str,
    file_path: str,
    request: Request,
    egress_store: EgressStore | None = Depends(get_egress_store),
) -> Response:
    """Serve one file from a channel's rolling live-HLS output directory.

    ``file_path`` is the manifest/segment path relative to the directory
    ``civiccast.egress.sinks.HlsSink`` writes into (``playlist.m3u8``,
    ``seg000000042.ts``) — the sink's own hls muxer args, so no URL
    rewriting is needed between what ffmpeg wrote and what this serves.

    A manifest GET also feeds the surge switch: a live player re-polls the
    playlist every segment, so distinct clients polling it approximate the
    concurrent-viewer count the switch keys on.
    """
    if file_path.endswith(".m3u8"):
        surge = get_surge_switch_service(request)
        if surge is not None:
            # resolve_client_ip walks X-Forwarded-For back to the real client
            # through trusted proxies -- essential here: the CDN-fronted
            # deployment this switch targets makes every poll arrive from a
            # handful of edge IPs, so the raw peer IP would undercount to ~1.
            surge.observe(channel_id, resolve_client_ip(request))
    with degrade_on_storage_failure():
        live_dir = _live_dir_for_channel(channel_id, egress_store)
    return _serve_live_from_directory(live_dir, file_path)


__all__ = ["live_router", "router"]
