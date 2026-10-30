# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""FastAPI routers for the schedule module's public + staff endpoints.

Sprint 0.3 task 2 wires three HTTP endpoints over the AssetStore Protocol:

  - GET  /api/public/assets             — list every asset (200 + JSON array)
  - GET  /api/public/assets/{asset_id}  — lookup by id (200 + AssetMetadata or 404)
  - POST /api/staff/assets              — create one asset (201 + canonical asset,
                                            422 invalid payload, 409 duplicate id)

The dependency ``get_asset_store`` reads the app-owned store bundle created by ``civiccast.app.create_app``. Tests can still use FastAPI dependency overrides, but router modules do not own mutable default stores.

Per Decision 3 — duplicate ``asset_id`` raises
:class:`civiccast.vod.store.AssetAlreadyExistsError` from the store; the
POST handler catches the exception and translates it to
``HTTPException(409, ...)``, mirroring the GET 404 pattern of
``vod.router.get_embed``.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, cast
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from civiccast.auth.roles import require_any_role
from civiccast.live.recording_paths import REHEARSAL_RECORDING_TARGET_ID, local_recording_path
from civiccast.platform.stores import resolve_app_store
from civiccast.schedule.ingest import (
    FfmpegNotFoundError,
    FfprobeError,
    FfprobeNotFoundError,
    UnsupportedFormatError,
    extract_thumbnail,
    hash_file,
    run_ffprobe,
    validate_ingest,
)
from civiccast.schedule.models import (
    ASSET_STATE_RECORDED,
    ASSET_STATE_VALIDATED,
    SCHEDULE_MODE_PREMIERE,
    SCHEDULE_STATE_PUBLISHED,
    AssetMetadataUpdate,
    ScheduleItemCreate,
    ScheduleItemResponse,
    StaffAssetRow,
    UploadedAssetResponse,
)
from civiccast.schedule.paths import resolve_upload_root, resolve_vod_package_root
from civiccast.stream.packager import PackagingError, pack_vod_asset
from civiccast.vod.models import AssetMetadata, public_manifest_reference
from civiccast.vod.store import AssetAlreadyExistsError, AssetStore

_PACKAGE_ADMISSION = threading.BoundedSemaphore(value=1)

# Media that has passed ffprobe validation and carries a readable local source
# file may be packaged for resident playback. Uploads land as ``validated``;
# scheduled/live captures finalize as ``recorded`` (see recording/runtime.py and
# live/finalization.py) — both run the same ffprobe + validate_ingest gate, so
# both are safe to package. This mirrors ``_AIRABLE_ASSET_STATES`` in
# schedule/commit_service.py, which already treats the two states together.
_PACKAGEABLE_ASSET_STATES: tuple[str, ...] = (ASSET_STATE_VALIDATED, ASSET_STATE_RECORDED)

_SOURCE_PLAYBACK_TTL = timedelta(minutes=15)
_SOURCE_PLAYBACK_MAX_GRANTS = 256
_SOURCE_PLAYBACK_COOKIE_PREFIX = "cc_source_playback_"
_SOURCE_PLAYBACK_LOCK = threading.Lock()
_SOURCE_PLAYBACK_ROLES = ("records_clerk", "meeting_operator", "support_admin")
_SOURCE_ASSET_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_SOURCE_HLS_CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".m4s": "video/mp4",
    ".mp4": "video/mp4",
    ".vtt": "text/vtt",
}
_SOURCE_PLAYBACK_HEADERS = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Vary": "Cookie",
}


@dataclass(frozen=True)
class _SourcePlaybackGrant:
    asset_id: str
    source_path: Path
    allowed_roots: tuple[Path, ...]
    package_root: Path | None
    source_offset_seconds: float
    expires_at: datetime


source_media_router = APIRouter(prefix="/media/staff-source", tags=["staff", "media"])


def _source_playback_now() -> datetime:
    return datetime.now(UTC)


def _source_playback_cookie_name(asset_id: str) -> str:
    asset_hash = hashlib.sha256(asset_id.encode("utf-8")).hexdigest()[:16]
    return f"{_SOURCE_PLAYBACK_COOKIE_PREFIX}{asset_hash}"


def _source_playback_store(request: Request) -> OrderedDict[str, _SourcePlaybackGrant]:
    """Return a lazily created, process-local bounded grant map for this app."""
    with _SOURCE_PLAYBACK_LOCK:
        grants = getattr(request.app.state, "source_playback_grants", None)
        if not isinstance(grants, OrderedDict):
            grants = OrderedDict()
            request.app.state.source_playback_grants = grants
        return cast(OrderedDict[str, _SourcePlaybackGrant], grants)


def _configured_source_roots(recording_target_store: Any) -> tuple[Path, ...]:
    roots: set[Path] = set()
    upload_root = resolve_upload_root()
    if upload_root is not None:
        roots.add(upload_root.resolve())
    if recording_target_store is not None:
        for target in recording_target_store.list():
            target_id = getattr(target, "recording_target_id", None)
            target_uri = getattr(target, "target_uri", None)
            if target_id == REHEARSAL_RECORDING_TARGET_ID or not isinstance(target_uri, str):
                continue
            local_path = local_recording_path(target_uri)
            if local_path is not None:
                roots.add(local_path.expanduser().resolve())
    return tuple(sorted(roots, key=str))


def _allowed_source_path(file_path: str | None, roots: tuple[Path, ...]) -> Path | None:
    if not file_path:
        return None
    source = Path(file_path).expanduser().resolve()
    if not source.is_file() or not any(source.is_relative_to(root) for root in roots):
        return None
    return source


def _source_media_type(source: Path) -> str:
    return _SOURCE_HLS_CONTENT_TYPES.get(source.suffix.lower()) or (
        mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    )


def _package_preview_marker_path(asset_id: str, package_parent: Path) -> Path:
    digest = hashlib.sha256(asset_id.encode("utf-8")).hexdigest()[:20]
    return package_parent / f".civiccast-preview-{digest}.json"


