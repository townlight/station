# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S7 media lifecycle worker: readiness, transcode dispatch, archival gate.

Mirrors :mod:`civiccast.schedule.retention_worker` and
:mod:`civiccast.schedule.media_integrity_worker`'s shape (same env-gated
settings dataclass, same ``run_once``/``run_forever`` split, same
``ThreadSupervisor`` wiring in ``civiccast.app``) -- that pair is this
codebase's established pattern for "periodically scan a table and flag/
compute something for an operator to see," and readiness computation is the
same shape of problem.

One pass of :meth:`MediaLifecycleWorker.run_once` does four things, each
independently covered by the DONE criteria in
``docs/spec/3.0/sections/S7-media-lifecycle-and-readiness.md`` §9:

1. **Recompute readiness** for every asset (state machine + loudness gate +
   in-flight transcode jobs + archive verification -> one
   :class:`~civiccast.schedule.media_lifecycle_models.AssetReadiness` row).
2. **Seed transcode jobs** for a newly-validated asset that has none yet
   (spec §6 "Transcode on ingest").
3. **Dispatch pending transcode jobs** through the injectable
   :class:`TranscodeExecutor` seam -- production wires
   :class:`FfmpegTranscodeExecutor`; tests inject a stub (mirrors the
   ``AssetFinalizerProtocol`` seam pattern S21 uses, per the S7 spec's own
   S21 cross-reference).
4. **Verify archival** (CLAUDE.md §4.6): an asset's ``archive_complete``
   flips true only when the portal, Internet Archive, and local-NAS tiers
   each have independent verification -- portal from the asset's own
   published manifest, IA/NAS from non-simulated
   :class:`~civiccast.schedule.media_lifecycle_models.AssetArchiveProof`
   rows. Nothing here uploads to IA/NAS; S7 owns the verification gate and
   badge, matching the ownership-boundary style the spec itself uses for
   loudness (D6) -- the publish pipeline that performs the actual archive
   writes calls :func:`record_archive_proof` when a write succeeds.

Every write this worker makes is preceded by a
:class:`~civiccast.schedule.media_lifecycle_models.MediaLifecycleAuditEntry`
row. In ``dry_run`` mode the same entries are written (tagged
``dry_run=True``) but no other table is mutated -- an operator can diff what
a live run would have done before switching the worker to apply mode.

Missing-media detection (spec §5 "Missing Media Alert") is intentionally
NOT a durable flag table here: "asset is missing" is a live join between
``schedule_items`` and ``assets``/``asset_readiness`` that self-heals the
moment the asset is fixed, so a stale flag row would be actively
misleading. :meth:`MediaLifecycleWorker.list_missing_media` runs that join
on demand; the worker's periodic pass still records a
``missing_media_flagged`` audit-log entry per pass so the count is visible
in the audit trail without a second source of truth.

