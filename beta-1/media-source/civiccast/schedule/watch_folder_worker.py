# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S7 watch-folder poll daemon.

PR #19 built the ``WatchFolderConfig`` data model, CRUD API, and settings
UI (``civiccast/schedule/media_lifecycle_{models,store,router}.py`` +
migration ``0079_media_lifecycle``) but explicitly deferred the daemon that
actually polls a configured directory and ingests what it finds --
``docs/spec/3.0/sections/S7-media-lifecycle-and-readiness.md`` §6's
"watch-folder monitor (background daemon)" DONE criterion was unmet: no
file on disk listed ``monitor_path``, checked for write-completion, or
called into ingest. This module is that daemon.

Same env-gated settings-dataclass / ``run_once``/``run_forever`` /
``ThreadSupervisor`` shape as
:mod:`civiccast.schedule.media_lifecycle_worker`,
:mod:`civiccast.schedule.retention_worker`, and
:mod:`civiccast.schedule.media_integrity_worker` -- this codebase's
established pattern for "periodically scan and act." It never runs its own
ingest pipeline: every file it hands off goes through the SAME path as an
operator's manual upload (:meth:`civiccast.schedule.store.PostgresAssetStore.ingest_upload`,
mirroring ``civiccast.schedule.router.upload_asset``'s ffprobe -> validate
-> hash -> thumbnail -> persist sequence) or, for a file that changed after
already being ingested, the SAME replace-source path an operator uses
(:meth:`civiccast.schedule.media_lifecycle_store.MediaLifecycleStore.apply_replace_source`),
tagged ``source_kind="watch_folder"`` for provenance
(:data:`~civiccast.schedule.media_lifecycle_models.INGEST_SOURCE_WATCH_FOLDER`).

Design decisions the spec text itself does not resolve -- processed-file
disposition (move-to-subfolder vs. leave-with-ledger), degraded/unreachable
-path visibility, and the delete-safety posture -- are recorded in
``docs/adr/0024-watch-folder-daemon-processed-file-and-degraded-state.md``
rather than picked silently (CLAUDE.md "Open decisions" policy). The short
version: the source file is NEVER deleted by this daemon in either mode.

