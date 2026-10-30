# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Per-channel exponential backoff for the live caption tap.

Field defect (tester DESKTOP-VBMA6O5, 1.0.0-beta.5 candidate kit, three
channels ON_AIR on the GStreamer engine): the caption tap could not keep up on
a CPU-only station, so :class:`~civiccast.captions.tap_worker.CaptionTapWorker`
detected overload, cleared the channel's captions, dropped the stale audio --
and then immediately tried again on the very next scan. The result was a
``CRITICAL`` overload line every ~30 seconds per channel forever and a control
plane burning ~247% of a core against three playout workers that were being
killed by their own 10-second no-output stall watchdog.

Captions are best effort. Playout is the product. This policy is the piece
that makes that ordering real: after an overload a channel's ASR is PAUSED for
an exponentially growing window (base, 2x, 4x ... capped), so a station that
cannot transcribe in real time spends its CPU on air instead of on a
transcription it is going to throw away anyway. A channel is only forgiven --
its escalation stepped back down -- after it has stayed within capacity for
several consecutive scans, and each such window removes ONE rung rather than
erasing the history, so a channel that flaps between "just barely coping" and
"overloaded" keeps escalating rather than resetting to the base delay every
other scan, while a channel that stays healthy long enough still walks all the
way back to zero and is released.

This module has no filesystem access, no logging, and an injectable clock --
the state machine is the part that has to be right, so it is the part that is
unit-testable on its own.

It is NOT pure, and calling it that would be a lie with a race behind it:
:class:`CaptionBackoffPolicy` owns mutable per-channel state that is touched
from two different threads. ``record_within_capacity`` is called from inside
:class:`~concurrent.futures.ThreadPoolExecutor` workers (one per channel being
transcribed), while ``record_overload``/``is_paused``/``forget`` are called
from the scan thread. Every public method therefore takes ``self._lock``; the
methods are short and never call back into the worker, so the lock is
uncontended in practice and cannot deadlock.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

__all__ = [
    "DEFAULT_BASE_BACKOFF_SECONDS",
    "DEFAULT_MAX_BACKOFF_SECONDS",
    "DEFAULT_RECOVERY_SCANS",
    "DEFAULT_RECOVERY_WINDOWS",
    "CaptionBackoffPolicy",
    "ChannelBackoffState",
]

#: One base window is deliberately much longer than one scan (default poll is
#: 2s): the point is to stop paying for ASR at all for a while, not to retry
#: promptly.
#:
#: Item 79 (sandbox candidate 3b, MEASURED: 10 "Caption tap overload" events
#: with a cluster of GStreamer worker stalls inside them, the same root cause
#: as the tester's beta.4 soak): 60s was not a long enough first pause -- a
#: station that overloads once is likely to still be catching up 60s later,
#: so it re-overloads almost immediately and burns a second escalation cycle
#: proving what the first one already showed. Doubled to 120s so the FIRST
#: pause alone gives a struggling station real recovery room before ASR is
#: attempted again.
DEFAULT_BASE_BACKOFF_SECONDS = 120.0
#: Ceiling on the exponential growth. A permanently under-powered station
#: retries every 15 minutes -- enough that recovery (the operator lowering the
#: caption tier, or the channel going off air) is noticed, cheap enough that it
#: costs nothing measurable.
DEFAULT_MAX_BACKOFF_SECONDS = 900.0
#: Consecutive within-capacity scans required to step the escalation down ONE rung.
DEFAULT_RECOVERY_SCANS = 3
#: Consecutive healthy WINDOWS (each ``recovery_scans`` scans) required before
#: ONE rung of escalation is retired. One short window (~6s at the 2s poll) is
#: not evidence of recovery on a loaded station, so stepping down is 3 windows
#: (~18s per rung) and full release needs the whole ladder walked down.
DEFAULT_RECOVERY_WINDOWS = 3


@dataclass
class ChannelBackoffState:
    """One channel's overload/recovery bookkeeping."""

    consecutive_overloads: int = 0
    healthy_scans: int = 0
    paused_until: float = 0.0
    #: The delay the current pause was opened with (reported to the operator).
    pause_seconds: float = 0.0