**Transcode defaults and resource posture (ADR 0007 amendment, 2026-08-24
S7 audit):** step 2 above previously seeded a fixed three-format ladder
including an HEVC target with a bare ``libx265`` (GPL) literal -- a license
violation, since this repo never links a GPL encoder into the product -- and
dispatched every job in step 3 synchronously at normal process priority
under ffmpeg's flat 6-hour default timeout, all unconditionally on by
default. That combination is a large amount of unsupervised, full-priority
ffmpeg work sharing the box with operator requests, with no way for an
operator to turn it down. Fixed here: the HEVC target is removed (no
GPL-free HEVC encoder exists anywhere in this tree to resolve it to,
unlike H.264's real ``resolve_h264_encoder()`` path), the seeded default is
now a single resolution-aware web rendition that never upscales past the
source's own probed height (mirrors PR #45's ``select_ladder`` fix to the
VOD packager), transcode seeding itself has a station-level off switch
(``transcode_seeding_enabled``), and each dispatched job runs at
BELOW_NORMAL process priority under a per-minute-of-source timeout budget
instead of the flat 6h ceiling. See ``_FORMAT_FFMPEG_ARGS``,
``MediaLifecycleWorkerSettings``, and ``_transcode_timeout_seconds`` below,
and ``DEFAULT_TRANSCODE_FORMATS``' docstring in ``media_lifecycle_models``
for the full rationale.
"""

from __future__ import annotations

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

from sqlalchemy import select
from sqlalchemy.orm import Session

from civiccast.native.supervisor.install_layout import default_civiccast_data_root
from civiccast.schedule.media_lifecycle_models import (
    ARCHIVE_TARGET_INTERNET_ARCHIVE,
    DEFAULT_TRANSCODE_FORMATS,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    LOUDNESS_FAILED,
    LOUDNESS_NOT_CHECKED,
    LOUDNESS_OK,
    READINESS_MISSING_FILE,
    READINESS_NOT_READY,
    READINESS_PENDING_TRANSCODE,
    READINESS_READY,
    READINESS_REJECTED,
    READINESS_TRANSCODING,
    AssetArchiveProof,
    AssetReadiness,
    MediaLifecycleAuditEntry,
    TranscodeJob,
)
from civiccast.schedule.models import (
    ASSET_STATE_RECORDED,
    ASSET_STATE_REJECTED,
    ASSET_STATE_VALIDATED,
    FILE_STATUS_MISSING,
    Asset,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]

_LOG = logging.getLogger(__name__)

MEDIA_LIFECYCLE_WORKER_MODE_INLINE = "inline"
MEDIA_LIFECYCLE_WORKER_MODE_OFF = "off"
_MEDIA_LIFECYCLE_WORKER_MODES = (
    MEDIA_LIFECYCLE_WORKER_MODE_INLINE,
    MEDIA_LIFECYCLE_WORKER_MODE_OFF,
)

# Local NAS target types the archival gate accepts as the "NAS peer" tier
# (any one non-simulated proof of these types satisfies the tier -- mirrors
# civiccast.archive.models.ArchiveProof.target_type's local_nas_* set).
_NAS_TARGET_TYPES = (
    "local_nas_rsync",
    "local_nas_zfs",
    "local_nas_copy",
    "local_nas_snapshot_copy",
)

# Streaming default per civiccast.stream.loudness -- S2/S11 own per-sink
# target selection at egress (D6); S7's ingest-time gate uses the station's
# streaming default so the badge means something before a sink is even
# configured.
_INGEST_LOUDNESS_TARGET_LUFS = -16.0
_INGEST_LOUDNESS_TOLERANCE_LUFS = 1.0

__all__ = [
    "FfmpegTranscodeExecutor",
    "MediaLifecycleScanResult",
    "MediaLifecycleWorker",
    "MediaLifecycleWorkerSettings",
    "StubTranscodeExecutor",
    "TranscodeExecutionResult",
    "TranscodeExecutor",
    "record_archive_proof",
]


@dataclass(frozen=True)
class MediaLifecycleScanResult:
    """Counters for one ``run_once`` pass -- the audit trail's summary row."""

    readiness_recomputed: int = 0
    readiness_changed: int = 0
    transcode_jobs_seeded: int = 0
    transcode_jobs_dispatched: int = 0
    transcode_jobs_completed: int = 0
    transcode_jobs_failed: int = 0
    archive_verified: int = 0
    missing_media_count: int = 0
    dry_run: bool = False


@dataclass(frozen=True)
class TranscodeExecutionResult:
    success: bool
    output_path: str | None = None
    file_size_bytes: int | None = None
    error_detail: str | None = None


class TranscodeExecutor(Protocol):
    """Injected seam for running one transcode job (mirrors S21's pattern)."""

    def run(
        self, *, asset: Asset, output_format: str, output_dir: Path
    ) -> TranscodeExecutionResult: ...


class StubTranscodeExecutor:
    """Deterministic no-ffmpeg executor for unit tests.

    Always "succeeds" without touching the filesystem, reporting the
    source asset's own file size (a proxy is never larger than its
    mezzanine source). Production code never uses this; the worker
    defaults to :class:`FfmpegTranscodeExecutor`.
    """

    def run(
        self, *, asset: Asset, output_format: str, output_dir: Path
    ) -> TranscodeExecutionResult:
        return TranscodeExecutionResult(
            success=True,
            output_path=str(output_dir / f"{asset.asset_id}.{output_format}.mp4"),
            file_size_bytes=asset.file_size_bytes,
        )


