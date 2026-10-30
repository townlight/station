# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Public contributor and staff review routes."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import ValidationError

from civiccast.auth.rate_limit import AuthRateLimiter
from civiccast.auth.roles import require_any_role
from civiccast.contribute.models import (
    BrokenMediaGateResult,
    ContributorNotificationOutbox,
    ContributorReviewQueue,
    ContributorReviewRequest,
    ContributorSubmission,
    ContributorSubmissionCreate,
    ContributorSubmissionReceipt,
    ProducerActivityReport,
    PublicSubmissionStatus,
    SubmissionAgreementCatalog,
    SubmissionMediaReference,
    utc_now,
)
from civiccast.contribute.store import (
    ContributorReceiptTokenError,
    ContributorReviewConflictError,
    ContributorStoreError,
    ContributorSubmissionNotFoundError,
    ContributorSubmissionStore,
    ContributorUploadAlreadyUsedError,
    default_contributor_store_path,
    default_contributor_upload_dir,
    resolve_contributor_upload_path,
)
from civiccast.schedule.ingest import (
    FfmpegNotFoundError,
    FfprobeError,
    FfprobeNotFoundError,
    FfprobeResult,
    UnsupportedFormatError,
    extract_thumbnail,
    hash_file,
    run_ffprobe,
    validate_ingest,
)
from civiccast.schedule.models import SCHEDULE_MODE_PREMIERE, ScheduleItemCreate, ScheduleModeValue
from civiccast.schedule.paths import resolve_upload_root

# TASK A: reusing the schedule module's own dependency callables (not
# reinventing store lookup) so accept/schedule bind to the SAME asset store
# and schedule store every other staff route uses -- civiccast.app.create_app
# already overrides these for both durable (Postgres) and ephemeral/dev mode.
from civiccast.schedule.router import get_postgres_store, get_schedule_store
from civiccast.vod.store import AssetAlreadyExistsError

_LOG = logging.getLogger(__name__)

public_router = APIRouter(prefix="/api/public/contribute", tags=["public", "contribute"])
staff_router = APIRouter(prefix="/api/staff/contribute", tags=["staff", "contribute"])

_ASSET_STORE_NOT_READY_DETAIL = (
    "Durable storage is not ready. Open Setup and choose Prepare storage, "
    "or set DATABASE_URL for a technical deployment."
)

# Audit item #27: the public status route verifies a receipt secret
# (secrets.compare_digest in the store) and had no rate limit anywhere on
# the path — the same gap the paywall verify/access routes already close
# with a per-IP sliding window. 30/min is generous for a contributor page
# polling its own status and still pointless for guessing a >=24-char
# token. Process-local, same trade-off as the paywall siblings.
# GauntletGate QA-2: POST /api/public/contribute/uploads had no auth, no rate
# limit and no size ceiling, and it touches no database -- so an anonymous caller
# could stream unlimited files of unlimited size onto the station's disk even on
# a station whose storage was not yet configured. Community producers do submit
# real programming, so the fix is a bounded ceiling plus a per-IP window rather
# than authentication. Both are env-tunable for stations with heavier intake.
DEFAULT_MAX_CONTRIBUTOR_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
MAX_CONTRIBUTOR_UPLOAD_BYTES = DEFAULT_MAX_CONTRIBUTOR_UPLOAD_BYTES


def _max_upload_bytes() -> int:
    """Resolved per call so the ceiling is tunable without a reimport."""
    raw = os.environ.get("CIVICCAST_CONTRIBUTOR_MAX_UPLOAD_BYTES")
    if not raw:
        return DEFAULT_MAX_CONTRIBUTOR_UPLOAD_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_CONTRIBUTOR_UPLOAD_BYTES
    return value if value > 0 else DEFAULT_MAX_CONTRIBUTOR_UPLOAD_BYTES


# GauntletGate rc18 QA-2: the per-request ceiling and per-IP window above bound
# ONE call and ONE hour. They do not bound the total. Every accepted upload is
# written here and nothing deletes it, so at the shipped defaults a single
# unauthenticated, un-rotated address can place ~480 GB/day on a station's disk
# indefinitely -- and CivicCast targets small self-hosted stations on commodity
# disks. This is the missing aggregate bound.
#
# It REFUSES rather than reclaims, deliberately. An age-based sweep is the wrong
# instrument while nothing reconciles `upload_ref` against the files on disk: it
# could delete a contributor's pending programme. Refusing is honest, reversible
# by an operator, and cannot destroy someone's submission. Reclamation belongs
# with the reference-tracking work, not inside this fix.
DEFAULT_UPLOAD_DIR_MAX_BYTES = 50 * 1024 * 1024 * 1024  # 50 GiB


def _upload_dir_max_bytes() -> int:
    """Total ceiling for the contributor upload directory, env-tunable."""
    raw = os.environ.get("CIVICCAST_CONTRIBUTOR_UPLOAD_DIR_MAX_BYTES", "").strip()
    if not raw:
        return DEFAULT_UPLOAD_DIR_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_UPLOAD_DIR_MAX_BYTES
    return value if value > 0 else DEFAULT_UPLOAD_DIR_MAX_BYTES


def _upload_dir_bytes(upload_dir: Path) -> int:
    """Bytes currently held in the upload directory (top level, non-recursive)."""
    total = 0
    try:
        for entry in upload_dir.iterdir():
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    continue
    except FileNotFoundError:
        return 0
    return total


# QA-2 (Critical), part 3: the per-request ceiling and the aggregate directory
# ceiling above still let ONE anonymous, un-rotated address alone consume the
# entire 50 GiB directory budget and lock out every other contributor. This
# adds a per-IP cumulative budget, in ADDITION to (not instead of) the
# directory ceiling, so no single source address can exhaust it alone.
DEFAULT_CONTRIBUTOR_UPLOAD_PER_IP_MAX_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB


