# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Production adapters for scheduled recording.

The service layer owns the schedule/job state machine. This module owns the
runtime edges that were deliberately protocol-shaped there: ffmpeg capture,
asset-row finalization, S8 alert ingest, and the lifespan-supervised tick loop.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from civiccast.alerting.models import AlertConditionKind
from civiccast.alerting.store import record_alert_condition
from civiccast.live.models import RecordingTarget
from civiccast.live.recording_paths import (
    REHEARSAL_RECORDING_TARGET_ID,
    local_recording_path,
)
from civiccast.recording.models import RecordingSource
from civiccast.recording.service import (
    CaptureResult,
    DropoutCheckResult,
    RecordingDrainResult,
    RecordingService,
    TickCounters,
)
from civiccast.schedule.ingest import run_ffprobe, validate_ingest
from civiccast.schedule.models import ASSET_STATE_RECORDED, Asset
from civiccast.stream._ffmpeg import (
    FfmpegProcessHandle,
    probe_media_duration_seconds,
    run_ffmpeg,
    start_ffmpeg,
)
from civiccast.vod.store import AssetAlreadyExistsError

if TYPE_CHECKING:
    import threading

    from sqlalchemy.orm import Session

SessionFactory = Callable[[], AbstractContextManager["Session"]]

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScheduledRecordingSettings:
    """Runtime knobs for production scheduled recording."""

    mode: str = "inline"
    poll_seconds: float = 10.0
    tick_horizon_seconds: int = 600
    capture_subdir: str = "scheduled-recordings"

    @classmethod
    def from_env(cls) -> ScheduledRecordingSettings:
        mode = os.environ.get("CIVICCAST_SCHEDULED_RECORDING", "inline").strip().lower()
        if mode not in {"inline", "off"}:
            raise ValueError("CIVICCAST_SCHEDULED_RECORDING must be 'inline' or 'off'.")
        return cls(
            mode=mode,
            poll_seconds=float(os.environ.get("CIVICCAST_SCHEDULED_RECORDING_POLL_SECONDS", "10")),
            tick_horizon_seconds=int(
                os.environ.get("CIVICCAST_SCHEDULED_RECORDING_HORIZON_SECONDS", "600")
            ),
            capture_subdir=os.environ.get(
                "CIVICCAST_SCHEDULED_RECORDING_CAPTURE_SUBDIR", "scheduled-recordings"
            ),
        )


@dataclass
class _ActiveCapture:
    """State for one job's in-progress ffmpeg capture (item 6: the fields
    beyond ``handle``/``output_path`` exist so ``check_dropout`` can relaunch
    an equivalent ffmpeg process against a new segment on the same source)."""

    handle: FfmpegProcessHandle | None
    output_path: Path
    source: RecordingSource
    encoder_profile: str
    loudness_regime: str
    capture_dir: Path
    # Prior segment files from earlier reconnects, oldest first. ``output_path``
    # is always the CURRENT (not-yet-closed) segment; finalize/stop concatenate
    # ``segments + [output_path]`` into one delivered capture.
    segments: list[Path] = field(default_factory=list)
    # Dropout-detection bookkeeping (stall = no file growth across a poll).
    last_known_size: int = 0
    reconnect_attempts: int = 0
    # CRITICAL fix: set once ``reconnect_attempts`` hits the cap and no
    # further reconnect will be attempted. Guards ``check_dropout`` against
    # re-processing the same dead segment on every subsequent scheduler poll
    # (~10s cadence) for the rest of the recording window — see that
    # method's docstring.
    exhausted: bool = False
    capture_deadline_monotonic: float | None = None
    capture_deadline_wall: datetime | None = None


# Item 6: a source is considered dropped if the output file hasn't grown
# across this many consecutive dropout-check polls (each poll runs on the
# scheduler tick cadence — default 10s, see ScheduledRecordingSettings). A
# single poll's worth of tolerance would false-positive on normal encoder
# buffering; two straight stalled polls is a real dropout, not jitter.
# ponytail: fixed threshold, not per-source-kind tuned; revisit if a real
# deployment's poll interval + encoder buffering combination false-positives.
_STALL_POLLS_BEFORE_DROPOUT = 2

# Cap on automatic reconnect attempts per job before check_dropout stops
# trying and reports an unreconnected dropout. Bounds a source that never
# comes back from hot-looping ffmpeg relaunches for the rest of the window.
_MAX_RECONNECT_ATTEMPTS = 20

