# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""24/7 channel automation driver (cable automation CA-2).

The egress daemon loop previously ran only under the ``civiccast egress run``
CLI — the app itself never drove channels. This module makes playout
self-driving:

* every poll, each enabled channel's command queue is processed
  (``daemon.process_once``), which also supervises the encoder process
  (crash auto-restart lives in the daemon already);
* channels flagged ``auto_start`` that have no live encoder get a ``start``
  command re-issued (``issued_by="channel-automation"``) — this is what
  brings a 24/7 channel back on air after an app or machine restart, since
  the operator's original start command was consumed long ago;
* a channel sitting on FALLBACK_SLATE gets a ``reload`` the moment the
  schedule yields a real source plan again, so a due program takes over the
  gap without operator action;
* a channel pass that runs longer than
  ``CIVICCAST_AUTOMATION_PASS_WATCHDOG_SECONDS`` (30s by default) has the
  automation thread's live Python stack dumped into the log, repeatedly until
  the pass ends, and the pass's total duration reported when it does
  (``_PassWatchdog``) -- so the next stall says which call it is stuck in
  (U01, 2026-09-24: ~446s in one pass, and nothing in the log named the call).

Combined with join-in-progress source plans (a rejoin resumes the current
program at the wall-clock offset), an app restart puts every automated
channel back where its published log says it should be.

Deployment shape mirrors the other lifespan workers: ``run_once`` is the
testable unit, ``run_forever`` survives and logs scan exceptions, and the
app lifespan runs the loop on a thread when ``CIVICCAST_CHANNEL_AUTOMATION``
is ``inline`` (the default — safe: with no auto_start channels and an empty
command queue each tick is a cheap poll; no encoder ever spawns uncommanded).
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from civiccast.eas.service import EasDisplayService
from civiccast.eas.store import EasStore
from civiccast.egress.daemon import AlertEvaluatorHook, EgressDaemon
from civiccast.egress.engine_select import build_encoder_strategy, gstreamer_engine_selected
from civiccast.egress.errors import SourcePrepareError
from civiccast.egress.gst.reload_policy import rollover_trigger_at
from civiccast.egress.models import (
    ChannelAutomationRollup,
    EgressCommand,
    EgressProofEvent,
    EgressSourcePlan,
)
from civiccast.egress.preparer import preparation_timeout_seconds_from_env
from civiccast.egress.store import EgressStore
from civiccast.native.station_runtime import EGRESS_DEGRADED_REASON_ENV

if TYPE_CHECKING:
    from civiccast.alerting.models import AlertConditionKind
    from civiccast.reporting.asrun_outbox import AsRunOutbox

_LOG = logging.getLogger(__name__)

#: The alerting condition raised when the GStreamer egress engine was found
#: corrupt and unrepairable at station-environment build time and egress was
#: switched to the FFmpeg concat engine to keep the channel airing
#: (``station_runtime._resolve_gstreamer_egress_environment`` sets
#: :data:`~civiccast.native.station_runtime.EGRESS_DEGRADED_REASON_ENV`). Seeded
#: CRITICAL (alerting migration 0039), so it forces the operator safe-to-air
#: surface OFF the green "healthy" state -- the channel is airing on FFmpeg and
#: is never dark, but the primary egress engine is down and the operator must
#: know loudly. The alert summary says the channel is still airing so a red
#: banner is not misread as dead air. Raised HERE, not in ``station_runtime``:
#: that module runs pre-DB under LocalSystem and holds no ``Session``; this
#: control-plane builder does, and is where the FFmpeg fallback engine is
#: actually selected (``build_encoder_strategy`` below).
_EGRESS_DEGRADED_ALERT_KIND: AlertConditionKind = "encoder-death"


def _raise_egress_degraded_alert(session_factory: Any, *, reason: str) -> None:
    """Raise the loud, critical GStreamer-egress-degraded operator alert.

    Never propagates (mirrors the supervisor's ``_AlertingOutbox`` posture): an
    alert INSERT against a degraded/absent DB must not block control-plane
    startup. The caller owns the transaction via ``session_factory``; a stable
    ``resource_ref`` lets the alerting store dedupe across restarts rather than
    spamming a fresh event every boot while the closure stays corrupt.
    """

    try:
        from civiccast.alerting.store import record_alert_condition

        # session_factory is a @contextmanager callable (see
        # civiccast.app._wire_stage_f_workers's _session_factory, the real
        # caller) -- calling it and reaching for .commit()/.close() directly
        # (the shape this used to have) raises AttributeError on the
        # _GeneratorContextManager it actually returns, so this alert has
        # never actually been recorded; caught here only because DEFECT C's
        # _ChannelAutomationAlerts._record copied the identical shape and a
        # new test for THAT code exercised the failure path for the first
        # time. Fixed alongside it, same file, same bug shape.
        with session_factory() as session:
            record_alert_condition(
                session,
                kind=_EGRESS_DEGRADED_ALERT_KIND,
                resource_ref="station:egress-engine",
                source_section="egress",
                summary=(
                    "GStreamer egress unavailable; channel airing on the FFmpeg "
                    "fallback engine (DEGRADED, not off air). Run the operator "
                    "'repair GStreamer runtime & restore full egress' action."
                ),
                detail=reason,
            )
            session.commit()
    except Exception:
        _LOG.exception(
            "raising the GStreamer egress-degraded alert failed; continuing -- "
            "alerting must never block control-plane startup."
        )


#: DEFECT C: a failed channel-automation pass (or one command inside it) used to
#: be visible ONLY as a log line -- "Channel automation pass failed for %s" --
#: which no operator would ever see. Wired to the same alerting hub every other
#: operational condition in this file already uses (no new UI surface).
_CHANNEL_AUTOMATION_FAILURE_KIND: AlertConditionKind = "channel-automation-failure"


class _ChannelAutomationAlerts:
    """Fires/clears the ``channel-automation-failure`` condition (DEFECT C).

    One instance is shared between the daemon's per-command failure hook
    (``EgressDaemon(command_failure_hook=...)``, fires mid-pass, inside
    ``process_once`` -- see the DEFECT D fix there) and
    ``ChannelAutomationService.run_once``'s per-channel exception handler
    (fires for failures OUTSIDE ``process_once``, e.g. a source-plan/relay/
    replan exception in ``_run_channel_pass``), so both routes land the same
    alert kind/resource_ref under one auto-clear contract: the alert for a
    channel stays firing until that channel completes one poll pass with NO
    failure recorded during it -- ``begin_tick``/``end_tick`` bracket every
    pass so a mid-pass command failure and a whole-pass failure both count,
    and a clean pass clears exactly once (not spammed every ~2s poll tick;
    ``record_alert_condition`` would happily create a fresh pre-resolved
    audit row on every call with nothing firing, so ``_firing`` gates that).
    Never propagates -- same posture as ``_raise_egress_degraded_alert``.
    """

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory
        self._firing: set[str] = set()
        self._failed_this_tick: set[str] = set()

    def begin_tick(self, channel_id: str) -> None:
        self._failed_this_tick.discard(channel_id)

    def on_command_failure(
        self, channel_id: str, command: EgressCommand, exc: BaseException
    ) -> None:
        self._failed_this_tick.add(channel_id)
        self._raise(
            channel_id,
            summary=f"Egress command {command.action!r} failed for channel {channel_id!r}.",
            detail=(
                f"command_id={command.command_id} issued_by={command.issued_by}: "
                f"{exc.__class__.__name__}: {exc}"
            ),
        )

    def on_pass_failure(self, channel_id: str, *, detail: str) -> None:
        self._failed_this_tick.add(channel_id)
        self._raise(
            channel_id,
            summary=f"Channel automation pass failed for channel {channel_id!r}.",
            detail=detail,
        )

    def end_tick(self, channel_id: str) -> None:
        """Call once a channel's pass has completed without raising."""
        if channel_id in self._failed_this_tick:
            return  # a command failed THIS tick even though the pass itself didn't raise
        if channel_id not in self._firing:
            return  # nothing to clear -- do not spam a resolved event every poll
        self._firing.discard(channel_id)
        self._record(
            channel_id,
            summary=f"Channel automation for {channel_id!r} recovered.",
            detail="The channel completed a poll pass with no failure.",
            resolved=True,
        )

    def _raise(self, channel_id: str, *, summary: str, detail: str) -> None:
        self._firing.add(channel_id)
        self._record(channel_id, summary=summary, detail=detail, resolved=False)

    def _record(self, channel_id: str, *, summary: str, detail: str, resolved: bool) -> None:
        try:
            from civiccast.alerting.store import record_alert_condition

            # session_factory is a @contextmanager callable -- see the note
            # in _raise_egress_degraded_alert above (same file) for why this
            # must be a ``with`` block, not a raw session.commit()/.close().
            with self._session_factory() as session:
                record_alert_condition(
                    session,
                    kind=_CHANNEL_AUTOMATION_FAILURE_KIND,
                    resource_ref=f"egress-channel:{channel_id}",
                    source_section="egress",
                    summary=summary[:300],
                    detail=detail[:2000],
                    resolved=resolved,
                )
                session.commit()
        except Exception:
            _LOG.exception(
                "recording the channel-automation-failure alert failed for %s; "
                "continuing -- alerting must never block channel automation.",
                channel_id,
            )


AUTOMATION_MODE_INLINE = "inline"
AUTOMATION_MODE_OFF = "off"
_AUTOMATION_MODES = (AUTOMATION_MODE_INLINE, AUTOMATION_MODE_OFF)

_ISSUED_BY = "channel-automation"

__all__ = [
    "ChannelAutomationService",
    "ChannelAutomationSettings",
    "build_channel_automation",
    "default_egress_work_dir",
    "pass_watchdog_repeat_seconds_from_env",
    "pass_watchdog_threshold_seconds_from_env",
    "rollover_lead_seconds_from_env",
]

#: BETA.10 U02: operator override for the GStreamer path's rollover lead (see
#: ``ChannelAutomationService._rollover_lead_seconds``). Its value is a
#: positive float of seconds; every invalid or non-positive value is logged
#: and ignored, exactly as ``preparer.preparation_timeout_seconds_from_env``
#: treats its own variable, so a typo can never disable the rollover.
ROLLOVER_LEAD_ENV = "CIVICCAST_EGRESS_ROLLOVER_LEAD_SECONDS"

#: BETA.10 U04: the stuck-pass watchdog. U01 established that the single
#: automation thread spent ~446s inside one channel pass on 2026-09-24
#: (13:30:44,903 -> 13:38:10,887) and that nothing in the log said which call
#: it was in, because a pass logs nothing until it returns. These two tunables
#: are that missing answer: how long one pass may run before its stack is
#: dumped (30s), and how often a pass that is STILL stalled is re-dumped (60s).
#: Both are read once per service, by
#: ``pass_watchdog_threshold_seconds_from_env`` /
#: ``pass_watchdog_repeat_seconds_from_env``.
#: The registry's spelling is ``CIVICCAST_`` (two C's, 9 characters), as it is
#: for every other variable this module reads. The one-C ``CIVICAST_`` form is
#: one character short and renders identically in most fonts -- the drift U03
#: fixed in ``env_vars.py`` and that this unit introduced here twice (the two
#: constants, and the module docstring). These names are new in U04, so there
#: is no legacy spelling to keep and no alias to honour.
WATCHDOG_THRESHOLD_ENV = "CIVICCAST_AUTOMATION_PASS_WATCHDOG_SECONDS"
WATCHDOG_REPEAT_ENV = "CIVICCAST_AUTOMATION_PASS_WATCHDOG_REPEAT_SECONDS"
_DEFAULT_PASS_WATCHDOG_SECONDS = 30.0
_DEFAULT_PASS_WATCHDOG_REPEAT_SECONDS = 60.0
#: The watcher thread's name. Named for the thread it watches -- the production
#: loop runs as ``civiccast-channel-automation`` (``app.py``), so an operator's
#: thread dump shows the watcher directly beside its subject.
WATCHDOG_THREAD_NAME = "civiccast-channel-automation-pass-watchdog"

#: The longest sleep ``threading.Event.wait`` can time on this platform
#: (CPython's ``TIMEOUT_MAX``: 4294967s here). U04 review (R1): a FINITE
#: interval above it raises ``OverflowError`` -- reproduced at U04 review time
#: as ``OverflowError: timeout value is too large`` from the watcher's own loop
#: condition, which killed the thread and silently ended the diagnostics. The
#: non-finite case was guarded from the start; this is the same failure reached
#: by an ordinary large number, so both the reader (rejects) and ``_watch``
#: (clamps, for a directly constructed watchdog) have to know about it.
_MAX_WATCHDOG_SECONDS = threading.TIMEOUT_MAX

#: The longest the watcher may sleep between look-ups of the pass it is
#: watching, whatever the operator's threshold and repeat are (they may both be
#: minutes). The wake period is ``min(threshold, repeat, this)``: the threshold
#: bounds how long a crossed threshold can go unnoticed, the repeat bounds a
#: tiny threshold from waking rarely, and this cap keeps the operator's own
#: numbers out of the wake-up period entirely at the shipped 30s/60s. U04
#: coordinator fix 2: the period used to BE the repeat after the first tick, so
#: the first report of a fresh stall could land a whole repeat after the
#: threshold -- ~90s at the defaults, and the whole of a short stall.
_MAX_WATCHDOG_TICK_SECONDS = 5.0


def default_egress_work_dir() -> Path:
    """Default directory for egress plans, prepared segments, and slates."""

    configured = os.environ.get("CIVICCAST_EGRESS_WORK_DIR")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "CivicCast" / "egress"
    return Path.home() / ".local" / "share" / "civiccast" / "egress"