def _per_ip_upload_max_bytes() -> int:
    """Per-IP cumulative upload ceiling, env-tunable."""
    raw = os.environ.get("CIVICCAST_CONTRIBUTOR_UPLOAD_PER_IP_MAX_BYTES", "").strip()
    if not raw:
        return DEFAULT_CONTRIBUTOR_UPLOAD_PER_IP_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CONTRIBUTOR_UPLOAD_PER_IP_MAX_BYTES
    return value if value > 0 else DEFAULT_CONTRIBUTOR_UPLOAD_PER_IP_MAX_BYTES


class ContributorUploadByteBudget:
    """In-process cumulative bytes-accepted counter, keyed by resolved client IP.

    Same "in-process, resets on restart" trade-off as
    ``civiccast.auth.rate_limit.AuthRateLimiter`` -- a soft budget that stops
    one address from consuming the whole directory ceiling alone, not a
    durable ledger.

    The upload endpoint is a plain ``def`` handler, so Starlette runs it in the
    AnyIO worker threadpool -- genuinely parallel, not just async-interleaved.
    An earlier version read the caller's total once before streaming and only
    added on success, with no lock, so N concurrent uploads from one address
    each saw the same stale total and each streamed up to the ceiling. The
    counter is therefore mutated only through :meth:`try_reserve` and
    :meth:`release` under a lock, and callers reserve each chunk against the
    *live* total before writing it.
    """

    # rc18 re-gate PE-2/TE-1: the aggregate directory ceiling's in-flight bytes
    # live in ``_totals`` under this sentinel key (never a real client IP), not a
    # separate attribute, so the SAME lock AND the same instrumented-dict test
    # harness that guard the per-IP counter also guard the directory reservation.
    _DIR_SLOT_KEY = "\x00contributor-upload-dir-in-flight"

    def __init__(self) -> None:
        self._totals: dict[str, int] = {}
        # rc18 re-gate TE-3: which committed file holds which IP's bytes, so the
        # reaper can return the right IP's budget when it deletes a stale upload.
        self._paths: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()

    def total(self, key: str) -> int:
        with self._lock:
            return self._totals.get(key, 0)

    def try_reserve(self, key: str, num_bytes: int, ceiling: int) -> bool:
        """Atomically reserve ``num_bytes`` if it keeps ``key`` at/under ``ceiling``.

        Returns True and increments the live total on success; returns False and
        changes nothing if the reservation would exceed the ceiling. Because the
        check and the increment happen under one lock, concurrent reservations
        from the same key cannot both succeed past the ceiling.
        """

        with self._lock:
            current = self._totals.get(key, 0)
            if current + num_bytes > ceiling:
                return False
            self._totals[key] = current + num_bytes
            return True

    def release(self, key: str, num_bytes: int) -> None:
        """Return previously reserved bytes (an aborted/partial upload was deleted)."""

        if num_bytes <= 0:
            return
        with self._lock:
            remaining = self._totals.get(key, 0) - num_bytes
            if remaining > 0:
                self._totals[key] = remaining
            else:
                # Drop the entry entirely at zero so the dict does not grow one
                # permanent entry per address that ever uploaded.
                self._totals.pop(key, None)

    def reserve_dir_slot(self, upload_dir: Path, ceiling: int, max_bytes: int) -> bool:
        """Atomically admit one upload against the aggregate directory ceiling.

        rc18 re-gate PE-2/TE-1. The directory ceiling used to be a plain
        unlocked ``_upload_dir_bytes(dir) >= ceiling`` read-then-act -- the
        exact TOCTOU QA-2 fixed for the per-IP counter but left here. Concurrent
        uploads each passed that check before any of them wrote, so the
        directory overshot. This reserves the upload's WORST CASE (``max_bytes``)
        under the same lock as the scan, so N in-flight uploads reserve
        ``N * max_bytes`` and peak on-disk bytes can never exceed ``ceiling``.
        It is deliberately conservative -- an upload is refused if its worst case
        would not fit even when its actual size would -- because erring toward
        refusing protects a small station's disk, which is the whole finding.
        """

        with self._lock:
            in_flight = self._totals.get(self._DIR_SLOT_KEY, 0)
            if _upload_dir_bytes(upload_dir) + in_flight + max_bytes > ceiling:
                return False
            self._totals[self._DIR_SLOT_KEY] = in_flight + max_bytes
            return True

    def release_dir_slot(self, max_bytes: int) -> None:
        """Return an upload's worst-case directory reservation once it finishes."""

        if max_bytes <= 0:
            return
        with self._lock:
            remaining = self._totals.get(self._DIR_SLOT_KEY, 0) - max_bytes
            if remaining > 0:
                self._totals[self._DIR_SLOT_KEY] = remaining
            else:
                self._totals.pop(self._DIR_SLOT_KEY, None)

    def record_committed(self, resolved_path: str, key: str, num_bytes: int) -> None:
        """Record that the committed file at ``resolved_path`` holds ``num_bytes`` for ``key``.

        The per-IP total was already incremented chunk by chunk through
        :meth:`try_reserve`; this only remembers which path holds it, so
        :meth:`release_path` can return it to the right IP when the reaper
        deletes the file (rc18 re-gate TE-3).
        """

        if num_bytes <= 0:
            return
        with self._lock:
            self._paths[resolved_path] = (key, num_bytes)

    def release_path(self, resolved_path: str) -> None:
        """Return a reaped file's bytes to the IP that uploaded it (TE-3).

        Idempotent: a path the accountant never recorded -- an upload from a
        previous process, or one already released -- is a no-op, so the reaper
        can call it for every file it deletes without tracking which it owns.
        """

        with self._lock:
            owner = self._paths.pop(resolved_path, None)
            if owner is None:
                return
            key, num_bytes = owner
            remaining = self._totals.get(key, 0) - num_bytes
            if remaining > 0:
                self._totals[key] = remaining
            else:
                self._totals.pop(key, None)


# Fallback singleton for callers/tests that hit this router outside
# create_app() (mirrors the _upload_rate_limiter/_status_rate_limiter
# fallback pattern below); create_app() installs a fresh instance on
# app.state so tests never share buckets across app instances.
_upload_byte_budget = ContributorUploadByteBudget()