# Coarse format -> ffmpeg args template. Real per-format tuning (rate
# control, GOP, audio profile) is a station-config follow-up; this is
# enough to produce a genuinely playable proxy/mezzanine file today.
#
# "{h264_encoder}" is a placeholder, not a literal encoder name: ADR 0007
# forbids a bare "libx264" (GPL) literal in production code outside
# civiccast.stream._ffmpeg's own resolver (tests/policy/test_ffmpeg_h264_encoder.py
# enforces this repo-wide). FfmpegTranscodeExecutor.run resolves the
# station's actual encoder at call time via resolve_h264_encoder() --
# hardware first, then h264_mf, then the royalty-free libopenh264, with
# libx264 reachable only when the probed binary itself carries it.
#
# "{scale_filter}" is likewise resolved at call time, never baked in as a
# fixed "scale=-2:<height>" -- FfmpegTranscodeExecutor.run caps the target
# height at the SOURCE asset's own height_px (never upscales), the same
# posture PR #45 gave the VOD packager's ABR ladder
# (civiccast.stream.config.select_ladder) after a 640x360 source encoding
# needlessly-upscaled 1080p/720p rungs blew a packaging request's client
# timeout. _FORMAT_TARGET_HEIGHT below names each format's own cap.
#
# There was a third entry here, "h265_1080p_8mbps", seeded by default and
# carrying a bare "libx265" (GPL) literal -- a license violation this ADR
# 0007 amendment removes; see DEFAULT_TRANSCODE_FORMATS' docstring in
# civiccast.schedule.media_lifecycle_models for the full rationale (no
# sanctioned GPL-free HEVC encoder exists anywhere in this tree, so the
# target is removed rather than shipped broken-by-default or silently
# GPL-linked).
_FORMAT_FFMPEG_ARGS: dict[str, list[str]] = {
    "h264_720p_5mbps": [
        "-vf",
        "{scale_filter}",
        "-c:v",
        "{h264_encoder}",
        "-b:v",
        "5M",
        "-c:a",
        "aac",
    ],
    "h264_mezzanine": ["-c:v", "{h264_encoder}", "-crf", "12", "-c:a", "aac"],
}

#: Target rendition height per format, for the never-upscale cap above.
#: Formats absent here (``h264_mezzanine``) carry no ``-vf scale`` at all --
#: mezzanine is deliberately full-source-resolution.
_FORMAT_TARGET_HEIGHT: dict[str, int] = {"h264_720p_5mbps": 720}

# Resource posture (ADR 0007 amendment, S7 audit): the worker previously
# dispatched every pending transcode synchronously in its own thread with
# ffmpeg's full 6-hour default timeout and normal process priority -- a lot
# of unsupervised, full-priority ffmpeg work competing with whatever else is
# serving operator requests on the same box. Bound per job to a budget
# proportional to the SOURCE's own duration instead of one flat ceiling:
# generous enough that a real-time-or-slower software encode on modest
# hardware still finishes comfortably, floored so a short clip's ffmpeg
# startup/flush overhead is never mistaken for a hang, and capped well below
# the previous flat 6h so a truly stuck process is caught much sooner.
_TRANSCODE_TIMEOUT_FLOOR_SECONDS = 600.0  # 10 min: absorbs startup/flush on short clips
_TRANSCODE_TIMEOUT_PER_SOURCE_SECOND = 10.0  # budget: 10x realtime
_TRANSCODE_TIMEOUT_CEILING_SECONDS = (
    2.0 * 3600
)  # 2h absolute ceiling (down from ffmpeg's 6h default)


def _transcode_timeout_seconds(duration_seconds: int | None) -> float:
    """Per-job ffmpeg timeout: a per-minute-of-source budget, floored and capped.

    ``duration_seconds`` unknown (asset row has no probed duration) falls
    back to the ceiling -- the same "don't guess a smaller budget than the
    source might need" posture as the never-upscale cap above, just applied
    to time instead of pixels.
    """
    if not duration_seconds or duration_seconds <= 0:
        return _TRANSCODE_TIMEOUT_CEILING_SECONDS
    budget = duration_seconds * _TRANSCODE_TIMEOUT_PER_SOURCE_SECOND
    return min(_TRANSCODE_TIMEOUT_CEILING_SECONDS, max(_TRANSCODE_TIMEOUT_FLOOR_SECONDS, budget))


