# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Worker loop for ending-session recording finalization and VOD packaging.

Settle rule:
    For file recording targets, the worker resolves a session recording as
    ``<target_uri>/<live_session_id>.mp4``. A file is considered settled only
    after it exists and its byte size is unchanged across two worker scans at
    least ``settle_seconds`` apart. Until then the job remains ``pending``.

Retry rule:
    A failed finalize/package attempt is retried after exponential backoff until
    ``max_attempts`` is reached. The terminal state remains ``failed`` with the
    latest failure reason persisted for operators.
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from civiccast.live.cdn_targets import live_package_cdn_prefix
from civiccast.live.finalization import FinalizationResult, LiveRecordingFinalizer
from civiccast.live.models import (
    FAILURE_CODE_CDN_UPLOAD_FAILED,
    FAILURE_CODE_INTERNAL_ERROR,
    FAILURE_CODE_INVALID_TRIM,
    FAILURE_CODE_PACKAGE_FAILED,
    FAILURE_CODE_PROBE_FAILED,
    FAILURE_CODE_RECORDING_NEVER_APPEARED,
    FAILURE_CODE_RECORDING_NOT_LOCAL,
    FAILURE_CODE_WORKER_INTERRUPTED,
    FINALIZATION_STATE_COMPLETED,
    FINALIZATION_STATE_FAILED,
    FINALIZATION_STATE_PENDING,
    FINALIZATION_STATE_RUNNING,
    LIVE_SESSION_STATE_ENDING,
    LiveFinalizationJob,
    LiveFinalizationStatusResponse,
    LiveSession,
    RecordingTarget,
)
from civiccast.live.recording_paths import (
    REHEARSAL_RECORDING_TARGET_ID,
    local_recording_path,
)
from civiccast.schedule.ingest import FfprobeResult, run_ffprobe
from civiccast.schedule.models import Asset
from civiccast.stream.cdn import CDNAdapter
from civiccast.stream.cdn.package_upload import upload_package_files
from civiccast.stream.packager import VodPackageResult, pack_vod_asset

SessionFactory = Callable[[], AbstractContextManager[Session]]

_LOG = logging.getLogger(__name__)

WORKER_MODE_INLINE = "inline"
WORKER_MODE_EXTERNAL = "external"
WORKER_MODE_OFF = "off"
_WORKER_MODES = (WORKER_MODE_INLINE, WORKER_MODE_EXTERNAL, WORKER_MODE_OFF)

# VOD local-serve default (no external CDN, no manual config): the app's own
# ``civiccast.stream.media_router`` mount, spelled SITE-RELATIVE
# (``/media/vod/{asset}/playlist.m3u8``, the same shape the staff package
# route stores and ``vod.models._validate_manifest_reference`` accepts). The
# resident portal is served from the same origin as the media router, so a
# relative URL resolves to whatever host the resident actually reached. The
# old default was the absolute ``http://127.0.0.1:8000`` -- a LAN resident
# clicking a recording was sent to their own loopback (review round 2 delta,
# MINOR 1; the identical live-manifest defect was BLOCKER 1 of round 2). An
# operator whose media origin differs from the portal's sets
# CIVICCAST_LOCAL_MEDIA_BASE_URL, which then means the same thing for VOD as
# it does for live (``civiccast.cable.channel.local_live_manifest_path``).
DEFAULT_LOCAL_MEDIA_BASE_URL = ""