def rollover_lead_seconds_from_env() -> float | None:
    """The operator's rollover-lead override (``ROLLOVER_LEAD_ENV``), or
    ``None`` when unset/invalid/non-positive -- the caller then uses the
    computed default (``ChannelAutomationService._rollover_lead_seconds``).

    Mirrors ``preparer.preparation_timeout_seconds_from_env``'s treatment of
    bad input: a warning naming the offending value, then the safe default.
    ``None`` rather than a sentinel value keeps "the operator said nothing"
    distinct from any legitimate lead the override could carry."""

    raw = os.environ.get(ROLLOVER_LEAD_ENV, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        _LOG.warning(
            "Invalid %s=%r; using the computed rollover lead instead.",
            ROLLOVER_LEAD_ENV,
            raw,
        )
        return None
    if value <= 0:
        _LOG.warning(
            "%s must be positive; using the computed rollover lead instead.",
            ROLLOVER_LEAD_ENV,
        )
        return None
    return value


def _watchdog_seconds_from_env(name: str, default: float, *, zero_allowed: bool) -> float:
    """Read one stuck-pass-watchdog tuning from ``name``, or ``default``.

    Mirrors ``rollover_lead_seconds_from_env``'s treatment of bad input -- a
    warning naming the offending value, then the safe default -- because a
    typo must never silently disable a stall detector. (U03's lesson, and this
    module's own: a setting that is quietly inert reads exactly like a setting
    that is working until the day it matters.)

    ``zero_allowed`` is the single asymmetry between the two tunables. ``0`` is
    the documented OFF switch for the THRESHOLD, so it is valid there and
    returned unchanged. It is invalid for the REPEAT interval: a 0s repeat
    would make the watcher thread spin on ``Event.wait(0)``, burning a core to
    re-check a pass it has just reported, which is the opposite of what a
    diagnostic aid should cost. Non-finite values are invalid for both -- a
    ``nan`` threshold compares false against every elapsed time (so every
    tick would report) and a ``nan``/``inf`` repeat raises out of
    ``Event.wait``, which would kill the watcher thread silently. U04 review
    (R1) added the ceiling: a finite value above ``_MAX_WATCHDOG_SECONDS``
    raises the same way, so it is rejected the same way. (A directly
    constructed ``_PassWatchdog`` never goes through here, so ``_watch``
    clamps as well.)

    ``-0`` is normalised to ``0.0``. It is arithmetically zero -- the
    documented off switch -- but it returns as ``-0.0``, whose sign then leaks
    into ``_PassWatchdog.enabled``'s comparison and into anything that echoes
    the value back. U04 review (R2)."""

    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        _LOG.warning("Invalid %s=%r; using %.1fs.", name, raw, default)
        return default
    if not math.isfinite(value) or value < 0 or (value == 0 and not zero_allowed):
        _LOG.warning(
            "%s=%r must be %s; using %.1fs.",
            name,
            raw,
            "0 (disables the watchdog) or a positive number" if zero_allowed else "positive",
            default,
        )
        return default
    if value > _MAX_WATCHDOG_SECONDS:
        _LOG.warning(
            "%s=%r must be at most %.0fs (the longest wait this platform can time); using %.1fs.",
            name,
            raw,
            _MAX_WATCHDOG_SECONDS,
            default,
        )
        return default
    return 0.0 if value == 0 else value


def pass_watchdog_threshold_seconds_from_env() -> float:
    """How long one channel pass may run before the watchdog dumps the
    automation thread's stack (``WATCHDOG_THRESHOLD_ENV``, default 30s).

    ``0`` disables the watchdog completely: ``_PassWatchdog.start`` spawns no
    thread, so there is no dump, no pass-end report and no thread to find in a
    dump. The default is deliberately shorter than a channel's own rollover
    lead (690s at the shipped 300s preparation timeout): the point is to catch
    a pass that is stuck EARLY enough for its stack to still describe the
    thing that is stuck."""

    return _watchdog_seconds_from_env(
        WATCHDOG_THRESHOLD_ENV, _DEFAULT_PASS_WATCHDOG_SECONDS, zero_allowed=True
    )


def pass_watchdog_repeat_seconds_from_env() -> float:
    """How often a pass that is STILL over the threshold is re-dumped
    (``WATCHDOG_REPEAT_ENV``, default 60s). Must be positive, finite and no
    more than ``_MAX_WATCHDOG_SECONDS`` -- see ``_watchdog_seconds_from_env``.

    This value sets the log volume for a long stall, exactly and predictably:
    a stall lasting ``D`` seconds emits about ``D / repeat`` stack dumps, each
    a handful of lines. At the 60s default a 15-minute stall is ~15 dumps; at
    a DEBUG-minded 0.1s it would be ~9000. Nothing here floors the value --
    short repeats are what the tests use and an operator measuring a suspected
    stall may want them -- but the off switch (threshold ``0``) is always
    available, and the arithmetic is stated so a value can be chosen knowing
    what it costs the log."""

    return _watchdog_seconds_from_env(
        WATCHDOG_REPEAT_ENV, _DEFAULT_PASS_WATCHDOG_REPEAT_SECONDS, zero_allowed=False
    )


def _log_watchdog_failure(message: str) -> None:
    """Log a watchdog-internal failure without ever raising out of it.

    Every guard in ``_PassWatchdog`` falls back to a log call, and a log call
    is not itself safe: ``logging`` re-raises ``RecursionError`` out of a
    handler's ``emit`` rather than swallowing it, so the fallback can raise the
    very exception the guard exists to contain. Reproduced at U04 review (R3),
    at DEBUG level: ``pass_finished`` raised ``RecursionError`` out of a pass's
    ``finally`` and replaced the pass's own exception. The nested guard here is
    what makes "the watchdog can never raise into a channel pass" true rather
    than merely intended.

    ``BaseException`` -- ``KeyboardInterrupt``, ``SystemExit`` -- still
    propagates, as it does from any Python code; that is not the watchdog's to
    swallow."""

    with contextlib.suppress(Exception):
        _LOG.debug(message, exc_info=True)


class _PassWatchdog:
    """BETA.10 U04: name the call a stalled channel pass is stuck in.

    2026-09-24 (U01): the single automation thread spent ~446s inside public's
    pass and the log could not say which call it was in, because
    ``_run_channel_pass`` logs nothing until it returns. This object is the
    answer for the NEXT stall: while one pass has been running longer than
    ``threshold_seconds``, a watcher thread dumps the automation thread's live
    Python stack -- the first one within a tick of the threshold, then one
    ``repeat_seconds`` after each report while the pass stays stalled -- and the
    pass's own total duration is reported the moment it ends.

    Three properties are load-bearing, because the thread being watched is the
    one this build cannot afford to disturb:

    * the automation thread only ever records or clears its own fields under
      ``_lock`` -- the channel, the start time, the next-report time, the
      watched thread's ident and the reported flag. It never formats a stack,
      never logs and never waits;
    * all logging happens on the WATCHER thread, and deliberately after the
      lock is released, so a slow log handler can never block the automation
      thread behind the watchdog;
    * nothing here touches the store, the daemon, or any daemon lock. The only
      inputs are ``sys._current_frames()`` and this object's own state.

    With ``threshold_seconds == 0`` the watchdog is disabled: ``start`` spawns
    no thread at all and every method here becomes a state update that logs
    nothing."""

    def __init__(
        self,
        *,
        threshold_seconds: float,
        repeat_seconds: float,
        monotonic: Callable[[], float] | None = None,
        current_frames: Callable[[], dict[int, Any]] | None = None,
    ) -> None:
        self._threshold_seconds = threshold_seconds
        self._repeat_seconds = repeat_seconds
        # The same clock the service uses, for the same reason: a test that
        # injects a clock gets one consistent notion of elapsed time.
        self._monotonic = monotonic or time.monotonic
        # ``sys._current_frames`` is a private CPython API (there is no public
        # one), injected rather than called directly so the failure path is
        # testable: a dump that raises must not take the watcher with it.
        self._current_frames = current_frames or sys._current_frames
        #: Guards ONLY the three fields below. Held for assignments and reads,
        #: never across a log call -- see the class docstring.
        self._lock = threading.Lock()
        self._channel_id: str | None = None
        self._started_at = 0.0
        #: Monotonic time at which the current pass may next be reported:
        #: ``started_at + threshold`` when it begins, ``now + repeat`` after each
        #: report. Held in the pass record rather than derived from a tick
        #: count, so the schedule does not depend on WHEN the watcher happens to
        #: wake -- a late tick reports late, never early or twice. U04
        #: coordinator fix 2: with the period alone, a pass that crossed the
        #: threshold between two ticks was first reported a whole repeat later.
        self._next_report_at = 0.0
        self._thread_ident: int | None = None
        #: Whether this pass has already been reported as over the threshold.
        #: Set by the watcher thread under ``_lock``; read and cleared by the
        #: pass's own end so it can report its total duration exactly once.
        self._reported = False
        self._stop_event = threading.Event()
        #: Touched only by ``start``/``stop``, both called from the automation
        #: thread (``run_forever``), so it needs no lock of its own.
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        """False when the threshold is 0: the documented off switch."""

        return self._threshold_seconds > 0 and self._repeat_seconds > 0

    @property
    def tick_seconds(self) -> float:
        """How long the watcher sleeps between look-ups of the pass.

        ``min(threshold, repeat, 5.0)``. Nothing is REPORTED on a tick that is
        not due -- the watcher only checks whether a report is due -- so a small
        operator value here costs wake-ups (one lock acquisition each, no stack
        formatting, no log call), never extra log lines. A ``repeat`` below a
        millisecond would make the watcher busy: the brief's formula, and a
        documented cost rather than a silent floor."""

        return min(self._threshold_seconds, self._repeat_seconds, _MAX_WATCHDOG_TICK_SECONDS)

    def start(self) -> None:
        """Start the watcher thread. No-op when disabled or already running.

        Called by ``run_forever`` -- never by ``run_once``, which is why the
        direct ``run_once`` this suite uses everywhere has no watcher thread
        and stays byte-for-byte the loop it was.

        Best-effort, deliberately. U04 review (R4): this runs on the automation
        thread, and a ``RuntimeError: can't start new thread`` here used to
        propagate out of ``run_forever`` before its ``try`` -- turning a
        non-essential diagnostic into a dead automation loop, since
        ``ThreadSupervisor`` does not restart a worker that has exited. A
        watcher that cannot start costs the diagnostics, not the station, so a
        failed spawn is logged and swallowed, and ``_thread`` is cleared so a
        later ``run_forever`` can try again."""

        if not self.enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._watch,
            name=WATCHDOG_THREAD_NAME,
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            self._thread = None
            _log_watchdog_failure("Stuck-pass watchdog could not start its watcher thread.")
            return
        self._thread = thread

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop the watcher thread and wait, bounded, for it to end.

        Bounded because this runs in ``run_forever``'s ``finally``: a watcher
        wedged in a log handler must not hold up an operator's shutdown. It is
        a daemon thread either way, so a timeout here cannot keep the process
        alive. ``self._thread`` is left set if the join times out, so a later
        ``start`` refuses to spawn a second watcher rather than double-reporting
        every stall."""

        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout)
        if not thread.is_alive():
            self._thread = None

    def pass_started(self, channel_id: str) -> None:
        """Record the pass now starting on the calling (automation) thread.

        Called from ``_run_channel_pass``. Guarded because a bookkeeping bug
        here must never fail a channel pass: this runs on the thread whose
        work is the thing being protected, and the alternative -- an exception
        from the watchdog -- would be charged to the pass."""

        try:
            with self._lock:
                self._channel_id = channel_id
                started_at = self._monotonic()
                self._started_at = started_at
                self._thread_ident = threading.get_ident()
                self._reported = False
                self._next_report_at = started_at + self._threshold_seconds
        except Exception:
            _log_watchdog_failure("Stuck-pass watchdog could not record a pass start.")

    def pass_finished(self, channel_id: str) -> None:
        """Clear the pass record; report its total duration if it tripped the
        watchdog, exactly once.

        Runs in a ``finally`` in ``_run_channel_pass``, so it must never raise:
        an exception here would REPLACE whatever the pass itself raised and
        blame the watchdog for it. The log call is inside the same guard for
        that reason -- a broken log handler is not the pass's problem -- and
        the guard's own fallback goes through ``_log_watchdog_failure``, whose
        nested guard is what closes that last path (U04 review, R3)."""

        try:
            with self._lock:
                if self._channel_id != channel_id:
                    # Not this pass (already cleared, or a different channel
                    # owns the record): nothing to clear, nothing to report.
                    return
                elapsed = self._monotonic() - self._started_at
                reported = self._reported
                threshold = self._threshold_seconds
                self._channel_id = None
                self._thread_ident = None
                self._reported = False
                self._next_report_at = 0.0
            if not reported:
                return
            _LOG.warning(
                "Channel automation pass for %s finished after %.1fs (exceeded watchdog %.1fs).",
                channel_id,
                elapsed,
                threshold,
            )
        except Exception:
            _log_watchdog_failure("Stuck-pass watchdog could not report a pass end.")

    def _watch(self) -> None:
        """The watcher thread's loop: sleep one tick, check, repeat.

        The period is a constant ``tick_seconds`` -- ``min(threshold, repeat,
        5.0)`` -- and it decides only how promptly a DUE report is noticed, not
        how often one is emitted. Whether a report is due is the pass's own
        ``_next_report_at``, so the schedule is independent of when the watcher
        wakes: a slow machine reports late rather than early or twice.

        U04 coordinator fix 2 replaced the two-phase period this used to have
        (first tick at the threshold, every later one at the repeat). That
        period was itself the report cadence, so a pass that crossed the
        threshold between two ticks waited up to a whole repeat to be named --
        measured at U04 review (R5) as up to ~90s at the shipped 30s/60s, and
        the whole of any stall shorter than the repeat. The tick is now the
        shortest interval that can make the threshold meaningful, and the
        repeat is what it was always documented to be: the interval between
        reports of a pass that is STILL stalled.

        The whole iteration -- the wait included -- is guarded. U04 review (R1):
        the wait itself can raise (``OverflowError`` above
        ``_MAX_WATCHDOG_SECONDS``), and it sits in the loop CONDITION, so
        guarding only ``_check`` left a path that killed the thread. Any raise
        now costs one tick, and the next iteration re-checks the stop event."""

        interval = self._wait_interval(self.tick_seconds)
        while True:
            try:
                if self._stop_event.wait(interval):
                    return
                self._check()
            except Exception:
                # This thread has no supervisor: a reporting failure must cost
                # one tick, never the rest of the stall.
                _log_watchdog_failure("Stuck-pass watchdog tick failed.")

    def _wait_interval(self, seconds: float) -> float:
        """``seconds`` clamped to what ``Event.wait`` can actually time.

        Only reachable by a directly constructed watchdog (the env reader
        rejects anything above the ceiling), and cheap insurance against the
        failure mode R1 found: a wait that raises out of the loop condition
        takes the watcher with it."""

        return min(seconds, _MAX_WATCHDOG_SECONDS)

    def _check(self) -> None:
        """Dump the automation thread's stack if a report of the current pass
        is due."""

        snapshot = self._snapshot_stalled_pass()
        if snapshot is None:
            return
        channel_id, elapsed, stack = snapshot
        # One decimal: U01's postmortem had to line the ~446s stall up against
        # sub-second log timestamps, so the watchdog's own numbers carry the
        # precision that made that reconstruction possible.
        _LOG.warning(
            "Channel automation pass for %s has run %.1fs (still running); "
            "automation thread stack:\n%s",
            channel_id,
            elapsed,
            stack,
        )

    def _snapshot_stalled_pass(self) -> tuple[str, float, str] | None:
        """``(channel_id, elapsed_seconds, formatted_stack)`` for a pass whose
        report is DUE, else ``None``.

        Due means ``now >= _next_report_at``: one threshold after the pass
        started, then one ``repeat`` after each report. The due time is
        advanced under the lock before the stack is read, so the cadence is
        settled by the pass record rather than by the watcher's wake-ups.

        The pass is marked reported under the lock, BEFORE its stack is read,
        for two reasons: a pass that ends in the same instant still reports its
        own total duration, and a pass whose thread has already gone is still
        reported as having exceeded the threshold -- which its elapsed time
        genuinely did."""

        with self._lock:
            channel_id = self._channel_id
            if channel_id is None:
                return None
            ident = self._thread_ident
            now = self._monotonic()
            elapsed = now - self._started_at
            if now < self._next_report_at:
                # Not due -- either under the threshold, or reported less than
                # a repeat ago. A tick that finds nothing due logs nothing.
                return None
            self._reported = True
            self._next_report_at = now + self._repeat_seconds

        if ident is None:  # defensive: pass_started always records an ident
            return None
        frame = self._current_frames().get(ident)
        if frame is None:
            # The automation thread has exited -- there is no stack left to
            # name. Its pass-end line still reports the overrun.
            return None
        return channel_id, elapsed, "".join(traceback.format_stack(frame))


@dataclass(frozen=True)
class ChannelAutomationSettings:
    """Deployment configuration for the channel automation driver."""

    mode: str = AUTOMATION_MODE_INLINE
    poll_seconds: float = 2.0

    @classmethod
    def from_env(cls) -> ChannelAutomationSettings:
        mode = (
            os.environ.get("CIVICCAST_CHANNEL_AUTOMATION", AUTOMATION_MODE_INLINE).strip().lower()
        )
        if mode not in _AUTOMATION_MODES:
            raise ValueError(
                f"CIVICCAST_CHANNEL_AUTOMATION must be one of {', '.join(_AUTOMATION_MODES)}; "
                f"got {mode!r}."
            )
        raw_poll = os.environ.get("CIVICCAST_CHANNEL_AUTOMATION_POLL_SECONDS", "").strip()
        if not raw_poll:
            return cls(mode=mode)
        try:
            poll_seconds = float(raw_poll)
        except ValueError as exc:
            raise ValueError(
                f"CIVICCAST_CHANNEL_AUTOMATION_POLL_SECONDS must be a number; got {raw_poll!r}."
            ) from exc
        if poll_seconds <= 0:
            raise ValueError(
                f"CIVICCAST_CHANNEL_AUTOMATION_POLL_SECONDS must be positive; got {raw_poll!r}."
            )
        return cls(mode=mode, poll_seconds=poll_seconds)


class ChannelAutomationService:
    """Drive every enabled channel's daemon loop from the app lifespan."""

    def __init__(
        self,
        store: EgressStore,
        daemon: EgressDaemon,
        source_plan_provider: Any,
        *,
        settings: ChannelAutomationSettings,
        boundary_source_plan_provider: Any = None,
        ndi_supervisor_factory: Any = None,
        sdi_supervisor_factory: Any = None,
        monotonic: Any = None,
        as_run_outbox: AsRunOutbox | None = None,
        automation_alerts: _ChannelAutomationAlerts | None = None,
    ) -> None:
        self._store = store
        self._daemon = daemon
        self._source_plan_provider = source_plan_provider
        self._boundary_source_plan_provider = boundary_source_plan_provider
        self._settings = settings
        self._monotonic = monotonic or time.monotonic
        # DEFECT C: shared with the daemon's command_failure_hook (see
        # build_channel_automation) so a mid-pass command failure and a
        # whole-pass failure land the SAME alert, with ONE auto-clear
        # contract. None (tests / the bare CLI daemon loop) is a no-op --
        # a failed pass is still logged, just not alerted.
        self._alerts = automation_alerts
        # BUG C2 fix: periodic drain of the as-run durable outbox
        # (civiccast.reporting.asrun_outbox). The recorder already drains
        # opportunistically on every write; this tick is what retries a
        # backlog left behind by a DB outage once the DB comes back, without
        # needing a dedicated thread -- it rides the same poll cadence that
        # already drives every other channel-automation concern.
        self._as_run_outbox = as_run_outbox
        # Pacing prevents command storms WITHOUT the one-shot deadlock that
        # left a dark channel unstarted for an hour (issue #152, found live
        # in the CA-8 run): a dark auto_start channel is retried every
        # cooldown until it comes up. Reload keeps the per-period latch.
        self._start_retry_at: dict[str, float] = {}
        self._reload_issued: set[str] = set()
        # Audit ENG-002: reload re-issue pacing (see _check_slate_replan).
        self._replan_retry_at: dict[str, float] = {}
        self._slate_warm_retry_at: dict[str, float] = {}
        # Soak evidence 2026-09-04 (kit 4b30c99): back-to-back scheduled
        # premieres EOS'd the engine at every source-plan boundary because
        # nothing ever extended a LIVE (ON_AIR) plan before it ran out --
        # only a FALLBACK_SLATE gap re-planned (_check_slate_replan above).
        # Each channel restarted 6-8x in a 2h soak, a worker-restart blip at
        # every 10-15 min boundary. _plan_horizon tracks, per channel, the
        # (current_proof_event_id, projected wall-clock end, projected start
        # of the plan's LAST segment) of the plan the daemon is actually
        # airing; _check_plan_rollover below extends it via the SAME
        # seamless content-reload path _check_slate_replan already uses,
        # before the engine ever reaches EOS.
        # D45 fix (2026-09-05): the tuple's 4th element is the tracked plan's
        # own planned duration (seconds), used by
        # _rollover_min_interval_seconds to size the per-channel dispatch
        # floor to the plan actually on air instead of a fixed constant --
        # see that method's docstring.
        self._plan_horizon: dict[str, tuple[str | None, datetime, datetime, float]] = {}
        self._rollover_issued: set[str] = set()
        # U53 item 3 (2026-09-26): the boundary plan resolved for the NEXT
        # boundary, remembered per channel as ``(boundary, seconds, label)``.
        # ``_rollover_lead_seconds`` sizes the lead from the item that must be
        # prepared, and an adaptive lead can only ever move the dispatch
        # EARLIER -- so the duration has to be known BEFORE the trigger the
        # duration itself computes, which is why it is resolved on the first
        # poll tick after a boundary's horizon is established rather than at
        # the trigger. Keyed by the boundary it was resolved for, which is what
        # makes it once-per-boundary rather than once-per-tick: a new plan
        # means a new ``plan_end_at``, which replaces this entry. Only used to
        # size the lead; the dispatch itself still resolves its own plan at
        # dispatch time, so a schedule edited in between is not missed.
        self._rollover_asset_probe: dict[str, tuple[datetime, float | None, str | None]] = {}
        # D43 hardening (2026-09-05): the monotonic timestamp of the last
        # rollover DISPATCH per channel, enforcing
        # _rollover_min_interval_seconds between them. Each dispatch runs
        # SourcePreparer.prepare synchronously on the automation thread
        # (daemon._try_content_reload), so the cadence has to be bounded
        # whatever the plan window turns out to be.
        self._rollover_dispatched_at: dict[str, float] = {}
        # Item 78 fix 2 (coordinator review, round 2): the monotonic timestamp
        # of the last RETRY dispatch specifically (the "reload never landed"
        # path below, distinct from _rollover_dispatched_at's "any dispatch,
        # first or retry" tracking) -- enforces
        # _ROLLOVER_RETRY_MIN_INTERVAL_SECONDS between CONSECUTIVE retries.
        # Measuring from the last retry (not the original, first dispatch)
        # is what makes this floor meaningful: the retry branch is only
        # entered after _ROLLOVER_ISSUED_TIMEOUT_SECONDS (45s) has already
        # elapsed since the ORIGINAL dispatch, so a floor measured from that
        # same origin can never bind (45s already exceeds any floor <= 45s).
        # Measuring from the last retry instead means a channel stuck
        # retrying over and over (each retry itself timing out again) is
        # still capped to one retry per floor interval, not one every poll
        # tick once the daemon clears its own latch each time.
        self._rollover_retry_dispatched_at: dict[str, float] = {}
        # Hostile-review B2 fix: when the reload lands (a fresh
        # current_proof_event_id shows up), _rollover_issued is discarded by the
        # "tracked is None or tracked[0] != proof_event_id" branch below. This
        # timestamp is how a reload that DIDN'T land within
        # _ROLLOVER_ISSUED_TIMEOUT_SECONDS is detected and retried once more
        # before the plan's projected end, instead of silently waiting forever
        # for a proof-event change that a dropped/failed reload will never
        # produce.
        self._rollover_issued_at: dict[str, float] = {}
        # Item 84 fix: last _monotonic() the "did not land; retrying" WARNING
        # was actually logged for this channel's current undelivered-rollover
        # window -- rate-limits that WARNING to at most once per
        # _ROLLOVER_RETRY_WARN_INTERVAL_SECONDS while the retry stays gated
        # (see the docstring on that constant). Cleared whenever
        # _rollover_issued_at/_rollover_retry_dispatched_at are (a resolved
        # rollover, or the retry actually dispatching) so a FUTURE undelivered
        # window logs its own fresh first-crossing WARNING.
        self._rollover_retry_warned_at: dict[str, float] = {}
        # Item 84 fix, coordinator review round 2: mirrors
        # ``_rollover_retry_warned_at`` above for the SEPARATE "worker pid has
        # only been alive Ns" deferred-WARNING below (the pid-age floor can
        # gate the retry for many consecutive ~2s ticks in its own right,
        # independent of the "did not land" cadence -- measured 150 WARNINGs
        # per 300s before this fix). Same rate-limit rule, own bookkeeping
        # since the two WARNINGs gate on different conditions and can be
        # active at different times.
        self._rollover_pid_age_warned_at: dict[str, float] = {}
        # Hostile-review B2 fix: mirrors _replan_retry_at (see
        # _check_slate_replan) -- backs off re-querying the schedule after a
        # SourcePrepareError or an empty/None plan, instead of hammering the
        # provider every ~2s poll tick until the next successful dispatch.
        self._rollover_retry_at: dict[str, float] = {}
        # Item 78 fix 2: per-channel (pid, monotonic-first-seen-at) so the
        # rollover retry-timeout path (_check_plan_rollover's
        # retrying_undelivered branch) can tell a worker that just relaunched
        # apart from one that has been airing a while -- see
        # _track_worker_pid/_worker_pid_age_seconds.
        self._pid_first_seen_at: dict[str, tuple[int | None, float]] = {}
        # Issue #116: one supervised BYO-NDI relay per named channel.
        self._ndi_supervisor_factory = ndi_supervisor_factory or _default_ndi_factory
        self._ndi_relays: dict[str, Any] = {}
        # Issue #117: one supervised BYO-SDI relay per configured channel.
        self._sdi_supervisor_factory = sdi_supervisor_factory or _default_sdi_factory
        self._sdi_relays: dict[str, Any] = {}
        # BETA.10 U03: one-shot latch for the "the rollover lead is closing on
        # the engine's defer-switch watchdog" WARNING -- see
        # _warn_if_rollover_lead_nears_the_defer_watchdog. Per SERVICE lifetime,
        # not per channel: the lead is a property of the deployment's
        # configuration, identical for every channel, and _check_plan_rollover
        # calls _rollover_lead_seconds on every ~2s poll tick.
        self._rollover_lead_ceiling_warned = False
        # BETA.10 U04: the stuck-pass watchdog's state, built here (once per
        # service, like every other setting this class reads from the
        # environment) but THREADED only by ``run_forever`` -- so ``run_once``
        # called directly keeps running exactly the loop it always did, with
        # no watcher thread. See ``_PassWatchdog``.
        self._pass_watchdog = _PassWatchdog(
            threshold_seconds=pass_watchdog_threshold_seconds_from_env(),
            repeat_seconds=pass_watchdog_repeat_seconds_from_env(),
            monotonic=self._monotonic,
        )

    _START_RETRY_COOLDOWN_SECONDS = 30.0
    _RELOAD_RETRY_COOLDOWN_SECONDS = 30.0
    # Hostile-review B3 fix: the floor of how far ahead of a live plan's
    # projected end the rollover trigger fires -- the actual trigger is
    # boundary-aligned (reload_policy.rollover_trigger_at: the moment the
    # plan's LAST segment begins, or this many seconds before the projected
    # end, whichever is EARLIER). 120s gives SourcePreparer's cold-conform
    # path (which can take minutes -- daemon.py's _try_content_reload calls it
    # synchronously) real runway before the engine reaches EOS; the previous
    # fixed 30s lookahead left conform racing EOS (soak evidence, kit
    # 4b30c99). Deliberately generous: even when conform finishes well before
    # the boundary, the new leg simply waits, prerolled, for the outgoing
    # leg's own EOS (the engine's switch_at_end_of_current -- see
    # reload_policy.should_defer_switch) rather than cutting in early and
    # truncating the still-airing item.
    #
    # Hostile-review fix (NEW-3, 2026-09-05): this fixed 120s value is no
    # longer used directly as the lead -- it is only the CEILING
    # _rollover_min_lead_seconds clamps to (mirrors
    # _ROLLOVER_MIN_INTERVAL_SECONDS's relationship to
    # _rollover_min_interval_seconds, above). A flat 120s lead pushed
    # ``plan_end_at - lead`` deep into the PAST for a plan shorter than
    # 120s (an 8x3s/24-second plan), which dominates ``rollover_trigger_at``'s
    # min() over the real last-segment-start candidate and lands the
    # dispatch at or after the plan's own end. See
    # _rollover_min_lead_seconds.
    _ROLLOVER_MIN_LEAD_SECONDS = 120.0
    # BETA.10 U02/U03: the GStreamer path's rollover lead is no longer the fixed
    # 120s above -- it has to cover a preparation that is allowed to run to
    # ``CIVICCAST_EGRESS_PREPARATION_TIMEOUT_SECONDS`` (300s at the station,
    # 300s by default) plus the reload's own post-preparation round trip.
    # 2026-09-24 13:28:25: public's rollover was issued with 119s left, its
    # cold conform (measured 270s, capped at 300s) did not finish, and the
    # channel went dark at 13:30:30. See ``_rollover_lead_seconds``.
    # U03: the timeout is covered PER PASS -- in the worktree this was sized
    # in, a normalized segment runs a loudnorm measurement pass and then the
    # conform, each within that same bound, so the U02 lead of one timeout
    # covered half of THAT build's worst case. U05: the installed base this
    # candidate patches has no separate loudnorm measurement pass, so the same
    # multiplier over-estimates rather than under-estimates here.
    # See ``_ROLLOVER_PREPARATION_PASSES``.
    #
    # The reload's settle budget after preparation completes: the worker
    # pipe's reload ack is bounded at ``strategy._WORKER_PIPE_ACK_TIMEOUT_S``
    # (5s), the daemon observes settlement on its ~2s poll tick, and building
    # + prerolling the new leg is seconds of work. 30s covers that several
    # times over; the 60s margin below absorbs the rest (command drain to the
    # daemon's next tick, clock granularity) without materially extending how
    # far ahead of EOS the reload is built.
    _ROLLOVER_RELOAD_SETTLE_BUDGET_SECONDS = 30.0
    _ROLLOVER_LEAD_MARGIN_SECONDS = 60.0
    # BETA.10 U03: how many ``_run_ffmpeg`` calls ONE reload preparation of ONE
    # segment can make on the GStreamer path. ``_run_ffmpeg`` applies
    # ``self._preparation_timeout_seconds`` to EACH call
    # (``preparer.py:1373``), so one pass is not the bound -- the count is.
    # U05 re-derived that count from the INSTALLED preparer rather than from
    # the worktree's. On this path (``playout_trim_supported=False``, wired at
    # ``build_channel_automation``) the installed ``_prepare_segment`` makes
    # ONE bounded call -- the conform encode that writes the segment
    # (``preparer.py:1839``); the cache-HIT path makes one too (the stream-copy
    # at ``preparer.py:949``). The worktree makes TWO: it adds a two-pass
    # loudnorm MEASUREMENT pass (``build_loudnorm_probe_args``) ahead of the
    # encode. The installed preparer has no such pass -- its loudness probe
    # goes through ``check_streaming_loudness`` (``preparer.py:1642``), which is
    # not a ``_run_ffmpeg`` call and carries no preparation timeout of its own.
    # 2.0 is kept as written for BOTH bases deliberately: it is the worktree's
    # worst case and a superset of this installed base's, so the computed lead
    # (690s at the shipped 300s) stays an over-estimate of the bounded work one
    # preparation can do, never an under-estimate. U05 was told to keep it
    # unless it would be WRONG here; too generous is not the failure mode a
    # rollover lead has.
    # ``_conform_full_asset_into_cache`` -- itself a probe plus an encode
    # (``preparer.py:550``) -- is deliberately NOT counted: its only
    # synchronous call site is guarded by ``_playout_trim_supported``
    # (``preparer.py:1779``), which is False for the gst engine, so on this path
    # it runs only on the background warm worker (``_schedule_warm``), never
    # inside a reload's preparation.
    _ROLLOVER_PREPARATION_PASSES = 2.0
    # U53 item 3 (coordinator's engineering call, 2026-09-26): the lead above
    # is sized from the preparation TIMEOUT, which bounds what one ffmpeg pass
    # may take -- not what the item coming up actually costs. The measured cost
    # of the whole-asset conform is a REALTIME FACTOR, and the two are
    # unrelated: a 300s timeout covers a 9020.8s asset here (555s, 16.3x) only
    # because the timeout happened to exceed the real cost, and it stops
    # covering it the moment the asset is longer or the box is busier (866s
    # measured under load, 18.6-23.8x).
    #
    # ``lead = max(floor, asset_seconds / factor + margin)`` makes the lead a
    # function of the item that must be prepared. The factor is deliberately
    # conservative: 12x is slower than every rate measured on this station
    # (16.3x uncontended, 18.6-23.8x under load) so the computed lead is
    # biased long, which is the safe direction -- an over-long lead arms the
    # rollover early and lets ``should_defer_switch`` hold it until the
    # boundary (U41's deferred path), while a short one leaves the channel
    # reaching EOS with nothing armed (2026-09-24 13:28:25, public).
    # ``_ROLLOVER_LEAD_CEILING_HEADROOM_SECONDS`` still applies: a lead that
    # approaches the defer watchdog is warned about, never clamped.
    _ROLLOVER_CONFORM_REALTIME_FACTOR = 12.0
    _ROLLOVER_CONFORM_MARGIN_SECONDS = 120.0
    #: BETA.10 U03: the engine's own deferred/boundary-aligned switch watchdog,
    #: ``GstPlayoutEngine.defer_switch_timeout_s`` (``engine.py:614``, default
    #: 900.0s). If it fires first it FORCES the switch to the armed leg
    #: (``engine.py:2793``'s ``_on_defer_switch_timeout``), the opposite of what
    #: a long rollover lead is buying, so a lead approaching it is worth one
    #: warning. Read as a named constant rather than imported:
    #: ``civiccast.egress.gst.engine`` imports ``gi`` (GStreamer) at module
    #: scope and does not import at all without the packaged runtime, so
    #: importing it here would break ``automation.py`` on every plain host.
    #: ``test_automation`` pins this constant and ``engine.py``'s own default
    #: equal by reading that source text.
    _ROLLOVER_DEFER_SWITCH_TIMEOUT_SECONDS = 900.0
    #: BETA.10 U03: how far short of the watchdog above a lead may sit before it
    #: is reported -- the ceiling is ``_ROLLOVER_DEFER_SWITCH_TIMEOUT_SECONDS``
    #: minus this (840s at the shipped default).
    _ROLLOVER_LEAD_CEILING_HEADROOM_SECONDS = 60.0
    # Hostile-review B2 fix: if a dispatched rollover reload has not landed
    # (no fresh current_proof_event_id) within this long, treat it as dropped
    # and retry once more before the plan's projected end, rather than waiting
    # on a proof-event change a failed/lost reload will never produce. Shorter
    # than _ROLLOVER_MIN_LEAD_SECONDS so a retry still has lead time to land.
    _ROLLOVER_ISSUED_TIMEOUT_SECONDS = 45.0
    # Item 84 fix: once a dispatched rollover is confirmed undelivered (past
    # _ROLLOVER_ISSUED_TIMEOUT_SECONDS above), the retry itself can stay GATED
    # for many ~2s poll ticks in a row (the retry-min-interval floor or the
    # worker-pid-age guard, both below) before it actually dispatches. Logging
    # a fresh WARNING every gated tick spammed the log 1:1 with "deferred:
    # worker pid has only been alive Ns" (measured: 150 WARNINGs per 300s).
    # This shared interval bounds how often BOTH gated-tick WARNINGs repeat
    # for the SAME still-gated channel -- the "did not land; retrying" one
    # (``_rollover_retry_warned_at``) and the "worker pid has only been alive
    # Ns" one (``_rollover_pid_age_warned_at``), each with its own
    # bookkeeping since they gate on different conditions; every other gated
    # tick logs at DEBUG instead. See ``_check_plan_rollover``'s
    # ``retrying_undelivered`` branch.
    _ROLLOVER_RETRY_WARN_INTERVAL_SECONDS = 60.0
    # Hostile-review B2 fix: mirrors _RELOAD_RETRY_COOLDOWN_SECONDS -- how long
    # to back off re-querying the schedule after the provider raises
    # SourcePrepareError or returns no/empty plan for a rollover check.
    _ROLLOVER_RETRY_COOLDOWN_SECONDS = 30.0
    # U63 defect A: the boundary answer can be a legal but degenerate plan -- a
    # short TAIL of the closing scheduled item, or nothing at all (C14,
    # 00:28:38, is the second case: the derived boundary instant is excluded by
    # the schedule's half-open item test).
    # These two constants mirror the daemon's own reload-time rule
    # (``EgressDaemon._resolve_schedule_tail``, with its own
    # ``_SCHEDULE_TAIL_FLOOR_SECONDS`` / ``_SCHEDULE_TAIL_BOUNDARY_MARGIN_S``
    # in daemon.py, which remain the source of truth for the policy): a tail
    # below the floor is re-resolved where it ENDS, and the margin keeps that
    # second look inside the NEXT item rather than on another boundary instant.
    # They are restated here rather than imported from daemon.py: the automation
    # owns its own rollover policy, and a cross-module import of another
    # process's private policy constant would silently retune this one the next
    # time the daemon's rule changes.
    _ROLLOVER_TAIL_FLOOR_SECONDS = 30.0
    _ROLLOVER_TAIL_BOUNDARY_MARGIN_SECONDS = 1.0
    # D43 hardening, D45 fix (2026-09-05): a floor under the per-channel
    # rollover CADENCE. The boundary-aligned trigger point alone is not
    # enough -- it is derived from the plan, so a plan that comes back short
    # for any reason can still drive a fast cycle. Each dispatch costs a
    # synchronous SourcePreparer.prepare on the automation thread
    # (daemon._try_content_reload), so no channel may dispatch rollovers
    # closer together than this. The B2 "the reload never landed" retry is
    # deliberately exempt -- that is recovery, not cadence.
    #
    # D45: this fixed 300s value is no longer used directly as the floor --
    # it is only the CEILING _rollover_min_interval_seconds clamps to. A flat
    # 300s floor is longer than the lifetime of a short plan (D45's own
    # trigger: PLAN_MIN_SECONDS reverted to 0.0 in source_plan.py means an
    # 8-segment, 30-second-item plan is only 240s long). MEASURED: against
    # that 240s plan a flat 300s floor produces a dispatch every 300s with
    # the boundary-aligned lead shrinking each cycle (120s, then 60s, then
    # 0s, then negative), so by the third rollover the trigger arrives at or
    # after the plan's real end and the engine reaches EOS and restarts --
    # exactly the failure this cadence floor exists to prevent. See
    # _rollover_min_interval_seconds.
    _ROLLOVER_MIN_INTERVAL_SECONDS = 300.0
    #: Hostile-review fix (2026-09-05): a trivial epsilon floor ONLY -- large
    #: enough to avoid a literal zero/near-zero interval for a degenerate
    #: plan, small enough to never bind for any plan worth measuring. An
    #: earlier version of this fix used 30.0 here, which is a real floor for
    #: a longer plan but is LONGER than half the lifetime of an 8x3s (24s)
    #: plan -- measured (with ``_rollover_min_lead_seconds``, NEW-3, also
    #: scaled): dispatches still happen every 30s (the floor), but the
    #: boundary-aligned lead shrinks each cycle (12s, 6s, 0s, then negative
    #: from the fourth rollover on) exactly like the flat-300s bug this
    #: whole mechanism exists to fix, just faster. ``_rollover_min_interval_
    #: seconds`` must never return more than half the plan's own planned
    #: duration; this constant cannot violate that because it is far smaller
    #: than any real plan.
    _ROLLOVER_MIN_INTERVAL_FLOOR_SECONDS = 1.0
    # Item 78 fix 2: the B2 "reload never landed" retry path used to be fully
    # exempt from any dispatch-cadence floor at all (deliberately -- it is
    # recovery, not steady-state cadence), which meant a worker that keeps
    # crashing/relaunching right after every reload got an UNTHROTTLED re-arm
    # roughly one second after every relaunch (the 45s ISSUED_TIMEOUT fires
    # again almost immediately against a worker that never gets a chance to
    # settle).
    #
    # Coordinator review, round 2: an earlier version of this fix measured
    # the floor from ``_rollover_dispatched_at`` -- the timestamp of the
    # ORIGINAL dispatch -- which is dead code: the retry branch is only ever
    # entered after ``_ROLLOVER_ISSUED_TIMEOUT_SECONDS`` (45s) has already
    # elapsed since that same original dispatch, so a floor <= 45s measured
    # from that origin can never bind (the gap is always >= 45s already).
    # This is now measured from ``_rollover_retry_dispatched_at`` instead --
    # the timestamp of the last RETRY specifically -- which is what actually
    # limits a channel stuck retrying repeatedly to one retry per this many
    # seconds, independent of (and a real storm limiter alongside)
    # ``_ROLLOVER_RETRY_MIN_WORKER_AGE_SECONDS`` below.
    _ROLLOVER_RETRY_MIN_INTERVAL_SECONDS = 60.0
    # Item 78 fix 2: never dispatch a rollover retry to a worker whose current
    # pid has been alive less than this long -- a pid this young is still
    # settling from its own relaunch, and throwing another synchronous
    # SourcePreparer.prepare at it immediately reproduces the same
    # unthrottled-re-arm churn the interval floor above exists to stop, just
    # gated on process age instead of wall-clock spacing.
    _ROLLOVER_RETRY_MIN_WORKER_AGE_SECONDS = 60.0

    @property
    def daemon(self) -> EgressDaemon:
        """RAT-004: the underlying daemon, exposed so the app lifespan's
        shutdown ``finally`` block can call ``stop_all_channels(...)`` as the
        graceful drain-all owner before halting this service's poll loop."""

        return self._daemon

    def run_forever(
        self,
        *,
        poll_seconds: float = 2.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Run the automation loop until stopped; survive scan errors."""

        enable = getattr(self._daemon, "enable_async_preparation", None)
        shutdown = getattr(self._daemon, "shutdown_preparation", None)
        if enable is not None:
            enable()
        # ONE startup reconciliation pass: a supervisor restart can leave
        # persisted ON_AIR rows whose encoder PIDs died with the old control
        # plane. Clearing those stale claims and re-queuing an ordinary start is
        # what stops the "frozen HLS, stale ON_AIR, zero commands" state
        # (2026-09-24). Best-effort: a reconciliation hiccup must never stop the
        # automation loop from running.
        reconcile = getattr(self._daemon, "reconcile_stale_state", None)
        if reconcile is not None:
            try:
                recovered = reconcile()
                if recovered:
                    _LOG.warning(
                        "Channel automation startup reconciliation cleared stale "
                        "on-air state and queued recovery starts for: %s",
                        ", ".join(recovered),
                    )
            except Exception:
                _LOG.exception(
                    "Startup stale-state reconciliation failed; automation continues "
                    "(channels will be supervised on the normal poll path)."
                )
        # BETA.10 U04: the stuck-pass watchdog rides THIS loop's lifetime. It
        # is started here, after the one-shot startup work above, so a failure
        # there cannot leave a watcher thread running with no loop to watch;
        # and it is stopped in the ``finally`` below, before the (possibly
        # slow) drain, so ``run_forever`` returning always means the watcher
        # is gone. Disabled (threshold 0) -> ``start`` spawns nothing.
        self._pass_watchdog.start()
        try:
            while stop_event is None or not stop_event.is_set():
                try:
                    self.run_once()
                except Exception:
                    _LOG.exception(
                        "Channel automation scan failed; retrying on the next poll interval."
                    )
                if stop_event is not None:
                    stop_event.wait(poll_seconds)
                else:
                    time.sleep(poll_seconds)
        finally:
            self._pass_watchdog.stop()
            if shutdown is not None:
                shutdown()

    def run_once(self, *, now: datetime | None = None) -> list[str]:
        """One pass over every enabled channel; returns the channel ids seen."""

        self._drain_as_run_outbox()
        # BETA.10 U06: the startup sweep runs ONCE, so a persisted on-air row
        # whose encoder was still alive at that instant and died afterwards was
        # never reconciled -- `_poll_process` returns immediately for a channel
        # with no tracked process, and a channel that is not auto_start has no
        # other supervisor, so the frozen-HLS/stale-ON_AIR state simply came
        # back. Sweep again on every pass, before the per-channel work, so a
        # recovery start queued here is drained by that channel's own
        # `process_once` below.
        self._reconcile_dead_encoders_this_pass()
        seen: list[str] = []
        for config in self._store.list_configs():
            channel_id = config.channel_id
            if not config.enabled:
                # Disabled channels stay out of automation, but an explicit
                # operator stop/drain must still reach the daemon. Do not run
                # normal polls here: they can service crash-relaunch state.
                pending_terminal_command = any(
                    command.action in {"stop", "drain"}
                    for command in self._store.peek_pending_commands(channel_id)
                )
                state = self._store.read_state(channel_id)
                if pending_terminal_command:
                    self._daemon.process_commands_only(channel_id)
                elif state is not None and state.state == "DRAINING":
                    # A graceful drain remains supervised until its worker exits,
                    # but disabled channels never enter schedule/autostart logic.
                    self._daemon.process_once(channel_id)
                continue
            seen.append(channel_id)
            if self._alerts is not None:
                self._alerts.begin_tick(channel_id)
            # Audit Critical (TEST-001): one channel's failure - a poisoned
            # relay config, a supervisor crash - must never starve the other
            # channels' supervision. Isolate per channel; the loop's outer
            # handler in run_forever still covers scan-level failures.
            #
            # Item 78 fix 1: ``now`` is passed through UNRESOLVED here (not
            # pre-computed once for the whole pass, and not even resolved
            # once per channel before ``_run_channel_pass`` runs) -- see that
            # method's own comment for where wall-clock time actually gets
            # read and why.
            try:
                self._run_channel_pass(config, channel_id, now)
            except Exception as exc:
                _LOG.exception(
                    "Channel automation pass failed for %s; other channels "
                    "continue. Fix this channel's configuration or inspect "
                    "the error above.",
                    channel_id,
                )
                # DEFECT C: this used to be visible ONLY as the log line above.
                if self._alerts is not None:
                    self._alerts.on_pass_failure(
                        channel_id, detail=f"{exc.__class__.__name__}: {exc}"
                    )
                continue
            if self._alerts is not None:
                self._alerts.end_tick(channel_id)
        return seen

    def _drain_as_run_outbox(self) -> None:
        """BUG C2 fix: retry a backlog left by a prior DB outage.

        Routes through ``ensure_started()`` rather than ``drain_once()``
        directly: this is the first of the two call sites
        (``AsRunOutbox.ensure_started``'s docstring names the other --
        the recorder's first opportunistic write) that can perform the
        one-time startup replay, deferred out of ``build_channel_automation``
        so create_app() never touches the database. In steady state (no
        backlog, replay already done) this is a cheap no-op read. Neither
        ``ensure_started`` nor ``drain_once`` ever raises for a store
        failure (both log + alert internally); this try/except is
        defense-in-depth against a genuinely unexpected bug, matching every
        other per-tick guard in this loop.
        """
        if self._as_run_outbox is None:
            return
        try:
            self._as_run_outbox.ensure_started()
        except Exception:
            _LOG.exception("As-run outbox drain tick failed unexpectedly; retrying next poll.")

    def _reconcile_dead_encoders_this_pass(self) -> None:
        """BETA.10 U06: per-pass sweep for persisted claims whose encoder died.

        The startup sweep (``reconcile_stale_state``, called once from
        ``run_forever``) cannot see a row whose encoder was still alive at that
        instant and died afterwards, and nothing else supervises a channel that
        is not ``auto_start``: the daemon's ``_poll_process`` returns immediately
        for a channel with no *tracked* process. Same best-effort contract as the
        startup call -- a sweep hiccup must never stop the pass -- and the same
        ``getattr`` guard, so a daemon double in a test that does not implement
        the sweep is simply skipped.
        """

        reconcile = getattr(self._daemon, "reconcile_dead_encoders", None)
        if reconcile is None:
            return
        try:
            recovered = reconcile()
            if recovered:
                _LOG.warning(
                    "Channel automation pass reconciliation cleared stale on-air "
                    "state and queued recovery starts for: %s",
                    ", ".join(recovered),
                )
        except Exception:
            _LOG.exception(
                "Per-pass stale-state reconciliation failed; this pass continues "
                "(the channel is still supervised on the normal poll path)."
            )

    def _run_channel_pass(self, config: Any, channel_id: str, now: datetime | None) -> None:
        """BETA.10 U04: the pass's own watchdog boundary.

        Records the pass (channel, start monotonic, automation thread ident)
        before dispatching to ``_run_channel_pass_body`` and clears it in a
        ``finally``, so the record survives every exit path -- including an
        exception, which ``run_once``'s per-channel guard then charges to this
        channel and not to the watchdog. With no watcher thread running (every
        direct ``run_once`` in this suite) both calls are state updates that
        log nothing and start nothing.
        """

        self._pass_watchdog.pass_started(channel_id)
        try:
            self._run_channel_pass_body(config, channel_id, now)
        finally:
            self._pass_watchdog.pass_finished(channel_id)

    def _run_channel_pass_body(self, config: Any, channel_id: str, now: datetime | None) -> None:
        if self._daemon.has_live_process(channel_id):
            self._start_retry_at.pop(channel_id, None)
        elif (
            config.auto_start
            and not self._daemon_has_operator_stop_intent(channel_id)
            and not any(
                command.action in {"stop", "drain"}
                for command in self._store.peek_pending_commands(channel_id)
            )
        ):
            retry_at = self._start_retry_at.get(channel_id)
            if retry_at is None or self._monotonic() >= retry_at:
                # Do not double-enqueue against a start already queued this
                # cycle: startup reconciliation (``reconcile_stale_state``)
                # queues a recovery start BEFORE the first pass, so enqueuing
                # an auto_start here too would give ONE intent TWO starts.
                pending_starts = [
                    cmd
                    for cmd in self._store.peek_pending_commands(channel_id)
                    if cmd.action == "start"
                ]
                if pending_starts:
                    # A start is already waiting to be drained this pass by
                    # ``process_once`` below; let it do its job. Arm the retry
                    # cooldown so a later pass (after that start is consumed
                    # and if the channel is still dark) resumes the normal
                    # auto_start supervision instead of looping.
                    self._start_retry_at[channel_id] = (
                        self._monotonic() + self._START_RETRY_COOLDOWN_SECONDS
                    )
                else:
                    self._enqueue(channel_id, "start", now=now or datetime.now(UTC))
                    self._start_retry_at[channel_id] = (
                        self._monotonic() + self._START_RETRY_COOLDOWN_SECONDS
                    )
                    _LOG.info(
                        "Channel automation issued start for dark auto_start "
                        "channel %s (retries every %.0fs until live).",
                        channel_id,
                        self._START_RETRY_COOLDOWN_SECONDS,
                    )
        self._daemon.process_once(channel_id)
        # Item 78 fix 1 (round 2, coordinator review): read wall-clock "now"
        # AFTER ``process_once`` returns, immediately before the checks that
        # use it for rollover math -- NOT before ``process_once`` is called.
        # ``process_once`` is exactly where a channel's own synchronous
        # ``_start`` (``SourcePreparer.prepare``, daemon.py) can block for
        # minutes. Round 1 of this fix moved the read from once-per-pass to
        # once-per-channel, but still read it at the TOP of this method --
        # before ``process_once`` -- so the channel that actually blocks
        # still computed its own ``_check_plan_rollover`` against a
        # pre-block timestamp (measured: plan_end_at -315s behind the
        # post-block clock, the exact frozen-horizon bug this fix exists to
        # close). When ``now`` is given explicitly (every test in this
        # suite) this is a no-op -- the same value applies before and after,
        # exactly as before.
        post_process_now = now or datetime.now(UTC)
        self._check_slate_replan(channel_id, now=post_process_now)
        self._check_plan_rollover(channel_id, now=post_process_now)
        self._sync_ndi_relay(config)
        self._sync_sdi_relay(config)

    def _sync_ndi_relay(self, config: Any) -> None:
        from civiccast.egress.ndi_relay import (
            NdiRelayStatus,
            drop_relay_status,
            set_relay_status,
        )

        channel_id = config.channel_id
        if not config.ndi_relay_name:
            existing = self._ndi_relays.pop(channel_id, None)
            if existing is not None:
                existing.stop()
                drop_relay_status(channel_id)
            return
        source_uri = _udp_ts_source_uri(config)
        if source_uri is None:
            set_relay_status(
                NdiRelayStatus(
                    channel_id=channel_id,
                    ndi_name=config.ndi_relay_name,
                    state="blocked",
                    next_step=(
                        "NDI relay needs the channel's UDP transport-stream "
                        "output. Apply a headend delivery preset (or add a "
                        "udp-ts/local-ts udp:// sink) first."
                    ),
                )
            )
            return
        relay = self._ndi_relays.get(channel_id)
        if relay is not None and (
            relay.ndi_name != config.ndi_relay_name or relay.source_uri != source_uri
        ):
            relay.stop()
            relay = None
        if relay is None:
            relay = self._ndi_supervisor_factory(
                channel_id=channel_id,
                ndi_name=config.ndi_relay_name,
                source_uri=source_uri,
            )
            self._ndi_relays[channel_id] = relay
        set_relay_status(relay.ensure_running())

    def _sync_sdi_relay(self, config: Any) -> None:
        from civiccast.egress.sdi_relay import (
            SdiRelayStatus,
            drop_relay_status,
            set_relay_status,
        )

        channel_id = config.channel_id
        if not config.sdi_relay_device:
            existing = self._sdi_relays.pop(channel_id, None)
            if existing is not None:
                existing.stop()
                drop_relay_status(channel_id)
            return
        source_uri = _udp_ts_source_uri(config)
        if source_uri is None:
            set_relay_status(
                SdiRelayStatus(
                    channel_id=channel_id,
                    device=config.sdi_relay_device,
                    state="blocked",
                    next_step=(
                        "SDI relay needs the channel's UDP transport-stream "
                        "output. Apply a headend delivery preset (or add a "
                        "udp-ts/local-ts udp:// sink) first."
                    ),
                )
            )
            return
        relay = self._sdi_relays.get(channel_id)
        if relay is not None and (
            relay.device != config.sdi_relay_device or relay.source_uri != source_uri
        ):
            relay.stop()
            relay = None
        if relay is None:
            relay = self._sdi_supervisor_factory(
                channel_id=channel_id,
                device=config.sdi_relay_device,
                source_uri=source_uri,
            )
            self._sdi_relays[channel_id] = relay
        set_relay_status(relay.ensure_running())

    def _check_slate_replan(self, channel_id: str, *, now: datetime) -> None:
        state_row = self._store.read_state(channel_id)
        if state_row is None or state_row.state != "FALLBACK_SLATE":
            self._reload_issued.discard(channel_id)
            # A program actually made it on air: the last reload SUCCEEDED,
            # so the failure-pacing cooldown resets and the next gap reloads
            # immediately. The TRANSITIONING leg of a failing flap does NOT
            # clear it - that is the churn the cooldown exists to stop.
            if state_row is not None and state_row.state == "ON_AIR":
                self._replan_retry_at.pop(channel_id, None)
            return
        if not self._daemon_worker_initial_control_connection_observed(channel_id):
            # A fallback worker started because the original scheduled plan
            # expired during preparation. Wait for that new worker's initial
            # pipe connection before asking it to reload back to the schedule.
            # Do not consume the latch/cooldown or call the provider yet.
            return
        if self._daemon_has_pending_preparation(channel_id):
            # U47: the daemon is preparing this channel's program for the
            # slate that is on air right now (a slate-first hand-off). The
            # reload this pass would queue is the very hand-off it is already
            # running: it reaches ``_request_reload`` -> ``_cancel_preparation``,
            # abandons the in-flight preparation and conforms the program a
            # second time. Like the control-connection gate above, this does NOT
            # consume the latch or the cooldown -- if the hand-off's preparation
            # FAILS, the channel stays FALLBACK_SLATE with no preparation
            # registered and the next tick issues the reload exactly as before.
            return
        chained_program_end = self._daemon_chained_slate_program_end(channel_id)
        if chained_program_end is not None and now < chained_program_end:
            # U53 item 1: this row does not mean "the channel is on the slate
            # with its programme still due". The plan on air is a chained
            # filler: the slate covers only the gap up to the programme's own
            # due instant, and that programme follows it in the same plan. So
            # the reload this pass would queue is for a programme that is
            # ALREADY on air (or about to be, with nothing left to prepare),
            # and issuing it would cut the chained leg off at the reload
            # boundary and conform the same programme a second time.
            #
            # Same contract as the two gates above: waiting consumes neither
            # the latch nor the cooldown. The record is cleared by the next
            # dispatch that is not a chain (``_record_dispatched_plan`` writes
            # it set-or-clear) and validated against the proof event of the
            # plan it describes, so a chain that never actually landed cannot
            # suppress anything; and once ``now`` reaches the chained
            # programme's own projected end this gate stops applying on its
            # own, so a channel that really is stuck on the slate after it is
            # replanned exactly as before.
            return
        if channel_id in self._reload_issued:
            return
        # Audit ENG-002: when the due item persistently fails PREPARATION,
        # the channel flaps TRANSITIONING->FALLBACK_SLATE (clearing the
        # one-shot latch above) and a naive re-issue strobes kill/restart
        # every ~2 ticks for the item's whole duration. Reloads get the
        # same cooldown pacing as #152 gave starts: 30s of honest slate
        # between retries.
        retry_at = self._replan_retry_at.get(channel_id)
        if retry_at is not None and self._monotonic() < retry_at:
            return
        try:
            plan = self._source_plan_provider(channel_id)
        except SourcePrepareError:
            # The due item exists but is not playable yet; stay on slate and
            # try again next poll — the daemon would land back on slate anyway.
            return
        if plan is None:
            self._warm_after_slate(channel_id, now=now)
            return
        self._enqueue(channel_id, "reload", now=now)
        self._reload_issued.add(channel_id)
        self._replan_retry_at[channel_id] = self._monotonic() + self._RELOAD_RETRY_COOLDOWN_SECONDS
        _LOG.info(
            "Channel automation issued reload for %s: a scheduled program is due.",
            channel_id,
        )

    def _warm_after_slate(self, channel_id: str, *, now: datetime) -> None:
        """Prepare the next published program during a slate gap, without taking air.

        The rollover warm only runs after dispatching a program. A station
        starting on slate otherwise pays its first cold conform at the due
        instant, losing part of the slot. Reuse that warm queue before the
        first dispatch, including its short-plan successor look-ahead.
        """
        next_start = getattr(self._source_plan_provider, "next_item_start_at", None)
        warmer = getattr(self._daemon, "warm_source_plan", None)
        if not callable(next_start) or not callable(warmer):
            return
        if self._boundary_source_plan_provider is None:
            return
        tick = self._monotonic()
        if tick < self._slate_warm_retry_at.get(channel_id, 0.0):
            return
        self._slate_warm_retry_at[channel_id] = tick + self._RELOAD_RETRY_COOLDOWN_SECONDS
        try:
            start = next_start(channel_id, now)
            lead_seconds = self._rollover_lead_seconds()
            if start is None or not now < start <= now + timedelta(seconds=lead_seconds):
                return
            plan = self._resolve_boundary_plan_at(channel_id, start)
            if plan is None or not plan.segments:
                return
            warmer(channel_id, plan)
            duration = sum(segment.duration_seconds for segment in plan.segments)
            if duration > 0:
                self._warm_upcoming_plan(
                    channel_id,
                    now=now,
                    lead_seconds=lead_seconds,
                    fresh_start=start,
                    fresh_end=start + timedelta(seconds=duration),
                )
        except Exception:
            _LOG.debug(
                "Channel automation slate look-ahead for %s did not complete; "
                "the due-program reload is unaffected.",
                channel_id,
                exc_info=True,
            )

    def _rollover_lead_seconds(self, asset_seconds: float | None = None) -> float:
        """BETA.10 U02: how far ahead of the live plan's projected end the
        GStreamer path's rollover must be issued for a preparation that runs
        to its configured bound to still settle before EOS.

        The GStreamer path is the one that pre-conforms its source (the engine
        reads only ``segment.path``), and ``_try_content_reload`` drives that
        conform through ``SourcePreparer``, whose every ffmpeg call is bounded
        by ``CIVICAST_EGRESS_PREPARATION_TIMEOUT_SECONDS``
        (``preparer.preparation_timeout_seconds_from_env``; 300s at the
        station and by default). The lead therefore has to be at least that
        bound -- measured 2026-09-24: a rollover issued 120s before EOS with a
        cold conform still in flight left the channel with nothing armed at
        EOS, and it went dark.

        ``max`` against ``_ROLLOVER_MIN_LEAD_SECONDS`` keeps the historic 120s
        floor for a deployment that has shortened the preparation timeout
        below it. ``ROLLOVER_LEAD_ENV`` overrides the whole computation for an
        operator who has measured their own preparation and settle times.

        BETA.10 U03: the timeout is multiplied by
        ``_ROLLOVER_PREPARATION_PASSES``. In the worktree this was sized in, a
        normalized segment's preparation runs a loudnorm MEASUREMENT pass and
        then the conform itself, each bounded by that timeout, so the U02
        single-timeout lead (390s at the station's 300s) covered only half of
        THAT build's worst case. U05: the installed base this candidate patches
        makes ONE bounded call per segment -- its loudness probe goes through
        ``check_streaming_loudness``, not ``_run_ffmpeg`` -- so the multiplier
        is a deliberate over-estimate here rather than an under-estimate. At
        the shipped 300s the lead is 2*300 + 30 + 60 = 690s either way; see
        ``_ROLLOVER_PREPARATION_PASSES``.

        U53 item 3 (2026-09-26): ``asset_seconds``, when the caller knows it,
        raises that floor to the measured cost of conforming the item that
        must actually be prepared --
        ``asset_seconds / _ROLLOVER_CONFORM_REALTIME_FACTOR +
        _ROLLOVER_CONFORM_MARGIN_SECONDS``. The timeout-derived value is a
        FLOOR, not a replacement: the two answer different questions ("what
        may one ffmpeg pass take" vs "what will this asset cost"), so the
        larger wins and an item cheaper than the timeout bound keeps exactly
        today's lead. ``None`` (no plan resolved, or the caller is not on the
        boundary-provider path) keeps the pre-U53 computation byte for byte.

        This is deliberately a LEAD, not a cadence: the dispatch it permits is
        still gated by ``_rollover_min_interval_seconds``, the issued latch,
        and ``has_pending_reload_settlement``, and it is still clamped by
        ``last_segment_start_at`` at the call site (never triggers before the
        last segment of the live plan begins).

        A lead longer than the live plan itself therefore arms at that last
        segment's START, not before it -- see the call site's ``max``. The
        ``_rollover_min_interval_seconds`` cadence floor still bounds how often
        that can happen for a channel."""

        override = rollover_lead_seconds_from_env()
        if override is not None:
            self._warn_if_rollover_lead_nears_the_defer_watchdog(
                override, source=f"{ROLLOVER_LEAD_ENV} override"
            )
            return override
        computed = max(
            self._ROLLOVER_MIN_LEAD_SECONDS,
            self._ROLLOVER_PREPARATION_PASSES * preparation_timeout_seconds_from_env()
            + self._ROLLOVER_RELOAD_SETTLE_BUDGET_SECONDS
            + self._ROLLOVER_LEAD_MARGIN_SECONDS,
        )
        if asset_seconds is not None and asset_seconds > 0.0:
            computed = max(
                computed,
                asset_seconds / self._ROLLOVER_CONFORM_REALTIME_FACTOR
                + self._ROLLOVER_CONFORM_MARGIN_SECONDS,
            )
        self._warn_if_rollover_lead_nears_the_defer_watchdog(computed, source="computed default")
        return computed

    def _cached_rollover_asset(
        self, channel_id: str, plan_end_at: datetime
    ) -> tuple[float | None, str | None] | None:
        """The duration this boundary ALREADY resolved, without resolving it again.

        U53b item 8: ``_probe_rollover_asset`` caches per boundary -- including its
        failures -- so its answer is available to any later tick of the same plan,
        notably the dispatch tick, which by construction never enters the probe
        window and must not pay for a provider call the dispatch body is about to
        make anyway. ``None`` (no answer) means this boundary has not been resolved
        at all; ``(None, None)`` means it WAS resolved and resolved to nothing.
        """

        cached = self._rollover_asset_probe.get(channel_id)
        if cached is not None and cached[0] == plan_end_at:
            return cached[1], cached[2]
        return None

    def _probe_rollover_asset(
        self, channel_id: str, plan_end_at: datetime
    ) -> tuple[float | None, str | None]:
        """U53 item 3: the duration and label of the item the boundary at
        ``plan_end_at`` resolves to, resolved ONCE per boundary.

        An adaptive lead can only ever move the dispatch EARLIER, so its input
        has to be known before the trigger that input itself computes -- a
        probe taken when the flat lead fires is a probe taken too late to move
        anything. This therefore runs on the first poll tick after the horizon
        is established, which costs at most one extra boundary resolution per
        plan per channel (the dispatch resolves its own plan again at dispatch
        time, so a schedule edited in between is still picked up); the
        resolver behind it is memoized on ``(path, mtime_ns, size)``, so the
        repeat is a schedule lookup, not another probe of the media.

        Fails open, and caches the failure so a broken provider is still
        attempted only once per boundary: an unresolvable or empty target
        leaves the timeout-derived floor standing, which is exactly the
        pre-U53 behaviour. The dispatch site keeps its own error handling and
        retry cooldown untouched.
        Callers outside this window read the same cache through
        ``_cached_rollover_asset`` rather than calling this.
        """

        cached_asset = self._cached_rollover_asset(channel_id, plan_end_at)
        if cached_asset is not None:
            return cached_asset
        assert self._boundary_source_plan_provider is not None  # caller-gated
        try:
            plan = self._boundary_source_plan_provider(channel_id, plan_end_at)
        except SourcePrepareError:
            self._rollover_asset_probe[channel_id] = (plan_end_at, None, None)
            return None, None
        if plan is None or not plan.segments:
            self._rollover_asset_probe[channel_id] = (plan_end_at, None, None)
            return None, None
        seconds = sum(segment.duration_seconds for segment in plan.segments)
        label = plan.segments[0].label
        self._rollover_asset_probe[channel_id] = (plan_end_at, seconds, label)
        return seconds, label

    def _warn_if_rollover_lead_nears_the_defer_watchdog(
        self, lead_seconds: float, *, source: str
    ) -> None:
        """BETA.10 U03: report ONCE per service lifetime when the rollover lead
        reaches within ``_ROLLOVER_LEAD_CEILING_HEADROOM_SECONDS`` of the
        engine's defer-switch watchdog (``_ROLLOVER_DEFER_SWITCH_TIMEOUT_SECONDS``,
        840s of lead at the shipped 900s).

        Past that point the watchdog (``engine.py:2793``) can force the switch
        to whatever leg is armed before the outgoing plan reaches its own end
        -- the deferral the whole rollover is built around stops being honoured,
        so the lead stops buying what it was sized for. The value is still USED
        as given: this warns, it does not clamp. Silently shortening an
        operator's explicit ``ROLLOVER_LEAD_ENV`` would reinstate exactly the
        failure the lead exists to prevent, and clamping the computed default
        would make the lead lie about the worst-case preparation it is meant to
        cover.

        Once per service lifetime rather than once per tick: this is a property
        of the deployment's configuration, not of the plan on air, and the
        caller runs on every ~2s poll tick (the same reason
        ``_ROLLOVER_RETRY_WARN_INTERVAL_SECONDS`` exists). The latch is a plain
        per-instance flag -- the value it reports can change under a running
        service only by an env change, which no live deployment performs."""

        if self._rollover_lead_ceiling_warned:
            return
        ceiling = (
            self._ROLLOVER_DEFER_SWITCH_TIMEOUT_SECONDS
            - self._ROLLOVER_LEAD_CEILING_HEADROOM_SECONDS
        )
        if lead_seconds <= ceiling:
            return
        self._rollover_lead_ceiling_warned = True
        _LOG.warning(
            "Rollover lead is %.1fs (%s), past the %.1fs ceiling -- the "
            "%.0fs headroom below the engine's %.0fs defer-switch watchdog. A "
            "switch deferred longer than that watchdog is FORCED to the armed "
            "leg, so this lead may be armed long enough for the watchdog to "
            "cut before the outgoing plan reaches its own end. Using the "
            "configured value as-is (raise the watchdog to match, or lower the "
            "lead).",
            lead_seconds,
            source,
            ceiling,
            self._ROLLOVER_LEAD_CEILING_HEADROOM_SECONDS,
            self._ROLLOVER_DEFER_SWITCH_TIMEOUT_SECONDS,
        )

    def _rollover_min_lead_seconds(self, planned_seconds: float) -> float:
        """Hostile-review fix (NEW-3, 2026-09-05): the boundary-aligned
        rollover LEAD, sized to the plan actually on air instead of a fixed
        constant -- the same problem ``_rollover_min_interval_seconds``
        solves for the dispatch-cadence FLOOR, one layer up.

        ``_ROLLOVER_MIN_LEAD_SECONDS`` (120s) is fine as a floor for
        ``rollover_trigger_at``'s ``min_lead_seconds`` when the plan is long
        enough to have 120s to spare. For a very short plan it is not: a
        flat 120s lead against an 8x3s (24-second) plan pushes
        ``plan_end_at - 120`` deep into the plan's own past, so the
        boundary-aligned trigger -- ``min(last_segment_start_at,
        plan_end_at - lead)`` -- is dominated by that negative candidate
        rather than the plan's real last-segment start, and the resulting
        dispatch lands at or after the plan's actual end (a negative lead
        over its own life) even with ``_rollover_min_interval_seconds``
        already fixed. Scaling this the same way (half the plan's own
        duration, clamped at the historic 120s ceiling) keeps the trigger
        boundary-aligned to a REAL point within a short plan's life instead
        of one that has already passed -- measured, an 8x3s (24-second)
        plan's every rollover now lands with a lead of at least a quarter of
        the plan's own duration (see
        ``TestRolloverCadence.test_a_very_short_plan_still_rolls_over_before_its_own_end``).
        This ceiling (120s) is smaller than ``_rollover_min_interval_
        seconds``'s (300s), so for the schedule-derived plans this whole
        pass targets (``max_segments=8`` by default, well under a 600s
        life) the two formulas agree; a plan long enough to separate them
        (several minutes or more) has ample runway from either number."""

        return min(self._ROLLOVER_MIN_LEAD_SECONDS, 0.5 * planned_seconds)

    def _rollover_min_interval_seconds(self, planned_seconds: float) -> float:
        """D45 fix: the per-channel rollover-dispatch floor, sized to the plan
        actually on air instead of a fixed constant.

        D43 fixed this floor at a flat ``_ROLLOVER_MIN_INTERVAL_SECONDS``
        (300s). That is longer than the lifetime of a short plan -- with
        ``source_plan.PLAN_MIN_SECONDS`` back to 0.0 (D45; see its
        docstring), a schedule of 30-second items yields an 8-segment,
        240-second plan. MEASURED against that plan with the flat 300s
        floor: 6 dispatches in 30 minutes, the first at 120s, each 300s
        apart, with the boundary-aligned lead shrinking every cycle (120s,
        then 60s, then 0s, then negative) -- so by the third rollover the
        trigger arrives at or after the plan's real end and the engine
        reaches EOS and restarts before that rollover can land: exactly the
        failure this cadence floor exists to prevent (see
        ``tests/egress/test_automation.py``'s
        ``test_the_flat_floor_bug_the_scaled_floor_fixes`` for the exact
        numbers).

        Scaling the floor to HALF the plan's own planned duration (clamped
        at ``_ROLLOVER_MIN_INTERVAL_SECONDS``, 300s, for a long plan)
        guarantees this can never exceed the plan's own lifetime, so a
        rollover always has room to dispatch again well before the plan runs
        out, however short the schedule's slots are. Hostile-review fix: an
        earlier version additionally floored this at a flat 30s
        (``max(30.0, 0.5 * planned_seconds)``), which is itself LONGER than
        HALF an 8x3s (24-second) plan's own lifetime and reproduces the same
        shrinking-lead bug at a smaller scale -- MEASURED (with
        ``_rollover_min_lead_seconds``, NEW-3, also in place): dispatches
        still happen, ten of them in a 300s window (12s, 42s, 72s, ...), but
        the lead shrinks every cycle (12s, 6s, 0s, then negative from the
        fourth rollover on) exactly like the 300s-floor case above, not
        "one dispatch and then nothing." The floor is now only
        ``_ROLLOVER_MIN_INTERVAL_FLOOR_SECONDS`` (a trivial 1.0s epsilon
        against a literal zero/near-zero interval for a degenerate plan),
        so this never returns more than half of ``planned_seconds`` -- the
        ONLY invariant this method guarantees. A long-item schedule (a plan
        well over 600s) is unaffected -- the 300s ceiling still applies."""

        return min(
            self._ROLLOVER_MIN_INTERVAL_SECONDS,
            max(self._ROLLOVER_MIN_INTERVAL_FLOOR_SECONDS, 0.5 * planned_seconds),
        )

    def _resolve_rollover_tail(
        self,
        channel_id: str,
        boundary: datetime,
        fresh_plan: EgressSourcePlan | None,
    ) -> EgressSourcePlan | None:
        """Re-resolve a degenerate boundary answer where its tail ENDS.

        U63 defect A. ``_check_plan_rollover`` asks the schedule about
        ``plan_end_at``, a DERIVED instant (``starts_at + sum(durations)``,
        never measured), so the question lands on the closing item's own slot
        end -- which the resolver's half-open item test
        (``starts_at <= t < ends_at``) excludes. Live 2026-09-29 00:28:38,
        education: the answer came back with no segments at all, the caller read
        that as "nothing is scheduled", and the channel aired the fallback slate
        for ~5 minutes while a programme was due where that closing slot ended
        (the daemon measured the closing item's remaining tail at 23.5s when its
        own reload ran at 00:37:28.275, i.e. the slot ended 23.5s later). The
        daemon resolved the very same boundary to that programme
        (``EgressDaemon._resolve_schedule_tail``) -- this applies the daemon's
        rule at the automation's boundary instead, so the two processes agree
        about what is due.

        Returns ``fresh_plan`` untouched (identity) whenever the answer is at or
        above the floor, whenever the second look finds nothing to air or finds
        no improvement, and on ``SourcePrepareError`` -- so a real schedule gap
        still resolves to the fallback slate (filler stays correct when
        ``SCHEDULE_GAP_ABSORB_SECONDS`` owns the gap) and a programme that
        genuinely has more than the floor left is never skipped in favour of
        the one after it.

        Mirrors ``EgressDaemon._resolve_schedule_tail`` with ONE deliberate
        divergence: the daemon also bails on ``tail_seconds <= 0.0``, while an
        empty answer is precisely the case this rule exists for here. The
        daemon never sees it -- its ``source_plan`` is always a plan it already
        resolved for some instant, so a ``0.0`` tail there is only a defensive
        guard. The automation's answer comes back empty at C14's 00:28:38
        boundary because the derived instant it asks about is excluded by that
        half-open test, and an empty answer is also what a genuine gap
        produces: the only way to tell them apart is to look one margin
        past the boundary. That look is the same one the daemon makes at its
        own reload, and on C14 it found the programme waiting: the daemon
        resolved that boundary to a 23.5s tail of the closing item and then to
        the 1800.0s item due where that tail ends (00:37:28.275, control-plane
        log; 2026-09-29). Where the two processes look at the same boundary,
        they should give the same answer. The exchange is still
        strictly-improvement-only --
        ``replacement_seconds <= tail_seconds`` keeps the empty answer, so
        filler is never replaced by anything, and only a plan with real media
        in it can ever be adopted.
        """

        assert self._boundary_source_plan_provider is not None
        tail_seconds = (
            0.0
            if fresh_plan is None
            else sum(segment.duration_seconds for segment in fresh_plan.segments)
        )
        if tail_seconds >= self._ROLLOVER_TAIL_FLOOR_SECONDS:
            return fresh_plan
        re_ask_at = boundary + timedelta(
            seconds=tail_seconds + self._ROLLOVER_TAIL_BOUNDARY_MARGIN_SECONDS
        )
        try:
            replacement = self._boundary_source_plan_provider(channel_id, re_ask_at)
        except SourcePrepareError:
            return fresh_plan
        if replacement is None or replacement.channel_id != channel_id:
            return fresh_plan
        replacement_seconds = sum(segment.duration_seconds for segment in replacement.segments)
        if replacement_seconds <= tail_seconds:
            return fresh_plan
        _LOG.info(
            "Channel automation rollover for %s resolved to a %.1fs tail of the closing "
            "scheduled item; using the plan due where that tail ends instead (%.1fs).",
            channel_id,
            tail_seconds,
            replacement_seconds,
        )
        return cast(EgressSourcePlan, replacement)

    def _check_plan_rollover(self, channel_id: str, *, now: datetime) -> None:
        """Extend a finite program or filler plan before it EOSes.

        ``_check_slate_replan`` reacts to a due program while filler is airing;
        this check protects both ON_AIR and finite FALLBACK_SLATE horizons. A
        schedule of back-to-back published premieres builds a source plan
        capped at ``max_segments`` (``source_plan.py``); once the last
        segment finishes the GStreamer worker EOSes, the daemon writes
        STOPPED, and only THEN does auto_start bring the channel back --
        6-8 worker restarts (and on-air blips) in the 2h soak
        (evidence: Desktop/CIVICCAST-EVIDENCE/soak-120-4b30c99-20260904,
        T6-beat-*.json pid churn).

        This tracks the projected wall-clock end of the plan the daemon is
        currently airing, AND the projected start of its last segment (keyed
        off ``current_proof_event_id``, which changes at every fresh
        start/reload -- see ``daemon._write_state`` call sites) and, once the
        boundary-aligned trigger computed by
        ``reload_policy.rollover_trigger_at`` is reached, re-fetches the
        schedule. Building the plan again at (a later) ``now`` is itself the
        rollover: the provider always resumes at the currently-airing item
        with a join-in-progress offset and windows forward from there
        (``source_plan.build_source_plan_from_schedule``), so a later call
        naturally reaches further into the schedule than the original call
        did. If that fresh plan reaches further than the one already airing,
        it becomes the rollover target. If the provider only returns the
        shrinking terminal item, filler is bound to the command instead. The
        SAME ``reload`` command ``_check_slate_replan`` uses is
        enqueued -- the daemon's ``_request_reload`` dispatches it through
        ``_try_content_reload`` (the GStreamer seamless content-swap;
        ``gst/strategy.py``'s ``reload_content``), so the channel stays
        ON_AIR with no worker restart and no EOS.

        Hostile-review B1 fix: skipped entirely while an operator override
        (live takeover / forced fallback slate) is active for the channel --
        see ``EgressDaemon.has_manual_override`` / ``PlayoutSupervisor.
        has_manual_override``. Neither writes a state-row transition this
        method could otherwise key off, so without this check a live takeover
        gets fought with a mid-event reload back to the SCHEDULED plan (a
        forced SRT/NDI reconnect), and a forced slate's reload attempt lands
        in the daemon's ``_pending_reloads`` latch and silently drops the
        operator's slate once it resolves.

        Hostile-review B3 fix: the trigger point moved EARLIER and became
        boundary-aligned (``_ROLLOVER_MIN_LEAD_SECONDS`` / last-segment-start,
        see ``reload_policy.rollover_trigger_at``'s docstring) -- the previous
        fixed 30s lookahead left ``SourcePreparer``'s synchronous cold-conform
        (daemon.py's ``_try_content_reload``, which can take minutes) racing
        the pipeline's own EOS. Triggering early enough that the new leg is
        typically ready well before the boundary means an IMMEDIATE switch
        (the old default) would truncate the tail of the still-airing item --
        so the daemon requests ``switch_at_end_of_current=True`` for this
        reload whenever the channel is ON_AIR with no override active
        (``reload_policy.should_defer_switch``), and the engine defers the
        actual selector switch to the outgoing leg's own EOS
        (``GstPlayoutEngine.reload_program``). The airing item is never
        re-decoded or cut short.

        Hostile-review B2 fix: ``_rollover_retry_at`` backs off re-querying
        the schedule (mirrors ``_replan_retry_at``) after a
        ``SourcePrepareError`` or an empty/None plan, instead of hammering the
        provider every ~2s poll tick. ``_rollover_issued_at`` bounds how long
        a dispatched reload is trusted to be in flight: if
        ``current_proof_event_id`` hasn't changed within
        ``_ROLLOVER_ISSUED_TIMEOUT_SECONDS`` (the reload was dropped, failed,
        or the command was never consumed), the latch clears and one retry is
        issued before the plan's projected end, rather than silently waiting
        forever on a proof-event change that will never arrive.

        D43 hardening, D45 fix (2026-09-05): ``_rollover_min_interval_seconds``
        is a floor under the gap between two rollover dispatches for one
        channel, independent of the plan window, because each dispatch runs
        ``SourcePreparer.prepare`` synchronously on this thread via
        ``daemon._try_content_reload``. D43 originally fixed this floor at a
        flat 300s and grew the plan itself (``source_plan.PLAN_MIN_SECONDS``)
        so a short-slot schedule would not need frequent rollovers. Real-
        hardware soak evidence then measured what that plan growth cost: an
        1800-second-minimum plan built from 30-second schedule items is ~60
        segments, and the GStreamer bridge builds one decoder sub-chain PER
        segment in a single pipeline -- ~1200 threads and ~3.5 GB on one
        worker, tripping the 10s stall watchdog every ~30s. D45 reverts
        ``PLAN_MIN_SECONDS`` to 0.0 (the plan's segment COUNT alone bounds
        pipeline shape now) and instead derives this floor from the plan
        actually on air: short plan, short floor, so a rollover is always
        allowed to land comfortably inside the plan's own lifetime, instead
        of a flat constant that can be longer than that lifetime -- measured
        with a flat 300s floor against an 8x30s (240s) plan: the
        boundary-aligned lead shrinks every cycle (120s, then 60s, then 0s,
        then negative) until a rollover arrives at or after the plan's real
        end and the engine reaches EOS. See ``_rollover_min_interval_seconds``
        for the exact numbers and the smaller-scale version of this same bug
        (a flat 30s sub-floor against a 24-second plan) an earlier version
        of this fix still had.

        If the schedule has nothing beyond what is already loaded (the
        freshly-built plan does not reach any further), this deliberately
        does nothing: there is no more program content to roll onto, so the
        plan is left to reach its own natural end. When no later schedule
        item extends that horizon, the command is instead bound to a finite
        filler plan and switches at the same natural boundary.
        """

        state_row = self._store.read_state(channel_id)
        if state_row is None or state_row.state not in {"ON_AIR", "FALLBACK_SLATE"}:
            # Not airing a finite program or filler plan right now (dark,
            # starting, transitioning, draining) -- nothing to extend.
            self._plan_horizon.pop(channel_id, None)
            self._rollover_issued.discard(channel_id)
            self._rollover_issued_at.pop(channel_id, None)
            # The cadence floor governs EXTENSIONS of a live plan; a channel
            # that has left the air starts fresh (a restart or a slate replan
            # must never be throttled by an earlier rollover).
            self._rollover_dispatched_at.pop(channel_id, None)
            self._rollover_retry_dispatched_at.pop(channel_id, None)
            self._rollover_retry_warned_at.pop(channel_id, None)
            self._rollover_pid_age_warned_at.pop(channel_id, None)
            return
        if self._daemon_has_manual_override(channel_id):
            # B1 fix: an operator override is live -- never fight it. Leave any
            # tracked horizon as-is; it will be re-established (or correctly
            # found stale and discarded) once the override clears and this
            # channel's state/proof-event settle back to a normal rollover.
            return
        if state_row.state == "FALLBACK_SLATE" and channel_id in self._reload_issued:
            # _check_slate_replan already queued the newly-due schedule plan
            # during this pass. Do not also queue a horizon rollover.
            return
        proof_event_id = state_row.current_proof_event_id
        self._track_worker_pid(channel_id, state_row)
        tracked = self._plan_horizon.get(channel_id)
        if tracked is None or tracked[0] != proof_event_id:
            # A fresh plan just took air (initial start, a slate replan, or
            # this method's own rollover reload completing) -- (re)establish
            # its projected end + last-segment-start and wait; nothing to
            # roll over yet. Also means any in-flight rollover landed.
            self._rollover_issued.discard(channel_id)
            self._rollover_issued_at.pop(channel_id, None)
            # Coordinator review, round 3, item E: a resolved rollover
            # sequence (this one landed) must not suppress the FIRST retry
            # of whatever rollover comes next for the fresh plan just
            # established -- clear the retry-interval floor's own timestamp
            # here too, everywhere _rollover_issued itself is discarded.
            self._rollover_retry_dispatched_at.pop(channel_id, None)
            self._rollover_retry_warned_at.pop(channel_id, None)
            self._rollover_pid_age_warned_at.pop(channel_id, None)
            previous_end_at = tracked[1] if tracked is not None else None
            previous_planned_seconds = tracked[3] if tracked is not None else None
            self._reestablish_plan_horizon(
                channel_id,
                now=now,
                proof_event_id=proof_event_id,
                previous_end_at=previous_end_at,
                previous_planned_seconds=previous_planned_seconds,
            )
            return
        _, plan_end_at, last_segment_start_at, planned_seconds = tracked

        # A newly started Windows worker can report ON_AIR before its pipe-reader
        # thread has accepted the daemon's control connection. Do not consume any
        # rollover latch or query/prepare the incoming plan during that narrow
        # window: the next automation tick retries as soon as the pipe is ready.
        if not self._daemon_worker_initial_control_connection_observed(channel_id):
            return

        stale_horizon_recovery = False
        if plan_end_at <= now:
            # The tracked finite plan has ended by wall clock. A seamless
            # reload that is already settling may legitimately run a few
            # seconds past this estimate, so leave that transaction alone.
            # Otherwise resolve the item due now and preserve this original
            # boundary on the recovery command; the daemon uses the expired
            # timestamp to select an immediate cut after preroll instead of
            # waiting for another outgoing EOS.
            if self._daemon_has_pending_reload_settlement(channel_id):
                # Sandbox soak 39d852e (2026-09-09, government + education
                # channels): a boundary-aligned seamless rollover was ARMED
                # (``switch_at_end_of_current=True``) and the OUTGOING leg ran
                # 5-17s past its projected end -- the dispatched durations sum
                # underestimates real playout, and the daemon observes the
                # engine's boundary commit 4-13s after the fact -- so this
                # stale check fired on a perfectly healthy, still-settling
                # rollover BEFORE the "already issued / daemon still settling"
                # guard below could see it. Re-establishing here read the
                # daemon's dispatch record, which is still the OLD plan
                # (``_record_dispatched_plan`` runs at settlement, not at
                # dispatch), and dated it from ``now``: a tuple one old
                # plan-length in the future. When the settlement then landed,
                # the "fresh plan just took air" branch fed that poisoned
                # ``previous_end_at`` to the deferred-start rule in
                # ``_establish_horizon_from_dispatch`` and dated the INCOMING
                # plan's horizon a whole plan too late -- no rollover was ever
                # issued for it, the plan ran to EOS, and the channel took the
                # 20s planned restart on every rollover. A tuple that is a few
                # seconds behind a settling switch is not the frozen-horizon
                # lie item 78 guards against; leave it alone and wait for the
                # daemon, whose own settlement deadline
                # (``_PENDING_RELOAD_SETTLE_DEADLINE_S``) or worker-exit
                # discard bounds this wait and then lets the branch run.
                #
                # Hostile review, round 2 (finding 1): this deliberately does
                # NOT also require ``channel_id in self._rollover_issued``.
                # The invariant is "a settlement is in flight, so the daemon's
                # ``dispatched_plan_horizon`` still describes the OUTGOING
                # plan and must not be re-read" -- and that holds no matter
                # who issued the reload: this method's own rollover, a slate
                # replan (``_check_slate_replan`` / ``_reload_issued``), or an
                # operator-queued reload. ``reload_policy.should_defer_switch``
                # defers ANY ON_AIR reload with no override, so every one of
                # those has the same 5-17s overrun window, and the reviewer
                # reproduced the EOS run with the conjunct in place via an
                # operator reload. Every discard path bounds the wait (daemon
                # settle deadline, worker exit, applied-but-dead, restart/
                # stop), so waiting on the daemon alone is never unbounded.
                #
                # Finding 3: the early return used to be silent, and the old
                # "was stale" WARNING is how the soak bug was found in the
                # first place -- log the wait, rate-limited per channel with
                # the same ``_rollover_retry_warned_at`` timestamp /
                # ``_ROLLOVER_RETRY_WARN_INTERVAL_SECONDS`` cadence the
                # undelivered-retry branch below uses (one line per settling
                # rollover, not one per ~2s tick). The timestamp is cleared
                # with the rest of the per-sequence state when the settlement
                # lands (fresh-plan branch) or is discarded (stale branch).
                past_due_seconds = (now - plan_end_at).total_seconds()
                last_warned_at = self._rollover_retry_warned_at.get(channel_id)
                if last_warned_at is None or (
                    self._monotonic() - last_warned_at >= self._ROLLOVER_RETRY_WARN_INTERVAL_SECONDS
                ):
                    _LOG.info(
                        "Channel automation rollover horizon for %s is past due by %.0fs "
                        "but the seamless reload is still settling; waiting.",
                        channel_id,
                        past_due_seconds,
                    )
                    self._rollover_retry_warned_at[channel_id] = self._monotonic()
                else:
                    _LOG.debug(
                        "Channel automation rollover horizon for %s is past due by %.0fs; "
                        "seamless reload still settling (last logged %.0fs ago).",
                        channel_id,
                        past_due_seconds,
                        self._monotonic() - last_warned_at,
                    )
                return
            if channel_id not in self._rollover_issued:
                # A live worker whose finite plan is already past due needs the
                # current scheduled item now. Re-reading the daemon's unchanged
                # dispatch would only date the expired plan from this tick and
                # let it run to EOS. Keep the true outgoing boundary and use the
                # ordinary dispatch path below; the daemon sees that the recorded
                # boundary is past and performs an immediate seamless cut once the
                # incoming leg is ready.
                _LOG.warning(
                    "Channel automation rollover horizon for %s was stale (plan_end_at "
                    "%s <= now %s); dispatching an immediate rollover.",
                    channel_id,
                    plan_end_at.isoformat(),
                    now.isoformat(),
                )
                self._rollover_issued_at.pop(channel_id, None)
                self._rollover_retry_dispatched_at.pop(channel_id, None)
                self._rollover_retry_warned_at.pop(channel_id, None)
                self._rollover_pid_age_warned_at.pop(channel_id, None)
                stale_horizon_recovery = True

        retrying_undelivered = False
        if channel_id in self._rollover_issued:
            if self._daemon_has_pending_reload_settlement(channel_id):
                # F1 redesign follow-up: the daemon reports this specific
                # reload is armed and still settling (a deferred switch can
                # take ~120s-900s) -- never retry while that is true; the
                # daemon's own deadline resolves it one way or the other.
                return
            issued_at = self._rollover_issued_at.get(channel_id)
            if issued_at is not None and (
                self._monotonic() - issued_at >= self._ROLLOVER_ISSUED_TIMEOUT_SECONDS
            ):
                # B2 fix: the proof event never changed -- the dispatched
                # reload did not land. Fall through to retry once more before
                # the plan's projected end.
                #
                # Item 78 fix 2: the latch is deliberately left in place here
                # (NOT cleared) until the retry actually dispatches, below --
                # clearing it up front and then getting blocked by the retry
                # floor/worker-pid-age guard would make the VERY NEXT tick
                # see "not currently issued" and fall through to the ordinary
                # (unguarded-on-retry) dispatch path instead, defeating both
                # guards entirely. Leaving it set means a blocked tick simply
                # re-enters this same branch next time with a fresh
                # ``_monotonic()`` reading and a fresh pid-age reading.
                # Item 84 fix: this branch re-enters on EVERY ~2s poll tick for
                # as long as the channel stays gated below (the retry-min-
                # interval floor or the worker-pid-age guard can block the
                # actual dispatch for many ticks in a row) -- logging a fresh
                # WARNING every single tick spammed the log 1:1 with "deferred:
                # worker pid has only been alive Ns" (measured, soak evidence).
                # Log the WARNING once when the 45s threshold is first crossed
                # for this undelivered reload, then at most once more per
                # _ROLLOVER_RETRY_WARN_INTERVAL_SECONDS while it stays gated;
                # every other gated tick logs at DEBUG instead. The gating
                # logic itself (the two floor checks below) is unchanged --
                # only the log cadence changes here.
                last_warned_at = self._rollover_retry_warned_at.get(channel_id)
                if last_warned_at is None or (
                    self._monotonic() - last_warned_at >= self._ROLLOVER_RETRY_WARN_INTERVAL_SECONDS
                ):
                    _LOG.warning(
                        "Channel automation rollover reload for %s did not land within "
                        "%.0fs (current_proof_event_id unchanged); retrying.",
                        channel_id,
                        self._ROLLOVER_ISSUED_TIMEOUT_SECONDS,
                    )
                    self._rollover_retry_warned_at[channel_id] = self._monotonic()
                else:
                    _LOG.debug(
                        "Channel automation rollover reload for %s still has not "
                        "landed (last warned %.0fs ago; still gated).",
                        channel_id,
                        self._monotonic() - last_warned_at,
                    )
                retrying_undelivered = True
            else:
                return  # already dispatched for this plan boundary; wait for it to land

        trigger_at = rollover_trigger_at(
            plan_end_at=plan_end_at,
            last_segment_start_at=last_segment_start_at,
            min_lead_seconds=self._rollover_min_lead_seconds(planned_seconds),
        )
        # U53 item 3: the trigger the flat, timeout-derived lead would fire at
        # -- the moment this dispatch is due WITHOUT any knowledge of what the
        # boundary resolves to. It guards the adaptive probe below: while
        # ``now`` is short of it the dispatch is still in the future, so the
        # probe is the only thing this tick can do and the only window in which
        # a longer lead can still move anything; at or past it the dispatch is
        # already due and the probe would buy nothing (which is what keeps a
        # plan arming at its own start, and a stale-horizon recovery, from
        # probing at all).
        flat_trigger_at = max(
            last_segment_start_at,
            plan_end_at - timedelta(seconds=self._rollover_lead_seconds()),
        )
        inside_lead_recovery = False
        # U41: hoisted so the boundary record can carry the horizon this
        # dispatch measured. ``None`` when there is no boundary provider (the
        # non-GStreamer path), which is exactly the "no horizon recorded" case
        # the daemon treats as today's behavior.
        lead_seconds: float | None = None
        asset_seconds: float | None = None
        asset_label: str | None = None
        if self._boundary_source_plan_provider is not None:
            # One scheduled item per plan: give short items their whole life
            # for preparation (the ``last_segment_start_at`` clamp below), for
            # long programmes a lead that covers the WORST-CASE preparation
            # this deployment allows -- BETA.10 U02: a preparation bounded by
            # ``CIVICAST_EGRESS_PREPARATION_TIMEOUT_SECONDS`` must still be
            # able to finish and settle before the outgoing pipeline reaches
            # EOS, or the channel reaches EOS with nothing armed (2026-09-24
            # 13:28:25, public). See ``_rollover_lead_seconds``.
            #
            # U53 item 3 (2026-09-26): the lead is ADAPTIVE -- the item the
            # boundary resolves to may cost more to conform than the
            # preparation timeout allows (a 9020.8s asset costs a measured
            # 485.8-866s), and a lead that does not cover it dispatches too
            # late. The duration is resolved once per boundary, on this same
            # tick-level path, so that the lead it produces can move THIS
            # trigger earlier; see ``_probe_rollover_asset``.
            if now < flat_trigger_at:
                asset_seconds, asset_label = self._probe_rollover_asset(channel_id, plan_end_at)
            else:
                # U53b item 8: this tick is at or past the flat trigger, so it does
                # not probe -- but the boundary's own duration was resolved a few
                # ticks earlier, inside this plan's window, and cached per boundary.
                # Reading that answer is free, and it is the difference between the
                # lead line saying the floor stands for a 300s item and saying "no
                # asset duration available for the boundary". The second is what the
                # station logged for EVERY floor-fired dispatch after C2 (8/8), and
                # it reads as a boundary that could not be resolved when it was in
                # fact resolved fine -- the floor simply outran it.
                cached_asset = self._cached_rollover_asset(channel_id, plan_end_at)
                if cached_asset is not None:
                    asset_seconds, asset_label = cached_asset
            lead_seconds = self._rollover_lead_seconds(asset_seconds)
            trigger_at = max(
                last_segment_start_at,
                plan_end_at - timedelta(seconds=lead_seconds),
            )
            # BETA.10 U41 (live 2026-09-25, education 23:53:17 -> 23:54:41):
            # a plan whose whole life is SHORTER than the lead can never be
            # armed ahead of its own start, so the clamp above collapses the
            # trigger to ``last_segment_start_at`` -- the instant the previous
            # rollover settled. That dispatch is recovery (less than a lead of
            # runway remained the moment the plan took air), not cadence, and
            # the D43 floor below is measured from the PREVIOUS dispatch while
            # being sized to the plan now on air: it can hold the recovery
            # back for up to half the remaining plan. Exempt it exactly like
            # the B2 retry path, which is exempt for the same reason.
            inside_lead_recovery = plan_end_at - last_segment_start_at < timedelta(
                seconds=lead_seconds
            )
        if now < trigger_at:
            return  # not yet at the boundary-aligned trigger point

        if not retrying_undelivered and not stale_horizon_recovery and not inside_lead_recovery:
            # D43 cadence floor, D45 fix: never dispatch rollovers for one
            # channel faster than _rollover_min_interval_seconds(planned_seconds)
            # apart -- sized to the plan actually on air, not a fixed
            # constant that can outlast a short plan (see that method's
            # docstring). Checked BEFORE the provider call so a throttled
            # tick costs nothing at all.
            last_dispatch = self._rollover_dispatched_at.get(channel_id)
            if (
                last_dispatch is not None
                and self._monotonic() - last_dispatch
                < self._rollover_min_interval_seconds(planned_seconds)
            ):
                return
        elif retrying_undelivered:
            # Item 78 fix 2: the B2 "didn't land" retry path used to be fully
            # EXEMPT from the cadence floor above (deliberately -- see that
            # branch's own comment: "recovery, not cadence"), which meant a
            # worker that keeps crashing/relaunching right after every reload
            # got hit with an unthrottled re-arm ~1s after every relaunch (the
            # ISSUED_TIMEOUT fires again almost immediately against a worker
            # that never gets a chance to settle). Two floors, independent of
            # the D45 cadence floor above (which stays exempt-on-retry as
            # before): a minimum gap between CONSECUTIVE retries (measured
            # from the last retry, not the original dispatch -- see
            # _rollover_retry_dispatched_at's docstring for why that
            # distinction is what makes this floor actually bind), and a
            # guard against dispatching to a worker whose current pid hasn't
            # been alive long enough to plausibly have settled from its own
            # relaunch.
            last_retry_dispatch = self._rollover_retry_dispatched_at.get(channel_id)
            if (
                last_retry_dispatch is not None
                and self._monotonic() - last_retry_dispatch
                < self._ROLLOVER_RETRY_MIN_INTERVAL_SECONDS
            ):
                return
            pid_age = self._worker_pid_age_seconds(channel_id)
            if pid_age is not None and pid_age < self._ROLLOVER_RETRY_MIN_WORKER_AGE_SECONDS:
                # Item 84 fix, coordinator review round 2: this branch is
                # re-entered on EVERY ~2s poll tick for as long as the pid
                # stays too young (a worker that keeps crashing/relaunching
                # right after every reload never gets a chance to settle) --
                # logging a fresh WARNING every single tick measured 150
                # WARNINGs per 300s. Same cadence rule as the "did not land"
                # WARNING above: once on first deferral, at most once more
                # per _ROLLOVER_RETRY_WARN_INTERVAL_SECONDS while still
                # gated, DEBUG for every other gated tick.
                last_warned_at = self._rollover_pid_age_warned_at.get(channel_id)
                if last_warned_at is None or (
                    self._monotonic() - last_warned_at >= self._ROLLOVER_RETRY_WARN_INTERVAL_SECONDS
                ):
                    _LOG.warning(
                        "Channel automation rollover retry for %s deferred: the current "
                        "worker pid has only been alive %.0fs (< %.0fs floor); waiting "
                        "for it to settle before dispatching another synchronous prepare.",
                        channel_id,
                        pid_age,
                        self._ROLLOVER_RETRY_MIN_WORKER_AGE_SECONDS,
                    )
                    self._rollover_pid_age_warned_at[channel_id] = self._monotonic()
                else:
                    _LOG.debug(
                        "Channel automation rollover retry for %s still deferred: "
                        "worker pid age %.0fs (< %.0fs floor; last warned %.0fs ago).",
                        channel_id,
                        pid_age,
                        self._ROLLOVER_RETRY_MIN_WORKER_AGE_SECONDS,
                        self._monotonic() - last_warned_at,
                    )
                return

        retry_at = self._rollover_retry_at.get(channel_id)
        if retry_at is not None and self._monotonic() < retry_at:
            return
        try:
            if self._boundary_source_plan_provider is not None:
                fresh_plan = self._boundary_source_plan_provider(channel_id, max(now, plan_end_at))
            else:
                fresh_plan = self._source_plan_provider(channel_id)
        except SourcePrepareError:
            # Try again next poll (after the cooldown) -- the current plan
            # still plays out fine meanwhile.
            self._rollover_retry_at[channel_id] = (
                self._monotonic() + self._ROLLOVER_RETRY_COOLDOWN_SECONDS
            )
            return
        if self._boundary_source_plan_provider is not None:
            # U63 defect A: a boundary answer that is only a sub-floor tail of
            # the closing item is not an answer at all -- re-resolve it where
            # that tail ends before deciding this is a fallback case. Only the
            # boundary-provider path can be asked a boundary at all, and a
            # manual override has already returned above, so this is exactly
            # the daemon's own guard set for the same rule.
            fresh_plan = self._resolve_rollover_tail(channel_id, max(now, plan_end_at), fresh_plan)
        force_fallback = fresh_plan is None or not fresh_plan.segments
        fresh_end = plan_end_at
        if not force_fallback:
            fresh_seconds = sum(segment.duration_seconds for segment in fresh_plan.segments)
            fresh_start = (
                max(now, plan_end_at) if self._boundary_source_plan_provider is not None else now
            )
            fresh_end = fresh_start + timedelta(seconds=fresh_seconds)
            # A final schedule window commonly resolves to the same item with
            # less time remaining. It cannot extend the live horizon, so roll
            # seamlessly onto filler instead of allowing EOS.
            force_fallback = fresh_end <= plan_end_at and fresh_seconds < planned_seconds
            if fresh_end <= plan_end_at and not force_fallback:
                self._rollover_retry_at[channel_id] = (
                    self._monotonic() + self._ROLLOVER_RETRY_COOLDOWN_SECONDS
                )
                return
        self._rollover_retry_at.pop(channel_id, None)
        # Item 78 fix 3: hand the daemon the plan_end_at THIS dispatch is
        # computed against before enqueuing the reload, so
        # daemon._try_content_reload can refuse to defer the selector switch
        # if that horizon has already passed by the time the reload actually
        # runs (reload_policy.should_defer_switch's plan_end_at/now
        # parameters) -- optional capability, getattr-probed like
        # dispatched_plan_horizon/has_manual_override above.
        #
        # Round 7 (coordinator review): generate THIS reload command's id
        # up front and pass the SAME id to both calls, so the daemon can
        # scope the recorded value to the exact command it is for (see
        # ``EgressDaemon._rollover_plan_end_at``'s docstring) -- a value
        # recorded here must never be consumed by some other reload for
        # this channel that happens to drain first (an operator-issued one
        # queued ahead of it, or one landing after an intervening crash-
        # relaunch that leaves this record untouched). ``_enqueue`` accepts
        # a pre-generated id for exactly this reason.
        reload_command_id = f"auto-reload-{uuid.uuid4().hex[:12]}"
        record_plan_end = getattr(self._daemon, "record_rollover_plan_end", None)
        if callable(record_plan_end):
            record_kwargs: dict[str, object] = {"command_id": reload_command_id}
            if force_fallback:
                record_kwargs["force_fallback"] = True
            if lead_seconds is not None:
                # U41: carry the horizon this dispatch measured, so a DEFERRED
                # rollover's plan can be built with its life measured from the
                # switch rather than from this dispatch. Only the boundary-
                # provider path measures a lead at all, and only the daemon
                # knows whether the switch will defer (it decides that at
                # reload time, against the live state), so the value travels
                # unconditionally and the daemon applies it to the deferred
                # case alone -- see ``record_rollover_plan_end``.
                record_kwargs["min_plan_seconds"] = lead_seconds
            record_plan_end(channel_id, plan_end_at, **record_kwargs)
        self._enqueue(channel_id, "reload", now=now, command_id=reload_command_id)
        self._rollover_issued.add(channel_id)
        self._rollover_issued_at[channel_id] = self._monotonic()
        self._rollover_dispatched_at[channel_id] = self._monotonic()
        if retrying_undelivered:
            # Item 78 fix 2: track this retry's own dispatch time separately
            # from _rollover_dispatched_at (which _rollover_min_interval_
            # seconds's D45 cadence floor -- the "not retrying_undelivered"
            # branch above -- measures from instead) so the NEXT retry's
            # floor check is measured from THIS retry, not from the original
            # dispatch that started the whole retry sequence.
            self._rollover_retry_dispatched_at[channel_id] = self._monotonic()
            # Item 84 fix: the retry actually dispatched this tick -- log at
            # WARNING (an operator-visible signal that a rollover needed a
            # retry at all) regardless of the rate-limited "still gated"
            # cadence above, and clear that cadence state so a FUTURE
            # undelivered window for this channel logs its own fresh
            # first-crossing WARNING instead of inheriting this timestamp.
            _LOG.warning(
                "Channel automation rollover retry for %s dispatched.",
                channel_id,
            )
            self._rollover_retry_warned_at.pop(channel_id, None)
            self._rollover_pid_age_warned_at.pop(channel_id, None)
        if lead_seconds is not None:
            # U53 item 3: the lead is now adaptive, so say what it was computed
            # FROM. A lead read off an incident log is otherwise
            # indistinguishable from the flat timeout-derived one it replaced
            # (690s for a 300s preparation timeout), and the item that widened
            # it is the whole reason the dispatch moved.
            if asset_seconds is not None and asset_label is not None:
                _LOG.info(
                    "Channel automation computed the rollover lead for %s: lead=%.0fs "
                    "for %s (%.0fs at %.0fx + %.0fs).",
                    channel_id,
                    lead_seconds,
                    asset_label,
                    asset_seconds,
                    self._ROLLOVER_CONFORM_REALTIME_FACTOR,
                    self._ROLLOVER_CONFORM_MARGIN_SECONDS,
                )
            else:
                _LOG.info(
                    "Channel automation computed the rollover lead for %s: lead=%.0fs "
                    "(no asset duration available for the boundary; the "
                    "timeout-derived floor stands).",
                    channel_id,
                    lead_seconds,
                )
        _LOG.info(
            "Channel automation issued a seamless plan rollover for %s: the live plan "
            "ends in %.0fs; target=%s before the engine reaches EOS.",
            channel_id,
            (plan_end_at - now).total_seconds(),
            "filler" if force_fallback else "extended schedule",
        )
        if inside_lead_recovery:
            # BETA.10 U41: say so explicitly. The 2026-09-25 education dark
            # window read "the live plan ends in 80s" with no indication that
            # this was the plan's WHOLE life, not a mistimed dispatch -- the
            # rollover was issued as early as it could be, and the remaining
            # plan was simply shorter than the lead the preparation needs.
            # Operators (and the next incident reader) need that distinction.
            _LOG.info(
                "Channel automation rollover for %s was issued as soon as the previous "
                "plan settled: the plan on air is %.0fs long, shorter than the %.0fs "
                "lead a preparation may need -- there is no earlier moment to dispatch.",
                channel_id,
                (plan_end_at - last_segment_start_at).total_seconds(),
                lead_seconds,
            )
        if not force_fallback and lead_seconds is not None:
            # BETA.10 U60: the work of the boundary ahead can be started before
            # its own dispatch -- warm the first one whose dispatch is still a
            # full lead away, off the air path, so the cache entry its reload
            # will need is resident before its preparation asks for it. The
            # walk inside ``_warm_upcoming_plan`` enforces that invariant.
            #
            # BETA.10 U61: this used to run ONLY when ``inside_lead_recovery``
            # -- the short-leg case U60 was written for, where the plan on air
            # is shorter than the lead and its own start WAS the trigger. That
            # gate excluded the ordinary cadence case, and ordinary cadence is
            # where a long title needs it most. Live, 2026-09-27: government
            # aired a 9020s meeting (Sustainability Advisory Board - April 2026)
            # as 1800s slices, one scheduled item per plan. 1800s of plan
            # against the deployed 690s lead is NOT ``inside_lead_recovery``, so
            # the look-ahead never ran once: zero look-ahead lines in any
            # station log, no ``warm/`` scratch under any channel, and the
            # title's only cache artifact was the probe-only sidecar
            # (``full_asset_conform: false``, no ``.ts``) its first slice left
            # behind. Every slice therefore paid its own cold bounded conform --
            # 383.6s, 406.0s, 458.5s, 293.6s, 267.0s, a fifth of each 1800s
            # slice's wall clock spent re-encoding an asset the station had
            # already conformed once, and the worst of them eating the 690s lead
            # down to ~230s of margin. The whole-asset conform that makes every
            # slice after the first a stream-copy HIT is exactly what this
            # look-ahead is for, and in this regime it never ran at all.
            #
            # Nothing in the walk below weakens for the wider gate: it still
            # stops at the first boundary at least ``now + lead`` out, so it
            # never races the reload's own synchronous conform, and a boundary
            # nearer than that is still deliberately left alone. In this regime
            # the very first step already qualifies (a full-length plan's next
            # boundary dispatches a whole plan length plus a lead from now),
            # which is what gives a repeat slice ~30 minutes of runway on the
            # single warm worker. The air path asks for the SAME warm, but it
            # asks from inside ``_prepare_segment`` and only once that slice's
            # own conform has returned -- a measured 267-458s later -- so its
            # window is what is left of the slice minus whatever else the one
            # worker is already holding. This gate is a head start, not a
            # guarantee: a title long enough to outrun the runway still needs
            # the queue fixes that follow.
            # Runs strictly AFTER the enqueue above -- a latency optimisation
            # must never sit between the dispatch and the command that carries
            # it.
            self._warm_upcoming_plan(
                channel_id,
                now=now,
                lead_seconds=lead_seconds,
                fresh_start=fresh_start,
                fresh_end=fresh_end,
            )

    #: How many boundaries the U60 look-ahead will step over before giving up.
    #: Deliberately a STEP cap and not a time horizon: a boundary beyond any
    #: fixed horizon is *more* savable than one inside it, not less (its
    #: dispatch, ``max(previous_start, its_end - lead)``, is further out), so a
    #: "too far away, stop" test would abandon the search one step before the
    #: first plan it should have warmed.
    _ROLLOVER_LOOKAHEAD_MAX_STEPS = 8

    def _warm_upcoming_plan(
        self,
        channel_id: str,
        *,
        now: datetime,
        lead_seconds: float,
        fresh_start: datetime,
        fresh_end: datetime,
    ) -> None:
        """Ask the daemon to pre-conform the first boundary a full lead away.

        Walks the boundaries after ``fresh_end`` -- the plan just dispatched --
        and stops at the first whose own dispatch is at least ``now +
        lead_seconds`` out. That is the earliest boundary a fresh whole-asset
        conform could still finish before, and asking for any NEARER one would
        be actively harmful: it would start a second whole-asset encode racing
        the reload's own synchronous cold conform for the very same asset.

        One case reaches past that rule and is warmed anyway (U63 defect B):
        a boundary whose dispatch the model collapses onto ``previous_start``.
        That happens exactly when the plan just dispatched is shorter than the
        lead -- the regime this whole walk was written for -- and it is not a
        collision with the reload's own conform but the SAME encode, started at
        the only moment left. A shared cache key is deduped either way
        (``_warming``), so the two can never run twice.

        The dispatch model is ``dispatch(B) = max(previous_boundary,
        B - lead)`` (``_check_plan_rollover``'s own ``trigger_at``), so the
        walk reproduces it exactly rather than estimating. Best-effort
        throughout: every fault, every unresolvable boundary and every missing
        daemon capability ends the walk quietly. The dispatch it follows has
        already been enqueued.
        """

        try:
            warmer = getattr(self._daemon, "warm_source_plan", None)
            if not callable(warmer):
                # An older/bare daemon, or one built without a source
                # preparer: nothing to warm with. Behaviour is unchanged.
                return
            lead = timedelta(seconds=lead_seconds)
            savable_at = now + lead
            boundary = fresh_end
            previous_start = fresh_start
            for _ in range(self._ROLLOVER_LOOKAHEAD_MAX_STEPS):
                dispatch_at = max(previous_start, boundary - lead)
                plan = self._resolve_boundary_plan_at(channel_id, boundary)
                if plan is None or not plan.segments:
                    return
                # U63 defect B: ``dispatch_at <= previous_start`` means this
                # boundary's own dispatch has collapsed onto the instant we are
                # dispatching from, and the ``now + lead`` test above cannot see
                # the difference -- it rejects such a boundary by the poll
                # overshoot ALONE. That collapse is precisely what a plan
                # shorter than the lead does to every boundary after it: the
                # max above always picks ``previous_start``, so the boundary the
                # warm exists for is skipped and the walk warms the one after it
                # a whole plan later. Live, 2026-09-29 00:23:48: government's
                # 155s tail plan was dispatched with the 1800s item behind it,
                # the 00:23:48 walk stepped over that item's boundary and warmed
                # the NEXT one, and the item then prepared cold for 302.8s while
                # the channel aired filler. When there is no earlier moment to
                # dispatch, the only moment left to start the conform is now --
                # the same sentence the rollover's own inside-lead recovery line
                # states for the dispatch. Only the first step can reach this:
                # every
                # later step's ``previous_start`` is a boundary past
                # ``fresh_start``.
                if dispatch_at >= savable_at or dispatch_at <= previous_start:
                    warmer(channel_id, plan)
                    return
                advanced = sum(segment.duration_seconds for segment in plan.segments)
                if advanced <= 0:
                    return
                previous_start = boundary
                boundary = boundary + timedelta(seconds=advanced)
        except Exception:
            # A look-ahead that cannot be computed is a missed optimisation,
            # never a failed rollover. The dispatch is already enqueued.
            _LOG.debug(
                "Channel automation plan look-ahead for %s did not complete; "
                "the dispatched rollover is unaffected.",
                channel_id,
                exc_info=True,
            )

    def _resolve_boundary_plan_at(
        self, channel_id: str, boundary: datetime
    ) -> EgressSourcePlan | None:
        """The schedule plan airing at ``boundary``, or None if there is none.

        The same provider call ``_check_plan_rollover`` itself makes, with the
        same "past the last published item" meaning: ``SourcePrepareError`` is
        the documented way the provider says so, and for a look-ahead that is
        simply the end of the walk.
        """

        provider = self._boundary_source_plan_provider
        if provider is None:
            return None
        try:
            return cast(EgressSourcePlan | None, provider(channel_id, boundary))
        except SourcePrepareError:
            return None

    def _reestablish_plan_horizon(
        self,
        channel_id: str,
        *,
        now: datetime,
        proof_event_id: str | None,
        previous_end_at: datetime | None,
        previous_planned_seconds: float | None = None,
    ) -> None:
        """(Re)establish ``channel_id``'s tracked plan horizon from the plan the
        daemon actually dispatched (falling back to re-querying the schedule
        provider when the daemon can't say). Shared by ``_check_plan_rollover``'s
        two "the tracked horizon can no longer be trusted" cases: a fresh
        proof event just took air, AND (item 78 fix 2) a tracked horizon whose
        ``plan_end_at`` has already slipped into the past -- both need the
        exact same recovery, just discard-and-recompute against the current
        proof event/dispatch rather than the stale tuple."""

        if self._establish_horizon_from_dispatch(
            channel_id,
            now=now,
            proof_event_id=proof_event_id,
            previous_end_at=previous_end_at,
            previous_planned_seconds=previous_planned_seconds,
        ):
            return
        retry_at = self._rollover_retry_at.get(channel_id)
        if retry_at is not None and self._monotonic() < retry_at:
            self._plan_horizon.pop(channel_id, None)
            return
        try:
            plan = self._source_plan_provider(channel_id)
        except SourcePrepareError:
            self._rollover_retry_at[channel_id] = (
                self._monotonic() + self._ROLLOVER_RETRY_COOLDOWN_SECONDS
            )
            return
        if plan is None or not plan.segments:
            self._plan_horizon.pop(channel_id, None)
            self._rollover_retry_at[channel_id] = (
                self._monotonic() + self._ROLLOVER_RETRY_COOLDOWN_SECONDS
            )
            return
        self._rollover_retry_at.pop(channel_id, None)
        planned_seconds = sum(segment.duration_seconds for segment in plan.segments)
        plan_end_at = now + timedelta(seconds=planned_seconds)
        last_segment_start_at = plan_end_at - timedelta(seconds=plan.segments[-1].duration_seconds)
        self._plan_horizon[channel_id] = (
            proof_event_id,
            plan_end_at,
            last_segment_start_at,
            planned_seconds,
        )

    def _track_worker_pid(self, channel_id: str, state_row: Any) -> None:
        """Item 78 fix 2: record when the channel's CURRENT worker pid was
        first observed, so ``_worker_pid_age_seconds`` can tell "just
        relaunched" apart from "been airing a while" for the rollover
        retry-timeout path (see ``_check_plan_rollover``'s ``retrying_undelivered``
        branch). Called on every ON_AIR pass regardless of whether a rollover
        is due, so the age is accurate whenever it is actually consulted."""

        pid = getattr(state_row, "pid", None)
        if pid is None:
            # No pid to time (a bare test double, or a state row written
            # before the daemon populated pid) -- nothing to track. Treated
            # as "no information" by _worker_pid_age_seconds, never as "just
            # relaunched": an unknown pid must never block a retry forever.
            self._pid_first_seen_at.pop(channel_id, None)
            return
        seen = self._pid_first_seen_at.get(channel_id)
        if seen is None or seen[0] != pid:
            self._pid_first_seen_at[channel_id] = (pid, self._monotonic())

    def _worker_pid_age_seconds(self, channel_id: str) -> float | None:
        """How long (seconds) the channel's current worker pid has been
        tracked as alive, per ``_track_worker_pid``. ``None`` when nothing has
        been tracked yet (never dispatch a rollover retry with no pid
        information at all -- treated as "don't know, don't block")."""

        seen = self._pid_first_seen_at.get(channel_id)
        if seen is None:
            return None
        return self._monotonic() - seen[1]

    def _establish_horizon_from_dispatch(
        self,
        channel_id: str,
        *,
        now: datetime,
        proof_event_id: str | None,
        previous_end_at: datetime | None,
        previous_planned_seconds: float | None = None,
    ) -> bool:
        """Set this channel's tracked plan horizon from the plan the daemon
        ACTUALLY dispatched. Returns False when the daemon cannot say (a bare test
        double, or no dispatch recorded for this proof event yet), leaving the
        caller on its legacy re-query path.

        Hostile-review (d) fix. The re-query path this replaces asked the source
        plan provider for a plan again and summed THAT. The provider re-windows
        from whatever schedule item is live at the moment of the call, capped at
        ``max_segments`` (``source_plan.build_source_plan_from_schedule``), so the
        answer is a different segment list than the one on air -- and the derived
        ``plan_end_at`` can overshoot far enough that
        ``rollover_trigger_at`` lands ON the real EOS, which is precisely the
        boundary this whole pass exists to get ahead of. The dispatched plan is
        the only list whose durations are the ones the engine is playing.

        The deferred-switch case is the second half of the fix: a boundary-aligned
        rollover plan (``switch_at_end_of_current``) does not start when it is
        dispatched -- the engine holds it, prerolled, until the OUTGOING leg's own
        end. Its projected end therefore runs from the previous plan's projected
        end, not from ``now`` (measuring from ``now`` would put the horizon a whole
        rollover lead time -- up to ``_ROLLOVER_MIN_LEAD_SECONDS`` -- early, and
        make every subsequent rollover fire before there was anything to roll)."""

        reader = getattr(self._daemon, "dispatched_plan_horizon", None)
        if not callable(reader):
            return False
        dispatched = reader(channel_id)
        if not dispatched:
            return False
        dispatched_proof_id, durations, switch_deferred = dispatched
        if dispatched_proof_id != proof_event_id or not durations:
            # A dispatch we have no matching proof event for is not evidence about
            # what is on air right now.
            return False
        starts_at = now
        if switch_deferred and previous_end_at is not None:
            # A deferred switch is observed at (or a few seconds after) the
            # outgoing leg's boundary, so a trustworthy ``previous_end_at`` is
            # at most the OUTGOING plan's own rollover lead ahead of ``now``.
            # Anything further out is a poisoned tuple (sandbox soak 39d852e:
            # a stale-horizon re-establishment during settlement dated the OLD
            # plan from "now", one plan-length ahead) and must not push this
            # horizon a whole plan into the future -- date from ``now``
            # instead.
            #
            # Hostile review, round 2 (finding 2): the bound is the outgoing
            # plan's own scaled lead, ``_rollover_min_lead_seconds(previous_
            # planned_seconds)`` = min(120s, half the plan), NOT the flat
            # ``_ROLLOVER_MIN_LEAD_SECONDS``. The poisoned anchor sits about
            # one outgoing-plan-length ahead, so against a plan shorter than
            # 120s a flat 120s bound honoured the poison and the belt was
            # inert exactly where short schedule slots make it matter. When
            # the caller cannot say how long the outgoing plan was (no tracked
            # tuple), fall back to the flat ceiling as before.
            if previous_planned_seconds is None:
                anchor_lead_seconds = self._ROLLOVER_MIN_LEAD_SECONDS
            else:
                anchor_lead_seconds = self._rollover_min_lead_seconds(previous_planned_seconds)
            if now < previous_end_at <= now + timedelta(seconds=anchor_lead_seconds):
                starts_at = previous_end_at
        planned_seconds = sum(durations)
        plan_end_at = starts_at + timedelta(seconds=planned_seconds)
        last_segment_start_at = plan_end_at - timedelta(seconds=durations[-1])
        self._plan_horizon[channel_id] = (
            proof_event_id,
            plan_end_at,
            last_segment_start_at,
            planned_seconds,
        )
        self._rollover_retry_at.pop(channel_id, None)
        return True

    def _daemon_has_manual_override(self, channel_id: str) -> bool:
        """B1 fix: consult ``EgressDaemon.has_manual_override`` /
        ``PlayoutSupervisor.has_manual_override`` if the wired daemon defines
        it (every real daemon does -- see daemon.py/supervisor.py); ``getattr``
        keeps a bare test double that predates this method working exactly as
        before (no override signal available -> treated as none active)."""

        has_override = getattr(self._daemon, "has_manual_override", None)
        if not callable(has_override):
            return False
        return bool(has_override(channel_id))

    def _daemon_has_operator_stop_intent(self, channel_id: str) -> bool:
        """Optional daemon latch: a direct operator stop suppresses auto_start."""

        has_stop = getattr(self._daemon, "has_operator_stop_intent", None)
        return bool(callable(has_stop) and has_stop(channel_id))

    def _daemon_has_pending_reload_settlement(self, channel_id: str) -> bool:
        """F1 redesign follow-up (coordinator hostile review, 2026-09-06): a
        DEFERRED/boundary-aligned reload's ``current_proof_event_id`` does not
        change until it actually SETTLES (``EgressDaemon._commit_reload_
        settlement``) -- for an automation-driven ON_AIR extension that can be
        ~120s+ after ``_check_plan_rollover`` dispatched it (the whole point of
        triggering early, ``reload_policy.rollover_trigger_at``), and up to the
        engine's own ``defer_switch_timeout_s`` (900s default) in the worst
        case. ``_ROLLOVER_ISSUED_TIMEOUT_SECONDS`` (45s) is far shorter than
        that, so without this check the "did the dispatched reload land"
        timeout below would clear its latch and retry -- re-preparing
        synchronously, superseding the still-armed leg in the engine, and
        creating another prepared-plan directory -- every ~45s while a
        perfectly healthy deferred reload is still legitimately settling.

        ``has_pending_reload_settlement`` is an OPTIONAL daemon capability
        (only ``EgressDaemon``/``PlayoutSupervisor`` implement it; a bare test
        double need not), resolved via ``getattr`` like
        ``has_manual_override``/``dispatched_plan_horizon`` above. While it
        reports True, this method treats the rollover as "landed, waiting" --
        never retrying -- and relies entirely on the DAEMON's own bounded
        deadline (``_PENDING_RELOAD_SETTLE_DEADLINE_S``) to eventually either
        commit (which changes ``current_proof_event_id``, handled by the
        existing "fresh plan just took air" branch above) or fall back to
        restart (which flips the state row away from ON_AIR, handled by this
        method's own top-of-function early return)."""

        reader = getattr(self._daemon, "has_pending_reload_settlement", None)
        if not callable(reader):
            return False
        return bool(reader(channel_id))

    def _daemon_has_pending_preparation(self, channel_id: str) -> bool:
        """U47: whether the daemon is already preparing THIS channel's program.

        Same optional-capability shape as ``has_manual_override`` and
        ``_daemon_has_pending_reload_settlement`` above: a daemon double that
        predates the reader answers "no", preserving its existing behavior.

        This is deliberately the NARROW reader (``has_pending_preparation``),
        not ``has_pending_reload_settlement``: the latter also reports an ARMED
        reload that is still settling, and using it here would hold the
        slate-replan pass off through every rollover's settle window -- a
        change with its own latch and cooldown, and none of U47's business.

        The window this closes is the slate-first hand-off's own conform: the
        channel is legitimately FALLBACK_SLATE (the slate is on air and the
        program is being prepared) while a preparation for it is registered, so
        without this the pass would queue a ``reload`` that
        ``EgressDaemon._request_reload`` answers with ``_cancel_preparation`` --
        abandoning the hand-off and conforming the same program a second time."""

        reader = getattr(self._daemon, "has_pending_preparation", None)
        if not callable(reader):
            return False
        return bool(reader(channel_id))

    def _daemon_chained_slate_program_end(self, channel_id: str) -> datetime | None:
        """U53 item 1: when the plan on air is a CHAINED filler -- a slate
        trimmed to the gap it fills, followed in the SAME plan by the programme
        due when that gap closes -- the projected end of that programme; else
        None.

        Same optional-capability shape as the three probes above: a daemon that
        predates the reader answers None, which is exactly this gate's
        "nothing chained here" case.

        Needed because a chained leg makes the state row lie in the one
        direction this pass acts on: ``target_state`` is FALLBACK_SLATE and
        ``segments[0]`` is the slate, so the row reads FALLBACK_SLATE for the
        whole leg -- while a real programme is airing behind the filler. See
        ``_check_slate_replan``'s gate for what is done with it."""

        reader = getattr(self._daemon, "chained_slate_program_end", None)
        if not callable(reader):
            return None
        value = reader(channel_id)
        return value if isinstance(value, datetime) else None

    def _daemon_worker_initial_control_connection_observed(self, channel_id: str) -> bool:
        """Read the optional initial worker-control connection observation.

        Older daemon doubles and non-GStreamer strategies have no asynchronous
        Windows pipe-connect phase, so absence preserves the existing ready
        behavior.
        """

        reader = getattr(self._daemon, "worker_initial_control_connection_observed", None)
        if not callable(reader):
            return True
        return bool(reader(channel_id))

    def _enqueue(
        self, channel_id: str, action: str, *, now: datetime, command_id: str | None = None
    ) -> None:
        """Enqueue an automation-issued command. ``command_id`` lets a caller
        that must correlate this command with other bookkeeping (round 7:
        ``_check_plan_rollover`` scoping a recorded ``record_rollover_plan_end``
        value to the exact reload it is for) pre-generate the id and pass it
        in, rather than only being able to read it back after the fact.
        Defaults to generating a fresh one, same as every pre-round-7 caller.

        Round 8 (coordinator review): dropped the ``str`` return value --
        every call site pre-generates its own id when it needs to correlate
        one (see ``_check_plan_rollover`` above), and nothing anywhere reads
        the id this method generates internally for the callers that don't
        supply one."""
        resolved_command_id = command_id or f"auto-{action}-{uuid.uuid4().hex[:12]}"
        self._store.enqueue_command(
            EgressCommand(
                channel_id=channel_id,
                action=action,  # type: ignore[arg-type]
                issued_at=now,
                issued_by=_ISSUED_BY,
                command_id=resolved_command_id,
            )
        )


_RELAY_CMDLINE_MARKERS = ("-f decklink", "-f libndi_newtek")


def reap_predecessor_relays(
    *,
    boot_epoch: float,
    scanner: Any = None,
    terminator: Any = None,
    store: Any = None,
) -> list[int]:
    """Terminate relay ffmpeg processes left by a previous server (ENG-003).

    Relay children survive a server kill exactly like encoder children
    (#161), but their pids are tracked only in memory - so they are found by
    their DISTINCTIVE command line instead: an ffmpeg whose argv contains an
    NDI relay muxer flag and whose create time predates this server. (3.0 is
    IP-only; the SDI/DeckLink relay is descoped, so the surviving case is the
    NDI relay holding an NDI name.) The kill is TOCTOU-safe (the terminator
    re-verifies create_time via ``process_identity.verify_and_kill_process``).

    S9-4: when ``store`` is given, each reap is recorded as a durable
    ``civiccast-egress-coprocess-lifecycle`` proof event so the boot cleanup is
    auditable, not just a log line.
    """

    scan = scanner or _default_relay_scanner
    kill = terminator or _default_relay_terminator
    reaped: list[int] = []
    for pid, name, cmdline, created_at in scan():
        if "ffmpeg" not in (name or "").lower():
            continue
        if created_at >= boot_epoch:
            continue
        if not any(marker in cmdline for marker in _RELAY_CMDLINE_MARKERS):
            continue
        kill(pid, created_at)
        reaped.append(pid)
        _LOG.warning(
            "Reaped predecessor relay ffmpeg pid %s (left by a previous "
            "server process; it was holding the relay output).",
            pid,
        )
        if store is not None:
            try:
                store.append_proof_event(
                    EgressProofEvent(
                        event_id=f"egress-coproc-reap-{pid}-{int(created_at)}",
                        observed_at=datetime.now(UTC),
                        channel_id="egress-system",
                        state="STARTING",
                        source_label="(predecessor relay reap)",
                        source_path=f"coproc-pid-{pid}",
                        proof_boundary="civiccast-egress-coprocess-lifecycle",
                        machine_summary=(
                            f"Reaped a predecessor relay co-process (pid {pid}) left by a "
                            "previous server before startup; its output/name is freed."
                        ),
                    )
                )
            except Exception:  # auditing must never block startup
                _LOG.exception("Failed to record co-process reap proof event for pid %s.", pid)
    return reaped


def _default_relay_scanner() -> list[tuple[int, str, str, float]]:
    import psutil

    rows: list[tuple[int, str, str, float]] = []
    for process in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            info = process.info
            cmdline = " ".join(info.get("cmdline") or [])
            rows.append(
                (info["pid"], info.get("name") or "", cmdline, info.get("create_time") or 0.0)
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return rows


def _default_relay_terminator(pid: int, created_at: float) -> None:
    from civiccast.egress.daemon import _default_orphan_terminator

    _default_orphan_terminator(pid, created_at)


def _udp_ts_source_uri(config: Any) -> str | None:
    """The channel's UDP TS output a side-relay can eat, if any."""

    for sink in config.sinks:
        if sink.kind in ("udp-ts", "local-ts") and sink.uri.startswith("udp://"):
            return sink.uri  # type: ignore[no-any-return]
    return None


def _default_ndi_factory(*, channel_id: str, ndi_name: str, source_uri: str) -> Any:
    from civiccast.egress.ndi_relay import NdiRelaySettings, NdiRelaySupervisor

    return NdiRelaySupervisor(
        channel_id=channel_id,
        ndi_name=ndi_name,
        source_uri=source_uri,
        settings=NdiRelaySettings.from_env(),
    )


def _default_sdi_factory(*, channel_id: str, device: str, source_uri: str) -> Any:
    from civiccast.egress.sdi_relay import SdiRelaySettings, SdiRelaySupervisor

    return SdiRelaySupervisor(
        channel_id=channel_id,
        device=device,
        source_uri=source_uri,
        settings=SdiRelaySettings.from_env(),
    )


def summarize_automation(store: EgressStore) -> ChannelAutomationRollup:
    """Roll up the state of every auto_start channel for System Health.

    "Dark" means an automated channel whose last known state is missing,
    STOPPED, or ERROR — the operator promised 24/7 and the channel is not
    delivering it. FALLBACK_SLATE counts as on-air-with-filler (the channel
    IS broadcasting; the schedule just has a gap).
    """

    from civiccast.egress.models import ChannelAutomationRollup

    automated = 0
    on_air = 0
    on_slate = 0
    dark: list[str] = []
    for config in store.list_configs():
        if not (config.enabled and config.auto_start):
            continue
        automated += 1
        state_row = store.read_state(config.channel_id)
        state = state_row.state if state_row is not None else None
        if state in ("ON_AIR", "STARTING", "TRANSITIONING"):
            on_air += 1
        elif state == "FALLBACK_SLATE":
            on_slate += 1
        else:
            dark.append(config.channel_id)
    return ChannelAutomationRollup(automated=automated, on_air=on_air, on_slate=on_slate, dark=dark)


def build_channel_automation(
    session_factory: Any,
    *,
    work_dir: Path | None = None,
    alert_evaluator_hook: AlertEvaluatorHook | None = None,
    channel_start_hook: Callable[[str], None] | None = None,
) -> ChannelAutomationService:
    """Construct the wired automation service (same shape as the egress CLI).

    ``alert_evaluator_hook`` (S8-3/S8-5) is threaded to the EgressDaemon so each
    health sample feeds the operational alert evaluator.
    """

    from civiccast.egress.audio_tracks import AudioTrackStore
    from civiccast.egress.bulletin_filler import (
        build_board_live_video_provider,
        build_filler_source_provider,
    )
    from civiccast.egress.caption_proof import build_caption_status_provider
    from civiccast.egress.hls_relay import HlsRelaySupervisor
    from civiccast.egress.preparer import SourcePreparer
    from civiccast.egress.source_plan import (
        SCHEDULE_GAP_ABSORB_SECONDS,
        ScheduleSourcePlanProvider,
        gstreamer_source_segment_seconds_from_env,
        schedule_loop_enabled_from_env,
    )
    from civiccast.egress.store import PostgresEgressStore
    from civiccast.egress.supervisor import PlayoutSupervisor
    from civiccast.egress.takeover_store import PostgresTakeoverAuditStore
    from civiccast.egress.ts_relay import TsRelaySupervisor
    from civiccast.reporting.asrun_outbox import AsRunOutbox, default_asrun_outbox_path
    from civiccast.reporting.asrun_recorder import StoreAsRunRecorder
    from civiccast.reporting.store import ReportingStore
    from civiccast.schedule.models import SCHEDULE_STATE_PUBLISHED
    from civiccast.schedule.store import PostgresAssetStore, PostgresScheduleStore
    from civiccast.underwriting.store import UnderwritingStore

    resolved_work_dir = (work_dir or default_egress_work_dir()).expanduser()
    resolved_work_dir.mkdir(parents=True, exist_ok=True)
    store = PostgresEgressStore(session_factory)
    # ENG-003 / S9-4: clear any relay co-process a previous server left holding its
    # NDI name, recording each reap as a durable proof event. Never blocks startup.
    try:
        reap_predecessor_relays(boot_epoch=time.time(), store=store)
    except Exception:
        _LOG.exception("Predecessor relay reap failed; continuing startup.")
    # BUG C2 fix (S23 §6.1 durable outbox): one AsRunOutbox for the process,
    # rooted at the real station-data-dir journal, shared by the recorder
    # (opportunistic drain on every write) and this service's periodic
    # drain tick. Constructing it here only opens the LOCAL journal file
    # (plain sqlite3, not the app's SQLAlchemy engine) -- cheap and safe to
    # do synchronously. The startup replay itself is deliberately NOT run
    # here: build_channel_automation runs inside civiccast.app.create_app()
    # (via _wire_stage_f_workers / _wire_durable_stores /
    # _install_durable_store_wiring when DATABASE_URL is set at boot), a
    # path that must never touch the database (pinned by
    # tests/schedule/test_app_wiring.py::TestAppFactorySetEnv::
    # test_create_app_does_not_call_engine_connect). AsRunOutbox.
    # ensure_started() defers the replay to the first real drain attempt
    # instead (the recorder's first opportunistic write, or this service's
    # first run_once poll tick -- see its docstring), which only happens
    # once the app is actually serving, so the "a crash mid-drain loses
    # nothing" guarantee still holds without an eager DB touch here.
    as_run_outbox = AsRunOutbox(
        ReportingStore(session_factory),
        db_path=default_asrun_outbox_path(),
        alert_session_factory=session_factory,
    )
    # DEFECT C: one alert-firing/clearing instance shared between the daemon's
    # per-command failure hook (fires mid-pass) and the service's whole-pass
    # exception handler (fires for a raise outside process_once) -- see
    # _ChannelAutomationAlerts's docstring for why they must share state.
    automation_alerts = _ChannelAutomationAlerts(session_factory)
    asset_store = PostgresAssetStore(session_factory)
    schedule_store = PostgresScheduleStore(session_factory)
    source_plan_provider = ScheduleSourcePlanProvider(
        schedule_items_provider=lambda channel_id: schedule_store.list(
            channel_id=channel_id,
            states=(SCHEDULE_STATE_PUBLISHED,),
        ),
        asset_resolver=asset_store.get_staff_row,
        max_segments=1 if gstreamer_engine_selected() else 8,
        loop_schedule=schedule_loop_enabled_from_env(),
        max_segment_seconds=(
            gstreamer_source_segment_seconds_from_env() if gstreamer_engine_selected() else None
        ),
        # U26: absorb a small schedule gap at the rollover boundary instead of
        # resolving it to filler. The boundary between two items is where this
        # provider's answer decides whether the channel leaves its program at
        # all -- a filler answer there is what puts it on slate and into F3(b)'s
        # exit-and-restart detour (reports/U26.md). See
        # SCHEDULE_GAP_ABSORB_SECONDS for the size and the reasoning.
        gap_absorb_seconds=SCHEDULE_GAP_ABSORB_SECONDS,
    )
    # #156: the persistent conform cache emits playout-time trims when the
    # engine honors them — the legacy ffmpeg-concat engine does (ffconcat
    # inpoint/outpoint); the gst engine (default) reads only segment.path, so
    # it gets the fast stream-copy fallback instead.
    # F3 fix (hostile-review follow-up, 2026-09-06): kept as a named instance
    # so `.release` (immediate per-plan-directory reclaim once the daemon
    # independently knows a plan is retired) can be wired alongside `.prepare`
    # -- see cli.py's identical pattern.
    source_preparer_instance = SourcePreparer(
        work_dir=resolved_work_dir,
        playout_trim_supported=not gstreamer_engine_selected(),
    )
    # S5: the production engine is the PlayoutSupervisor (subclass of EgressDaemon)
    # so live takeover/handback work. lookahead_source_plan_provider=None keeps
    # source selection a single-plan passthrough of source_plan_provider —
    # behaviorally identical to the base daemon for scheduled playout (the
    # look-ahead-combine window is a deliberate follow-on, not enabled here, to
    # protect the 24h-proven automation path). takeover_audit_store lets the
    # supervisor read the open session a "takeover" command must put live.
    daemon = PlayoutSupervisor(
        store,
        work_dir=resolved_work_dir,
        source_plan_provider=source_plan_provider,
        boundary_source_plan_provider=(
            source_plan_provider.plan_at if gstreamer_engine_selected() else None
        ),
        # U53 item 1: a filler rollover trims the filler to the gap and chains
        # the programme due when it closes. Wired only where the shape it
        # depends on holds -- the GStreamer engine, whose preparer writes each
        # per-plan file at the segment's declared duration (the same reason
        # boundary_source_plan_provider above is gated the same way). The
        # ffmpeg-concat engine stays exactly as it was: filler airs whole.
        next_program_start_provider=(
            source_plan_provider.next_item_start_at if gstreamer_engine_selected() else None
        ),
        lookahead_source_plan_provider=None,
        takeover_audit_store=PostgresTakeoverAuditStore(session_factory),
        # CA-3: gaps fill per the channel's fill_policy — rotating approved
        # community bulletins or the plain slate.
        fallback_source_provider=build_filler_source_provider(
            session_factory, work_dir=resolved_work_dir
        ),
        board_live_video_provider=build_board_live_video_provider(session_factory),
        source_preparer=source_preparer_instance.prepare,
        async_source_preparer=lambda plan, config, cancel, protected: (
            source_preparer_instance.prepare(
                plan, config, cancel_event=cancel, protected_plan_dirs=protected
            )
        ),
        prepared_plan_release=source_preparer_instance.release,
        # U60: when the plan on air is shorter than the rollover lead, the
        # next boundary's preparation has less runway than its own whole-asset
        # conform needs. This lets the automation warm a later boundary's
        # assets while the current short leg is still airing. Ungated by
        # engine: it is a cache warm, and the ffmpeg-concat path benefits the
        # same way.
        source_plan_warmer=source_preparer_instance.warm_plan,
        resolve_secret=lambda ref: os.environ.get(ref),
        # S15: the GStreamer engine (default) or ffmpeg-concat (legacy), per
        # CIVICCAST_EGRESS_ENGINE.
        # S11 gap 9: the gst engine gets a per-channel secondary-audio (SAP/descriptive)
        # provider so it can mux extra audio PIDs; the ffmpeg path ignores it.
        encoder_strategy=build_encoder_strategy(
            audio_tracks_provider=lambda channel_id: AudioTrackStore(session_factory).list_tracks(
                scope="channel", target_id=channel_id, enabled_only=True
            ),
            emergency_provider=EasDisplayService(EasStore(session_factory)).active_presentation,
        ),
        # S8: feed each health sample to the operational alert evaluator.
        alert_evaluator_hook=alert_evaluator_hook,
        # #151: channel-lifetime TS relay (TSDuck continuity --fix + pcradjust)
        # so encoder relaunches never reset the mux session at a udp-ts
        # headend. auto mode: active only when tsp is available.
        ts_relay_supervisor=TsRelaySupervisor(),
        # DEFECT A: the GStreamer engine's hls sinks are delivered by a
        # supervised ffmpeg relay (real segments + a real manifest) -- see
        # civiccast.egress.hls_relay's module docstring for why no native
        # GStreamer element can do this with the shipped runtime's plugins.
        hls_relay_supervisor=HlsRelaySupervisor(),
        # DEFECT D/C: a command that fails inside process_once no longer
        # aborts the rest of its batch (see EgressDaemon.process_once); this
        # hook is how that failure still reaches the operator alert surface.
        command_failure_hook=automation_alerts.on_command_failure,
        # S11a: caption_status reflects the latest caption decode-back proof
        # (fail-closed — not-verified until a fresh PASS is persisted by the
        # caption proof loop), instead of a hardcoded posture.
        caption_status_provider=build_caption_status_provider(store),
        # Live captions do not retain review audio. An optional recording-review
        # archive must never gate broadcast readiness or trigger discovery here.
        caption_readiness_provider=None,
        # S23: at each ACTUAL source transition the engine appends an as-run
        # entry (proof-of-performance) to the durable franchise-compliance
        # ledger. Append-only side-write; never blocks playout (every call is
        # guarded). Distinct from the trimmed proof-event ring buffer. BUG C2
        # fix: routed through the shared as_run_outbox (journal-first, never
        # a bare direct store write) instead of a fresh unwired recorder.
        as_run_recorder=StoreAsRunRecorder(
            ReportingStore(session_factory),
            outbox=as_run_outbox,
            underwriting_store=UnderwritingStore(session_factory),
        ),
        channel_start_hook=channel_start_hook,
    )
    # Item 5 fix: only the daemon knows which per-plan directories are
    # currently LIVE (active on-air + armed-not-yet-settled) -- wire it back
    # into the preparer's own GC pass now that both exist.
    source_preparer_instance.set_protected_plan_dirs_provider(daemon.live_prepared_plan_dirs)
    # GStreamer degraded-mode tier 3: if the station bootstrap found the
    # GStreamer closure corrupt/unrepairable and switched egress to FFmpeg
    # (build_encoder_strategy above therefore returned ConcatEncoderStrategy),
    # raise the loud operator alert now -- this is the first seam past the
    # pre-DB station bootstrap that holds a session_factory. Health goes off
    # "green healthy" via the critical alert (safe-to-air surface).
    _egress_degraded_reason = os.environ.get(EGRESS_DEGRADED_REASON_ENV, "").strip()
    if _egress_degraded_reason:
        _raise_egress_degraded_alert(session_factory, reason=_egress_degraded_reason)
    return ChannelAutomationService(
        store,
        daemon,
        source_plan_provider,
        settings=ChannelAutomationSettings.from_env(),
        boundary_source_plan_provider=(
            source_plan_provider.plan_at if gstreamer_engine_selected() else None
        ),
        # BUG C2 fix: the periodic poll retries any as-run drain backlog
        # left by a DB outage once the DB comes back (see run_once's
        # _drain_as_run_outbox).
        as_run_outbox=as_run_outbox,
        # DEFECT C: whole-pass failures raise/clear through the same
        # instance the daemon's command_failure_hook above uses.
        automation_alerts=automation_alerts,
    )