def _get_upload_byte_budget(request: Request) -> ContributorUploadByteBudget:
    budget = getattr(request.app.state, "contributor_upload_byte_budget", None)
    if isinstance(budget, ContributorUploadByteBudget):
        return budget
    return _upload_byte_budget


UPLOAD_RATE_LIMIT = int(os.environ.get("CIVICCAST_CONTRIBUTOR_UPLOAD_RATE_LIMIT", 10))
UPLOAD_RATE_WINDOW_SECONDS = 3600
_upload_rate_limiter = AuthRateLimiter()


def _enforce_upload_rate_limit(request: Request) -> None:
    from civiccast.common.trusted_proxy import resolve_client_ip

    limiter = getattr(request.app.state, "auth_rate_limiter", None)
    if not isinstance(limiter, AuthRateLimiter):
        limiter = _upload_rate_limiter
    key = f"contribute-upload:{resolve_client_ip(request)}"
    if limiter.allow(key, limit=UPLOAD_RATE_LIMIT, window_seconds=UPLOAD_RATE_WINDOW_SECONDS):
        return
    retry_after = limiter.retry_after_seconds(key, window_seconds=UPLOAD_RATE_WINDOW_SECONDS)
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many contributor uploads from this address. Wait before trying again.",
        headers={"Retry-After": str(retry_after)},
    )


_STATUS_RATE_LIMIT = 30
_STATUS_RATE_WINDOW_SECONDS = 60
_status_rate_limiter = AuthRateLimiter()


def _enforce_status_rate_limit(request: Request) -> None:
    from civiccast.common.trusted_proxy import resolve_client_ip

    # Prefer the per-app limiter create_app() owns (fresh per app instance,
    # so tests and Mode-B hosts never share buckets); fall back to the
    # module-level one when this router is mounted outside create_app.
    limiter = getattr(request.app.state, "auth_rate_limiter", None)
    if not isinstance(limiter, AuthRateLimiter):
        limiter = _status_rate_limiter
    key = f"contribute-status:{resolve_client_ip(request)}"
    if limiter.allow(key, limit=_STATUS_RATE_LIMIT, window_seconds=_STATUS_RATE_WINDOW_SECONDS):
        return
    retry_after = limiter.retry_after_seconds(key, window_seconds=_STATUS_RATE_WINDOW_SECONDS)
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many submission status requests. Wait before trying again.",
        headers={"Retry-After": str(retry_after)},
    )


# rc18 re-gate PE-1 (Critical): POST /submissions was the one public POST with no
# per-IP limiter -- /uploads and the status route both close that gap, this one was
# missed, so an anonymous caller could flood the operator review queue as fast as it
# could send requests. Same per-IP sliding window as the sibling routes; env-tunable
# and resolved per call so a station can widen it without a reimport. A submission is
# lighter than an upload (no file transfer) but each one enqueues operator work, so
# the default is generous-but-bounded rather than unlimited.
DEFAULT_SUBMISSION_RATE_LIMIT = 20
SUBMISSION_RATE_WINDOW_SECONDS = 3600
_submission_rate_limiter = AuthRateLimiter()


def _submission_rate_limit() -> int:
    raw = os.environ.get("CIVICCAST_CONTRIBUTOR_SUBMISSION_RATE_LIMIT", "").strip()
    if not raw:
        return DEFAULT_SUBMISSION_RATE_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SUBMISSION_RATE_LIMIT
    return value if value > 0 else DEFAULT_SUBMISSION_RATE_LIMIT


def _enforce_submission_rate_limit(request: Request) -> None:
    from civiccast.common.trusted_proxy import resolve_client_ip

    limiter = getattr(request.app.state, "auth_rate_limiter", None)
    if not isinstance(limiter, AuthRateLimiter):
        limiter = _submission_rate_limiter
    key = f"contribute-submission:{resolve_client_ip(request)}"
    if limiter.allow(
        key, limit=_submission_rate_limit(), window_seconds=SUBMISSION_RATE_WINDOW_SECONDS
    ):
        return
    retry_after = limiter.retry_after_seconds(key, window_seconds=SUBMISSION_RATE_WINDOW_SECONDS)
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many contributor submissions from this address. Wait before trying again.",
        headers={"Retry-After": str(retry_after)},
    )


def get_contributor_store(request: Request) -> ContributorSubmissionStore:
    store = getattr(request.app.state, "contributor_submission_store", None)
    if isinstance(store, ContributorSubmissionStore):
        return store
    try:
        store = ContributorSubmissionStore(default_contributor_store_path())
    except ContributorStoreError as exc:
        # ContributorStoreError wraps a JSON store read/write OSError, whose
        # text commonly names the store's absolute path (same class of leak
        # as the upload_ref finding this router was fixed for). This
        # dependency backs both the public and staff routers, so the
        # generic detail matters for the public ones; log the real cause
        # server-side either way.
        _LOG.error("contributor submission store is not available: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Contributor submission storage is not ready. Please contact the station.",
        ) from exc
    request.app.state.contributor_submission_store = store
    return store


@public_router.get(
    "/agreements/current",
    response_model=SubmissionAgreementCatalog,
    summary="Read current contributor submission agreement",
)
def read_current_submission_agreement(
    store: ContributorSubmissionStore = Depends(get_contributor_store),
) -> SubmissionAgreementCatalog:
    return store.current_agreement()