@dataclass(frozen=True)
class FinalizationWorkerSettings:
    """Deployment configuration for the finalization worker.

    Loaded from the environment by :meth:`from_env`. ``mode`` selects the
    hybrid architecture's deployment shape:

    - ``inline`` (default): the app lifespan runs the worker loop in a
      background thread whenever durable storage is active.
    - ``external``: the app never starts the loop; an operator runs
      ``python -m civiccast.live.finalization_worker`` as a separate process
      (see ``docs/ops/finalization-worker-runbook.md``).
    - ``off``: the loop never runs anywhere (status endpoints stay readable).
    """

    mode: str = WORKER_MODE_INLINE
    public_manifest_base_url: str | None = None
    local_media_base_url: str = DEFAULT_LOCAL_MEDIA_BASE_URL
    settle_seconds: float = 30.0
    max_attempts: int = 3
    backoff_seconds: float = 30.0
    poll_seconds: float = 5.0
    running_lease_seconds: float = 900.0
    never_appeared_seconds: float = 1800.0

    @classmethod
    def from_env(cls) -> FinalizationWorkerSettings:
        mode = os.environ.get("CIVICCAST_FINALIZATION_WORKER", WORKER_MODE_INLINE).strip().lower()
        if mode not in _WORKER_MODES:
            raise ValueError(
                f"CIVICCAST_FINALIZATION_WORKER must be one of {', '.join(_WORKER_MODES)}; "
                f"got {mode!r}."
            )
        base_url = os.environ.get("CIVICCAST_LIVE_MANIFEST_BASE_URL", "").strip() or None
        local_media_base_url = (
            os.environ.get("CIVICCAST_LOCAL_MEDIA_BASE_URL", "").strip()
            or DEFAULT_LOCAL_MEDIA_BASE_URL
        )
        defaults = cls()
        return cls(
            mode=mode,
            public_manifest_base_url=base_url,
            local_media_base_url=local_media_base_url,
            settle_seconds=_env_float(
                "CIVICCAST_FINALIZATION_SETTLE_SECONDS", defaults.settle_seconds
            ),
            max_attempts=_env_int("CIVICCAST_FINALIZATION_MAX_ATTEMPTS", defaults.max_attempts),
            backoff_seconds=_env_float(
                "CIVICCAST_FINALIZATION_BACKOFF_SECONDS", defaults.backoff_seconds
            ),
            poll_seconds=_env_float("CIVICCAST_FINALIZATION_POLL_SECONDS", defaults.poll_seconds),
            running_lease_seconds=_env_float(
                "CIVICCAST_FINALIZATION_RUNNING_LEASE_SECONDS", defaults.running_lease_seconds
            ),
            never_appeared_seconds=_env_float(
                "CIVICCAST_FINALIZATION_NEVER_APPEARED_SECONDS", defaults.never_appeared_seconds
            ),
        )


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number; got {raw!r}.") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer; got {raw!r}.") from exc


def build_worker(
    session_factory: SessionFactory,
    settings: FinalizationWorkerSettings,
    *,
    cdn_adapter: CDNAdapter | None = None,
) -> LiveFinalizationWorker:
    """Construct a worker from deployment settings (single source of truth).

    ``cdn_adapter`` is the Stage C factory's selection
    (``CIVICCAST_CDN_PROVIDER``); when present, completed packages publish
    through it (Beta B4, decision #7A).
    """

    return LiveFinalizationWorker(
        session_factory,
        settle_seconds=settings.settle_seconds,
        max_attempts=settings.max_attempts,
        backoff_seconds=settings.backoff_seconds,
        public_manifest_base_url=settings.public_manifest_base_url,
        local_media_base_url=settings.local_media_base_url,
        running_lease_seconds=settings.running_lease_seconds,
        never_appeared_seconds=settings.never_appeared_seconds,
        cdn_adapter=cdn_adapter,
    )


class RecordingPackager(Protocol):
    def __call__(
        self,
        input_path: Path,
        output_dir: Path,
        *,
        trim_in_seconds: float | None = None,
        trim_out_seconds: float | None = None,
    ) -> VodPackageResult: ...


class RecordingProbe(Protocol):
    def __call__(self, path: Path) -> FfprobeResult: ...


_PACKAGE_LOCKS_GUARD = threading.Lock()
_PACKAGE_LOCKS: dict[str, threading.Lock] = {}

# Resolution rules live in civiccast.live.recording_paths so the store's
# go-on-air stamping (provenance, Beta sprint B1) and this worker can never
# drift apart. Aliased under the old private names for existing imports.
_REHEARSAL_RECORDING_TARGET_ID = REHEARSAL_RECORDING_TARGET_ID
_local_recording_path = local_recording_path


class _ClassifiedFailureError(Exception):
    """A finalization failure with a stable code and operator-facing copy.

    ``code`` is one of :data:`civiccast.live.models.FINALIZATION_FAILURE_CODES`;
    ``operator_message`` is rendered verbatim to operators (``failure_reason``);
    ``detail`` carries raw diagnostics (``failure_detail``).
    """

    def __init__(self, code: str, operator_message: str, detail: str | None = None) -> None:
        super().__init__(operator_message)
        self.code = code
        self.operator_message = operator_message
        self.detail = detail


class FinalizationRetryConflictError(Exception):
    """Raised when a finalization job cannot be retried in its current state."""

    def __init__(self, live_session_id: str, state: str) -> None:
        self.live_session_id = live_session_id
        self.state = state
        super().__init__(
            f"Finalization for {live_session_id!r} is {state!r} and cannot be retried."
        )