class FfmpegTranscodeExecutor:
    """Production :class:`TranscodeExecutor` -- shells out through the shared
    ffmpeg wrapper (``civiccast.stream._ffmpeg.run_ffmpeg``), the single
    permitted seam for ffmpeg subprocess calls per ADR 0007.
    """

    def run(
        self, *, asset: Asset, output_format: str, output_dir: Path
    ) -> TranscodeExecutionResult:
        from civiccast.stream._ffmpeg import (
            H264EncoderUnavailableError,
            check_ffmpeg,
            resolve_h264_encoder,
            run_ffmpeg,
        )

        if not asset.file_path:
            return TranscodeExecutionResult(
                success=False, error_detail="Asset has no source file_path."
            )
        source = Path(asset.file_path)
        if not source.is_file():
            return TranscodeExecutionResult(
                success=False, error_detail=f"Source file not found at {source}."
            )
        if check_ffmpeg() is None:
            return TranscodeExecutionResult(
                success=False,
                error_detail="ffmpeg is not installed; run `civiccast doctor` and retry.",
            )
        template = _FORMAT_FFMPEG_ARGS.get(output_format)
        if template is None:
            return TranscodeExecutionResult(
                success=False, error_detail=f"Unknown transcode output_format {output_format!r}."
            )
        extra_args = list(template)
        if "{scale_filter}" in extra_args:
            # Never upscale: cap the rung's target height at the source's
            # own height_px when ffprobe measured one at ingest (mirrors
            # civiccast.stream.config.select_ladder's posture from PR #45).
            # Unknown height_px (probe never ran / failed) falls back to the
            # rung's own cap unchanged -- the previous, safe default.
            target_height = _FORMAT_TARGET_HEIGHT[output_format]
            effective_height = target_height
            if asset.height_px and asset.height_px > 0:
                effective_height = min(target_height, asset.height_px)
                effective_height -= effective_height % 2  # yuv420p needs even dims
                effective_height = max(2, effective_height)
            scale_filter = f"scale=-2:{effective_height}"
            extra_args = [scale_filter if arg == "{scale_filter}" else arg for arg in extra_args]
        if "{h264_encoder}" in extra_args:
            # ADR 0007 / tests/policy/test_ffmpeg_h264_encoder.py: no bare
            # "libx264" literal outside the resolver. Resolve the station's
            # actual encoder (hardware first, then h264_mf, then the
            # royalty-free libopenh264; libx264 only when the probed binary
            # itself carries it) at call time instead.
            try:
                h264_encoder = resolve_h264_encoder()
            except H264EncoderUnavailableError as exc:
                return TranscodeExecutionResult(success=False, error_detail=str(exc))
            extra_args = [h264_encoder if arg == "{h264_encoder}" else arg for arg in extra_args]
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{asset.asset_id}.{output_format}.mp4"
        # Resource posture (ADR 0007 amendment): this is unattended
        # background work sharing the box with operator requests, so it
        # runs BELOW_NORMAL priority (Windows; harmless no-op elsewhere) and
        # under a per-minute-of-source timeout budget instead of ffmpeg's
        # flat 6-hour default -- see _transcode_timeout_seconds.
        result = run_ffmpeg(
            ["-y", "-i", str(source), *extra_args, str(output_path)],
            timeout=_transcode_timeout_seconds(asset.duration_seconds),
            lower_priority=True,
        )
        if result.returncode != 0 or not output_path.is_file():
            return TranscodeExecutionResult(
                success=False,
                error_detail=(result.stderr or "ffmpeg exited non-zero with no stderr")[-2000:],
            )
        return TranscodeExecutionResult(
            success=True,
            output_path=str(output_path),
            file_size_bytes=output_path.stat().st_size,
        )