def _packaged_preview_root(
    asset: StaffAssetRow,
    source: Path,
    roots: tuple[Path, ...],
    finalization_worker: Any,
) -> tuple[Path | None, float]:
    """Find only a completed local package belonging to this exact asset."""
    candidates: list[tuple[Path, float, bool]] = []
    if asset.source_live_session_id and finalization_worker is not None:
        try:
            finalized = finalization_worker.get_status(asset.source_live_session_id)
        except Exception:
            finalized = None
        if (
            finalized is not None
            and finalized.state == "completed"
            and finalized.asset_id == asset.asset_id
            and finalized.packaged_trim_in_seconds == asset.trim_in_seconds
            and finalized.packaged_trim_out_seconds == asset.trim_out_seconds
            and finalized.local_package_manifest_path
        ):
            manifest = Path(finalized.local_package_manifest_path).expanduser().resolve()
            candidate = manifest.parent
            if (
                manifest.name == "playlist.m3u8"
                and manifest.is_file()
                and any(candidate.is_relative_to(root) for root in roots)
            ):
                candidates.append((candidate, max(0.0, float(asset.trim_in_seconds or 0.0)), False))

    upload_root = resolve_upload_root()
    if asset.manifest_url and upload_root is not None and source.is_relative_to(upload_root):
        package_root = resolve_vod_package_root(upload_root)
        if package_root.is_relative_to(upload_root):
            candidates.append(
                (
                    (package_root / asset.asset_id).resolve(),
                    max(0.0, float(asset.trim_in_seconds or 0.0)),
                    True,
                )
            )
    for candidate, source_offset, needs_marker in candidates:
        if not any(candidate.is_relative_to(root) for root in roots):
            continue
        manifest = (candidate / "playlist.m3u8").resolve()
        if not manifest.is_relative_to(candidate) or not manifest.is_file():
            continue
        if not needs_marker:
            return candidate, source_offset
        marker_path = _package_preview_marker_path(asset.asset_id, candidate.parent)
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Only current packages with known trim/source provenance may be
            # used to map transcript times onto the rebased HLS timeline.
            continue
        try:
            current = source.stat()
            fresh = (
                marker.get("asset_id") == asset.asset_id
                and marker.get("source_path") == str(source)
                and marker.get("source_size_bytes") == current.st_size
                and marker.get("source_mtime_ns") == current.st_mtime_ns
                and marker.get("trim_in_seconds") == asset.trim_in_seconds
                and marker.get("trim_out_seconds") == asset.trim_out_seconds
            )
        except (OSError, AttributeError):
            fresh = False
        if fresh:
            return candidate, source_offset
    return None, 0.0


def get_asset_store(request: Request) -> AssetStore:
    """FastAPI dependency for the active asset store.

    The schedule module owns its own dependency callable rather than importing
    ``vod.router.get_store``. Both resolve through the app-owned store bundle.
    """
    return cast(
        AssetStore, resolve_app_store(request, "asset_store", surface="Schedule asset store")
    )


def get_postgres_store() -> Any:
    """FastAPI dependency for a Postgres-backed asset store (upload + library).

    Returns None when ``DATABASE_URL`` is not configured; the upload handler
    translates None into HTTP 503. The app factory in ``civiccast.app``
    overrides this to return a real :class:`PostgresAssetStore` instance when
    ``DATABASE_URL`` is set. Typed as ``Any`` to avoid a circular import
    between router and store.
    """


def get_schedule_store() -> Any:
    """FastAPI dependency for the Postgres-backed schedule store.

    Returns None when ``DATABASE_URL`` is not configured; the schedule
    handlers translate None into HTTP 503. The app factory in
    ``civiccast.app`` overrides this to return a real
    :class:`PostgresScheduleStore` instance when ``DATABASE_URL`` is set.
    Typed as ``Any`` to avoid a circular import between router and store.
    """


public_router = APIRouter(prefix="/api/public", tags=["public"])
staff_router = APIRouter(prefix="/api/staff", tags=["staff"])

# Schedule-item role gate (legacy-findings fix): mirrors the role sets used by
# playout_router.py / autoschedule_router.py. Writes (create, cancel) can put
# content on air or pull it, so they require the same roles as commit-to-air;
# reads additionally allow support_admin, same as those siblings.
_WRITE_ROLES = ("publish_operator", "setup_admin")
_READ_ROLES = ("publish_operator", "setup_admin", "support_admin")

_DB_NOT_READY_DESCRIPTION = "Durable storage not ready -- run Setup storage or set DATABASE_URL"
_DB_NOT_READY_DETAIL = (
    "Durable storage is not ready. Open Setup and choose Prepare storage, "
    "or set DATABASE_URL for a technical deployment."
)
_LOG = logging.getLogger(__name__)


@public_router.get(
    "/assets",
    response_model=list[AssetMetadata],
    summary="List every asset in the active store",
)
def list_assets(
    request: Request,
    store: AssetStore = Depends(get_asset_store),
) -> list[AssetMetadata]:
    """Return every asset in the active store as a JSON array.

    Public — no auth. The portal SPA consumes this list to populate the
    asset directory. Ordering is the store's natural ordering (the
    default in-memory + Postgres stores both order by published_at DESC
    NULLS LAST, asset_id ASC); clients that need a specific order should
    not rely on this endpoint's order beyond what the conformance test
    asserts (presence, not order).
    """
    del request  # The same-origin path is intentionally independent of Host.
    return [
        asset.model_copy(
            update={"manifest_url": public_manifest_reference(asset.asset_id, asset.manifest_url)}
        )
        for asset in store.list()
        if asset.published_at is not None
    ]


@public_router.get(
    "/assets/{asset_id}",
    response_model=AssetMetadata,
    summary="Get a single asset by id",
    responses={
        404: {"description": "Asset not found"},
    },
)
def get_asset(
    asset_id: str,
    request: Request,
    store: AssetStore = Depends(get_asset_store),
) -> AssetMetadata:
    """Return the AssetMetadata for ``asset_id`` or 404.

    Public — no auth. Mirrors the 404 shape of ``vod.router.get_embed``
    (``detail`` includes the asset id) so operators see consistent error
    text across the public endpoints.
    """
    asset = store.get(asset_id)
    if asset is None or asset.published_at is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset not found: {asset_id}",
        )
    del request
    return asset.model_copy(
        update={"manifest_url": public_manifest_reference(asset.asset_id, asset.manifest_url)}
    )


