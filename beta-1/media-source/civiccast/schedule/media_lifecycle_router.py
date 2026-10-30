# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Staff API for S7 media lifecycle & readiness.

Endpoint surface (spec §4 + §8 test plan):

* ``GET  /api/staff/assets/readiness-dashboard``       -- operator dashboard
* ``GET  /api/staff/assets/{asset_id}/readiness``      -- one asset's badge
* ``PUT  /api/staff/assets/{asset_id}/replace-source``  -- ReplaceMediaWorkflow
* ``PUT  /api/staff/assets/{asset_id}/legal-hold``      -- CLAUDE.md §4.6 gate
* ``GET/POST/PUT/DELETE /api/staff/media-lifecycle/watch-folder-configs``
* ``POST /api/staff/media-lifecycle/watch-folder-configs/{config_id}/scan-now``
* ``GET  /api/staff/media-lifecycle/browse-folders``          -- non-technical folder picker
* ``GET/POST/PUT/DELETE /api/staff/media-lifecycle/retention-policies``
* ``POST /api/staff/media-lifecycle/retention-policies/apply``
* ``GET  /api/staff/media-lifecycle/storage-budget``
* ``GET  /api/staff/media-lifecycle/missing-media``
* ``GET  /api/staff/media-lifecycle/audit-log``

Route-ordering note (mirrors the existing ``/assets/broken`` /
``/assets/duplicates`` comment in ``civiccast.schedule.router``):
``/assets/readiness-dashboard`` is a literal path competing with
``civiccast.schedule.router``'s ``GET /assets/{asset_id}``. Starlette
matches app-wide in registration order, so ``civiccast.app`` MUST
``include_router(media_lifecycle_staff_router)`` before
``include_router(schedule_staff_router)`` -- otherwise a request for
``/assets/readiness-dashboard`` would be swallowed by
``get_staff_asset(asset_id="readiness-dashboard")`` and 404.
``/{asset_id}/readiness``, ``/{asset_id}/replace-source``, and
``/{asset_id}/legal-hold`` are two path segments past ``/assets/``, so they
never collide with either the one-segment ``{asset_id}`` route or each
other, regardless of registration order.