class CaptionBackoffPolicy:
    """Exponential per-channel pause after caption-tap overload.

    Args:
        base_seconds: the first pause window; each further consecutive
            overload doubles it.
        max_seconds: ceiling for the doubled window.
        recovery_scans: consecutive within-capacity scans that must pass
            before a channel's escalation is forgiven.
        monotonic: injectable clock (``time.monotonic`` semantics -- never a
            wall clock, so a station clock step cannot un-pause a channel).
    """

    def __init__(
        self,
        *,
        base_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS,
        max_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        recovery_scans: int = DEFAULT_RECOVERY_SCANS,
        recovery_windows: int = DEFAULT_RECOVERY_WINDOWS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if base_seconds <= 0:
            raise ValueError("Caption tap backoff base_seconds must be greater than zero.")
        if max_seconds < base_seconds:
            raise ValueError(
                "Caption tap backoff max_seconds must be at least base_seconds "
                f"({base_seconds}); got {max_seconds}."
            )
        if recovery_scans < 1:
            raise ValueError("Caption tap backoff recovery_scans must be at least 1.")
        if recovery_windows < 1:
            raise ValueError("Caption tap backoff recovery_windows must be at least 1.")
        self._base_seconds = float(base_seconds)
        self._max_seconds = float(max_seconds)
        self._recovery_scans = int(recovery_scans)
        self._recovery_windows = int(recovery_windows)
        self._monotonic = monotonic
        self._states: dict[str, ChannelBackoffState] = {}
        # Guards ``_states``. See the module docstring: the scan thread and the
        # per-channel ASR pool threads both reach this object.
        self._lock = threading.Lock()

    @property
    def base_seconds(self) -> float:
        return self._base_seconds

    @property
    def max_seconds(self) -> float:
        return self._max_seconds

    def state(self, channel_id: str) -> ChannelBackoffState:
        """A SNAPSHOT of this channel's state (zeroed when never seen).

        A copy, not the live object: the caller reads it outside the lock, and
        handing out the mutable instance would let a reader observe a half-
        applied escalation from another thread.
        """

        with self._lock:
            state = self._states.get(channel_id)
            return ChannelBackoffState() if state is None else replace(state)

    def is_paused(self, channel_id: str) -> bool:
        """Whether ASR for ``channel_id`` is currently suspended."""

        with self._lock:
            state = self._states.get(channel_id)
            return state is not None and self._monotonic() < state.paused_until

    def remaining_seconds(self, channel_id: str) -> float:
        """Seconds until ``channel_id`` may transcribe again (0.0 when free)."""

        with self._lock:
            state = self._states.get(channel_id)
            if state is None:
                return 0.0
            return max(0.0, state.paused_until - self._monotonic())

    def record_overload(self, channel_id: str) -> ChannelBackoffState:
        """Escalate ``channel_id`` and open a new pause window.

        Returns the resulting state so the caller can log the escalation ONCE,
        at the moment the pause opens, instead of re-logging every scan.
        """

        with self._lock:
            state = self._states.setdefault(channel_id, ChannelBackoffState())
            state.consecutive_overloads += 1
            state.healthy_scans = 0
            delay = min(
                self._base_seconds * (2 ** (state.consecutive_overloads - 1)),
                self._max_seconds,
            )
            state.pause_seconds = delay
            state.paused_until = self._monotonic() + delay
            return replace(state)

    def record_within_capacity(self, channel_id: str) -> str:
        """Record one healthy scan and report any ladder transition.

        Returns a token the caller can log WITHOUT re-reading private state:
        ``"stepped-down"`` when a full recovery window retired one rung,
        ``"released"`` when the channel returned to rung 0 and its state was
        dropped, or ``""`` when nothing changed yet. Field instrumentation
        (beta.9 ladder, 2026-09-19): the shipped log only recorded the overload
        line, so a live second trip could not tell us whether the sustained
        recovery bar was ever reached. This return value is that evidence.
        """
        """Count one healthy scan; step the channel back down once it has enough.

        Forgiveness is deliberately NOT immediate. A channel that alternates
        between coping and overloading is a station that cannot transcribe in
        real time; resetting on its first good scan would put it back on the
        base delay forever and reproduce the every-30-seconds churn this
        policy exists to stop.

        FIELD DEFECT (beta.9 N=3 ladder, 2026-09-18, three channels ON_AIR):
        this method used to DELETE the whole channel state once
        ``recovery_scans`` healthy scans had passed. At the shipped default of
        3 scans -- about six seconds at the 2s poll -- that erased
        ``consecutive_overloads`` far too eagerly, so a channel that tripped,
        got a few seconds of relief and then tripped again restarted at the
        BASE window instead of resuming the ladder. Live evidence: 110 of 111
        gate trips logged ``overload #1``, and the station sat in a perpetual
        120s-on / 120s-off cycle that neither escalated nor settled.

        Recovery now STEPS THE LADDER DOWN one rung per full recovery window
        instead of destroying the history. A brief healthy spell therefore no
        longer earns a reset (a flapping channel resumes at a higher window),
        while sustained health still walks all the way back to zero and is
        fully released -- which is the behaviour the existing contract in
        tests/captions/test_caption_tap_backoff.py
        (``test_enough_healthy_scans_forgive_the_channel``) already required,
        just with a proxy that a real multi-channel station cannot satisfy in
        six seconds.
        """

        with self._lock:
            state = self._states.get(channel_id)
            if state is None:
                return ""
            if self._monotonic() < state.paused_until:
                # Still inside a pause window; a caller draining the backlog
                # does not count as the channel having recovered.
                return ""
            state.healthy_scans += 1
            # The bar for STEPPING DOWN is deliberately higher than the bar for
            # one short spell of coping: ``recovery_windows`` consecutive
            # healthy windows (each ``recovery_scans`` scans) must be earned
            # before a rung is retired. Under the shipped defaults that is 3
            # windows x 3 scans = 9 consecutive within-capacity scans (~18s at
            # the 2s poll) per rung, versus the single ~6s window the defective
            # version treated as full recovery.
            step_scans = self._recovery_scans * self._recovery_windows
            if state.healthy_scans < step_scans:
                return ""
            state.healthy_scans = 0
            if state.consecutive_overloads > 0:
                # Descend ONE rung, keeping the remaining history, so a channel
                # that flaps resumes higher than base instead of at base.
                state.consecutive_overloads -= 1
                state.pause_seconds = min(
                    self._base_seconds * (2 ** (state.consecutive_overloads - 1)),
                    self._max_seconds,
                )
                return "stepped-down"
            # Rung 0 and a full sustained-health period earned: release.
            del self._states[channel_id]
            return "released"

    def forget(self, channel_id: str) -> None:
        """Drop all state for a channel (it went off air / was removed)."""

        with self._lock:
            self._states.pop(channel_id, None)