@public_router.get(
    "/schedule/coming-up",
    response_model=list[ScheduleItemResponse],
    summary="List committed (published) public premieres for the Coming Up widget",
    responses={
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def list_public_coming_up(
    channel_id: str | None = None,
    schedule_store: Any = Depends(get_schedule_store),
) -> list[ScheduleItemResponse]:
    """Return resident-visible committed premieres.

    Shows only ``published`` premieres — the state that is actually resolved
    onto the air by :func:`civiccast.egress.source_plan.build_source_plan_from_schedule`
    now that Commit-to-Air is enforced. ``scheduled`` items are operator drafts
    that have not been approved to air (they may be edited, fail the dry-run, or
    never be committed), so advertising them would promise programs that may
    never broadcast. Embargo entries (release controls, not programming) and
    cancelled rows are excluded.
    """
    if schedule_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_DB_NOT_READY_DETAIL,
        )
    rows = cast(
        list[ScheduleItemResponse],
        schedule_store.list(
            channel_id=channel_id,
            states=(SCHEDULE_STATE_PUBLISHED,),
        ),
    )
    now = datetime.now(UTC)
    return [row for row in rows if row.mode == SCHEDULE_MODE_PREMIERE and row.scheduled_at >= now]


@staff_router.get(
    "/assets",
    response_model=list[StaffAssetRow],
    summary="List every asset (operator library)",
    responses={
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def list_staff_assets(
    response: Response,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    postgres_store: Any = Depends(get_postgres_store),
) -> list[StaffAssetRow]:
    """Return a page of assets, including uploaded-but-not-packaged rows.

    Sprint 0.3 task 3 — operator console asset library. The public
    ``GET /api/public/assets`` endpoint filters to packaged-and-published
    assets; this endpoint intentionally does not, so
    the operator can see ``pending_ingest`` / ``ingesting`` / ``validated``
    (pre-packaged) / ``rejected`` rows alongside packaged assets.

    4.0 media-library-hardening item 5 (pagination): ``limit``/``offset``
    match the repo's existing convention (see
    ``civiccast.cg.board_router.board_audit``), default page size 50, max
    500. The response body stays a bare JSON array (unchanged shape — the
    operator portal's ``listStaffAssets()`` and its e2e specs assume this),
    so the total matching row count is surfaced as the ``X-Total-Count``
    response header instead of wrapping the body in an envelope.

    Returns 503 only in explicit throwaway/ephemeral mode, when durable storage
    is intentionally not wired for staff writes.
    """
    if postgres_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_DB_NOT_READY_DETAIL,
        )
    rows, total = cast(
        tuple[list[StaffAssetRow], int],
        postgres_store.list_all_page(limit=limit, offset=offset),
    )
    response.headers["X-Total-Count"] = str(total)
    return rows


# 4.0 media-library-hardening: these two literal-path routes
# (``/assets/broken``, ``/assets/duplicates``) are registered here,
# *before* the ``/assets/{asset_id}`` parametrized route below, because
# Starlette matches routes in registration order — if a ``{asset_id}``
# route were registered first, a request for ``/assets/broken`` would
# match it with ``asset_id="broken"`` instead of reaching this endpoint.
@staff_router.get(
    "/assets/broken",
    response_model=list[StaffAssetRow],
    summary="List assets whose backing file is missing",
    responses={
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def list_broken_assets(
    postgres_store: Any = Depends(get_postgres_store),
) -> list[StaffAssetRow]:
    """Return assets flagged ``file_status='missing'`` by the integrity scan.

    See ``civiccast.schedule.media_integrity_worker`` for what sets the
    flag and how often.
    """
    if postgres_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_DB_NOT_READY_DETAIL,
        )
    return cast(list[StaffAssetRow], postgres_store.list_broken())


@staff_router.get(
    "/assets/duplicates",
    response_model=list[list[StaffAssetRow]],
    summary="List groups of assets sharing identical file content",
    responses={
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def list_duplicate_assets(
    postgres_store: Any = Depends(get_postgres_store),
) -> list[list[StaffAssetRow]]:
    """Return groups of 2+ assets whose ``content_hash`` matches.

    Report-only: this endpoint never deletes or merges anything. Assets
    ingested before the 4.0 media-library-hardening migration (no
    ``content_hash`` yet) are excluded from grouping, not treated as
    duplicates of each other.
    """
    if postgres_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_DB_NOT_READY_DETAIL,
        )
    return cast(list[list[StaffAssetRow]], postgres_store.list_duplicates())


@staff_router.get(
    "/assets/{asset_id}",
    response_model=StaffAssetRow,
    summary="Get one asset (operator detail view)",
    responses={
        404: {"description": "Asset not found"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def get_staff_asset(
    asset_id: str,
    postgres_store: Any = Depends(get_postgres_store),
) -> StaffAssetRow:
    """Return the operator-side projection for one asset.

    Used by the asset-detail screen and the trim/chapter editor —
    surfaces every asset regardless of state, including trim/chapter/
    retention metadata so the editor can render without a second call.
    """
    if postgres_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_DB_NOT_READY_DETAIL,
        )
    result = postgres_store.get_staff_row(asset_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset not found: {asset_id}",
        )
    return cast(StaffAssetRow, result)


@staff_router.post(
    "/assets/{asset_id}/playback-grant",
    summary="Issue short-lived private playback access for an asset source",
    dependencies=[Depends(require_any_role(*_SOURCE_PLAYBACK_ROLES))],
    responses={
        404: {"description": "Asset not found or source media is unavailable"},
        409: {"description": "Asset has no source file within configured media storage"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def grant_staff_asset_playback(
    asset_id: str,
    request: Request,
    postgres_store: Any = Depends(get_postgres_store),
) -> JSONResponse:
    """Grant this browser short-lived cookie access to one local asset source.

    Native media elements cannot attach CivicCast's staff bearer header. This
    exchange keeps bearer auth on the grant request, then binds an opaque,
    HttpOnly cookie to exactly one asset and its configured local source path.
    The same cookie can read that asset's completed private HLS package; the
    original source URL remains available for absolute transcript timestamps.
    """
    if not _SOURCE_ASSET_ID.fullmatch(asset_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found.")
    if postgres_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_DB_NOT_READY_DETAIL,
        )
    asset = postgres_store.get_staff_row(asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found.")
    if asset.state not in _PACKAGEABLE_ASSET_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Playback is available after source media has been validated or recorded.",
        )
    if not asset.file_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This asset has no local source file available for playback.",
        )

    # Import the live dependency keys only when the playback route is called.
    # Importing live.router at module load creates a cycle through live
    # finalization -> schedule package initialization during narrow tooling
    # imports (for example, caption embed verification without create_app).
    from civiccast.live.router import get_live_finalization_worker, get_recording_target_store

    overrides = request.app.dependency_overrides
    recording_target_provider = overrides.get(
        get_recording_target_store, get_recording_target_store
    )
    finalization_provider = overrides.get(
        get_live_finalization_worker, get_live_finalization_worker
    )
    recording_target_store = recording_target_provider()
    finalization_worker = finalization_provider()

    roots = _configured_source_roots(recording_target_store)
    if not roots:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Local media storage is not configured for source playback.",
        )
    source_unresolved = Path(asset.file_path).expanduser().resolve()
    if not any(source_unresolved.is_relative_to(root) for root in roots):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The asset source is outside configured CivicCast media storage.",
        )
    source = _allowed_source_path(asset.file_path, roots)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The source media file is unavailable. Relink or restore the file, then retry.",
        )

    package_root, source_offset_seconds = _packaged_preview_root(
        asset,
        source,
        roots,
        finalization_worker,
    )
    now = _source_playback_now()
    expires_at = now + _SOURCE_PLAYBACK_TTL
    grant = _SourcePlaybackGrant(
        asset_id=asset_id,
        source_path=source,
        allowed_roots=roots,
        package_root=package_root,
        source_offset_seconds=source_offset_seconds,
        expires_at=expires_at,
    )
    token = secrets.token_urlsafe(32)
    token_digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    grants = _source_playback_store(request)
    with _SOURCE_PLAYBACK_LOCK:
        for old_digest, old_grant in list(grants.items()):
            if old_grant.expires_at <= now:
                grants.pop(old_digest, None)
        while len(grants) >= _SOURCE_PLAYBACK_MAX_GRANTS:
            grants.popitem(last=False)
        grants[token_digest] = grant

    response = JSONResponse(
        {
            "url": f"/media/staff-source/{asset_id}/source",
            "packaged_url": (
                f"/media/staff-source/{asset_id}/hls/playlist.m3u8"
                if package_root is not None
                else None
            ),
            "playback_kind": "hls" if package_root is not None else "source",
            "source_content_type": _source_media_type(source),
            "source_offset_seconds": grant.source_offset_seconds,
            "expires_at": expires_at.isoformat(),
        },
        headers=_SOURCE_PLAYBACK_HEADERS,
    )
    peer = request.client.host if request.client is not None else ""
    try:
        peer_is_loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        peer_is_loopback = False
    secure = request.url.scheme.lower() == "https"
    if not secure and not peer_is_loopback:
        with _SOURCE_PLAYBACK_LOCK:
            grants.pop(token_digest, None)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Private source playback requires HTTPS for remote stations. "
                "Use the local CivicCast station console or configure a trusted HTTPS proxy."
            ),
        )
    response.set_cookie(
        key=_source_playback_cookie_name(asset_id),
        value=token,
        max_age=int(_SOURCE_PLAYBACK_TTL.total_seconds()),
        expires=expires_at,
        path=f"/media/staff-source/{asset_id}/",
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    return response


def _get_staff_source_grant(request: Request, asset_id: str) -> _SourcePlaybackGrant:
    token = request.cookies.get(_source_playback_cookie_name(asset_id))
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Source playback access is missing or expired. Request a new playback grant.",
        )
    digest = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
    grants = _source_playback_store(request)
    now = _source_playback_now()
    with _SOURCE_PLAYBACK_LOCK:
        grant = grants.get(digest)
        if grant is None or grant.asset_id != asset_id or grant.expires_at <= now:
            grants.pop(digest, None)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Source playback access is missing or expired. Request a new playback grant.",
            )
        grants.move_to_end(digest)
    return grant


def _private_media_file(
    file_path: Path,
    *,
    root: Path,
) -> FileResponse:
    candidate = file_path.resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The source media file is unavailable.",
        )
    media_type = _SOURCE_HLS_CONTENT_TYPES.get(candidate.suffix.lower())
    if media_type is None:
        media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    return FileResponse(candidate, media_type=media_type, headers=_SOURCE_PLAYBACK_HEADERS)


@source_media_router.api_route(
    "/{asset_id}/source",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
def get_staff_source_media(asset_id: str, request: Request) -> FileResponse:
    """Stream one granted source file with native Range/seek support."""
    grant = _get_staff_source_grant(request, asset_id)
    source_root = next(
        (root for root in grant.allowed_roots if grant.source_path.is_relative_to(root)),
        None,
    )
    if source_root is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The source media file is unavailable.",
        )
    return _private_media_file(grant.source_path, root=source_root)


@source_media_router.api_route(
    "/{asset_id}/hls/{file_path:path}",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
def get_staff_packaged_media(asset_id: str, file_path: str, request: Request) -> FileResponse:
    """Serve only files inside this grant's configured, completed HLS package."""
    grant = _get_staff_source_grant(request, asset_id)
    if grant.package_root is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No completed local HLS package is available for this asset.",
        )
    candidate = (grant.package_root / file_path).resolve()
    if not candidate.is_relative_to(grant.package_root) or not any(
        grant.package_root.is_relative_to(root) for root in grant.allowed_roots
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested packaged media file is unavailable.",
        )
    return _private_media_file(candidate, root=grant.package_root)


@staff_router.post(
    "/assets/{asset_id}/package",
    response_model=StaffAssetRow,
    summary="Package validated or recorded local media for resident playback",
    dependencies=[Depends(require_any_role("publish_operator", "setup_admin"))],
    responses={
        404: {"description": "Asset not found"},
        409: {"description": "Asset has no readable local source file"},
        422: {"description": "Media packaging failed"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
async def package_staff_asset(
    asset_id: str,
    request: Request,
    postgres_store: Any = Depends(get_postgres_store),
) -> StaffAssetRow:
    """Encode one validated or recorded asset into the local HLS media service.

    Packaging writes to a sibling staging directory and only swaps it into
    place after the multivariant manifest exists. The database is updated
    last, so residents never receive a manifest URL for a partial package.
    """
    # Deferred because captions -> live.finalization -> schedule imports this router.
    from civiccast.captions.vod import (
        OfflineCaptionPackageError,
        published_caption_sidecar,
        reattach_packaged_captions,
    )

    if postgres_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_DB_NOT_READY_DETAIL,
        )
    asset = postgres_store.get_staff_row(asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset not found: {asset_id}",
        )
    if asset.state not in _PACKAGEABLE_ASSET_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Only validated or recorded media can be packaged. Wait for validation "
                "or the recording to finish, or correct the ingest failure, then try again."
            ),
        )
    upload_root = resolve_upload_root()
    if upload_root is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Upload storage is not configured. Open Setup and prepare storage first.",
        )
    if not asset.file_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The asset has no local source file to package.",
        )
    source = Path(asset.file_path).expanduser().resolve()
    if not source.is_relative_to(upload_root):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The asset source is outside CivicCast upload storage and cannot be packaged.",
        )
    if not source.is_file():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The asset source file is missing. Relink or re-upload it, then package again.",
        )

    package_root = resolve_vod_package_root(upload_root)
    package_dir = (package_root / asset_id).resolve()
    staging_dir = (package_root / f".{asset_id}-{uuid.uuid4().hex}.tmp").resolve()
    backup_dir = (package_root / f".{asset_id}-{uuid.uuid4().hex}.previous").resolve()
    if not all(
        candidate.is_relative_to(upload_root)
        for candidate in (package_root, package_dir, staging_dir, backup_dir)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The package path resolved outside CivicCast upload storage.",
        )
    if not _PACKAGE_ADMISSION.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Another recording is already being packaged. CivicCast limits packaging "
                "to one recording at a time so live and operator work remain responsive. "
                "Wait for it to finish, then retry."
            ),
        )

    marker_path = _package_preview_marker_path(asset_id, package_dir.parent)
    marker_temp = marker_path.with_name(f"{marker_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        result = await asyncio.to_thread(
            pack_vod_asset,
            source,
            staging_dir,
            trim_in_seconds=asset.trim_in_seconds,
            trim_out_seconds=asset.trim_out_seconds,
            # Ingest already probed the source; handing the dimensions over
            # lets the packager skip a redundant ffprobe and, more
            # importantly, drop the ladder rungs that would upscale.
            source_width=asset.width_px,
            source_height=asset.height_px,
        )
        staged_manifest = Path(result.manifest_path).resolve()
        if not staged_manifest.is_relative_to(staging_dir) or not staged_manifest.is_file():
            raise PackagingError("The packager did not produce a usable manifest.")
        source_stat = source.stat()
        if published_caption_sidecar(package_dir).is_file():
            review_store = resolve_app_store(
                request, "caption_review_store", surface="Caption review store"
            )
            await asyncio.to_thread(
                reattach_packaged_captions,
                package_dir,
                staging_dir,
                asset_id=asset_id,
                review_store=review_store,
            )
        marker_temp.write_text(
            json.dumps(
                {
                    "asset_id": asset_id,
                    "source_path": str(source),
                    "source_size_bytes": source_stat.st_size,
                    "source_mtime_ns": source_stat.st_mtime_ns,
                    "trim_in_seconds": asset.trim_in_seconds,
                    "trim_out_seconds": asset.trim_out_seconds,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        if package_dir.exists():
            package_dir.rename(backup_dir)
        staging_dir.rename(package_dir)
        marker_temp.replace(marker_path)
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
    except Exception as exc:
        _PACKAGE_ADMISSION.release()
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        marker_temp.unlink(missing_ok=True)
        if backup_dir.exists() and not package_dir.exists():
            backup_dir.rename(package_dir)
        _LOG.exception("Asset packaging failed for %s.", asset_id)
        if isinstance(exc, OfflineCaptionPackageError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Caption preservation failed: {exc} The previous package was kept.",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "CivicCast could not package this media for playback. The original file "
                "was kept unchanged; review the server log, then try again."
            ),
        ) from exc
    _PACKAGE_ADMISSION.release()

    manifest_url = f"/media/vod/{quote(asset_id, safe='')}/playlist.m3u8"
    try:
        return cast(StaffAssetRow, postgres_store.mark_packaged(asset_id, manifest_url))
    except Exception as exc:
        _LOG.exception("Packaged asset %s but could not record its manifest URL.", asset_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The media package was created, but CivicCast could not save its playback "
                "address. The original file and package were kept; try again."
            ),
        ) from exc


@staff_router.patch(
    "/assets/{asset_id}",
    response_model=StaffAssetRow,
    summary="Update an asset's metadata (title, description, trim, chapters, retention)",
    dependencies=[Depends(require_any_role("records_clerk", "meeting_operator", "support_admin"))],
    responses={
        404: {"description": "Asset not found"},
        409: {
            "description": (
                "Optimistic concurrency conflict -- the asset has been "
                "updated by another writer since the version the client "
                "submitted. Refetch and retry."
            )
        },
        422: {
            "description": "Invalid payload (trim ordering, retention enum, chapter past duration)"
        },
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def update_asset_metadata(
    asset_id: str,
    update: AssetMetadataUpdate,
    postgres_store: Any = Depends(get_postgres_store),
) -> StaffAssetRow:
    """Apply a partial metadata update to an asset.

    Sprint 0.3 task 5 + audit-team v0.3.0 fixes (ENG-008, QA-008, QA-012).
    Fields not present in the payload are left unchanged. ``chapters``
    is a full replacement (the editor sends the whole list); send an
    empty list to clear all chapters.

    Optimistic concurrency: the client must echo back the version it
    last observed (``expected_version``). The store rejects with 409
    if the row has advanced since.

    Trim and chapter edits are non-destructive — the original file is
    never modified. The Sprint 0.4 packager reads these columns when
    producing the HLS manifest + WebVTT chapter track.
    """
    if postgres_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_DB_NOT_READY_DETAIL,
        )

    from civiccast.schedule.store import (
        AssetAlreadyPublishedError,
        AssetNotFoundError,
        AssetVersionConflictError,
    )

    try:
        return cast(StaffAssetRow, postgres_store.update_metadata(asset_id, update))
    except AssetNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset not found: {asset_id}",
        ) from exc
    except AssetVersionConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    f"Asset {asset_id} was updated by another writer. Reload and try again."
                ),
                "expected_version": exc.expected_version,
                "current_version": exc.current_version,
            },
        ) from exc
    except AssetAlreadyPublishedError as exc:
        # QA-007 (audit-team v0.3.0): the asset has at least one linked
        # schedule item in state 'published'. Editing trim/title/chapters
        # underneath a published surface would silently change what
        # residents see. Operator must unpublish (or cancel + re-create)
        # the named schedule items before retrying. The detail body
        # carries the conflicting item ids so the operator UI can render
        # actionable links without a follow-up query.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    f"Asset {asset_id} cannot be edited: "
                    f"{len(exc.published_schedule_item_ids)} linked schedule "
                    "item(s) already published. Unpublish or cancel the "
                    "named items before retrying the edit."
                ),
                "published_schedule_item_ids": exc.published_schedule_item_ids,
            },
        ) from exc
    except (ValueError, OverflowError) as exc:
        # QA-012: chapter past duration. Pydantic-level errors return 422
        # naturally; this is the cross-field validation that needed a DB
        # lookup. OverflowError (coordinator-directed fix, follow-up
        # commit, MAJOR finding 1): a bounded retention_term_value should
        # never let civiccast.schedule.retention_terms.compute_retention_until's
        # timedelta arithmetic overflow anymore (the Pydantic ``le=`` bound
        # and this module's own per-unit ceiling both close that off
        # before it can happen), but this stays a defense-in-depth catch
        # rather than trusting the bound to be the only thing standing
        # between a large integer and an uncaught 500.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@staff_router.post(
    "/assets/{asset_id}/unpublish",
    response_model=StaffAssetRow,
    summary="Withdraw an asset from Portal visibility",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    responses={
        404: {"description": "Asset not found"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def unpublish_asset(
    asset_id: str,
    postgres_store: Any = Depends(get_postgres_store),
) -> StaffAssetRow:
    """Clear the asset's Portal publish state (the inverse of publish approval).

    Added for A-1 (Codex review, PR #419): the first-run seeded sample's own
    description tells the operator to "Delete it like any other asset once
    real content is ready," but no removal or unpublish endpoint existed
    anywhere in the product -- an operator following that instruction had no
    way to act on it. This makes the promise true: clearing ``published_at``
    is exactly what ``list_assets`` / ``get_asset`` above check, so the
    asset immediately stops appearing on the public portal.

    Same role gate as ``cancel_schedule_item``: pulling an asset off the
    portal is as consequential as putting one on. Idempotent -- unpublishing
    an asset that is not currently published returns the row unchanged
    rather than erroring, mirroring ``cancel_schedule_item``'s no-op
    semantics for an already-cancelled item.

    Deliberately scoped to Portal visibility only. It does not delete the
    underlying media, metadata, or file-storage rows (a public-record asset
    may still need to exist for the archival/records-retention surfaces),
    and it does not attempt to reverse Internet Archive, YouTube, or
    ActivityPub delivery -- those are independent peer surfaces under the
    three-tier publish model (spec Sec 2.6), out of scope for this endpoint.
    """
    if postgres_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_DB_NOT_READY_DETAIL,
        )

    from civiccast.schedule.store import AssetNotFoundError

    try:
        return cast(StaffAssetRow, postgres_store.mark_unpublished(asset_id))
    except AssetNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset not found: {asset_id}",
        ) from exc