@dataclass(frozen=True)
class MediaLifecycleWorkerSettings:
    """Deployment configuration for the media lifecycle worker.

    Resource posture (ADR 0007 amendment, S7 audit): earlier defaults seeded
    every configured transcode format for every validated asset unconditionally,
    with no way for an operator to say "not on this station" -- a station-in-
    a-box product has exactly one place to surface that choice, this
    env-gated settings surface (the same mechanism ``retention_worker`` and
    ``media_integrity_worker`` already use as their own per-station config
    surface). ``transcode_seeding_enabled`` is that switch: default ``True``
    so out-of-box ingest still produces a playable proxy (S7 spec §6/DONE
    criterion 3 -- "Upload valid asset -> validated -> TranscodeJob created"
    -- is a first-run expectation, not something to silently drop), but an
    operator who wants zero unattended background ffmpeg on their box can
    turn it off with ``CIVICCAST_MEDIA_LIFECYCLE_TRANSCODE_SEEDING_ENABLED=0``.
    Honest copy for that switch belongs on the operator settings screen;
    no such screen exists yet for this worker (there is no UI surface at
    all today, seeding or otherwise) -- flagged as a real follow-up, not
    claimed as done here.
    """

    mode: str = MEDIA_LIFECYCLE_WORKER_MODE_INLINE
    poll_seconds: float = 300.0
    dry_run: bool = False
    transcode_formats: tuple[str, ...] = DEFAULT_TRANSCODE_FORMATS
    # OFF switch for the whole transcode-on-ingest feature (see docstring
    # above); ON by default so first-run behaviour still produces a proxy.
    transcode_seeding_enabled: bool = True
    missing_media_horizon_days: int = 7
    transcode_output_root: str | None = None
    # Batch cap per pass, so one scan can't monopolize the box's ffmpeg
    # capacity indefinitely; the next poll picks up whatever's left.
    max_transcode_dispatch_per_pass: int = 4
    # Config-surfaced per ADR 0007's amendment, but there is only one
    # implemented value: _dispatch_pending_transcodes already runs jobs one
    # at a time in a single loop, never in parallel (no thread pool, no
    # asyncio.gather). This field makes that fact visible and station-
    # configurable in principle rather than an unstated implementation
    # detail; raising on anything else keeps the field honest instead of a
    # setting nothing reads.
    transcode_concurrency: int = 1

    def __post_init__(self) -> None:
        if self.transcode_concurrency != 1:
            raise ValueError(
                "transcode_concurrency must be 1 -- the dispatch loop runs "
                "jobs strictly one at a time; parallel dispatch is not "
                f"implemented (got {self.transcode_concurrency!r})."
            )

    @classmethod
    def from_env(cls) -> MediaLifecycleWorkerSettings:
        mode = (
            os.environ.get("CIVICCAST_MEDIA_LIFECYCLE_WORKER", MEDIA_LIFECYCLE_WORKER_MODE_INLINE)
            .strip()
            .lower()
        )
        if mode not in _MEDIA_LIFECYCLE_WORKER_MODES:
            raise ValueError(
                f"CIVICCAST_MEDIA_LIFECYCLE_WORKER must be one of "
                f"{', '.join(_MEDIA_LIFECYCLE_WORKER_MODES)}; got {mode!r}."
            )
        defaults = cls()
        raw_poll = os.environ.get("CIVICCAST_MEDIA_LIFECYCLE_POLL_SECONDS", "").strip()
        poll = defaults.poll_seconds
        if raw_poll:
            try:
                poll = float(raw_poll)
            except ValueError as exc:
                raise ValueError(
                    f"CIVICCAST_MEDIA_LIFECYCLE_POLL_SECONDS must be a number; got {raw_poll!r}."
                ) from exc
        dry_run_raw = os.environ.get("CIVICCAST_MEDIA_LIFECYCLE_WORKER_DRY_RUN", "").strip().lower()
        dry_run = dry_run_raw in ("1", "true", "yes", "on")
        formats_raw = os.environ.get("CIVICCAST_TRANSCODE_FORMATS", "").strip()
        formats = (
            tuple(f.strip() for f in formats_raw.split(",") if f.strip())
            or defaults.transcode_formats
        )
        seeding_raw = os.environ.get("CIVICCAST_MEDIA_LIFECYCLE_TRANSCODE_SEEDING_ENABLED")
        seeding_enabled = (
            defaults.transcode_seeding_enabled
            if seeding_raw is None
            else seeding_raw.strip().lower() in ("1", "true", "yes", "on")
        )
        horizon_raw = os.environ.get("CIVICCAST_MISSING_MEDIA_HORIZON_DAYS", "").strip()
        horizon = defaults.missing_media_horizon_days
        if horizon_raw:
            try:
                horizon = int(horizon_raw)
            except ValueError as exc:
                raise ValueError(
                    f"CIVICCAST_MISSING_MEDIA_HORIZON_DAYS must be an int; got {horizon_raw!r}."
                ) from exc
        return cls(
            mode=mode,
            poll_seconds=poll,
            dry_run=dry_run,
            transcode_formats=formats,
            transcode_seeding_enabled=seeding_enabled,
            missing_media_horizon_days=horizon,
            transcode_output_root=os.environ.get("CIVICCAST_TRANSCODE_OUTPUT_ROOT") or None,
        )


