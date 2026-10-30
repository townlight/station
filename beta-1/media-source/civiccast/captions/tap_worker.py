# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Caption tap worker: live broadcast audio -> durable caption review queue.

Beta sprint B6 (decision #1 option A). The egress encoder forks live audio to
rolling WAV segments (:mod:`civiccast.captions.tap`); this worker bridges
those segments into the existing live caption seam
(:class:`~civiccast.captions.worker.LiveCaptionWorker`: pipeline →
two-window stabilization → durable review queue).

House worker shape (same as the finalization/Stage F workers):

- env settings with fail-fast ``from_env``
  (``CIVICCAST_CAPTION_TAP=off|inline|external``, default ``off`` — live
  captioning needs a transcription model, so a station opts in);
- ``run_once`` scans ``<tap_root>/<channel>/chunk-NNNNNN.wav``; a segment is
  consumed only when a newer-numbered segment exists (ffmpeg has moved on),
  so a half-written file is never read;
- consumed segments move to ``<channel>/processed/``, unreadable ones to
  ``<channel>/quarantine/`` — a scan never double-feeds and one bad file is
  never fatal;
- ``run_forever(poll_seconds, stop_event)`` survives and logs scan errors;
- inline mode runs under the app lifespan's ``ThreadSupervisor``; external
  mode is ``python -m civiccast.captions.tap_worker`` against the same env.

Chunk timing derives from the segment index times the configured segment
length, so cue timestamps line up with broadcast time even after restarts.

CAPTIONS ARE BEST EFFORT; PLAYOUT WINS. Three mechanisms enforce that here,
because on a real CPU-only station they did not:

- overload backs OFF (:mod:`civiccast.captions.tap_backoff`) -- a channel that
  cannot keep up is paused for an exponentially growing window instead of
  retrying, and re-logging, every scan;
- ASR concurrency is BOUNDED and hardware-aware -- CPU live captions keep one
  channel's ASR call in flight station-wide, while CUDA live captions allow up
  to three so a three-channel station can meet the five-second segment cadence
  (:func:`default_max_channel_workers`),
  and the CTranslate2 model itself runs with 1-2 intra-op threads,
  core-count-aware and capped
  (:func:`civiccast.captions.runtime.default_live_tap_cpu_threads`), instead
  of the batch/VOD default of "every core";
- the playout workers are spawned at ``ABOVE_NORMAL`` priority class, and the
  Python ASR threads here drop to ``BELOW_NORMAL``. The first of those is the
  load-bearing one; see :func:`_lower_current_thread_priority` for what the
  second does and does NOT cover.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import ctypes
import hashlib
import logging
import math
import os
import re
import threading
import time
import wave
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from civiccast.captions.live_sidecar import (
    CaptionRuntimeState,
    LiveWebVttPublisher,
    active_caption_sidecar,
    publish_caption_runtime_status,
    reset_existing_live_sidecars,
)
from civiccast.captions.models import AudioChunk, CaptionCue
from civiccast.captions.phase_timing import phase_timing_from_env
from civiccast.captions.pipeline import CaptionPipeline
from civiccast.captions.retention import (
    RETENTION_SWEEP_SECONDS,
    CaptionEvidenceRetentionPolicy,
)
from civiccast.captions.review import CaptionReviewAudioEvidence, CaptionReviewStore
from civiccast.captions.review_media import write_caption_review_audio_evidence
from civiccast.captions.runtime import CaptionRuntime
from civiccast.captions.stabilize import CaptionStabilizer
from civiccast.captions.tap_backoff import (
    DEFAULT_BASE_BACKOFF_SECONDS,
    DEFAULT_MAX_BACKOFF_SECONDS,
    CaptionBackoffPolicy,
)
from civiccast.captions.tap_batch_diagnostic import (
    BatchDiagnosticCollector,
    NullBatchDiagnostic,
    batch_diagnostic_from_env,
)
from civiccast.captions.worker import AudioEvidenceFactory, LiveCaptionWorker, ReviewPersistenceMode
from civiccast.egress.gst.audio_tap import read_program_boundary

if TYPE_CHECKING:
    from civiccast.translate.service import TranslationProvider

_LOG = logging.getLogger(__name__)

_SEGMENT_RE = re.compile(r"^chunk-(\d+)\.wav$")

TAP_MODE_OFF = "off"
TAP_MODE_INLINE = "inline"
TAP_MODE_EXTERNAL = "external"
_TAP_MODES = (TAP_MODE_OFF, TAP_MODE_INLINE, TAP_MODE_EXTERNAL)

__all__ = [
    "CaptionTapScanResult",
    "CaptionTapWorker",
    "CaptionTapWorkerSettings",
    "default_max_channel_workers",
]

#: Windows ``THREAD_PRIORITY_BELOW_NORMAL``, applied to the per-channel ASR
#: threads. PARTIAL COVERAGE BY CONSTRUCTION -- read
#: :func:`_lower_current_thread_priority` before relying on it.
_THREAD_PRIORITY_BELOW_NORMAL = -1

#: How long a channel's runtime-status file may go unrewritten while its state
#: is unchanged. A heartbeat, not a poll interval: it keeps ``updated_at`` and
#: a paused channel's ``resume_in_seconds`` countdown moving for an operator
#: who is watching, without paying a durable write every ~2-second scan for a
#: state that has not changed. See ``CaptionTapWorker._publish_status``.
_STATUS_REFRESH_SECONDS = 30.0

#: How often the retention sweep may run, independent of the scan interval.
#: The number itself is owned by the retention policy module, which explains
#: it: ONE cadence for every sweep in the process, this one and the egress
#: readiness sweep (``CaptionRetentionVerdictSource``).
_RETENTION_SWEEP_SECONDS = RETENTION_SWEEP_SECONDS

#: How long the SCAN thread will wait for the FIRST retention verdict before
#: proceeding fail-closed.  Deliberately far below the 5 s segment cadence so a
#: slow verification can never let settled segments accumulate past the max-2
#: backlog gate -- the measured beta.9 N=3 defect was a 16.75 s synchronous
#: first sweep against a 13,550-candidate archive, i.e. >3x the cadence.
_RETENTION_FIRST_VERDICT_WAIT_SECONDS = 1.0

#: Consecutive over-limit scans required before the backlog gate PAUSES a
#: channel (U11 B2).  A one-scan overshoot is a transient, not a collapse.
#:
#: Chosen from the U10 field measurement (2026-09-24, live station): 182
#: overload trips in a day, 70% of them at EXACTLY 3 settled segments against a
#: ``max_backlog_segments`` of 2 -- the smallest overshoot the gate can see --
#: while the ASR pool was idle with no lock waits at trip time.  Per-batch ASR
#: elapsed on that station was p50 1.95 s, p90 5.73 s, MAX 19.08 s, so the
#: transient behind a worst-case batch is a ~19 s stretch with the queue over
#: the limit, i.e. ~9.5 scans at the shipped 2 s poll.  This window is 15 scans
#: = 30 s: 1.57x that tail, while still shedding a genuine collapse in a quarter
#: of the 120 s first pause.
#:
#: EPISTEMIC LIMIT, stated so this number is not overclaimed: the log cannot
#: measure how long an over-limit episode would have lasted unfixed, because
#: every trip today TRUNCATES the episode (it drops the audio and pauses).  This
#: is therefore a bounded engineering judgement on a measured tail, not a
#: measured distribution -- see reports/U11.md.
#:
#: ``1`` restores the pre-U11 contract (pause on the first over-limit scan) and
#: is the operator's escape hatch if the deferred path is ever wrong.
DEFAULT_OVERLOAD_PERSISTENCE_SCANS = 15

#: Waiting tap files normally stay at a 60-second ceiling (12 five-second
#: WAVs). A runtime with a larger ``caption_backlog_grace_seconds`` advertises
#: how long serialized inference can legitimately take; its file ceiling is
#: sized from that grace and the configured segment duration.
DEFAULT_WORKING_AUDIO_LIMIT_SEGMENTS = 12

#: How many CATCH-UP SHEDS a channel may take inside
#: :data:`DEFAULT_CATCH_UP_SHED_WINDOW_SECONDS` before the tap concludes the box
#: genuinely cannot keep up and falls back to the retained pause ladder (U23).
#:
#: WHY 3 IN 300 s. The U11 persistence window is 15 scans = 30 s at the shipped
#: 2 s poll, and that is already 1.57x the worst transient ever measured on a
#: live station (U10 max per-batch ASR 19.08 s; U22 observed 19.5 s), so a
#: single GPU stall cannot produce even ONE shed -- the over-limit stretch would
#: have to persist a whole window first. Three sheds inside five minutes
#: therefore requires at least ~90 s of over-limit scans in that span (three
#: whole persistence windows), which no transient produces. A channel that
#: genuinely cannot keep up sheds roughly once per persistence window (~30-40 s,
#: since a shed leaves exactly a legal batch of work), so it escalates to the
#: pause ladder after ~105 s instead of limping forever at a growing loss.
#:
#: ``1`` escalates on the very first shed; ``0`` disables catch-up entirely and
#: is the operator's escape hatch back to the U11 rule -- pause a channel on the
#: first PERSISTENT overshoot, with none of the catch-up benefit. (``1`` is not
#: that escape hatch, because a limit of one still sheds once before the second
#: over-limit episode trips the ladder.)
DEFAULT_CATCH_UP_SHED_LIMIT = 3

#: Window over which catch-up sheds are counted (see above). Five minutes is
#: long enough to contain a real under-capacity episode and short enough that a
#: station which recovers -- the operator lowers the caption tier, a channel
#: goes off air -- returns to catch-up behaviour instead of carrying a stale
#: verdict forever.
DEFAULT_CATCH_UP_SHED_WINDOW_SECONDS = 300.0


def default_max_channel_workers(runtime: CaptionRuntime | None = None) -> int:
    """How many channels' ASR calls may be IN FLIGHT at once by default.

    MEASURED defect this bound originally replaced (tester DESKTOP-VBMA6O5,
    three channels ON_AIR): a prior flat default of 3, with each
    faster-whisper model built with ``cpu_threads=0`` ("use every core"), was
    part of a control plane burning ~247% of a core while the playout workers
    were starved into their own 10-second stall watchdog.

    CPU live captions remain flat at **1**, station-wide, regardless of core
    count.  A CUDA live runtime uses up to three channel workers, capped by
    the runtime's CTranslate2 ``num_workers``.  That distinction is required
    for the station's three five-second audio streams: serial 2.75-second GPU
    calls take roughly 8.25 seconds per cycle and inevitably build backlog,
    while three GPU calls in flight stay inside the segment interval.

    Item 79 (sandbox candidate 3b, 10 "Caption tap overload" events, the same
    GStreamer-worker-stall cluster as the tester's beta.4 soak) tightened CPU
    operation from the ``one concurrent channel per 8 CPUs, max 3`` formula.
    Every channel shares one
    :class:`~civiccast.captions.runtime.FasterWhisperRuntime` instance.  Its
    CPU live profile retains one CTranslate2 worker and the tap therefore
    retains one Python caller.  Its CUDA live profile has three CTranslate2
    workers, so this tap permits three callers to use that capacity.  A
    station with more ON_AIR channels than its selected bound can still spend
    most of a scan cycle waiting; when a channel's settled backlog stays over
    ``max_backlog_segments`` for the whole persistence window, it SHEDS its
    oldest settled audio and keeps captioning from the newest (U23) -- the
    pause ladder is retained only for a channel that sheds
    ``catch_up_shed_limit`` times inside ``catch_up_shed_window_seconds``, i.e.
    one that genuinely cannot keep up.

    Overridable end-to-end via ``CIVICCAST_CAPTION_TAP_MAX_CHANNEL_WORKERS``,
    which still means what it always has: force a different concurrency for a
    station the operator has personally sized.
    """

    isolated_workers = getattr(runtime, "channel_workers", None)
    if isinstance(isolated_workers, int) and 1 <= isolated_workers <= 3:
        return isolated_workers
    on_cuda = getattr(runtime, "on_cuda", None)
    if runtime is None or not callable(on_cuda) or not on_cuda():
        return 1
    return min(3, max(1, int(getattr(runtime, "num_workers", 1))))


def _resolved_runtime_identity(runtime: object) -> tuple[object, ...]:
    """Caption runtime identity, preferring the LOADED backend over the request.

    ``device``/``compute_type`` on the adapter are the REQUESTED values and are
    NOT rewritten on a successful load (so "auto" stays "auto").  The underlying
    CTranslate2 model knows what it actually loaded, so use that when present and
    fall back to the requested fields otherwise.  Order: requested_device,
    requested_compute_type, loaded_device, loaded_compute_type, on_cuda,
    num_workers.
    """

    req_device = getattr(runtime, "device", "unknown")
    req_compute = getattr(runtime, "compute_type", "unknown")
    on_cuda = getattr(runtime, "on_cuda", None)
    model = getattr(runtime, "_model", None)
    backend = getattr(model, "model", None) if model is not None else None
    if backend is not None and hasattr(backend, "device"):
        # The backend model is authoritative about what it loaded.
        loaded_device = backend.device
        loaded_compute = getattr(backend, "compute_type", "unknown")
        source = "backend-model"
    else:
        # No backend evidence available: report UNAVAILABLE rather than echoing
        # the requested value under a "loaded_" label.  An earlier version
        # substituted the request here, which could print loaded_device=cuda with
        # no evidence that CUDA was ever loaded.
        loaded_device = "unavailable"
        loaded_compute = "unavailable"
        source = "unavailable"
    return (
        req_device,
        req_compute,
        loaded_device,
        loaded_compute,
        (on_cuda() if callable(on_cuda) else None),
        getattr(runtime, "num_workers", "n/a"),
        source,
    )


def _lower_current_thread_priority() -> None:
    """Best-effort ``BELOW_NORMAL`` for the calling ASR thread (Windows).

    WHAT THIS DOES NOT DO, stated plainly because an earlier version of this
    comment claimed a symmetry it does not have: it lowers the PYTHON thread
    that calls ``transcribe`` and nothing else. CTranslate2 runs the actual
    inference on its own intra-op thread pool, created inside the native
    library at whatever priority the process had when the model was
    constructed -- i.e. ``NORMAL``. Those threads are where the CPU is
    genuinely spent, and this call does not reach them. On a saturated box the
    Python thread yields; the CT2 pool does not.

    The real, load-bearing protections for playout are the two that do not
    depend on thread priorities at all: the ASR concurrency bound plus a
    capped ``cpu_threads`` of 1-2 (:func:`civiccast.captions.runtime.default_live_tap_cpu_threads`,
    which limits how many CT2 threads exist in the first place), and the
    overload backoff (which stops the work entirely). This
    call is a cheap extra nudge on top of those, not a mechanism to rely on.
    Lowering the CT2 pool itself would mean running the live ASR in its own
    process at ``BELOW_NORMAL_PRIORITY_CLASS``; that is a larger change than
    this fix and is not attempted here.

    A no-op everywhere but Windows and on any failure: the hint is a
    protection for playout, never a precondition for captions. Cheap enough
    (one syscall) to re-apply per scan, which is required because the caption
    scan builds a fresh :class:`~concurrent.futures.ThreadPoolExecutor` each
    time and therefore does not keep its worker threads.
    """

    if os.name != "nt":
        return
    windll = getattr(ctypes, "windll", None)
    if windll is None:  # pragma: no cover - non-Windows interpreters
        return
    try:
        kernel32 = windll.kernel32
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), _THREAD_PRIORITY_BELOW_NORMAL)
    except Exception:  # pragma: no cover - defensive; never fatal to captions
        _LOG.debug("Could not lower caption ASR thread priority.", exc_info=True)