DI: :func:`get_media_lifecycle_store` returns ``None`` until the app factory
wires a real :class:`~civiccast.schedule.media_lifecycle_store.MediaLifecycleStore`
(handlers translate ``None`` to 503), mirroring
``civiccast.schedule.autoschedule_router.get_autoschedule_service``.
"""

from __future__ import annotations

import asyncio
import os
import re
import string
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, cast

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from civiccast.auth.roles import require_any_role
from civiccast.schedule.ingest import (
    FfprobeNotFoundError,
    UnsupportedFormatError,
    hash_file,
    run_ffprobe,
    validate_ingest,
)
from civiccast.schedule.media_lifecycle_models import (
    AssetReadinessResponse,
    AssetRetentionPolicyInput,
    AssetRetentionPolicyResponse,
    FolderBrowseEntry,
    FolderBrowseResponse,
    LegalHoldInput,
    LifecycleAuditEntryResponse,
    MissingMediaAlertRow,
    ReadinessDashboardResponse,
    StorageBudgetResponse,
    WatchFolderConfigInput,
    WatchFolderConfigResponse,
    WatchFolderScanNowResponse,
)
from civiccast.schedule.media_lifecycle_store import (
    AssetNotFoundError,
    AssetRetentionPolicyNotFoundError,
    MediaLifecycleStore,
    WatchFolderConfigNotFoundError,
)
from civiccast.schedule.watch_folder_worker import WatchFolderScanInProgressError

_WRITE_ROLES = ("publish_operator", "setup_admin")
_READ_ROLES = ("meeting_operator", "publish_operator", "support_admin")

# Finding 2 (adversarial audit of PR #69): every OTHER _WRITE_ROLES route in
# this file manipulates watch-folder/retention RECORDS (a publish_operator's
# normal job). ``browse-folders`` and ``scan-now`` instead reach raw OS
# filesystem APIs (``os.scandir`` over an arbitrary path, and an immediate
# unthrottled disk scan) -- a content-publishing role has no operational need
# for either, and granting it one hands a lower-trust role an OS-level
# enumeration primitive. Both routes are folder-picker/diagnostic actions a
# station admin performs during setup or troubleshooting (S3 "Configure
# watch folder" step, spec S7 §10), not part of day-to-day publish work, so
# they are restricted to setup_admin only.
_FS_ROLES = ("setup_admin",)

_DB_NOT_READY_DETAIL = (
    "Durable storage is not ready. Open Setup and choose Prepare storage, "
    "or set DATABASE_URL for a technical deployment."
)


def get_media_lifecycle_store() -> Any:
    """FastAPI dependency for the media lifecycle store; see module docstring."""

    return None


def _require_store(store: Any) -> MediaLifecycleStore:
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY_DETAIL
        )
    return cast(MediaLifecycleStore, store)


def get_watch_folder_worker() -> Any:
    """FastAPI dependency for the watch-folder poll daemon; see module docstring.

    Same ``None``-until-wired shape as :func:`get_media_lifecycle_store` --
    ``civiccast.app`` overrides this with ``lambda: app.state.watch_folder_worker``.
    Backs the operator-triggered "Scan now" action only; the daemon itself
    (:class:`~civiccast.schedule.watch_folder_worker.WatchFolderWorker`)
    keeps polling on its own schedule regardless of whether this dependency
    is ever exercised.
    """

    return None


_UNREADABLE_PATH_DETAIL = (
    "That folder does not exist or CivicCast cannot read it. Check the path is "
    "correct, the drive/share is connected, and the account running CivicCast "
    "has read access to it."
)


# ---------------------------------------------------------------------------
# Path-shape safety (adversarial audit of PR #69, findings 1 and 3)
#
# The audit ran real requests against the real (unmerged-fix) router and
# proved ``browse_folders``/``_validate_monitor_path`` had NO confinement
# at all -- ``GET browse-folders?path=C:\Users`` (200, real usernames),
# ``...?path=C:\Windows\System32`` (200, full listing), and
# ``...?path=\\localhost\C$`` (200, the admin share OVER UNC) all worked,
# and ``POST watch-folder-configs`` accepted ``C:\Windows\System32`` and
# bare ``C:\`` as 201-Created monitor paths.
#
# These checks are deliberately RAW-STRING parsers, not something built on
# ``pathlib``: verified empirically against this repo's own Python
# (3.13.5) that ``PureWindowsPath(r"\\host\share").is_absolute()`` is
# ``False`` and ``.drive`` is empty -- pathlib's own UNC/absolute detection
# cannot be trusted to reject a UNC path, on any Python version, and this
# suite's actual CI gate (``ci-test.yml``'s "Unit tests" job) runs on
# ``ubuntu-latest``, where a Windows-shaped string like ``C:\Windows``
# isn't a path pathlib recognizes as absolute AT ALL (backslash isn't a
# POSIX separator) -- a ``resolve()``-based check would silently no-op
# there. String-shape rejection behaves identically regardless of host OS,
# so it is what actually gets proven by CI; real ``Path.resolve()``
# containment against the station's fixed local drives is layered on top
# in ``browse_folders`` itself as Windows-real defense-in-depth (catches a
# junction/symlink escape), but that layer is inherently unverifiable on a
# Linux CI runner and is not where the regression coverage lives.
_WINDOWS_SYSTEM_DIR_NAMES = frozenset(
    {
        "windows",
        "program files",
        "program files (x86)",
        "programdata",
        "system volume information",
        "$recycle.bin",
        "recovery",
        "perflogs",
        "boot",
        "msocache",
        "documents and settings",
        "config.msi",
        "recycler",
    }
)

_UNSAFE_PATH_DETAIL = (
    "That path is not allowed. CivicCast will not browse or watch Windows system "
    "directories, an entire drive, or a raw device-namespace path."
)

_DRIVE_RELATIVE_RE = re.compile(r"^[A-Za-z]:(?![\\/])")
_WINDOWS_LOCAL_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def _normalize_windows_slashes(raw: str) -> str:
    return raw.replace("/", "\\")


def _is_unc_or_device_path(raw: str) -> bool:
    r"""True for a UNC network path (``\\host\share``) or the Win32
    extended-length / device-namespace prefix (``\\?\...``, ``\\.\...`` --
    both of which can themselves hide a UNC target, e.g. ``\\?\UNC\host\share``).
    A single leading-two-backslash check catches all of these at once.
    """

    return _normalize_windows_slashes(raw).startswith("\\\\")


def _is_device_prefix(raw: str) -> bool:
    normalized = _normalize_windows_slashes(raw)
    return normalized.startswith("\\\\?\\") or normalized.startswith("\\\\.\\")


def _unc_share_is_admin_or_hidden(raw: str) -> bool:
    r"""True if a UNC path's share name ends in ``$`` -- the built-in Windows
    administrative/hidden shares (``C$``, ``D$``, ``ADMIN$``, ``IPC$``, ...)
    that expose an entire drive or OS control surface over SMB, as opposed
    to an operator-created named share (``\\nas\Videos``) that spec S7 open
    decision 5 (D13) explicitly supports as a watch-folder source.
    """

    normalized = _normalize_windows_slashes(raw)
    if not normalized.startswith("\\\\"):
        return False
    segments = [s for s in normalized[2:].split("\\") if s]
    return len(segments) >= 2 and segments[1].endswith("$")


def _is_drive_relative(raw: str) -> bool:
    """True for ``C:Windows`` -- a drive letter with NO following separator,
    which Windows resolves against that drive's *current directory*
    (process-global, unpredictable for a web server) rather than an
    absolute location. Auditor-flagged edge case.
    """

    return bool(_DRIVE_RELATIVE_RE.match(raw))


def _has_traversal_segment(raw: str) -> bool:
    return any(seg == ".." for seg in _normalize_windows_slashes(raw).split("\\"))


def _windows_local_segments(raw: str) -> list[str] | None:
    r"""For a Windows-shaped absolute local path (``X:\...``), the path
    segments below the drive root (``.`` dropped). ``None`` for anything
    else (a POSIX path, a bare relative name, UNC, ...) so callers only
    apply this policy to Windows-local-drive-shaped input.
    """

    match = _WINDOWS_LOCAL_RE.match(_normalize_windows_slashes(raw))
    if match is None:
        return None
    return [seg for seg in match.group(2).split("\\") if seg not in ("", ".")]


def _reject_unsafe_watch_folder_path(raw: str) -> None:
    """Finding 3: a watch-folder ``monitor_path`` must not be a Windows
    device-namespace path, a Windows administrative/hidden UNC share, a
    drive-relative path, an entire drive, or a well-known system directory.
    NAS/SMB paths in general remain fully supported (D13) -- only the
    dangerous shapes above are rejected, not UNC as a class.
    """

    if _is_device_prefix(raw):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=r"Device-namespace paths (\\?\... or \\.\...) are not accepted. "
            "Use a normal drive letter or network share path.",
        )
    if _unc_share_is_admin_or_hidden(raw):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=r"Windows administrative shares (e.g. \\host\C$) are not accepted as "
            r"watch folders. Point at a named share instead (e.g. \\host\Videos).",
        )
    if _is_drive_relative(raw):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=r"Enter a full path like C:\Videos\Incoming, not a drive-relative path.",
        )
    if _has_traversal_segment(raw):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_UNSAFE_PATH_DETAIL
        )
    segments = _windows_local_segments(raw)
    if segments is None:
        return
    if not segments:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=r"An entire drive (e.g. C:\) cannot be a watch folder. Choose a specific "
            "subfolder.",
        )
    if segments[0].casefold() in _WINDOWS_SYSTEM_DIR_NAMES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_UNSAFE_PATH_DETAIL
        )


def _reject_unsafe_browse_path(raw: str) -> None:
    r"""Finding 1: the folder PICKER is local-disk-only by design (its own
    docstring: frontend and backend always run on the SAME station
    machine) -- unlike a typed-in watch-folder ``monitor_path``, UNC/network
    paths are rejected here outright, not just the admin-share subset, and
    browsing the bare ``Users`` directory is refused (it would enumerate
    every local account name on the station -- exactly what the audit's
    live ``GET browse-folders?path=C:\Users`` demonstration exposed). A
    subfolder already known to be UNDER ``Users`` (e.g. via an existing
    config's path) is unaffected -- only the exact ``C:\Users`` listing is
    refused, not the whole subtree, so editing an existing local-profile
    watch folder still works.
    """

    if _is_unc_or_device_path(raw):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Network paths and device-namespace paths are not browsable here. This "
            "picker only lists local station drives.",
        )
    if _is_drive_relative(raw):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=r"Enter a full path like C:\Videos\Incoming, not a drive-relative path.",
        )
    if _has_traversal_segment(raw):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_UNSAFE_PATH_DETAIL
        )
    segments = _windows_local_segments(raw)
    if segments is None:
        return
    if not segments:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=r"An entire drive (e.g. C:\) cannot be browsed as one listing. Pick a "
            "subfolder.",
        )
    top = segments[0].casefold()
    if top in _WINDOWS_SYSTEM_DIR_NAMES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_UNSAFE_PATH_DETAIL
        )
    if top == "users" and len(segments) == 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=r"Browsing C:\Users directly is not allowed (it would list every local "
            "account name on this station).",
        )


def _enforce_local_drive_containment(resolved: Path) -> None:
    r"""Windows-real defense-in-depth on top of the string-shape checks
    above: after ``Path.resolve()`` follows any symlink/junction, require
    the REAL target to still be under one of the station's fixed local
    drive roots, and re-run the system-directory denylist against the
    RESOLVED parts -- so a junction inside an otherwise-allowed folder that
    secretly points at ``C:\Windows`` is caught even though the literal
    input string looked innocent. Only meaningful when ``os.name == "nt"``
    (the only OS this product ships on); a no-op everywhere ``Path.resolve``
    can't reason about Windows drives, which is also why this layer isn't
    where this fix's regression coverage lives (see the module comment
    above) -- CI's "Unit tests" job runs on ``ubuntu-latest``.
    """

    if os.name != "nt":
        return
    roots = {Path(root).resolve() for root in _list_drive_roots()}
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_UNSAFE_PATH_DETAIL
        )
    if any(part.casefold() in _WINDOWS_SYSTEM_DIR_NAMES for part in resolved.parts):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_UNSAFE_PATH_DETAIL
        )


def _validate_monitor_path(monitor_path: str) -> None:
    """Fail closed with an operator-readable 422 rather than silently accepting
    a path the daemon will only discover is broken on its next poll (finding 3,
    candidate #17 tester report: "no clear message when the folder is missing
    or unreadable"). A watch folder must be an existing, listable directory --
    the same check :meth:`~civiccast.schedule.watch_folder_worker.WatchFolderWorker
    ._list_candidate_files` performs, just surfaced at config time instead of
    silently degrading on the first poll.
    """

    _reject_unsafe_watch_folder_path(monitor_path)
    try:
        path = Path(monitor_path)
        is_dir = path.is_dir()
    except OSError:
        is_dir = False
    if not is_dir:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_UNREADABLE_PATH_DETAIL
        )
    try:
        next(iter(os.scandir(monitor_path)), None)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{_UNREADABLE_PATH_DETAIL} ({exc.strerror or exc})",
        ) from exc


staff_router = APIRouter(prefix="/api/staff", tags=["media-lifecycle"])


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


@staff_router.get(
    "/assets/readiness-dashboard",
    response_model=ReadinessDashboardResponse,
    summary="Operator readiness dashboard across every asset",
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
    responses={503: {"description": _DB_NOT_READY_DETAIL}},
)
def get_readiness_dashboard(
    store: Any = Depends(get_media_lifecycle_store),
) -> ReadinessDashboardResponse:
    return _require_store(store).dashboard()


@staff_router.get(
    "/assets/{asset_id}/readiness",
    response_model=AssetReadinessResponse,
    summary="One asset's readiness badge state",
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
    responses={404: {"description": "Asset not found"}, 503: {"description": _DB_NOT_READY_DETAIL}},
)
def get_asset_readiness(
    asset_id: str, store: Any = Depends(get_media_lifecycle_store)
) -> AssetReadinessResponse:
    resolved = _require_store(store)
    result = resolved.get_readiness(asset_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Asset not found: {asset_id}"
        )
    return result


@staff_router.put(
    "/assets/{asset_id}/legal-hold",
    response_model=AssetReadinessResponse,
    summary="Set or clear a legal hold (blocks retention expiry per CLAUDE.md §4.6)",
    dependencies=[Depends(require_any_role("records_clerk", "support_admin"))],
    responses={404: {"description": "Asset not found"}, 503: {"description": _DB_NOT_READY_DETAIL}},
)
def set_legal_hold(
    asset_id: str, payload: LegalHoldInput, store: Any = Depends(get_media_lifecycle_store)
) -> AssetReadinessResponse:
    resolved = _require_store(store)
    try:
        resolved.set_legal_hold(asset_id, hold=payload.legal_hold, reason=payload.reason)
    except AssetNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Asset not found: {asset_id}"
        ) from exc
    result = resolved.get_readiness(asset_id)
    assert result is not None  # just proven to exist above
    return result


# ---------------------------------------------------------------------------
# Replace-source (spec §2 net-new "ReplaceMediaWorkflow")
# ---------------------------------------------------------------------------


@staff_router.put(
    "/assets/{asset_id}/replace-source",
    response_model=AssetReadinessResponse,
    summary="Swap out an asset's backing file; the old file is archived, not deleted",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    responses={
        404: {"description": "Asset not found"},
        422: {"description": "Unsupported file format"},
        503: {"description": _DB_NOT_READY_DETAIL},
    },
)
async def replace_asset_source(
    asset_id: str,
    file: UploadFile = File(..., description="Replacement video file."),
    store: Any = Depends(get_media_lifecycle_store),
) -> AssetReadinessResponse:
    """Corrupt or wrong source file? Replace it -- old file is archived, not deleted.

    Unlike ``POST /api/staff/assets/{asset_id}/relink`` (which requires the
    replacement to match the original's duration/codec within a tolerance --
    "same recording, re-encoded"), this endpoint accepts ANY validated file:
    the spec's own scenario is "recording is corrupt, need a different file
    entirely." A fresh ffprobe validation still runs; only the tolerance
    check is skipped.
    """

    resolved = _require_store(store)
    upload_dir_str = os.environ.get("CIVICCAST_UPLOAD_DIR")
    if not upload_dir_str:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Upload directory not configured. Set CIVICCAST_UPLOAD_DIR.",
        )
    existing = resolved.get_readiness(asset_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Asset not found: {asset_id}"
        )

    upload_dir = Path(upload_dir_str).resolve()
    asset_dir = (upload_dir / asset_id).resolve()
    incoming_dir = (upload_dir / ".incoming").resolve()
    if not asset_dir.is_relative_to(upload_dir) or not incoming_dir.is_relative_to(upload_dir):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid asset_id."
        )

    raw_name = file.filename or "replacement"
    base_name = PurePosixPath(raw_name.replace("\\", "/")).name or "replacement"
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", base_name) or "replacement"
    if safe_name in (".", ".."):
        safe_name = "replacement"
    dest_path = (asset_dir / safe_name).resolve()
    if not dest_path.is_relative_to(asset_dir):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid filename."
        )

    await asyncio.to_thread(incoming_dir.mkdir, parents=True, exist_ok=True)
    temp_path = (incoming_dir / f"{asset_id}-{uuid.uuid4().hex}.replace").resolve()

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
                        detail=f"Upload exceeds maximum size of {max_bytes} bytes.",
                    )
                await asyncio.to_thread(f.write, chunk)
    finally:
        await file.close()

    success = False
    try:
        try:
            ffprobe_result = await asyncio.to_thread(run_ffprobe, temp_path)
        except FfprobeNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        try:
            validate_ingest(ffprobe_result)
        except UnsupportedFormatError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.reason
            ) from exc

        # Archive the old file (rename aside, never delete) before the new
        # file takes its place -- spec DONE criteria #8: "old file archived,
        # new file validated." Any existing file already in the asset's
        # directory is old content (including ``dest_path`` on same-name
        # replacement); the new upload is staged at ``temp_path``.
        # Auxiliary files are archived too, but the audit must name the
        # primary source recorded on the asset, regardless of directory order.
        original_source = resolved.get_asset_source_path(asset_id)
        original_source_path = Path(original_source).resolve() if original_source else None
        archived_old_path: str | None = None
        await asyncio.to_thread(asset_dir.mkdir, parents=True, exist_ok=True)
        for candidate in asset_dir.glob("*"):
            if candidate.is_file() and not candidate.name.startswith(f"{asset_id}.replaced-"):
                archived_name = f"{asset_id}.replaced-{uuid.uuid4().hex[:8]}-{candidate.name}"
                archived_path = asset_dir / archived_name
                await asyncio.to_thread(os.replace, candidate, archived_path)
                if candidate == original_source_path:
                    archived_old_path = str(archived_path)

        await asyncio.to_thread(os.replace, temp_path, dest_path)

        try:
            content_hash = await asyncio.to_thread(hash_file, dest_path)
        except OSError:
            content_hash = None

        await asyncio.to_thread(
            resolved.apply_replace_source,
            asset_id,
            new_file_path=str(dest_path),
            file_size_bytes=dest_path.stat().st_size,
            codec_video=ffprobe_result.codec_video,
            codec_audio=ffprobe_result.codec_audio,
            width_px=ffprobe_result.width_px,
            height_px=ffprobe_result.height_px,
            bitrate_bps=ffprobe_result.bitrate_bps,
            format_name=ffprobe_result.format_name,
            duration_seconds=ffprobe_result.duration_seconds,
            content_hash=content_hash,
            thumbnail_path=None,
            archived_old_path=archived_old_path,
        )
        success = True
    finally:
        if not success:
            await asyncio.to_thread(temp_path.unlink, missing_ok=True)

    result = resolved.get_readiness(asset_id)
    assert result is not None
    return result


# ---------------------------------------------------------------------------
# Watch-folder configs
# ---------------------------------------------------------------------------

watch_folder_router = APIRouter(prefix="/media-lifecycle", tags=["media-lifecycle"])


@watch_folder_router.get(
    "/watch-folder-configs",
    response_model=list[WatchFolderConfigResponse],
    summary="List configured auto-ingest watch folders",
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
)
def list_watch_folder_configs(
    store: Any = Depends(get_media_lifecycle_store),
) -> list[WatchFolderConfigResponse]:
    return _require_store(store).list_watch_folder_configs()


@watch_folder_router.post(
    "/watch-folder-configs",
    response_model=WatchFolderConfigResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a watch folder (local disk, USB, or NAS/SMB path)",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    responses={422: {"description": "monitor_path does not exist or is not readable"}},
)
def create_watch_folder_config(
    payload: WatchFolderConfigInput, store: Any = Depends(get_media_lifecycle_store)
) -> WatchFolderConfigResponse:
    _validate_monitor_path(payload.monitor_path)
    return _require_store(store).create_watch_folder_config(payload)


@watch_folder_router.put(
    "/watch-folder-configs/{config_id}",
    response_model=WatchFolderConfigResponse,
    summary="Update a watch folder config",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    responses={
        404: {"description": "Watch folder config not found"},
        422: {"description": "monitor_path does not exist or is not readable"},
    },
)
def update_watch_folder_config(
    config_id: str, payload: WatchFolderConfigInput, store: Any = Depends(get_media_lifecycle_store)
) -> WatchFolderConfigResponse:
    _validate_monitor_path(payload.monitor_path)
    try:
        return _require_store(store).update_watch_folder_config(config_id, payload)
    except WatchFolderConfigNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Watch folder config not found: {config_id}",
        ) from exc


@watch_folder_router.post(
    "/watch-folder-configs/{config_id}/scan-now",
    response_model=WatchFolderScanNowResponse,
    summary="Scan one watch folder immediately, bypassing its poll interval",
    dependencies=[Depends(require_any_role(*_FS_ROLES))],
    responses={
        404: {"description": "Watch folder config not found"},
        409: {"description": "A scan for this watch folder is already in progress"},
        503: {"description": "Watch-folder daemon not wired, or CIVICCAST_UPLOAD_DIR unset"},
    },
)
def scan_watch_folder_now(
    config_id: str,
    store: Any = Depends(get_media_lifecycle_store),
    worker: Any = Depends(get_watch_folder_worker),
) -> WatchFolderScanNowResponse:
    """Finding 4 (candidate #17): "Last poll: never" with no way to force a
    check reads as broken even when the daemon is working -- this runs the
    SAME per-folder scan (`WatchFolderWorker._scan_one_folder`) the daemon's
    own poll loop uses, immediately, and returns what it found so the
    operator gets real feedback instead of waiting out the poll interval.
    """

    resolved_store = _require_store(store)
    if worker is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The watch-folder daemon is not running in this deployment.",
        )
    try:
        result = worker.scan_now(config_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Watch folder config not found: {config_id}",
        ) from exc
    except WatchFolderScanInProgressError as exc:
        # Finding 4 (adversarial audit of PR #69): scan_now had no lock, no
        # idempotency guard, no rate limit -- 5 concurrent scan-now calls on
        # one config produced unhandled database errors under the SQLite
        # test harness. WatchFolderWorker now serializes per-config scans
        # (against itself, other scan-now calls, and the daemon's own
        # run_once picking up the same config) and raises this when a scan
        # is already in flight; the operator gets an actionable 409 instead
        # of a raced or duplicated scan.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    configs = resolved_store.list_watch_folder_configs()
    fresh = next((c for c in configs if c.config_id == config_id), None)
    if fresh is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Watch folder config not found: {config_id}",
        )
    return WatchFolderScanNowResponse(
        config=fresh,
        healthy=result.healthy,
        files_seen=result.files_seen,
        files_ingested=result.files_ingested,
        files_reprocessed=result.files_reprocessed,
        files_failed=result.files_failed,
        error=result.error,
    )


def _list_drive_roots() -> list[str]:
    if os.name == "nt":
        return [
            f"{letter}:\\" for letter in string.ascii_uppercase if Path(f"{letter}:\\").exists()
        ]
    return ["/"]


@watch_folder_router.get(
    "/browse-folders",
    response_model=FolderBrowseResponse,
    summary="List subdirectories of a path, for the non-technical watch-folder picker",
    dependencies=[Depends(require_any_role(*_FS_ROLES))],
)
def browse_folders(path: str | None = None) -> FolderBrowseResponse:
    """Finding 3 (candidate #17): a non-technical operator cannot be expected
    to type an exact filesystem path. A browser cannot hand back an absolute
    path itself (the File System Access API and ``<input webkitdirectory>``
    both withhold it for security), but this app's frontend and backend
    always run on the SAME station machine -- so the backend lists local
    directories for a picker UI to navigate instead of asking the operator
    to type one.

    No ``path`` (or an empty one) lists drive roots on Windows / ``/`` on
    POSIX. Files are never listed, only directories -- this endpoint picks a
    folder, it does not browse file content.
    """

    separator = "\\" if os.name == "nt" else "/"
    if not path:
        roots = _list_drive_roots()
        return FolderBrowseResponse(
            current_path=None,
            parent_path=None,
            separator=separator,
            entries=[FolderBrowseEntry(name=root, path=root) for root in roots],
            readable=True,
        )

    _reject_unsafe_browse_path(path)

    resolved = Path(path)
    if os.name == "nt":
        try:
            real = resolved.resolve(strict=False)
        except OSError:
            real = resolved
        _enforce_local_drive_containment(real)

    parent = str(resolved.parent) if resolved.parent != resolved else None
    try:
        with os.scandir(path) as it:
            subdirs = sorted(
                (entry.name for entry in it if entry.is_dir(follow_symlinks=False)),
                key=str.casefold,
            )
    except OSError as exc:
        return FolderBrowseResponse(
            current_path=path,
            parent_path=parent,
            separator=separator,
            entries=[],
            readable=False,
            error=exc.strerror or str(exc),
        )

    entries = [FolderBrowseEntry(name=name, path=str(resolved / name)) for name in subdirs[:1000]]
    return FolderBrowseResponse(
        current_path=str(resolved),
        parent_path=parent,
        separator=separator,
        entries=entries,
        readable=True,
    )


@watch_folder_router.delete(
    "/watch-folder-configs/{config_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a watch folder config",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    responses={404: {"description": "Watch folder config not found"}},
)
def delete_watch_folder_config(
    config_id: str, store: Any = Depends(get_media_lifecycle_store)
) -> None:
    try:
        _require_store(store).delete_watch_folder_config(config_id)
    except WatchFolderConfigNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Watch folder config not found: {config_id}",
        ) from exc


# ---------------------------------------------------------------------------
# Retention policy automation
# ---------------------------------------------------------------------------


@watch_folder_router.get(
    "/retention-policies",
    response_model=list[AssetRetentionPolicyResponse],
    summary="List retention automation rules",
    dependencies=[Depends(require_any_role(*_READ_ROLES, "records_clerk"))],
)
def list_retention_policies(
    store: Any = Depends(get_media_lifecycle_store),
) -> list[AssetRetentionPolicyResponse]:
    return _require_store(store).list_retention_policies()


@watch_folder_router.post(
    "/retention-policies",
    response_model=AssetRetentionPolicyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a retention automation rule",
    dependencies=[Depends(require_any_role("records_clerk", "setup_admin"))],
)
def create_retention_policy(
    payload: AssetRetentionPolicyInput, store: Any = Depends(get_media_lifecycle_store)
) -> AssetRetentionPolicyResponse:
    return _require_store(store).create_retention_policy(payload)


@watch_folder_router.put(
    "/retention-policies/{policy_id}",
    response_model=AssetRetentionPolicyResponse,
    summary="Update a retention automation rule",
    dependencies=[Depends(require_any_role("records_clerk", "setup_admin"))],
    responses={404: {"description": "Retention policy not found"}},
)
def update_retention_policy(
    policy_id: str,
    payload: AssetRetentionPolicyInput,
    store: Any = Depends(get_media_lifecycle_store),
) -> AssetRetentionPolicyResponse:
    try:
        return _require_store(store).update_retention_policy(policy_id, payload)
    except AssetRetentionPolicyNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Retention policy not found: {policy_id}"
        ) from exc


@watch_folder_router.delete(
    "/retention-policies/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a retention automation rule",
    dependencies=[Depends(require_any_role("records_clerk", "setup_admin"))],
    responses={404: {"description": "Retention policy not found"}},
)
def delete_retention_policy(
    policy_id: str, store: Any = Depends(get_media_lifecycle_store)
) -> None:
    try:
        _require_store(store).delete_retention_policy(policy_id)
    except AssetRetentionPolicyNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Retention policy not found: {policy_id}"
        ) from exc


@watch_folder_router.post(
    "/retention-policies/apply",
    summary="Apply enabled retention rules to every asset now",
    dependencies=[Depends(require_any_role("records_clerk", "setup_admin"))],
)
def apply_retention_policies(store: Any = Depends(get_media_lifecycle_store)) -> dict[str, int]:
    return {"assets_changed": _require_store(store).apply_retention_policies()}


# ---------------------------------------------------------------------------
# Storage budget
# ---------------------------------------------------------------------------


@watch_folder_router.get(
    "/storage-budget",
    response_model=StorageBudgetResponse,
    summary="Storage used vs the configured budget, by retention tier",
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
)
def get_storage_budget(store: Any = Depends(get_media_lifecycle_store)) -> StorageBudgetResponse:
    raw_budget = os.environ.get("CIVICCAST_MEDIA_STORAGE_BUDGET_BYTES", "").strip()
    budget_bytes = int(raw_budget) if raw_budget else None
    return _require_store(store).storage_budget(budget_bytes=budget_bytes)


# ---------------------------------------------------------------------------
# Missing media
# ---------------------------------------------------------------------------


def get_missing_media_reader() -> Any:
    """DI seam for the live missing-media join; wired to the lifecycle worker."""

    return None


@watch_folder_router.get(
    "/missing-media",
    response_model=list[MissingMediaAlertRow],
    summary="Scheduled items whose backing asset is not ready within the horizon",
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
)
def list_missing_media(
    reader: Any = Depends(get_missing_media_reader),
) -> list[MissingMediaAlertRow]:
    if reader is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY_DETAIL
        )
    return [MissingMediaAlertRow(**row) for row in reader.list_missing_media()]


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@watch_folder_router.get(
    "/audit-log",
    response_model=list[LifecycleAuditEntryResponse],
    summary="Media lifecycle worker audit trail (dry-run entries included)",
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
)
def list_audit_log(
    asset_id: str | None = None,
    limit: int = 100,
    store: Any = Depends(get_media_lifecycle_store),
) -> list[LifecycleAuditEntryResponse]:
    return _require_store(store).list_audit_log(asset_id=asset_id, limit=limit)


# ``staff_router`` is the single router civiccast.app registers. It carries
# the ``/assets/...`` sub-resource routes directly and folds in every
# ``/media-lifecycle/...`` route from ``watch_folder_router`` above (kept as
# a separate APIRouter above purely for this file's own readability).
staff_router.include_router(watch_folder_router)