@public_router.post(
    "/uploads",
    response_model=SubmissionMediaReference,
    status_code=status.HTTP_201_CREATED,
    summary="Upload contributor media into the intake holding area",
    responses={
        413: {"description": "Contributor media exceeds the station's per-file upload limit"},
        429: {"description": "Too many uploads, or this address has used its upload budget"},
        503: {"description": "Contributor upload storage is not ready or could not be written"},
        507: {"description": "The station's contributor upload storage is full"},
    },
)
def upload_contributor_media(
    request: Request,
    file: UploadFile = File(...),
) -> SubmissionMediaReference:
    _enforce_upload_rate_limit(request)
    filename = _safe_filename(file.filename or "submission-media")
    upload_dir = default_contributor_upload_dir()
    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # A misconfigured / unwritable upload directory must return the same
        # actionable 503 the streaming path already gives, not a bare 500.
        # The raw OSError text is logged only -- it names the server's
        # absolute storage path (same class of leak as the upload_ref
        # finding this router was fixed for) and must never reach an
        # anonymous public caller in the response body.
        _LOG.error("contributor upload directory is not writable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Contributor upload storage is not ready. Please contact the station.",
        ) from exc
    # QA-2 part 3: a per-IP cumulative budget so one un-rotated address cannot
    # alone consume the whole directory budget and lock out every contributor.
    from civiccast.common.trusted_proxy import resolve_client_ip

    byte_budget = _get_upload_byte_budget(request)
    ip_key = resolve_client_ip(request)
    per_ip_ceiling = _per_ip_upload_max_bytes()
    dir_ceiling = _upload_dir_max_bytes()
    max_bytes = _max_upload_bytes()
    # Aggregate bound (rc18 QA-2 + re-gate PE-2/TE-1): reserve this upload's
    # worst case against the directory ceiling atomically, BEFORE a byte is
    # written. The old check was a plain unlocked disk scan, so concurrent
    # uploads each passed it before any of them wrote and the directory
    # overshot. reserve_dir_slot takes the reservation under the budget's lock;
    # a station at capacity refuses here instead of growing further.
    if not byte_budget.reserve_dir_slot(upload_dir, dir_ceiling, max_bytes):
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail=(
                "This station's contributor upload storage is full, so new "
                "submissions cannot be accepted right now. Nothing you sent was "
                "lost or stored. Please contact the station; an operator needs "
                "to review and clear pending contributor uploads."
            ),
        )
    # The directory slot is now HELD. Everything from here until it is released
    # must run inside the try/finally -- including _unique_upload_path, which
    # touches the filesystem and can raise -- or a failure before the try would
    # strand the reservation and permanently shrink the ceiling.
    digest = hashlib.sha256()
    size_bytes = 0
    reserved = 0
    committed = False
    try:
        destination = _unique_upload_path(upload_dir, filename)
        with destination.open("wb") as handle:
            while chunk := file.file.read(1024 * 1024):
                size_bytes += len(chunk)
                # Stop AT the ceiling, not after: checking only once the stream
                # ends would still let the whole file reach the disk first.
                if size_bytes > max_bytes:
                    handle.close()
                    destination.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail=(
                            "Contributor media exceeds the station's upload limit of "
                            f"{_format_bytes(max_bytes)}. Ask the station "
                            "for a direct hand-off if the programme is larger."
                        ),
                    )
                # Reserve THIS chunk against the LIVE per-IP total before writing
                # it. Reserving per chunk under the budget's lock is what makes
                # concurrent uploads from one address see each other -- a single
                # snapshot taken before the loop let N parallel uploads each pass.
                if not byte_budget.try_reserve(ip_key, len(chunk), per_ip_ceiling):
                    handle.close()
                    destination.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail=(
                            "This address has already used its contributor upload "
                            "budget for this station. Nothing you sent was lost or "
                            "stored. Please contact the station if you need to send "
                            "more programming."
                        ),
                    )
                reserved += len(chunk)
                digest.update(chunk)
                handle.write(chunk)
        if size_bytes == 0:
            destination.unlink(missing_ok=True)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Contributor media upload cannot be empty.",
            )
        committed = True
        # Record which path holds this IP's bytes so the reaper can return the
        # per-IP budget when it later deletes the file (re-gate TE-3). Best-effort:
        # the upload is already committed to disk, so a failure to resolve the path
        # for bookkeeping must NOT turn a successful upload into a 503 (the enclosing
        # `except OSError` would otherwise catch destination.resolve() and misreport
        # the committed upload as failed). Worst case the reaper cannot credit this
        # one file's bytes back until restart -- an acceptable, restart-bounded cost.
        with contextlib.suppress(OSError):
            byte_budget.record_committed(str(destination.resolve()), ip_key, size_bytes)
    except OSError as exc:
        # Same rationale as the mkdir failure above: log the real (possibly
        # path-bearing) error server-side, return a safe generic detail.
        _LOG.error("could not store contributor media: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not store contributor media due to a server storage error. "
            "Nothing you sent was kept; please try again.",
        ) from exc
    finally:
        # Release the reservations BEFORE closing the source handle. An OSError
        # from file.file.close() -- a real disk-pressure failure, exactly what
        # this ceiling guards against -- must not skip the releases and strand
        # the directory slot forever (each such leak shrinks the effective
        # ceiling until restart). On commit the bytes are now on disk (the next
        # reserve_dir_slot scan counts them, so keeping the reservation too would
        # double-count); on any failure the partial file was already deleted.
        byte_budget.release_dir_slot(max_bytes)
        # A path that did not commit (413, 429, 422-empty, OSError) deleted its
        # partial file above; return the per-IP bytes it reserved so a failed
        # attempt does not permanently consume the address's budget.
        if not committed:
            byte_budget.release(ip_key, reserved)
        # Closing the upload's temp handle is best-effort: the data is already
        # read, and its failure must not undo the releases above.
        with contextlib.suppress(OSError):
            file.file.close()
    # Field evidence / GauntletGate finding: this used to be `str(destination)`
    # -- the absolute server filesystem path -- returned to an anonymous
    # public caller (reveals the service account's profile layout, e.g.
    # C:\Windows\System32\config\systemprofile\...). `destination.name` is
    # the opaque, unguessable filename `_unique_upload_path` generated (a
    # uuid4 token, not the contributor's own filename), with no directory
    # component. `store.resolve_contributor_upload_path` is the one place
    # that ever turns it back into a real path, joined onto the contributor
    # upload directory the server already knows -- see its docstring.
    return SubmissionMediaReference(
        upload_ref=destination.name,
        filename=filename,
        content_type=file.content_type or "application/octet-stream",
        size_bytes=size_bytes,
        sha256=digest.hexdigest(),
    )