@dataclass(frozen=True)
class CaptionTapWorkerSettings:
    """Deployment configuration for the caption tap worker."""

    mode: str = TAP_MODE_OFF
    tap_root: Path | None = None
    segment_seconds: float = 5.0
    atomic_segments: bool = False
    overlap_seconds: float = 5.0
    poll_seconds: float = 2.0
    # ``None`` means select from the constructed runtime: one channel on CPU,
    # up to three on CUDA.  An environment value remains an exact override.
    max_channel_workers: int | None = None
    max_backlog_segments: int = 2
    overload_persistence_scans: int = DEFAULT_OVERLOAD_PERSISTENCE_SCANS
    overload_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS
    max_overload_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS
    # U23 catch-up: how many sheds inside the window before the pause ladder
    # (which is now reserved for a channel that genuinely cannot keep up) is
    # re-engaged.  See DEFAULT_CATCH_UP_SHED_LIMIT for the derivation.
    catch_up_shed_limit: int = DEFAULT_CATCH_UP_SHED_LIMIT
    catch_up_shed_window_seconds: float = DEFAULT_CATCH_UP_SHED_WINDOW_SECONDS

    @classmethod
    def from_env(cls) -> CaptionTapWorkerSettings:
        mode = os.environ.get("CIVICCAST_CAPTION_TAP", TAP_MODE_OFF).strip().lower()
        if mode not in _TAP_MODES:
            raise ValueError(
                f"CIVICCAST_CAPTION_TAP must be one of {', '.join(_TAP_MODES)}; got {mode!r}."
            )
        root_raw = os.environ.get("CIVICCAST_CAPTION_TAP_DIR", "").strip()
        if mode != TAP_MODE_OFF and not root_raw:
            raise ValueError(
                "CIVICCAST_CAPTION_TAP_DIR must be set when CIVICCAST_CAPTION_TAP "
                f"is {mode!r} (the directory the egress audio fork writes into)."
            )
        defaults = cls()
        return cls(
            mode=mode,
            tap_root=Path(root_raw) if root_raw else None,
            segment_seconds=_env_float(
                "CIVICCAST_CAPTION_TAP_SEGMENT_SECONDS", defaults.segment_seconds
            ),
            atomic_segments=_env_bool("CIVICCAST_CAPTION_TAP_ATOMIC", defaults.atomic_segments),
            overlap_seconds=_env_float(
                "CIVICCAST_CAPTION_TAP_OVERLAP_SECONDS", defaults.overlap_seconds
            ),
            poll_seconds=_env_float("CIVICCAST_CAPTION_TAP_POLL_SECONDS", defaults.poll_seconds),
            max_channel_workers=(
                _env_int("CIVICCAST_CAPTION_TAP_MAX_CHANNEL_WORKERS", 1)
                if os.environ.get("CIVICCAST_CAPTION_TAP_MAX_CHANNEL_WORKERS", "").strip()
                else None
            ),
            max_backlog_segments=_env_int(
                "CIVICCAST_CAPTION_TAP_MAX_BACKLOG_SEGMENTS",
                defaults.max_backlog_segments,
            ),
            # Same fail-fast family as MAX_BACKLOG_SEGMENTS: both are gate
            # thresholds, and both are meaningless below 1. A value of 1 is a
            # LEGAL choice, not a mistake -- it restores the pre-U11 rule of
            # pausing on the first over-limit scan.
            overload_persistence_scans=_env_int(
                "CIVICCAST_CAPTION_TAP_OVERLOAD_PERSISTENCE_SCANS",
                defaults.overload_persistence_scans,
            ),
            # A count of episodes, so it must be a whole number; but unlike
            # MAX_BACKLOG_SEGMENTS, 0 is legal and meaningful: it restores the
            # pre-U23 rule (pause on the first persistent overshoot) and is the
            # documented escape hatch if catch-up is ever wrong.
            catch_up_shed_limit=_clamped_env_int(
                "CIVICCAST_CAPTION_TAP_CATCH_UP_SHED_LIMIT",
                defaults.catch_up_shed_limit,
                minimum=0,
            ),
            # A duration that only tunes an already-degraded optional feature,
            # so it is CLAMPED rather than fatal -- same reasoning as the
            # backoff windows just below.
            catch_up_shed_window_seconds=_clamped_env_seconds(
                "CIVICCAST_CAPTION_TAP_CATCH_UP_SHED_WINDOW_SECONDS",
                defaults.catch_up_shed_window_seconds,
                minimum=1.0,
            ),
            **_backoff_settings_from_env(
                base_default=defaults.overload_backoff_seconds,
                max_default=defaults.max_overload_backoff_seconds,
            ),
        )


def _backoff_settings_from_env(*, base_default: float, max_default: float) -> dict[str, float]:
    """Backoff windows from the environment, CLAMPED rather than fatal.

    Every other setting on this class fails fast, and should: a bad tap
    directory or an unparseable mode means the operator asked for something
    CivicCast cannot do, and starting anyway would silently caption nothing.

    These two are different in kind. They tune how long an already-degraded
    OPTIONAL feature waits before retrying. ``from_env`` runs inside the app
    lifespan, so raising here does not degrade captions -- it aborts control-
    plane startup and takes the station OFF AIR over a mistyped duration for a
    feature that is explicitly best effort. Refusing to broadcast because
    someone wrote ``60s`` instead of ``60`` is a worse failure than any
    misconfiguration it could be protecting against.

    So: unparseable or non-positive values fall back to the shipped default,
    and a maximum below the base is raised to the base. Each correction is
    logged at WARNING naming the variable, the rejected value and what is
    being used instead, so it is visible rather than silent.
    """

    base = _clamped_env_seconds(
        "CIVICCAST_CAPTION_TAP_OVERLOAD_BACKOFF_SECONDS", base_default, minimum=1.0
    )
    ceiling = _clamped_env_seconds(
        "CIVICCAST_CAPTION_TAP_MAX_OVERLOAD_BACKOFF_SECONDS", max_default, minimum=1.0
    )
    if ceiling < base:
        _LOG.warning(
            "CIVICCAST_CAPTION_TAP_MAX_OVERLOAD_BACKOFF_SECONDS (%s) is below the base "
            "backoff (%s); using the base as the ceiling so the caption tap still "
            "backs off instead of refusing to start.",
            ceiling,
            base,
        )
        ceiling = base
    return {"overload_backoff_seconds": base, "max_overload_backoff_seconds": ceiling}