@staff_router.post(
    "/assets",
    response_model=AssetMetadata,
    status_code=status.HTTP_201_CREATED,
    summary="Create one asset (staff)",
    dependencies=[Depends(require_any_role("records_clerk", "meeting_operator", "support_admin"))],
    responses={
        409: {"description": "Asset already exists"},
        422: {"description": "Invalid payload"},
    },
)
def create_asset(
    asset: AssetMetadata,
    store: AssetStore = Depends(get_asset_store),
) -> AssetMetadata:
    """Persist a new asset and return the canonical persisted form.

    Staff-route bearer authentication is enforced by middleware. Operators
    still keep the API on loopback or behind their normal reverse proxy for
    network and TLS policy.

    Validation:

      - 422 when ``asset_id`` is missing, malformed, or violates the
        ``^[a-z0-9][a-z0-9-]{2,63}$`` pattern (Pydantic surface).
      - 422 when any required field is missing.
      - 409 when ``asset_id`` already exists in the active store.

    Response body is the canonical persisted asset (re-fetched after
    write per Q6), which may differ from the input due to server-side
    timestamp normalization (tzinfo coercion on the SQLite path).
    """
    try:
        return store.create(asset)
    except AssetAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Asset already exists: {exc.asset_id}",
        ) from exc


@staff_router.post(
    "/assets/upload",
    response_model=UploadedAssetResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload an asset file and run ffprobe ingest (staff)",
    dependencies=[Depends(require_any_role("records_clerk", "meeting_operator", "support_admin"))],
    responses={
        409: {"description": "Asset id already exists"},
        422: {"description": "Invalid form fields or unsupported file format"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
async def upload_asset(
    asset_id: str = Form(
        ...,
        pattern=r"^[a-z0-9][a-z0-9-]{2,63}$",
        description="URL-safe asset identifier.",
    ),
    title: str = Form(..., min_length=1, max_length=200),
    description: str | None = Form(None, max_length=2000),
    select_for_rehearsal: bool = Form(
        False,
        description="Select this validated upload as the next private-rehearsal sample.",
    ),
    file: UploadFile = File(..., description="Video file to ingest."),
    postgres_store: Any = Depends(get_postgres_store),
) -> UploadedAssetResponse:
    """Accept a video file upload, run ffprobe ingest, and persist the asset.

    Validation gate: rejects files whose codec or container format are
    outside the supported set with HTTP 422 and an operator-readable reason.

    Staff-route bearer authentication is enforced by middleware. Durable
    storage is prepared by setup/startup unless the app is in explicit
    throwaway mode. ``CIVICCAST_UPLOAD_DIR`` must be set to a writable
    directory.
    """
    if postgres_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_DB_NOT_READY_DETAIL,
        )

    upload_dir_str = os.environ.get("CIVICCAST_UPLOAD_DIR")
    if not upload_dir_str:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Upload directory not configured. Set CIVICCAST_UPLOAD_DIR.",
        )

    upload_dir = Path(upload_dir_str).resolve()
    asset_dir = (upload_dir / asset_id).resolve()
    incoming_dir = (upload_dir / ".incoming").resolve()
    if not asset_dir.is_relative_to(upload_dir):
        # Defense-in-depth — asset_id is regex-validated to a slug pattern,
        # so this branch should be unreachable. Treat any escape as 422.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid asset_id resolves outside the upload directory.",
        )
    if not incoming_dir.is_relative_to(upload_dir):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid upload staging directory.",
        )
    get_staff_row = getattr(postgres_store, "get_staff_row", None)
    if callable(get_staff_row) and get_staff_row(asset_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Asset already exists: {asset_id}",
        )

    # ENG-001 (audit-team v0.3.0): the multipart filename header is
    # client-controlled and must be sanitized. PurePosixPath().name strips
    # any directory components on either separator; the regex pins the
    # remainder to a portable, traversal-safe character set. The post-
    # resolve `is_relative_to` check is the belt-and-suspenders.
    raw_name = file.filename or "upload"
    base_name = PurePosixPath(raw_name.replace("\\", "/")).name or "upload"
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", base_name) or "upload"
    if safe_name in (".", ".."):
        safe_name = "upload"
    dest_path = (asset_dir / safe_name).resolve()
    if not dest_path.is_relative_to(asset_dir):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid filename.",
        )
    await asyncio.to_thread(incoming_dir.mkdir, parents=True, exist_ok=True)
    temp_path = (incoming_dir / f"{asset_id}-{uuid.uuid4().hex}.upload").resolve()
    if not temp_path.is_relative_to(incoming_dir):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Invalid upload staging path.",
        )

    # Bound the upload size as defense against DoS via unbounded streams.
    # Operators can override the default via CIVICCAST_UPLOAD_MAX_BYTES.
    max_bytes = int(os.environ.get("CIVICCAST_UPLOAD_MAX_BYTES", str(10 * 1024 * 1024 * 1024)))

    written = 0
    try:
        with temp_path.open("wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    f.close()
                    await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail=(
                            f"Upload exceeds maximum size of {max_bytes} bytes. "
                            "Increase CIVICCAST_UPLOAD_MAX_BYTES if intentional."
                        ),
                    )
                await asyncio.to_thread(f.write, chunk)
    finally:
        await file.close()

    file_size_bytes = temp_path.stat().st_size
    success = False

    try:
        try:
            ffprobe_result = await asyncio.to_thread(run_ffprobe, temp_path)
        except FfprobeNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc

        await asyncio.to_thread(asset_dir.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(os.replace, temp_path, dest_path)

        try:
            validate_ingest(ffprobe_result)
        except UnsupportedFormatError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=exc.reason,
            ) from exc

        # 4.0 media-library-hardening: content hash (duplicate detection)
        # and a thumbnail, both best-effort. Neither blocks ingest — a
        # hashing hiccup or an absent/failing ffmpeg must not turn a valid
        # upload into a 500; the asset simply lands with content_hash
        # and/or thumbnail_path as None, same as any other optional
        # ffprobe-derived field on this row.
        try:
            content_hash = await asyncio.to_thread(hash_file, dest_path)
        except OSError:
            content_hash = None

        thumbnail_target = asset_dir / "thumbnail.jpg"
        thumbnail_path: Path | None
        try:
            await asyncio.to_thread(extract_thumbnail, dest_path, thumbnail_target)
            thumbnail_path = thumbnail_target
        except (FfmpegNotFoundError, FfprobeError, OSError):
            thumbnail_path = None

        if select_for_rehearsal:
            # Local import avoids making schedule-router module initialization
            # depend on the installer router while still keeping selection
            # bound to this exact, already-validated upload path.
            from civiccast.installer.service import record_sample_rehearsal_media

            await asyncio.to_thread(
                record_sample_rehearsal_media,
                asset_id=asset_id,
                file_path=dest_path,
                upload_dir=upload_dir,
            )

        try:
            result = cast(
                UploadedAssetResponse,
                await asyncio.to_thread(
                    postgres_store.ingest_upload,
                    asset_id=asset_id,
                    title=title,
                    description=description,
                    file_path=str(dest_path),
                    file_size_bytes=file_size_bytes,
                    ffprobe_result=ffprobe_result,
                    content_hash=content_hash,
                    thumbnail_path=str(thumbnail_path) if thumbnail_path is not None else None,
                ),
            )
            success = True
            return result
        except AssetAlreadyExistsError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Asset already exists: {exc.asset_id}",
            ) from exc
    finally:
        if not success:
            await asyncio.to_thread(temp_path.unlink, missing_ok=True)
            await asyncio.to_thread(dest_path.unlink, missing_ok=True)