# MAJOR fix: without an explicit network I/O timeout, ffmpeg's own connect
# attempt against an unreachable RTSP/SRT/HLS/RTMP/MPEG-TS source blocks for
# whatever the OS TCP stack decides (commonly minutes on Windows) before
# ffmpeg itself ever reports failure — per attempt, and every scheduled
# `record_now`/arm retries the same unreachable source. 15s bounds that to a
# single scheduler-tick-scale wait, in line with the ~10s poll cadence
# elsewhere in this module. Expressed in microseconds, the unit every one of
# the flags below takes.
_NETWORK_IO_TIMEOUT_MICROSECONDS = 15_000_000


class FfmpegScheduledCapturePipeline:
    """Capture network and configured device sources to a local MPEG-TS file.

    Item 6 (recording/ingest hardening): ``check_dropout`` detects a
    mid-recording source dropout (the ffmpeg child exited, or the output
    file stopped growing) and attempts an inline reconnect — a fresh ffmpeg
    process against the same source, writing a new segment. Segments are
    concatenated (lossless, ``-c copy``) into one delivered file at
    finalize/stop, so a dropout mid-recording produces one asset with a
    gap rather than a truncated capture or a lost job.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        settings: ScheduledRecordingSettings | None = None,
        ffmpeg_starter: Callable[..., FfmpegProcessHandle] = start_ffmpeg,
        ffmpeg_runner: Callable[..., Any] = run_ffmpeg,
        hardware_input_args_resolver: Callable[[RecordingSource], list[str] | None] | None = None,
        stall_polls_before_dropout: int = _STALL_POLLS_BEFORE_DROPOUT,
        max_reconnect_attempts: int = _MAX_RECONNECT_ATTEMPTS,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings or ScheduledRecordingSettings.from_env()
        self._ffmpeg_starter = ffmpeg_starter
        self._ffmpeg_runner = ffmpeg_runner
        self._hardware_input_args_resolver = hardware_input_args_resolver
        self._stall_polls_before_dropout = stall_polls_before_dropout
        self._max_reconnect_attempts = max_reconnect_attempts
        self._active: dict[str, _ActiveCapture] = {}
        # Consecutive stalled polls per job, reset to 0 the moment the file
        # grows again. Separate from ``_active`` bookkeeping because a job
        # can be popped from ``_active`` (finalize/stop) while a stall streak
        # is mid-count; we don't want stale streaks read back into a later,
        # unrelated job that happens to reuse the id (won't happen in
        # practice — job ids are timestamped — but keeping the dict scoped
        # to only currently-active jobs, cleared in stop(), avoids the class).
        self._stall_streak: dict[str, int] = {}
        # Guards ``_active``/``_stall_streak`` dict membership only (fast,
        # never held across ffmpeg I/O).
        self._lock = threading.Lock()
        # ponytail: a per-job lock dict sounded right but its own lifecycle
        # (create-on-demand, delete-on-teardown) was the bug: deleting a
        # job's lock entry right after release opened a gap where a third
        # caller for the SAME job_id could fetch a brand-new Lock while an
        # earlier caller still held/awaited the old one, so two "serialized"
        # critical sections ran concurrently anyway (the exact class item 6
        # exists to close). A single instance-level lock serializing
        # check_dropout/stop/stop_arming across ALL jobs has no such gap —
        # correct by construction — and a dropout check or a stop takes
        # milliseconds, so unrelated jobs briefly queuing behind each other
        # is not a real throughput cost for a station-in-a-box handful of
        # concurrent recordings. Shard per-job (with a lock registry that's
        # never deleted mid-life) only if concurrent-recording throughput
        # ever actually becomes a bottleneck.
        self._job_lock = threading.Lock()

    def arm(
        self,
        *,
        job_id: str,
        source: RecordingSource,
        encoder_profile: str,
        loudness_regime: str,
    ) -> None:
        with self._lock:
            if job_id in self._active:
                return
            capture_dir = self._capture_root() / self._settings.capture_subdir
            capture_dir.mkdir(parents=True, exist_ok=True)
            output_path = self._segment_path(capture_dir, job_id, segment=0)
            output_path.unlink(missing_ok=True)
            self._active[job_id] = _ActiveCapture(
                handle=None,
                output_path=output_path,
                source=source,
                encoder_profile=encoder_profile,
                loudness_regime=loudness_regime,
                capture_dir=capture_dir,
            )
            self._stall_streak[job_id] = 0

    def start(self, job_id: str, *, duration_seconds: float | None = None) -> None:
        with self._job_lock:
            with self._lock:
                active = self._require_active(job_id)
                if active.handle is not None:
                    if active.handle.poll() is None:
                        return
                    raise RuntimeError(
                        f"ffmpeg exited before recording started for job {job_id!r}."
                    )
            if duration_seconds is not None and duration_seconds <= 0:
                raise RuntimeError(
                    f"Recording window elapsed before capture started for job {job_id!r}."
                )
            deadline_wall = (
                datetime.now(UTC) + timedelta(seconds=duration_seconds)
                if duration_seconds is not None
                else None
            )
            try:
                handle = self._launch(
                    job_id=job_id,
                    source=active.source,
                    encoder_profile=active.encoder_profile,
                    loudness_regime=active.loudness_regime,
                    capture_dir=active.capture_dir,
                    output_path=active.output_path,
                    duration_seconds=duration_seconds,
                )
            except Exception:
                with self._lock:
                    self._active.pop(job_id, None)
                    self._stall_streak.pop(job_id, None)
                raise
            with self._lock:
                active.handle = handle
                active.capture_deadline_monotonic = (
                    time.monotonic() + duration_seconds if duration_seconds is not None else None
                )
                active.capture_deadline_wall = deadline_wall
            if handle.poll() is not None:
                with self._lock:
                    self._active.pop(job_id, None)
                    self._stall_streak.pop(job_id, None)
                handle.terminate(grace_seconds=1.0)
                raise RuntimeError(f"ffmpeg exited before recording started for job {job_id!r}.")

    def recorded_position_seconds(self, job_id: str) -> float:
        """Sample written media, including reconnect segments, with a five-second budget."""
        with self._job_lock:
            with self._lock:
                active = self._require_active(job_id)
                if active.handle is None or active.handle.poll() is not None:
                    raise RuntimeError("Recording is not currently writing media.")
                paths = [*active.segments, active.output_path]
            deadline = time.monotonic() + 5.0
            total = 0.0
            for path in paths:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        "Recording time is not available yet; retry or enter a reviewed time."
                    )
                duration = probe_media_duration_seconds(path, timeout_seconds=remaining)
                if duration is None or not math.isfinite(duration) or duration <= 0:
                    raise RuntimeError(
                        "Recording time is not available yet; retry or enter a reviewed time."
                    )
                total += duration
            return total

    def finalize(self, job_id: str) -> CaptureResult:
        return self.stop(job_id)

    def stop(self, job_id: str) -> CaptureResult:
        # _job_lock guards only the bounded in-memory state transition: pop
        # the job out of _active (so no concurrent check_dropout/stop/
        # stop_arming for this job_id can find or touch it again — see
        # check_dropout's own active.get() no-op) and take a private
        # snapshot of what the merge needs. Once popped, this job is ours
        # alone; the unbounded ffmpeg concat merge below runs OUTSIDE the
        # lock so a hung/slow merge for THIS job can never block stop()/
        # check_dropout() for every other job on the box (see _ffmpeg.py's
        # run_ffmpeg timeout for the other half of this fix).
        with self._job_lock:
            with self._lock:
                active = self._require_active(job_id)
                self._active.pop(job_id, None)
                self._stall_streak.pop(job_id, None)
            if active.handle is None:
                raise RuntimeError(f"Recording capture was not started for job {job_id!r}.")
            active.handle.terminate(grace_seconds=10.0)
            capture_ended_at = datetime.now(UTC)
            if active.capture_deadline_wall is not None:
                capture_ended_at = min(capture_ended_at, active.capture_deadline_wall)
        capture_path = self._finalize_segments(job_id, active)
        size = capture_path.stat().st_size
        return CaptureResult(
            bytes_written=size,
            capture_path=str(capture_path),
            sha256=_sha256_file(capture_path),
            capture_ended_at=capture_ended_at,
        )

    def stop_arming(self, job_id: str) -> None:
        with self._job_lock:
            with self._lock:
                active = self._active.pop(job_id, None)
                self._stall_streak.pop(job_id, None)
            if active is None:
                return
            if active.handle is not None:
                active.handle.terminate(grace_seconds=5.0)

    def check_dropout(self, job_id: str) -> DropoutCheckResult:
        """Item 6: poll one job's live capture for a source dropout.

        Detects a dropout via either signal:

        * the ffmpeg child has exited (the source hung up / the process
          crashed) — detected immediately, no stall wait needed.
        * the output file has stopped growing across
          ``stall_polls_before_dropout`` consecutive polls (the process is
          alive but the source stopped delivering frames — e.g. a stalled
          RTSP/SRT connection that never sends a clean EOF).

        On detection, closes the current segment and launches a fresh
        ffmpeg process against the same source as a new segment (the
        "reconnect"). Capped by ``max_reconnect_attempts`` per job so a
        source that never comes back doesn't hot-loop ffmpeg for the rest
        of the recording window.

        CRITICAL fix: once the reconnect cap is exhausted, this method must
        stop mutating ``active.segments``/``active.output_path`` entirely.
        The previous version appended ``active.output_path`` to
        ``active.segments`` unconditionally, before checking the cap; once
        exhausted it returned without relaunching, so every following poll
        (same scheduler cadence, ~10s) re-detected the same dead process and
        appended the SAME already-dead segment again -- unbounded duplicate
        entries that ``_finalize_segments`` would later concatenate into a
        looped/wrong-duration asset. The dead segment is now appended only
        on the branch that actually rolls to a new ``output_path`` (a
        successful reconnect); the cap-exhausted branch leaves ``segments``
        untouched so ``_finalize_segments``'s ``[*segments, output_path]``
        still includes the one real dead segment exactly once. The
        ``exhausted`` flag additionally short-circuits every following poll
        so a job whose source will never come back stops re-running the
        detection work (and re-emitting the dropout alert) for the
        remainder of the recording window.
        """
        # Held for the whole method: check_dropout (scheduler tick thread)
        # and stop/stop_arming (operator HTTP thread) must never interleave
        # on the same job's _ActiveCapture — see _job_lock's comment. A
        # concurrent stop() blocks here until we return (or, if stop()
        # already popped the job, active.get() below simply returns None
        # and we no-op — no stale object is ever touched).
        with self._job_lock:
            with self._lock:
                active = self._active.get(job_id)
            if active is None:
                return DropoutCheckResult(dropout_detected=False)
            if active.exhausted:
                # The reconnect cap was already hit on a prior poll — the
                # one real dead segment was recorded then; never touch
                # segments/output_path again for this job.
                return DropoutCheckResult(dropout_detected=False)

            if (
                active.capture_deadline_monotonic is not None
                and time.monotonic() >= active.capture_deadline_monotonic
            ):
                active.exhausted = True
                return DropoutCheckResult(dropout_detected=False)
            if active.handle is None:
                return DropoutCheckResult(dropout_detected=False)

            exited = active.handle.poll() is not None
            current_size = active.output_path.stat().st_size if active.output_path.exists() else 0
            if not exited:
                if current_size > active.last_known_size:
                    active.last_known_size = current_size
                    self._stall_streak[job_id] = 0
                    return DropoutCheckResult(dropout_detected=False)
                streak = self._stall_streak.get(job_id, 0) + 1
                self._stall_streak[job_id] = streak
                if streak < self._stall_polls_before_dropout:
                    return DropoutCheckResult(dropout_detected=False)
                detail = (
                    f"Source stalled: no output growth across {streak} consecutive polls "
                    f"(job {job_id!r})."
                )
            else:
                detail = f"ffmpeg child exited unexpectedly mid-recording (job {job_id!r})."

            # Dropout confirmed — close out the dead process.
            active.handle.terminate(grace_seconds=5.0)
            self._stall_streak[job_id] = 0
            if active.reconnect_attempts >= self._max_reconnect_attempts:
                # No reconnect will be attempted: leave active.segments and
                # active.output_path exactly as they are (the still-current
                # output_path IS the one real dead segment; it reaches
                # _finalize_segments via its own `[*segments, output_path]`
                # construction). Mark exhausted so no later poll repeats
                # any of this work for this job.
                active.exhausted = True
                return DropoutCheckResult(
                    dropout_detected=True,
                    reconnected=False,
                    detail=f"{detail} Reconnect attempt cap ({self._max_reconnect_attempts}) reached.",
                )
            # Attempting a reconnect. Do NOT mutate active.segments or
            # active.reconnect_attempts until we know the outcome — a
            # relaunch attempt that raises (ffmpeg-not-found, arg error)
            # must leave active.output_path/segments exactly as they were,
            # same reasoning as the cap-exhausted branch above, otherwise a
            # persistently-failing launch would re-append the same dead
            # segment on every poll until the cap was reached.
            next_output = self._segment_path(
                active.capture_dir, job_id, segment=len(active.segments) + 1
            )
            next_output.unlink(missing_ok=True)
            remaining_seconds = (
                max(0.0, active.capture_deadline_monotonic - time.monotonic())
                if active.capture_deadline_monotonic is not None
                else None
            )
            if remaining_seconds is not None and remaining_seconds <= 0:
                active.exhausted = True
                return DropoutCheckResult(
                    dropout_detected=True,
                    reconnected=False,
                    detail=f"{detail} The scheduled recording window has ended.",
                )
            try:
                new_handle = self._launch(
                    job_id=job_id,
                    source=active.source,
                    encoder_profile=active.encoder_profile,
                    loudness_regime=active.loudness_regime,
                    capture_dir=active.capture_dir,
                    output_path=next_output,
                    segment=len(active.segments) + 1,
                    duration_seconds=remaining_seconds,
                )
            except Exception as exc:  # ffmpeg-not-found, arg error, etc.
                # A relaunch that can't even start is not something a 10s
                # retry is likely to fix — treat it the same as cap
                # exhaustion: stop here, leave segments/output_path
                # untouched (the still-current output_path is the one real
                # dead segment and reaches _finalize_segments via its own
                # `[*segments, output_path]`), and never revisit this job.
                active.exhausted = True
                return DropoutCheckResult(
                    dropout_detected=True,
                    reconnected=False,
                    detail=f"{detail} Reconnect failed: {exc}",
                )
            # Reconnect succeeded: the now-dead output_path becomes a closed
            # segment, and the fresh output_path takes over as the current one.
            active.segments.append(active.output_path)
            active.reconnect_attempts += 1
            active.handle = new_handle
            active.output_path = next_output
            active.last_known_size = 0
            # Read active.reconnect_attempts and build the result INSIDE
            # the lock — outside it, a concurrent stop() could already
            # have torn active down.
            return DropoutCheckResult(
                dropout_detected=True,
                reconnected=True,
                detail=(
                    f"{detail} Reconnected (attempt {active.reconnect_attempts}/"
                    f"{self._max_reconnect_attempts}); recording continues as a new segment."
                ),
            )

    def _require_active(self, job_id: str) -> _ActiveCapture:
        active = self._active.get(job_id)
        if active is None:
            raise RuntimeError(f"No active ffmpeg capture exists for job {job_id!r}.")
        return active

    def _finalize_segments(self, job_id: str, active: _ActiveCapture) -> Path:
        """Return the one delivered capture file for ``job_id``.

        No reconnect ever happened: the single segment (unchanged
        behavior/filename from before item 6) IS the capture. A reconnect
        happened: concatenate every closed segment plus the current one
        (lossless ``-c copy`` via ffmpeg's concat demuxer) into a merged
        file so finalize/stop always hand the service one path.
        """
        segments = [*active.segments, active.output_path]
        non_empty = [s for s in segments if s.exists() and s.stat().st_size > 0]
        if not non_empty:
            raise RuntimeError(
                f"ffmpeg created only zero-byte recording segments for job {job_id!r}."
            )
        if len(non_empty) == 1:
            return non_empty[0]
        merged_path = active.capture_dir / f"{_safe_filename(job_id)}-merged.ts"
        concat_list_path = active.capture_dir / f"{_safe_filename(job_id)}-concat.txt"
        # ffmpeg's concat demuxer quotes each path in single quotes; a
        # literal single-quote inside the path must be escaped per its
        # quoting rule (close quote, escaped quote, reopen quote — the same
        # convention as POSIX shell single-quoting). job_id is sanitized via
        # _safe_filename, but segment paths also carry capture_dir
        # (server-configured, from RecordingTarget.target_uri), so escape
        # unconditionally rather than assuming it's clean.
        concat_list_path.write_text(
            "".join(f"file '{_ffmpeg_concat_quote(segment)}'\n" for segment in non_empty),
            encoding="utf-8",
        )
        result = self._ffmpeg_runner(
            [
                "-hide_banner",
                "-nostdin",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list_path),
                "-c",
                "copy",
                str(merged_path),
            ]
        )
        if result.returncode != 0 or not merged_path.exists() or merged_path.stat().st_size <= 0:
            raise RuntimeError(
                f"Failed to merge {len(non_empty)} reconnect segments for job {job_id!r}: "
                f"{result.stderr[-500:]}"
            )
        return merged_path

    def _segment_path(self, capture_dir: Path, job_id: str, *, segment: int) -> Path:
        suffix = "" if segment == 0 else f"-seg{segment}"
        return capture_dir / f"{_safe_filename(job_id)}{suffix}.ts"

    def _launch(
        self,
        *,
        job_id: str,
        source: RecordingSource,
        encoder_profile: str,
        loudness_regime: str,
        capture_dir: Path,
        output_path: Path,
        segment: int = 0,
        duration_seconds: float | None = None,
    ) -> FfmpegProcessHandle:
        args = [
            "-hide_banner",
            "-nostdin",
            *self._input_args(source),
            "-map",
            "0:v?",
            "-map",
            "0:a?",
            *self._output_args(
                encoder_profile=encoder_profile,
                loudness_regime=loudness_regime,
            ),
            # Item 6 follow-up (real-capture proof, native Windows): without
            # this, ffmpeg's mpegts muxer buffers ~256 KiB of output in the
            # process's own memory before an OS-level write, and
            # FfmpegProcessHandle.terminate() maps to Win32 TerminateProcess
            # -- an unconditional kill with NO chance for ffmpeg to run its
            # normal shutdown path (unlike POSIX SIGTERM, which ffmpeg traps
            # to flush + exit cleanly). Measured on this box: an 8s capture
            # at ~200kbps never crossed the first 256 KiB flush boundary, so
            # `stop()`/`finalize()` always produced a 0-byte file --
            # scheduled recording could not produce ANY asset shorter than
            # that boundary, and any recording lost its unflushed tail
            # regardless of length. `-flush_packets 1` makes the muxer write
            # (and the OS see) each packet as it's produced, so a
            # Windows-abrupt kill loses at most the packet in flight instead
            # of up to a quarter-megabyte of the most recent capture.
            "-flush_packets",
            "1",
            *(["-t", f"{duration_seconds:.3f}"] if duration_seconds is not None else []),
            "-f",
            "mpegts",
            str(output_path),
        ]
        log_dir = capture_dir / "logs"
        log_suffix = "" if segment == 0 else f"-seg{segment}"
        return self._ffmpeg_starter(
            args,
            stdout_path=log_dir / f"{_safe_filename(job_id)}{log_suffix}.stdout.log",
            stderr_path=log_dir / f"{_safe_filename(job_id)}{log_suffix}.stderr.log",
        )

    def _capture_root(self) -> Path:
        with self._session_factory() as session:
            targets = session.execute(
                select(RecordingTarget).order_by(
                    RecordingTarget.created_at.asc(),
                    RecordingTarget.recording_target_id.asc(),
                )
            ).scalars()
            saw_target = False
            saw_rehearsal_target = False
            for target in targets:
                saw_target = True
                if target.recording_target_id == REHEARSAL_RECORDING_TARGET_ID:
                    saw_rehearsal_target = True
                    continue
                path = local_recording_path(target.target_uri)
                if path is not None:
                    return path
            if not saw_target:
                raise RuntimeError("No local recording target is configured.")
            if saw_rehearsal_target:
                raise RuntimeError(
                    "No production local recording target is configured; only the installer "
                    "rehearsal target was available."
                )
            raise RuntimeError("No usable local recording target is configured.")

    def _input_args(self, source: RecordingSource) -> list[str]:
        # MAJOR fix: an unreachable network source (dead RTSP/SRT/HLS/RTMP/
        # MPEG-TS URI) must fail fast and bounded rather than hang ffmpeg for
        # the OS's own TCP connect timeout (commonly minutes on Windows) on
        # every single arm/record_now attempt. Each protocol below takes its
        # own real, documented AVOption for this — there is no one flag that
        # covers all of them:
        #  - rtsp demuxer: `-timeout` (microseconds) is its own documented
        #    socket TCP I/O timeout AVOption.
        #  - srt protocol: `-timeout` (microseconds) is its own documented
        #    raise-error timeout for read/write/connect (distinct from the
        #    rtsp option of the same CLI name — ffmpeg resolves it per the
        #    active protocol).
        #  - hls/rtmp/mpegts have no protocol-specific timeout AVOption;
        #    they take the generic AVFormatContext `-rw_timeout`
        #    (microseconds) instead, which bounds any network read/write.
        timeout_us = str(_NETWORK_IO_TIMEOUT_MICROSECONDS)
        match source.kind:
            case "rtsp":
                return ["-rtsp_transport", "tcp", "-timeout", timeout_us, "-i", source.uri]
            case "srt":
                return ["-timeout", timeout_us, "-i", source.uri]
            case "hls" | "rtmp" | "mpegts":
                return ["-rw_timeout", timeout_us, "-i", source.uri]
            case "ndi":
                return ["-f", "libndi_newtek", "-i", source.input_id]
            case "sdi" | "hdmi":
                # The schedule stores a stable CivicCast preset id, not an
                # operator-authored FFmpeg fragment. The resolver owns the
                # platform-specific boundary (DeckLink, DirectShow, etc.) and
                # returns an argv list, so no shell or parse-launch string is
                # ever constructed from browser input.
                resolved = (
                    self._hardware_input_args_resolver(source)
                    if self._hardware_input_args_resolver is not None
                    else None
                )
                if not resolved:
                    raise RuntimeError(
                        f"Recording input preset {source.input_id!r} is not available for "
                        f"{source.kind.upper()}. Open Recording and select a detected or "
                        "configured input preset."
                    )
                return list(resolved)
            case _:
                raise RuntimeError(f"Unsupported scheduled recording source kind {source.kind!r}.")

    def _output_args(self, *, encoder_profile: str, loudness_regime: str) -> list[str]:
        audio_args = _audio_args(loudness_regime)
        match encoder_profile.strip().lower():
            case "copy" | "default" | "inherit":
                return ["-c:v", "copy", *audio_args]
            case "hw-h264-1080p" | "h264-1080p":
                return [
                    "-c:v",
                    "h264",
                    "-b:v",
                    "6000k",
                    "-maxrate",
                    "6500k",
                    "-bufsize",
                    "12000k",
                    "-vf",
                    "scale=-2:1080",
                    *audio_args,
                ]
            case "hw-h264-720p" | "h264-720p":
                return [
                    "-c:v",
                    "h264",
                    "-b:v",
                    "3500k",
                    "-maxrate",
                    "4000k",
                    "-bufsize",
                    "7000k",
                    "-vf",
                    "scale=-2:720",
                    *audio_args,
                ]
            case _:
                raise RuntimeError(
                    f"Unsupported scheduled recording encoder profile {encoder_profile!r}."
                )


class ScheduledRecordingAssetFinalizer:
    """Create a normal recorded asset row for a scheduled capture."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def finalize_to_asset(
        self,
        *,
        station_id: str,
        capture_path: str,
        target_series: str | None,
        custom_field_values: dict[str, Any],
        sha256: str | None,
    ) -> str:
        path = Path(capture_path)
        if not path.exists():
            raise RuntimeError(f"Capture file {capture_path!r} does not exist.")
        stat = path.stat()
        if stat.st_size <= 0:
            raise RuntimeError(f"Capture file {capture_path!r} is empty.")
        probe = run_ffprobe(path)
        validate_ingest(probe)
        asset_id = _asset_id_for(path, sha256)
        title = _title_for(path, target_series)
        description_bits = [f"Scheduled recording for station {station_id}."]
        if target_series:
            description_bits.append(f"Series: {target_series}.")
        if sha256:
            description_bits.append(f"SHA256: {sha256}.")
        if custom_field_values:
            description_bits.append("Custom fields captured at recording finalization.")
        row = Asset.from_upload(
            asset_id=asset_id,
            title=title,
            description=" ".join(description_bits),
            file_path=str(path),
            file_size_bytes=stat.st_size,
            state=ASSET_STATE_RECORDED,
            duration_seconds=probe.duration_seconds,
            codec_video=probe.codec_video,
            codec_audio=probe.codec_audio,
            width_px=probe.width_px,
            height_px=probe.height_px,
            bitrate_bps=probe.bitrate_bps,
            format_name=probe.format_name,
        )
        with self._session_factory() as session:
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AssetAlreadyExistsError(asset_id=asset_id) from exc
        return asset_id