@public_router.post(
    "/submissions",
    response_model=ContributorSubmissionReceipt,
    status_code=status.HTTP_201_CREATED,
    summary="Create an external producer submission for operator review",
    responses={
        409: {"description": "The referenced upload is already attached to another submission"},
        429: {"description": "Rate limit exceeded"},
    },
)
def create_contributor_submission(
    payload: ContributorSubmissionCreate,
    request: Request,
    store: ContributorSubmissionStore = Depends(get_contributor_store),
) -> ContributorSubmissionReceipt:
    _enforce_submission_rate_limit(request)
    try:
        return store.create_submission(payload)
    except ContributorStoreError as exc:
        # Same rationale as get_contributor_store above: never echo the raw
        # (possibly path-bearing) persistence error to the anonymous caller.
        _LOG.error("could not persist contributor submission: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not save your submission due to a server storage error. "
            "Please try again.",
        ) from exc
    except ContributorUploadAlreadyUsedError as exc:
        # PE-1: the body is well-formed; the referenced upload is already spent,
        # which is a state conflict (409), not a malformed request (422).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        # QA-3: store.create_submission now rejects a media reference whose
        # upload_ref/sha256 don't match a real file in the contributor
        # upload directory. Same mapping review_contributor_submission
        # already uses for its ValueError below.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@public_router.get(
    "/submissions/{submission_id}/status",
    response_model=PublicSubmissionStatus,
    summary="Read contributor-safe submission status",
    responses={
        403: {"description": "Receipt token does not match this submission"},
        404: {"description": "Submission not found"},
        429: {"description": "Rate limit exceeded"},
    },
)
def read_public_submission_status(
    submission_id: str,
    request: Request,
    receipt_token: str = Query(min_length=24, max_length=160),
    store: ContributorSubmissionStore = Depends(get_contributor_store),
) -> PublicSubmissionStatus:
    _enforce_status_rate_limit(request)
    try:
        return store.public_status(submission_id, receipt_token)
    except ContributorReceiptTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Receipt token does not match this submission.",
        ) from exc
    except ContributorSubmissionNotFoundError as exc:
        raise _submission_not_found(submission_id) from exc


@staff_router.get(
    "/submissions",
    response_model=ContributorReviewQueue,
    summary="List operator contributor submission review queue",
    dependencies=[
        Depends(require_any_role("publish_operator", "meeting_operator", "support_admin"))
    ],
)
def list_contributor_review_queue(
    store: ContributorSubmissionStore = Depends(get_contributor_store),
) -> ContributorReviewQueue:
    return store.list_queue()


@staff_router.get(
    "/submissions/{submission_id}",
    response_model=ContributorSubmission,
    summary="Read one contributor submission with operator review state",
    responses={404: {"description": "Submission not found"}},
    dependencies=[
        Depends(require_any_role("publish_operator", "meeting_operator", "support_admin"))
    ],
)
def read_contributor_submission(
    submission_id: str,
    store: ContributorSubmissionStore = Depends(get_contributor_store),
) -> ContributorSubmission:
    try:
        return store.get_submission(submission_id)
    except ContributorSubmissionNotFoundError as exc:
        raise _submission_not_found(submission_id) from exc


_ASSET_ID_INVALID_CHARS = re.compile(r"[^a-z0-9-]")


def _derive_asset_id(submission_id: str) -> str:
    """Deterministic-ish library asset_id for an accepted contributor submission.

    Mirrors ``civiccast.schedule.watch_folder_worker.WatchFolderWorker.
    _derive_asset_id`` -- an id-derived slug plus a random suffix, matching
    the asset_id regex (``^[a-z0-9][a-z0-9-]{2,63}$``) -- rather than a
    collision-checked counter, so acceptance never needs an extra
    round-trip to the asset store just to pick a free id.
    """
    base = _ASSET_ID_INVALID_CHARS.sub("-", submission_id.lower()).strip("-")
    base = (base or "contributor-submission")[:40]
    if not base[0].isalnum():
        base = f"a{base}"
    return f"contributor-{base}-{uuid.uuid4().hex[:8]}"[:64].rstrip("-")


