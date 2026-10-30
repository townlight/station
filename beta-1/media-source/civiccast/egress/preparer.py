# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Source preparation for canonical egress playout assets.

#156 (long-asset TRANSITIONING stall): preparation used to re-conform the
same asset from scratch at every airing, synchronously, at airtime — a long
recording could hold the channel in TRANSITIONING for minutes at top-of-hour.
Preparation now keeps a **persistent conform cache** under
``work_dir/conform-cache``:

* the cacheable unit is the **full-asset conform** (keyed by source file
  fingerprint + canonical profile + loudness config). Trim/join-in-progress
  offsets are applied at *playout* instead — the emitted segment carries
  ``inpoint``/``outpoint`` and the encoder's ffconcat plan already honors them
  (:func:`civiccast.egress.runtime.write_concat_plan`);
* a cache **hit** costs zero ffmpeg work and zero loudness probing: a program
  whose asset has aired before starts within seconds (the #156 acceptance bar);
* a trimmed cache **miss** (typically a first-ever join-in-progress start)
  conforms only the remaining portion straight to air — identical latency to
  the old behavior — and *warm-behind* conforms the full asset into the cache
  on a background thread so the next airing hits. First-ever airing latency is
  therefore unchanged and documented in the runbook rather than hidden;
* the cache is bounded (``CIVICCAST_CONFORM_CACHE_GB``, default 60; ``<= 0``
  disables caching entirely) with oldest-first eviction, hits refreshing the
  entry's clock.
"""

from __future__ import annotations

import contextlib
import hashlib
import heapq
import itertools
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from civiccast.egress.env_vars import resolve_renamed_env
from civiccast.egress.errors import SourcePrepareError
from civiccast.egress.loudness_ride import (
    LOUDNESS_WHOLE_TOL_LU,
    LOUDNESS_WINDOW_TOL_LU,
    LeveledWindow,
    LoudnessRideCancelledError,
    LoudnessRideError,
    RideParams,
    build_video_from_source_args,
    canonical_video_filter,
    level_window,
    ride_audio_path,
)
from civiccast.egress.models import (
    CanonicalProfile,
    EgressConfig,
    EgressSourcePlan,
    EgressSourceSegment,
)
from civiccast.egress.runtime import FfmpegRunner
from civiccast.stream._ffmpeg import (
    FfmpegCancelledError,
    FfmpegNotFoundError,
    FfmpegResult,
    probe_canonical_video,
    probe_has_decodable_stream,
    probe_media_duration_seconds,
    run_ffmpeg,
)
from civiccast.stream.loudness import (
    DEFAULT_LOUDNESS_STANDARD,
    LoudnessGateResult,
    check_streaming_loudness,
)

LoudnessChecker = Callable[..., LoudnessGateResult]
_LOG = logging.getLogger(__name__)
WarmScheduler = Callable[[Callable[[], None]], None]
#: The U25 ride, injected so tests (and a host without numpy) can substitute a
#: stand-in.  Keyword-only in the same shape as :func:`level_window`, which is
#: the real default.
WindowLeveler = Callable[..., LeveledWindow]

_CACHE_DIR_NAME = "conform-cache"
_DEFAULT_CACHE_GB = 60.0
_DEFAULT_PREPARATION_TIMEOUT_SECONDS = 300.0

#: BETA.10 U29: the conform-cache warm is a WHOLE-ASSET conform, but it used
#: the same 300s flat bound as a bounded foreground segment. A long meeting
#: can never finish inside it, so the warm was killed, the failure was logged
#: and the next airing re-queued it -- the same asset burned CPU forever and
#: never populated the cache. Measured on the live station (2026-09-25, three
#: station encoders running) a 9020.8s asset conforms at 16.3x realtime on one
#: thread at below-normal priority, i.e. 555s alone and 866s under contention;
#: the flat 300s bound was below even the uncontended cost. The warm bound now
#: scales with the asset, from a de-rated design speed rather than the best
#: measurement, and keeps the flat bound as a floor for short assets -- see
#: ``warm_preparation_timeout_seconds``. The on-demand/air-path timeout is
#: deliberately NOT rescaled: a segment's airing must still fail closed fast.
#:
#: 8.0x is the design speed: the measured 16.3x alone and 10.4x against a
#: concurrent foreground conform are both faster, so this is a worst case that
#: still terminates rather than a target.
_WARM_CONFORM_DESIGN_SPEED_RPS = 8.0
#: Headroom over the design-speed estimate. The warm has no interactive
#: deadline -- its only cost of over-running is a longer-lived child -- so the
#: bound is set to tolerate a machine materially slower than the one the design
#: speed was measured on.
_WARM_CONFORM_TIMEOUT_FACTOR = 1.5
#: Ceiling on the scaled bound. There is ONE warm worker thread, so a bound
#: that scales without limit would let a single pathological asset (or a
#: misreported duration) hold the warm queue for the rest of the day. This is a
#: cap on WALL CLOCK, not on asset duration: at the measured 16.3x, 2h still
#: covers an asset longer than a day. No asset the station has recorded comes
#: anywhere near it (the longest observed, 9154s, designs to ~29 min).
_WARM_CONFORM_TIMEOUT_CEILING_SECONDS = 7200.0
#: Failed warms retry on later scheduling passes after 60s, doubling up to
#: 30 minutes. Cancellation does not consume a retry; success resets the delay.
_WARM_FAILURE_BACKOFF_SECONDS = 60.0
_WARM_FAILURE_MAX_BACKOFF_SECONDS = 30 * 60.0

#: F3 fix (hostile-review follow-up, 2026-09-06): the age-only GC this replaced
#: had no size/count budget at all, so a channel doing frequent rollovers (the
#: exact scenario H5 was fixed for) could accumulate an unbounded number of
#: per-plan directories over a 6-hour window. GC is now keep-N-most-recent
#: FIRST (a directory this recent could plausibly still be the one a live
#: worker is reading, or one the daemon hasn't yet released -- see
#: ``release``), THEN a byte budget across whatever is left, and only falls
#: back to a bare age floor as an absolute last resort for anything neither
#: of those already caught. The floor is deliberately longer than H5's
#: original 6h now that keep-N is the primary defense.
_PREPARED_PLAN_DIR_KEEP_N = 3
_PREPARED_PLAN_DIR_BUDGET_GB = 5.0
_PREPARED_PLAN_DIR_MAX_AGE_S = 24.0 * 3600.0

#: Item 66 round-3 (Opus review): _evict_cache_over_budget's orphan reap.
#: Any cache-dir intermediate this old was abandoned mid-write (a crash, a
#: killed process) -- any legitimate in-flight conform or promotion finishes
#: in seconds to low minutes even for an hour-long asset. A ``.json`` with no
#: sibling ``.ts`` this old is a loudness-only probe (see
#: ``_write_cache_meta``) whose conform never landed (every attempt failed,
#: or the process was killed between the two writes).
_ORPHAN_CACHE_SCRATCH_MAX_AGE_S = 3600.0
_ORPHAN_CACHE_META_MAX_AGE_S = 24.0 * 3600.0

#: The only two names this module ever creates for a REAL cache entry:
#: ``{key}.ts`` (the conformed media) and ``{key}.json`` (its sidecar meta),
#: where ``key`` is ``sha256(...).hexdigest()[:32]`` -- see ``_cache_key``.
#: Everything else in the directory is scratch written by a conform, a
#: promotion, or the loudness ride: a ``{key}.ts.tmp``, a ``{key}.json.tmp``,
#: the ride's ``{key}.ts.tmp.ride-audio.ts`` muxed intermediate and its
#: ``civiccast-ride-*.pcm`` PCM tee (``civiccast.egress.loudness_ride``).
#: BETA.10 U53: classifying by name instead of by a ``*.ts`` glob is what
#: keeps the ride's two artifacts out of the ENTRY set -- ``.ride-audio.ts``
#: ends in ``.ts``, so the old glob fed it to oldest-first eviction, which
#: could unlink a ride's live intermediate as if it were a stale entry, and
#: neither artifact was ever reaped by the ``*.ts.tmp`` orphan sweep.
_CACHE_ENTRY_TS_RE = re.compile(r"\A[0-9a-f]{32}\.ts\Z")
_CACHE_ENTRY_META_RE = re.compile(r"\A[0-9a-f]{32}\.json\Z")

#: BETA.10 U53: how recently an intermediate must have been written to count
#: as in-flight. Every writer here (an ffmpeg conform, a promotion copy, the
#: ride) writes continuously, so an mtime this old means the writer is gone
#: or wedged and nothing is waiting on the space. Such a file stops counting
#: toward the budget immediately (the 1h floor above still decides when it is
#: deleted). Without this, one abandoned 9.0 GB partial left by a restart
#: counted for a full hour, and the next conform promotion evicted warm
#: entries -- the entries that make a preparation take 6 s instead of the
#: 100-500 s measured on the station through 17:59-18:40.
_SCRATCH_LIVENESS_S = 60.0

#: Item 66 round-3 (Opus review, point 7): an untrimmed loudness probe no
#: longer decodes the whole asset -- it samples a window instead. This is
#: the one place that sampling-window assumption is encoded; see
#: ``_prepare_segment``'s loudness-probe comment.
_UNTRIMMED_LOUDNESS_PROBE_CAP_S = 120.0

#: Item 66 round-4 (Opus review, point 2): a HEAD sample (the round-3
#: shape) can land on cold open silence/room tone and measure the silence
#: floor instead of the program's real loudness -- a real field failure
#: (-70 LUFS at the head of a 39-minute meeting recording), which would
#: then drive loudnorm's normalization target completely wrong and get
#: memoized for every other segment/airing of that asset. The sample
#: window now starts partway into the asset instead of at the very head.
_UNTRIMMED_LOUDNESS_PROBE_FRACTION = 0.4

#: Item 66 round-5 (Opus review, point 3): the offset for the SECOND
#: bounded sample, taken only when the first (40%-in) sample measures at
#: the silence floor. A different offset than the first sample so the two
#: are genuinely independent evidence (both landing on, say, the same
#: extended silence would otherwise look like agreement).
_UNTRIMMED_LOUDNESS_PROBE_FRACTION_2 = 0.7

#: A measurement at or below this integrated LUFS is indistinguishable from
#: silence/room tone for any of CivicCast's target loudness standards (the
#: least aggressive, ATSC A/85, still targets -24 LKFS +/- tolerance) --
#: treat it as an unusable sample rather than a real reading of the asset's
#: program loudness.
_LOUDNESS_SILENCE_FLOOR_LUFS = -60.0

#: Item 66 round-7 (Opus review, point 2, MEDIUM fix): a single HEAD sample
#: is only genuinely conclusive proof of silence when its own window
#: (``min(duration, _UNTRIMMED_LOUDNESS_PROBE_CAP_S)`` from the very start)
#: covers the WHOLE asset -- i.e. ``duration <= _UNTRIMMED_LOUDNESS_PROBE_CAP_S``
#: itself. Round-6 set this to ``_UNTRIMMED_LOUDNESS_PROBE_CAP_S * 2`` (240s)
#: instead, reasoning only about when a genuinely independent SECOND sample
#: could fit -- but that let a 120-240s asset's single 120s HEAD sample (at
#: most half the file) get trusted as "the whole (or nearly the whole)
#: asset" when it was really only up to half of it: a real field failure
#: (a 200s clip with audio starting at 130s, entirely past the sampled
#: window, memoized as -70 LUFS silence). Below/at this duration the one
#: sample truly is (nearly) the whole file, so it needs no corroboration;
#: above it, the "long asset" branch below is used instead -- its first
#: sample already sits at 40% in (never the head) and, on a floor reading,
#: corroborates with a second bounded sample before trusting silence, which
#: is what protects the 120-240s range now.
_SHORT_ASSET_SINGLE_SAMPLE_MAX_S = _UNTRIMMED_LOUDNESS_PROBE_CAP_S

#: Item 66 round-7 (point 1, HIGH): D42 (source_plan.py's ``_segment_duration``,
#: ``min(slot, playable)``) means an "untrimmed" segment (no inpoint/outpoint)
#: can carry a ``duration_seconds`` SHORTER than the asset's real media length
#: whenever the schedule slot is shorter than the asset -- a 30s slot on a
#: 67s asset produces a 30s bounded conform that is NOT the whole asset,
#: even though ``trimmed`` (inpoint/outpoint both None) is False. The same
#: tolerance ``source_plan.py``'s own ``_covers_slot`` uses for its slot/media
#: fuzz (1s) is reused here so a 29.97s asset in a 30s slot still counts as
#: "the whole asset" and gets promoted into the shared cache.
_FULL_ASSET_DURATION_TOLERANCE_S = 1.0


def _prepared_plan_dir_budget_bytes() -> float:
    raw = os.environ.get("CIVICCAST_PREPARED_PLAN_DIR_BUDGET_GB", "").strip()
    try:
        gb = float(raw) if raw else _PREPARED_PLAN_DIR_BUDGET_GB
    except ValueError:
        gb = _PREPARED_PLAN_DIR_BUDGET_GB
    return gb * 1e9


def _dir_size_bytes(directory: Path) -> int:
    total = 0
    with contextlib.suppress(OSError):
        for entry in directory.rglob("*"):
            with contextlib.suppress(OSError):
                if entry.is_file():
                    total += entry.stat().st_size
    return total


_warm_queue: queue.PriorityQueue[tuple[int, int, Callable[[], None]]] = queue.PriorityQueue()
_warm_sequence = itertools.count()
_warm_worker_lock = threading.Lock()
_warm_worker_started = False


def _warm_worker() -> None:
    """Single background worker draining ``_warm_queue`` by priority, one job at a
    time, forever -- see ``_default_warm_scheduler``. A job's own exception
    handling (``_schedule_warm``'s ``_job`` already catches and logs) should
    make this outer catch unreachable in practice; it exists purely so a
    bug in that handling can never permanently kill warming for the rest of
    the process (the worker is started exactly once -- see
    ``_warm_worker_started`` -- so a thread that dies uncaught would never
    be replaced)."""
    while True:
        _, _, job = _warm_queue.get()
        try:
            job()
        except Exception:
            _LOG.exception("Conform-cache warm job raised past its own error handling.")
        finally:
            _warm_queue.task_done()


def _default_warm_scheduler(job: Callable[[], None]) -> None:
    """Queue a cache warm onto a single background worker (production
    default).

    Item 66 (point 4, Opus review): this used to spawn one daemon thread
    PER job -- unbounded, so every distinct asset aired while a previous
    warm was still running got its own thread, all contending for CPU with
    the synchronous foreground conforms this whole feature exists to keep
    fast. Upcoming-plan jobs precede opportunistic jobs, FIFO within each priority: at most one background
    warm conform runs at a time, queued jobs simply wait. ``_schedule_warm``'s
    own per-key dedupe (``self._warming``) still prevents the same asset
    from being queued twice while its warm is pending or running.
    """
    global _warm_worker_started
    with _warm_worker_lock:
        if not _warm_worker_started:
            threading.Thread(target=_warm_worker, name="conform-cache-warm", daemon=True).start()
            _warm_worker_started = True
    _warm_queue.put((getattr(job, "_warm_priority", 10), next(_warm_sequence), job))


def _promote_queued_warm(key: str) -> None:
    """Promote queued work under the queue lock; a dequeued job is untouched."""
    with _warm_queue.mutex:
        for index, (priority, sequence, job) in enumerate(_warm_queue.queue):
            if priority > 0 and getattr(job, "_warm_key", None) == key:
                _warm_queue.queue[index] = (0, sequence, job)
                heapq.heapify(_warm_queue.queue)
                break


def _cache_budget_bytes() -> float:
    raw = os.environ.get("CIVICCAST_CONFORM_CACHE_GB", "").strip()
    try:
        gb = float(raw) if raw else _DEFAULT_CACHE_GB
    except ValueError:
        gb = _DEFAULT_CACHE_GB
    return gb * 1e9


#: BETA.10 U03: the spelling the station's service registry actually sets.
#: The station's ``Environment`` REG_MULTI_SZ (see ``env_vars``) uses two C's;
#: this reader used one, so ``=300`` was silently inert. The one-C name below
#: is still read as a legacy fallback -- see ``env_vars.resolve_renamed_env``.
PREPARATION_TIMEOUT_ENV = "CIVICCAST_EGRESS_PREPARATION_TIMEOUT_SECONDS"
LEGACY_PREPARATION_TIMEOUT_ENV = "CIVICAST_EGRESS_PREPARATION_TIMEOUT_SECONDS"

#: One-time-warning latch for the legacy/conflict messages above -- see
#: ``env_vars.resolve_renamed_env``'s ``warned`` parameter.
_RENAMED_ENV_WARNED: set[str] = set()


def preparation_timeout_seconds_from_env() -> float:
    """Return the fail-closed timeout for one source-preparation ffmpeg call.

    Preparation is background work, but an ffmpeg child must still have a
    finite lifetime so a corrupt input or stalled encoder cannot leave a
    channel in ``STARTING`` forever.  The default is generous for a bounded
    GStreamer segment and can be tuned for slower hardware without disabling
    the bound.

    BETA.10 U03: reads ``PREPARATION_TIMEOUT_ENV``, falling back to the legacy
    ``LEGACY_PREPARATION_TIMEOUT_ENV`` spelling -- see ``env_vars``. Every
    invalid or non-positive value is still logged and replaced by the default,
    whichever spelling supplied it, so a typo can never disable the bound.
    """

    resolved = resolve_renamed_env(
        name=PREPARATION_TIMEOUT_ENV,
        legacy_name=LEGACY_PREPARATION_TIMEOUT_ENV,
        logger=_LOG,
        warned=_RENAMED_ENV_WARNED,
    )
    if resolved is None:
        return _DEFAULT_PREPARATION_TIMEOUT_SECONDS
    env_name, raw = resolved
    try:
        value = float(raw)
    except ValueError:
        _LOG.warning(
            "Invalid %s=%r; using %.1fs.",
            env_name,
            raw,
            _DEFAULT_PREPARATION_TIMEOUT_SECONDS,
        )
        return _DEFAULT_PREPARATION_TIMEOUT_SECONDS
    if value <= 0:
        _LOG.warning(
            "%s must be positive; using %.1fs.",
            env_name,
            _DEFAULT_PREPARATION_TIMEOUT_SECONDS,
        )
        return _DEFAULT_PREPARATION_TIMEOUT_SECONDS
    return value


def warm_preparation_timeout_seconds(
    media_duration_seconds: float | None,
    base_timeout_seconds: float,
) -> float:
    """BETA.10 U29: the ffmpeg-call timeout a cache WARM may use for an asset.

    A conform-cache warm re-encodes the whole asset, so its cost is
    proportional to the asset's duration -- unlike the foreground path, whose
    unit of work is a bounded segment. Applying the foreground path's flat
    ``base_timeout_seconds`` to a warm made every long meeting unwarmable: the
    child was killed at the bound, the failure was logged, and the next airing
    re-queued the same asset, so CPU burned forever without the cache ever
    being populated (U29's live symptom). This scales the warm's budget with
    the asset instead, from a de-rated design speed:

        timeout = max(base, duration / design_speed * factor), capped by the
        ceiling; ``base`` alone when the duration is unknown or non-positive.

    The base is kept as a FLOOR, so this can only ever widen a warm's budget.
    It is never used for the on-demand/air path: an airing must still fail
    closed fast, and a slow warm is cheaper than a slow channel.

    ``media_duration_seconds`` is the probe-reported duration (``None`` when
    the source has not been probed or reports no duration); ``base_timeout_seconds``
    is the configured preparation timeout the foreground path uses.
    """

    if media_duration_seconds is None or media_duration_seconds <= 0:
        return base_timeout_seconds
    estimate = (
        media_duration_seconds / _WARM_CONFORM_DESIGN_SPEED_RPS * _WARM_CONFORM_TIMEOUT_FACTOR
    )
    return max(
        base_timeout_seconds,
        min(estimate, _WARM_CONFORM_TIMEOUT_CEILING_SECONDS),
    )


def _foreground_thread_cap() -> int:
    """Item 66 (point 2, Opus review, measured on HALO): conforming 300s of
    content at ``-threads 1`` took 233s vs 36.6s unthrottled -- the
    original item-66 fix's unconditional single-threaded background=False
    knob was dead on the shipped default (``playout_trim_supported=False``)
    and, once reached via ``EgressDaemon._try_content_reload``'s
    synchronous prepare on the ffmpeg-concat engine (``daemon.py`` around
    line 1839, which can run while another channel is genuinely on air),
    fully unthrottled foreground encodes would starve everything else on
    the box. Cap foreground (synchronous, blocks the caller) conforms at
    half the machine's cores instead of leaving them fully unbounded or
    fully serialized."""
    cpu_count = os.cpu_count() or 2
    return max(1, cpu_count // 2)


def _describe_segment_window(segment: EgressSourceSegment | None) -> str:
    """Name the window a loudnorm pass measured, for operator-facing log lines.

    ``"full asset"`` for the whole-file conform (``segment is None``),
    otherwise the segment's own label plus the ``-ss``/``-t`` window the probe
    and the encode both use.  Used by
    ``SourcePreparer._measure_loudnorm_metadata``'s single fallback warning.
    """
    if segment is None:
        return "full asset"
    return (
        f"{segment.label!r} in={segment.inpoint_seconds or 0.0:g}s "
        f"dur={segment.duration_seconds:g}s"
    )


def _fmt_measure(value: float | None, unit: str = "") -> str:
    """Render one measured number for an operator-facing log line.

    ``None`` is ``"unmeasurable"``, never ``0``: the ride reports a measurement
    it could not take (an unscanned peak, a window family with no measured
    block) as ``None``, and a log line that printed ``0.00`` for that would tell
    an operator the opposite of what happened.  ``unit`` is appended bare, so
    callers pass ``"LU"``/``"dBFS"`` with the spacing they want in the format
    string.
    """

    if value is None:
        return "unmeasurable"
    return f"{value:.2f}{unit}"


@dataclass(frozen=True)
class PreparedSegmentRecord:
    """Trace of one source segment preparation decision."""

    label: str
    source_path: str
    prepared_path: str
    loudness_status: str
    measured_lufs: float | None
    normalized: bool
    #: Which shape produced ``prepared_path``: ``"ride"``, ``"two-pass"``,
    #: ``"single-pass-fallback"``, or ``None`` when neither ran (an
    #: unnormalized/passthrough segment, or a live passthrough emitted without
    #: entering the preparer's conform paths).  See the ``_LOUDNESS_METHOD_*``
    #: constants for what each value means.
    loudness_method: str | None = None


@dataclass(frozen=True)
class SourcePreparationReport:
    """Prepared source plan plus per-segment decisions for proof/debug output."""

    source_plan: EgressSourcePlan
    records: tuple[PreparedSegmentRecord, ...]
    #: F3 fix: this call's unique ``<channel>/prepared/<uuid>`` directory (see
    #: ``prepare``'s docstring), so a caller that independently knows a plan is
    #: retired (e.g. the daemon, once a content-reload reports its predecessor
    #: settled) can hand it straight back to ``SourcePreparer.release`` instead
    #: of waiting for GC. ``None`` for a caller/test double that never
    #: constructs a real per-plan directory (e.g. a live-only plan whose
    #: ``prepare()`` call wrote nothing at all -- see F7 -- or a fake
    #: preparer in a test).
    plan_dir: Path | None = None


class SourcePreparationCancelledError(SourcePrepareError):
    """The owner cancelled preparation before it could be put on air."""


def _format_optional_seconds(value: float | None) -> str:
    """A segment's ``-ss``/``-t`` value for a log line: an untrimmed segment
    carries ``None`` for both (see ``SourcePreparer._prepare_segment``'s emitted
    segment), and ``"start"``/``"end"`` read better in a warning than ``None``
    — which is also what a ``%.3f`` conversion would crash on."""
    return "start" if value is None else f"{value:.3f}s"


def _probe_prepared_segment_decodability(path: Path) -> bool | None:
    """U36 item 7: the decodability probe the emission-time rejection uses.

    A named boundary rather than a direct ffprobe call so two different
    questions stay separable: "how long is the SOURCE media" (the
    loudness/probe machinery around ``_prepare_segment``, which callers fake to
    simulate an unknown-duration asset) and "is the file preparation just
    EMITTED playable media at all" (this one). They both end at ffprobe today,
    but faking one must not silently answer the other — the second question has
    to stay answerable on its own, and with three answers rather than a
    duration: ``True`` (ffprobe listed a stream), ``False`` (ffprobe ran and
    found none) and ``None`` (ffprobe could not be asked at all). See
    :func:`civiccast.stream._ffmpeg.probe_has_decodable_stream`.
    """
    return probe_has_decodable_stream(path)


class SourcePreparer:
    """Conform one source plan to the channel's canonical egress profile."""

    def __init__(
        self,
        *,
        work_dir: Path,
        ffmpeg_runner: FfmpegRunner = run_ffmpeg,
        loudness_checker: LoudnessChecker = check_streaming_loudness,
        warm_scheduler: WarmScheduler = _default_warm_scheduler,
        playout_trim_supported: bool = False,
        preparation_timeout_seconds: float | None = None,
        window_leveler: WindowLeveler = level_window,
    ) -> None:
        """``playout_trim_supported``: the consuming encoder honors per-segment
        ``inpoint``/``outpoint`` on prepared segments (true for the legacy
        ffmpeg-concat engine via its ffconcat plan; the GStreamer engine reads
        only ``segment.path``). When False — the safe default — cache hits are
        emitted as a fast ``-c copy`` trim into the per-plan output instead, so
        the historic "prepared segments are trim-free" contract holds for any
        consumer. Either way a hit costs seconds, not a re-encode."""
        self._work_dir = work_dir
        self._ffmpeg_runner = ffmpeg_runner
        self._loudness_checker = loudness_checker
        self._warm_scheduler = warm_scheduler
        self._window_leveler = window_leveler
        self._playout_trim_supported = playout_trim_supported
        if preparation_timeout_seconds is not None and preparation_timeout_seconds <= 0:
            raise ValueError("preparation_timeout_seconds must be greater than zero when set.")
        self._preparation_timeout_seconds = (
            preparation_timeout_seconds
            if preparation_timeout_seconds is not None
            else preparation_timeout_seconds_from_env()
        )
        self._warming_guard = threading.Lock()
        self._warming: set[str] = set()
        # ponytail: one Lock per cache key ever seen, never pruned -- bounded
        # by the number of distinct assets aired over the process lifetime.
        self._conform_locks: dict[str, threading.Lock] = {}
        # BETA.10 U29: monotonic deadline before which a warm that FAILED or
        # timed out for this cache key is not re-attempted -- the anti-loop for
        # the live symptom, where every airing re-queued the same unwarmable
        # asset and burned CPU forever. Guarded by ``self._warming_guard``
        # (the same lock that owns ``self._warming``), pruned on success and
        # whenever an expired entry is read, so it is bounded by the assets
        # that have failed inside one backoff window.
        self._warm_backoff_until: dict[str, float] = {}
        self._warm_failure_counts: dict[str, int] = {}
        self._video_copy_checks: dict[tuple[object, ...], bool] = {}
        self._video_copy_guard = threading.Lock()
        # Hostile-review follow-up, item 5: this module has no visibility of
        # its own into which per-plan directories a caller still considers
        # LIVE (an active on-air plan, an armed-but-not-yet-settled reload) --
        # only the daemon knows that. None means GC falls back to keep-N/
        # budget/age alone (still safe, just less precise); see
        # ``set_protected_plan_dirs_provider``.
        self._protected_plan_dirs_provider: Callable[[str], frozenset[Path]] | None = None

    def set_protected_plan_dirs_provider(
        self, provider: Callable[[str], frozenset[Path]] | None
    ) -> None:
        """Wire a callback ``prepare()`` consults before every GC pass to learn
        which of THIS channel's per-plan directories must never be evicted,
        however old, large, or far outside keep-N recency they are.

        A setter rather than a constructor arg because of construction order:
        production wiring builds the ``SourcePreparer`` instance FIRST (so its
        ``.prepare``/``.release`` bound methods can be passed into
        ``EgressDaemon.__init__``), and only the resulting daemon can answer
        "which directories are live" (``EgressDaemon.live_prepared_plan_dirs``)
        -- see cli.py's/automation.py's wiring, both of which call this right
        after constructing the daemon."""
        self._protected_plan_dirs_provider = provider

    # -- persistent conform cache -------------------------------------------

    def _copy_video(
        self, source: Path, profile: CanonicalProfile, segment: EgressSourceSegment | None
    ) -> bool:
        # Input seeking without a known random-access boundary cannot preserve
        # an exact inpoint through stream copy. Keep accurate decoded trimming.
        if segment is not None and segment.inpoint_seconds:
            return False
        if "264" not in profile.video_codec:
            return False
        try:
            stat = source.stat()
        except OSError:
            return False
        key = (
            str(source),
            stat.st_size,
            stat.st_mtime_ns,
            profile.width,
            profile.height,
            profile.fps,
        )
        with self._video_copy_guard:
            cached = self._video_copy_checks.get(key)
        if cached is not None:
            return cached
        # Probing may take seconds; never hold the cache lock across subprocess
        # work. Return our own decision even if another worker evicts the entry.
        decision = probe_canonical_video(
            source, width=profile.width, height=profile.height, fps=profile.fps
        )
        with self._video_copy_guard:
            if len(self._video_copy_checks) >= 512:
                self._video_copy_checks.clear()
            self._video_copy_checks[key] = decision
        return decision

    def _record_warm_failure(self, key: str) -> float:
        with self._warming_guard:
            count = min(self._warm_failure_counts.get(key, 0) + 1, 6)
            self._warm_failure_counts[key] = count
            delay = min(
                _WARM_FAILURE_BACKOFF_SECONDS * 2 ** (count - 1), _WARM_FAILURE_MAX_BACKOFF_SECONDS
            )
            self._warm_backoff_until[key] = time.monotonic() + delay
            return float(delay)

    def _cache_dir(self) -> Path:
        return self._work_dir / _CACHE_DIR_NAME

    def _cache_key(self, source_path: Path, config: EgressConfig) -> str | None:
        """Fingerprint (source file, canonical profile, loudness config) or None.

        Stat-based (path, size, mtime_ns): a re-finalized recording at the same
        path gets a new key. Returns None when caching is disabled or the file
        cannot be statted (callers fall back to uncached behavior).
        """
        if _cache_budget_bytes() <= 0:
            return None
        try:
            st = source_path.stat()
        except OSError:
            return None
        raw = "|".join(
            [
                str(source_path.resolve()),
                str(st.st_size),
                str(st.st_mtime_ns),
                config.canonical_profile.model_dump_json(),
                f"{config.loudness_target_lufs:g}",
                f"{config.loudness_tolerance_lufs:g}",
                # Normalization-method version: bumping this invalidates every
                # cached conform produced by an older method, so a switch from
                # one-pass to two-pass loudnorm can never be served from a
                # stale single-pass cache entry.
                _LOUDNORM_METHOD_VERSION,
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _read_cache_meta(self, key: str) -> dict[str, object] | None:
        meta_path = self._cache_dir() / f"{key}.json"
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _write_cache_meta(
        self,
        key: str,
        loudness: LoudnessGateResult,
        normalized: bool,
        *,
        media_duration_seconds: float | None = None,
        full_asset_conform: bool = False,
        loudness_method: str | None = None,
        only_if_absent: bool = False,
    ) -> None:
        """Item 66 (point 1): now also called BEFORE any conform for this
        asset exists (a loudness-only probe result, persisted early so
        later segments of the same asset can skip re-probing) -- so this
        must create the cache directory itself rather than assume a conform
        call already did.

        Item 66 round-3 (Opus review): tmp+replace atomic write, same
        discipline as every ``.ts`` write in this module -- a reader
        (``_read_cache_meta``, or the cache-HIT check's ``.is_file()``) must
        never observe a partially-written ``{key}.json``.

        ``media_duration_seconds`` (item 66 round-5, point 2; simplified
        round-6, point 3): the asset's real media duration, probed once via
        ``probe_media_duration_seconds`` and cached here so later
        segments/airings of the same asset never re-probe it. Every call
        site that could plausibly know this value now threads it through
        explicitly (``_conform_full_asset_into_cache``,
        ``_promote_conform_into_cache``, ``_promote_finished_conform_into_
        cache``, ``_schedule_cache_copy_promotion``, ``_schedule_warm`` all
        gained the same keyword) -- a round-5 version of this method instead
        read the existing meta back and merged in whatever value it already
        held, purely to avoid a later write-without-the-value clobbering an
        earlier write-with-it. That extra read on every write is no longer
        needed now that the value travels with the call: this write is the
        single atomic write of everything this cache entry's meta holds.

        ``full_asset_conform`` (item 66 round-7, point 1, HIGH): True ONLY
        when this write accompanies (or follows) an actual ``{key}.ts`` that
        genuinely holds the whole asset -- set exclusively by
        ``_promote_conform_into_cache``, the single choke point every
        promotion path (``_conform_full_asset_into_cache``,
        ``_promote_finished_conform_into_cache``, ``_schedule_cache_copy_
        promotion``'s job) funnels through right after the ``.ts`` itself is
        finalized. The probe-only call in ``_prepare_segment`` (persisted
        before any conform exists) never sets it, so it defaults False there.
        The cache-HIT check in ``_prepare_segment`` requires this flag before
        trusting ``{key}.ts`` as the shared full-asset entry -- see that
        check's own comment for why a numeric ``duration_seconds`` comparison
        alone cannot detect an already-on-disk short/truncated ``.ts`` (D42,
        round-6 BLOCKER: a slot-capped "untrimmed" conform used to get
        promoted as if it were the whole asset). A meta file written by
        pre-round-7 code never carries this key, so ``.get(...)`` on it
        naturally reads False too -- a legacy short entry self-heals into a
        MISS (and gets correctly re-conformed) rather than staying silently
        wrong.

        ``loudness_method`` (U20): which loudnorm shape produced the ``.ts``
        this meta describes -- ``"two-pass"`` or ``"single-pass-fallback"``, or
        ``None`` when no loudnorm filter ran at all. Written by the same single
        choke point that sets ``full_asset_conform``
        (``_promote_conform_into_cache``, which every conform path funnels
        through), and read back by the cache-HIT path so a served segment's own
        record still says how its audio was conformed. The probe-only write in
        ``_prepare_segment`` -- which lands BEFORE any conform for this asset
        exists -- leaves it ``None``, which is the honest answer at that point:
        no conform has chosen a shape yet.

        ``only_if_absent`` (U65): publish this sidecar only if no sidecar for
        this key exists yet, atomically. Round-6's "the later promote's write
        replaces the whole sidecar with the real value" holds only WITHIN one
        ``_prepare_segment`` call -- that is where ``meta`` is read, and the
        probe branch is reached only when it read ``None``, so the probe's
        write always precedes this call's own promote. It does NOT hold across
        two concurrent prepares for the same asset, which is the ordinary case
        on a multi-channel station sharing one cache: prepare A reads ``meta``
        as ``None`` (cache cold) and starts its probe; prepare B finishes a
        whole-asset conform and promotes it; A's probe then finishes and its
        write here REPLACED B's sidecar, erasing ``full_asset_conform`` and
        ``loudness_method``. The next prepare then read the flag False and
        re-conformed the entire asset -- live 2026-09-29, three times in one
        8 h rung on a 4.4 h asset. The probe-only caller passes
        ``only_if_absent=True`` so this write can never clear markers it did
        not set; the promote caller leaves it False, because a promote's write
        is the authoritative one and must replace what it supersedes. This is
        a no-op in the uncontended case: with no sidecar on disk (the only
        state in which the probe branch runs single-threaded) the atomic
        create always succeeds. It falls back to replacing only when the
        existing file is not usable meta at all (unparseable or truncated),
        so a corrupt sidecar still self-heals rather than stranding every
        later prepare in a re-probe.
        """
        cache_dir = self._cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "loudness_status": loudness.status,
            "measured_lufs": loudness.measured_lufs,
            "normalized": normalized,
            "media_duration_seconds": media_duration_seconds,
            "full_asset_conform": full_asset_conform,
            "loudness_method": loudness_method,
        }
        final = cache_dir / f"{key}.json"
        tmp = final.with_name(final.name + ".tmp")
        tmp.write_text(json.dumps(meta), encoding="utf-8")
        if not only_if_absent:
            tmp.replace(final)
            return
        # U65: atomic create-if-absent. ``os.link`` raises ``FileExistsError``
        # when ``final`` already exists, rather than overwriting it the way
        # ``replace`` does, so the FILESYSTEM arbitrates the race. That is why
        # this needs neither the per-key conform lock (held for the whole ride,
        # and deliberately taken non-blocking by the air path, so blocking a
        # start-path probe write behind it would stall the prepare) nor a
        # read-modify-write (which would still be a TOCTOU). Hard-linking is
        # the same idiom ``_promote_finished_conform_into_cache`` already uses
        # for the ``.ts`` (see its ``os.link`` fallback below for the
        # cross-volume shape).
        try:
            os.link(tmp, final)
        except FileExistsError:
            # Somebody else published this key's sidecar while this probe was
            # in flight -- normally a promote, whose write carries the
            # ``full_asset_conform``/``loudness_method`` markers this probe
            # cannot know about (the key fingerprints the asset, so it is the
            # same asset). Theirs wins; this probe's result is dropped.
            #
            # Exception: if what is on disk is not usable meta at all -- an
            # unparseable or truncated file, which ``_read_cache_meta`` reports
            # as ``None`` -- there is nothing worth preserving, and the probe's
            # own result is the better entry, so replace it. Without this the
            # corrupt file would strand every later prepare in a re-probe.
            if self._read_cache_meta(key) is None:
                tmp.replace(final)
        except OSError:
            # The filesystem cannot hard-link (a network share or FAT32 work
            # dir). Fall back to exactly the pre-U65 write so the probe result
            # is still persisted and a prepare never newly fails here; the
            # create-if-absent guarantee is then unavailable, which is the
            # pre-U65 status quo rather than a regression.
            tmp.replace(final)
        finally:
            tmp.unlink(missing_ok=True)

    def _conform_lock(self, key: str) -> threading.Lock:
        """A per-cache-key lock: a background warm and a foreground
        untrimmed-miss conform for the same asset must never write the
        identical {key}.ts.tmp concurrently."""
        with self._warming_guard:
            return self._conform_locks.setdefault(key, threading.Lock())

    def _measure_loudnorm_metadata(
        self,
        *,
        source_path: Path,
        segment: EgressSourceSegment | None,
        loudness_target_lufs: float,
        threads: int | None,
        cancel_event: threading.Event | None,
        timeout_seconds: float | None = None,
        lower_priority: bool = False,
    ) -> tuple[dict[str, str] | None, str]:
        """Pass 1 of loudnorm for one window: measure it, or degrade loudly.

        U20: a measurement pass that fails, times out, exits non-zero, emits no
        parseable JSON, or yields metadata ``_validated_measured_loudness``
        rejects is NOT fatal any more.  It degrades: ONE warning naming the
        source, the exact window and the reason, and ``None`` for the metadata
        -- which both callers read as "conform this window with the plain
        single-pass ``loudnorm`` filter", byte-for-byte what the installed
        station ships today.  The reason a measurement can fail here is
        operator-legible on purpose: an unreadable/absent ``-af`` JSON block, an
        ffmpeg that rejected the input, a runner timeout, or a non-finite
        measured value.

        Returns ``(metadata, method)``: ``(parsed, "two-pass")`` on success and
        ``(None, "single-pass-fallback")`` on any measured failure, so the
        caller can both pick the right filter and record which shape ran.

        Cancellation is NOT a measurement failure -- ``SourcePreparationCancel
        ledError`` is a shutdown/pause signal and re-raises untouched, exactly
        as both call sites handled it before.  Real conform errors (pass 2's
        encode) are likewise untouched: only the measurement pass degrades.

        U29: the probe decodes the same window the conform will encode, so on
        the warm path it must carry the warm's own scaled budget and priority
        -- a probe killed at the flat 300s bound measures nothing, degrades to
        single-pass, and (per U20) leaves the whole-asset cache entry unpopulated,
        which is the same permanent re-warm loop by another route.  Both knobs
        default to the previous behavior for the on-demand/air path.
        """
        args = build_loudnorm_probe_args(
            source_path=source_path,
            segment=segment,
            loudness_target_lufs=loudness_target_lufs,
            threads=threads,
        )
        window = _describe_segment_window(segment)
        reason = "unrecognized measurement failure"
        parsed: dict[str, str] | None = None
        try:
            probe_result = self._run_ffmpeg(
                args,
                cancel_event,
                timeout_seconds=timeout_seconds,
                lower_priority=lower_priority,
            )
        except SourcePreparationCancelledError:
            raise
        except Exception as exc:  # any probe failure degrades to single-pass
            reason = f"{type(exc).__name__}: {exc}"
        else:
            if probe_result.returncode != 0:
                reason = f"probe exited {probe_result.returncode}"
            else:
                candidate = parse_loudnorm_measurement(probe_result.stderr)
                if candidate is None:
                    reason = "probe emitted no parseable loudnorm JSON"
                else:
                    try:
                        _validated_measured_loudness(candidate)
                    except SourcePrepareError as exc:
                        reason = f"probe metadata unusable: {exc}"
                    else:
                        parsed = candidate
        if parsed is not None:
            return parsed, _LOUDNESS_METHOD_TWO_PASS
        _LOG.warning(
            "Loudnorm measurement pass failed for %r (window: %s): %s. Conforming "
            "with the single-pass loudnorm filter instead.",
            source_path.name,
            window,
            reason,
        )
        return None, _LOUDNESS_METHOD_SINGLE_PASS_FALLBACK

    def _ride_conform_tmp(
        self,
        *,
        source_path: Path,
        tmp_path: Path,
        config: EgressConfig,
        segment: EgressSourceSegment | None,
        threads: int | None,
        cancel_event: threading.Event | None,
        timeout_seconds: float | None = None,
        lower_priority: bool = False,
    ) -> str | None:
        """U25: level this window with the ride, mux it onto the source video,
        and leave the program at ``tmp_path``.

        The ride is tried FIRST on every normalized conform, because it is the
        only shape that holds the level across every 4-minute stretch of the
        window rather than only in the whole-program average.  It is attempted,
        never assumed: ``None`` means the ride could not run for this window
        (no numpy, no FFmpeg, no soxr in the build, a decode/encode failure, a
        failed program mux) and the caller must conform the window with
        ``loudnorm`` exactly as it did before U25 -- after ONE warning naming
        the source, the window and the reason.  A degradation is never a
        failure: a station whose host cannot ride still airs leveled audio.

        Cancellation is the one thing that is NOT a degradation.  A cancelled
        ride and a cancelled mux both re-raise as
        ``SourcePreparationCancelledError`` so a shutdown or pause stops this
        prepare the same way it stops a cancelled measurement or encode.

        Both intermediates are the ride's own to clean up: the ride's audio-only
        TS is unlinked in the ``finally`` (the mux stream-copies it, so nothing
        downstream needs it), and the caller's ``tmp_path`` is unlinked before a
        degradation return -- ``build_video_from_source_args`` carries no
        ``-y``, so a leftover file would make the fallback's own encode fail
        with "File exists" instead of airing the segment.

        U25 x U29 (merge resolution, U40): ``timeout_seconds``/``lower_priority``
        are U29's warm-only knobs, threaded through so a ride attempted by a
        conform-cache warm runs under the warm's duration-scaled budget and at
        the lowered priority -- both this window's leveler (``timeout_s``) and
        the program mux. ``None``/``False`` (every air-path caller) leave the
        call exactly as it was: the configured preparation timeout at normal
        priority, so an airing still fails closed fast.

        Returns ``_LOUDNESS_METHOD_RIDE`` when ``tmp_path`` holds the muxed
        program, or ``None`` to fall back.
        """
        target_lufs = config.loudness_target_lufs
        assert target_lufs is not None  # both callers gate on this
        window = _describe_segment_window(segment)
        audio_path = ride_audio_path(tmp_path)
        try:
            try:
                window_result = self._window_leveler(
                    source_path=source_path,
                    audio_path=audio_path,
                    params=RideParams(target_lufs=target_lufs),
                    profile=config.canonical_profile,
                    duration_s=None if segment is None else segment.duration_seconds,
                    segment=segment,
                    threads=threads,
                    cancel_event=cancel_event,
                    # U29: a warm's scaled budget is a FLOOR-widening for this
                    # one call; the air path passes ``None`` and keeps the
                    # configured preparation timeout.
                    timeout_s=(
                        self._preparation_timeout_seconds
                        if timeout_seconds is None
                        else timeout_seconds
                    ),
                )
            except LoudnessRideCancelledError as exc:
                # Checked first: a cancellation IS a LoudnessRideError, and it
                # must never degrade into a fallback conform on a shutdown.
                raise SourcePreparationCancelledError(
                    f"Egress source {source_path.name!r} speech leveling ride was cancelled."
                ) from exc
            except (LoudnessRideError, FfmpegNotFoundError, OSError) as exc:
                self._log_ride_degraded(source_path, window, str(exc))
                return None
            self._log_ride_selection(source_path, window, window_result)
            try:
                result = self._run_ffmpeg(
                    build_video_from_source_args(
                        source_path=source_path,
                        audio_path=audio_path,
                        output_path=tmp_path,
                        segment=segment,
                        profile=config.canonical_profile,
                        copy_video=self._copy_video(source_path, config.canonical_profile, segment),
                        threads=threads,
                    ),
                    cancel_event,
                    timeout_seconds=timeout_seconds,
                    lower_priority=lower_priority,
                )
            except SourcePreparationCancelledError:
                tmp_path.unlink(missing_ok=True)
                raise
            except SourcePrepareError as exc:
                # A runner timeout (or any other runner failure) can leave a
                # partial program behind; remove it before propagating so the
                # fallback's own encode does not land on "File exists".
                tmp_path.unlink(missing_ok=True)
                self._log_ride_degraded(source_path, window, str(exc))
                return None
            if result.returncode != 0:
                tmp_path.unlink(missing_ok=True)
                self._log_ride_degraded(
                    source_path, window, f"the program mux exited {result.returncode}"
                )
                return None
            return _LOUDNESS_METHOD_RIDE
        finally:
            audio_path.unlink(missing_ok=True)

    @staticmethod
    def _log_ride_degraded(source_path: Path, window: str, reason: str) -> None:
        """ONE warning for a ride that could not run -- then loudnorm conforms."""

        _LOG.warning(
            "Speech leveling ride failed for %r (window: %s): %s. Conforming with "
            "loudnorm instead, which does not hold the level across every 4-minute "
            "stretch.",
            source_path.name,
            window,
            reason,
        )

    @staticmethod
    def _log_ride_selection(source_path: Path, window: str, result: LeveledWindow) -> None:
        """Report what the ride shipped: its warning, its round error, and -- on
        the caller's side of the contract -- the kept attempt's own gate verdicts.

        The module never logs, so this is where the ride's one WARNING and its
        (rarer) ERROR become operator-visible, phrased with the window's terms.
        """

        selection = result.selection
        kept = selection.kept
        if selection.warning is not None:
            _LOG.warning(
                "Speech leveling for %r (window: %s): %s",
                source_path.name,
                window,
                selection.warning,
            )
        if result.round_error is not None:
            _LOG.warning(
                "Speech leveling for %r (window: %s): a re-encode round failed (%s); "
                "airing the best attempt the guard kept.",
                source_path.name,
                window,
                result.round_error,
            )
        if not kept.loudness_ok(
            window_tol_lu=LOUDNESS_WINDOW_TOL_LU, whole_tol_lu=LOUDNESS_WHOLE_TOL_LU
        ):
            _LOG.warning(
                "Speech leveling for %r (window: %s): the kept attempt misses the loudness "
                "gate (worst 4-minute stretch %s LU, whole program %s LU).",
                source_path.name,
                window,
                _fmt_measure(kept.worst_window_err_lu),
                _fmt_measure(kept.whole_err_lu),
            )
        if not selection.hard_tp_met:
            # The artifact airs anyway -- a hot artifact beats a silent channel
            # -- but the hard true-peak bound did not hold, and that is an
            # operator's problem, not a footnote.  U42: the encoder settings
            # name which lever produced this artifact, because the two levers
            # are not equally cheap and the next reader will want to know.
            _LOG.error(
                "Speech leveling for %r (window: %s): the kept attempt is above the hard "
                "true-peak bound (%s dBFS sample peak; %s dBTP emitted, ceiling %s dBTP; "
                "encoder %s).",
                source_path.name,
                window,
                _fmt_measure(kept.decoded_peak_dbfs),
                _fmt_measure(kept.emitted_dbtp),
                _fmt_measure(kept.limit_dbtp),
                kept.encoder or "not recorded",
            )

    def _conform_full_asset_into_cache(
        self,
        key: str,
        source_path: Path,
        config: EgressConfig,
        loudness: LoudnessGateResult,
        normalized: bool,
        *,
        threads: int = 1,
        media_duration_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
        lower_priority: bool = False,
    ) -> Path | None:
        """Conform the WHOLE asset (no trim) into the cache, atomically.

        ``threads`` (item 66, revised): the warm-behind path
        (``_schedule_warm``) keeps the default ``1`` -- single-threaded, so a
        background warm can never starve the on-air encoder. The synchronous
        start-path call (the untrimmed-MISS branch in ``_prepare_segment``,
        reachable only when ``self._playout_trim_supported`` is True -- see
        the guard there) passes ``_foreground_thread_cap()`` instead: an
        Opus-review follow-up found the original fix's unconditional
        ``background=False`` (fully unthrottled) starved the ffmpeg-concat
        engine's synchronous content-reload prepare (``daemon.py``'s
        ``_try_content_reload``, which runs this call ON the automation
        thread while other channels may be on air) -- a bounded cap keeps
        the synchronous path fast without letting it claim every core.

        Returns ``None`` instead of conforming (item 66 round-4 BLOCKER fix,
        Opus review) when this key's per-key lock is already held --
        virtually always ``_schedule_warm``'s background job already
        conforming the identical asset. This lock used to be acquired with a
        BLOCKING ``with`` statement: a background warm holds it for its
        entire single-threaded conform (tens of minutes for a long asset),
        so a synchronous caller landing here while that warm is running
        would stall behind it for just as long -- exactly the kind of delay
        item 66 exists to close, just via lock contention instead of a
        direct synchronous conform. Callers on the synchronous path
        (``_prepare_segment``'s untrimmed/``playout_trim_supported`` branch)
        must treat ``None`` as "fall back to the bounded per-segment conform
        instead of waiting"; ``_schedule_warm``'s own background job ignores
        the return value entirely (a warm colliding with another warm for
        the same key can't happen -- see its own dedupe -- so ``None`` there
        would only mean an unrelated caller is already populating this exact
        entry, which is fine to leave to that caller).

        U20 adds a SECOND ``None`` case to the same contract: the loudnorm
        measurement pass for the whole asset failed or returned unusable
        metadata, so this call degraded to the single-pass filter
        (``_measure_loudnorm_metadata`` logged ONE warning and returned
        ``"single-pass-fallback"``). A single-pass artifact must never occupy
        the whole-asset cache entry -- see that branch's own comment -- so this
        method returns ``None`` before writing anything, and the caller falls
        through to its bounded per-segment conform exactly as it does for lock
        contention. Both ``None`` cases mean the same thing to every caller
        ("this call did not populate ``{key}``"); neither is an error.

        U29 adds ``timeout_seconds``/``lower_priority`` as pass-through knobs
        for the warm path only. They are parameters rather than a computed
        scaling here on purpose: this method is ALSO called synchronously from
        ``_prepare_segment``'s untrimmed-MISS branch, on the air path, and
        computing a duration-scaled budget inside would silently change the
        air path's timeout -- which U29 must not do. Defaults keep that
        caller exactly as it was; see ``warm_preparation_timeout_seconds``.

        U25 puts the speech leveling ride FIRST here too, with ``segment=None``
        (the whole asset) and the same ``tmp``/promote tail: a ridden window is
        promoted through ``loudness_method="ride"`` exactly as a two-pass one
        is promoted through ``"two-pass"``, and a ride that could not run
        degrades to the loudnorm path below it with ONE warning
        (``_ride_conform_tmp``), never to a ``None`` return.

        U25 x U29 (merge resolution, U40): BOTH knobs are threaded into every
        stage this method runs -- they reach ``_ride_conform_tmp`` (its window
        leveler's ``timeout_s`` and its program mux) as well as the loudnorm
        fallback's ``_measure_loudnorm_metadata`` and conform encode. U29's
        point is that a warm's budget scales with the asset; a ride that
        ignored the knobs would be killed at the flat foreground bound on
        exactly the long assets the warm exists for, and would then pay for the
        loudnorm path as well. The air path still passes neither knob, so its
        fail-closed-fast behaviour is unchanged.
        """
        lock = self._conform_lock(key)
        if not lock.acquire(blocking=False):
            _LOG.info(
                "Full-asset conform skipped for %r: another conform/warm already holds "
                "this asset's cache lock.",
                source_path.name,
            )
            return None
        try:
            cache_dir = self._cache_dir()
            cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = cache_dir / f"{key}.ts.tmp"
            loudness_method: str | None = None
            measured_loudness: dict[str, str] | None = None
            if normalized and config.loudness_target_lufs is not None:
                loudness_method = self._ride_conform_tmp(
                    source_path=source_path,
                    tmp_path=tmp,
                    config=config,
                    segment=None,
                    threads=threads,
                    cancel_event=cancel_event,
                    timeout_seconds=timeout_seconds,
                    lower_priority=lower_priority,
                )
                if loudness_method != _LOUDNESS_METHOD_RIDE:
                    # U20's two-pass measurement, carrying U29's warm knobs: a
                    # warm's measurement probe is as duration-bound as its
                    # encode, so it takes the same scaled budget.
                    measured_loudness, loudness_method = self._measure_loudnorm_metadata(
                        source_path=source_path,
                        segment=None,
                        loudness_target_lufs=config.loudness_target_lufs,
                        threads=threads,
                        cancel_event=cancel_event,
                        timeout_seconds=timeout_seconds,
                        lower_priority=lower_priority,
                    )
                if loudness_method == _LOUDNESS_METHOD_SINGLE_PASS_FALLBACK:
                    # U20: the measurement pass measured nothing usable, so we
                    # degrade instead of failing -- but NOT here.  A conform
                    # under ``{key}`` is the cached WHOLE-ASSET artifact that
                    # ``_promote_conform_into_cache`` marks
                    # ``full_asset_conform=True`` and every later airing trusts
                    # outright, so a single-pass artifact must never be written
                    # under that key: it would be served to every future airing
                    # as if a two-pass measurement had produced it.  Nothing has
                    # been written at this point (the probe above outputs to
                    # ``-f null``), so return ``None`` -- the same "someone else
                    # is already conforming this asset" signal the lock-
                    # contention path above uses -- leaving no ``.ts`` and no
                    # ``.json`` behind for this key at all.  The synchronous
                    # caller (``_prepare_segment``'s untrimmed-MISS branch)
                    # falls through to its bounded per-segment conform, which
                    # re-runs the measurement itself and degrades only that one
                    # window straight to air.  Because no fallback entry is ever
                    # created, a later run whose probe succeeds populates the
                    # genuine two-pass entry with nothing to replace.
                    return None
                # ``ride`` needs no measured metadata -- it levels the window
                # itself, so ``measured_loudness`` stays ``None`` on that path
                # by design.  Anything else reaching this line went through
                # ``_measure_loudnorm_metadata`` and must carry its validated
                # metadata, which is what ``build_conform_source_args`` is
                # about to consume.
                assert loudness_method == _LOUDNESS_METHOD_RIDE or measured_loudness is not None
            if loudness_method != _LOUDNESS_METHOD_RIDE:
                # U25: a ridden window is already the program at ``tmp`` -- the
                # ride muxed its own audio onto the source video -- so the only
                # work left is the promotion below.
                args = build_conform_source_args(
                    source_path=source_path,
                    output_path=tmp,
                    segment=None,
                    profile=config.canonical_profile,
                    copy_video=self._copy_video(source_path, config.canonical_profile, None),
                    loudness_target_lufs=config.loudness_target_lufs if normalized else None,
                    threads=threads,
                    measured_loudness=measured_loudness,
                )
                try:
                    # U29 knobs on the loudnorm conform encode (the ride muxed
                    # its own program above, so this encodes only the fallback).
                    result = self._run_ffmpeg(
                        args,
                        cancel_event,
                        timeout_seconds=timeout_seconds,
                        lower_priority=lower_priority,
                    )
                except Exception:
                    # A timeout or any other runner failure can leave a partial
                    # multi-gigabyte output behind.  Remove it before propagating
                    # the error so a failed warm cannot silently consume the
                    # conform-cache budget.
                    with contextlib.suppress(OSError):
                        tmp.unlink(missing_ok=True)
                    raise
                if result.returncode != 0:
                    tmp.unlink(missing_ok=True)
                    raise SourcePrepareError(
                        f"Full-asset conform for cache failed for {source_path.name!r}."
                    )
            return self._promote_conform_into_cache(
                key,
                tmp,
                source_path,
                loudness,
                normalized,
                media_duration_seconds=media_duration_seconds,
                loudness_method=loudness_method,
            )
        finally:
            lock.release()

    def _promote_conform_into_cache(
        self,
        key: str,
        tmp: Path,
        source_path: Path,
        loudness: LoudnessGateResult,
        normalized: bool,
        *,
        media_duration_seconds: float | None = None,
        loudness_method: str | None,
    ) -> Path:
        """Move an already-conformed ``tmp`` file (a full-asset conform, no
        trim) into the persistent conform cache atomically: rename into
        place, write the sidecar meta, run eviction, and fail cleanly if the
        just-written entry alone exceeds budget.

        Shared tail of every call site that already holds ``self._conform_
        lock(key)`` around the call (item 66 round-4: NON-BLOCKING
        acquisition at every one of them, so this method itself never
        blocks -- see ``_conform_full_asset_into_cache``,
        ``_promote_finished_conform_into_cache``, and
        ``_schedule_cache_copy_promotion``'s docstrings for what "already
        holds it" means at each -- round-5: the LATTER TWO must always
        release their own hold before returning, including on every
        failure path, since this method's own ``SourcePrepareError`` below
        is exactly the kind of failure a caller needs to still unwind
        cleanly from).

        Raises ``SourcePrepareError`` if the just-written entry alone
        exceeds the configured budget -- callers that must never let a
        cache-promotion failure interrupt something already safely airing
        (``_promote_finished_conform_into_cache``,
        ``_schedule_cache_copy_promotion``) catch this themselves.

        Item 66 round-7 (point 1, HIGH): this is the SOLE place a ``.ts`` is
        ever renamed into the persistent cache, so it is the one authoritative
        place to mark the sidecar meta ``full_asset_conform=True`` -- every
        caller (``_conform_full_asset_into_cache``'s own whole-file conform,
        and ``_promote_finished_conform_into_cache``/``_schedule_cache_copy_
        promotion``'s copy of an already-finished per-plan file) is now
        responsible for only ever reaching this method with a ``tmp`` that
        genuinely IS the whole asset -- ``_prepare_segment``'s cache-hit check
        depends on that invariant holding here unconditionally.
        """
        cache_dir = self._cache_dir()
        final = cache_dir / f"{key}.ts"
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp.replace(final)
        self._write_cache_meta(
            key,
            loudness,
            normalized,
            media_duration_seconds=media_duration_seconds,
            full_asset_conform=True,
            loudness_method=loudness_method,
        )
        self._evict_cache_over_budget()
        if not final.exists():
            # The just-written entry alone exceeded the budget and was
            # evicted by the call above -- fail cleanly instead of
            # returning a path the encoder will find missing at air time.
            (cache_dir / f"{key}.json").unlink(missing_ok=True)
            raise SourcePrepareError(
                f"Conform-cache budget too small to retain {source_path.name!r}; "
                "increase CIVICCAST_CONFORM_CACHE_GB or exclude this asset."
            )
        return final

    def _promote_finished_conform_into_cache(
        self,
        key: str,
        finished_output_path: Path,
        source_path: Path,
        loudness: LoudnessGateResult,
        normalized: bool,
        *,
        media_duration_seconds: float | None = None,
        loudness_method: str | None = None,
    ) -> None:
        """Item 66 round-3 BLOCKER fix (Opus review): populate the
        persistent conform cache from an ALREADY-FINISHED, already-airing
        per-plan file via a hard link (``os.link``) -- never by moving
        ``finished_output_path`` itself. The segment is airing from that
        exact path; this method must never make it disappear.

        Round-4 (Opus review), point 1: the per-key lock is now acquired
        NON-BLOCKING. A background warm holds this SAME lock for its
        entire single-threaded conform (tens of minutes for a long asset);
        a blocking acquire here meant a synchronous caller could stall
        behind an in-progress warm for just as long. If the lock is
        already held, the promotion is skipped entirely (logged) -- the
        segment already airs from ``finished_output_path`` regardless, so
        skipping costs nothing except that this one airing's copy is (for
        now) absent from the persistent cache; the in-progress warm (or
        the next airing) populates it instead.

        Round-5 (Opus review) BLOCKER fix: linking is a metadata-only
        filesystem operation (fast) and still runs inline; but when it
        FAILS (e.g. the cache lives on a different volume than the
        per-plan directory), the full byte-for-byte copy is handed to the
        warm scheduler (``_schedule_cache_copy_promotion``) -- and that
        scheduling now happens ONLY AFTER this method's own lock is fully
        released (the ``lock.release()`` in ``finally`` below runs before
        the scheduling call, never after). Round 4 scheduled the copy job
        from INSIDE the still-held lock: the queued job itself
        blocking-acquired the SAME lock, so a synchronous
        ``warm_scheduler`` (the job runs inline, before this frame's
        ``finally`` could ever run) was a guaranteed self-deadlock, and
        even the real threaded scheduler head-of-line-blocked its single
        worker on a lock THIS caller was still holding.

        Any OTHER failure here (a failed link, or
        ``_promote_conform_into_cache``'s own over-budget
        ``SourcePrepareError``) is logged and swallowed, never raised: the
        segment is already safely on air from ``finished_output_path``
        regardless. The only cost of a promotion failure is that the NEXT
        airing of this asset misses the cache and re-conforms, exactly
        like a failed background warm already does (see
        ``_schedule_warm``).
        """
        lock = self._conform_lock(key)
        if not lock.acquire(blocking=False):
            _LOG.info(
                "Cache promotion skipped for %r: another conform/warm already holds "
                "this asset's cache lock; the segment already aired from its own file.",
                source_path.name,
            )
            return
        cache_dir = self._cache_dir()
        tmp = cache_dir / f"{key}.ts.tmp"
        needs_background_copy = False
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            tmp.unlink(missing_ok=True)
            try:
                os.link(finished_output_path, tmp)
            except OSError:
                # Cross-volume (or otherwise unlinkable) -- the actual copy
                # is scheduled AFTER this method's own lock is released
                # (see the `finally` below and the call after this
                # try/except/finally) rather than here, while still held.
                needs_background_copy = True
            else:
                self._promote_conform_into_cache(
                    key,
                    tmp,
                    source_path,
                    loudness,
                    normalized,
                    media_duration_seconds=media_duration_seconds,
                    loudness_method=loudness_method,
                )
        except Exception:
            _LOG.exception(
                "Promoting the finished conform for %r into the persistent conform cache "
                "failed; the segment already airs from its per-plan file unaffected. "
                "The next airing of this asset misses the cache and re-conforms.",
                source_path.name,
            )
            with contextlib.suppress(OSError):
                tmp.unlink()
        finally:
            lock.release()

        if needs_background_copy:
            self._schedule_cache_copy_promotion(
                key,
                finished_output_path,
                source_path,
                loudness,
                normalized,
                media_duration_seconds=media_duration_seconds,
                loudness_method=loudness_method,
            )

    def _schedule_cache_copy_promotion(
        self,
        key: str,
        finished_output_path: Path,
        source_path: Path,
        loudness: LoudnessGateResult,
        normalized: bool,
        *,
        media_duration_seconds: float | None = None,
        loudness_method: str | None = None,
    ) -> None:
        """Item 66 round-4 (Opus review, point 3): queue a full
        byte-for-byte copy of an already-finished, already-airing per-plan
        file into the persistent cache, for the case where
        ``_promote_finished_conform_into_cache`` could not hard-link it
        (typically a cross-volume cache/work-dir layout). MUST be called
        only after the caller has released its own hold on this key's
        per-key lock (round-5 BLOCKER fix -- see
        ``_promote_finished_conform_into_cache``'s docstring for why
        scheduling from inside a still-held lock was a self-deadlock with
        a synchronous scheduler and head-of-line blocking with the real
        one). Runs on the same single-worker warm queue as
        ``_schedule_warm`` (see ``_default_warm_scheduler``), so a slow
        cross-volume copy never contends with a foreground conform, and
        shares its dedupe (``self._warming``) so a burst of airings of the
        same asset never queues more than one populate job for it -- a
        warm and a copy-promotion for the same key are mutually exclusive
        the same way two warms for the same key already are.

        The queued job itself acquires the lock NON-BLOCKING too: on
        contention (a warm, or another copy-promotion job, already holds
        this key's lock) it skips outright rather than waiting or
        immediately re-queuing itself -- a naive re-queue with nothing
        else in the queue would busy-loop the single warm worker for as
        long as the contending operation runs, potentially tens of minutes
        for a warm's full-asset conform. Skipping costs nothing: whatever
        holds the lock is already populating this exact cache entry.

        Item 66 round-5 (point 5): this job's ``finished_output_path``
        lives under a per-plan ``prepared/<uuid>/`` directory, and this
        job can sit in the warm queue for a while before it runs -- see
        ``release``'s docstring for the accepted (not fixed) race against
        that directory being reclaimed (by ``release`` or
        ``_gc_prepared_plan_dirs``) before the copy runs.

        Item 66 round-6 (point 7): if handing ``_job`` to ``self.
        _warm_scheduler`` itself raises (the job is never queued at all --
        "discarded" before it ever got a chance to run its own cleanup),
        ``key`` is removed from ``self._warming`` right here instead of
        being left there forever -- otherwise ``_schedule_warm``'s own
        top-of-function dedupe check (which shares this same set) would
        silently refuse to ever schedule a GENUINE warm for this asset
        again for the life of this ``SourcePreparer`` instance.
        """
        with self._warming_guard:
            if key in self._warming:
                return
            self._warming.add(key)

        def _job() -> None:
            tmp: Path | None = None
            try:
                cached_ts = self._cache_dir() / f"{key}.ts"
                # Item 66 round-8 (MEDIUM fix): a flagless meta (a
                # pre-round-7 entry, or a probe-only write that never got a
                # conform) is not a genuine full-asset cache hit -- see
                # ``_write_cache_meta``'s and the cache-HIT check's own
                # docstrings for why ``full_asset_conform`` must be
                # explicitly True. Without this check, a legacy flagless
                # entry sitting here read as "already populated" and this
                # job returned without ever conforming, so the asset never
                # healed: every future airing kept paying the foreground
                # conform cost forever, and the stale short ``.ts`` stayed
                # in the eviction budget taking up space it was never
                # supposed to hold.
                cached_meta = self._read_cache_meta(key)
                if (
                    cached_ts.is_file()
                    and cached_meta is not None
                    and cached_meta.get("full_asset_conform") is True
                ):
                    return  # already populated by the time this job ran
                lock = self._conform_lock(key)
                if not lock.acquire(blocking=False):
                    _LOG.info(
                        "Background cache-copy promotion skipped for %r: another "
                        "conform/warm already holds this asset's cache lock.",
                        source_path.name,
                    )
                    return
                try:
                    cache_dir = self._cache_dir()
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    tmp = cache_dir / f"{key}.ts.tmp"
                    tmp.unlink(missing_ok=True)
                    try:
                        shutil.copy2(finished_output_path, tmp)
                    except Exception:
                        # Item 66 round-5 (point 5): a failed/partial copy
                        # must never leave a corrupt .ts.tmp behind for
                        # _evict_cache_over_budget's orphan reap to trip
                        # over later -- clean it up right away.
                        with contextlib.suppress(OSError):
                            tmp.unlink()
                        raise
                    self._promote_conform_into_cache(
                        key,
                        tmp,
                        source_path,
                        loudness,
                        normalized,
                        media_duration_seconds=media_duration_seconds,
                        loudness_method=loudness_method,
                    )
                finally:
                    lock.release()
            except Exception:
                _LOG.exception(
                    "Background cache-copy promotion failed for %r; the next airing "
                    "re-populates the cache.",
                    source_path.name,
                )
                # Item 66 round-6 (point 5): the inner handler above already
                # covers a failed copy2; this covers every OTHER failure in
                # the block (cache_dir.mkdir, the initial tmp.unlink, or
                # _promote_conform_into_cache's own over-budget raise after
                # copy2 succeeded but before tmp was renamed away) so a
                # partial .ts.tmp is never left for the orphan reap to find.
                if tmp is not None:
                    with contextlib.suppress(OSError):
                        tmp.unlink()
            finally:
                with self._warming_guard:
                    self._warming.discard(key)

        try:
            _job.__dict__["_warm_key"] = key
            self._warm_scheduler(_job)
        except Exception:
            with self._warming_guard:
                self._warming.discard(key)
            _LOG.exception(
                "Failed to queue background cache-copy promotion for %r; the next "
                "airing re-populates the cache.",
                source_path.name,
            )

    def _emit_prepared_from_cache(
        self,
        cached_ts: Path,
        segment: EgressSourceSegment,
        *,
        source_path: Path,
        output_path: Path,
        loudness_status: str,
        measured_lufs: float | None,
        normalized: bool,
        cancel_event: threading.Event | None = None,
        loudness_method: str | None = None,
    ) -> tuple[EgressSourceSegment, PreparedSegmentRecord]:
        """Emit a prepared segment backed by the cached full-asset conform.

        Engine honors playout trims -> point straight at the cache with
        ``inpoint``/``outpoint`` (zero ffmpeg work). Otherwise -> stream-copy
        the wanted window into the per-plan output: no re-encode, seconds even
        for hour-long assets, and the historic trim-free contract holds.
        ``-ss`` before ``-i`` under ``-c copy`` floors to the previous keyframe
        (<= one GOP early at the canonical profile) — the honest trade for
        engine-agnostic prepared segments; documented in the playout runbook.
        """
        inpoint = segment.inpoint_seconds
        if self._playout_trim_supported:
            emitted = str(cached_ts)
            emit_inpoint = inpoint
            emit_outpoint = (inpoint or 0.0) + segment.duration_seconds
        else:
            # H5 fix (atomic write, second half): write to a ``.tmp`` sibling and
            # ``rename`` into place only on success -- mirrors
            # ``_conform_full_asset_into_cache``'s tmp+replace pattern (every
            # write site under ``conform-cache/`` now follows it, including
            # ``_write_cache_meta``'s sidecar -- item 66 round-3 review).
            # ``output_path`` is now unique per ``prepare()`` call (see that
            # method's docstring), so this is defense-in-depth rather than the
            # primary H5 fix -- but a reader that opens the final path
            # mid-write (this module has no control over when a consumer looks)
            # must never observe a partial file either.
            # F7 fix: prepare() no longer pre-creates the per-plan directory
            # (it may end up removed again if nothing is ever written into it),
            # so the first actual write site must create it lazily.
            output_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_output_path = output_path.with_name(output_path.name + ".tmp")
            args = ["-hide_banner", "-loglevel", "warning"]
            if inpoint is not None:
                args.extend(["-ss", f"{inpoint:g}"])
            args.extend(
                [
                    "-i",
                    str(cached_ts),
                    "-t",
                    f"{segment.duration_seconds:g}",
                    "-c",
                    "copy",
                    "-f",
                    "mpegts",
                    str(tmp_output_path),
                ]
            )
            try:
                result = self._run_ffmpeg(args, cancel_event)
            except SourcePreparationCancelledError:
                tmp_output_path.unlink(missing_ok=True)
                raise
            if result.returncode != 0:
                tmp_output_path.unlink(missing_ok=True)
                raise SourcePrepareError(
                    f"Cached conform copy-out failed for {segment.label!r}; inspect FFmpeg output."
                )
            tmp_output_path.replace(output_path)
            emitted = str(output_path)
            emit_inpoint = None
            emit_outpoint = None
        return (
            EgressSourceSegment(
                label=segment.label,
                path=emitted,
                duration_seconds=segment.duration_seconds,
                inpoint_seconds=emit_inpoint,
                outpoint_seconds=emit_outpoint,
                kind=segment.kind,
                source_ref=segment.source_ref,
                schedule_occurrence_id=segment.schedule_occurrence_id,
            ),
            PreparedSegmentRecord(
                label=segment.label,
                source_path=str(source_path),
                prepared_path=emitted,
                loudness_status=loudness_status,
                measured_lufs=measured_lufs,
                normalized=normalized,
                loudness_method=loudness_method,
            ),
        )

    def _schedule_warm(
        self,
        key: str,
        source_path: Path,
        config: EgressConfig,
        loudness: LoudnessGateResult,
        normalized: bool,
        *,
        media_duration_seconds: float | None = None,
    ) -> None:
        """Warm-behind: populate the full-asset cache without blocking airtime.

        Item 66 round-6 (point 7): if handing ``_job`` to ``self.
        _warm_scheduler`` itself raises, ``key`` is removed from
        ``self._warming`` here too -- see
        ``_schedule_cache_copy_promotion``'s docstring, which shares this
        exact concern (and this exact ``self._warming`` set): a job
        "discarded" before it ever ran would otherwise leave the key
        stuck, silently suppressing every future GENUINE warm for this
        asset for the life of this ``SourcePreparer`` instance.

        U29 makes three properties of this job explicit, because the station
        showed all three were missing: the job's ffmpeg calls get a
        duration-scaled budget (``warm_preparation_timeout_seconds``) instead
        of the flat foreground bound that no long meeting can meet; they run
        at lowered process priority so the warm yields to the live encoders,
        the caption ASR and the air path; and a warm that fails or times out
        puts this cache key on a ``_WARM_FAILURE_BACKOFF_SECONDS`` window so
        the next airings stop re-queuing the attempt that just burned a
        whole-asset conform. Nothing here touches the audio filter chain or
        the conform arguments -- ``_conform_full_asset_into_cache`` and
        ``build_conform_source_args`` build those exactly as before.
        """
        # U29: the warm's OWN budget, scaled to the asset -- see
        # ``warm_preparation_timeout_seconds``. U42 moved WHERE it is computed:
        # no longer here, but inside ``_job``. The caller cannot always supply
        # a duration -- a TRIMMED airing probes nothing at all (the ``if
        # trimmed:`` branch in ``prepare``), and a cache meta can have
        # persisted ``media_duration_seconds: null`` for a later airing to read
        # back -- and ``warm_preparation_timeout_seconds(None, base)`` is the
        # bare base. That is the flat 300 s that killed the live whole-asset
        # conforms ("unknown duration, allowed 300s to conform", six warms in
        # one day), so the job probes the duration itself when it has none.
        # It probes THERE, on the single warm worker, and not here: this method
        # runs inline on the air path's thread (``prepare`` calls it), and an
        # ffprobe must never sit between a segment and airtime. Computing it
        # inside the job keeps the property U29 wanted -- the value handed to
        # ffmpeg and the value the failure warning prints are provably the same
        # number -- because both read the one local the job binds below.
        with self._warming_guard:
            if key in self._warming:
                return
            # U29: the anti-loop. A warm that failed or timed out leaves a
            # deadline for this cache key; until it passes, airings of the same
            # asset must NOT enqueue the warm that just failed. Without this the
            # live symptom was self-sustaining: every airing re-queued the same
            # unwarmable asset, each attempt burned a full whole-asset conform
            # (or its probe) until the bound killed it, and the cache never
            # populated. The entry is dropped once its window has passed, so a
            # genuinely transient cause (contention, a full disk) gets another
            # attempt on a later airing without any manual intervention.
            backoff_until = self._warm_backoff_until.get(key)
            if backoff_until is not None:
                if backoff_until > time.monotonic():
                    return
                del self._warm_backoff_until[key]
            self._warming.add(key)

        def _job() -> None:
            # U42: both names are bound BEFORE the ``try``, so the failure
            # branch below can always print a coherent bound -- including when
            # the probe is itself what raised. ``warm_preparation_timeout_
            # seconds`` is pure arithmetic, so the pre-try computation costs
            # nothing when the caller already supplied a usable duration.
            warm_duration_seconds = media_duration_seconds
            warm_timeout_seconds = warm_preparation_timeout_seconds(
                warm_duration_seconds, self._preparation_timeout_seconds
            )
            try:
                # U42: an unknown duration scales nothing. Probe it here -- on
                # the warm worker, never on the air path -- then size the
                # budget, the conform call and the warning text from what the
                # probe read. ``probe_media_duration_seconds`` is deliberately
                # total (``None`` for every failure mode), so a genuinely
                # unprobeable asset keeps the base bound and the honest
                # "unknown duration" wording instead of inventing one.
                if warm_duration_seconds is None or warm_duration_seconds <= 0:
                    warm_duration_seconds = probe_media_duration_seconds(source_path)
                    warm_timeout_seconds = warm_preparation_timeout_seconds(
                        warm_duration_seconds, self._preparation_timeout_seconds
                    )
                # Item 66 round-4 (Opus review, point 1): this job may have
                # sat in the single-worker warm queue for a while (round-3,
                # point 4) -- by the time it is finally about to run, the
                # identical asset may already be cached (e.g. an untrimmed
                # airing of it promoted its own already-finished conform
                # straight in -- see _promote_finished_conform_into_cache).
                # Re-check right before doing any work and skip the
                # redundant re-conform if it's already there.
                cached_ts = self._cache_dir() / f"{key}.ts"
                # Item 66 round-8 (MEDIUM fix): same fix as
                # ``_schedule_cache_copy_promotion``'s ``_job`` -- a flagless
                # meta is not a genuine full-asset hit (see
                # ``_write_cache_meta``'s docstring); without this the exact
                # same permanent-miss self-heal failure applied here too.
                cached_meta = self._read_cache_meta(key)
                if (
                    cached_ts.is_file()
                    and cached_meta is not None
                    and cached_meta.get("full_asset_conform") is True
                ):
                    return
                populated = self._conform_full_asset_into_cache(
                    key,
                    source_path,
                    config,
                    loudness,
                    normalized,
                    media_duration_seconds=warm_duration_seconds,
                    timeout_seconds=warm_timeout_seconds,
                    lower_priority=True,
                )
            except SourcePreparationCancelledError:
                # U29: a shutdown/pause is not a property of the asset, so it
                # must neither warn nor start a backoff window -- the caller
                # driving the cancellation is already reporting it, and the
                # asset deserves a warm as soon as the station is running
                # again. (Nothing currently passes a cancel_event to a warm;
                # this keeps that true if it ever does.)
                pass
            except Exception as exc:
                # U29: a failed warm must never surface at air -- but it must
                # also not be re-attempted by every airing forever. Record the
                # backoff deadline and report ONCE, as a warning: this is a
                # known, bounded, per-asset condition (the asset is too slow to
                # conform inside its budget on this box, or the box is
                # saturated), not an unexpected crash, so a traceback is noise
                # the operator cannot act on. ``_run_ffmpeg`` has already
                # turned a timeout into a ``SourcePrepareError`` naming the
                # bound it exceeded; ffmpeg's own stderr is in the log.
                retry_seconds = self._record_warm_failure(key)
                # U42: the DURATION THE WARM USED (its own probe's value when
                # the caller had none), so this line and the bound next to it
                # always describe the same attempt.
                duration_text = (
                    "unknown duration"
                    if warm_duration_seconds is None
                    else f"{warm_duration_seconds:g}s of media"
                )
                reason = f"{type(exc).__name__}: {exc}"
                if len(reason) > 300:
                    reason = f"{type(exc).__name__} (details in the ffmpeg log)"
                _LOG.warning(
                    "Conform-cache warm failed for %r (%s, allowed %gs to conform, "
                    "reason: %s); retrying after %gs. Airings of this asset "
                    "keep serving their bounded per-segment conform and the "
                    "full-asset cache entry stays cold in the meantime.",
                    source_path.name,
                    duration_text,
                    warm_timeout_seconds,
                    reason,
                    retry_seconds,
                )
            else:
                # The warm completed the attempt. Only a populated cache entry
                # clears the backoff: ``None`` means the call deliberately did
                # not write one (the per-key lock was held by another conform,
                # or the loudnorm measurement degraded to single-pass), which is
                # a benign, already-logged outcome rather than a failure of this
                # asset's warm.
                if populated is not None:
                    with self._warming_guard:
                        self._warm_backoff_until.pop(key, None)
                        self._warm_failure_counts.pop(key, None)
            finally:
                with self._warming_guard:
                    self._warming.discard(key)

        try:
            _job.__dict__["_warm_key"] = key
            self._warm_scheduler(_job)
        except Exception:
            with self._warming_guard:
                self._warming.discard(key)
            _LOG.exception("Failed to queue conform-cache warm; next airing re-warms.")

    def warm_plan(self, config: EgressConfig, plan: EgressSourcePlan) -> None:
        """U60: pre-conform every asset an UPCOMING plan will air, off the air.

        Live C10, 2026-09-27 21:17:42. The station rolls one scheduled item per
        plan, and an item shorter than the rollover lead collapses the
        next dispatch onto the current plan's own start -- so the incoming
        plan's cold whole-asset conform has the length of the OUTGOING plan as
        its entire runway (measured: a 290s government item, a 383.6s
        first-time conform, ~100s of slate). The automation cannot dispatch any
        earlier; what it CAN do is start the encode long before it dispatches.
        This is the entry point for that: the automation hands the daemon a
        boundary's plan while its own dispatch is still a full lead away, and
        the cache entry the air path will need is resident by the time the
        reload's synchronous prepare asks for it.

        It deliberately does NOT reimplement conforming. Each segment is handed
        to :meth:`_prepare_segment` as a synthetic UNTRIMMED whole-asset
        segment -- the real source path, no in/out points, and the probed media
        duration as its length -- so every derivation the live air path makes
        (the cache key, the loudnorm probe window, the conform arguments, the
        whole-asset promotion gate) runs unchanged and this can never populate
        the cache with an artifact the air path would not have produced itself.
        The jobs are queued on the same single-worker priority queue
        ``_schedule_warm`` uses, at lowered priority and on a duration-scaled
        budget, and they share its per-key dedupe and failure backoff: a warm
        already running for an asset makes a later one a no-op, and one that
        fails does not become an immediately-retried loop.

        Best-effort by construction. This is called from the automation's
        dispatch tick; it must never raise into it, so every per-segment
        decision is defensive and the whole body is bounded by the queue's own
        behavior.
        """

        for segment in plan.segments:
            try:
                self._schedule_plan_warm(config, segment)
            except Exception:
                # The walk is a latency optimisation, not a correctness
                # requirement: the reload this precedes will still prepare
                # synchronously and still air. Never let a look-ahead fault
                # reach the caller's tick.
                _LOG.exception(
                    "U60 plan look-ahead warm could not be queued for %r; the "
                    "air path is unaffected and will prepare normally.",
                    segment.label,
                )

    def _schedule_plan_warm(self, config: EgressConfig, segment: EgressSourceSegment) -> None:
        """Queue one whole-asset warm for one plan segment. See :meth:`warm_plan`."""

        source_path = Path(segment.path).expanduser()
        # The file may not be finalized yet (a recording still being written)
        # or may already be gone; either way there is nothing to warm and the
        # air path will report it in its own terms.
        if not source_path.is_file():
            _LOG.debug(
                "U60 plan look-ahead warm skipped for %r: %s is not a file.",
                segment.label,
                source_path,
            )
            return
        key = self._cache_key(source_path, config)
        if key is None:
            return

        # Exactly ``_schedule_warm``'s admission control, and deliberately the
        # SAME two structures: registering here means the air path's own
        # ``_schedule_warm`` for this asset sees the key as already warming and
        # declines -- which is the point, since a second whole-asset encode
        # racing the first is the one outcome worse than a cold cache.
        with self._warming_guard:
            if key in self._warming:
                if self._warm_scheduler is _default_warm_scheduler:
                    _promote_queued_warm(key)
                return
            backoff_until = self._warm_backoff_until.get(key)
            if backoff_until is not None:
                if backoff_until > time.monotonic():
                    return
                del self._warm_backoff_until[key]
            self._warming.add(key)

        def _job() -> None:
            # Both names bound before any work so the failure branch below can
            # always print a coherent bound, even when the probe is what raised
            # -- same discipline (and same helper) as ``_schedule_warm``'s job.
            warm_duration_seconds: float | None = None
            warm_timeout_seconds = self._preparation_timeout_seconds
            scratch = self._work_dir / config.channel_id / "warm" / uuid.uuid4().hex[:12]
            try:
                cached_ts = self._cache_dir() / f"{key}.ts"
                cached_meta = self._read_cache_meta(key)
                if (
                    cached_ts.is_file()
                    and cached_meta is not None
                    and cached_meta.get("full_asset_conform") is True
                ):
                    return
                # Probed HERE, on the warm worker -- never on the automation
                # thread that queued this. An unprobeable asset is skipped
                # rather than warmed on a guess: without a duration the
                # synthetic segment could not claim to be the whole asset, and
                # the promotion gate would (correctly) refuse to cache it.
                warm_duration_seconds = probe_media_duration_seconds(source_path)
                if warm_duration_seconds is None or warm_duration_seconds <= 0:
                    _LOG.debug(
                        "U60 plan look-ahead warm skipped for %r: no probeable "
                        "media duration, so a whole-asset conform cannot be "
                        "claimed for it.",
                        source_path.name,
                    )
                    return
                warm_timeout_seconds = warm_preparation_timeout_seconds(
                    warm_duration_seconds, self._preparation_timeout_seconds
                )
                scratch.mkdir(parents=True, exist_ok=True)
                # The synthetic whole-asset segment: the REAL source path (so
                # the cache key is the air path's own), no in/out points, and
                # the probed duration as its length -- the two properties the
                # whole-asset promotion gate reads. ``kind``/``source_ref`` are
                # carried through only so any log or record this produces names
                # the item it belongs to.
                synthetic = EgressSourceSegment(
                    label=segment.label,
                    path=str(source_path),
                    duration_seconds=warm_duration_seconds,
                    kind=segment.kind,
                    source_ref=segment.source_ref,
                    schedule_occurrence_id=segment.schedule_occurrence_id,
                )
                # The scratch output is thrown away -- the value of this call
                # is its SIDE EFFECT, the promoted ``conform-cache/{key}.ts``.
                self._prepare_segment(
                    synthetic,
                    config=config,
                    output_path=scratch / "warm.ts",
                    lower_priority=True,
                    timeout_seconds=warm_timeout_seconds,
                )
                if cached_ts.is_file():
                    with self._warming_guard:
                        self._warm_backoff_until.pop(key, None)
                        self._warm_failure_counts.pop(key, None)
            except SourcePreparationCancelledError:
                # A shutdown/pause is not a property of the asset: no warning,
                # no backoff window -- the same rule ``_schedule_warm`` states.
                pass
            except Exception as exc:
                retry_seconds = self._record_warm_failure(key)
                duration_text = (
                    "unknown duration"
                    if warm_duration_seconds is None
                    else f"{warm_duration_seconds:g}s of media"
                )
                reason = f"{type(exc).__name__}: {exc}"
                if len(reason) > 300:
                    reason = f"{type(exc).__name__} (details in the ffmpeg log)"
                _LOG.warning(
                    "U60 plan look-ahead warm failed for %r (%s, allowed %gs to "
                    "conform, reason: %s); retrying after %gs. The air path "
                    "is unaffected and will prepare this asset synchronously if "
                    "it reaches it.",
                    source_path.name,
                    duration_text,
                    warm_timeout_seconds,
                    reason,
                    retry_seconds,
                )
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
                with self._warming_guard:
                    self._warming.discard(key)

        try:
            _job.__dict__["_warm_priority"] = 0
            self._warm_scheduler(_job)
        except Exception:
            with self._warming_guard:
                self._warming.discard(key)
            _LOG.exception(
                "Failed to queue the U60 plan look-ahead warm; the air path "
                "will prepare this asset synchronously if it reaches it."
            )

    def release(self, plan_dir: Path | None) -> None:
        """F3 fix: immediately reclaim ONE specific per-plan directory the caller
        independently knows is safe to remove -- e.g. the daemon calls this for
        the PREVIOUS plan's directory once a content-reload's replacement has
        actually SETTLED (``engine.reload_program``'s ``on_settled`` landing
        "applied" means the old leg is disposed engine-side and its file is no
        longer open for read). ``None`` (no discrete directory was ever tracked
        for that plan -- see ``SourcePreparationReport.plan_dir``) is a no-op.
        Best-effort and silent, same as GC: a locked file (still-open handle on
        Windows, worker slower to let go than expected) just means this
        directory falls through to the next GC pass instead of a hard failure.

        Item 66 round-5 (point 5): this can race a QUEUED
        ``_schedule_cache_copy_promotion`` job that still names a file
        under ``plan_dir`` as its ``finished_output_path`` -- the warm
        queue (round-3, point 4) can leave that job waiting behind other
        work for a while, and this method (or ``_gc_prepared_plan_dirs``
        below, on the very next ``prepare()`` call) has no way to know a
        copy job still needs that file. This is accepted, not fixed: the
        job's own ``shutil.copy2`` simply fails (the source is gone),
        caught and logged same as any other promotion failure -- the
        segment already aired from this file while it existed, and the
        next real airing of that asset re-populates the cache normally.
        """
        if plan_dir is None:
            return
        with contextlib.suppress(OSError):
            shutil.rmtree(plan_dir)

    def _gc_prepared_plan_dirs(
        self, channel_prepared_root: Path, *, keep: frozenset[Path] = frozenset()
    ) -> None:
        """Reclaim old per-plan ``prepared/<uuid>`` directories (H5 fix; budget +
        keep-N added by the F3 follow-up). Best-effort and silent throughout: a
        GC hiccup (a locked file, a permissions error) must never fail the
        ``prepare()`` call it runs inside of -- the worst case is one stale
        directory surviving to the next prepare's GC pass.

        Three layers, in order:

        1. **Keep-N-most-recent** (``_PREPARED_PLAN_DIR_KEEP_N``, by directory
           mtime) is NEVER eligible for removal here, regardless of size or age
           -- one of these could plausibly still be the plan a live worker is
           reading, or one the daemon has armed but not yet confirmed settled.
           ``keep`` (the caller's own actively-tracked directories, e.g. the
           daemon's current on-air plan) is unioned into this protected set,
           so an explicitly-tracked directory is never swept even if it is
           somehow older than the N most recent by mtime.
        2. **Byte budget** (``_prepared_plan_dir_budget_bytes``) across
           whatever is left after (1): oldest-first eviction until the
           channel's ``prepared/`` tree (excluding the protected set) is back
           under budget.
        3. **Age floor** (``_PREPARED_PLAN_DIR_MAX_AGE_S``, 24h) as an absolute
           last resort for anything (1) and (2) did not already catch -- a
           directory this old was very likely already reclaimed by (2) on a
           channel with any reload cadence at all; this floor exists for the
           degenerate case of a channel that barely reloads, where the byte
           budget alone might never trigger.

        F6 (same follow-up): also removes any PRE-UPGRADE flat
        ``prepared/segment-NNNN.ts``/``.tmp`` file sitting directly under
        ``channel_prepared_root`` itself (never inside a plan subdirectory) --
        the layout this fix replaced. Safe unconditionally: no code path
        written after this fix ever reads from that flat location again, and a
        file a pre-upgrade worker still has open simply fails to delete (a
        locked file raises ``OSError``, suppressed) and is retried next pass."""
        try:
            entries = list(channel_prepared_root.iterdir())
        except OSError:
            return
        # F6: pre-upgrade flat files directly under the channel's prepared/ root.
        for entry in entries:
            if entry.is_file() and entry.suffix in (".ts", ".tmp"):
                with contextlib.suppress(OSError):
                    entry.unlink()
        plan_dirs = [entry for entry in entries if entry.is_dir()]

        def _mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        plan_dirs.sort(key=_mtime, reverse=True)  # newest first
        protected = set(plan_dirs[:_PREPARED_PLAN_DIR_KEEP_N]) | (keep & set(plan_dirs))
        candidates = [entry for entry in plan_dirs if entry not in protected]
        # oldest-first for eviction (both the budget and age passes below want
        # to give up the least-recently-used directory first).
        candidates.sort(key=_mtime)

        budget = _prepared_plan_dir_budget_bytes()
        sizes = {entry: _dir_size_bytes(entry) for entry in candidates}
        total = sum(sizes.values())
        remaining: list[Path] = []
        for entry in candidates:
            if total <= budget:
                remaining.append(entry)
                continue
            with contextlib.suppress(OSError):
                shutil.rmtree(entry)
            total -= sizes[entry]

        now = time.time()
        for entry in remaining:
            if now - _mtime(entry) <= _PREPARED_PLAN_DIR_MAX_AGE_S:
                continue
            with contextlib.suppress(OSError):
                shutil.rmtree(entry)

    def _evict_cache_over_budget(self) -> None:
        """Oldest-first eviction of ``.ts`` entries over
        ``CIVICCAST_CONFORM_CACHE_GB``.

        Item 66 round-3 (Opus review) also reaps two kinds of orphaned
        cache-dir detritus that no other code path here ever cleans up:

        * an abandoned intermediate -- any file that is neither a
          ``{key}.ts`` entry nor a ``{key}.json`` sidecar, see
          ``_CACHE_ENTRY_TS_RE`` -- older than
          ``_ORPHAN_CACHE_SCRATCH_MAX_AGE_S`` (1h) is deleted outright; a
          younger one that is still being written to (mtime within
          ``_SCRATCH_LIVENESS_S``) is assumed in-flight and its bytes are
          counted toward the budget below so a burst of concurrent
          warms/promotions can't blow past the configured budget before any
          of them finish and become real ``.ts`` entries;
        * a ``{key}.json`` with no sibling ``{key}.ts`` (a loudness-only
          probe meta -- see ``_write_cache_meta`` -- whose conform never
          followed) older than ``_ORPHAN_CACHE_META_MAX_AGE_S`` (24h) is
          deleted outright.
        """
        budget = _cache_budget_bytes()
        cache_dir = self._cache_dir()
        now = time.time()
        try:
            names = sorted(cache_dir.iterdir())
        except OSError:
            return

        ts_entries: list[tuple[float, Path]] = []
        tmp_bytes = 0
        for path in names:
            try:
                stat = path.stat()
            except OSError:
                continue
            if _CACHE_ENTRY_TS_RE.match(path.name):
                ts_entries.append((stat.st_mtime, path))
                continue
            if _CACHE_ENTRY_META_RE.match(path.name):
                continue  # a sidecar -- handled by the meta sweep below
            age = now - stat.st_mtime
            if age > _ORPHAN_CACHE_SCRATCH_MAX_AGE_S:
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            if age <= _SCRATCH_LIVENESS_S:
                tmp_bytes += stat.st_size

        with contextlib.suppress(OSError):
            for meta_path in cache_dir.glob("*.json"):
                if meta_path.with_suffix(".ts").exists():
                    continue
                try:
                    age = now - meta_path.stat().st_mtime
                except OSError:
                    continue
                if age > _ORPHAN_CACHE_META_MAX_AGE_S:
                    with contextlib.suppress(OSError):
                        meta_path.unlink()

        ts_entries.sort()  # oldest first: by mtime, then by path for a stable tie-break
        total = tmp_bytes
        for _mtime_s, p in ts_entries:
            with contextlib.suppress(OSError):
                total += p.stat().st_size
        for _mtime_s, oldest in ts_entries:
            if total <= budget:
                break
            try:
                size = oldest.stat().st_size
                oldest.unlink()
                oldest.with_suffix(".json").unlink(missing_ok=True)
                total -= size
            except OSError:
                continue

    def prepare(
        self,
        source_plan: EgressSourcePlan,
        config: EgressConfig,
        *,
        cancel_event: threading.Event | None = None,
        protected_plan_dirs: frozenset[Path] | None = None,
    ) -> SourcePreparationReport:
        """Return a canonical, trim-free source plan ready for the persistent encoder.

        H5 fix (measured on real hardware, tester soak): before this fix every
        ``prepare()`` call for a channel wrote its non-cached/trim-copy output to
        the SAME fixed path (``<channel>/prepared/segment-NNNN.ts``, keyed only by
        segment INDEX within its own call, never by which plan the call was
        preparing). For the GStreamer engine (``playout_trim_supported=False``),
        a content-reload's ``prepare()`` call for the NEWLY-due plan therefore
        wrote directly over the exact file the CURRENTLY LIVE worker's ``filesrc``
        was still reading for the plan already on air -- no GStreamer warning, no
        error, just a truncated/rewritten file underneath a live read, surfacing
        only as a downstream ``CTRL stall: no output for 10s`` on the worker with
        nothing in its own log pointing at the cause. Every call now gets its own
        uniquely-named subdirectory (``<channel>/prepared/<uuid>/segment-NNNN.ts``)
        so two ``prepare()`` calls -- however close together, however they
        overlap in wall-clock time -- can never share an output path. See
        ``_gc_prepared_plan_dirs`` for how the resulting directories are
        eventually reclaimed (keep-N-most-recent + a byte budget + an age
        floor as a last resort -- F3 fix), ``release`` for how a caller that
        independently knows a plan is retired can reclaim it immediately, and
        each write site's own comments for the accompanying atomic-write fix
        (H5's second half: a partial write must never be observable at the
        final path either). F7 fix: the directory itself is created lazily (by
        the write sites below, not here) and removed again at the end of this
        call if nothing was ever written into it -- a plan whose every segment
        is ``kind == "live"``, or whose every segment is a cache HIT under
        ``playout_trim_supported=True`` (which points straight at the shared
        conform-cache entry, never writing a per-plan file at all), leaves no
        trace under ``prepared/`` rather than an empty directory nothing will
        ever clean up."""

        channel_prepared_root = self._work_dir / config.channel_id / "prepared"
        channel_prepared_root.mkdir(parents=True, exist_ok=True)
        # Item 5 fix: ask the wired provider (the daemon, if one is
        # configured -- set_protected_plan_dirs_provider) which of this
        # channel's directories are LIVE right now, so GC can never evict one
        # regardless of age, size, or keep-N recency -- not just the
        # keep-N-most-recent heuristic on its own.
        self._raise_if_cancelled(cancel_event)
        protected = protected_plan_dirs
        if protected is None:
            protected = (
                self._protected_plan_dirs_provider(config.channel_id)
                if self._protected_plan_dirs_provider is not None
                else frozenset()
            )
        self._gc_prepared_plan_dirs(channel_prepared_root, keep=protected)
        prepared_dir = channel_prepared_root / uuid.uuid4().hex[:12]
        prepared_segments: list[EgressSourceSegment] = []
        records: list[PreparedSegmentRecord] = []
        # CA-8: filler plans repeat one rendered segment hundreds of times to
        # span the fill target. Preparing (loudness probe + conform) per
        # ENTRY multiplied channel startup ~120x — identical (path, trim)
        # segments prepare exactly once and share the prepared output.
        # Cache assumption (audit ENG-015): repeats of one (path, trim) are
        # intentionally IDENTICAL entries, so the first segment's label and
        # record stand in for later repeats. A plan that repeats one file
        # under different labels would surface the first label only.
        seen: dict[
            tuple[str, float | None, float | None],
            tuple[EgressSourceSegment, PreparedSegmentRecord],
        ] = {}
        for index, segment in enumerate(source_plan.segments, start=1):
            try:
                self._raise_if_cancelled(cancel_event)
            except SourcePreparationCancelledError:
                shutil.rmtree(prepared_dir, ignore_errors=True)
                raise
            if segment.kind == "live":
                prepared_segments.append(segment)
                records.append(
                    PreparedSegmentRecord(
                        label=segment.label,
                        source_path=segment.path,
                        prepared_path=segment.path,
                        loudness_status="not_checked_live_passthrough",
                        measured_lufs=None,
                        normalized=False,
                    )
                )
                continue
            key = (segment.path, segment.inpoint_seconds, segment.outpoint_seconds)
            cached = seen.get(key)
            if cached is not None:
                prepared_segment = cached[0]
                if prepared_segment.schedule_occurrence_id != segment.schedule_occurrence_id:
                    prepared_segment = prepared_segment.model_copy(
                        update={"schedule_occurrence_id": segment.schedule_occurrence_id}
                    )
                prepared_segments.append(prepared_segment)
                records.append(cached[1])
                continue
            prepared_path = prepared_dir / f"segment-{index:04d}.ts"
            try:
                prepared_segment, record = self._prepare_segment(
                    segment,
                    config=config,
                    output_path=prepared_path,
                    cancel_event=cancel_event,
                )
            except SourcePreparationCancelledError:
                shutil.rmtree(prepared_dir, ignore_errors=True)
                raise
            # U36 item 7: never hand the worker a segment that is not real
            # media. ffmpeg can exit 0 and still leave nothing playable -- a
            # copy-out trimmed wholly past the media's end measured a 0-byte
            # file on this station (reports/U36.md section 1.4), and the
            # GStreamer worker's own diagnosis of the discarded boundary reloads
            # was ``gst_decode_bin_expose (): ... all streams without buffers``,
            # which is what an empty or stream-less segment produces. Rejecting
            # it HERE names the source and the in-point and fails preparation,
            # which the caller already handles -- instead of letting the worker
            # abort the reload pre-commit and leave the boundary unprepared.
            rejection = self._prepared_segment_rejection(prepared_segment)
            if rejection is not None:
                _LOG.warning(
                    "Rejecting prepared segment for source %s at in-point %s "
                    "(out-point %s, emitted %s): %s",
                    segment.path,
                    _format_optional_seconds(segment.inpoint_seconds),
                    _format_optional_seconds(segment.outpoint_seconds),
                    prepared_segment.path,
                    rejection,
                )
                shutil.rmtree(prepared_dir, ignore_errors=True)
                raise SourcePrepareError(
                    f"Prepared segment for {segment.path} at in-point "
                    f"{_format_optional_seconds(segment.inpoint_seconds)} is not "
                    f"playable: {rejection}"
                )
            seen[key] = (prepared_segment, record)
            prepared_segments.append(prepared_segment)
            records.append(record)
        # F7 fix: nothing was ever written into prepared_dir (every segment was
        # live-passthrough and/or a playout_trim_supported=True cache hit, which
        # points straight at the shared conform-cache entry) -- remove the
        # directory rather than leave an empty one nothing will ever clean up,
        # and report no plan_dir at all (there is nothing to release or GC).
        reported_plan_dir: Path | None = prepared_dir
        if prepared_dir.exists() and not any(prepared_dir.iterdir()):
            with contextlib.suppress(OSError):
                prepared_dir.rmdir()
                reported_plan_dir = None
        elif not prepared_dir.exists():
            reported_plan_dir = None
        return SourcePreparationReport(
            source_plan=EgressSourcePlan(
                channel_id=source_plan.channel_id,
                segments=prepared_segments,
            ),
            records=tuple(records),
            plan_dir=reported_plan_dir,
        )

    @staticmethod
    def _prepared_segment_rejection(prepared: EgressSourceSegment) -> str | None:
        """U36 item 7: is the segment preparation just EMITTED actually playable?

        Returns a human-readable reason it is not (the caller logs it, naming the
        source and the in-point, and fails preparation), or ``None`` when it looks
        like real media.

        Two checks, both cheap: the file exists and is non-empty, and ffprobe can
        list a stream in it. The second is the one that matters -- ffmpeg exited
        0 and wrote bytes for every discarded boundary reload on this station, and
        the worker's own diagnosis was ``all streams without buffers``, i.e. bytes
        with no decodable stream. A stream listing is a container read, not a
        decode, so this costs one ffprobe per unique (path, in, out) segment.

        An UNANSWERABLE probe is not evidence of bad media: ``None`` from
        :func:`_probe_prepared_segment_decodability` means ffprobe is absent or
        could not run, and rejecting on that would fail every prepared segment on
        a box without ffmpeg -- and would also reject a perfectly playable file
        whose container simply reports no duration, which is what a duration
        probe cannot tell apart from a missing tool. Only a positive "no stream"
        answer rejects.
        """
        emitted_path = Path(prepared.path)
        try:
            size = emitted_path.stat().st_size
        except OSError as exc:
            return f"the emitted file is missing ({exc.__class__.__name__}: {exc})"
        if size == 0:
            return "the emitted file is 0 bytes (the trim produced no media)"
        decodable = _probe_prepared_segment_decodability(emitted_path)
        if decodable is False:
            return (
                "ffprobe found no decodable stream in the emitted file (the worker "
                "logs this as 'all streams without buffers')"
            )
        if decodable is None:
            _LOG.debug(
                "Could not probe emitted segment %s for a decodable stream "
                "(ffprobe unavailable); accepting it.",
                emitted_path,
            )
        return None

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise SourcePreparationCancelledError("Source preparation was cancelled.")

    def _run_ffmpeg(
        self,
        args: list[str],
        cancel_event: threading.Event | None,
        *,
        timeout_seconds: float | None = None,
        lower_priority: bool = False,
    ) -> FfmpegResult:
        """Run one preparation ffmpeg child.

        BETA.10 U29: ``timeout_seconds`` overrides the configured preparation
        timeout for this one call, and ``lower_priority`` asks the runner to
        start the child one scheduling step down (Windows BELOW_NORMAL, POSIX
        ``nice`` -- see ``civiccast.stream._ffmpeg.run_ffmpeg``). Both are
        warm-only knobs: the conform-cache warm is unattended whole-asset
        background work whose budget is scaled to the asset
        (``warm_preparation_timeout_seconds``) and which must yield to the live
        encoders, the caption ASR and the air path. The defaults leave every
        existing caller on exactly the previous behavior -- the configured
        timeout at normal priority.
        """

        effective_timeout = (
            self._preparation_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        self._raise_if_cancelled(cancel_event)
        try:
            if self._ffmpeg_runner is run_ffmpeg:
                result = self._ffmpeg_runner(
                    args,
                    cancel_event=cancel_event,
                    timeout=effective_timeout,
                    lower_priority=lower_priority,
                )
            else:
                result = self._ffmpeg_runner(args)
        except FfmpegCancelledError as exc:
            raise SourcePreparationCancelledError("Source preparation was cancelled.") from exc
        except subprocess.TimeoutExpired as exc:
            raise SourcePrepareError(
                "FFmpeg source preparation timed out after "
                f"{effective_timeout:g}s; the channel will use its "
                "configured fallback until the source can be prepared."
            ) from exc
        self._raise_if_cancelled(cancel_event)
        return result

    def _check_loudness(
        self, *, cancel_event: threading.Event | None, **kwargs: Any
    ) -> LoudnessGateResult:
        self._raise_if_cancelled(cancel_event)
        try:
            if self._loudness_checker is check_streaming_loudness:
                result = self._loudness_checker(**kwargs, cancel_event=cancel_event)
            else:
                result = self._loudness_checker(**kwargs)
        except FfmpegCancelledError as exc:
            raise SourcePreparationCancelledError("Source preparation was cancelled.") from exc
        self._raise_if_cancelled(cancel_event)
        return result

    def _prepare_segment(
        self,
        segment: EgressSourceSegment,
        *,
        config: EgressConfig,
        output_path: Path,
        cancel_event: threading.Event | None = None,
        lower_priority: bool = False,
        timeout_seconds: float | None = None,
    ) -> tuple[EgressSourceSegment, PreparedSegmentRecord]:
        """Conform ``segment``'s source into ``output_path`` and record it.

        BETA.10 U60 adds the same two warm-only knobs ``_run_ffmpeg`` and
        ``_conform_full_asset_into_cache`` already carry (U29):
        ``lower_priority`` and ``timeout_seconds``. They exist so
        :meth:`warm_plan` -- an unattended, off-air whole-asset conform --
        can run this exact code path at a lowered process priority and on a
        duration-scaled budget instead of the flat foreground bound. The
        defaults leave every existing caller on exactly the previous
        behavior, and ``lower_priority`` additionally pins the conform's
        ffmpeg thread count to 1 (see ``conform_threads`` below): a warm must
        never claim half the box from the live encoders it is warming for.
        """
        self._raise_if_cancelled(cancel_event)
        source_path = Path(segment.path).expanduser()
        if not source_path.exists() or not source_path.is_file():
            raise SourcePrepareError(
                f"Egress source {segment.label!r} is missing before preparation: {source_path}."
            )
        key = self._cache_key(source_path, config)
        trimmed = segment.inpoint_seconds is not None or segment.outpoint_seconds is not None
        # U60: one local, read by every stage this call runs, so "this is a
        # lowered-priority warm" is a single decision rather than six
        # independently-drifting ones. See ``_foreground_thread_cap`` for why
        # the foreground path is capped at all; a warm is one step further
        # down (a single thread) because it is the only whole-asset encode
        # that is allowed to run for many minutes beside a live channel.
        conform_threads = 1 if lower_priority else _foreground_thread_cap()

        # Cache HIT: the full-asset conform already exists — no re-encode, no
        # probing (#156: an aired-before program starts within seconds). Trim is
        # applied at playout when the engine supports it, else as a fast
        # stream-copy into the per-plan output.
        meta = self._read_cache_meta(key) if key is not None else None
        if key is not None:
            cached_ts = self._cache_dir() / f"{key}.ts"
            # Item 66 round-7 (point 1, HIGH): ``full_asset_conform`` must be
            # explicitly True, not just present -- see ``_promote_conform_
            # into_cache``'s docstring for the invariant this depends on and
            # ``_write_cache_meta``'s for why a meta file from BEFORE this
            # flag existed (or the probe-only write, which never sets it)
            # reads as False here rather than raising. D42 (source_plan.py's
            # ``_segment_duration``) can make an "untrimmed" segment's own
            # ``duration_seconds`` SHORTER than the asset's real media length
            # (a schedule slot shorter than the asset) -- a round-6 BLOCKER:
            # such a segment's bounded conform used to get promoted as if it
            # were the whole asset (``trimmed`` is False either way, since
            # D42's truncation carries no inpoint/outpoint), so a LATER,
            # longer-slot airing of the same asset would hit this exact
            # cache entry and stream-copy ``-t`` past what it actually holds
            # -- dead air, sticky until the entry's size/mtime changed. A
            # plain ``segment.duration_seconds`` vs ``media_duration_seconds``
            # comparison HERE cannot catch this: ``media_duration_seconds``
            # records the asset's true length, not the short entry's actual
            # bytes, so it would look identical whether or not the ``.ts`` on
            # disk is genuinely the whole asset. The explicit marker (set only
            # at the one choke point that finalizes a ``.ts`` -- see that
            # method) is what actually distinguishes them.
            if cached_ts.is_file() and meta is not None and meta.get("full_asset_conform") is True:
                try:
                    os.utime(cached_ts)  # refresh the eviction clock on hit
                except FileNotFoundError:
                    # Item 66 round-3 (Opus review): a concurrent eviction
                    # pass (_evict_cache_over_budget, running under a
                    # DIFFERENT SourcePreparer/channel sharing this cache
                    # dir) removed the entry between the .is_file() check
                    # above and this utime call. Fall through and treat it
                    # as a MISS instead of returning a cache path that no
                    # longer exists.
                    pass
                else:
                    return self._emit_prepared_from_cache(
                        cached_ts,
                        segment,
                        source_path=source_path,
                        output_path=output_path,
                        loudness_status=str(meta.get("loudness_status", "ok")),
                        measured_lufs=(
                            float(lufs)
                            if isinstance(lufs := meta.get("measured_lufs"), int | float)
                            else None
                        ),
                        normalized=bool(meta.get("normalized", False)),
                        cancel_event=cancel_event,
                        # U20: read back HOW the cached artifact was conformed
                        # so the served segment's own record still says which
                        # shape ran. A meta written before U20 (or by the
                        # probe-only write, which has no conform behind it yet)
                        # carries no such key -> ``None``, the honest answer.
                        loudness_method=_meta_loudness_method(meta),
                    )

        # Item 66 round-6 (point 3): declared here, before the reuse/probe
        # split, so it is ALWAYS bound regardless of which branch runs --
        # both branches' tail calls (``_conform_full_asset_into_cache`` /
        # ``_promote_finished_conform_into_cache`` / ``_schedule_warm``)
        # thread it through to ``_write_cache_meta``.
        media_duration: float | None = None
        # U20: same "always bound" discipline for the conform shape -- which
        # loudnorm form produced the segment this call finally airs. Every
        # conform path below sets it (``_conform_full_asset_into_cache``
        # computes its own; the bounded per-segment path sets it right before
        # its encode); the cache-HIT branch above returns early with its own
        # value read from the meta. ``None`` -- no loudnorm filter ran (an
        # unnormalized/passthrough segment) -- is the honest default.
        loudness_method: str | None = None
        if meta is not None:
            # Item 66 (point 1, Opus review): the full conform isn't cached
            # yet, but a loudness probe for this SAME asset fingerprint
            # already ran and its meta was persisted -- by an earlier
            # segment of this asset in this very prepare() call, or an
            # earlier prepare() call whose warm never landed. Reuse it: an
            # 8-segment plan of one asset used to mean 8 full-file ebur128
            # passes (measured ~46.7s each on a 39-min clip) all on the
            # synchronous start path; now it's exactly one.
            loudness_status = str(meta.get("loudness_status", "ok"))
            loudness = LoudnessGateResult(
                status=loudness_status,
                standard=DEFAULT_LOUDNESS_STANDARD,
                target_lufs=config.loudness_target_lufs,
                used_ffmpeg_wrapper=True,
                measured_lufs=(
                    float(lufs)
                    if isinstance(lufs := meta.get("measured_lufs"), int | float)
                    else None
                ),
                operator_action=(
                    "Loudness is within tolerance."
                    if loudness_status == "ok"
                    else f"Normalize audio to {config.loudness_target_lufs:g} LUFS "
                    "and rerun the loudness gate."
                ),
            )
            normalized = bool(meta.get("normalized", False))
            # Item 66 round-6 (point 3): read the cached media duration back
            # from this SAME meta -- reused here so a downstream conform's
            # own ``_write_cache_meta`` call (which does not itself know the
            # duration) does not have to guess at it. Whichever segment
            # first probed this asset already persisted this value in the
            # exact same atomic write as the loudness fields above; a round-5
            # version of ``_write_cache_meta`` instead did a defensive
            # read-modify-write on every single write to avoid losing this
            # value -- reading it back once here, at the one place loudness
            # itself is also being reused from disk, replaces that.
            media_duration = (
                float(cached_duration)
                if isinstance(cached_duration := meta.get("media_duration_seconds"), int | float)
                else None
            )
        else:
            # Item 66 round-3 (Opus review): bound the probe to a WINDOW
            # instead of decoding/analyzing the whole file. A trimmed
            # segment probes exactly its own wanted window (``-ss
            # <inpoint>`` before ``-i`` for the seek, ``-t <duration>`` after
            # it -- the same convention ``build_conform_source_args`` uses).
            # An untrimmed segment (the full asset) samples
            # ``_UNTRIMMED_LOUDNESS_PROBE_CAP_S`` (120s) of the middle of the
            # asset instead -- round-4 (Opus review, point 2): a HEAD sample
            # (round-3's original shape) can land on cold-open silence/room
            # tone and measure the silence floor, a real field failure that
            # would drive loudnorm's target completely wrong and get
            # memoized for every other segment/airing of the asset.
            silent_asset = False
            # Item 66 round-6 (Opus review): two different ways a floor
            # reading on the FIRST sample can become a trusted silence
            # verdict. ``allow_second_sample`` -- corroborate with a
            # second, genuinely independent sample before trusting it (the
            # "long asset" branch below). ``single_sample_is_conclusive`` --
            # trust the FIRST sample alone, because its window already
            # covers the whole (or nearly the whole) known-length asset, so
            # there is nothing left to corroborate against (the "short,
            # known-duration asset" branch below). Neither applies to a
            # trimmed probe, nor when the real duration is unknown --
            # point 1(a): a single, uncorroborated sample against an asset
            # of UNKNOWN length is never trusted as proof of silence.
            allow_second_sample = False
            single_sample_is_conclusive = False
            if trimmed:
                probe_start_seconds = segment.inpoint_seconds
                probe_duration_seconds = segment.duration_seconds
            else:
                # Item 66 round-5 (Opus review, point 2): ``segment.
                # duration_seconds`` is NOT the asset's real media duration
                # here -- source_plan.py's ``_segment_duration`` returns
                # ``min(slot, playable)`` (or the bare SLOT for an
                # un-probed asset), so a schedule slot shorter than the
                # asset would place the 40%-in sample past the asset's
                # actual EOF (measured: a past-EOF ``-ss`` reads as silence
                # -> -70 LUFS -> an unnecessary floor fallback). Use the
                # REAL media duration instead, via a cheap ffprobe
                # ``-format`` query (not a decode). This branch is only
                # ever reached with ``meta is None`` (the sibling ``if meta
                # is not None:`` branch above already reused a cached
                # measurement and returned before probing at all) -- so
                # there is never an already-cached duration to reuse HERE;
                # ``_write_cache_meta`` below persists this probe's result
                # so a LATER ``prepare()`` call for this asset (once its
                # loudness eventually needs re-probing too, e.g. a
                # re-finalized recording gets a new cache key) still only
                # pays for one ffprobe, not a repeat one, alongside the
                # loudness memo it is written together with.
                media_duration = probe_media_duration_seconds(source_path)
                if media_duration is not None and media_duration > 0:
                    if media_duration <= _SHORT_ASSET_SINGLE_SAMPLE_MAX_S:
                        # Item 66 round-7 (point 2, MEDIUM fix): a single
                        # sample is only conclusive when its own window
                        # (min(duration, 120s) from the very start) IS the
                        # whole file -- i.e. duration itself is at most the
                        # probe cap. Round-6 used ``* 2`` (240s) here, which
                        # let a 120-240s asset's HEAD-only 120s sample stand
                        # in for the whole file when it was really covering
                        # at most half of it (see this constant's own
                        # docstring for the field failure that caught this).
                        probe_start_seconds = 0.0
                        probe_duration_seconds = min(
                            media_duration, _UNTRIMMED_LOUDNESS_PROBE_CAP_S
                        )
                        single_sample_is_conclusive = True
                    else:
                        # Long asset (now also anything just over 120s, not
                        # only >240s -- see the constant's docstring): 40% in,
                        # clamped so a LATER 70%-in resample can still fit
                        # non-overlapping when there IS room for one.
                        #
                        # Item 66 round-8 (LOW fix): the "room for one" cutoff
                        # is NOT 240s. Working the two clamped expressions
                        # through (``probe_start = max(0, min(0.4d, d-120))``,
                        # ``second = min(max(0.7d, probe_start+120), d-120)``,
                        # both below) shows the non-overlap requirement
                        # (``second >= probe_start + 120``) only holds once
                        # ``d >= 400`` -- verified by direct evaluation of both
                        # expressions across the whole range, not just algebra
                        # (see the round-8 review). Below 400s (which fully
                        # covers the 120-240s range this comment used to name,
                        # but also the 240-400s range it missed) the reserved
                        # gap can't fit a second non-overlapping window at
                        # all, and ``second_sample_is_usable``'s explicit
                        # non-overlap check (below) is what actually keeps a
                        # too-close/identical resample from being trusted as
                        # corroboration in that range, rather than this
                        # reservation alone.
                        probe_start_seconds = max(
                            0.0,
                            min(
                                media_duration * _UNTRIMMED_LOUDNESS_PROBE_FRACTION,
                                media_duration - _SHORT_ASSET_SINGLE_SAMPLE_MAX_S,
                            ),
                        )
                        probe_duration_seconds = _UNTRIMMED_LOUDNESS_PROBE_CAP_S
                        allow_second_sample = True
                else:
                    # Item 66 round-6 (Opus review, point 1): the real
                    # duration is genuinely unknown (ffprobe unavailable or
                    # failed) -- round 5's fixed 120s/240s offsets were
                    # PROVEN wrong here: for anything shorter than 120s,
                    # BOTH blind offsets land past the asset's actual end,
                    # read as silence, and get misreported as a genuinely
                    # silent asset (measured: a real 67s/-10.9 LUFS clip
                    # reported as silent). Sample from the very start
                    # instead -- never past EOF for any asset with any
                    # audio at all -- and never corroborate/trust this
                    # single, uncorroborated sample as proof of silence:
                    # there is no independent second window to cross-check
                    # it against when the asset's true length isn't known.
                    probe_start_seconds = 0.0
                    probe_duration_seconds = _UNTRIMMED_LOUDNESS_PROBE_CAP_S
            loudness = self._check_loudness(
                cancel_event=cancel_event,
                media_path=source_path,
                target_lufs=config.loudness_target_lufs,
                tolerance_lufs=config.loudness_tolerance_lufs,
                probe_start_seconds=probe_start_seconds,
                probe_duration_seconds=probe_duration_seconds,
                threads=conform_threads,
            )
            if (
                single_sample_is_conclusive
                and loudness.measured_lufs is not None
                and loudness.measured_lufs <= _LOUDNESS_SILENCE_FLOOR_LUFS
            ):
                # Item 66 round-6 (point 2 companion): the short,
                # known-duration asset's ONE sample already covers the
                # whole (or nearly the whole) file -- no second window
                # exists to corroborate against, so this reading alone is
                # trusted directly.
                silent_asset = True
            elif (
                allow_second_sample
                and loudness.measured_lufs is not None
                and loudness.measured_lufs <= _LOUDNESS_SILENCE_FLOOR_LUFS
            ):
                # Item 66 round-5 (Opus review, point 3): the round-4 shape
                # fell back to a WHOLE-FILE probe here -- measured 46.7s on
                # a 39-minute clip, exactly the kind of synchronous,
                # start-path-blocking work item 66 exists to close. Take a
                # SECOND bounded sample at a different offset instead
                # (``_UNTRIMMED_LOUDNESS_PROBE_FRACTION_2``, 70% into the
                # media) rather than a full decode. ``allow_second_sample``
                # (only True for the "long asset" branch above) guarantees
                # ``media_duration`` is known and positive here, and that
                # there is room for a window starting at least
                # ``_UNTRIMMED_LOUDNESS_PROBE_CAP_S`` after the first one
                # without running past EOF (round-6, point 2's overlap
                # fix). Only if BOTH independent samples land at the
                # silence floor is the asset treated as genuinely silent.
                assert media_duration is not None
                assert probe_start_seconds is not None  # untrimmed: always a concrete float
                second_probe_start = min(
                    max(
                        media_duration * _UNTRIMMED_LOUDNESS_PROBE_FRACTION_2,
                        probe_start_seconds + _UNTRIMMED_LOUDNESS_PROBE_CAP_S,
                    ),
                    media_duration - _UNTRIMMED_LOUDNESS_PROBE_CAP_S,
                )
                # Item 66 round-6 (point 1b): defense in depth -- a window
                # that would start at or past EOF is unusable, not silence,
                # however it got computed. The clamping above should always
                # keep this comfortably non-negative and within bounds; this
                # guard only disables corroboration if that arithmetic is
                # ever wrong, rather than trusting a past-EOF read.
                #
                # Item 66 round-7 (point 2, MEDIUM): also require genuine
                # non-overlap with the first window. Lowering
                # ``_SHORT_ASSET_SINGLE_SAMPLE_MAX_S`` from 240s to 120s (see
                # that constant's docstring) means this "long asset" branch
                # now also runs for assets as short as just over 120s, where
                # two full non-overlapping 120s windows cannot both fit --
                # exactly the "identical/overlapping, never genuinely
                # independent evidence" failure round-6 already fixed, but
                # (round-8 LOW fix) that gap actually runs up to just under
                # 400s, not 240s -- see the sibling comment above this
                # branch for the worked-out boundary. Without this check the
                # arithmetic above can compute a second window that starts AT
                # OR BEFORE the first one ends (or is identical to it), which
                # would count as "corroboration" while providing none.
                second_sample_is_usable = (
                    second_probe_start >= 0.0
                    and second_probe_start + _UNTRIMMED_LOUDNESS_PROBE_CAP_S <= media_duration
                    and second_probe_start >= probe_start_seconds + _UNTRIMMED_LOUDNESS_PROBE_CAP_S
                )
                if second_sample_is_usable:
                    _LOG.info(
                        "Loudness sample for %r measured at the silence floor "
                        "(%.1f LUFS) at %.1fs in; resampling at %.1fs in before "
                        "treating the asset as silent.",
                        source_path.name,
                        loudness.measured_lufs,
                        probe_start_seconds,
                        second_probe_start,
                    )
                    second_loudness = self._check_loudness(
                        cancel_event=cancel_event,
                        media_path=source_path,
                        target_lufs=config.loudness_target_lufs,
                        tolerance_lufs=config.loudness_tolerance_lufs,
                        probe_start_seconds=second_probe_start,
                        probe_duration_seconds=_UNTRIMMED_LOUDNESS_PROBE_CAP_S,
                        threads=conform_threads,
                    )
                    if second_loudness.measured_lufs is not None:
                        # Item 66 round-6 (point 4): only replace the first
                        # (floor) reading if the resample actually produced
                        # a measurement -- if the resample itself failed
                        # (e.g. an ffmpeg error), keep the first reading
                        # rather than overwriting it with an unmeasured
                        # result and raising below.
                        if second_loudness.measured_lufs <= _LOUDNESS_SILENCE_FLOOR_LUFS:
                            # Two independent samples, two different
                            # offsets, both at the floor -- treat as a
                            # genuinely silent asset: never normalize
                            # toward a target that isn't there.
                            silent_asset = True
                        loudness = second_loudness
            if loudness.status != "ok" and loudness.measured_lufs is None:
                raise SourcePrepareError(
                    f"Egress source {segment.label!r} could not be measured for loudness: "
                    f"{loudness.operator_action}"
                )
            normalized = False if silent_asset else loudness.status != "ok"
            if key is not None:
                # Persist the probe result immediately -- BEFORE any conform
                # runs -- so every other segment of this asset (this
                # prepare() call or a later one) skips the probe too, even
                # though the full-asset conform itself may still be a MISS.
                #
                # U65: ``only_if_absent`` -- this is the PROBE-ONLY write. It
                # carries no conform markers, so it must never replace a
                # sidecar a concurrent prepare already published for this same
                # key (a promote, whose write does carry them). See
                # ``_write_cache_meta``'s own docstring for the live failure
                # this closes.
                self._write_cache_meta(
                    key,
                    loudness,
                    normalized,
                    media_duration_seconds=media_duration,
                    only_if_absent=True,
                )

        # Untrimmed MISS: the full-asset conform IS what airs — conform it once,
        # directly into the cache, then emit from the cache (duration truncation
        # applied at playout or via stream-copy per engine capability).
        #
        # Item 66: this branch's conform runs SYNCHRONOUSLY on the automation
        # thread and blocks first ON_AIR (measured 8.5-12+ min on a fresh
        # station) -- only take it when the engine can actually make use of
        # the resulting untrimmed cache object via a playout-side trim
        # (``self._playout_trim_supported``). When the engine cannot trim at
        # playout (the GStreamer engine), fall through to the trimmed-MISS
        # branch below instead: it conforms only the wanted window
        # (``-t segment.duration_seconds``) straight to the prepared file --
        # bounded, not a whole-clip re-encode -- and (point 3) promotes that
        # same conform straight into the cache instead of scheduling a
        # redundant warm.
        if key is not None and not trimmed and self._playout_trim_supported:
            full_asset_cached_ts = self._conform_full_asset_into_cache(
                key,
                source_path,
                config,
                loudness,
                normalized,
                threads=conform_threads,
                media_duration_seconds=media_duration,
                cancel_event=cancel_event,
                timeout_seconds=timeout_seconds,
                lower_priority=lower_priority,
            )
            if full_asset_cached_ts is not None:
                return self._emit_prepared_from_cache(
                    full_asset_cached_ts,
                    segment,
                    source_path=source_path,
                    output_path=output_path,
                    loudness_status=loudness.status,
                    measured_lufs=loudness.measured_lufs,
                    normalized=normalized,
                    cancel_event=cancel_event,
                    # U20/U25: reaching here means ``_conform_full_asset_into_cache``
                    # returned a path, and the ONLY way it does that is a
                    # successful conform that got promoted (its single-pass
                    # fallback returns ``None`` before writing anything). Which
                    # shape that was -- ``"ride"`` or ``"two-pass"`` -- is
                    # decided inside that call, so read it back from the meta
                    # sidecar the promotion wrote, rather than assuming. That
                    # meta is written synchronously before the promotion can
                    # return, so it describes THIS artifact.
                    loudness_method=_meta_loudness_method(self._read_cache_meta(key)),
                )
            # Item 66 round-4 BLOCKER fix (Opus review, point 1): ``None``
            # means a background warm already holds this exact asset's
            # cache lock -- rather than block behind it (potentially tens
            # of minutes, see _conform_full_asset_into_cache's docstring),
            # fall through to the SAME bounded per-segment conform path the
            # GStreamer engine always takes. That path's own tail (below)
            # attempts a best-effort, non-blocking promotion afterward too.

        # Trimmed MISS (first-ever join-in-progress start), an untrimmed
        # miss on an engine that cannot trim at playout
        # (self._playout_trim_supported is False, e.g. GStreamer), OR (round-4)
        # an untrimmed miss on an engine that CAN trim at playout but whose
        # full-asset conform lock was already held by a concurrent warm:
        # either way conform only the wanted window straight to air -- bounded by
        # ``-t segment.duration_seconds`` in build_conform_source_args below,
        # never a whole-clip re-encode. Foreground (synchronous) conforms are
        # thread-capped rather than single-threaded or unbounded (point 2,
        # Opus review, measured on HALO): this call is reachable both on
        # first ON_AIR and on ``EgressDaemon._try_content_reload``'s
        # synchronous prepare while another channel may be on air
        # (``daemon.py`` around line 1839) -- fully serializing it
        # (233s/300s measured) regressed latency there, and leaving it fully
        # unbounded (36.6s/300s) risked starving everything else on the box.
        # H5 fix (atomic write, second half): same tmp+rename pattern as the
        # cache-hit stream-copy branch above -- see that branch's comment.
        # F7 fix: create the per-plan directory lazily -- see the sibling
        # comment in _emit_prepared_from_cache.
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_output_path = output_path.with_name(output_path.name + ".tmp")

        # Candidate A (two-pass loudnorm): when this segment is normalized,
        # measure the SAME window first, then encode with the measured_*
        # parameters so the single-pass loudnorm shortfall on high-LRA material
        # is removed.  The probe pass is bounded by the identical -ss/-t as the
        # encode, so its measurement describes exactly the audio being written.
        # U20: a probe that fails or yields unusable metadata no longer FAILS
        # CLOSED.  It degrades -- ONE warning naming the source, the window and
        # the reason (see ``_measure_loudnorm_metadata``), then the plain
        # single-pass ``loudnorm`` filter, which is exactly what the installed
        # station ships today -- so the segment still airs instead of failing
        # the channel over a measurement pass.  ``loudness_method`` records
        # which of the two shapes actually ran; the tail gate below uses it to
        # keep a degraded artifact out of the whole-asset cache entry.
        measured_loudness: dict[str, str] | None = None
        if normalized and config.loudness_target_lufs is not None:
            # U25: the ride first, exactly as on the full-asset path above --
            # but here it levels only this window (``segment``), which is the
            # point: a join-in-progress start is the airing whose 4-minute
            # stretches the whole-program loudnorm average gets most wrong.
            # ``_ride_conform_tmp`` writes the finished program to
            # ``tmp_output_path`` or degrades (ONE warning) to the loudnorm
            # path below.
            loudness_method = self._ride_conform_tmp(
                source_path=source_path,
                tmp_path=tmp_output_path,
                config=config,
                segment=segment,
                threads=conform_threads,
                cancel_event=cancel_event,
                timeout_seconds=timeout_seconds,
                lower_priority=lower_priority,
            )
            if loudness_method != _LOUDNESS_METHOD_RIDE:
                measured_loudness, loudness_method = self._measure_loudnorm_metadata(
                    source_path=source_path,
                    segment=segment,
                    loudness_target_lufs=config.loudness_target_lufs,
                    threads=conform_threads,
                    cancel_event=cancel_event,
                    timeout_seconds=timeout_seconds,
                    lower_priority=lower_priority,
                )

        if loudness_method != _LOUDNESS_METHOD_RIDE:
            args = build_conform_source_args(
                source_path=source_path,
                output_path=tmp_output_path,
                segment=segment,
                profile=config.canonical_profile,
                copy_video=self._copy_video(source_path, config.canonical_profile, segment),
                loudness_target_lufs=config.loudness_target_lufs if normalized else None,
                threads=conform_threads,
                measured_loudness=measured_loudness,
            )
            try:
                result = self._run_ffmpeg(
                    args,
                    cancel_event,
                    timeout_seconds=timeout_seconds,
                    lower_priority=lower_priority,
                )
            except SourcePreparationCancelledError:
                tmp_output_path.unlink(missing_ok=True)
                raise
            except SourcePrepareError:
                tmp_output_path.unlink(missing_ok=True)
                raise
            if result.returncode != 0:
                tmp_output_path.unlink(missing_ok=True)
                raise SourcePrepareError(
                    f"Egress source {segment.label!r} could not be conformed; inspect FFmpeg output."
                )
        # Item 66 round-3 BLOCKER fix (Opus review): the per-plan file is
        # finished FIRST, unconditionally, before anything else touches the
        # cache. The previous round moved this same tmp file INTO the cache
        # and then copied it back OUT again for the per-plan output -- an
        # extra full-length copy on the blocking path, and if the cache
        # promotion raised (e.g. this entry alone exceeds
        # CIVICCAST_CONFORM_CACHE_GB), the per-plan file no longer existed
        # and the segment failed to air where it used to air fine. Now the
        # segment's own file exists and is ready to air before the cache is
        # touched at all.
        tmp_output_path.replace(output_path)

        if key is not None:
            # Item 66 round-7 (point 1, HIGH fix): an UNTRIMMED segment
            # (inpoint/outpoint both None) is NOT, by definition, the whole
            # asset -- D42 (source_plan.py's ``_segment_duration``, ``min(slot,
            # playable)``) makes an untrimmed segment's own ``duration_seconds``
            # SHORTER than the asset's real media length whenever the
            # schedule slot is shorter than the asset (a 30s slot on a 67s
            # asset produces a 30s bounded conform here, no inpoint/outpoint
            # attached, so ``trimmed`` reads False even though this output is
            # only a fragment). A round-6 BLOCKER promoted that fragment into
            # the persistent cache as if it WERE the whole asset; a later,
            # longer-slot airing of the same asset then hit this exact entry
            # and stream-copied ``-t`` past what it actually held -- dead air,
            # sticky until the entry's size/mtime changed. Only promote this
            # bounded conform directly when it demonstrably covers the whole
            # known media duration (the same tolerance ``source_plan.py``'s
            # own ``_covers_slot`` uses).
            #
            # Item 66 round-8 (HIGH fix): round-7's ``media_duration is None``
            # branch here still promoted -- reasoning that D42 can only ever
            # cap ``segment.duration_seconds`` below the real media length
            # when ``source_plan.py`` itself already knows the asset's
            # duration, so an untrimmed segment reaching here with an UNKNOWN
            # duration was "never capped by D42 to begin with." That
            # reasoning compares the wrong two sources: D42's own cap in
            # ``source_plan.py`` (``_segment_duration``/``_playable_duration``,
            # source_plan.py:523-543) reads ``asset.duration_seconds`` off the
            # DATABASE row, while ``media_duration`` here comes from THIS
            # call's own live ffprobe -- the two can and do disagree. Two real
            # reproductions on an 8s test asset (3s slot, then a 6s slot):
            # (a) ffprobe genuinely unavailable/failing for this call while
            # the DB row still carries a real (shorter) duration -- D42 caps
            # the segment to 3s off the DB row, this call's own probe fails,
            # ``media_duration`` reads ``None`` here, and the round-7 code
            # promoted the 3s fragment as the full 8s asset; (b) a TRIMMED
            # airing runs first and takes the sibling ``if trimmed:`` branch
            # above, which probes nothing (it seeks/duration-limits off the
            # segment's own inpoint/outpoint) and persists
            # ``media_duration_seconds=None`` via ``_write_cache_meta`` --  no
            # failure anywhere -- and the NEXT airing (untrimmed, slot-capped)
            # takes the meta-reuse branch near the top of this method, reads
            # that ``None`` back, and promotes ITS fragment as the whole
            # asset. Both are the exact "DB-known, ffprobe-unknown" poisoning
            # quadrant the round-7 comment dismissed. Fail closed instead:
            # ``media_duration is None`` now NEVER promotes here, regardless
            # of ``trimmed`` -- it always falls through to ``_schedule_warm``,
            # which conforms the WHOLE asset (not this bounded fragment) on a
            # background thread. That job builds its conform with
            # ``build_conform_source_args(segment=None, ...)`` -- no
            # ``-ss``/``-t`` at all (see that function's docstring) -- so
            # unlike this bounded fragment, its output genuinely IS the whole
            # file regardless of what ``media_duration`` measured. Once it
            # finishes, ``_promote_conform_into_cache`` marks the cache entry
            # ``full_asset_conform=True`` -- unconditionally, from the mere
            # fact of having been called (it does not itself measure
            # anything; see its own docstring) -- which is safe here only
            # because the caller reaching it (the warm job) is the one that
            # already guaranteed a trim-free, whole-file conform, never from
            # this unverified fragment. Follow-up (not done this round, listed
            # rather than silently deferred): thread the plan's own known
            # asset duration
            # (the DB row `source_plan.py` already read) through to the
            # segment/``PreparedSegment`` spec so this method stops
            # re-deriving it via a second, independently-fallible ffprobe at
            # all -- see the round-7 review notes for the suggested shape.
            is_full_asset_conform = (
                not trimmed
                and media_duration is not None
                and segment.duration_seconds >= media_duration - _FULL_ASSET_DURATION_TOLERANCE_S
            )
            if is_full_asset_conform and loudness_method != _LOUDNESS_METHOD_SINGLE_PASS_FALLBACK:
                # This bounded conform's output already IS the full-asset
                # conform the persistent cache wants. Populate the cache from
                # the ALREADY-FINISHED, already-airing ``output_path`` via a
                # hard link (queuing a background copy instead if linking
                # fails -- e.g. a cross-volume layout) instead of moving it --
                # see ``_promote_finished_conform_into_cache``'s docstring for
                # the full round-4 shape: non-blocking on lock contention
                # (skipped rather than waited on), and any failure here is
                # logged and swallowed rather than raised -- the segment is
                # already safely on air regardless of whether this succeeds.
                # U20: the ``loudness_method`` half of the gate is what keeps a
                # DEGRADED artifact out of that entry. This window covers the
                # whole asset, so without it a single-pass fallback conform
                # would be promoted and then served to every later airing as
                # the two-pass whole-asset conform; with it, a degraded window
                # promotes nothing and the ``else`` branch below warms the
                # whole asset behind it instead -- and that warm re-measures,
                # so a later run whose probe succeeds populates the genuine
                # two-pass entry. ``None`` (no loudnorm ran at all) stays
                # promotable: an unnormalized asset's whole-file conform is
                # exactly as trustworthy as a normalized one's.
                self._promote_finished_conform_into_cache(
                    key,
                    output_path,
                    source_path,
                    loudness,
                    normalized,
                    media_duration_seconds=media_duration,
                    loudness_method=loudness_method,
                )
            else:
                # A genuinely trimmed miss, OR an untrimmed-but-slot-capped
                # (D42) conform that is only a fragment of the asset, OR (U20)
                # a window that did cover the whole asset but whose loudnorm
                # measurement pass degraded to single-pass -- that last one
                # still airs from ``output_path``, it just must not become the
                # cached whole-asset artifact (see the gate above). Warm the
                # full-asset cache behind it for later airings, same as before
                # item 66; the warm re-measures the whole asset from scratch.
                self._schedule_warm(
                    key,
                    source_path,
                    config,
                    loudness,
                    normalized,
                    media_duration_seconds=media_duration,
                )
        return (
            EgressSourceSegment(
                label=segment.label,
                path=str(output_path),
                duration_seconds=segment.duration_seconds,
                kind=segment.kind,
                source_ref=segment.source_ref,
                schedule_occurrence_id=segment.schedule_occurrence_id,
            ),
            PreparedSegmentRecord(
                label=segment.label,
                source_path=str(source_path),
                prepared_path=str(output_path),
                loudness_status=loudness.status,
                measured_lufs=loudness.measured_lufs,
                normalized=normalized,
                loudness_method=loudness_method,
            ),
        )


_LOUDNORM_MEASURED_KEYS = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")

#: Bump when the loudness normalization method changes so the conform
#: cache cannot serve a stale artifact produced by an older method.
_LOUDNORM_METHOD_VERSION = "loudnorm-v3-ride"

#: ``loudness_method`` recorded on every ``PreparedSegmentRecord`` and in the
#: conform cache's sidecar meta: which shape actually produced the audio.
#: ``ride`` = the U25 speech leveling ride leveled the window and its program
#: was muxed onto the source video -- the only shape that holds the level across
#: every 4-minute stretch, not just the whole-program average.  ``two-pass`` =
#: pass 1 measured the window and the encode ran with its measured_*
#: parameters.  ``single-pass-fallback`` = the measurement pass failed or
#: returned unusable metadata, so the window was conformed with the plain
#: single-pass ``loudnorm`` filter -- byte-for-byte what the installed station
#: ships today -- and the failure was logged as ONE warning instead of being
#: raised (U20).  ``None`` means no loudnorm filter ran at all (an
#: unnormalized/passthrough segment, or a meta sidecar written before the
#: conform landed).
_LOUDNESS_METHOD_RIDE = "ride"
_LOUDNESS_METHOD_TWO_PASS = "two-pass"  # noqa: S105 - label, not a secret
_LOUDNESS_METHOD_SINGLE_PASS_FALLBACK = "single-pass-fallback"  # noqa: S105 - label, not a secret


def _meta_loudness_method(meta: dict[str, Any] | None) -> str | None:
    """Read ``loudness_method`` back out of a conform cache sidecar.

    A cache HIT is the one place the preparer reports a method it did not
    choose in this process, so it must report what the ARTIFACT was actually
    made with rather than a constant.  ``_promote_conform_into_cache`` writes
    the meta before it can return successfully and only a successful promotion
    leaves the artifact at the key, so an entry with no readable method is
    unnormalized (or older than the sidecar): ``None`` is the honest answer.
    """

    if not isinstance(meta, dict):
        return None
    value = meta.get("loudness_method")
    return value if isinstance(value, str) else None


def parse_loudnorm_measurement(stderr: str) -> dict[str, str] | None:
    """Parse the JSON block ffmpeg\'s ``loudnorm ...:print_format=json`` emits.

    Returns a dict containing exactly the five measured keys, or ``None`` when
    the block is absent/unparseable. Values are kept as strings; numeric
    validation happens in :func:`_validated_measured_loudness`.
    """

    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start < 0 or end < 0 or end < start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    out: dict[str, str] = {}
    for key in _LOUDNORM_MEASURED_KEYS:
        if key not in data:
            return None
        out[key] = str(data[key])
    return out


def _validated_measured_loudness(measured: dict[str, str]) -> dict[str, str]:
    """Return a validated copy of measured loudnorm metadata, or raise.

    Every value must be present and parse as a FINITE float.  Bad metadata is
    rejected here, loudly, rather than being passed on to
    ``build_conform_source_args`` as if it described the audio about to be
    encoded.

    U20: this function's own contract is unchanged, but what a caller does with
    the raise changed. ``_measure_loudnorm_metadata`` catches it, logs ONE
    warning, and conforms that window with the plain single-pass ``loudnorm``
    filter -- the behaviour the installed station ships today -- instead of
    refusing to conform at all. So bad metadata still never reaches a two-pass
    encode; it just no longer stops a segment from airing. Callers that DO
    still treat it as fatal: ``build_conform_source_args``, which validates
    again right before the encode.
    """

    if not isinstance(measured, dict):
        raise SourcePrepareError("loudnorm measurement metadata must be a mapping")
    out: dict[str, str] = {}
    for key in _LOUDNORM_MEASURED_KEYS:
        if key not in measured:
            raise SourcePrepareError(f"loudnorm measurement metadata missing {key!r}")
        raw = measured[key]
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise SourcePrepareError(
                f"loudnorm measurement metadata {key}={raw!r} is not numeric"
            ) from None
        if value != value or value in (float("inf"), float("-inf")):
            raise SourcePrepareError(f"loudnorm measurement metadata {key}={raw!r} is not finite")
        out[key] = str(raw)
    return out


def build_loudnorm_probe_args(
    *,
    source_path: Path,
    segment: EgressSourceSegment | None,
    loudness_target_lufs: float,
    threads: int | None = None,
) -> list[str]:
    """First-pass args: measure the SAME window the encode will process.

    ``threads`` mirrors ``build_conform_source_args``: ``None`` leaves ffmpeg's
    default untouched; a value caps the decode.  Synchronous conforms pass
    ``_foreground_thread_cap()`` so a three-channel simultaneous start cannot
    each launch an unthrottled analysis pass; warm-behind conforms pass ``1``
    exactly like their encode.  ``-threads`` is an INPUT/decoder option, so it
    is placed before ``-i`` (same arg-grammar rule the encode path follows).
    """

    args = ["-hide_banner", "-loglevel", "info"]
    if threads is not None:
        args.extend(["-threads", str(threads)])
    if segment is not None and segment.inpoint_seconds is not None:
        args.extend(["-ss", f"{segment.inpoint_seconds:g}"])
    args.extend(["-i", str(source_path)])
    if segment is not None:
        args.extend(["-t", f"{segment.duration_seconds:g}"])
    args.extend(
        [
            "-vn",
            "-af",
            f"loudnorm=I={loudness_target_lufs:g}:LRA=11:TP=-1.5:print_format=json",
            "-f",
            "null",
            "-",
        ]
    )
    return args


def build_conform_source_args(
    *,
    source_path: Path,
    output_path: Path,
    segment: EgressSourceSegment | None,
    profile: CanonicalProfile,
    loudness_target_lufs: float | None = None,
    threads: int | None = None,
    measured_loudness: dict[str, str] | None = None,
    copy_video: bool = False,
) -> list[str]:
    """Build FFmpeg args that conform one media source to the canonical profile.

    ``segment=None`` conforms the WHOLE asset (no ``-ss``/``-t``) — the
    persistent conform-cache unit; trim happens at playout via the ffconcat
    plan.

    ``threads`` (item 66, revised after Opus review): when given, caps the
    encode at that many threads (``-threads <N>``); ``None`` leaves ffmpeg's
    own default untouched. Warm-behind conforms (``_schedule_warm`` /
    ``_conform_full_asset_into_cache``'s default) pass ``threads=1`` so a
    background warm can never starve the on-air encoder. Synchronous
    (foreground) conforms pass ``_foreground_thread_cap()`` instead of
    either extreme -- the original item-66 fix's unconditional single
    thread measured 233s for a 300s foreground conform on HALO vs 36.6s
    fully unthrottled, an unacceptable regression on the synchronous
    start/content-reload path (``daemon.py``'s ``_try_content_reload``,
    which can run this call while another channel is genuinely on air).
    """

    args = ["-hide_banner", "-loglevel", "warning"]
    if segment is not None and segment.inpoint_seconds is not None:
        args.extend(["-ss", f"{segment.inpoint_seconds:g}"])
    args.extend(["-i", str(source_path)])
    if segment is not None:
        args.extend(["-t", f"{segment.duration_seconds:g}"])
    # U25: this used to build the identical string inline.  It is now the one
    # shared definition (``loudness_ride.canonical_video_filter``), which the
    # ride's program mux calls too -- so a ridden conform and a loudnorm
    # conform of the same window cannot drift apart in geometry.
    if not copy_video:
        args.extend(["-vf", canonical_video_filter(profile)])
    if loudness_target_lufs is not None:
        if measured_loudness is None:
            args.extend(["-af", f"loudnorm=I={loudness_target_lufs:g}:LRA=11:TP=-1.5"])
        else:
            measured = _validated_measured_loudness(measured_loudness)
            args.extend(
                [
                    "-af",
                    (
                        f"loudnorm=I={loudness_target_lufs:g}:LRA=11:TP=-1.5"
                        f":measured_I={measured['input_i']}"
                        f":measured_LRA={measured['input_lra']}"
                        f":measured_TP={measured['input_tp']}"
                        f":measured_thresh={measured['input_thresh']}"
                        f":offset={measured['target_offset']}"
                        ":linear=true"
                    ),
                ]
            )
    if threads is not None:
        args.extend(["-threads", str(threads)])
    if copy_video:
        # Probe and emitted stream must name the same video stream even when
        # the source contains another default/higher-resolution video track.
        args.extend(["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy"])
    else:
        args.extend(
            [
                "-c:v",
                profile.video_codec,
                "-b:v",
                f"{profile.video_bitrate_kbps}k",
                "-g",
                str(profile.gop_size),
            ]
        )
    args.extend(
        [
            "-c:a",
            profile.audio_codec,
            "-b:a",
            f"{profile.audio_bitrate_kbps}k",
            "-ar",
            str(profile.audio_sample_rate),
            "-ac",
            str(profile.audio_channels),
            "-f",
            "mpegts",
            str(output_path),
        ]
    )
    return args