# ===========================================================================
# Media-library hardening (4.0 scope item 5): missing-file listing, relink,
# duplicate detection, and thumbnail serving.
# ===========================================================================

# Relink tolerance policy: the replacement file must be the "same recording,
# possibly re-encoded/re-muxed/trimmed by a few seconds" — not an
# unrelated file the operator fat-fingered. Duration tolerance is the
# larger of a flat 5 seconds or 2% of the recorded duration, so a
# multi-hour meeting recording (where a re-encode can drift by more than
# 5s due to keyframe rounding) isn't rejected for a difference nobody
# would notice, while a short clip still gets a tight absolute bound.
# Video codec must match exactly — a codec change means a different
# transcode pipeline touched the file, which is worth a human's attention
# even if the content is the same. Audio codec is informational only (not
# every asset has an audio stream) and is not part of the gate.
_RELINK_DURATION_TOLERANCE_FLOOR_SECONDS = 5
_RELINK_DURATION_TOLERANCE_FRACTION = 0.02


class RelinkAssetRequest(BaseModel):
    """Request payload for ``POST /api/staff/assets/{asset_id}/relink``."""

    model_config = ConfigDict(extra="forbid")

    new_file_path: str = Field(
        ...,
        min_length=1,
        description="Server-side path to the replacement file.",
    )