class RecordingAlertSink:
    """Route scheduled-recording conditions into S8's condition hub."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def emit(
        self,
        *,
        severity: str,
        source: str,
        message: str,
        context: dict[str, Any],
    ) -> None:
        job_id = str(context.get("job_id") or context.get("schedule_id") or "scheduled-recording")
        # Item 6: a dropout is a distinct, resolvable condition kind — the job
        # may still be recording (reconnect succeeded), so it must not read
        # on the dashboard as the same hard failure as a finalize/arm crash.
        kind: AlertConditionKind = (
            "scheduled-recording-dropout"
            if source == "recording.dropout"
            else "scheduled-recording-failure"
        )
        with self._session_factory() as session:
            record_alert_condition(
                session,
                kind=kind,
                resource_ref=job_id,
                source_section="S21",
                summary=f"Scheduled recording {severity}: {source}",
                detail=message,
            )
            session.commit()


class ScheduledRecordingWorker:
    """Periodic driver for materialization, arming, starting, and finalizing."""

    def __init__(
        self,
        service: RecordingService,
        *,
        station_id: str,
        settings: ScheduledRecordingSettings | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._service = service
        self._station_id = station_id
        self._settings = settings or ScheduledRecordingSettings.from_env()
        self._clock = clock or (lambda: datetime.now(UTC))

    def tick(self) -> TickCounters:
        return self._service.tick(
            self._station_id,
            horizon=timedelta(seconds=self._settings.tick_horizon_seconds),
        )

    def drain_in_flight(self, *, deadline_seconds: float) -> RecordingDrainResult:
        """Shutdown drain for this worker's station: gracefully finalize every
        in-flight recording to a valid asset, bounded by ``deadline_seconds``.

        Thin wrapper over :meth:`RecordingService.drain_in_flight` that supplies
        this worker's ``station_id`` — the seam the app lifespan's ``finally``
        block calls (peer to the egress ``stop_all_channels`` drain) so a
        mid-recording shutdown produces a finalized asset instead of an orphan.
        """
        return self._service.drain_in_flight(self._station_id, deadline_seconds=deadline_seconds)

    def run_forever(self, *, poll_seconds: float, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                counters = self.tick()
                if any(counters.model_dump().values()):
                    _LOG.info("scheduled-recording tick: %s", counters.model_dump())
            except Exception:
                _LOG.exception("scheduled-recording worker tick failed")
            stop_event.wait(poll_seconds)


def _safe_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-") or "recording"


def _ffmpeg_concat_quote(segment: Path) -> str:
    """Escape a path for ffmpeg's concat-demuxer single-quoted ``file`` line.

    Per the demuxer's own quoting rule, a literal ``'`` inside the quoted
    value is written as ``'\\''`` (close quote, escaped quote, reopen
    quote) — the same convention as POSIX shell single-quoting.
    """
    return segment.as_posix().replace("'", "'\\''")


def _audio_args(loudness_regime: str) -> list[str]:
    match loudness_regime.strip().lower():
        case "inherit" | "copy" | "":
            return ["-c:a", "copy"]
        case "atsc-a85":
            return [
                "-af",
                "loudnorm=I=-24:TP=-2:LRA=7",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
            ]
        case "ebu-r128":
            return [
                "-af",
                "loudnorm=I=-23:TP=-2:LRA=7",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
            ]
        case "streaming":
            return [
                "-af",
                "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
            ]
        case _:
            raise RuntimeError(
                f"Unsupported scheduled recording loudness regime {loudness_regime!r}."
            )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_id_for(path: Path, sha256: str | None) -> str:
    seed = (sha256 or _sha256_file(path))[:16]
    stem = re.sub(r"[^a-z0-9-]+", "-", path.stem.lower()).strip("-")
    stem = stem[:40] if stem else "recording"
    return f"{stem}-{seed}"[:64].rstrip("-")


def _title_for(path: Path, target_series: str | None) -> str:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    if target_series:
        return f"{target_series.replace('-', ' ').title()} recording {timestamp}"
    return f"Scheduled recording {timestamp}"