def _clamped_env_seconds(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        _LOG.warning(
            "%s must be a number; got %r. Using the default of %ss so the station "
            "still starts -- live captions are best effort and must never hold the "
            "control plane down.",
            name,
            raw,
            default,
        )
        return default
    if value < minimum:
        _LOG.warning(
            "%s must be at least %ss; got %s. Using the default of %ss.",
            name,
            minimum,
            value,
            default,
        )
        return default
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number; got {raw!r}.") from exc


def _clamped_env_int(name: str, default: int, *, minimum: int) -> int:
    """Like :func:`_env_int`, but a bad value falls back instead of raising.

    Same split as the seconds parsers above and for the same reason: the values
    still use ``_env_int`` where a wrong value would silently break a core
    invariant (``MAX_BACKLOG_SEGMENTS``), and are clamped here where the knob
    only tunes an already-degraded optional behaviour and a station that
    refuses to start over it would be a worse failure.
    """

    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _LOG.warning(
            "%s must be an integer; got %r. Using the default of %s.",
            name,
            raw,
            default,
        )
        return default
    if value < minimum:
        _LOG.warning(
            "%s must be at least %s; got %s. Using the default of %s.",
            name,
            minimum,
            value,
            default,
        )
        return default
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer; got {raw!r}.") from exc
    if value < 1:
        raise ValueError(f"{name} must be at least 1; got {value}.")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean; got {raw!r}.")


@dataclass(frozen=True)
class CaptionTapScanResult:
    """Outcome of one ``run_once`` scan (or one explicit :meth:`CaptionTapWorker.flush_channel`)."""

    consumed_segments: int = 0
    quarantined_segments: int = 0
    committed_review_items: int = 0
    # Settled segments discarded because the channel was over its backlog
    # limit. Since U23 that is BOTH shapes of the over-limit response: the
    # oldest-settled shed of a catch-up (``max-backlog-catch-up-shed``, captions
    # keep running) and the fail-closed discard of a retained pause
    # (``max-backlog-exceeded``). Read ``overloaded_channels``/``paused_channels``
    # alongside it to tell the two apart -- a shed leaves both empty.
    dropped_overload_segments: int = 0
    # Pending hypotheses the stabilizer expired without re-confirmation: never
    # committed/never on-air, but counted here so the drop is never silent
    # (civiccast/captions/stabilize.py CaptionStabilizer.expired_unconfirmed).
    expired_unconfirmed_cues: int = 0
    channels: tuple[str, ...] = field(default_factory=tuple)
    overloaded_channels: tuple[str, ...] = field(default_factory=tuple)
    # Channels whose ASR is suspended by the overload backoff for this scan --
    # both the ones that overloaded in THIS scan (they are in
    # ``overloaded_channels`` too) and the ones still inside an earlier pause
    # window, whose settled audio is drained without transcription.
    paused_channels: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class _ChannelScanResult:
    published_cues: int = 0
    consumed_segments: int = 0
    quarantined_segments: int = 0
    committed_review_items: int = 0
    expired_unconfirmed_cues: int = 0


class CaptionTapWorker:
    """Deliver live captions using transient audio and bounded live history.

    Review retention is an explicit compatibility/testing option, never the
    ordinary production live path. Recorded review uses its separate pipeline.
    """

    def __init__(
        self,
        *,
        tap_root: Path,
        caption_work_dir: Path,
        runtime: CaptionRuntime,
        review_store: CaptionReviewStore,
        segment_seconds: float = 5.0,
        atomic_segments: bool = False,
        overlap_seconds: float = 5.0,
        max_channel_workers: int | None = None,
        max_backlog_segments: int = 2,
        overload_persistence_scans: int = DEFAULT_OVERLOAD_PERSISTENCE_SCANS,
        catch_up_shed_limit: int = DEFAULT_CATCH_UP_SHED_LIMIT,
        catch_up_shed_window_seconds: float = DEFAULT_CATCH_UP_SHED_WINDOW_SECONDS,
        reviewer_note: str = "Auto-generated from the live broadcast audio tap.",
        translation_provider: TranslationProvider | None = None,
        retain_review_evidence: bool = False,
        retention_policy: CaptionEvidenceRetentionPolicy | None = None,
        backoff_policy: CaptionBackoffPolicy | None = None,
        is_enabled: Callable[[], bool] | None = None,
        monotonic: Callable[[], float] | None = None,
        batch_diagnostic: BatchDiagnosticCollector | NullBatchDiagnostic | None = None,
    ) -> None:
        self._tap_root = tap_root
        self._caption_work_dir = caption_work_dir.expanduser().resolve()
        self._runtime = runtime
        self._retain_review_evidence = retain_review_evidence
        self._review_store = review_store
        self._segment_seconds = segment_seconds
        try:
            caption_backlog_grace_seconds = float(
                getattr(runtime, "caption_backlog_grace_seconds", 0.0)
            )
        except (TypeError, ValueError):
            caption_backlog_grace_seconds = 0.0
        if not math.isfinite(caption_backlog_grace_seconds) or caption_backlog_grace_seconds < 0:
            caption_backlog_grace_seconds = 0.0
        self._caption_backlog_grace_seconds = caption_backlog_grace_seconds
        if caption_backlog_grace_seconds > 0 and segment_seconds > 0:
            self._working_audio_limit_segments = max(
                1, math.ceil(caption_backlog_grace_seconds / segment_seconds)
            )
        else:
            self._working_audio_limit_segments = DEFAULT_WORKING_AUDIO_LIMIT_SEGMENTS
        self._atomic_segments = atomic_segments
        if overlap_seconds <= 0:
            raise ValueError("Caption tap overlap_seconds must be greater than zero.")
        self._overlap_seconds = overlap_seconds
        self._max_channel_workers_override = max_channel_workers
        if max_channel_workers is None:
            max_channel_workers = default_max_channel_workers(runtime)
        if max_channel_workers < 1:
            raise ValueError("Caption tap max_channel_workers must be at least 1.")
        if max_backlog_segments < 1:
            raise ValueError("Caption tap max_backlog_segments must be at least 1.")
        self._max_channel_workers = max_channel_workers
        # Production scans must not wait for ASR to finish.  Keep one bounded
        # executor alive for the worker lifetime instead of constructing a
        # pool per scan and waiting on ``pool.map``.  ``run_once`` remains
        # synchronous by default for the external entry point and the existing
        # unit-test seam; ``run_forever`` selects the non-blocking mode below.
        self._channel_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._channel_executor_workers: int | None = None
        self._channel_executor_lock = threading.RLock()
        # A token is (session generation, settled filename).  Generation is
        # part of the token so a late result from a prior broadcast cannot make
        # a same-numbered chunk in a new session look in-flight.
        self._channel_inflight: dict[str, set[tuple[int, str]]] = {}
        self._channel_inflight_started_at: dict[str, datetime] = {}
        self._channel_futures: dict[
            concurrent.futures.Future[_ChannelScanResult],
            tuple[str, int, frozenset[str]],
        ] = {}
        self._backoff = backoff_policy or CaptionBackoffPolicy()
        # One clock for the whole worker, and the SAME clock the backoff policy
        # runs on, so a test that drives the backoff forward also drives the
        # status heartbeat forward. Monotonic, never wall-clock: a station
        # clock step must not un-pause a channel or fake a heartbeat.
        self._monotonic = monotonic or time.monotonic
        #: channel -> (semantic status key, monotonic time it was published)
        self._published_status: dict[str, tuple[tuple[object, ...], float]] = {}
        # These are operator observations, not speech/caption-latency metrics.
        # Segment timestamps mark when this worker first saw/finished work;
        # exact all-zero PCM is reported as digital silence rather than inferred
        # from the absence of recognized words.
        self._last_seen_segment_index: dict[str, int] = {}
        self._program_tokens: dict[str, str] = {}
        self._last_input_at: dict[str, datetime] = {}
        self._last_processed_at: dict[str, datetime] = {}
        self._pending_since_at: dict[str, datetime] = {}
        self._audio_signal: dict[str, Literal["digital-silence", "audio-present"]] = {}
        #: monotonic time of the last retention sweep, and its last verdict.
        #: ``None`` means "never swept", so the first scan always sweeps.
        self._last_retention_sweep: float | None = None
        #: The retention sweep runs on its own thread so its (measured 4-5 s) work
        #: can never starve the caption scan path into a false overload.  The
        #: verdict it publishes is guarded by a lock; the scan only ever READS it.
        self._retention_thread: threading.Thread | None = None
        self._retention_lock = threading.RLock()
        #: How long the tap waits for an in-flight retention sweep to finish when
        #: its own scan loop exits.  Bounded so a wedged sweep can never hold
        #: shutdown open; the thread is a daemon either way.
        self._retention_shutdown_timeout = 10.0
        #: Initial verification and any explicit refusal gate ASR. During later
        #: rechecks the previous successful verdict permits transient captions
        #: and text-only review, NEVER audio persistence: the persistence guard
        #: also checks in-flight state atomically at the write boundary.
        self._retention_verified = False
        self._retention_in_flight = False
        self._retention_ready = not retain_review_evidence
        self._retention_refusal: str | None = "retention-verification-pending"
        # Consulted on EVERY scan, not once at construction: the operator's
        # switch (``StationProfile.live_captions_enabled``) has to take effect
        # on a station that is on air, and restarting the control plane to
        # apply it is exactly the kind of interruption this whole change
        # exists to avoid. Default: always enabled, so an injected worker in a
        # test or an external-mode process behaves as before.
        self._is_enabled = is_enabled or (lambda: True)
        self._disabled_announced = False
        #: Last caption-runtime identity logged, so the resolved-identity line is
        #: emitted once per distinct identity rather than on every pending scan.
        self._logged_runtime_identity: tuple[object, ...] | None = None
        self._max_backlog_segments = max_backlog_segments
        if overload_persistence_scans < 1:
            raise ValueError("Caption tap overload_persistence_scans must be at least 1.")
        self._overload_persistence_scans = overload_persistence_scans
        #: Monotonic start of the current over-limit episode. Whistle advertises
        #: a finite grace for the station-wide serialized request cycle; the
        #: normal scan-count persistence rule still applies to every runtime.
        self._overload_started_at: dict[str, float] = {}
        #: channel -> consecutive scans whose queued backlog was OVER the limit.
        #: Only this count decides the pause (see the scan loop): a transient
        #: overshoot must be drained, not punished. Touched only from the scan
        #: thread, under the per-channel session lock.
        self._overload_scan_streak: dict[str, int] = {}
        #: Channels already told (once per episode, INFO) that this scan is
        #: behind and is transcribing the oldest segments rather than pausing.
        self._overload_deferral_announced: set[str] = set()
        if catch_up_shed_limit < 0:
            raise ValueError("Caption tap catch_up_shed_limit must not be negative.")
        if catch_up_shed_window_seconds <= 0:
            raise ValueError("Caption tap catch_up_shed_window_seconds must be greater than zero.")
        self._catch_up_shed_limit = catch_up_shed_limit
        # Keep the existing bounded shed budget reachable when Whistle's
        # legitimate serialized cycle exceeds the generic five-minute window.
        # The window must contain the allowed sheds plus the next shed attempt.
        self._catch_up_shed_window_seconds = max(
            catch_up_shed_window_seconds,
            (catch_up_shed_limit + 1) * caption_backlog_grace_seconds
            if caption_backlog_grace_seconds > 0 and catch_up_shed_limit > 0
            else catch_up_shed_window_seconds,
        )
        #: channel -> monotonic timestamps of its recent catch-up sheds. Pruned
        #: to the window whenever it is read or appended, so it cannot grow.
        #:
        #: A shed is NOT cleared by a healthy scan the way the overload streak
        #: is: a shed leaves exactly a legal batch of work in flight, so the
        #: very next scan sees a queue at or under the limit and would erase the
        #: history immediately -- which is precisely the history that has to
        #: survive long enough to tell a transient from a box that cannot keep
        #: up. It expires on its own window, and is dropped at a session
        #: boundary, a flush, and when the ladder takes over (see
        #: ``_clear_catch_up_sheds``).
        self._catch_up_sheds: dict[str, list[float]] = {}
        self._reviewer_note = reviewer_note
        # S13 (T3/M4): the operator-selected translation model, injected at the same
        # DI seam summary/captions use. Threaded into each per-channel LiveCaptionWorker
        # so the running translator consults the operator's selection.
        self._translation_provider = translation_provider
        # Opt-in, default-off phase timing (CIVICCAST_CAPTION_TAP_PHASE_TIMING=1).
        # Inert -- no clock read, no counters -- unless explicitly enabled.
        self._phase_timing = phase_timing_from_env()
        # Opt-in, default-off PER-BATCH diagnostic
        # (CIVICCAST_CAPTION_TAP_BATCH_DIAGNOSTIC=1). Inert unless enabled:
        # no clock read and no record kept when the switch is off.
        self._batch_diagnostic = batch_diagnostic or batch_diagnostic_from_env()
        self._retention_policy = retention_policy or CaptionEvidenceRetentionPolicy.from_system(
            storage_root=self._caption_work_dir
        )
        # One persistent LiveCaptionWorker per channel so the two-window
        # stabilization contract survives across scans.
        self._channel_workers: dict[str, LiveCaptionWorker] = {}
        self._channel_publishers: dict[str, LiveWebVttPublisher] = {}
        self._previous_segments: dict[str, tuple[int, AudioChunk]] = {}
        # Per-channel session generation.  begin_channel_session() bumps it, so a
        # caption result produced by the PREVIOUS session's worker can be told
        # apart from the current one and refused.  Without this, resetting the
        # sidecar was a one-shot write: an in-flight ASR result from the old
        # session could publish AFTER the reset and put the old broadcast's cue
        # back on air (reproduced: reset -> old worker publish -> old cue
        # visible again).
        self._session_generation: dict[str, int] = {}
        self._failed_sessions: set[str] = set()
        # Serialize session reset with publication and short state/file updates,
        # not with ASR or the retention sweep. Locks live for the worker lifetime:
        # replacing a lock during reset would leave an old caller unprotected.
        self._session_locks: dict[str, threading.RLock] = {}
        self._session_locks_guard = threading.Lock()
        reset_existing_live_sidecars(self._caption_work_dir)
        # Same reasoning as the sidecar reset above, one level down: a worker
        # process can start while the channel directories still hold the audio
        # of the broadcast that just ended. See ``_discard_pre_existing_segments``.
        self._discard_pre_existing_segments()
        # One line, logged once at tap start, so a field report or a sandbox
        # run records what box it ran on and what the tap actually sized
        # itself to -- without cross-referencing two separate log lines from
        # two different modules. `cpu_threads` is read via `getattr` because
        # `runtime` is a `CaptionRuntime` Protocol -- an injected test double
        # or the whisper.cpp/Vulkan runtime has no such attribute.
        _LOG.info(
            "Caption tap starting: cpu_count=%s, max_channel_workers=%d, "
            "runtime_workers=%s, live_cpu_threads=%s",
            os.cpu_count(),
            self._max_channel_workers,
            getattr(runtime, "num_workers", "n/a"),
            getattr(runtime, "cpu_threads", "n/a"),
        )

    def run_forever(
        self,
        *,
        poll_seconds: float = 2.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Run the scan loop until ``stop_event`` is set; scan errors are logged."""

        try:
            while stop_event is None or not stop_event.is_set():
                try:
                    # The live station cannot let a slow ASR batch hold the
                    # two-second scan loop hostage.  Results are committed by
                    # these bounded channel workers; the next scan observes
                    # completed files and queues only work that is not already
                    # in flight.
                    self.run_once(wait_for_results=False)
                except Exception:
                    _LOG.exception("Caption tap scan failed; continuing.")
                if stop_event is None:
                    threading.Event().wait(poll_seconds)  # pragma: no cover - loop shape
                else:
                    stop_event.wait(poll_seconds)
        finally:
            # Join the retention sweep thread on loop exit.  It is a daemon, so it
            # cannot block process exit, but a SOFT shutdown (this worker stopped
            # or replaced while the process survives) must not leave a sweep
            # running against the store/dirs a successor worker may also sweep.
            # Joining here makes ThreadSupervisor.stop()'s existing bounded join
            # cover the retention thread transitively.
            if not self.wait_for_retention_sweep(timeout=self._retention_shutdown_timeout):
                # The sweep outlived the bounded wait and is left to process exit
                # (it is a daemon).  No extra guard is needed to prevent a
                # successor from overlapping it: ``_retention_in_flight`` stays
                # True until the running sweep finishes, and the dispatch path
                # returns early on that flag.  Verified by removing this whole
                # branch and re-running the blocked-sweep scenario -- a successor
                # still did not dispatch while the orphan was alive, and sweeps
                # resumed once it completed.
                _LOG.warning(
                    "Caption retention sweep did not finish within %.0fs of tap "
                    "shutdown; left to process exit (successor dispatch is blocked "
                    "by the in-flight flag until it completes).",
                    self._retention_shutdown_timeout,
                )
            self._shutdown_channel_executor()
            close_runtime = getattr(self._runtime, "close", None)
            if callable(close_runtime):
                close_runtime()

    def _ensure_channel_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        """Return the bounded channel executor, creating it after sizing resolves."""

        with self._channel_executor_lock:
            workers = max(1, self._max_channel_workers)
            if self._channel_executor is None or self._channel_executor_workers != workers:
                previous = self._channel_executor
                self._channel_executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="civiccast-caption-channel",
                )
                self._channel_executor_workers = workers
                if previous is not None:
                    # No future is cancelled here: a runtime fallback or a
                    # future operator override must not abandon a chunk that
                    # already entered ASR.  The old pool drains independently;
                    # generation checks still prevent stale publication.
                    previous.shutdown(wait=False, cancel_futures=False)
            return self._channel_executor

    def _submit_channel(
        self,
        args: tuple[str, Path, list[tuple[int, Path]], int, str, bool],
    ) -> concurrent.futures.Future[_ChannelScanResult]:
        """Submit one channel batch and mark its settled files in-flight."""

        channel_id, _channel_dir, segments, generation, _batch_id, _deferred = args
        names = frozenset(path.name for _index, path in segments)
        future = self._ensure_channel_executor().submit(
            self._isolated_process_channel,
            *args,
        )
        with self._channel_executor_lock:
            tokens = self._channel_inflight.setdefault(channel_id, set())
            self._channel_inflight_started_at.setdefault(channel_id, datetime.now(UTC))
            tokens.update((generation, name) for name in names)
            self._channel_futures[future] = (channel_id, generation, names)
        future.add_done_callback(self._channel_future_done)
        return future

    def _channel_future_done(
        self,
        future: concurrent.futures.Future[_ChannelScanResult],
    ) -> None:
        """Release in-flight tokens and surface unexpected worker failures."""

        with self._channel_executor_lock:
            metadata = self._channel_futures.pop(future, None)
            if metadata is None:
                return
            channel_id, generation, names = metadata
            tokens = self._channel_inflight.get(channel_id)
            if tokens is not None:
                tokens.difference_update((generation, name) for name in names)
                if not tokens:
                    self._channel_inflight.pop(channel_id, None)
                    self._channel_inflight_started_at.pop(channel_id, None)
        try:
            future.result()
        except Exception:
            _LOG.exception("Caption tap channel %s future failed outside isolation.", channel_id)

    def _inflight_names(self, channel_id: str) -> frozenset[str]:
        """Return paths owned by any live ASR task for this channel."""

        with self._channel_executor_lock:
            return frozenset(
                name for _generation, name in self._channel_inflight.get(channel_id, ())
            )

    def _channel_has_inflight(self, channel_id: str) -> bool:
        """Return whether a channel already has a batch executing.

        ``LiveCaptionWorker`` carries stabilization state across segments, so
        two batches for the same channel must not run concurrently even when
        their filenames differ.  Sibling channels still use the bounded pool
        concurrently; a same-channel backlog waits for its current batch to
        finish and is then evaluated by the normal fail-closed gate.
        """

        with self._channel_executor_lock:
            return bool(self._channel_inflight.get(channel_id))

    def _note_backlog_depth(self, channel_id: str, depth: int) -> bool:
        """Count this scan for/against the overload persistence window.

        Returns True after the backlog has exceeded ``max_backlog_segments``
        for the configured consecutive-scan window. A runtime may advertise a
        longer finite grace for serialized calls; that deadline is measured
        from the first over-limit scan and must also expire before shedding.
        Any scan at or under the limit clears both timers, so a transient that
        drains never pauses a channel.
        """

        if depth <= self._max_backlog_segments:
            self._reset_overload_persistence(channel_id)
            return False
        streak = self._overload_scan_streak.get(channel_id, 0) + 1
        self._overload_scan_streak[channel_id] = streak
        started_at = self._overload_started_at.setdefault(channel_id, self._monotonic())
        if streak < self._overload_persistence_scans:
            return False
        if self._caption_backlog_grace_seconds <= 0:
            return True
        return self._monotonic() - started_at >= self._caption_backlog_grace_seconds

    def _reset_overload_persistence(self, channel_id: str) -> None:
        """Forget a channel's over-limit streak and its once-per-episode notice."""

        self._overload_scan_streak.pop(channel_id, None)
        self._overload_started_at.pop(channel_id, None)
        self._overload_deferral_announced.discard(channel_id)

    def _announce_deferred_backlog(self, channel_id: str, depth: int) -> None:
        """Say ONCE per episode that this scan is behind but still captioning.

        Deliberately INFO, and deliberately not the overload line: this is the
        tap working as designed under a transient, and an operator grepping the
        log for a pause must not find one here.
        """

        if channel_id in self._overload_deferral_announced:
            return
        self._overload_deferral_announced.add(channel_id)
        _LOG.info(
            "Caption tap is behind for channel %s: %d settled segments is over the "
            "maximum %d on scan %d of %d. Transcribing the oldest %d now and leaving "
            "the rest queued; live captions continue.",
            channel_id,
            depth,
            self._max_backlog_segments,
            self._overload_scan_streak.get(channel_id, 0),
            self._overload_persistence_scans,
            self._max_backlog_segments,
        )

    def _catch_up_sheds_in_window(self, channel_id: str) -> int:
        """Sheds this channel has taken inside the catch-up window (pruning)."""

        now = self._monotonic()
        cutoff = now - self._catch_up_shed_window_seconds
        recent = [at for at in self._catch_up_sheds.get(channel_id, ()) if at > cutoff]
        if recent:
            self._catch_up_sheds[channel_id] = recent
        else:
            self._catch_up_sheds.pop(channel_id, None)
        return len(recent)

    def _clear_catch_up_sheds(self, channel_id: str) -> None:
        """Drop a channel's catch-up shed history (session/flush/escalation)."""

        self._catch_up_sheds.pop(channel_id, None)

    def _catch_up_must_pause(self, channel_id: str) -> bool:
        """Whether the NEXT shed would take this channel past its shed budget.

        False is the normal answer: a shed is the tap catching up, not failing.
        True means the channel has already shed ``catch_up_shed_limit`` times
        inside ``catch_up_shed_window_seconds``, i.e. it is shedding as fast as
        the persistence window allows and still not holding the cadence -- the
        case the retained pause ladder exists for.  A limit of ``0`` is the
        supported "catch-up off" setting: the history is always empty, the
        comparison is true on the first over-limit episode, and the tap behaves
        exactly as it did before this unit.
        """

        return self._catch_up_sheds_in_window(channel_id) >= self._catch_up_shed_limit

    def _shed_backlog_to_catch_up(
        self,
        channel_id: str,
        queued_segments: list[tuple[int, Path]],
    ) -> tuple[list[tuple[int, Path]], int]:
        """Catch up by discarding the OLDEST settled audio, never by pausing.

        Returns ``(kept_segments, shed_count)`` -- the newest
        ``max_backlog_segments`` are kept and submitted as this scan's batch,
        and everything older is unlinked.

        THE SEAM GUARANTEE. A window handed to ASR must be contiguous audio, so
        the batch after a shed must not overlap-join across the gap:

        * ``self._previous_segments`` is popped here.  ``_with_overlap`` would
          already refuse (its ``previous_index + 1 != index`` and
          ``abs(previous.end - current.start) <= tolerance`` guards both fail
          across a shed, which by construction leaves at least one whole segment
          between the last transcribed one and the first kept one) -- the pop is
          the belt to that suspenders, and it is what makes the intent explicit
          rather than incidental.
        * The stabilizer needs no help: ``_observe_live_words`` expires every
          pending word whose ``min(word.end, window_end) <= new_window_start``
          into the review-only ``_expired_unconfirmed`` track, so a forward jump
          ages pre-gap words OUT rather than confirming them, and on-air cues
          require word-time overlap across two windows.

        Keeping ``max_backlog_segments`` (rather than 1) is deliberate: it is
        exactly the size of the legal deferred batch this tap already submits,
        so catching up never issues a LARGER ASR call than the station was
        already making. Keeping 1 would resume on half a stabilizer window
        (no 5 s overlap partner) and would also discard the newest audio's
        overlap context at a moment when the model is already behind.
        """

        keep = self._max_backlog_segments
        if len(queued_segments) <= keep:  # defensive; the caller only sheds over-limit
            return list(queued_segments), 0
        shed = queued_segments[:-keep]
        kept = queued_segments[-keep:]
        with self._session_lock(channel_id):
            self._previous_segments.pop(channel_id, None)
        self._record_discarded_batch(
            channel_id,
            shed,
            outcome="overloaded",
            reason="max-backlog-catch-up-shed",
            queue_depth=len(queued_segments),
        )
        # Measure BEFORE deleting: the span comes from each segment's own WAV
        # header, and ``_chunk_span_seconds`` cannot read a file that is already
        # gone -- it would silently fall back to the nominal cadence and report
        # audio that was not discarded. The order here is what makes the number
        # in the operator's log the truth about this channel's real segments.
        skipped_seconds = sum(max(0.0, self._chunk_span_seconds(path)) for _index, path in shed)
        for _index, segment in shed:
            segment.unlink(missing_ok=True)
        now = self._monotonic()
        self._catch_up_sheds.setdefault(channel_id, []).append(now)
        # ONE WARNING PER EPISODE, naming what an operator needs to correlate
        # the log with the archive: which channel, how much was skipped, and
        # that captions are still running. Deliberately NOT the overload line --
        # grepping for a pause must not find one here.
        _LOG.warning(
            "Caption tap catch-up for channel %s: %d settled segments stayed over "
            "the maximum %d for the whole %d-scan window. Discarded the oldest %d "
            "segment(s) (%.1fs of audio, indices %d-%d) and resumed at the newest "
            "%d. Live captions continue; the pause is not engaged (shed %d of %d "
            "in %.0fs).",
            channel_id,
            len(queued_segments),
            self._max_backlog_segments,
            self._overload_persistence_scans,
            len(shed),
            skipped_seconds,
            shed[0][0],
            shed[-1][0],
            len(kept),
            self._catch_up_sheds_in_window(channel_id),
            self._catch_up_shed_limit,
            self._catch_up_shed_window_seconds,
        )
        return kept, len(shed)

    def _chunk_span_seconds(self, path: Path) -> float:
        """Duration of a queued segment for the shed log, best effort.

        The header only -- no PCM is read for a log line. A segment that cannot
        be read is reported as one cadence of audio rather than blocking the
        catch-up, which must never fail because a log line could not be
        formatted.
        """

        try:
            with wave.open(str(path), "rb") as handle:
                frames = handle.getnframes()
                rate = handle.getframerate()
            if rate > 0 and frames > 0:
                return frames / rate
        except Exception:  # pragma: no cover - defensive; logging must not fail
            _LOG.debug("Could not read a shed segment's duration: %s", path, exc_info=True)
        return self._segment_seconds

    def _invalidate_inflight_for_overload(self, channel_id: str) -> None:
        """Refuse stale ASR publication when a queued backlog trips fail-closed."""

        if not self._inflight_names(channel_id):
            return
        with self._session_lock(channel_id):
            self._session_generation[channel_id] = self._session_generation.get(channel_id, 0) + 1
            self._previous_segments.pop(channel_id, None)
        _LOG.warning(
            "Caption tap channel %s had ASR work in flight when its queued "
            "backlog exceeded the fail-closed limit; stale results will be dropped.",
            channel_id,
        )

    def _shutdown_channel_executor(self) -> None:
        """Stop accepting caption work without making service shutdown wait on ASR."""

        with self._channel_executor_lock:
            executor = self._channel_executor
            self._channel_executor = None
            self._channel_executor_workers = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    @contextmanager
    def _timed_session_lock(self, channel_id: str) -> Iterator[threading.RLock]:
        """Session lock with ACQUISITION time recorded separately from work.

        Waiting for a lock and working while holding it demand different fixes,
        so the wait is its own phase.
        """

        with self._phase_timing.wait("wait_session_lock", channel=channel_id):
            lock = self._session_lock(channel_id)
            lock.acquire()
        try:
            yield lock
        finally:
            lock.release()

    @contextmanager
    def _timed_retention_lock(self) -> Iterator[threading.RLock]:
        """Retention lock with ACQUISITION time recorded separately from work."""

        with self._phase_timing.wait("wait_retention_lock"):
            self._retention_lock.acquire()
        try:
            yield self._retention_lock
        finally:
            self._retention_lock.release()

    def _session_lock(self, channel_id: str) -> threading.RLock:
        with self._session_locks_guard:
            lock = self._session_locks.get(channel_id)
            if lock is None:
                lock = threading.RLock()
                self._session_locks[channel_id] = lock
            return lock

    def begin_channel_session(self, channel_id: str) -> None:
        """Reset a channel atomically with respect to its old caption results."""

        with self._session_lock(channel_id):
            # A failed disk reset is not repaired by starting a new publisher on
            # the next scan. Only a successful explicit session reset can re-arm.
            self._failed_sessions.add(channel_id)
            self._begin_channel_session_locked(channel_id)
            self._failed_sessions.discard(channel_id)

    def _begin_channel_session_locked(self, channel_id: str) -> None:
        """Fail closed when a channel starts a new live broadcast session.

        ``reset_existing_live_sidecars`` is a PROCESS-start guard.  A worker
        process can outlive multiple channel sessions -- a channel stops, another
        starts, and the sidecar from the previous broadcast is still sitting in
        ``active.vtt``.  Publication must be session-scoped: the new session must
        start from an empty sidecar and empty stabilizer state, or the feed will
        treat prior cues as current until something else happens to clear them.
        The Blackwell beta.8 short run hit that exact state: a fresh Public
        session inherited 12 stale cues from an earlier broadcast.

        Safe to call for an unknown channel and safe to call repeatedly.
        """

        # The same lock covers generation comparison + VTT publication. A publish
        # already writing finishes BEFORE this reset; any later one is refused.
        self._session_generation[channel_id] = self._session_generation.get(channel_id, 0) + 1
        self._last_seen_segment_index.pop(channel_id, None)
        self._last_input_at.pop(channel_id, None)
        self._last_processed_at.pop(channel_id, None)
        self._pending_since_at.pop(channel_id, None)
        self._audio_signal.pop(channel_id, None)
        self._channel_workers.pop(channel_id, None)
        self._channel_publishers.pop(channel_id, None)
        self._previous_segments.pop(channel_id, None)
        self._backoff.forget(channel_id)
        # The over-limit streak belongs to the session that just ended, exactly
        # like the backoff state above it -- and so does the catch-up shed
        # budget: a channel that comes back on air is a different mix of load
        # and must not escalate on its first shed of the new session.
        self._reset_overload_persistence(channel_id)
        self._clear_catch_up_sheds(channel_id)
        LiveWebVttPublisher(active_caption_sidecar(self._caption_work_dir, channel_id)).reset()
        # Discard the channel's leftover segments: they belong to the PREVIOUS
        # session and can never air in this one.  This is the session-scoped
        # analogue of the sidecar reset above.
        #
        # WHY THIS IS SOUND HERE (and only here): the daemon fires
        # channel_start_hook and only THEN calls _start() (see
        # EgressDaemon._process_command), so no writer for the new session exists
        # yet -- every chunk present belongs to the previous session or process.
        # Leaving them makes a fresh session inherit the old backlog, and >2
        # settled segments then fail-closed into a 120 s pause before any new
        # audio existed.
        #
        # SCOPE LIMIT, deliberately not overclaimed: this runs ONLY on an explicit
        # START command.  It therefore does NOT explain the 04:22:15 startup
        # overload, where no START was issued and the hook never ran -- that
        # event's attribution stays PROVISIONAL.  A first-scan/"restart without
        # START" variant is NOT implemented, because the WAV writer is a separate
        # per-channel GStreamer subprocess, so file mtime cannot establish which
        # process produced a chunk.
        self._discard_settled_segments(channel_id)

    def flush_channel(self, channel_id: str) -> CaptionTapScanResult:
        """Commit every cue still pending for one channel at stream end.

        Call this when the caller knows a channel's audio has ended (channel
        stop, station shutdown) so the stabilizer's last unconfirmed
        hypotheses are not lost silently -- ``run_once``/``run_forever`` only
        ever look forward and there is no second transcription pass after
        audio ends. Flushed cues persist through the exact same review-store
        and active-VTT publication path as a normal scan. Safe to call on a
        channel with no worker yet (returns zero counts) and safe to call
        twice (the second call flushes nothing new).
        """

        # The channel's audio has ended, so its overload history is spent: a
        # channel that comes back on air starts from the base delay, not from
        # whatever escalation its previous broadcast left behind.
        with self._session_lock(channel_id):
            self._backoff.forget(channel_id)
            self._clear_catch_up_sheds(channel_id)
            worker = self._channel_workers.get(channel_id)
            generation = self._session_generation.get(channel_id, 0)
        if worker is None:
            return CaptionTapScanResult(channels=(channel_id,))
        result = worker.flush()
        with self._session_lock(channel_id), self._retention_lock:
            if self._retention_ready or self._retention_in_flight:
                self._publish_cues(channel_id, generation, worker.committed_cues())
        return CaptionTapScanResult(
            committed_review_items=len(result.committed_review_items),
            expired_unconfirmed_cues=len(result.expired_unconfirmed_cues),
            channels=(channel_id,),
        )

    def run_once(self, *, wait_for_results: bool = True) -> CaptionTapScanResult:
        """Scan every channel directory once and consume settled segments.

        ``run_once`` remains synchronous by default for the external ``--once``
        entry point and existing callers.  The long-lived station loop passes
        ``wait_for_results=False`` so a slow ASR batch cannot hold the next
        scan, channel rollover, or playout watchdog hostage.
        """

        consumed = 0
        quarantined = 0
        committed = 0
        dropped = 0
        expired = 0
        channels: list[str] = []
        overloaded_channels: list[str] = []
        paused_channels: list[str] = []
        # Retention runs BEFORE the enabled check, deliberately and in this
        # order. It prunes audio this station has ALREADY recorded --
        # `<channel>/processed/` and the review evidence -- under the station's
        # retention schedule (spec 4.3). Switching live captions off is a
        # decision about future transcription, never a licence to stop
        # deleting what is already on disk: gating pruning behind the switch
        # would freeze every retention clock for as long as the switch is off,
        # which is the exact opposite of what an operator turning captions off
        # is asking for. (``enforce_discovered`` tolerates a tap root that does
        # not exist yet, so this needs no directory guard.)
        with self._phase_timing.phase("retention_dispatch"):
            if self._retain_review_evidence:
                self._sweep_retention()
        # The enabled check runs BEFORE the tap-directory check (round-2 review
        # MAJOR 3). With live captions off on a station whose tap root was
        # never created -- or was swept by an operator cleaning up -- the old
        # order returned here without ever reaching ``_run_disabled``, so a
        # stale ``active.vtt`` from before the switch was thrown was never
        # blanked, and the caption feed kept re-reading and re-sending every
        # cue in it every 2 s forever (``caption_feed`` only marks a cue seen
        # on a successful push, and with no embed leg there is nothing to
        # push into).
        if not self._is_enabled():
            return self._run_disabled()
        self._disabled_announced = False
        if not self._tap_root.is_dir():
            return CaptionTapScanResult()
        with self._retention_lock:
            retention_ready = self._retention_ready
            retention_in_flight = self._retention_in_flight
            retention_refusal = self._retention_refusal
        if not retention_ready and not retention_in_flight:
            channels = sorted(path.name for path in self._tap_root.iterdir() if path.is_dir())
            for channel_id in channels:
                with self._session_lock(channel_id):
                    for _index, segment in self._settled_segments(self._tap_root / channel_id):
                        segment.unlink(missing_ok=True)
                    try:
                        self._clear_channel_captions(channel_id)
                        self._publish_status(
                            channel_id,
                            state="storage-refused",
                            backlog_segments=0,
                            refusal_reason=retention_refusal,
                        )
                    except OSError:
                        # One broken sidecar directory must not prevent draining
                        # the other channels' newly arriving raw audio.
                        _LOG.exception("Cannot clear refused captions for channel %s", channel_id)
            return CaptionTapScanResult(channels=tuple(channels))
        # (channel, dir, batch segments, generation, batch_id, deferred) -- the
        # last flag marks a batch submitted from an OVER-limit queue, which must
        # not be counted as recovery evidence by the backoff ladder.
        pending: list[tuple[str, Path, list[tuple[int, Path]], int, str, bool]] = []
        for channel_dir in sorted(p for p in self._tap_root.iterdir() if p.is_dir()):
            channel_id = channel_dir.name
            with self._session_lock(channel_id):
                token, _first_segment = read_program_boundary(channel_dir)
                if token != self._program_tokens.get(channel_id, ""):
                    self._clear_channel_captions(channel_id)
                    self._program_tokens[channel_id] = token
                if channel_id in self._failed_sessions:
                    # The daemon inhibits this session's audio tap, but discard
                    # any settled leftovers even if sidecar storage stays broken.
                    for _index, segment in self._settled_segments(channel_dir):
                        segment.unlink(missing_ok=True)
                    continue
                with self._phase_timing.phase("scan_settle_and_backlog_gate", channel=channel_id):
                    segments = self._settled_segments(channel_dir)
                if segments:
                    newest_index = segments[-1][0]
                    if newest_index > self._last_seen_segment_index.get(channel_id, -1):
                        observed_at = datetime.now(UTC)
                        last_input_at = self._last_input_at.get(channel_id)
                        last_processed_at = self._last_processed_at.get(channel_id)
                        caught_up = (
                            last_input_at is not None
                            and last_processed_at is not None
                            and last_processed_at >= last_input_at
                            and not self._channel_has_inflight(channel_id)
                        )
                        if channel_id not in self._pending_since_at or caught_up:
                            self._pending_since_at[channel_id] = observed_at
                        self._last_seen_segment_index[channel_id] = newest_index
                        self._last_input_at[channel_id] = observed_at
                    else:
                        self._pending_since_at.setdefault(channel_id, datetime.now(UTC))
                if not segments:
                    # No settled audio at all means nothing is queued, which is
                    # under the limit -- and the streak must be CONSECUTIVE, so
                    # a healthy gap clears it rather than merely failing to
                    # advance it.  (A genuinely behind channel never reaches
                    # here: the deferral below leaves the excess queued and the
                    # settle guard leaves the newest file, so only a channel
                    # that has caught up can have an empty scan.)
                    self._reset_overload_persistence(channel_id)
                    if not self._channel_has_inflight(channel_id):
                        self._pending_since_at.pop(channel_id, None)
                    self._publish_status(
                        channel_id,
                        state="within-capacity",
                        backlog_segments=0,
                    )
                    continue
                channels.append(channel_id)
                inflight_names = self._inflight_names(channel_id)
                queued_segments = [item for item in segments if item[1].name not in inflight_names]
                if self._channel_has_inflight(channel_id):
                    # Count the depth even while a batch runs.  "Has this
                    # station been behind for the whole persistence window" is
                    # just as true mid-batch as between batches -- and a slow
                    # batch IS the measured shape of the U10 defect (p90 5.7 s,
                    # max 19.08 s), so ignoring in-flight scans would blind the
                    # window to the case it exists for.  The trip itself is only
                    # ever taken on an idle scan, below.
                    #
                    # Preserve per-channel ordering and the stateful
                    # stabilizer.  The current batch owns its paths; new
                    # arrivals remain queued for the next scan after it ends.
                    self._note_backlog_depth(channel_id, len(queued_segments))
                    self._publish_status(
                        channel_id,
                        state="within-capacity",
                        backlog_segments=len(queued_segments),
                    )
                    continue
                if not queued_segments:
                    self._note_backlog_depth(channel_id, 0)
                    self._publish_status(
                        channel_id,
                        state="within-capacity",
                        backlog_segments=0,
                    )
                    continue
                if self._backoff.is_paused(channel_id):
                    # A paused channel is draining, not over-limit: it must not
                    # carry a streak into the scan where it is released.
                    self._reset_overload_persistence(channel_id)
                    self._record_discarded_batch(
                        channel_id,
                        queued_segments,
                        outcome="discarded",
                        reason="paused-drain",
                        queue_depth=len(queued_segments),
                    )
                    dropped += self._drain_paused_channel(channel_id, channel_dir, queued_segments)
                    paused_channels.append(channel_id)
                    continue
                if len(queued_segments) > self._max_backlog_segments:
                    if not self._note_backlog_depth(channel_id, len(queued_segments)):
                        # TRANSIENT, not a collapse (U11 B2): the overshoot has
                        # not persisted for the whole window, so captions keep
                        # flowing.  Only the OLDEST ``max_backlog_segments`` are
                        # transcribed, so the ASR call size is exactly what a
                        # legal batch would be -- the deferral spends no more
                        # CPU per cycle than the station already spends, and the
                        # excess stays queued instead of being deleted.
                        self._announce_deferred_backlog(channel_id, len(queued_segments))
                        deferred = queued_segments[: self._max_backlog_segments]
                        batch_id = self._begin_batch(
                            channel_id,
                            self._session_generation.get(channel_id, 0),
                            deferred,
                        )
                        pending.append(
                            (
                                channel_id,
                                channel_dir,
                                deferred,
                                self._session_generation.get(channel_id, 0),
                                batch_id,
                                True,
                            )
                        )
                        continue
                    # The overshoot has PERSISTED the whole window, so this is a
                    # collapse, not a transient.  U23: the answer is to CATCH
                    # UP, not to pause -- a GPU-bound stall measured at ~16 s
                    # used to cost 2-8 minutes of no captions at all, which the
                    # acceptance verifier samples as "no embedded cues".  Keep
                    # captioning from the NEWEST audio; discard only the oldest
                    # settled segments that can no longer be transcribed in
                    # time.  The pause ladder is retained, unchanged, for the
                    # case this cannot fix: a channel that is shedding as fast
                    # as the persistence window allows and still not keeping up.
                    if not self._catch_up_must_pause(channel_id):
                        kept, shed_count = self._shed_backlog_to_catch_up(
                            channel_id, queued_segments
                        )
                        dropped += shed_count
                        # Same contract as the transient deferral above: an
                        # over-limit scan is NOT recovery evidence for the
                        # ladder, so this batch is submitted ``deferred=True``.
                        batch_id = self._begin_batch(
                            channel_id,
                            self._session_generation.get(channel_id, 0),
                            kept,
                        )
                        pending.append(
                            (
                                channel_id,
                                channel_dir,
                                kept,
                                self._session_generation.get(channel_id, 0),
                                batch_id,
                                True,
                            )
                        )
                        # The EPISODE is over even though the channel is not
                        # paused: the shed resolved it, so a later overshoot
                        # starts a fresh streak. The shed history survives (see
                        # ``_catch_up_sheds``) -- it is the escalation evidence.
                        self._reset_overload_persistence(channel_id)
                        continue
                    # A currently-running batch is not queued backlog.  Keep
                    # the generation invalidation as a defensive race guard
                    # for a session/reset that becomes visible between the
                    # queue snapshot and this gate.
                    self._record_discarded_batch(
                        channel_id,
                        queued_segments,
                        outcome="overloaded",
                        reason="max-backlog-exceeded",
                        queue_depth=len(queued_segments),
                    )
                    self._invalidate_inflight_for_overload(channel_id)
                    dropped += self._fail_closed_overload(channel_id, channel_dir, queued_segments)
                    # The episode is over: the channel is paused and its audio is
                    # gone, so a later release starts counting from zero.
                    self._reset_overload_persistence(channel_id)
                    # ...and so is the shed budget: the ladder has taken over,
                    # and on release the channel must be judged afresh rather
                    # than escalating instantly on the first new shed.
                    self._clear_catch_up_sheds(channel_id)
                    overloaded_channels.append(channel_id)
                    paused_channels.append(channel_id)
                    continue
                # Bind the queued paths BEFORE runtime preparation/executor delay.
                batch_id = self._begin_batch(
                    channel_id,
                    self._session_generation.get(channel_id, 0),
                    queued_segments,
                )
                pending.append(
                    (
                        channel_id,
                        channel_dir,
                        queued_segments,
                        self._session_generation.get(channel_id, 0),
                        batch_id,
                        False,
                    )
                )

        if pending:
            if self._max_channel_workers_override is None:
                # Resolve the real faster-whisper device before creating a
                # multi-channel executor. CUDA initialization may fall back to
                # CPU; doing that here prevents the first scan from submitting
                # three calls before the runtime has reduced its capacity.
                prepare_runtime = getattr(self._runtime, "prepare", None)
                if callable(prepare_runtime):
                    # Model initialization and a possible CPU fallback can be
                    # expensive too. Keep this supervisor-side preparation at
                    # the same lowered priority as per-channel ASR work so it
                    # cannot preempt playout during first use.
                    _lower_current_thread_priority()
                    prepare_runtime()
                    # Log the caption-runtime identity ONCE after the model is loaded.
                    # Accuracy: on_cuda()/device BEFORE prepare is the REQUESTED device, and
                    # prepare() may fall back CUDA->CPU.  On SUCCESS the runtime does NOT
                    # rewrite device, so a request of "auto" can still read "auto" even when
                    # the model loaded on CUDA.  The loaded-model answer is therefore read
                    # from the backend model itself when available.
                    # Change-only: emitted once per distinct identity, not per pending scan.
                    identity = _resolved_runtime_identity(self._runtime)
                    if identity != self._logged_runtime_identity:
                        self._logged_runtime_identity = identity
                        _LOG.info(
                            "Caption runtime resolved after prepare: requested_device=%s "
                            "requested_compute_type=%s loaded_device=%s loaded_compute_type=%s "
                            "on_cuda=%s num_workers=%s",
                            identity[0],
                            identity[1],
                            identity[2],
                            identity[3],
                            identity[4],
                            identity[5],
                        )
                effective_workers = default_max_channel_workers(self._runtime)
                if effective_workers != self._max_channel_workers:
                    _LOG.info(
                        "Caption tap adjusted channel concurrency after runtime device change: "
                        "%d -> %d (device=%s, runtime_workers=%s)",
                        self._max_channel_workers,
                        effective_workers,
                        getattr(self._runtime, "device", "unknown"),
                        getattr(self._runtime, "num_workers", "n/a"),
                    )
                    self._max_channel_workers = effective_workers
            # The executor bound is the ASR concurrency bound.  It is retained
            # across scans so a slow batch is visible as in-flight work rather
            # than blocking the scan thread.  Per-channel isolation remains in
            # ``_isolated_process_channel``; a failure in one future cannot
            # unwind sibling channels.
            futures = [self._submit_channel(args) for args in pending]
            results = [future.result() for future in futures] if wait_for_results else []
            consumed += sum(result.consumed_segments for result in results)
            quarantined += sum(result.quarantined_segments for result in results)
            committed += sum(result.committed_review_items for result in results)
            expired += sum(result.expired_unconfirmed_cues for result in results)
        # Bounded, one-shot per-phase summary for this scan pass. The collector
        # gates this on its own event/window caps and emits at most once.
        self._phase_timing.summarise()
        # Same opt-in/bounded contract for the per-batch diagnostic.
        self._batch_diagnostic.summarise()
        return CaptionTapScanResult(
            consumed_segments=consumed,
            quarantined_segments=quarantined,
            committed_review_items=committed,
            dropped_overload_segments=dropped,
            expired_unconfirmed_cues=expired,
            channels=tuple(channels),
            overloaded_channels=tuple(overloaded_channels),
            paused_channels=tuple(paused_channels),
        )

    def _isolated_process_channel(
        self,
        channel_id: str,
        channel_dir: Path,
        segments: list[tuple[int, Path]],
        generation: int | None = None,
        batch_id: str | None = None,
        deferred_over_limit: bool = False,
    ) -> _ChannelScanResult:
        """Run one channel's scan pass without letting its failure abort the pass.

        Field defect (beta.9 three-channel ladder, 2026-09-19): ``run_once``
        submitted every channel through one ``pool.map``; a single channel's
        sidecar-publish ``PermissionError`` unwound the whole pass. This wrapper
        records the failure on that channel's runtime status (via the existing
        surface) and returns a degraded, empty result so the sibling channels
        still publish and the pass still completes. The failure is NOT
        swallowed: it is logged and written to the channel's runtime status.
        """

        started = time.monotonic()
        try:
            result = self._process_channel(
                channel_id,
                channel_dir,
                segments,
                generation,
                deferred_over_limit=deferred_over_limit,
            )
            if batch_id is not None:
                # Outcome reflects whether cues were COMMITTED, not merely
                # whether a segment was consumed: a consumed segment whose
                # ASR returned nothing (or only expired hypotheses) must not
                # be reported as a commit.
                committed_items = result.committed_review_items + result.published_cues
                self._finish_batch(
                    batch_id,
                    outcome="committed" if committed_items else "no-commit",
                    reason=(
                        "asr-committed" if committed_items else "asr-completed-no-committed-items"
                    ),
                    consumed_segments=result.consumed_segments,
                    committed_review_items=result.committed_review_items,
                    expired_unconfirmed_cues=result.expired_unconfirmed_cues,
                    elapsed_seconds=time.monotonic() - started,
                )
            return result
        except Exception:
            _LOG.exception(
                "Caption tap channel %s failed this scan; isolating so the pass "
                "continues for the other channels.",
                channel_id,
            )
            if batch_id is not None:
                self._finish_batch(
                    batch_id,
                    outcome="failed",
                    reason="channel-process-exception",
                    consumed_segments=0,
                    expired_unconfirmed_cues=0,
                    elapsed_seconds=time.monotonic() - started,
                )
            with suppress(Exception):
                self._publish_status(
                    channel_id,
                    state="overloaded",
                    backlog_segments=len(segments),
                )
            return _ChannelScanResult()

    def _process_channel(
        self,
        channel_id: str,
        channel_dir: Path,
        segments: list[tuple[int, Path]],
        generation: int | None = None,
        deferred_over_limit: bool = False,
    ) -> _ChannelScanResult:
        # This thread is about to run ASR. Hint the scheduler that it must
        # yield to the playout workers when the box is saturated.
        _lower_current_thread_priority()
        consumed = 0
        quarantined = 0
        committed = 0
        expired = 0
        published = 0
        with self._timed_session_lock(channel_id):
            if generation is None:
                generation = self._session_generation.get(channel_id, 0)
        for index, segment in segments:
            with self._timed_session_lock(channel_id):
                if (
                    generation != self._session_generation.get(channel_id, 0)
                    or channel_id in self._failed_sessions
                ):
                    break
                processed = channel_dir / "processed" / segment.name
                if self._retain_review_evidence and processed.exists():
                    with self._timed_retention_lock():
                        if (
                            self._retain_review_evidence
                            and self._retention_ready
                            and not self._retention_in_flight
                        ):
                            collision_path = self._move_collision(
                                segment, channel_dir / "collision"
                            )
                            self._retention_policy.record_event(
                                outcome="quarantined",
                                reason="restarted-chunk-index-collision",
                                path=collision_path,
                                sha256=_sha256(collision_path),
                            )
                        else:
                            segment.unlink(missing_ok=True)
                    quarantined += 1
                    continue
            raw_chunk = self._read_chunk(channel_id, index, segment)
            with self._session_lock(channel_id):
                if generation != self._session_generation.get(channel_id, 0):
                    break
                if raw_chunk is None:
                    self._previous_segments.pop(channel_id, None)
                    with self._retention_lock:
                        if (
                            self._retain_review_evidence
                            and self._retention_ready
                            and not self._retention_in_flight
                        ):
                            self._move(segment, channel_dir / "quarantine")
                        else:
                            segment.unlink(missing_ok=True)
                    quarantined += 1
                    continue
                chunk = self._with_overlap(channel_id, index, raw_chunk)
                worker = self._worker_for(channel_id)
            with self._phase_timing.phase(
                "asr_process_batch",
                channel=channel_id,
                generation=generation,
                index=index,
            ):
                result = worker.process_batch(
                    [chunk],
                    audio_evidence_factory=(
                        self._audio_evidence_factory(channel_id, chunk)
                        if self._retain_review_evidence
                        else None
                    ),
                )
            committed += len(result.committed_review_items)
            expired += len(result.expired_unconfirmed_cues)
            with self._session_lock(channel_id):
                if generation != self._session_generation.get(channel_id, 0):
                    # A new writer may already have reused this numbered path.
                    # Do not move it or restore the old session's overlap.
                    break
                self._last_processed_at[channel_id] = datetime.now(UTC)
                self._audio_signal[channel_id] = (
                    "audio-present" if any(raw_chunk.pcm_s16le) else "digital-silence"
                )
                with self._retention_lock:
                    retention_ready = self._retention_ready
                    retention_in_flight = self._retention_in_flight
                    if not retention_ready and not retention_in_flight:
                        for _index, stale_segment in segments:
                            stale_segment.unlink(missing_ok=True)
                        self._clear_channel_captions(channel_id)
                        self._publish_status(
                            channel_id,
                            state="storage-refused",
                            backlog_segments=0,
                            refusal_reason=self._retention_refusal,
                        )
                        break
                    if self._publish_cues(channel_id, generation, worker.committed_cues()):
                        published += len(result.committed_cues)
                    if not self._retain_review_evidence or self._retention_in_flight:
                        # Consumed live audio is transient.
                        # Captions/review text continue while periodic audio
                        # retention verification is pending. Never keep raw WAVs
                        # under yesterday's verdict or defer them into a backlog.
                        segment.unlink(missing_ok=True)
                    else:
                        with self._phase_timing.phase(
                            "file_move_to_processed", channel=channel_id, index=index
                        ):
                            self._move(segment, channel_dir / "processed")
                    self._previous_segments[channel_id] = (index, raw_chunk)
                    consumed += 1
        # One healthy scan. The policy forgives the channel's escalation only
        # after several of these in a row, so a channel that flaps does not
        # reset itself to the base delay every other scan.
        with self._session_lock(channel_id):
            if generation == self._session_generation.get(channel_id, 0):
                if deferred_over_limit:
                    # A scan whose QUEUE was over the backlog limit is not
                    # evidence that the channel is within capacity, whatever its
                    # batch managed to finish (U11 B2). Counting it as recovery
                    # would let a station that is still behind walk its
                    # escalation ladder back down -- and then trip again at the
                    # base window, the exact flap the ladder exists to prevent.
                    # Publish the queue remaining NOW, including new arrivals
                    # during inference; a finished batch is not the backlog.
                    self._publish_status(
                        channel_id,
                        state="within-capacity",
                        backlog_segments=len(self._settled_segments(channel_dir)),
                    )
                else:
                    transition = self._backoff.record_within_capacity(channel_id)
                    if transition:
                        # Field instrumentation (beta.9 ladder, 2026-09-19): the
                        # shipped log only recorded the overload OPEN, so a live
                        # second trip could not show whether the sustained recovery
                        # bar was ever reached. INFO on transition only (bounded by
                        # the rare recovery events, not per scan), metadata only.
                        _LOG.info(
                            "Caption tap backoff for channel %s: %s (rung=%d).",
                            channel_id,
                            transition,
                            self._backoff.state(channel_id).consecutive_overloads,
                        )
                    self._publish_status(
                        channel_id,
                        state="within-capacity",
                        backlog_segments=len(self._settled_segments(channel_dir)),
                    )
        return _ChannelScanResult(
            published_cues=published,
            consumed_segments=consumed,
            quarantined_segments=quarantined,
            committed_review_items=committed,
            expired_unconfirmed_cues=expired,
        )

    def _discard_pre_existing_segments(self) -> int:
        """Discard segments already on disk when this worker is constructed.

        THE STARTUP GAP (U11 B1).  ``_discard_settled_segments`` runs only on an
        explicit START, i.e. from the daemon's ``channel_start_hook``. A
        restart of the control plane does not issue one, so a worker could come
        up over a directory still holding the previous broadcast's audio and
        count it as backlog. MEASURED on the live station (U10, 2026-09-24): the
        17:17:41 and 17:17:43 trips followed a service restart at ~17:17:2x,
        with the oldest queued segment 50.9-56.5 s old at trip time -- audio
        from BEFORE the restart, discarded only when someone pressed START.

        Why a construction-time watermark is sound here, and mtime is not: every
        chunk present at construction was produced by something other than this
        worker -- a previous process or a finished session -- so no writer's
        ownership has to be inferred from the filesystem. (The SCOPE LIMIT note
        on ``_begin_channel_session_locked`` records why file mtime cannot
        establish that, and that reasoning still holds.)

        Deliberately scoped to what exists NOW: a segment the tap writes after
        construction is this session's audio and is processed normally, even if
        the first scan has not run yet.

        Never fatal: the tap directory is an operator-managed surface, and a
        worker that cannot be constructed would take the control plane -- and
        the station -- down over a caption-tap cleanup.
        """

        try:
            channel_dirs = sorted(path for path in self._tap_root.iterdir() if path.is_dir())
        except OSError:
            _LOG.debug(
                "Caption tap could not list %s at startup; no leftover audio was "
                "discarded. Live captions continue.",
                self._tap_root,
                exc_info=True,
            )
            return 0
        removed = 0
        for channel_dir in channel_dirs:
            removed += self._discard_settled_segments(channel_dir.name, context="worker startup")
        return removed

    def _discard_settled_segments(self, channel_id: str, *, context: str = "session start") -> int:
        """Discard a channel's leftover settled segments at a session boundary.

        Segments left in ``<channel>/`` by a previous broadcast belong to a
        session that has already ended, so they can never air.  Removing them
        keeps a new session from starting on the old one's backlog, which would
        otherwise trip the max-backlog fail-closed before any new audio existed.
        ``context`` names the boundary in the log line -- "session start" for an
        explicit START, "worker startup" for the construction-time sweep.
        """

        channel_dir = self._tap_root / channel_id
        if not channel_dir.is_dir():
            return 0
        # NB: use the RAW numbered list, not ``_settled_segments``.  That helper
        # deliberately omits the newest file so a live scan never reads a
        # half-written segment -- but at SESSION START there is no live writer for
        # this session yet, so every leftover chunk (including the newest) belongs
        # to the finished broadcast and must go.
        numbered: list[tuple[int, Path]] = []
        for path in channel_dir.iterdir():
            match = _SEGMENT_RE.match(path.name)
            if match is not None and path.is_file():
                numbered.append((int(match.group(1)), path))
        numbered.sort()
        removed = 0
        for _index, path in numbered:
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                _LOG.exception(
                    "Caption tap could not discard a leftover segment for channel %s: %s",
                    channel_id,
                    path,
                )
        if removed:
            _LOG.info(
                "Caption tap discarded %d leftover segment(s) for channel %s at "
                "%s; they belonged to a finished broadcast",
                removed,
                channel_id,
                context,
            )
        return removed

    # ------------------------------------------------------------------
    # Per-batch diagnostic seams (opt-in; inert unless enabled).
    # ------------------------------------------------------------------
    def _queue_age_seconds(self, segment: Path) -> float:
        """Age of a settled segment, from its mtime, bounded to >= 0.

        Metadata only: this reads a timestamp, never audio. Best effort -- a
        filesystem error yields 0.0 rather than disturbing the scan.
        """

        try:
            return max(0.0, time.time() - segment.stat().st_mtime)
        except OSError:
            return 0.0

    def _oldest_queue_age_seconds(self, segments: list[tuple[int, Path]]) -> float:
        return max((self._queue_age_seconds(path) for _index, path in segments), default=0.0)

    def _begin_batch(
        self,
        channel_id: str,
        generation: int,
        segments: list[tuple[int, Path]],
        queue_depth: int | None = None,
    ) -> str:
        """Open a per-batch record and return its id.

        The id is deterministic from channel/generation/first segment index so
        a log line and a record can be correlated without a side channel.

        ``queue_depth`` defaults to the size of the batch, which is what the
        normal and the pre-U23 fail-closed paths always were. The over-limit
        gate is the exception (U23): a catch-up shed transcribes the NEWEST
        segments and discards the rest, so the queue the gate SAW is deeper
        than the batch it acted on. A caller that discarded part of a longer
        queue must say so, or the record -- the operator's only after-the-fact
        account of missing captions -- would under-report how far behind the
        channel was. ``oldest_queue_age_seconds`` stays the batch's own age:
        that is the audio this decision was made about.

        DEFAULT-OFF IS TRULY INERT: when the diagnostic is disabled this
        returns the id and does NO ``Path.stat`` and NO clock read, so the hot
        scan path pays nothing beyond building the string.
        """

        first_index = segments[0][0] if segments else -1
        batch_id = f"{channel_id}-g{generation}-b{first_index:06d}-n{len(segments)}"
        if not self._batch_diagnostic.enabled:
            return batch_id
        self._batch_diagnostic.begin_batch(
            channel=channel_id,
            generation=generation,
            batch_id=batch_id,
            segment_indices=[index for index, _path in segments],
            segment_names=[path.name for _index, path in segments],
            queue_depth=len(segments) if queue_depth is None else queue_depth,
            oldest_queue_age_seconds=self._oldest_queue_age_seconds(segments),
        )
        return batch_id

    def _finish_batch(
        self,
        batch_id: str,
        *,
        outcome: str,
        reason: str,
        consumed_segments: int,
        expired_unconfirmed_cues: int,
        elapsed_seconds: float,
        committed_review_items: int = 0,
    ) -> None:
        self._batch_diagnostic.finish_batch(
            batch_id=batch_id,
            outcome=outcome,
            reason=reason,
            consumed_segments=consumed_segments,
            committed_review_items=committed_review_items,
            expired_unconfirmed_cues=expired_unconfirmed_cues,
            elapsed_seconds=elapsed_seconds,
        )

    def _record_discarded_batch(
        self,
        channel_id: str,
        segments: list[tuple[int, Path]],
        *,
        outcome: str,
        reason: str,
        queue_depth: int,
    ) -> None:
        """Record a batch the gate discarded BEFORE any ASR ran.

        Begun and finished immediately so a discard cannot appear as an
        unfinished in-flight batch, while still carrying the queue depth and
        age the gate actually saw. ``queue_depth`` is passed through rather than
        inferred from ``segments`` because a catch-up shed discards the OLDEST
        of a longer queue: the batch is a subset, and the depth is the truth an
        operator needs.
        """

        batch_id = self._begin_batch(
            channel_id,
            self._session_generation.get(channel_id, 0),
            segments,
            queue_depth=queue_depth,
        )
        self._finish_batch(
            batch_id,
            outcome=outcome,
            reason=reason,
            consumed_segments=0,
            expired_unconfirmed_cues=0,
            elapsed_seconds=0.0,
        )

    def _fail_closed_overload(
        self,
        channel_id: str,
        channel_dir: Path,
        segments: list[tuple[int, Path]],
    ) -> int:
        self._clear_channel_captions(channel_id)
        state = self._backoff.record_overload(channel_id)
        self._publish_status(
            channel_id,
            state="paused",
            backlog_segments=len(segments),
            resume_in_seconds=state.pause_seconds,
            consecutive_overloads=state.consecutive_overloads,
        )
        # WARNING, once per pause window -- not CRITICAL every scan. The field
        # log this replaces carried 663 caption lines, the same CRITICAL
        # repeating every ~30s for all three channels, while the real casualty
        # was the playout worker being restarted by its stall watchdog. An
        # overloaded caption tap is a degraded optional feature, not a station
        # emergency, and it must not drown the log the operator needs.
        _LOG.warning(
            "Caption tap overload for channel %s: %d settled segments exceeds "
            "the maximum %d. Live captions are PAUSED for %.0fs (overload #%d) so "
            "playout keeps the CPU; active captions were cleared and the stale "
            "audio was discarded.",
            channel_id,
            len(segments),
            self._max_backlog_segments,
            state.pause_seconds,
            state.consecutive_overloads,
        )
        # DISCARDED, not filed under `<channel>/overload/`. That directory was
        # never swept by anything -- the retention policy's tap sweep reads
        # `processed/` only (civiccast/captions/retention.py) -- and no review
        # row ever referenced it, so on a chronically overloaded station it
        # grew without bound across every pause cycle: one directory of raw
        # broadcast audio per channel, accumulating for as long as the station
        # could not keep up, with no retention clock on it at all. Audio that
        # was never transcribed and can never be reviewed is not evidence; it
        # is a disk leak wearing the word.
        for _index, segment in segments:
            segment.unlink(missing_ok=True)
        return len(segments)

    def _sweep_retention(self) -> None:
        """Kick the retention sweep WITHOUT blocking the caption scan path.

        ``enforce_discovered`` is heavy: MEASURED at 4.1-5.6 s per sweep on the
        Blackwell host (2026-09-16).  The measured breakdown is
        ``_discover_candidates`` 4.78 s and ``enforce`` 0.83 s over 8,105
        candidates; within discovery, hashing 6,747 processed chunks measured
        1.01 s and hashing the evidence WAVs 0.19 s.  The remaining ~3.5 s of
        discovery is the review-store ``list()`` plus a per-row audio-evidence
        lookup, which was NOT separately instrumented -- treat that share as
        modeled, not measured.  It used to run inline here, at the TOP
        of ``run_once``, i.e. on the caption-critical thread and BEFORE the backlog
        gate.  Segments arrive every 5 s while the tap scans every 2 s, so a ~5 s
        block let ~1-2 segments pile up; the gate then saw >2 and fail-closed
        (120 s pause, live VTT cleared, audio discarded) with ZERO ASR attempted,
        even though warm ASR measured 0.11 s/segment.  That chain is the MODELED
        causality from the measured sweep duration plus the measured segment
        cadence (5 s) and scan interval (2 s); the live timeline itself was not
        instrumented per-scan, so treat the mechanism as a model that the
        regression test below reproduces, not as a per-scan trace.  The overload
        was self-inflicted by cleanup, not by the tap failing to keep up.

        Later sweep WORK runs on its own thread. Pending verification allows
        transient captions and text-only review but no retained WAVs. The current
        verdict and in-flight flag are checked atomically at each persistence
        boundary; a refused verdict also blocks subsequent VTT publication.
        """

        now = self._monotonic()
        with self._retention_lock:
            if self._retention_in_flight:
                # A sweep is already running; never queue a second one.
                return
            if (
                self._last_retention_sweep is not None
                and now - self._last_retention_sweep < _RETENTION_SWEEP_SECONDS
            ):
                return
            first_verification = not self._retention_verified
            # Mark cadence + in-flight immediately so no later scan re-dispatches
            # while this sweep is still running.
            self._last_retention_sweep = now
            self._retention_in_flight = True
        # The sweep ALWAYS runs on its own daemon thread, first verification
        # included.  It used to run inline for the first verification, on the
        # rationale that "it happens at session start, before this session has
        # live captions to lose".  MEASURED beta.9 N=3 (Blackwell, 2026-09-18):
        # that assumption is false on a running station -- the first
        # verification happens at WORKER CONSTRUCTION against an existing
        # 13,550-candidate archive and took 16.75 s, more than three times the
        # 5 s segment cadence, so 3+ settled segments accumulated before the
        # first scan reached the max-2 backlog gate and the gate tripped at
        # construction.  The cost is an N+1 database pattern in discovery
        # (review_store.list() then a fresh session + row lookup PER ROW).
        #
        # SAFETY IS PRESERVED BY THE WAIT, NOT BY THE THREAD: the scan thread
        # waits up to _RETENTION_FIRST_VERDICT_WAIT_SECONDS (1 s, well under the
        # 5 s cadence) for the verdict.  If it arrives, behaviour is exactly as
        # before -- verdict published before any ASR.  If it does NOT, the scan
        # proceeds with the existing PENDING state, which already fails closed:
        # ``_retention_ready`` stays False and ``_retention_refusal`` stays
        # "retention-verification-pending", so no ASR is transcribed into an
        # unverified store and no WAV is retained.  The thread publishes the real
        # verdict when it finishes, exactly as later sweeps already do.
        thread = threading.Thread(
            target=self._run_retention_sweep,
            name="civiccast-caption-retention",
            daemon=True,
        )
        self._retention_thread = thread
        with self._phase_timing.phase("retention_sweep_work"):
            thread.start()
            if first_verification:
                # Bounded wait only for the FIRST verdict; later sweeps are
                # fire-and-forget as before.
                thread.join(timeout=_RETENTION_FIRST_VERDICT_WAIT_SECONDS)
                if thread.is_alive():
                    _LOG.info(
                        "Caption retention first verification is still running "
                        "after %.1fs; the scan proceeds FAIL-CLOSED (no ASR into "
                        "an unverified store) and the verdict is applied when the "
                        "sweep publishes it.",
                        _RETENTION_FIRST_VERDICT_WAIT_SECONDS,
                    )

    def wait_for_retention_sweep(self, timeout: float = 30.0) -> bool:
        """Block until any in-flight retention sweep finishes. Returns True if idle.

        The sweep runs on a daemon thread (so it can never hold process shutdown
        open), but callers and tests need a deterministic way to know the work is
        done rather than racing it.  This joins the last dispatched thread and
        reports whether it is finished within ``timeout``.
        """

        thread = self._retention_thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _run_retention_sweep(self) -> None:
        """The blocking half of the retention sweep, on its own thread."""

        try:
            retention = self._retention_policy.enforce_discovered(
                tap_root=self._tap_root,
                review_store=self._review_store,
                segment_seconds=self._segment_seconds,
            )
        except Exception:
            # A sweep that RAISES means the store could not be verified on this
            # pass.  For a safety gate, "cannot verify" must not silently keep
            # writing caption evidence, so a failure fails CLOSED whether it is
            # the first sweep or a later one.  This is deliberately stricter than
            # the earlier "keep the last known verdict" behaviour, which left a
            # post-success failure reading ready=True -- i.e. an unverifiable
            # store could keep being transcribed into.
            #
            # Recovery is explicit and automatic: the next sweep that SUCCEEDS
            # republishes a real verdict (ready or refused), so this is a
            # fail-closed-with-retry, never a latch.  The refusal reason
            # distinguishes "never verified" from "verification failed" from a
            # policy refusal, so an operator can tell them apart.
            _LOG.exception("Caption retention sweep failed; captions stay fail-closed.")
            with self._retention_lock:
                self._retention_in_flight = False
                self._retention_ready = False
                self._retention_refusal = "retention-verification-failed"
            return
        with self._retention_lock:
            self._retention_in_flight = False
            self._retention_verified = True
            self._retention_ready = bool(retention.ready)
            self._retention_refusal = retention.refusal_reason

    def _run_disabled(self) -> CaptionTapScanResult:
        """The operator turned live captions OFF: transcribe nothing, keep nothing.

        The egress audio fork is part of the playout graph and keeps writing a
        segment every few seconds regardless of this switch, so "off" cannot
        just mean "stop reading". Every channel's live VTT is blanked, the
        status reads ``disabled``, and settled audio is DELETED rather than
        filed as evidence -- a station that switched live captioning off has
        not asked CivicCast to keep a rolling recording of its broadcast audio.
        """

        if not self._disabled_announced:
            # Clear by the CHANNEL SET, not by tap-directory presence. A
            # channel whose tap directory was never created (or was already
            # swept) has no directory to iterate, yet it can still be serving a
            # stale ``active.vtt`` from before the switch was thrown -- captions
            # on air that nothing is producing any more. ``reset_existing_live_
            # sidecars`` blanks every channel that HAS a live sidecar, which is
            # exactly that set; the loop below then handles the per-channel ASR
            # state for the channels this worker knows about.
            reset_existing_live_sidecars(self._caption_work_dir)
            for channel_id in sorted(
                {*self._channel_workers, *self._channel_publishers, *self._previous_segments}
            ):
                self._clear_channel_captions(channel_id)
                self._backoff.forget(channel_id)
                self._reset_overload_persistence(channel_id)

        channels: list[str] = []
        discarded = 0
        # The tap root may not exist (never created, or swept): the sidecar
        # clear above already ran by the channel set, and there is then no
        # forked audio to discard.
        tap_dirs = (
            sorted(p for p in self._tap_root.iterdir() if p.is_dir())
            if self._tap_root.is_dir()
            else []
        )
        for channel_dir in tap_dirs:
            channel_id = channel_dir.name
            channels.append(channel_id)
            with self._session_lock(channel_id):
                if not self._disabled_announced:
                    self._clear_channel_captions(channel_id)
                    self._backoff.forget(channel_id)
                self._reset_overload_persistence(channel_id)
                # Throttle unchanged status, as in the enabled scan path.
                self._publish_status(channel_id, state="disabled", backlog_segments=0)
                for _index, segment in self._settled_segments(channel_dir):
                    segment.unlink(missing_ok=True)
                    discarded += 1
        if not self._disabled_announced:
            _LOG.info(
                "Live captions are switched off for this station "
                "(StationProfile.live_captions_enabled); the caption tap is "
                "discarding forked audio without transcribing it."
            )
            self._disabled_announced = True
        return CaptionTapScanResult(
            dropped_overload_segments=discarded,
            channels=tuple(channels),
        )

    def _drain_paused_channel(
        self,
        channel_id: str,
        channel_dir: Path,
        segments: list[tuple[int, Path]],
    ) -> int:
        """Discard settled audio for a paused channel without transcribing it.

        The pause is the whole point: not one sample is handed to the ASR
        runtime while it holds. Draining is still required -- the egress audio
        tap keeps publishing a segment every few seconds whether anyone reads
        them or not, so a paused channel that kept its segments would fill the
        station's disk instead of its CPU.
        """

        self._publish_status(
            channel_id,
            state="paused",
            backlog_segments=len(segments),
            resume_in_seconds=self._backoff.remaining_seconds(channel_id),
            consecutive_overloads=self._backoff.state(channel_id).consecutive_overloads,
        )
        # DELETED, not filed as evidence. `<channel>/overload/` is swept by
        # nothing -- the retention policy's tap sweep reads `processed/` only
        # -- so a station stuck in a long backoff would accumulate its own
        # broadcast audio there forever, unpruned and unreferenced by any
        # review row. The one-off `_fail_closed_overload` move that opens a
        # pause still files its segments as overload evidence, which is what
        # the capacity proof's negative control inspects; the unbounded
        # per-scan drain that follows it must not.
        for _index, segment in segments:
            segment.unlink(missing_ok=True)
        return len(segments)

    def _publish_status(
        self,
        channel_id: str,
        *,
        state: CaptionRuntimeState,
        backlog_segments: int,
        refusal_reason: str | None = None,
        resume_in_seconds: float | None = None,
        consecutive_overloads: int | None = None,
    ) -> bool:
        """Publish channel status only when it CHANGED, or when it went stale.

        The scan loop runs every ~2 seconds per channel forever. Rewriting an
        unchanged status file on every one of those scans is a durable write
        (the publisher fsyncs and renames) for no new information: a
        three-channel station idles at ~90 pointless fsync+rename pairs a
        minute, against the same disk the recordings are written to.

        Two things are excluded from the comparison on purpose. ``updated_at``
        always differs, so comparing whole payloads would never suppress
        anything. ``resume_in_seconds`` ticks down continuously while a channel
        is paused, so it would do the same -- it is therefore not part of the
        change key, and freshness is preserved instead by the
        :data:`_STATUS_REFRESH_SECONDS` heartbeat below, which still republishes
        a steady state periodically so an operator's countdown advances and
        ``updated_at`` never looks abandoned.
        """

        now = self._monotonic()
        if state == "within-capacity" and backlog_segments > 0:
            pending = self._pending_since_at.get(channel_id)
            age = max(0.0, (datetime.now(UTC) - pending).total_seconds()) if pending else 0.0
            age_limit = max(
                self._caption_backlog_grace_seconds,
                self._max_backlog_segments * self._segment_seconds,
            )
            over_limit = backlog_segments > self._max_backlog_segments or age > age_limit
            if over_limit:
                started = self._overload_started_at.get(channel_id, now)
                sustained = (
                    self._overload_scan_streak.get(channel_id, 0)
                    >= self._overload_persistence_scans
                    and now - started >= self._caption_backlog_grace_seconds
                )
                state = "overloaded" if sustained or age > age_limit else "delayed"
        provider_status = getattr(self._runtime, "live_provider_status", None)
        provider_state = "unknown"
        provider_retry_in_seconds: int | None = None
        if callable(provider_status):
            try:
                provider_state, provider_retry_in_seconds = provider_status(channel_id)
            except Exception:
                provider_state = "unknown"
        with self._channel_executor_lock:
            inference_inflight = bool(self._channel_inflight.get(channel_id))
            inference_started_at = self._channel_inflight_started_at.get(channel_id)
        last_input_at = self._last_input_at.get(channel_id)
        last_processed_at = self._last_processed_at.get(channel_id)
        pending_since_at = self._pending_since_at.get(channel_id)
        key = (
            state,
            backlog_segments,
            consecutive_overloads,
            refusal_reason,
            provider_state,
            inference_inflight,
            inference_started_at,
            last_input_at,
            last_processed_at,
            pending_since_at,
        )
        previous = self._published_status.get(channel_id)
        if previous is not None:
            previous_key, published_at = previous
            if previous_key == key and now - published_at < _STATUS_REFRESH_SECONDS:
                return False
        publish_caption_runtime_status(
            self._caption_work_dir,
            channel_id,
            state=state,
            backlog_segments=backlog_segments,
            max_backlog_segments=self._max_backlog_segments,
            refusal_reason=refusal_reason,
            resume_in_seconds=resume_in_seconds,
            consecutive_overloads=consecutive_overloads,
            worker_heartbeat_at=datetime.now(UTC),
            last_input_at=last_input_at,
            last_processed_at=last_processed_at,
            pending_since_at=pending_since_at,
            inference_started_at=inference_started_at,
            audio_signal=self._audio_signal.get(channel_id, "unknown"),
            provider_state=provider_state,
            provider_retry_in_seconds=provider_retry_in_seconds,
            inference_inflight=inference_inflight,
        )
        self._published_status[channel_id] = (key, now)
        return True

    def _clear_channel_captions(self, channel_id: str) -> None:
        """Fail closed: drop the channel's ASR state and blank its live VTT."""

        with self._session_lock(channel_id):
            self._session_generation[channel_id] = self._session_generation.get(channel_id, 0) + 1
            self._clear_channel_captions_locked(channel_id)

    def _clear_channel_captions_locked(self, channel_id: str) -> None:
        self._channel_workers.pop(channel_id, None)
        publisher = self._channel_publishers.pop(channel_id, None)
        if publisher is None:
            publisher = LiveWebVttPublisher(
                active_caption_sidecar(self._caption_work_dir, channel_id)
            )
        publisher.reset()
        self._previous_segments.pop(channel_id, None)

    def _settled_segments(self, channel_dir: Path) -> list[tuple[int, Path]]:
        """Numbered segments with a newer sibling (ffmpeg moved past them)."""

        _token, first_segment = read_program_boundary(channel_dir)
        numbered: list[tuple[int, Path]] = []
        for path in channel_dir.iterdir():
            match = _SEGMENT_RE.match(path.name)
            if match is not None and path.is_file():
                if int(match.group(1)) < first_segment:
                    path.unlink(missing_ok=True)
                    continue
                numbered.append((int(match.group(1)), path))
        numbered.sort()
        # GStreamer publishes through ``.wav.partial -> .wav`` atomically, so
        # every visible WAV is complete. Legacy FFmpeg segment output writes the
        # highest WAV in place; preserve its one-file settling guard.
        settled = numbered if self._atomic_segments else numbered[:-1]
        if not self._retain_review_evidence or self._caption_backlog_grace_seconds > 0:
            # A hung inference child must not turn its continuing audio fork
            # into an unlimited recording. Never delete an in-flight owner's
            # inputs. Ordinary runtimes keep the 12-file (60s at 5s cadence)
            # ceiling; a Whistle runtime advertises a longer finite grace, so
            # its cap grows only enough to retain that much queued audio.
            owned = self._inflight_names(channel_dir.name)
            waiting = [item for item in settled if item[1].name not in owned]
            if len(waiting) > self._working_audio_limit_segments:
                discarded = waiting[: -self._working_audio_limit_segments]
                discarded_names = {path.name for _, path in discarded}
                discarded_seconds = sum(self._chunk_span_seconds(path) for _, path in discarded)
                self._record_discarded_batch(
                    channel_dir.name,
                    discarded,
                    outcome="overloaded",
                    reason="working-audio-limit",
                    queue_depth=len(waiting),
                )
                for _, path in discarded:
                    path.unlink(missing_ok=True)
                settled = [item for item in settled if item[1].name not in discarded_names]
                _LOG.warning(
                    "Caption tap catch-up for channel %s: working-audio limit "
                    "discarded %d queued segments (%.1fs of audio); "
                    "kept the newest %d and all in-flight inputs.",
                    channel_dir.name,
                    len(discarded),
                    discarded_seconds,
                    self._working_audio_limit_segments,
                )
        return settled

    def _read_chunk(self, channel_id: str, index: int, path: Path) -> AudioChunk | None:
        try:
            with wave.open(str(path), "rb") as handle:
                rate = handle.getframerate()
                channels = handle.getnchannels()
                width = handle.getsampwidth()
                frames = handle.readframes(handle.getnframes())
            if channels != 1 or width != 2:
                raise ValueError(f"expected mono s16le tap audio, got {channels}ch/{width * 8}-bit")
            if not frames:
                raise ValueError("segment contains no audio frames")
        except Exception:
            _LOG.exception("Unreadable caption tap segment for channel %s: %s", channel_id, path)
            return None
        duration = len(frames) / 2 / rate
        start = index * self._segment_seconds
        return AudioChunk(
            chunk_id=f"{channel_id}-tap-{index:06d}",
            start_seconds=start,
            end_seconds=start + duration,
            sample_rate_hz=rate,
            pcm_s16le=frames,
        )

    def _worker_for(self, channel_id: str) -> LiveCaptionWorker:
        worker = self._channel_workers.get(channel_id)
        if worker is None:
            worker = LiveCaptionWorker(
                self._runtime,
                self._review_store,
                asset_id=channel_id,
                persist_review=self._retain_review_evidence,
                reviewer_note=self._reviewer_note,
                translation_provider=self._translation_provider,
                # LIVE stabilizer: this tap feeds OVERLAPPING audio windows (5 s
                # segments with 5 s of overlap -- 10 s windows advancing 5 s), so
                # the same audio is re-heard by the next window rather than
                # re-transcribed identically. Confirmation requires substantive
                # audio overlap AND matching words across those windows; only
                # the shared phrase over their intersecting interval commits.
                # Geometry alone cannot corroborate the incoming tail. Every
                # other caller -- offline/VOD and the whole existing test suite --
                # keeps exact-text re-confirmation unchanged.
                pipeline=CaptionPipeline(
                    self._runtime,
                    stabilizer=CaptionStabilizer(
                        live=True, history_seconds=300.0, max_history_cues=512
                    ),
                    prepare_review_items=self._retain_review_evidence,
                    phase_timing=self._phase_timing,
                    phase_timing_channel=channel_id,
                ),
                persistence_guard=(
                    self._review_persistence_guard if self._retain_review_evidence else None
                ),
                phase_timing=self._phase_timing,
                phase_timing_channel=channel_id,
            )
            self._channel_workers[channel_id] = worker
        return worker

    @contextmanager
    def _review_persistence_guard(self) -> Iterator[ReviewPersistenceMode]:
        """Never retain audio using a stale or concurrently revoked verdict.

        The sweep does heavy work without this lock. Only its dispatch/verdict
        transitions and short persistence transactions hold it. Pending periodic
        verification permits text-only review (including low-confidence review),
        with the existing unavailable-audio UI contract; no deferred audio queue.
        """

        with self._retention_lock:
            if not self._retention_ready and not self._retention_in_flight:
                yield "refused"
            elif self._retention_in_flight:
                yield "text-only"
            else:
                yield "audio"

    def _audio_evidence_factory(
        self,
        channel_id: str,
        chunk: AudioChunk,
    ) -> AudioEvidenceFactory:
        """Return one lazy evidence writer bound to this channel and ASR window."""

        evidence: CaptionReviewAudioEvidence | None = None

        def write_once(_cue: CaptionCue) -> CaptionReviewAudioEvidence:
            nonlocal evidence
            if evidence is None:
                digest = hashlib.sha256(chunk.chunk_id.encode("utf-8")).hexdigest()[:24]
                evidence = write_caption_review_audio_evidence(
                    chunk,
                    self._caption_work_dir / channel_id / "captions" / "evidence" / f"{digest}.wav",
                )
            return evidence

        return write_once

    def _with_overlap(
        self,
        channel_id: str,
        index: int,
        current: AudioChunk,
    ) -> AudioChunk:
        """Prepend the prior segment tail so consecutive ASR windows overlap."""

        previous_entry = self._previous_segments.get(channel_id)
        if previous_entry is None:
            return current
        previous_index, previous = previous_entry
        if previous_index + 1 != index or previous.sample_rate_hz != current.sample_rate_hz:
            return current
        sample_tolerance = 1.0 / current.sample_rate_hz
        if abs(previous.end_seconds - current.start_seconds) > sample_tolerance:
            return current

        available_frames = len(previous.pcm_s16le) // 2
        requested_frames = round(self._overlap_seconds * current.sample_rate_hz)
        overlap_frames = min(available_frames, requested_frames)
        if overlap_frames <= 0:
            return current
        overlap_bytes = overlap_frames * 2
        actual_overlap = overlap_frames / current.sample_rate_hz
        return AudioChunk(
            chunk_id=f"{current.chunk_id}-overlap",
            start_seconds=current.start_seconds - actual_overlap,
            end_seconds=current.end_seconds,
            sample_rate_hz=current.sample_rate_hz,
            pcm_s16le=previous.pcm_s16le[-overlap_bytes:] + current.pcm_s16le,
        )

    def publish_for_current_session(
        self, channel_id: str, generation: int, cues: list[CaptionCue]
    ) -> bool:
        """Publish ``cues`` only if they belong to the channel's live session.

        Public seam used by the tap's own scan path and by tests.  Returns True
        when the cues were written, False when they were dropped as belonging to
        a session that has already ended.
        """

        with self._session_lock(channel_id), self._retention_lock:
            token, _first_segment = read_program_boundary(self._tap_root / channel_id)
            if (
                generation != self._session_generation.get(channel_id, 0)
                or token != self._program_tokens.get(channel_id, "")
                or channel_id in self._failed_sessions
                or (not self._retention_ready and not self._retention_in_flight)
            ):
                _LOG.info(
                    "channel %s: caption publish refused (generation %d, current %d, "
                    "reset_failed=%s, retention_ready=%s)",
                    channel_id,
                    generation,
                    self._session_generation.get(channel_id, 0),
                    channel_id in self._failed_sessions,
                    self._retention_ready,
                )
                return False
            if token:
                cues = [
                    cue.model_copy(update={"cue_id": f"{token}-{cue.cue_id}"[:160]}) for cue in cues
                ]
            self._publisher_for(channel_id).publish(cues)
            return True

    def _publish_cues(self, channel_id: str, generation: int, cues: list[CaptionCue]) -> bool:
        """Publish committed cues unless their session has already ended.

        ``generation`` is captured when the worker that produced these cues was
        created.  If ``begin_channel_session`` has since bumped the channel's
        generation, this result belongs to a finished broadcast and must be
        dropped rather than written over the new session's sidecar.
        """

        return self.publish_for_current_session(channel_id, generation, cues)

    def _publisher_for(self, channel_id: str) -> LiveWebVttPublisher:
        publisher = self._channel_publishers.get(channel_id)
        if publisher is None:
            publisher = LiveWebVttPublisher(
                active_caption_sidecar(self._caption_work_dir, channel_id),
                window_seconds=300.0,
                max_cues=512,
            )
            publisher.reset()
            self._channel_publishers[channel_id] = publisher
        return publisher

    @staticmethod
    def _move(path: Path, into: Path) -> None:
        into.mkdir(parents=True, exist_ok=True)
        path.replace(into / path.name)

    @staticmethod
    def _move_collision(path: Path, into: Path) -> Path:
        into.mkdir(parents=True, exist_ok=True)
        destination = into / path.name
        if destination.exists():
            destination = into / f"{path.stem}-{_sha256(path)[:12]}{path.suffix}"
        path.replace(destination)
        return destination


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_tap_worker(
    settings: CaptionTapWorkerSettings,
    review_store: CaptionReviewStore,
    *,
    runtime: CaptionRuntime | None = None,
    translation_provider: TranslationProvider | None = None,
    caption_work_dir: Path | None = None,
    is_enabled: Callable[[], bool] | None = None,
) -> CaptionTapWorker:
    """Construct a tap worker from deployment settings.

    Without an injected runtime, ``CIVICCAST_CAPTION_RUNTIME`` selects the
    runtime. Activated native stations are locked to the accepted
    faster-whisper large-v3 contract. Runtime failures surface immediately
    (fail fast, never a silent no-op).
    ``translation_provider`` (S13) is the operator-selected translation model; when
    omitted no translation track is produced.
    """

    if settings.tap_root is None:
        raise ValueError("Caption tap worker requires a tap_root (CIVICCAST_CAPTION_TAP_DIR).")
    if runtime is None:
        backend = (
            os.environ.get(
                "CIVICCAST_CAPTION_RUNTIME",
                "faster-whisper",
            )
            .strip()
            .lower()
        )
        if (
            os.environ.get("CIVICCAST_NATIVE_STATION", "").strip() == "1"
            and backend != "faster-whisper"
        ):
            raise ValueError(
                "Activated native stations require the accepted faster-whisper "
                "large-v3 caption runtime."
            )
        if backend == "faster-whisper":
            # Do not pass max_channel_workers into the runtime. The runtime
            # selects its own device capacity (one CT2 worker on CPU, three on
            # CUDA); the tap follows that capacity unless the operator set an
            # explicit channel-pool override. They remain separate controls.
            #
            # MEASURED, and it does NOT cost what I claimed. I originally
            # changed this believing inter_threads replicated the model and
            # therefore explained the 16 GB field failure. TESTER2 A/B'd it on
            # a real station (request 0192): same audio, same 12 segments,
            # override verified live, real faster-whisper inference confirmed
            # from the VAD/language rows. Going from 3 to 1 moved the private-
            # byte plateau by 3.93% -- 5.41 GB to 5.20 GB -- not to a third.
            # The replication hypothesis is REFUTED. Do not repeat it.
            #
            # What this change is actually worth: ~2.9% peak private, ~18% peak
            # RSS, and a 3.6% speedup at no measured cost. A tidy-up, not a fix.
            #
            # The real defect is still open and is NOT a leak: both runs held a
            # FLAT shelf from segment 1 and completed 12/12. It is a fixed
            # ~5.4 GB commit against only ~1.1 GB resident -- i.e. commit
            # charge, which is what actually exhausts a 16 GB box. Next
            # suspects are compute_type and cpu_threads sizing CTranslate2's
            # arenas, not the worker count.
            #
            # Deliberately overridable: CIVICCAST_WHISPER_NUM_WORKERS.
            #
            # ``live=True``: this is the live captioner, sharing a box with
            # playout, so the runtime sizes ITSELF conservatively -- 1-2
            # CTranslate2 intra-op threads (core-count-aware, capped -- see
            # civiccast.captions.runtime.default_live_tap_cpu_threads) and
            # greedy decoding once it resolves to CPU
            # (civiccast.captions.runtime.LIVE_TAP_CPU_THREADS records the
            # measured field failure the batch sizing produced here).
            #
            # NOTE this branch runs only for the EXTERNAL entrypoint and for
            # callers that inject no runtime. The native service pre-builds the
            # runtime through civiccast.ai_models.runtime.build_caption_runtime
            # and injects it, which is exactly why ``live`` is a property of
            # the RUNTIME and not a bundle of kwargs applied here: kwargs here
            # were dead code in the product, and the live tap went on using
            # every core at beam 5.
            from civiccast.captions.runtime import FasterWhisperRuntime

            runtime = FasterWhisperRuntime(live=True)
        elif backend == "whispercpp-vulkan":
            from civiccast.captions.runtime import WhisperCppRuntime

            executable = os.environ.get("CIVICCAST_WHISPER_CPP_EXE", "").strip()
            model = os.environ.get("CIVICCAST_WHISPER_CPP_MODEL_PATH", "").strip()
            if not executable or not model:
                raise ValueError(
                    "CIVICCAST_WHISPER_CPP_EXE and "
                    "CIVICCAST_WHISPER_CPP_MODEL_PATH must be set when "
                    "CIVICCAST_CAPTION_RUNTIME='whispercpp-vulkan'."
                )
            runtime = WhisperCppRuntime(
                executable=Path(executable),
                model=Path(model),
            )
        else:
            raise ValueError(
                "CIVICCAST_CAPTION_RUNTIME must be 'faster-whisper' or "
                f"'whispercpp-vulkan'; got {backend!r}."
            )
    if caption_work_dir is None:
        from civiccast.egress.automation import default_egress_work_dir

        caption_work_dir = default_egress_work_dir()
    return CaptionTapWorker(
        tap_root=settings.tap_root,
        caption_work_dir=caption_work_dir,
        runtime=runtime,
        review_store=review_store,
        segment_seconds=settings.segment_seconds,
        atomic_segments=settings.atomic_segments,
        overlap_seconds=settings.overlap_seconds,
        max_channel_workers=settings.max_channel_workers,
        max_backlog_segments=settings.max_backlog_segments,
        overload_persistence_scans=settings.overload_persistence_scans,
        catch_up_shed_limit=settings.catch_up_shed_limit,
        catch_up_shed_window_seconds=settings.catch_up_shed_window_seconds,
        translation_provider=translation_provider,
        backoff_policy=CaptionBackoffPolicy(
            base_seconds=settings.overload_backoff_seconds,
            max_seconds=settings.max_overload_backoff_seconds,
        ),
        is_enabled=is_enabled,
    )


def main(argv: list[str] | None = None) -> int:
    """External entrypoint: ``python -m civiccast.captions.tap_worker``.

    Requires ``DATABASE_URL`` (durable review queue) and the same
    ``CIVICCAST_CAPTION_TAP*`` settings as the in-app thread; the mode value
    is not consulted — running this entrypoint IS the external mode.
    """

    parser = argparse.ArgumentParser(
        prog="python -m civiccast.captions.tap_worker",
        description="CivicCast live caption tap worker (external process mode).",
    )
    parser.add_argument(
        "--once", action="store_true", help="Run a single scan and exit (smoke checks)."
    )
    args = parser.parse_args(argv)

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        parser.error("DATABASE_URL must be set to run the caption tap worker.")

    logging.basicConfig(level=logging.INFO)

    from contextlib import contextmanager

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from civiccast.captions.persistence import PostgresCaptionReviewStore
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

    settings = CaptionTapWorkerSettings.from_env()
    worker = build_tap_worker(settings, PostgresCaptionReviewStore(_session_factory))
    if args.once:
        worker.run_once()
        return 0
    stop_event = threading.Event()
    try:
        worker.run_forever(poll_seconds=settings.poll_seconds, stop_event=stop_event)
    except KeyboardInterrupt:  # pragma: no cover - interactive shutdown path
        stop_event.set()
        _LOG.info("Caption tap worker interrupted; exiting.")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via external mode
    raise SystemExit(main())