class RelinkMismatchDetail(BaseModel):
    """409 detail body when the candidate file fails the relink tolerance check."""

    message: str
    expected_duration_seconds: int | None
    actual_duration_seconds: int | None
    expected_codec_video: str | None
    actual_codec_video: str | None


def _relink_tolerance_seconds(expected_duration: int) -> float:
    return max(
        _RELINK_DURATION_TOLERANCE_FLOOR_SECONDS,
        expected_duration * _RELINK_DURATION_TOLERANCE_FRACTION,
    )


@staff_router.post(
    "/assets/{asset_id}/relink",
    response_model=StaffAssetRow,
    summary="Point an asset at a replacement file after validating it matches",
    dependencies=[Depends(require_any_role("records_clerk", "meeting_operator", "support_admin"))],
    responses={
        404: {"description": "Asset not found, or new_file_path does not exist"},
        409: {"description": "Candidate file fails the duration/codec tolerance check"},
        422: {
            "description": (
                "new_file_path resolves outside the upload directory, or "
                "ffprobe could not read the candidate file"
            )
        },
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
async def relink_asset(
    asset_id: str,
    payload: RelinkAssetRequest,
    postgres_store: Any = Depends(get_postgres_store),
) -> StaffAssetRow:
    """Validate a replacement file and, if it matches, relink the asset to it.

    Validation gate (see the tolerance constants above this route):

      1. ``new_file_path`` must resolve (symlinks followed) to a path
         inside ``CIVICCAST_UPLOAD_DIR`` (422 if not — same containment
         contract as the upload handler; an asset's ``file_path`` must
         never leave the media root, because downstream consumers like the
         thumbnails-backfill command write sibling files next to it).
      2. It must exist on disk (404 if not — same shape as a not-found
         asset_id, since either way there is nothing to link).
      3. ffprobe must be able to read it (422 on failure — same as ingest).
      4. Video codec must match the asset's recorded ``codec_video``
         exactly, and duration must be within tolerance of the recorded
         ``duration_seconds``. Either check failing is a 409 (the
         candidate is a real, readable file — it's just not a confident
         match for this asset) with a detail body naming both the
         expected and actual values so the operator can judge for
         themselves rather than guessing from a bare "no match" message.
         Assets with no prior ``duration_seconds``/``codec_video``
         (never ffprobed, e.g. legacy manifest-only rows) skip whichever
         half of the check has nothing to compare against.

    On success, re-hashes the new file and updates the asset's ffprobe
    columns from its actual probe result (a relinked file can be a
    different encode of the same recording, not a byte-identical copy).
    """
    if postgres_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_DB_NOT_READY_DETAIL,
        )

    from civiccast.schedule.store import AssetNotFoundError

    get_staff_row = getattr(postgres_store, "get_staff_row", None)
    existing = get_staff_row(asset_id) if callable(get_staff_row) else None
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset not found: {asset_id}",
        )

    # Path containment — same pattern (and same order: resolve BEFORE the
    # is_relative_to check, so symlinks can't escape) as the upload
    # handler's four containment checks above. Without this, a staff token
    # could repoint any asset at any ffprobe-readable file anywhere on the
    # box or its mounted shares, and the thumbnails-backfill command would
    # then write thumbnail.jpg into whatever directory file_path points at
    # — an attacker-directed write outside the media root.
    upload_dir_str = os.environ.get("CIVICCAST_UPLOAD_DIR")
    if not upload_dir_str:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Upload directory not configured. Set CIVICCAST_UPLOAD_DIR.",
        )
    upload_dir = Path(upload_dir_str).resolve()
    new_path = await asyncio.to_thread(Path(payload.new_file_path).resolve)
    if not new_path.is_relative_to(upload_dir):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="new_file_path resolves outside the upload directory.",
        )

    if not await asyncio.to_thread(new_path.is_file):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"new_file_path does not exist or is not a file: {payload.new_file_path}",
        )

    try:
        ffprobe_result = await asyncio.to_thread(run_ffprobe, new_path)
    except FfprobeNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except FfprobeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    if (
        existing.codec_video is not None
        and ffprobe_result.codec_video is not None
        and existing.codec_video != ffprobe_result.codec_video
    ) or (
        existing.duration_seconds is not None
        and ffprobe_result.duration_seconds is not None
        and abs(ffprobe_result.duration_seconds - existing.duration_seconds)
        > _relink_tolerance_seconds(existing.duration_seconds)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=RelinkMismatchDetail(
                message=(
                    f"Candidate file does not match asset {asset_id!r} within tolerance "
                    f"(duration ±{_relink_tolerance_seconds(existing.duration_seconds or 0):.0f}s, "
                    "exact video codec)."
                ),
                expected_duration_seconds=existing.duration_seconds,
                actual_duration_seconds=ffprobe_result.duration_seconds,
                expected_codec_video=existing.codec_video,
                actual_codec_video=ffprobe_result.codec_video,
            ).model_dump(),
        )

    try:
        content_hash = await asyncio.to_thread(hash_file, new_path)
    except OSError:
        content_hash = None

    try:
        return cast(
            StaffAssetRow,
            await asyncio.to_thread(
                postgres_store.relink,
                asset_id,
                new_file_path=str(new_path),
                ffprobe_result=ffprobe_result,
                content_hash=content_hash,
            ),
        )
    except AssetNotFoundError as exc:
        # TOCTOU: the asset existed at the get_staff_row check above but
        # was deleted before this write. Vanishingly unlikely (no delete
        # endpoint exists for assets today) but handled for correctness.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset not found: {asset_id}",
        ) from exc


