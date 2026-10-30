# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Opt-in, bounded PER-BATCH diagnostic for the live caption tap.

Field need (beta.10, 2026-09-21/22): one channel at a time went caption-empty
for 1-4 minutes and the identity ROTATED across channels. The overload gate was
also tripping at 3 settled segments against a max of 2. The available phase
timing answers "where does a scan's time go" in aggregate, but it cannot answer
"what happened to THIS batch":

* did this settled batch reach ASR, or was it discarded before any ASR ran?
* was the discard an overload, a paused-channel drain, or a storage refusal?
* how deep was the queue and how OLD was the oldest segment when the decision
  was made, and how long did the batch then take?
* which channel and session generation owned it - so a rotating episode can be
  attributed to a channel instead of averaged into a station-wide number?

This module answers exactly those questions and nothing else.

DESIGN CONSTRAINTS (all deliberate, mirroring
``civiccast.captions.phase_timing``):
* DEFAULT OFF. ``batch_diagnostic_from_env`` returns an inert collector unless
  ``CIVICCAST_CAPTION_TAP_BATCH_DIAGNOSTIC=1``. When off, NO clock is read and
  NO record is kept, so production behaviour is unchanged.
* BOUNDED. A hard cap on retained batch records and a wall-clock window
  measured from construction. Once either is exhausted the collector stops
  recording; the scan path is never made to fail or block because of it.
* METADATA ONLY. Channel, session generation, batch id, segment indices and
  file NAMES, queue depth, queue age, durations, counts and outcome/reason.
  No audio, no speech text, no cue text, no credentials, and no exception
  messages.
* FAILURE-PROOF. Every public method suppresses its own errors so a broken
  handler or a bad value can never change scan behaviour.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "BATCH_DIAGNOSTIC_ENV_VAR",
    "DEFAULT_SUMMARY_INTERVAL_SECONDS",
    "BatchDiagnosticCollector",
    "NullBatchDiagnostic",
    "batch_diagnostic_from_env",
]

_LOG = logging.getLogger(__name__)

#: Explicit opt-in switch. Absent or anything that is not "1"/"true"/"yes"/"on"
#: leaves the tap on the inert collector.
BATCH_DIAGNOSTIC_ENV_VAR: Final[str] = "CIVICCAST_CAPTION_TAP_BATCH_DIAGNOSTIC"

#: Hard bound on retained batch records.  The beta.9 N=3 run produced ~109 ASR
#: batches per 600 s, i.e. ~700 per 10-minute window across three channels, so
#: 2000 records comfortably covers a dense investigation window while staying
#: bounded.  Only COMPLETED and OVERFLOW records are retained; see
#: ``_MAX_TRACKED_BATCHES`` for in-flight tracking.
_MAX_BATCHES: Final[int] = 2000
#: Hard bound on the recording window, measured from collector construction.
_MAX_SECONDS: Final[float] = 900.0
#: How often a running collector re-emits its summary.  A one-shot summary was
#: too coarse for a rotating 1-4 minute episode: the event must be resolvable
#: while it is happening, not only after a 15-minute run.
DEFAULT_SUMMARY_INTERVAL_SECONDS: Final[float] = 30.0
#: Bound on simultaneously tracked in-flight batches.  A station has at most a
#: handful of channels; this only guards against a leak if a begin is never
#: paired with a finish.
_MAX_INFLIGHT_BATCHES: Final[int] = 64
#: Bound on the segment-name list retained per batch, so a malformed backlog
#: cannot inflate one record without bound.
_MAX_SEGMENT_NAMES: Final[int] = 32

_TRUE_VALUES: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class _BeginRecord:
    batch_id: str
    channel: str
    generation: int
    segment_indices: tuple[int, ...]
    segment_names: tuple[str, ...]
    queue_depth: int
    oldest_queue_age_seconds: float
    started_at: float