Concurrency shape (spec doesn't say; this is this build's resolution, also
in ADR 0024): per-folder work is fully serialized -- one config's files are
scanned and (if due) ingested one at a time, in listing order, inside a
single call to :meth:`WatchFolderWorker._scan_one_folder` -- while multiple
*different* configs' folders may be scanned concurrently, bounded by
``max_concurrent_folders`` (mirrors
``MediaLifecycleWorkerSettings.max_transcode_dispatch_per_pass``'s "batch
cap per pass" idiom, just applied to folders instead of transcode jobs).
Per-file work is further bounded by
``max_files_ingested_per_pass_per_folder`` so one folder full of files
can't monopolize a pass indefinitely; leftover files are picked up on the
next due poll.

Settle-window / partial-copy safety (D13, spec §10.5): a file is only
handed to ingest once its ``(size, mtime)`` pair has been observed
IDENTICAL on two consecutive polls of its own config's cadence -- tracked
durably per file in
:class:`~civiccast.schedule.media_lifecycle_models.WatchFolderFileState`
(the "ledger" the ``leave_with_ledger`` processed-file mode refers to) so
the check survives a daemon restart between polls. A file still being
written has a size that keeps growing between polls and therefore never
satisfies two consecutive identical observations until the copy finishes.

Reprocess-on-change: a file that was already ingested and then changes
again (same path, different size/mtime) is NOT treated as a brand-new
asset -- it re-enters the settle window, and once stable again is applied
via ``apply_replace_source`` against the SAME ``asset_id`` the ledger
already associated with that path, so the existing asset's readiness/
transcode state is correctly invalidated and recomputed for the new file
rather than creating a duplicate asset.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from civiccast.schedule.ingest import (
    FfmpegNotFoundError,
    FfprobeError,
    FfprobeNotFoundError,
    UnsupportedFormatError,
    extract_thumbnail,
    hash_file,
    run_ffprobe,
)
from civiccast.schedule.ingest import validate_ingest as validate_ffprobe_result
from civiccast.schedule.media_lifecycle_models import (
    FILE_STATE_FAILED,
    FILE_STATE_INGESTED,
    FILE_STATE_INGESTING,
    FILE_STATE_PENDING,
    FILE_STATE_STABLE,
    INGEST_SOURCE_WATCH_FOLDER,
    JOB_STATUS_COMPLETED,
    PROCESSED_FILE_MODE_MOVE_TO_SUBFOLDER,
    WATCH_FOLDER_HEALTH_DEGRADED,
    WATCH_FOLDER_HEALTH_OK,
    MediaIngestJob,
    MediaLifecycleAuditEntry,
    WatchFolderConfig,
    WatchFolderFileState,
)
from civiccast.schedule.media_lifecycle_store import MediaLifecycleStore
from civiccast.schedule.store import PostgresAssetStore
from civiccast.vod.store import AssetAlreadyExistsError

SessionFactory = Callable[[], AbstractContextManager[Session]]

_LOG = logging.getLogger(__name__)


def _naive_utc(value: datetime) -> datetime:
    """Normalize a datetime for cross-dialect equality comparison.

    Postgres round-trips ``DateTime(timezone=True)`` as tz-aware; SQLite
    (used by every test in this suite, and by any station running the
    file-backed dev DB) silently drops tzinfo on the way through its
    generic DateTime adapter. Comparing an aware value freshly built from
    ``os.stat()`` against a naive value just loaded back from SQLite with
    Python's ``==`` never raises (only ordering comparisons do) -- it
    just silently returns ``False``, which would make the settle-window
    check never observe "unchanged" on SQLite. Normalizing both sides to
    naive UTC before comparing makes the check dialect-independent.
    """

    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


WATCH_FOLDER_WORKER_MODE_INLINE = "inline"
WATCH_FOLDER_WORKER_MODE_OFF = "off"
_WATCH_FOLDER_WORKER_MODES = (WATCH_FOLDER_WORKER_MODE_INLINE, WATCH_FOLDER_WORKER_MODE_OFF)

# Exceptions the ingest pipeline (ffprobe -> validate -> copy -> persist) can
# raise for a single file that must fail THAT FILE, not the whole pass or
# the whole folder -- caught per-file in _ingest_stable_file.
_INGEST_FILE_ERRORS = (
    UnsupportedFormatError,
    FfprobeNotFoundError,
    FfprobeError,
    FfmpegNotFoundError,
    OSError,
    AssetAlreadyExistsError,
)

__all__ = [
    "WatchFolderFolderResult",
    "WatchFolderScanInProgressError",
    "WatchFolderScanResult",
    "WatchFolderWorker",
    "WatchFolderWorkerSettings",
]


class WatchFolderScanInProgressError(RuntimeError):
    """Raised by :meth:`WatchFolderWorker.scan_now` when the same config is
    already mid-scan -- either another concurrent ``scan_now`` call, or the
    daemon's own :meth:`WatchFolderWorker.run_once` poll loop picking up the
    same config. Finding 4 (adversarial audit of PR #69): the pre-fix
    ``scan_now`` had no lock, no idempotency guard, and no rate limit --
    five concurrent calls against one config produced unhandled database
    errors under the SQLite test harness. A subclass of :class:`RuntimeError`
    so any caller that only knows to catch the pre-existing "daemon not
    usable" contract still fails closed (503) rather than crashing; the
    router now catches this specifically and returns 409 instead.
    """


@dataclass(frozen=True)
class WatchFolderFolderResult:
    """Outcome of one config's pass within one :meth:`WatchFolderWorker.run_once`."""

    config_id: str
    monitor_path: str
    healthy: bool
    files_seen: int = 0
    files_ingested: int = 0
    files_reprocessed: int = 0
    files_failed: int = 0
    error: str | None = None


@dataclass(frozen=True)
class WatchFolderScanResult:
    """Counters for one ``run_once`` pass across every due config."""

    folders_scanned: int = 0
    folders_degraded: int = 0
    files_ingested: int = 0
    files_reprocessed: int = 0
    files_failed: int = 0
    folder_results: tuple[WatchFolderFolderResult, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class WatchFolderWorkerSettings:
    """Deployment configuration for the watch-folder poll daemon."""

    mode: str = WATCH_FOLDER_WORKER_MODE_INLINE
    # Global tick: how often run_forever wakes up to check whether ANY
    # config is due for its own poll_interval_seconds. Per-config cadence
    # (WatchFolderConfig.poll_interval_seconds, spec §6 default 5s) governs
    # how often a given folder is actually scanned; this is the floor a
    # single-config station will effectively run at.
    poll_seconds: float = 2.0
    upload_dir: str | None = None
    # Global concurrency cap across folders; each folder's own files are
    # always processed serially within its own pass (see module docstring).
    max_concurrent_folders: int = 4
    # Bounded per-file: caps how many settle-confirmed files one config's
    # pass will hand to ingest, success or failure, so one folder can't
    # monopolize a pass. Leftovers are picked up next due poll.
    max_files_ingested_per_pass_per_folder: int = 25

    @classmethod
    def from_env(cls) -> WatchFolderWorkerSettings:
        mode = (
            os.environ.get("CIVICCAST_WATCH_FOLDER_WORKER", WATCH_FOLDER_WORKER_MODE_INLINE)
            .strip()
            .lower()
        )
        if mode not in _WATCH_FOLDER_WORKER_MODES:
            raise ValueError(
                f"CIVICCAST_WATCH_FOLDER_WORKER must be one of "
                f"{', '.join(_WATCH_FOLDER_WORKER_MODES)}; got {mode!r}."
            )
        defaults = cls()
        raw_poll = os.environ.get("CIVICCAST_WATCH_FOLDER_POLL_SECONDS", "").strip()
        poll = defaults.poll_seconds
        if raw_poll:
            try:
                poll = float(raw_poll)
            except ValueError as exc:
                raise ValueError(
                    f"CIVICCAST_WATCH_FOLDER_POLL_SECONDS must be a number; got {raw_poll!r}."
                ) from exc
        raw_concurrency = os.environ.get(
            "CIVICCAST_WATCH_FOLDER_MAX_CONCURRENT_FOLDERS", ""
        ).strip()
        max_concurrent = defaults.max_concurrent_folders
        if raw_concurrency:
            try:
                max_concurrent = int(raw_concurrency)
            except ValueError as exc:
                raise ValueError(
                    "CIVICCAST_WATCH_FOLDER_MAX_CONCURRENT_FOLDERS must be an int; "
                    f"got {raw_concurrency!r}."
                ) from exc
        return cls(
            mode=mode,
            poll_seconds=poll,
            upload_dir=os.environ.get("CIVICCAST_UPLOAD_DIR") or None,
            max_concurrent_folders=max(1, max_concurrent),
        )


class WatchFolderWorker:
    """Polls every enabled :class:`WatchFolderConfig` and ingests new/changed files."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        settings: WatchFolderWorkerSettings,
        asset_store: PostgresAssetStore | None = None,
        lifecycle_store: MediaLifecycleStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._asset_store = asset_store or PostgresAssetStore(session_factory)
        self._lifecycle_store = lifecycle_store or MediaLifecycleStore(session_factory)
        self._clock = clock or (lambda: datetime.now(UTC))
        # Finding 4: per-config scan serialization. Guards ONE config's scan
        # against itself -- a concurrent scan_now, or the daemon's own
        # run_once picking up the same config -- never against different
        # configs, which remain free to scan concurrently (bounded by
        # max_concurrent_folders, unchanged). ``_scan_locks_guard`` protects
        # only the dict's own get-or-create; the per-config ``Lock`` objects
        # it hands out are held for the duration of one scan.
        self._scan_locks: dict[str, threading.Lock] = {}
        self._scan_locks_guard = threading.Lock()

    def _acquire_scan_lock(self, config_id: str) -> bool:
        """Non-blocking. Returns ``True`` iff this call now owns the
        per-config scan lock; the caller MUST release it via
        :meth:`_release_scan_lock` in a ``finally`` block when it does.
        """

        with self._scan_locks_guard:
            lock = self._scan_locks.setdefault(config_id, threading.Lock())
        return lock.acquire(blocking=False)

    def _release_scan_lock(self, config_id: str) -> None:
        with self._scan_locks_guard:
            lock = self._scan_locks.get(config_id)
        if lock is not None:
            lock.release()

    def run_forever(
        self,
        *,
        poll_seconds: float = 2.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        while stop_event is None or not stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                _LOG.exception("Watch-folder scan failed; retrying on the next poll interval.")
            if stop_event is not None:
                stop_event.wait(poll_seconds)
            else:
                time.sleep(poll_seconds)

    # -- Operator-triggered single-folder scan -------------------------------

    def scan_now(self, config_id: str, *, now: datetime | None = None) -> WatchFolderFolderResult:
        """Scan one config immediately, ignoring its ``poll_interval_seconds`` due-check.

        Backs the staff "Scan now" action (media_lifecycle_router.py) added
        after field evidence (candidate #17 tester report, finding 4): a
        freshly-added watch folder shows ``Last poll: never`` with no way to
        force an immediate check, so an operator who just dropped a file in
        has no way to find out whether ingest is working short of waiting
        out the poll interval. Runs the exact same per-folder pass
        ``run_once`` uses (settle-window, degraded-state, and ingest
        handling all unchanged) -- never a parallel path.

        Raises :class:`ValueError` if ``config_id`` names no watch folder, so
        the router can turn that into a 404 rather than a silent no-op.
        Raises :class:`WatchFolderScanInProgressError` (Finding 4) if this
        same config is already being scanned -- by another concurrent
        ``scan_now`` call, or by the daemon's own ``run_once`` -- rather
        than racing a second pass against it; the router turns that into a
        409.
        """

        resolved_now = now or self._clock()
        if not self._settings.upload_dir:
            raise RuntimeError(
                "CIVICCAST_UPLOAD_DIR is not set; the watch-folder daemon has nowhere to "
                "ingest into."
            )
        with self._session_factory() as session:
            exists = session.get(WatchFolderConfig, config_id) is not None
        if not exists:
            raise ValueError(config_id)
        if not self._acquire_scan_lock(config_id):
            raise WatchFolderScanInProgressError(
                f"A scan for watch folder {config_id} is already in progress."
            )
        try:
            return self._scan_one_folder(config_id, resolved_now)
        finally:
            self._release_scan_lock(config_id)

    # -- Top-level pass ------------------------------------------------------

    def run_once(
        self, *, now: datetime | None = None, force_all: bool = False
    ) -> WatchFolderScanResult:
        """One pass: scan every enabled config that is due, ingest what's stable.

        ``force_all`` bypasses each config's ``poll_interval_seconds`` due-
        check (used by tests exercising the settle-window/ingest logic
        without wiring up interval timing).
        """

        resolved_now = now or self._clock()
        if not self._settings.upload_dir:
            _LOG.warning(
                "CIVICCAST_UPLOAD_DIR is not set; the watch-folder daemon has nowhere to "
                "ingest into and is skipping this pass."
            )
            return WatchFolderScanResult()

        with self._session_factory() as session:
            configs = list(
                session.execute(
                    select(WatchFolderConfig).where(WatchFolderConfig.enabled.is_(True))
                ).scalars()
            )
            due_ids = [
                c.config_id
                for c in configs
                if force_all
                or c.last_poll_at is None
                or (_naive_utc(resolved_now) - _naive_utc(c.last_poll_at)).total_seconds()
                >= c.poll_interval_seconds
            ]

        if not due_ids:
            return WatchFolderScanResult()

        results: list[WatchFolderFolderResult] = []
        workers = max(1, min(self._settings.max_concurrent_folders, len(due_ids)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._scan_one_folder_guarded, config_id, resolved_now): config_id
                for config_id in due_ids
            }
            for future in as_completed(futures):
                results.append(future.result())

        return WatchFolderScanResult(
            folders_scanned=len(results),
            folders_degraded=sum(1 for r in results if not r.healthy),
            files_ingested=sum(r.files_ingested for r in results),
            files_reprocessed=sum(r.files_reprocessed for r in results),
            files_failed=sum(r.files_failed for r in results),
            folder_results=tuple(results),
        )

    # -- Per-folder pass (fully serialized within one config) ---------------

    def _scan_one_folder_guarded(self, config_id: str, now: datetime) -> WatchFolderFolderResult:
        """Finding 4: ``run_once``'s own entry point into a per-config scan,
        so a ``run_once`` pass can never race a concurrent ``scan_now`` (or,
        in principle, an overlapping ``run_once`` pass) against the SAME
        config. Unlike :meth:`scan_now`, this coalesces rather than rejects:
        a config that's already mid-scan is silently skipped for this pass
        (not an error -- nothing here is operator-facing) and picked up
        again on the next due poll, since its ``last_poll_at`` was never
        advanced.
        """

        if not self._acquire_scan_lock(config_id):
            _LOG.info(
                "Skipping this pass's scan of %s: a scan (scan-now or another pass) is "
                "already in progress for it.",
                config_id,
            )
            return WatchFolderFolderResult(config_id=config_id, monitor_path="", healthy=True)
        try:
            return self._scan_one_folder(config_id, now)
        finally:
            self._release_scan_lock(config_id)

    def _scan_one_folder(self, config_id: str, now: datetime) -> WatchFolderFolderResult:
        # Deliberately short-lived sessions throughout this method: nothing
        # here holds a session/transaction open while the per-file ingest
        # pipeline runs (that pipeline opens its OWN short sessions via
        # self._asset_store / self._lifecycle_store / _record_watch_folder_job).
        # An earlier version held the ledger-row session open across those
        # nested calls -- harmless on Postgres's per-connection MVCC, but a
        # self-inflicted lock wait (sometimes indefinite) on SQLite's single
        # database-wide writer, surfaced by the multi-folder-concurrency
        # tests. Committing early and re-opening is the fix, and it's also
        # just better hygiene independent of which dialect is behind it.
        with self._session_factory() as session:
            config = session.get(WatchFolderConfig, config_id)
            if config is None or not config.enabled:
                return WatchFolderFolderResult(config_id=config_id, monitor_path="", healthy=True)
            monitor_path = config.monitor_path

            try:
                candidates = self._list_candidate_files(config)
            except OSError as exc:
                self._mark_degraded(session, config, now, reason=str(exc))
                session.commit()
                return WatchFolderFolderResult(
                    config_id=config_id, monitor_path=monitor_path, healthy=False, error=str(exc)
                )

            self._mark_healthy(session, config, now, files_found=len(candidates))
            processed_file_mode = config.processed_file_mode
            processed_subfolder_name = config.processed_subfolder_name
            session.commit()

        files_ingested = files_reprocessed = files_failed = 0
        attempts = 0
        cap = self._settings.max_files_ingested_per_pass_per_folder
        for path in candidates:
            if attempts >= cap:
                break
            try:
                stat = path.stat()
            except OSError:
                continue  # vanished between listdir and stat; next poll re-evaluates

            outcome = self._process_one_file(
                config_id, path, stat, now, processed_file_mode, processed_subfolder_name
            )
            if outcome == "":
                continue
            attempts += 1
            if outcome == "ingested":
                files_ingested += 1
            elif outcome == "reprocessed":
                files_reprocessed += 1
            elif outcome == "failed":
                files_failed += 1

        if files_ingested or files_reprocessed:
            with self._session_factory() as session:
                config = session.get(WatchFolderConfig, config_id)
                if config is not None:
                    config.last_ingest_at = now
                    config.updated_at = now
                    session.commit()

        return WatchFolderFolderResult(
            config_id=config_id,
            monitor_path=monitor_path,
            healthy=True,
            files_seen=len(candidates),
            files_ingested=files_ingested,
            files_reprocessed=files_reprocessed,
            files_failed=files_failed,
        )

    def _list_candidate_files(self, config: WatchFolderConfig) -> list[Path]:
        """List files directly under ``monitor_path``.

        Raises :class:`OSError` (its usual subclasses -- ``FileNotFoundError``
        for a missing local/USB mount, generic ``OSError`` for an
        unreachable SMB share) when the directory cannot be listed at all;
        the caller treats that as the degraded-state trigger. Never
        descends into the ``processed_subfolder_name`` directory (or any
        subdirectory) -- watch-folder ingest is flat by spec (§6 lists
        files in ``monitor_path``), and not recursing is also what keeps a
        move-to-subfolder disposition from being immediately re-discovered
        as a "new" file.
        """

        root = Path(config.monitor_path)
        names = sorted(entry.name for entry in root.iterdir())
        pattern = config.import_naming_pattern
        candidates: list[Path] = []
        for name in names:
            candidate = root / name
            if not candidate.is_file():
                continue
            if pattern and not fnmatch.fnmatch(name, pattern):
                continue
            candidates.append(candidate)
        return candidates

    def _mark_degraded(
        self, session: Session, config: WatchFolderConfig, now: datetime, *, reason: str
    ) -> None:
        was_degraded = config.health_status == WATCH_FOLDER_HEALTH_DEGRADED
        config.health_status = WATCH_FOLDER_HEALTH_DEGRADED
        config.degraded_reason = reason[:2000]
        if not was_degraded:
            config.degraded_since = now
            session.add(
                MediaLifecycleAuditEntry(
                    asset_id=None,
                    action="watch_folder_degraded",
                    detail=f"{config.config_id} ({config.monitor_path}): {reason}",
                    dry_run=False,
                )
            )
        config.last_poll_at = now
        config.updated_at = now

    def _mark_healthy(
        self, session: Session, config: WatchFolderConfig, now: datetime, *, files_found: int
    ) -> None:
        was_degraded = config.health_status == WATCH_FOLDER_HEALTH_DEGRADED
        config.health_status = WATCH_FOLDER_HEALTH_OK
        config.degraded_reason = None
        config.degraded_since = None
        config.last_poll_at = now
        config.last_scanned_at = now
        config.last_scan_files_found = files_found
        config.updated_at = now
        if was_degraded:
            session.add(
                MediaLifecycleAuditEntry(
                    asset_id=None,
                    action="watch_folder_recovered",
                    detail=f"{config.config_id} ({config.monitor_path}) is reachable again.",
                    dry_run=False,
                )
            )

    # -- Per-file settle-window + ingest dispatch ----------------------------

    def _process_one_file(
        self,
        config_id: str,
        path: Path,
        stat: os.stat_result,
        now: datetime,
        processed_file_mode: str,
        processed_subfolder_name: str,
    ) -> str:
        """Advance one file's ledger row. Returns ``""``, ``"ingested"``,
        ``"reprocessed"``, or ``"failed"``.

        Opens and commits its own short session for the ledger bookkeeping
        below, then -- ONLY when stability is newly confirmed, and with no
        session held open -- calls :meth:`_ingest_stable_file`. See
        ``_scan_one_folder``'s comment for why nothing here holds a
        transaction open across that call.
        """

        file_path_str = str(path)
        size = stat.st_size
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        newly_stable_state_id: str | None = None
        existing_asset_id: str | None = None

        with self._session_factory() as session:
            row = session.execute(
                select(WatchFolderFileState).where(
                    WatchFolderFileState.config_id == config_id,
                    WatchFolderFileState.file_path == file_path_str,
                )
            ).scalar_one_or_none()

            if row is None:
                session.add(
                    WatchFolderFileState(
                        config_id=config_id,
                        file_path=file_path_str,
                        file_size_bytes=size,
                        file_mtime=mtime,
                        status=FILE_STATE_PENDING,
                        stable_since=None,
                        first_seen_at=now,
                        last_seen_at=now,
                        updated_at=now,
                    )
                )
                session.commit()
                return ""  # first observation; needs a second matching poll

            row.last_seen_at = now
            unchanged = row.file_size_bytes == size and _naive_utc(row.file_mtime) == _naive_utc(
                mtime
            )

            if row.status == FILE_STATE_INGESTED:
                if unchanged:
                    session.commit()
                    return ""  # already ingested, untouched since -- nothing to do
                # Reprocess-on-change: re-enter the settle window before
                # touching the asset again (partial-copy safety applies to
                # updates too, not just first ingest).
                row.status = FILE_STATE_PENDING
                row.file_size_bytes = size
                row.file_mtime = mtime
                row.stable_since = None
                row.error_detail = None
                row.updated_at = now
                session.commit()
                return ""

            if not unchanged:
                # Still changing (mid-copy), or changed again since a prior
                # STABLE/FAILED observation before we got to it -- reset the
                # settle window. This is the partial-copy safety guard.
                row.status = FILE_STATE_PENDING
                row.file_size_bytes = size
                row.file_mtime = mtime
                row.stable_since = None
                row.error_detail = None
                row.updated_at = now
                session.commit()
                return ""

            # Unchanged from the previously recorded observation -- two
            # consecutive polls now agree (D13 settle window satisfied).
            if row.stable_since is None:
                row.stable_since = now
            row.status = FILE_STATE_STABLE
            row.updated_at = now
            newly_stable_state_id = row.state_id
            existing_asset_id = row.asset_id
            session.commit()

        assert newly_stable_state_id is not None
        return self._ingest_stable_file(
            state_id=newly_stable_state_id,
            existing_asset_id=existing_asset_id,
            path=path,
            now=now,
            processed_file_mode=processed_file_mode,
            processed_subfolder_name=processed_subfolder_name,
        )

    def _ingest_stable_file(
        self,
        *,
        state_id: str,
        existing_asset_id: str | None,
        path: Path,
        now: datetime,
        processed_file_mode: str,
        processed_subfolder_name: str,
    ) -> str:
        is_reprocess = existing_asset_id is not None

        with self._session_factory() as session:
            row = session.get(WatchFolderFileState, state_id)
            if row is None:
                return ""  # config or ledger row deleted concurrently
            row.status = FILE_STATE_INGESTING
            row.updated_at = now
            session.commit()

        # No session held open here: the ingest pipeline opens its own
        # short sessions (self._asset_store.ingest_upload,
        # self._lifecycle_store.apply_replace_source, or
        # _record_watch_folder_job).
        try:
            asset_id, job_id, content_hash = self._run_ingest_pipeline(
                source_path=path, existing_asset_id=existing_asset_id, now=now
            )
        except _INGEST_FILE_ERRORS as exc:
            with self._session_factory() as session:
                row = session.get(WatchFolderFileState, state_id)
                if row is not None:
                    row.status = FILE_STATE_FAILED
                    row.error_detail = str(exc)[:2000]
                    row.updated_at = now
                session.add(
                    MediaLifecycleAuditEntry(
                        asset_id=existing_asset_id,
                        action="watch_folder_ingest_failed",
                        detail=f"{path}: {exc}",
                        dry_run=False,
                    )
                )
                session.commit()
            return "failed"

        self._apply_processed_disposition(
            path,
            processed_file_mode=processed_file_mode,
            processed_subfolder_name=processed_subfolder_name,
        )

        with self._session_factory() as session:
            row = session.get(WatchFolderFileState, state_id)
            if row is not None:
                row.status = FILE_STATE_INGESTED
                row.asset_id = asset_id
                row.last_ingest_job_id = job_id
                row.content_hash = content_hash
                row.ingested_at = now
                row.error_detail = None
                row.updated_at = now
            session.add(
                MediaLifecycleAuditEntry(
                    asset_id=asset_id,
                    action="watch_folder_reingested" if is_reprocess else "watch_folder_ingested",
                    detail=f"{path} -> asset {asset_id} (job {job_id})",
                    dry_run=False,
                )
            )
            session.commit()

        return "reprocessed" if is_reprocess else "ingested"

    def _run_ingest_pipeline(
        self,
        *,
        source_path: Path,
        existing_asset_id: str | None,
        now: datetime,
    ) -> tuple[str, str, str | None]:
        """Same ffprobe -> validate -> hash -> thumbnail -> persist sequence
        as :func:`civiccast.schedule.router.upload_asset`, plus (for a
        never-before-seen path) a :class:`MediaIngestJob` row, or (for a
        path whose ledger already names an ``asset_id``) the same
        replace-source path an operator uses. Never a parallel pipeline.
        """

        assert self._settings.upload_dir is not None
        upload_dir = Path(self._settings.upload_dir).resolve()

        ffprobe_result = run_ffprobe(source_path)
        validate_ffprobe_result(ffprobe_result)

        asset_id = existing_asset_id or self._derive_asset_id(source_path)
        asset_dir = (upload_dir / asset_id).resolve()
        if not asset_dir.is_relative_to(upload_dir):
            raise OSError(f"Derived asset_id {asset_id!r} resolves outside the upload directory.")
        asset_dir.mkdir(parents=True, exist_ok=True)

        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", source_path.name) or "watch-folder-file"
        dest_path = (asset_dir / safe_name).resolve()
        if not dest_path.is_relative_to(asset_dir):
            raise OSError("Invalid derived filename.")

        # Copy, never move: the watch-folder original is untouched by this
        # step regardless of processed_file_mode (delete-safety posture,
        # ADR 0024) -- _apply_processed_disposition decides afterward
        # whether the original stays put or moves to a processed subfolder.
        shutil.copy2(source_path, dest_path)

        # Post-copy size verification (spec §10.5/D13: "verify a CRC/size
        # match after copy before queueing ingest"). A full CRC/hash
        # comparison would require hashing the SMB-side source too --
        # doubling read I/O over the network for every file -- so this
        # build verifies size, the cheap check that catches the realistic
        # SMB failure mode (a connection drop mid-copy truncating the
        # destination). A source-side content hash to fully close D13's
        # "CRC" wording is a documented follow-up, not silently skipped.
        source_size = source_path.stat().st_size
        dest_size = dest_path.stat().st_size
        if source_size != dest_size:
            dest_path.unlink(missing_ok=True)
            raise OSError(
                f"Post-copy size mismatch for {source_path}: source={source_size} bytes, "
                f"copied={dest_size} bytes. Discarded the partial copy; will retry next poll."
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

        file_size_bytes = dest_path.stat().st_size

        if existing_asset_id is None:
            try:
                response = self._asset_store.ingest_upload(
                    asset_id=asset_id,
                    title=source_path.stem,
                    description=None,
                    file_path=str(dest_path),
                    file_size_bytes=file_size_bytes,
                    ffprobe_result=ffprobe_result,
                    content_hash=content_hash,
                    thumbnail_path=str(thumbnail_path) if thumbnail_path is not None else None,
                )
            except AssetAlreadyExistsError:
                dest_path.unlink(missing_ok=True)
                raise
            job_id = self._record_watch_folder_job(response.asset_id, source_path, now)
            return response.asset_id, job_id, content_hash

        job_id = self._lifecycle_store.apply_replace_source(
            existing_asset_id,
            new_file_path=str(dest_path),
            file_size_bytes=file_size_bytes,
            codec_video=ffprobe_result.codec_video,
            codec_audio=ffprobe_result.codec_audio,
            width_px=ffprobe_result.width_px,
            height_px=ffprobe_result.height_px,
            bitrate_bps=ffprobe_result.bitrate_bps,
            format_name=ffprobe_result.format_name,
            duration_seconds=ffprobe_result.duration_seconds,
            content_hash=content_hash,
            thumbnail_path=str(thumbnail_path) if thumbnail_path is not None else None,
            archived_old_path=None,
            source_kind=INGEST_SOURCE_WATCH_FOLDER,
        )
        return existing_asset_id, job_id, content_hash

    def _record_watch_folder_job(self, asset_id: str, source_path: Path, now: datetime) -> str:
        """``PostgresAssetStore.ingest_upload`` doesn't create a
        :class:`MediaIngestJob` (only ``apply_replace_source`` does, today)
        -- this is that row for the first-ingest path, so
        ``source_kind="watch_folder"`` provenance exists for every
        watch-folder-originated asset, not just reprocessed ones."""

        with self._session_factory() as session:
            job = MediaIngestJob(
                asset_id=asset_id,
                source_kind=INGEST_SOURCE_WATCH_FOLDER,
                source_path=str(source_path),
                status=JOB_STATUS_COMPLETED,
                progress_percent=100,
                started_at=now,
                completed_at=now,
            )
            session.add(job)
            session.flush()
            job_id = job.job_id
            session.commit()
            return job_id

    def _derive_asset_id(self, source_path: Path) -> str:
        """Deterministic-ish slug from the filename + a random suffix.

        Router's regex is ``^[a-z0-9][a-z0-9-]{2,63}$``. The random suffix
        avoids needing a pre-check query for uniqueness (bounded, since
        collisions are astronomically unlikely and a real collision simply
        fails this file this pass -- caught by ``_INGEST_FILE_ERRORS`` --
        and is retried next poll with a fresh suffix).
        """

        base = re.sub(r"[^a-z0-9-]", "-", source_path.stem.lower()).strip("-")
        base = (base or "watch-folder-asset")[:40]
        if not base[0].isalnum():
            base = f"a{base}"
        candidate = f"{base}-{uuid.uuid4().hex[:8]}"
        return candidate[:64]

    def _apply_processed_disposition(
        self, path: Path, *, processed_file_mode: str, processed_subfolder_name: str
    ) -> None:
        """ADR 0024: never deletes the source file in either mode.

        ``leave_with_ledger`` (default) leaves the file exactly where it
        was; :class:`WatchFolderFileState` is the durable record that it
        was already ingested, so subsequent polls don't re-ingest it (see
        ``_process_one_file``'s ``FILE_STATE_INGESTED`` + unchanged branch).
        ``move_to_subfolder`` additionally relocates it under
        ``processed_subfolder_name`` (created if absent) purely for
        operator tidiness -- a move failure (permissions, still-locked
        handle) is logged and left in place; the ledger already marks it
        ingested either way, so nothing is re-ingested or lost.
        """

        if processed_file_mode != PROCESSED_FILE_MODE_MOVE_TO_SUBFOLDER:
            return
        subfolder = processed_subfolder_name or "processed"
        processed_dir = path.parent / subfolder
        try:
            processed_dir.mkdir(parents=True, exist_ok=True)
            dest = processed_dir / path.name
            if dest.exists():
                dest = processed_dir / f"{path.stem}-{uuid.uuid4().hex[:8]}{path.suffix}"
            shutil.move(str(path), str(dest))
        except OSError:
            _LOG.warning(
                "Could not move %s to processed subfolder %s; leaving it in place "
                "(the ledger already marks it ingested, so it will not be re-ingested).",
                path,
                processed_dir,
            )