class LiveFinalizationWorker:
    """Synchronous worker service with testable ``run_once`` and loop entrypoint."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        finalizer: LiveRecordingFinalizer | None = None,
        packager: RecordingPackager = pack_vod_asset,
        probe: RecordingProbe = run_ffprobe,
        settle_seconds: float = 30.0,
        max_attempts: int = 3,
        backoff_seconds: float = 30.0,
        public_manifest_base_url: str | None = None,
        local_media_base_url: str = DEFAULT_LOCAL_MEDIA_BASE_URL,
        running_lease_seconds: float = 900.0,
        never_appeared_seconds: float = 1800.0,
        cdn_adapter: CDNAdapter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._finalizer = finalizer or LiveRecordingFinalizer(session_factory)
        self._packager = packager
        self._probe = probe
        self._settle_seconds = settle_seconds
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._public_manifest_base_url = (
            public_manifest_base_url.rstrip("/") if public_manifest_base_url else None
        )
        self._local_media_base_url = local_media_base_url.rstrip("/")
        self._running_lease_seconds = running_lease_seconds
        self._never_appeared_seconds = never_appeared_seconds
        self._cdn_adapter = cdn_adapter

    def run_forever(
        self,
        *,
        poll_seconds: float = 5.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Run the worker loop until ``stop_event`` is set.

        A scan exception is logged and the loop continues (ENG-009/W-7): a
        transient DB or filesystem error must not silently kill finalization
        for the life of the process.
        """
        while stop_event is None or not stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                _LOG.exception("Finalization scan failed; retrying on the next poll interval.")
            if stop_event is not None:
                # Waiting on the event (instead of a bare sleep) lets shutdown
                # interrupt the poll interval immediately (ENG-014).
                stop_event.wait(poll_seconds)
            else:
                time.sleep(poll_seconds)

    def run_once(self, *, now: datetime | None = None) -> list[LiveFinalizationStatusResponse]:
        """Scan ending sessions once and attempt any settled due jobs."""
        resolved_now = now or datetime.now(UTC)
        self._requeue_trim_drift(now=resolved_now)
        statuses: list[LiveFinalizationStatusResponse] = []
        for live_session_id in self._candidate_session_ids():
            job = self._ensure_or_observe_job(live_session_id, now=resolved_now)
            recovered = self._recover_stale_running(job, now=resolved_now)
            if recovered is not None:
                statuses.append(recovered)
                continue
            expired = self._fail_never_appeared(job, now=resolved_now)
            if expired is not None:
                statuses.append(expired)
                continue
            if self._job_due(job, now=resolved_now) and self._job_settled(job, now=resolved_now):
                job = self._attempt_job(job.live_session_id, now=resolved_now)
            statuses.append(job)
        return statuses

    def _requeue_trim_drift(self, *, now: datetime) -> None:
        """Repackage-on-trim-update (Beta B3, decision #4).

        A completed job whose asset trim no longer matches the trim its
        package was rendered with re-enters the queue as ``pending`` with a
        fresh attempt budget. The normal attempt path then re-runs: the
        finalizer is idempotent (returns the existing asset, with the NEW
        trim), and packaging is trim-aware, so the package is re-rendered —
        with the same retries/backoff/failure codes as first-time packaging.
        """

        with self._session_factory() as session:
            drifted = session.execute(
                select(LiveFinalizationJob)
                .join(Asset, Asset.asset_id == LiveFinalizationJob.asset_id)
                .where(
                    LiveFinalizationJob.state == FINALIZATION_STATE_COMPLETED,
                    or_(
                        Asset.trim_in_seconds.is_distinct_from(
                            LiveFinalizationJob.packaged_trim_in_seconds
                        ),
                        Asset.trim_out_seconds.is_distinct_from(
                            LiveFinalizationJob.packaged_trim_out_seconds
                        ),
                    ),
                )
            ).scalars()
            for row in drifted:
                row.state = FINALIZATION_STATE_PENDING
                row.attempts = 0
                row.next_attempt_at = None
                row.failure_reason = None
                row.failure_code = None
                row.failure_detail = None
                row.updated_at = now
                _LOG.info(
                    "Asset trim changed for session %s; package will be re-rendered.",
                    row.live_session_id,
                )
            session.commit()

    def _recover_stale_running(
        self,
        job: LiveFinalizationStatusResponse,
        *,
        now: datetime,
    ) -> LiveFinalizationStatusResponse | None:
        """Lease recovery (ENG-007/W-5): a crash mid-attempt leaves the row
        ``running`` forever; treat a ``running`` row whose ``started_at`` is
        older than the lease as a failed attempt and requeue it. The recovered
        job retries on a later scan, not within this one."""

        if job.state != FINALIZATION_STATE_RUNNING or job.started_at is None:
            return None
        age = (_as_utc_naive(now) - _as_utc_naive(job.started_at)).total_seconds()
        if age <= self._running_lease_seconds:
            return None
        _LOG.warning(
            "Recovering stale running finalization job %s: attempt started %.0fs ago "
            "(lease %.0fs); the worker process likely crashed mid-attempt.",
            job.live_session_id,
            age,
            self._running_lease_seconds,
        )
        return self._record_failure(
            job.live_session_id,
            failure=_ClassifiedFailureError(
                FAILURE_CODE_WORKER_INTERRUPTED,
                "Finalization was interrupted (the app or worker restarted "
                "mid-attempt). It will retry automatically.",
                detail=f"running since {job.started_at.isoformat()}; lease "
                f"{self._running_lease_seconds:.0f}s exceeded",
            ),
            now=now,
        )

    def _fail_never_appeared(
        self,
        job: LiveFinalizationStatusResponse,
        *,
        now: datetime,
    ) -> LiveFinalizationStatusResponse | None:
        """Never-appeared deadline (ENG-008/W-6): a recording file that has
        never been observed within ``never_appeared_seconds`` of the session's
        ``ended_at`` fails terminally with an actionable reason instead of
        pending silently forever."""

        if job.state != FINALIZATION_STATE_PENDING or job.recording_size_bytes is not None:
            return None
        with self._session_factory() as session:
            ended_at = session.execute(
                select(LiveSession.ended_at).where(
                    LiveSession.live_session_id == job.live_session_id
                )
            ).scalar_one_or_none()
        if ended_at is None:
            return None
        waited = (_as_utc_naive(now) - _as_utc_naive(ended_at)).total_seconds()
        if waited <= self._never_appeared_seconds:
            return None
        expected = (
            _local_recording_path(job.recording_uri) if job.recording_uri is not None else None
        )
        if expected is None:
            expected = self._recording_path_for_session(job.live_session_id)
        location = (
            str(expected)
            if expected is not None
            else ("no resolvable local recording target is configured")
        )
        _LOG.warning(
            "Finalization job %s: no recording file appeared within %.0fs of "
            "end-broadcast (expected %s); failing terminally.",
            job.live_session_id,
            self._never_appeared_seconds,
            location,
        )
        with self._session_factory() as session:
            row = session.execute(
                select(LiveFinalizationJob).where(
                    LiveFinalizationJob.live_session_id == job.live_session_id
                )
            ).scalar_one()
            row.state = FINALIZATION_STATE_FAILED
            row.attempts = row.max_attempts
            row.failure_code = FAILURE_CODE_RECORDING_NEVER_APPEARED
            row.failure_reason = (
                f"No recording file was found for this session (expected "
                f"{location}). Check that the recorder wrote to the configured "
                f"recording target."
            )
            row.failure_detail = (
                f"never observed within {self._never_appeared_seconds:.0f}s of "
                f"ended_at={ended_at.isoformat()}"
            )
            row.next_attempt_at = None
            row.updated_at = now
            session.commit()
            session.refresh(row)
            return _job_to_status(row)

    def list_statuses(self) -> list[LiveFinalizationStatusResponse]:
        with self._session_factory() as session:
            rows = session.execute(
                select(LiveFinalizationJob).order_by(LiveFinalizationJob.created_at.asc())
            ).scalars()
            return [_job_to_status(row) for row in rows]

    def get_status(self, live_session_id: str) -> LiveFinalizationStatusResponse | None:
        with self._session_factory() as session:
            row = session.execute(
                select(LiveFinalizationJob).where(
                    LiveFinalizationJob.live_session_id == live_session_id
                )
            ).scalar_one_or_none()
            return _job_to_status(row) if row is not None else None

    def request_retry(
        self,
        live_session_id: str,
        *,
        now: datetime | None = None,
    ) -> LiveFinalizationStatusResponse | None:
        """Operator retry (Beta B2): re-queue a failed job for a fresh run.

        Returns None when no job exists; raises
        :class:`FinalizationRetryConflictError` for ``running``/``completed``
        jobs (an attempt is in flight / there is nothing to retry). A
        ``failed`` job — retrying or terminal — resets to a clean ``pending``
        with a full attempt budget; the worker's next scan re-attempts it
        through the normal machinery.
        """

        resolved_now = now or datetime.now(UTC)
        with self._session_factory() as session:
            row = session.execute(
                select(LiveFinalizationJob).where(
                    LiveFinalizationJob.live_session_id == live_session_id
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if row.state in (FINALIZATION_STATE_RUNNING, FINALIZATION_STATE_COMPLETED):
                raise FinalizationRetryConflictError(live_session_id, row.state)
            row.state = FINALIZATION_STATE_PENDING
            row.attempts = 0
            row.next_attempt_at = None
            row.failure_reason = None
            row.failure_code = None
            row.failure_detail = None
            row.updated_at = resolved_now
            session.commit()
            session.refresh(row)
            _LOG.info("Operator requested finalization retry for session %s.", live_session_id)
            return _job_to_status(row)

    def _candidate_session_ids(self) -> list[str]:
        with self._session_factory() as session:
            ending_ids = list(
                session.execute(
                    select(LiveSession.live_session_id)
                    .where(LiveSession.state == LIVE_SESSION_STATE_ENDING)
                    .order_by(LiveSession.created_at.asc())
                ).scalars()
            )
            retry_ids = list(
                session.execute(
                    select(LiveFinalizationJob.live_session_id)
                    .where(LiveFinalizationJob.state != FINALIZATION_STATE_COMPLETED)
                    # Terminal failures (attempts exhausted) leave the scan set
                    # entirely (ENG-011): rescanning them forever clobbers the
                    # "when did it fail" signal and grows the scan unboundedly.
                    .where(
                        or_(
                            LiveFinalizationJob.state != FINALIZATION_STATE_FAILED,
                            LiveFinalizationJob.attempts < LiveFinalizationJob.max_attempts,
                        )
                    )
                    .order_by(LiveFinalizationJob.created_at.asc())
                ).scalars()
            )
            terminal_ids = set(
                session.execute(
                    select(LiveFinalizationJob.live_session_id).where(
                        LiveFinalizationJob.state == FINALIZATION_STATE_FAILED,
                        LiveFinalizationJob.attempts >= LiveFinalizationJob.max_attempts,
                    )
                ).scalars()
            )
            return [
                session_id
                for session_id in dict.fromkeys([*ending_ids, *retry_ids])
                if session_id not in terminal_ids
            ]

    def _ensure_or_observe_job(
        self,
        live_session_id: str,
        *,
        now: datetime,
    ) -> LiveFinalizationStatusResponse:
        recording_path = self._recording_path_for_session(live_session_id)
        recording_uri = recording_path.as_uri() if recording_path is not None else None
        observed_size = (
            recording_path.stat().st_size
            if recording_path is not None and recording_path.exists()
            else None
        )
        with self._session_factory() as session:
            row = session.execute(
                select(LiveFinalizationJob).where(
                    LiveFinalizationJob.live_session_id == live_session_id
                )
            ).scalar_one_or_none()
            if row is None:
                row = LiveFinalizationJob(
                    live_session_id=live_session_id,
                    max_attempts=self._max_attempts,
                    recording_uri=recording_uri,
                    recording_size_bytes=observed_size,
                    last_observed_size_bytes=observed_size,
                    last_observed_at=now if observed_size is not None else None,
                    updated_at=now,
                )
                session.add(row)
            else:
                # Prefer the fresh resolution: a previously mis-resolved URI
                # must not stay sticky after the operator fixes their targets
                # (ENG-005).
                row.recording_uri = recording_uri or row.recording_uri
                if observed_size is not None:
                    if row.last_observed_size_bytes != observed_size:
                        row.last_observed_size_bytes = observed_size
                        row.last_observed_at = now
                    row.recording_size_bytes = observed_size
                row.updated_at = now
            session.commit()
            session.refresh(row)
            return _job_to_status(row)

    def _job_due(self, job: LiveFinalizationStatusResponse, *, now: datetime) -> bool:
        if job.state == FINALIZATION_STATE_COMPLETED or job.state == FINALIZATION_STATE_RUNNING:
            return False
        if job.attempts >= job.max_attempts:
            return False
        if job.next_attempt_at is None:
            return True
        return _as_utc_naive(job.next_attempt_at) <= _as_utc_naive(now)

    def _job_settled(self, job: LiveFinalizationStatusResponse, *, now: datetime) -> bool:
        if job.recording_uri is None or job.recording_size_bytes is None:
            return False
        path = _local_recording_path(job.recording_uri)
        if path is None or not path.exists():
            return False
        current_size = path.stat().st_size
        if current_size != job.recording_size_bytes:
            return False
        with self._session_factory() as session:
            row = session.execute(
                select(LiveFinalizationJob).where(
                    LiveFinalizationJob.live_session_id == job.live_session_id
                )
            ).scalar_one()
            if row.last_observed_at is None:
                return False
            observed_at = _as_utc_naive(row.last_observed_at)
        return (_as_utc_naive(now) - observed_at).total_seconds() >= self._settle_seconds

    def _attempt_job(
        self,
        live_session_id: str,
        *,
        now: datetime,
    ) -> LiveFinalizationStatusResponse:
        with self._session_factory() as session:
            row = session.execute(
                select(LiveFinalizationJob).where(
                    LiveFinalizationJob.live_session_id == live_session_id
                )
            ).scalar_one()
            row.state = FINALIZATION_STATE_RUNNING
            row.started_at = now
            row.updated_at = now
            session.commit()

        _LOG.info("Finalization attempt starting for session %s.", live_session_id)
        try:
            status = self.get_status(live_session_id)
            if status is None or status.recording_uri is None:
                raise _ClassifiedFailureError(
                    FAILURE_CODE_RECORDING_NOT_LOCAL,
                    "The recording location for this session could not be "
                    "resolved. Check the recording target configuration.",
                )
            recording_path = _local_recording_path(status.recording_uri)
            if recording_path is None:
                raise _ClassifiedFailureError(
                    FAILURE_CODE_RECORDING_NOT_LOCAL,
                    "The recording location for this session is not a local "
                    "file path the worker can read. Check the recording "
                    "target configuration.",
                    detail=f"recording_uri={status.recording_uri}",
                )
            try:
                probe = self._probe(recording_path)
            except Exception as exc:
                raise _ClassifiedFailureError(
                    FAILURE_CODE_PROBE_FAILED,
                    "The recording file could not be read (it may be "
                    "incomplete or corrupt). The file was kept; see server "
                    "logs for details.",
                    detail=str(exc),
                ) from exc
            try:
                finalized = self._finalizer.finalize_recording(
                    live_session_id,
                    recording_uri=status.recording_uri,
                    duration_seconds=probe.duration_seconds,
                    trim_in_seconds=status.trim_in_seconds,
                    trim_out_seconds=status.trim_out_seconds,
                    finalized_at=now,
                )
            except ValueError as exc:
                raise _ClassifiedFailureError(
                    FAILURE_CODE_INVALID_TRIM,
                    "The stored trim window is invalid (the trim start must "
                    "come before the trim end and lie inside the recording). "
                    "Fix the trim values; the original recording is safe.",
                    detail=str(exc),
                ) from exc
            try:
                package_path = self._package_once(
                    live_session_id=live_session_id,
                    recording_path=recording_path,
                    finalized=finalized,
                    packaged_trim=(
                        status.packaged_trim_in_seconds,
                        status.packaged_trim_out_seconds,
                    ),
                )
            except Exception as exc:
                raise _ClassifiedFailureError(
                    FAILURE_CODE_PACKAGE_FAILED,
                    "Packaging for playback failed. The original recording is "
                    "safe. Retries are automatic until attempts are exhausted.",
                    detail=str(exc),
                ) from exc
            manifest_url: str | None = self._servable_manifest_url(
                live_session_id, asset_id=finalized.asset.asset_id
            )
            if self._cdn_adapter is not None:
                try:
                    manifest_url = self._upload_package(live_session_id, package_path)
                except Exception as exc:
                    raise _ClassifiedFailureError(
                        FAILURE_CODE_CDN_UPLOAD_FAILED,
                        "Uploading the packaged recording to the CDN failed. "
                        "The local package is safe. Retries are automatic "
                        "until attempts are exhausted.",
                        detail=str(exc),
                    ) from exc
            with self._session_factory() as session:
                row = session.execute(
                    select(LiveFinalizationJob).where(
                        LiveFinalizationJob.live_session_id == live_session_id
                    )
                ).scalar_one()
                row.state = FINALIZATION_STATE_COMPLETED
                row.failure_reason = None
                row.failure_code = None
                row.failure_detail = None
                row.asset_id = finalized.asset.asset_id
                row.local_package_manifest_path = str(package_path)
                row.package_manifest_url = manifest_url
                row.trim_in_seconds = finalized.asset.trim_in_seconds
                row.trim_out_seconds = finalized.asset.trim_out_seconds
                # Bookkeeping for repackage-on-trim-update (Beta B3): record
                # what trim the package on disk now reflects.
                row.packaged_trim_in_seconds = finalized.asset.trim_in_seconds
                row.packaged_trim_out_seconds = finalized.asset.trim_out_seconds
                row.completed_at = now
                row.updated_at = now
                asset = session.execute(
                    select(Asset).where(Asset.asset_id == finalized.asset.asset_id)
                ).scalar_one()
                if manifest_url is not None:
                    asset.manifest_url = manifest_url
                session.commit()
                session.refresh(row)
                _LOG.info(
                    "Finalization completed for session %s (asset %s).",
                    live_session_id,
                    finalized.asset.asset_id,
                )
                return _job_to_status(row)
        except Exception as exc:
            return self._record_failure(live_session_id, failure=exc, now=now)

    def _package_once(
        self,
        *,
        live_session_id: str,
        recording_path: Path,
        finalized: FinalizationResult,
        packaged_trim: tuple[float | None, float | None] = (None, None),
    ) -> Path:
        lock = _package_lock_for(live_session_id)
        with lock:
            output_dir = recording_path.parent / f"{live_session_id}-hls"
            manifest_path = output_dir / "playlist.m3u8"
            requested_trim = (
                finalized.asset.trim_in_seconds,
                finalized.asset.trim_out_seconds,
            )
            # Trim-aware idempotency (Beta B3): skip only when the existing
            # package was rendered with the trim being requested. A bare
            # manifest-exists check made operator trims decorative (ENG-004).
            if manifest_path.exists() and packaged_trim == requested_trim:
                return manifest_path.resolve()
            package = self._packager(
                recording_path,
                output_dir,
                trim_in_seconds=finalized.asset.trim_in_seconds,
                trim_out_seconds=finalized.asset.trim_out_seconds,
            )
            return package.manifest_path.resolve()

    def _record_failure(
        self,
        live_session_id: str,
        *,
        failure: Exception,
        now: datetime,
    ) -> LiveFinalizationStatusResponse:
        if isinstance(failure, _ClassifiedFailureError):
            code = failure.code
            operator_message = failure.operator_message
            detail = failure.detail
        else:
            code = FAILURE_CODE_INTERNAL_ERROR
            operator_message = (
                "An unexpected error occurred during finalization. The "
                "original recording is safe; see server logs for details."
            )
            detail = str(failure)
        with self._session_factory() as session:
            row = session.execute(
                select(LiveFinalizationJob).where(
                    LiveFinalizationJob.live_session_id == live_session_id
                )
            ).scalar_one()
            row.attempts += 1
            row.state = FINALIZATION_STATE_FAILED
            row.failure_reason = operator_message
            row.failure_code = code
            row.failure_detail = detail
            if row.attempts < row.max_attempts:
                delay = self._backoff_seconds * (2 ** max(row.attempts - 1, 0))
                row.next_attempt_at = now + timedelta(seconds=delay)
            else:
                row.next_attempt_at = None
            row.updated_at = now
            session.commit()
            session.refresh(row)
            _LOG.warning(
                "Finalization attempt failed for session %s (%s, attempt %d/%d%s): %s",
                live_session_id,
                code,
                row.attempts,
                row.max_attempts,
                "" if row.next_attempt_at is not None else ", terminal",
                detail or operator_message,
            )
            return _job_to_status(row)

    def _recording_path_for_session(self, live_session_id: str) -> Path | None:
        """Resolve the expected recording file for a session.

        Provenance first (Beta sprint B1, decision #5): a session stamped at
        go-on-air with its recording target resolves there — no guessing.
        Sessions that predate the stamp fall back to the legacy scan: skip the
        installer's rehearsal target, prefer the first non-rehearsal target
        whose candidate file exists, else the first resolvable candidate (so
        never-appeared messaging can name the expected path).
        """

        with self._session_factory() as session:
            stamped_uri = session.execute(
                select(LiveSession.recording_target_uri).where(
                    LiveSession.live_session_id == live_session_id
                )
            ).scalar_one_or_none()
        if stamped_uri:
            base = local_recording_path(stamped_uri)
            if base is not None:
                return base / f"{live_session_id}.mp4"

        first_resolvable: Path | None = None
        with self._session_factory() as session:
            targets = session.execute(
                select(RecordingTarget).order_by(RecordingTarget.created_at.asc())
            ).scalars()
            for target in targets:
                if target.recording_target_id == REHEARSAL_RECORDING_TARGET_ID:
                    continue
                base = local_recording_path(target.target_uri)
                if base is None:
                    continue
                candidate = base / f"{live_session_id}.mp4"
                if candidate.exists():
                    return candidate
                if first_resolvable is None:
                    first_resolvable = candidate
        return first_resolvable

    def _upload_package(self, live_session_id: str, manifest_path: Path) -> str:
        """Publish the packaged HLS tree through the selected CDN adapter.

        Segments upload first and the manifest last, so a resident can never
        fetch a manifest whose segments are not on the CDN yet. Returns the
        CDN public URL of the manifest — the adapter only returns it after a
        successful upload, which is what keeps ``manifest_url`` honest.
        """
        assert self._cdn_adapter is not None  # guarded by the caller
        package_dir = manifest_path.parent
        return upload_package_files(
            self._cdn_adapter,
            package_dir=package_dir,
            prefix=live_package_cdn_prefix(live_session_id),
            files=(path for path in package_dir.rglob("*") if path.is_file()),
            manifest_path=manifest_path,
        )

    def _servable_manifest_url(self, live_session_id: str, *, asset_id: str) -> str:
        """URL the portal can fetch this asset's manifest from right now.

        Precedence: an operator-set ``CIVICCAST_LIVE_MANIFEST_BASE_URL`` wins
        (existing behavior, unchanged) — the CDN path in the caller
        overwrites this afterwards when a CDN adapter is configured. With
        neither set (the stock-install case this method exists for), this
        falls back to the app's own ``media_router`` mount so
        ``manifest_url`` is never left null / pointing nowhere.
        """
        if self._public_manifest_base_url is not None:
            return f"{self._public_manifest_base_url}/{quote(live_session_id)}/playlist.m3u8"
        # Site-relative unless an operator base is configured (see
        # DEFAULT_LOCAL_MEDIA_BASE_URL); the base is already rstripped of "/".
        return f"{self._local_media_base_url}/media/vod/{quote(asset_id)}/playlist.m3u8"


def _package_lock_for(live_session_id: str) -> threading.Lock:
    with _PACKAGE_LOCKS_GUARD:
        lock = _PACKAGE_LOCKS.get(live_session_id)
        if lock is None:
            lock = threading.Lock()
            _PACKAGE_LOCKS[live_session_id] = lock
        return lock


def _job_to_status(row: LiveFinalizationJob) -> LiveFinalizationStatusResponse:
    return LiveFinalizationStatusResponse.model_validate(row)


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


class FinalizationWorkerSupervisor:
    """Owns the inline worker thread for the app-lifespan deployment mode.

    ``start()`` is idempotent and a no-op unless ``settings.mode`` is
    ``inline``; ``stop()`` signals the loop's stop event and joins the thread.
    The loop body is ``run_forever``, which is synchronous and blocking
    (``ffmpeg`` work, ``Event.wait``) — it must live on a thread, never an
    asyncio task.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        settings: FinalizationWorkerSettings,
        *,
        cdn_adapter: CDNAdapter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.settings = settings
        self.cdn_adapter = cdn_adapter
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        if self.settings.mode != WORKER_MODE_INLINE:
            return
        with self._lock:
            if self.running:
                return
            self._stop_event.clear()
            worker = build_worker(
                self._session_factory, self.settings, cdn_adapter=self.cdn_adapter
            )
            self._thread = threading.Thread(
                target=worker.run_forever,
                kwargs={
                    "poll_seconds": self.settings.poll_seconds,
                    "stop_event": self._stop_event,
                },
                name="civiccast-finalization-worker",
                daemon=True,
            )
            self._thread.start()
            _LOG.info(
                "Finalization worker started (inline thread, poll=%ss, settle=%ss).",
                self.settings.poll_seconds,
                self.settings.settle_seconds,
            )

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._stop_event.set()
            thread.join(timeout=timeout)
            if thread.is_alive():  # pragma: no cover - defensive timeout path
                _LOG.warning("Finalization worker thread did not stop within %ss.", timeout)
            else:
                _LOG.info("Finalization worker stopped.")
            self._thread = None


def main(argv: list[str] | None = None) -> int:
    """External worker entrypoint: ``python -m civiccast.live.finalization_worker``.

    Requires ``DATABASE_URL``. Reads the same ``CIVICCAST_FINALIZATION_*`` /
    ``CIVICCAST_LIVE_MANIFEST_BASE_URL`` settings the in-app thread uses; the
    ``CIVICCAST_FINALIZATION_WORKER`` mode value is not consulted here — running
    this entrypoint IS the external mode. See
    ``docs/ops/finalization-worker-runbook.md``.
    """

    parser = argparse.ArgumentParser(
        prog="python -m civiccast.live.finalization_worker",
        description="CivicCast recording finalization worker (external process mode).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan and exit (smoke checks, cron-style operation).",
    )
    args = parser.parse_args(argv)

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        parser.error("DATABASE_URL must be set to run the finalization worker.")

    logging.basicConfig(level=logging.INFO)

    from contextlib import contextmanager

    from sqlalchemy import create_engine

    from civiccast.db import connect_options
    from civiccast.db.url import normalize_database_url

    database_url = normalize_database_url(database_url)
    engine = create_engine(
        database_url, future=True, pool_pre_ping=True, **connect_options(database_url)
    )
    if database_url.startswith("sqlite"):
        engine = engine.execution_options(schema_translate_map={"civiccast": None})

    @contextmanager
    def _session_factory():  # type: ignore[no-untyped-def]
        with Session(bind=engine) as session:
            yield session

    # Same CDN selection as the in-app thread (CIVICCAST_CDN_PROVIDER): the
    # external process publishes packages through the identical adapter, and
    # invalid provider config fails fast here too.
    from civiccast.stream.cdn.factory import CdnSettings, build_cdn_adapter

    settings = FinalizationWorkerSettings.from_env()
    cdn_adapter = build_cdn_adapter(CdnSettings.from_env())
    worker = build_worker(_session_factory, settings, cdn_adapter=cdn_adapter)
    try:
        if args.once:
            worker.run_once()
            return 0
        stop_event = threading.Event()
        try:
            worker.run_forever(poll_seconds=settings.poll_seconds, stop_event=stop_event)
        except KeyboardInterrupt:  # pragma: no cover - interactive shutdown path
            stop_event.set()
            _LOG.info("Finalization worker interrupted; exiting.")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess smoke test
    raise SystemExit(main())