def _ingest_accepted_media(
    submission: ContributorSubmission,
    request: ContributorReviewRequest,
    *,
    asset_store: Any,
) -> tuple[BrokenMediaGateResult, str]:
    """Run the real broken-media probe and ingest an accepted submission's file.

    TASK A (field evidence candidate #17): "accepted" used to be a bare
    state flip -- the file stayed in ``contributor-uploads`` and was never
    ingested, so nothing existed for an operator to trim/package/publish.
    This runs the EXACT ffprobe -> validate -> hash -> thumbnail ->
    ``AssetStore.ingest_upload`` sequence
    ``civiccast.schedule.router.upload_asset`` (staff upload) and
    ``civiccast.schedule.watch_folder_worker.WatchFolderWorker.
    _run_ingest_pipeline`` (watch folder) both already use -- never a
    parallel pipeline -- so an accepted submission becomes a real,
    trimmable, packageable library asset.

    TASK C: by default this is a REAL automated probe, not an attestation.
    ffprobe genuinely reads the file and ``validate_ingest`` genuinely
    rejects an unsupported or corrupt one (raised as HTTP 422 before any
    state change -- a corrupt file cannot be accepted). The one exception is
    an explicit operator OVERRIDE
    (``broken_media_gate.state == "override_accepted"``): the file is still
    ingested (a schedulable asset always needs one), but the pass/fail
    verdict is the operator's own attestation -- honestly labeled as such by
    the pre-existing ``override_accepted`` state name, never silently
    replaced with a fabricated "passed".

    Returns the real gate result to persist and the new asset's id.
    """
    # media.upload_ref is the opaque token issued at upload time (or, for a
    # submission recorded before this fix, a legacy absolute path) -- always
    # resolve it through the shared helper rather than treating it as a
    # path on its own. This submission was already validated by
    # store._verified_media_reference at creation time, so no additional
    # containment check is needed here.
    source_path = resolve_contributor_upload_path(submission.media.upload_ref)
    if not source_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "The contributor's uploaded file is no longer on disk and cannot be "
                "accepted. Ask the contributor to resubmit."
            ),
        )

    upload_dir = resolve_upload_root()
    if upload_dir is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Asset upload storage is not configured. Set CIVICCAST_UPLOAD_DIR.",
        )

    override = (
        request.broken_media_gate is not None
        and request.broken_media_gate.state == "override_accepted"
    )

    try:
        ffprobe_result = run_ffprobe(source_path)
    except FfprobeNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except FfprobeError as exc:
        if not override:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "The broken-media probe could not read this file -- it is likely "
                    f"corrupt or truncated: {exc}"
                ),
            ) from exc
        # Explicit operator override past a probe failure: still ingest with
        # whatever ffprobe could not tell us left blank, same as any other
        # optional ffprobe-derived field elsewhere in this codebase.
        ffprobe_result = FfprobeResult(
            duration_seconds=None,
            codec_video=None,
            codec_audio=None,
            width_px=None,
            height_px=None,
            bitrate_bps=None,
            format_name=None,
        )

    if override:
        gate = request.broken_media_gate
        assert gate is not None  # `override` is only True when this is set
    else:
        try:
            validate_ingest(ffprobe_result)
        except UnsupportedFormatError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.reason
            ) from exc
        gate = BrokenMediaGateResult(
            state="passed",
            checked_at=utc_now(),
            summary=(
                f"Automated ffprobe probe passed: {ffprobe_result.codec_video} video, "
                f"{ffprobe_result.codec_audio or 'no'} audio, "
                f"{ffprobe_result.duration_seconds or 0}s duration, "
                f"container {ffprobe_result.format_name or 'unknown'}."
            ),
        )

    asset_id = _derive_asset_id(submission.submission_id)
    get_staff_row = getattr(asset_store, "get_staff_row", None)
    if callable(get_staff_row):
        for _ in range(5):
            if get_staff_row(asset_id) is None:
                break
            asset_id = _derive_asset_id(submission.submission_id)
        else:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Could not allocate a free library asset id. Try accepting again.",
            )

    asset_dir = (upload_dir / asset_id).resolve()
    if not asset_dir.is_relative_to(upload_dir):
        raise HTTPException(  # pragma: no cover - asset_id is regex-derived, defense-in-depth
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Derived asset id resolves outside the upload directory.",
        )
    asset_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", submission.media.filename) or "contributor-media"
    dest_path = (asset_dir / safe_name).resolve()
    if not dest_path.is_relative_to(asset_dir):  # pragma: no cover - defense-in-depth
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Invalid derived filename."
        )

    # Copy, never move: the contributor's original stays in contributor-uploads
    # (the reaper already treats it as referenced -- see
    # ContributorSubmissionStore.referenced_upload_paths -- for as long as
    # this submission exists in any state) exactly like the watch-folder
    # ingest never deletes its source (ADR 0024, delete-safety posture).
    try:
        shutil.copy2(source_path, dest_path)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not copy contributor media into the asset library: {exc}",
        ) from exc

    source_size = source_path.stat().st_size
    dest_size = dest_path.stat().st_size
    if source_size != dest_size:
        dest_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Copying the contributor's file into the asset library was "
                "incomplete. Try accepting again."
            ),
        )

    try:
        content_hash: str | None = hash_file(dest_path)
    except OSError:
        content_hash = None

    thumbnail_target = asset_dir / "thumbnail.jpg"
    thumbnail_path: Path | None
    try:
        extract_thumbnail(dest_path, thumbnail_target)
        thumbnail_path = thumbnail_target
    except (FfmpegNotFoundError, FfprobeError, OSError):
        thumbnail_path = None

    try:
        response = asset_store.ingest_upload(
            asset_id=asset_id,
            title=submission.title,
            description=submission.description,
            file_path=str(dest_path),
            file_size_bytes=dest_size,
            ffprobe_result=ffprobe_result,
            content_hash=content_hash,
            thumbnail_path=str(thumbnail_path) if thumbnail_path is not None else None,
        )
    except AssetAlreadyExistsError as exc:
        dest_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A library asset already exists at {exc.asset_id!r}.",
        ) from exc

    return gate, response.asset_id


def _create_real_schedule_item(
    submission: ContributorSubmission,
    request: ContributorReviewRequest,
    *,
    schedule_store: Any,
) -> str:
    """Create a real ``civiccast.schedule_items`` row and return its id.

    TASK A: this is what makes ``schedule_item_id`` non-null on a success
    response. Calls the exact ``PostgresScheduleStore.create`` that
    ``POST /api/staff/schedule`` uses -- never a parallel scheduling path --
    so a schedule conflict or a since-deleted asset surfaces as the same
    real HTTP error that endpoint would give, instead of a silently-accepted
    phantom booking.
    """
    handoff = request.schedule_handoff
    assert handoff is not None  # enforced by ContributorReviewRequest's validator
    assert submission.asset_id is not None  # enforced by the router before calling this

    try:
        payload = ScheduleItemCreate(
            asset_id=submission.asset_id,
            channel_id=handoff.channel_id,
            mode=cast(ScheduleModeValue, SCHEDULE_MODE_PREMIERE),
            scheduled_at=handoff.requested_start,
            duration_seconds=handoff.duration_seconds,
            notes=handoff.notes,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Could not build a schedule item from this handoff: {exc}",
        ) from exc

    from civiccast.schedule.store import AssetNotFoundError, ScheduleConflictError

    try:
        response = schedule_store.create(payload)
    except AssetNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"The library asset for this submission ({submission.asset_id!r}) no "
                "longer exists, so it cannot be scheduled."
            ),
        ) from exc
    except ScheduleConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Schedule conflict on channel {handoff.channel_id!r}: {exc}",
        ) from exc

    return str(response.id)