@staff_router.get(
    "/assets/{asset_id}/thumbnail",
    summary="Serve an asset's generated thumbnail image",
    responses={
        404: {"description": "Asset not found, or has no thumbnail"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def get_asset_thumbnail(
    asset_id: str,
    postgres_store: Any = Depends(get_postgres_store),
) -> FileResponse:
    """Serve the JPEG thumbnail generated at ingest (or by the backfill command).

    404 (not a placeholder image) when the asset has no thumbnail yet —
    generation can fail (ffmpeg absent, corrupt source) without blocking
    ingest, so "no thumbnail" is an expected, honest state, not an error
    the caller needs to distinguish from "asset not found" for this
    endpoint's purposes. ``Cache-Control`` is long-lived + immutable: a
    thumbnail is only ever replaced by re-running the backfill command for
    this asset_id specifically, which callers can bust with a query string
    if they ever need to (not needed today — no code path re-generates a
    thumbnail for an asset that already has one).
    """
    if postgres_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_DB_NOT_READY_DETAIL,
        )
    get_staff_row = getattr(postgres_store, "get_staff_row", None)
    row = get_staff_row(asset_id) if callable(get_staff_row) else None
    if row is None or row.thumbnail_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No thumbnail available for asset: {asset_id}",
        )
    thumbnail_path = Path(row.thumbnail_path)
    if not thumbnail_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No thumbnail available for asset: {asset_id}",
        )
    return FileResponse(
        thumbnail_path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


# ===========================================================================
# Schedule endpoints — premiere / embargo (Sprint 0.3 task 4)
# (``live`` was retired in migration 0005 per audit-team v0.3.0 ENG-004;
# Sprint 0.5 live-ingest will model live events separately.)
# ===========================================================================


@staff_router.post(
    "/schedule",
    response_model=ScheduleItemResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    summary="Create a schedule item (premiere | embargo)",
    responses={
        409: {"description": "Schedule conflict -- overlapping premiere on the same channel"},
        422: {"description": "Invalid payload (mode/duration coupling, missing fields)"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def create_schedule_item(
    payload: ScheduleItemCreate,
    schedule_store: Any = Depends(get_schedule_store),
) -> ScheduleItemResponse:
    """Persist a new schedule item.

    Per the spec's Schedule lifecycle section (the "§1070" tag from earlier
    drafts predates current spec numbering — see DOC-010 in the audit-team
    v0.3.0 deep-dive):

    - ``premiere`` occupies a time range; overlapping premieres on the
      same channel are rejected at the DB layer (Postgres btree_gist
      EXCLUDE constraint, migration 0003 + 0005) and surface here as
      HTTP 409.
    - ``embargo`` mode targets a single publish moment, not a duration;
      embargo entries never trip the conflict check by design.

    Requires the ``publish_operator`` or ``setup_admin`` role — same gate as
    ``playout_router.py``'s commit endpoints, since a schedule item created
    here airs automatically once its air time arrives.
    """
    if schedule_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_DB_NOT_READY_DETAIL,
        )

    # Local import to avoid a circular dependency (router → store → models
    # → router would surface).
    from civiccast.schedule.store import AssetNotFoundError, ScheduleConflictError

    try:
        return cast(ScheduleItemResponse, schedule_store.create(payload))
    except AssetNotFoundError as exc:
        # QA-004: scheduling against an asset_id that doesn't exist
        # surfaces as 404 instead of letting the row land as a phantom.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ScheduleConflictError as exc:
        detail: dict[str, Any] = {"message": str(exc)}
        if exc.conflicting_item is not None:
            detail["conflicting_item"] = exc.conflicting_item.model_dump(mode="json")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
        ) from exc


@staff_router.get(
    "/schedule",
    response_model=list[ScheduleItemResponse],
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
    summary="List schedule items",
    responses={
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def list_schedule_items(
    channel_id: str | None = None,
    state: str | None = None,
    schedule_store: Any = Depends(get_schedule_store),
) -> list[ScheduleItemResponse]:
    """Return schedule items, optionally filtered by channel or state.

    Both filters are optional. ``state`` supports the three state-machine
    values (``scheduled``, ``cancelled``, ``published``). Multiple states
    are not supported in this rung — pass one at a time.
    """
    if schedule_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_DB_NOT_READY_DETAIL,
        )

    states = (state,) if state is not None else None
    try:
        return cast(
            list[ScheduleItemResponse],
            schedule_store.list(channel_id=channel_id, states=states),
        )
    except ValueError as exc:
        # ValueError is the store's signal for an unknown state value;
        # 422 keeps the contract aligned with FastAPI's validation conventions.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@staff_router.get(
    "/schedule/{schedule_id}",
    response_model=ScheduleItemResponse,
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
    summary="Get a single schedule item by id",
    responses={
        404: {"description": "Schedule item not found"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def get_schedule_item(
    schedule_id: uuid.UUID,
    schedule_store: Any = Depends(get_schedule_store),
) -> ScheduleItemResponse:
    """Return one schedule item by id. 404 if absent.

    FastAPI coerces the path string to ``uuid.UUID`` before calling;
    invalid UUID strings surface as 422.
    """
    if schedule_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_DB_NOT_READY_DETAIL,
        )

    result = schedule_store.get(schedule_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Schedule item not found: {schedule_id}",
        )
    return cast(ScheduleItemResponse, result)


@staff_router.post(
    "/schedule/{schedule_id}/cancel",
    response_model=ScheduleItemResponse,
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    summary="Cancel a schedule item",
    responses={
        404: {"description": "Schedule item not found"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def cancel_schedule_item(
    schedule_id: uuid.UUID,
    schedule_store: Any = Depends(get_schedule_store),
) -> ScheduleItemResponse:
    """Transition a scheduled item to cancelled.

    Cancelling an already-cancelled or already-published item is a no-op;
    the operator UI can decide whether to surface that as a "nothing to
    cancel" toast or hide the cancel control entirely once the item leaves
    the ``scheduled`` state.

    Requires the ``publish_operator`` or ``setup_admin`` role — same gate as
    ``create_schedule_item`` above, since pulling an item off the schedule
    is as consequential as putting one on.
    """
    if schedule_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_DB_NOT_READY_DETAIL,
        )

    from civiccast.schedule.store import ScheduleItemNotFoundError

    try:
        return cast(ScheduleItemResponse, schedule_store.cancel(schedule_id))
    except ScheduleItemNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Schedule item not found: {schedule_id}",
        ) from exc