@dataclass
class BatchDiagnosticCollector:
    """Bounded per-batch records with a cumulative summary.

    Not thread-safe by accident: the scan thread (gate decisions, discards) and
    the per-channel ASR pool threads (completions) both record, so ``_begin``
    and ``_finish`` take a lock. The lock is held only for dict/integer work,
    never across scan or ASR work, so it cannot serialise the path it measures.
    """

    window_seconds: float = _MAX_SECONDS
    max_batches: int = _MAX_BATCHES
    summary_interval_seconds: float = DEFAULT_SUMMARY_INTERVAL_SECONDS
    _started_at: float = field(default=0.0, init=False)
    _expires_at: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _inflight: dict[str, _BeginRecord] = field(default_factory=dict, init=False)
    _records: list[dict[str, object]] = field(default_factory=list, init=False)
    _last_summary_at: float = field(default=0.0, init=False)
    _exhausted: bool = field(default=False, init=False)
    _last_emitted_events: int = field(default=0, init=False)
    #: Set once the window/cap-closing report has been emitted. Nothing may
    #: be emitted afterwards, even if a batch is still in flight: a hung
    #: batch must not re-log the same unfinished record every interval.
    _terminal_emitted: bool = field(default=False, init=False)

    @property
    def enabled(self) -> bool:
        """True once recording has started and the window has not expired.

        The tap consults this BEFORE any ``Path.stat``/clock read so the
        default-off and post-window paths are genuinely inert.
        """

        return not self._exhausted and time.monotonic() < self._expires_at

    def __post_init__(self) -> None:
        # Resolve the clock through the module attribute at CONSTRUCTION time so
        # a test can install a fake clock before building the collector.
        self._started_at = time.monotonic()
        self._expires_at = self._started_at + self.window_seconds
        self._last_summary_at = self._started_at

    def begin_batch(
        self,
        *,
        channel: str,
        generation: int,
        batch_id: str,
        segment_indices: tuple[int, ...] | list[int],
        segment_names: tuple[str, ...] | list[str],
        queue_depth: int,
        oldest_queue_age_seconds: float,
    ) -> None:
        """Record that a batch was selected, BEFORE it runs.

        Called at the decision point, so the queue depth and age describe the
        state the gate actually saw. A batch that is begun and never finished
        is still reported (``outcome="unfinished"``) with its age, because a
        rotating empty window can leave work queued for minutes.
        """

        with contextlib.suppress(Exception), self._lock:
            if time.monotonic() >= self._expires_at or len(self._records) >= self.max_batches:
                self._exhausted = True
                return
            if len(self._inflight) >= _MAX_INFLIGHT_BATCHES:
                return
            self._inflight[str(batch_id)] = _BeginRecord(
                batch_id=str(batch_id),
                channel=str(channel),
                generation=int(generation),
                segment_indices=tuple(int(i) for i in segment_indices)[:_MAX_SEGMENT_NAMES],
                segment_names=tuple(str(n) for n in segment_names)[:_MAX_SEGMENT_NAMES],
                queue_depth=int(queue_depth),
                oldest_queue_age_seconds=_round_seconds(oldest_queue_age_seconds),
                started_at=time.monotonic(),
            )

    def finish_batch(
        self,
        *,
        batch_id: str,
        outcome: str,
        reason: str,
        consumed_segments: int,
        expired_unconfirmed_cues: int,
        elapsed_seconds: float,
        queue_depth_after: int | None = None,
        committed_review_items: int = 0,
    ) -> None:
        """Close a begun batch with its OUTCOME and reason.

        ``outcome`` is one of the small closed set the tap actually produces in
        this slice, or ``"unfinished"`` when the summary is taken mid-batch.
        """

        with contextlib.suppress(Exception), self._lock:
            begun = self._inflight.pop(str(batch_id), None)
            if begun is None:
                return
            if time.monotonic() >= self._expires_at or len(self._records) >= self.max_batches:
                self._exhausted = True
                return
            self._records.append(
                {
                    "channel": begun.channel,
                    "generation": begun.generation,
                    "batch_id": str(batch_id),
                    "segment_indices": list(begun.segment_indices),
                    "segment_names": list(begun.segment_names),
                    "queue_depth": begun.queue_depth,
                    "queue_depth_after": (
                        int(queue_depth_after)
                        if queue_depth_after is not None
                        else begun.queue_depth
                    ),
                    "oldest_queue_age_seconds": begun.oldest_queue_age_seconds,
                    "outcome": str(outcome),
                    "reason": str(reason),
                    "consumed_segments": int(consumed_segments),
                    "committed_review_items": int(committed_review_items),
                    "expired_unconfirmed_cues": int(expired_unconfirmed_cues),
                    "elapsed_seconds": _round_seconds(elapsed_seconds),
                    "age_seconds": _round_seconds(time.monotonic() - begun.started_at),
                }
            )

    def summarise(self, *, force: bool = False) -> dict[str, object]:
        """Return (and log) the per-batch summary.

        FINISHED records are returned as recorded. In-flight batches are
        reported as ``outcome="unfinished"`` with their age, never dropped, so
        a batch still stuck in ASR when the episode is observed is visible.
        """

        now = time.monotonic()
        with self._lock:
            if not force and now - self._last_summary_at < int(self.summary_interval_seconds):
                return {}
            self._last_summary_at = now
            # BOUNDED EMISSION. The window may be over (or the record cap
            # hit), but activity since the previous summary -- including
            # records and in-flight state -- must still be reported exactly
            # ONCE. After that final report a frozen collector is silent.
            window_over = now >= self._expires_at or len(self._records) >= self.max_batches
            if window_over:
                self._exhausted = True
            # TERMINAL LATCH: once the window/cap-closing report has been
            # emitted, this collector is silent for good. This is what keeps
            # an in-flight batch that outlives the window (e.g. a hung ASR
            # call) from re-emitting the same unfinished record forever.
            if self._terminal_emitted:
                return {}
            # Nothing new to say: no duplicate cumulative payload, whether
            # or not the window is over. This is the latch that was
            # previously assigned but never consulted.
            if self._last_emitted_events >= len(self._records) and not self._inflight:
                return {}
            records = list(self._records[self._last_emitted_events :])
            # The final report is the last chance to surface in-flight work,
            # so include it on the window-closing emission rather than
            # letting it be dropped by the early return below.
            if window_over or self._inflight:
                for begun in self._inflight.values():
                    records.append(
                        {
                            "channel": begun.channel,
                            "generation": begun.generation,
                            "batch_id": begun.batch_id,
                            "committed_review_items": 0,
                            "segment_indices": list(begun.segment_indices),
                            "segment_names": list(begun.segment_names),
                            "queue_depth": begun.queue_depth,
                            "queue_depth_after": None,
                            "oldest_queue_age_seconds": begun.oldest_queue_age_seconds,
                            "outcome": "unfinished",
                            "reason": "still-in-flight",
                            "consumed_segments": 0,
                            "expired_unconfirmed_cues": 0,
                            "elapsed_seconds": _round_seconds(now - begun.started_at),
                            "age_seconds": _round_seconds(now - begun.started_at),
                        }
                    )
            self._last_emitted_events = len(self._records)
            exhausted = self._exhausted
            if window_over:
                # This was the ONE closing report. Include the in-flight
                # record(s) above, then never emit again.
                self._terminal_emitted = True
        if not records:
            return {}
        # A broken logging handler must never propagate out of diagnostic code.
        with contextlib.suppress(Exception):
            _LOG.info(
                "Caption tap batch diagnostic summary %s",
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "window_s": self.window_seconds,
                        "exhausted": exhausted,
                        "batches": records,
                    }
                ),
            )
        return {"batches": records, "exhausted": exhausted}


@dataclass
class NullBatchDiagnostic:
    """Inert collector used whenever the switch is off.

    Every method is a no-op and reads NO clock, so an operator who never opts
    in pays nothing and production behaviour is bit-for-bit unchanged.
    """

    enabled: bool = False

    def begin_batch(self, **_: object) -> None:  # pragma: no cover - inert
        return None

    def finish_batch(self, **_: object) -> None:  # pragma: no cover - inert
        return None

    def summarise(self, *, force: bool = False) -> dict[str, object]:  # pragma: no cover
        return {}


def _round_seconds(value: float) -> float:
    return round(max(0.0, float(value)), 3)


def _env_truthy(raw: str) -> bool:
    return raw.strip().lower() in _TRUE_VALUES


def batch_diagnostic_from_env() -> BatchDiagnosticCollector | NullBatchDiagnostic:
    """Build the collector the environment asks for (default: inert).

    Called once per tap worker at construction, matching how the tap reads its
    other settings.
    """

    raw = os.environ.get(BATCH_DIAGNOSTIC_ENV_VAR, "")
    if not _env_truthy(raw):
        return NullBatchDiagnostic()
    return BatchDiagnosticCollector()