def record_archive_proof(session: Session, *, asset_id: str, proof: object) -> AssetArchiveProof:
    """Persist a produced :class:`civiccast.archive.models.ArchiveProof`.

    Called by whatever publish/archive pipeline performs the actual IA/NAS
    write (out of S7's scope -- S7 owns verification, not the upload
    itself, matching the spec's D6 ownership-boundary pattern). Accepts
    ``proof`` structurally (duck-typed) rather than importing
    ``civiccast.archive.models`` directly, so this module has no import-time
    dependency on the archive module.
    """

    row = AssetArchiveProof(
        asset_id=asset_id,
        target_type=proof.target_type,  # type: ignore[attr-defined]
        target_url_or_path=proof.target_url_or_path,  # type: ignore[attr-defined]
        verification_hash=proof.verification_hash,  # type: ignore[attr-defined]
        simulated=proof.simulated,  # type: ignore[attr-defined]
        verified_at=datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


class MediaLifecycleWorker:
    """Computes readiness, dispatches transcodes, verifies archival."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        settings: MediaLifecycleWorkerSettings,
        transcode_executor: TranscodeExecutor | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._transcode_executor = transcode_executor or FfmpegTranscodeExecutor()

    def run_forever(
        self,
        *,
        poll_seconds: float = 300.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        while stop_event is None or not stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                _LOG.exception("Media lifecycle scan failed; retrying on the next poll interval.")
            if stop_event is not None:
                stop_event.wait(poll_seconds)
            else:
                time.sleep(poll_seconds)

    def _audit(
        self, session: Session, *, asset_id: str | None, action: str, detail: str, dry_run: bool
    ) -> None:
        session.add(
            MediaLifecycleAuditEntry(
                asset_id=asset_id, action=action, detail=detail, dry_run=dry_run
            )
        )

    def run_once(
        self, *, now: datetime | None = None, dry_run: bool | None = None
    ) -> MediaLifecycleScanResult:
        """One scan pass. ``dry_run`` overrides the settings default when given."""

        resolved_now = now or datetime.now(UTC)
        effective_dry_run = self._settings.dry_run if dry_run is None else dry_run

        readiness_recomputed = 0
        readiness_changed = 0
        transcode_jobs_seeded = 0
        archive_verified = 0

        with self._session_factory() as session:
            assets = list(session.execute(select(Asset).order_by(Asset.asset_id.asc())).scalars())
            for asset in assets:
                changed, seeded, is_archive_complete = self._recompute_one(
                    session, asset, now=resolved_now, dry_run=effective_dry_run
                )
                readiness_recomputed += 1
                if changed:
                    readiness_changed += 1
                if is_archive_complete:
                    archive_verified += 1
                transcode_jobs_seeded += seeded
            self._audit(
                session,
                asset_id=None,
                action="readiness_scan_complete",
                detail=f"Recomputed readiness for {readiness_recomputed} asset(s); "
                f"{readiness_changed} changed.",
                dry_run=effective_dry_run,
            )
            # Always commit: in dry-run mode ``_recompute_one`` never adds
            # anything to the session except audit-log rows (every
            # AssetReadiness/TranscodeJob mutation is individually guarded
            # on ``not dry_run``), so there is nothing here a rollback would
            # need to protect -- and rolling back would also discard the
            # audit trail dry-run mode exists to produce.
            session.commit()

        dispatched, completed, failed = self._dispatch_pending_transcodes(dry_run=effective_dry_run)

        missing = self.list_missing_media(now=resolved_now)
        with self._session_factory() as session:
            self._audit(
                session,
                asset_id=None,
                action="missing_media_scan_complete",
                detail=f"{len(missing)} scheduled item(s) missing ready media within "
                f"{self._settings.missing_media_horizon_days} day(s).",
                dry_run=effective_dry_run,
            )
            session.commit()

        return MediaLifecycleScanResult(
            readiness_recomputed=readiness_recomputed,
            readiness_changed=readiness_changed,
            transcode_jobs_seeded=transcode_jobs_seeded,
            transcode_jobs_dispatched=dispatched,
            transcode_jobs_completed=completed,
            transcode_jobs_failed=failed,
            archive_verified=archive_verified,
            missing_media_count=len(missing),
            dry_run=effective_dry_run,
        )

    def _recompute_one(
        self, session: Session, asset: Asset, *, now: datetime, dry_run: bool
    ) -> tuple[bool, int, bool]:
        """Recompute one asset's :class:`AssetReadiness` row.

        Returns ``(changed, transcode_jobs_seeded, archive_complete)``. In
        dry-run mode nothing is written except the per-asset audit entry
        (tagged ``dry_run=True``) describing what would have changed.
        """

        row = session.get(AssetReadiness, asset.asset_id)
        before = (
            (row.readiness_state, row.loudness_status, row.archive_complete)
            if row is not None
            else None
        )

        seeded = 0
        readiness_state = READINESS_NOT_READY
        readiness_reason: str | None = None

        if asset.state == ASSET_STATE_REJECTED:
            readiness_state = READINESS_REJECTED
            readiness_reason = "Ingest validation rejected this file (unsupported codec/container)."
        elif asset.file_status == FILE_STATUS_MISSING:
            readiness_state = READINESS_MISSING_FILE
            readiness_reason = "Backing file not found on disk; relink or replace the source."
        elif asset.state in (ASSET_STATE_VALIDATED, ASSET_STATE_RECORDED):
            in_flight = (
                session.execute(
                    select(TranscodeJob).where(
                        TranscodeJob.asset_id == asset.asset_id,
                        TranscodeJob.status.in_((JOB_STATUS_PENDING, JOB_STATUS_RUNNING)),
                    )
                )
                .scalars()
                .all()
            )
            existing_any = session.execute(
                select(TranscodeJob.job_id).where(TranscodeJob.asset_id == asset.asset_id).limit(1)
            ).first()
            # transcode_seeding_enabled (ADR 0007 amendment): the station's
            # own off switch for ingest-time transcode. Disabled means "no
            # proxy will ever be generated by this worker" -- the asset
            # still reaches READINESS_READY below (no in-flight jobs, none
            # will be seeded), same honest outcome as the no-file_path case
            # a few lines up: nothing to wait on is not the same as failed.
            can_seed = (
                existing_any is None
                and asset.file_path
                and self._settings.transcode_seeding_enabled
            )
            if can_seed and not dry_run:
                for fmt in self._settings.transcode_formats:
                    session.add(TranscodeJob(asset_id=asset.asset_id, output_format=fmt))
                    seeded += 1
                session.flush()
                in_flight = (
                    session.execute(
                        select(TranscodeJob).where(
                            TranscodeJob.asset_id == asset.asset_id,
                            TranscodeJob.status.in_((JOB_STATUS_PENDING, JOB_STATUS_RUNNING)),
                        )
                    )
                    .scalars()
                    .all()
                )
            elif can_seed and dry_run:
                seeded = len(self._settings.transcode_formats)

            if in_flight or (existing_any is None and seeded):
                readiness_state = (
                    READINESS_TRANSCODING
                    if any(j.status == JOB_STATUS_RUNNING for j in in_flight)
                    else READINESS_PENDING_TRANSCODE
                )
            else:
                readiness_state = READINESS_READY
        # else: pending_ingest / ingesting -> stays READINESS_NOT_READY

        loudness_status = row.loudness_status if row is not None else None
        measured_lufs = row.measured_lufs if row is not None else None
        if (
            loudness_status in (None, LOUDNESS_NOT_CHECKED)
            and asset.file_path
            and Path(asset.file_path).is_file()
            and not dry_run
        ):
            loudness_status, measured_lufs = self._check_loudness(asset)

        portal_verified = asset.manifest_url is not None and asset.published_at is not None
        proofs = (
            session.execute(
                select(AssetArchiveProof).where(
                    AssetArchiveProof.asset_id == asset.asset_id,
                    AssetArchiveProof.simulated.is_(False),
                )
            )
            .scalars()
            .all()
        )
        ia_verified = any(p.target_type == ARCHIVE_TARGET_INTERNET_ARCHIVE for p in proofs)
        nas_verified = any(p.target_type in _NAS_TARGET_TYPES for p in proofs)
        archive_complete = portal_verified and ia_verified and nas_verified

        after = (readiness_state, loudness_status, archive_complete)
        changed = before != after

        detail = (
            f"state={readiness_state} loudness={loudness_status} "
            f"archive_complete={archive_complete} (portal={portal_verified} "
            f"ia={ia_verified} nas={nas_verified})"
        )
        self._audit(
            session,
            asset_id=asset.asset_id,
            action="readiness_recomputed" if not dry_run else "readiness_would_recompute",
            detail=detail,
            dry_run=dry_run,
        )

        if dry_run:
            return changed, seeded, archive_complete

        if row is None:
            row = AssetReadiness(asset_id=asset.asset_id)
            session.add(row)
        row.readiness_state = readiness_state
        row.readiness_reason = readiness_reason
        row.loudness_status = loudness_status
        row.measured_lufs = measured_lufs
        row.archive_portal_verified_at = now if portal_verified else None
        row.archive_ia_verified_at = now if ia_verified else None
        row.archive_nas_verified_at = now if nas_verified else None
        row.archive_complete = archive_complete
        row.updated_at = now
        return changed, seeded, archive_complete

    def _check_loudness(self, asset: Asset) -> tuple[str, float | None]:
        from civiccast.stream.loudness import check_loudness

        assert asset.file_path is not None
        result = check_loudness(
            media_path=Path(asset.file_path),
            target_lufs=_INGEST_LOUDNESS_TARGET_LUFS,
            tolerance_lufs=_INGEST_LOUDNESS_TOLERANCE_LUFS,
        )
        if result.status == "failed":
            return LOUDNESS_FAILED, result.measured_lufs
        return LOUDNESS_OK, result.measured_lufs

    def _dispatch_pending_transcodes(self, *, dry_run: bool) -> tuple[int, int, int]:
        dispatched = completed = failed = 0
        with self._session_factory() as session:
            pending = list(
                session.execute(
                    select(TranscodeJob)
                    .where(TranscodeJob.status == JOB_STATUS_PENDING)
                    .order_by(TranscodeJob.created_at.asc())
                    .limit(self._settings.max_transcode_dispatch_per_pass)
                ).scalars()
            )
            if dry_run:
                for job in pending:
                    self._audit(
                        session,
                        asset_id=job.asset_id,
                        action="transcode_would_dispatch",
                        detail=f"Would dispatch {job.output_format} for job {job.job_id}.",
                        dry_run=True,
                    )
                session.commit()
                return 0, 0, 0

            if self._settings.transcode_output_root:
                output_root = Path(self._settings.transcode_output_root)
            elif os.environ.get("CIVICCAST_SUPERVISED") == "1":
                # The installed runtime is read-only to the service account;
                # its existing writable station data tree owns generated media.
                output_root = default_civiccast_data_root() / "data" / "transcodes"
            else:
                output_root = Path.cwd() / "civiccast-data" / "transcodes"
            for job in pending:
                asset = session.get(Asset, job.asset_id)
                if asset is None:
                    job.status = JOB_STATUS_FAILED
                    job.error_detail = "Backing asset row no longer exists."
                    job.completed_at = datetime.now(UTC)
                    failed += 1
                    continue
                job.status = JOB_STATUS_RUNNING
                job.started_at = datetime.now(UTC)
                session.flush()
                dispatched += 1
                self._audit(
                    session,
                    asset_id=job.asset_id,
                    action="transcode_dispatched",
                    detail=f"Dispatched {job.output_format} for job {job.job_id}.",
                    dry_run=False,
                )
                result = self._transcode_executor.run(
                    asset=asset, output_format=job.output_format, output_dir=output_root
                )
                job.completed_at = datetime.now(UTC)
                if result.success:
                    job.status = JOB_STATUS_COMPLETED
                    job.progress_percent = 100
                    job.output_path = result.output_path
                    job.file_size_bytes = result.file_size_bytes
                    completed += 1
                    self._audit(
                        session,
                        asset_id=job.asset_id,
                        action="transcode_completed",
                        detail=f"{job.output_format} -> {result.output_path}",
                        dry_run=False,
                    )
                else:
                    job.status = JOB_STATUS_FAILED
                    job.error_detail = result.error_detail
                    failed += 1
                    self._audit(
                        session,
                        asset_id=job.asset_id,
                        action="transcode_failed",
                        detail=result.error_detail or "unknown error",
                        dry_run=False,
                    )
            session.commit()
        return dispatched, completed, failed

    def list_missing_media(self, *, now: datetime | None = None) -> list[dict[str, object]]:
        """Live join: scheduled items in the horizon whose asset isn't ready.

        Not persisted -- see the module docstring for why a durable flag
        table would be actively misleading here.
        """

        from civiccast.schedule.models import ScheduleItem

        resolved_now = now or datetime.now(UTC)
        horizon_end = resolved_now + timedelta(days=self._settings.missing_media_horizon_days)
        rows: list[dict[str, object]] = []
        with self._session_factory() as session:
            items = session.execute(
                select(ScheduleItem).where(
                    ScheduleItem.state == "scheduled",
                    ScheduleItem.scheduled_at >= resolved_now,
                    ScheduleItem.scheduled_at <= horizon_end,
                )
            ).scalars()
            for item in items:
                asset = session.get(Asset, item.asset_id)
                if asset is None:
                    reason = "Referenced asset no longer exists."
                elif asset.state not in (ASSET_STATE_VALIDATED, ASSET_STATE_RECORDED):
                    reason = f"Asset is in state '{asset.state}', not validated/recorded."
                elif asset.file_status == FILE_STATUS_MISSING:
                    reason = "Asset's backing file is missing."
                else:
                    continue
                rows.append(
                    {
                        "schedule_id": str(item.id),
                        "asset_id": item.asset_id,
                        "asset_title": asset.title if asset is not None else item.asset_id,
                        "channel_id": item.channel_id,
                        "scheduled_start": item.scheduled_at,
                        "asset_state": asset.state if asset is not None else "unknown",
                        "reason": reason,
                    }
                )
        return rows