def _cancel_schedule_item_for_decline(
    submission: ContributorSubmission,
    *,
    schedule_store: Any,
) -> None:
    """Pull a declined submission's real schedule item off the schedule.

    BLOCKER field evidence: declining an already-accepted-and-scheduled
    submission used to be a bare state flip in the JSON store -- the real
    ``civiccast.schedule_items`` row ``_create_real_schedule_item`` created
    earlier was never touched, so a "declined" recording still aired at its
    scheduled time. This is called from
    :func:`review_contributor_submission` BEFORE the decline is persisted
    (see there) -- pulling it off the schedule is the urgent safety action,
    so a decline is refused (rather than silently recorded) if the item
    cannot actually be pulled, instead of ever leaving a submission marked
    "declined" while it is still a live, airable booking.

    A no-op when the submission never reached a real schedule item (most
    declines: nothing to cancel). Cancelling an already-cancelled/already-
    published item is itself a no-op (`PostgresScheduleStore.cancel` /
    `_EphemeralScheduleStore.cancel`), so a decline that races a manual
    cancel is safe, and a decline retried after a transient failure is safe.
    """
    if submission.schedule_handoff is None:
        return
    schedule_item_id = submission.schedule_handoff.schedule_item_id
    if not schedule_item_id:
        return
    if schedule_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "This submission has a real schedule booking, and schedule storage "
                "is not ready to cancel it, so declining cannot be completed safely "
                "right now. " + _ASSET_STORE_NOT_READY_DETAIL
            ),
        )

    from civiccast.schedule.store import ScheduleItemNotFoundError

    try:
        parsed_id = uuid.UUID(schedule_item_id)
    except ValueError:
        # Not expected -- _create_real_schedule_item always persists a real
        # UUID string -- but never let an unparsable id silently let a live
        # booking through un-cancelled.
        _LOG.error(
            "submission %r has an unparsable schedule_item_id %r; refusing to "
            "decline without being able to cancel it",
            submission.submission_id,
            schedule_item_id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "This submission's schedule booking could not be identified, so "
                "declining cannot be completed safely right now. Contact support."
            ),
        ) from None

    # Already gone (manually cancelled, or deleted) -- nothing left to pull.
    with contextlib.suppress(ScheduleItemNotFoundError):
        schedule_store.cancel(parsed_id)


def _rollback_created_schedule_item(schedule_item_id: str, *, schedule_store: Any) -> None:
    """Cancel a schedule item this request just created after persisting failed.

    BLOCKER fix: ``_create_real_schedule_item`` creates a real
    ``civiccast.schedule_items`` row BEFORE ``store.review_submission``
    persists the "scheduled" state. Reserving every review action (see
    ``review_contributor_submission``) closes the interleaving that used to
    let a concurrent decline slip in ahead of that persist, but the persist
    call can still fail for other reasons (a store I/O error, a payload
    validation issue caught late) -- if it does after the row already
    exists, that row must not stay live and airable behind a submission
    that never actually reached "scheduled". Best-effort: an already-gone
    item is a no-op, matching ``_cancel_schedule_item_for_decline``.
    """
    from civiccast.schedule.store import ScheduleItemNotFoundError

    try:
        parsed_id = uuid.UUID(schedule_item_id)
    except ValueError:
        # Not expected -- _create_real_schedule_item always returns a real
        # UUID string -- but never crash the error path we are already on.
        _LOG.error(
            "could not parse schedule_item_id %r to roll back after a failed "
            "review persist; a live schedule item may be orphaned",
            schedule_item_id,
        )
        return
    with contextlib.suppress(ScheduleItemNotFoundError):
        schedule_store.cancel(parsed_id)


@staff_router.post(
    "/submissions/{submission_id}/review",
    response_model=ContributorSubmission,
    summary="Apply operator review decision to contributor submission",
    responses={
        404: {"description": "Submission not found"},
        409: {
            "description": (
                "Not yet ingested / not yet accepted / schedule conflict / asset id collision / "
                "another review action already in flight"
            )
        },
        422: {
            "description": (
                "Invalid review request, or the contributor's media failed the broken-media probe"
            )
        },
        503: {"description": "ffprobe or asset/schedule storage not ready"},
    },
    dependencies=[Depends(require_any_role("publish_operator", "meeting_operator"))],
)
def review_contributor_submission(
    submission_id: str,
    request: ContributorReviewRequest,
    store: ContributorSubmissionStore = Depends(get_contributor_store),
    asset_store: Any = Depends(get_postgres_store),
    schedule_store: Any = Depends(get_schedule_store),
) -> ContributorSubmission:
    effective_request = request
    ingested_asset_id: str | None = None
    created_schedule_item_id: str | None = None
    # CRITICAL race fix: EVERY review action below is reserved before it
    # runs -- not just accept/schedule. accept/schedule each trigger a REAL
    # external side effect (ffprobe ingest into the asset library / a real
    # civiccast.schedule_items row); a guard applied only when persisting to
    # the JSON store (after the side effect already ran) is too late, it
    # would have already created a second real asset or a second real, live
    # schedule booking. decline/mark_under_review/request_changes create no
    # side effect of their own, but BLOCKER field evidence showed a decline
    # left unreserved can still interleave with an in-flight schedule: the
    # schedule request creates its real schedule_items row, reads a
    # not-yet-declined `current`, then loses the persist race to a decline
    # that ran (and read stale state) in between -- leaving that real row
    # live while the submission is marked declined. Reserving every action
    # under the same in-flight ledger closes that off: whichever action
    # claims the reservation first runs to completion (or fails and rolls
    # back, see the persist `except` blocks below) before any other action
    # for this submission can even start. reserve_review_action claims the
    # submission atomically (existence + no other action already in flight +
    # the submission's current state actually allows this action) before any
    # side effect runs; release_reservation in the `finally` below always
    # releases it, on every exit path.
    reserved = False
    try:
        try:
            current = store.reserve_review_action(submission_id, request.action)
        except ContributorSubmissionNotFoundError as exc:
            raise _submission_not_found(submission_id) from exc
        except ContributorReviewConflictError as exc:
            # TASK B: refuse rather than report success -- this is exactly
            # the operator action that field evidence candidate #17 found
            # missing from both the response and the resident-facing
            # message. Also covers CRITICAL: an already-scheduled/published
            # submission, or a concurrent/duplicate/interleaved review
            # request of ANY kind, is refused here too.
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        reserved = True

        if request.action == "accept":
            if asset_store is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=_ASSET_STORE_NOT_READY_DETAIL,
                )
            gate, ingested_asset_id = _ingest_accepted_media(
                current, request, asset_store=asset_store
            )
            effective_request = request.model_copy(update={"broken_media_gate": gate})
        elif request.action == "schedule":
            if schedule_store is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=_ASSET_STORE_NOT_READY_DETAIL,
                )
            created_schedule_item_id = _create_real_schedule_item(
                current, request, schedule_store=schedule_store
            )
        elif request.action == "decline":
            # BLOCKER fix: pull any real schedule item off the schedule
            # BEFORE the decline is persisted -- see
            # _cancel_schedule_item_for_decline's docstring for why this
            # ordering matters. `current` is the fresh, reservation-locked
            # read above, not a pre-reservation snapshot, so this can no
            # longer race a concurrent schedule request past this point.
            _cancel_schedule_item_for_decline(current, schedule_store=schedule_store)

        try:
            return store.review_submission(
                submission_id,
                effective_request,
                ingested_asset_id=ingested_asset_id,
                created_schedule_item_id=created_schedule_item_id,
            )
        except ContributorSubmissionNotFoundError as exc:
            raise _submission_not_found(submission_id) from exc
        except ContributorReviewConflictError as exc:
            # BLOCKER fix: reserve_review_action already re-validates state
            # atomically at reservation time, so this should be unreachable
            # in the normal case -- but if persisting still fails here for
            # any reason, a schedule action that already created a real
            # schedule_items row must not leave it dangling behind a
            # declined-elsewhere or otherwise-conflicting submission.
            if created_schedule_item_id is not None:
                _rollback_created_schedule_item(
                    created_schedule_item_id, schedule_store=schedule_store
                )
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ContributorStoreError as exc:
            if created_schedule_item_id is not None:
                _rollback_created_schedule_item(
                    created_schedule_item_id, schedule_store=schedule_store
                )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        except ValueError as exc:
            if created_schedule_item_id is not None:
                _rollback_created_schedule_item(
                    created_schedule_item_id, schedule_store=schedule_store
                )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
    finally:
        if reserved:
            store.release_reservation(submission_id)


@staff_router.get(
    "/reports/producers",
    response_model=ProducerActivityReport,
    summary="Read producer activity report for station reporting",
    dependencies=[
        Depends(require_any_role("publish_operator", "meeting_operator", "support_admin"))
    ],
)
def read_producer_activity_report(
    store: ContributorSubmissionStore = Depends(get_contributor_store),
) -> ProducerActivityReport:
    return store.producer_report()


@staff_router.get(
    "/notifications/outbox",
    response_model=ContributorNotificationOutbox,
    summary="Read queued contributor status notifications",
    dependencies=[
        Depends(require_any_role("publish_operator", "meeting_operator", "support_admin"))
    ],
)
def read_contributor_notification_outbox(
    store: ContributorSubmissionStore = Depends(get_contributor_store),
) -> ContributorNotificationOutbox:
    return store.notification_outbox()


def _submission_not_found(submission_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Contributor submission {submission_id!r} not found.",
    )


def _format_bytes(num_bytes: int) -> str:
    """Human-readable size for a contributor-facing message (65536 -> '64 KB').

    UX-REGATE-2: the oversized-upload 413 is read by a public community producer,
    not an operator, so it must name a size a person can act on rather than a raw
    ten-digit byte count.
    """
    if num_bytes < 1024:
        return f"{num_bytes} bytes"
    value = float(num_bytes)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024.0
        if value < 1024.0 or unit == "TB":
            rendered = f"{value:.1f}".rstrip("0").rstrip(".")
            return f"{rendered} {unit}"
    return f"{num_bytes} bytes"  # pragma: no cover - the TB branch already returns


def _safe_filename(filename: str) -> str:
    cleaned = "".join(
        char if char.isalnum() or char in {"-", "_", "."} else "-" for char in filename
    ).strip(".-")
    return cleaned[:120] or "submission-media"


def _unique_upload_path(upload_dir: Path, filename: str) -> Path:
    """Pick a fresh on-disk destination for an uploaded file.

    Field evidence / GauntletGate finding: the returned path's *filename*
    doubles as the public ``upload_ref`` handed back to the caller (see
    ``upload_contributor_media`` below), so it must be unguessable, not
    merely collision-free. The old behaviour named the file after the
    contributor's own sanitized filename plus a numeric collision counter
    ("council-speech.mp4", "council-speech-2.mp4") -- predictable for
    anything with an obvious name, which matters here because
    ``_verified_media_reference`` treats "a file that exists inside the
    contributor upload directory" as sufficient proof of a real upload:
    ``sha256`` is only checked when the client bothers to send one. A
    second anonymous caller who guessed the filename could attach a
    stranger's pending upload to their own submission with no proof of
    anything. A random token closes that off. The contributor's original
    filename is preserved separately in ``SubmissionMediaReference.filename``
    for display; only the extension is kept in the stored name.
    """
    suffix = Path(filename).suffix
    candidate = upload_dir / f"{uuid.uuid4().hex}{suffix}"
    while candidate.exists():  # pragma: no cover - a uuid4 collision is not reachable in tests
        candidate = upload_dir / f"{uuid.uuid4().hex}{suffix}"
    return candidate
