# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The pure D3 decision table plus the continuous-enforcement monitor.

``decide`` is the whole hazard boundary for the native (Windows) half of the
dual-runtime exclusion guard (charter Sec.6 / gate 4, spec
``spec-dual-runtime-guard.md``): a pure, total function from
:class:`~civiccast.native.models.GuardInputs` to
:class:`~civiccast.native.models.GuardDecision`. It performs no I/O -- every
probe result is composed by the caller (real probes in
``civiccast.native.win_probes`` on Windows, fakes in tests everywhere).

This module also owns the registry/serialization contracts that
``win_probes`` and the CLI verbs read/write (kept here, not duplicated,
because they are the vocabulary ``decide`` is defined in terms of).

Disclosed interpretation decisions (brief-resolved ambiguities; the auditor
reads this list -- see also ``evidence/DESIGN-NOTES.md`` for the full set
including the ones that live outside this module):

1. **Selector unreadable -> blocked_probe_unavailable, not refuse.** D1
   defines "absent" as a value, not what happens when the read itself fails
   (wrong registry type, or a string that isn't exactly native/wsl/absent).
   Spec D3's table has no explicit row for "selector unreadable"; this
   module treats it the same as every other probe-can't-be-trusted case: a
   non-authorizing, bounded retry (10s), never a permanent both-stopped
   deadlock -- consistent with D3's own closing sentence ("an *error* is
   never escalated into a permanent both-stopped deadlock").
2. **A3 error -> blocked_probe_unavailable, not refuse.** A3 "error"
   (mutex API failure, distinct from "denied") means the guard cannot
   arbitrate ownership at all, so it cannot prove exclusivity either way --
   non-authorizing per the same D3 principle, not a conflict signal.
3. **A1 run_entry error under selector=native is ignored, not degraded.**
   The Run-entry sub-signal is a PRESENCE-only signal (D2: it only counts
   combined with selector != native). Under selector=native it is already
   disregarded when positive, so a failed *read* of it carries no
   information the decision needs -- only ``a1.live_process`` errors trigger
   ``start_degraded``. Silently degrading on a signal the table never
   consults would be theater, not caution.
4. **Mid-operation refusal state mapping (blocked_wsl_active).** ``decide``
   itself never sets ``state_name="blocked_wsl_active"`` -- that is a
   :class:`GuardMonitor`-level relabeling applied only when a non-start
   decision follows a start (D5's controlled-stop path), including the
   selector-flips-to-wsl-mid-operation case (AC7). See ``GuardMonitor``'s
   docstring.
5. **An EXPLICIT selector is the authority basis; an unreadable A2 under it
   degrades rather than blocks (chain I, owner-decided 2026-08-01).** D3's
   row 4 originally read "| native | A2 unreadable/timeout |
   NON-AUTHORIZING: blocked_probe_unavailable". Its stated justification is
   "A2 is the WSL-transmitter lifetime proof; transmission never starts on
   its absence" -- but that reasoning is what the SELECTOR exists to settle.
   The guard's own blocked message says what is missing: "cannot establish
   the authority basis for a native start". A validly-read
   ``ActiveRuntime=native`` -- exact match, REG_SZ, machine-global,
   admin-writable only (D1), and since chain G actually WRITTEN by the
   native installer -- IS that authority basis. So in exactly one cell
   (selector explicitly ``"native"`` AND A2 unreadable AND nothing else
   blocking) the start proceeds as ``start_degraded``, naming A2, and the
   unreadability is logged as a structured warning by the supervisor
   (``supervisor.core._log_guard_degraded``) instead of withholding the
   station. This reuses D3 row 3's EXISTING degraded vocabulary
   ("start + log ``probe-degraded``, re-probe per D5") rather than inventing
   a new one, and D5's continuous enforcement is untouched: ``start_degraded``
   is in ``_STARTED_ACTIONS``, so the monitor keeps re-probing every interval
   and a later POSITIVE A2 still triggers the controlled stop.

   What does NOT move (each is pinned by its own falsification test):

   * selector ``"absent"`` (even with a CONFIRMED-False WSL install, which
     reaches the same native path) -- no selector was ever written, so there
     is no authority artifact to stand on: still ``blocked_probe_unavailable``.
   * selector unreadable (step 2) and selector ``"wsl"`` (step 3).
   * step 4's ``absent`` + ``wsl_install_detected is None``.
   * a READABLE A2 ``positive`` under ``native`` (step 6) -- a real conflict,
     not probe noise: still ``refuse``.
   * ``a3 == "acquired_abandoned"`` + A2 unreadable (step 7) -- D4's MANDATORY
     abandoned-mutex re-verify. An abandoned mutex is affirmative evidence
     that a prior owner died holding it, which is not probe noise either;
     that row is deliberately left blocking and is NOT part of this cell.
   * A3 ``denied``/``error`` and the interlock rows.

   Exactly 18 of the exhaustive table's 3888 points change; a differential
   test against a pre-chain-I oracle asserts the set of changed points is
   EXACTLY that cell (``tests/native/test_guard_table.py``).

Cross-spec note: ``blocked_probe_unavailable`` is mandated by this spec's
D3/AC9 but is absent from ``spec-supervisor.md`` D6's state enumeration --
surfaced here explicitly, not silently resolved; ws5's supervisor spec needs
to account for it when it lands.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from civiccast.native.models import (
    A1Result,
    A2Result,
    A3Result,
    GuardAction,
    GuardDecision,
    GuardInputs,
    InterlockRead,
    SelectorRead,
)
from civiccast.native.service_identity import object_dacl

# ---------------------------------------------------------------------------
# Registry / serialization contracts (WS4-owned; win_probes.py and
# runtime_cli.py import these rather than redeclaring them).
# ---------------------------------------------------------------------------

SELECTOR_KEY = r"SOFTWARE\CivicCast"
SELECTOR_VALUE_NAME = "ActiveRuntime"

MAINTENANCE_KEY = r"SOFTWARE\CivicCast"
MAINTENANCE_VALUE_NAME = "Maintenance"

MUTEX_NAME = r"Global\CivicCastRuntimeOwner"
# Protected DACL: SYSTEM + BUILTIN\Administrators GENERIC_ALL, nobody else
# (spec D4) -- an unprivileged local process must be DENIED acquisition.
MUTEX_SDDL = object_dacl()

# From the SHIPPING WSL product (verified in-repo):
#   civiccast/apps/installer/src-tauri/src/main.rs:26,676-684 (spawn_civiccast_wsl_keepalive)
#   civiccast/apps/installer/src-tauri/resources/headless-bootstrap.ps1:1531-1536 (Ensure-RuntimeHostAutostart)
KEEPER_WSL_ARGV_MARKERS = (
    "--distribution",
    "CivicCast-Ubuntu-24.04",
    "--exec",
    "/usr/bin/sleep",
    "infinity",
)
RUNTIME_HOST_FLAG = "--civiccast-runtime-host"
RUN_VALUE_NAME = "CivicCast Autostart"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

WSL_DISTRO_NAME = "CivicCast-Ubuntu-24.04"
A2_TIMEOUT_SECONDS = 5.0

BLOCKED_RETRY_SECONDS = 10
ALERT_AFTER_CONSECUTIVE_BLOCKED = 3
MONITOR_DEFAULT_INTERVAL_SECONDS = 30.0

_STARTED_ACTIONS: frozenset[GuardAction] = frozenset({"start", "start_degraded"})


# ---------------------------------------------------------------------------
# decide()
# ---------------------------------------------------------------------------


def _blocked(message: str, *, named_probe: str | None = None) -> GuardDecision:
    return GuardDecision(
        action="blocked_probe_unavailable",
        named_probe=named_probe,
        message=message,
        retry_seconds=BLOCKED_RETRY_SECONDS,
        state_name="blocked_probe_unavailable",
    )


def _refuse(message: str, *, named_probe: str | None) -> GuardDecision:
    return GuardDecision(
        action="refuse",
        named_probe=named_probe,
        message=message,
        retry_seconds=None,
        state_name=None,
    )


def decide(inputs: GuardInputs) -> GuardDecision:
    """The D3 decision table, encoded as ordered precedence -- each step
    returns; no fallthrough past the first row that applies. Total over the
    input product (see tests/native/test_guard_table.py's P1 property).

    1. interlock held -> refuse, named_probe="interlock" (D7a: every start
       path honors it). interlock unreadable -> blocked_probe_unavailable
       (fail-closed: "a transmitter that can't check permission doesn't
       transmit" -- D4's phrase, applied here as a disclosed extension).
    2. selector unreadable -> blocked_probe_unavailable (disclosed
       interpretation #1 above; a non-authorizing, bounded retry, never a
       permanent deadlock).
    3. selector "wsl" -> never_start.
    4. selector "absent": wsl_install_detected is a TRI-STATE (F1 fix) --
       True -> refuse_instruct (set selector or run cutover); False ->
       continue as native (D1); None (install-detection probe itself could
       not determine an answer) -> blocked_probe_unavailable (cannot
       establish the authority basis for a native start; non-authorizing,
       bounded retry -- the fix for a fail-open bug where None, being falsy
       in Python, used to silently fall through to "continue as native").
    5. A1 composition rule (D2): effective A1 positive = (a1.live_process
       positive) OR (a1.run_entry positive AND selector was not read as
       "native"). Effective positive -> refuse, named_probe="A1".
    6. a2 positive -> refuse, named_probe="A2".
    7. a3 denied -> refuse, named_probe="A3" (mutex held by the other side).
       a3 error -> blocked_probe_unavailable (disclosed interpretation #2).
       a3 acquired_abandoned -> mandatory A2 re-verify (D4): a2 unreadable
       -> blocked_probe_unavailable; otherwise (a2 positive is already
       excluded by step 6 above, so only "negative" remains) -> continue.
    8. a2 unreadable -> blocked_probe_unavailable, UNLESS the selector was
       read as an EXPLICIT "native" (disclosed interpretation #5, chain I),
       in which case the unreadability is collected as a DEGRADATION and the
       start proceeds. "absent-as-native" deliberately does NOT qualify: no
       selector was ever written, so there is no authority artifact.
    9. a1.live_process error -> degradation (a2 is readable-negative here by
       construction UNLESS step 8 already degraded on it).
       a1.run_entry error under selector=native is deliberately NOT checked
       here (disclosed interpretation #3).
    10. any degradation collected in steps 8-9 -> start_degraded; else start.
    """

    # Step 1: D7a maintenance/freeze interlock -- every start path honors it.
    if inputs.interlock.status == "held":
        return _refuse(
            f"Maintenance interlock held ({inputs.interlock.detail}); refusing to start.",
            named_probe="interlock",
        )
    if inputs.interlock.status == "unreadable":
        return _blocked(
            f"Maintenance interlock unreadable ({inputs.interlock.detail}); "
            "a transmitter that can't check permission doesn't transmit.",
            named_probe="interlock",
        )

    # Step 2: selector read failure.
    if not inputs.selector.ok:
        return _blocked(
            f"Selector (ActiveRuntime) unreadable ({inputs.selector.detail}); "
            "non-authorizing, bounded retry.",
            named_probe="selector",
        )

    # Step 3: selector says wsl.
    if inputs.selector.value == "wsl":
        return GuardDecision(
            action="never_start",
            named_probe=None,
            message="selector=wsl; native must never start while the WSL runtime is authoritative.",
            retry_seconds=None,
            state_name=None,
        )

    # Step 4: selector absent -- F1 fix: wsl_install_detected is now a
    # TRI-STATE (True/False/None). The pre-fix bug treated None (the
    # install-detection probe itself failing) as falsy in `if ... and
    # wsl_install_detected:`, silently falling through to "continue as
    # native" -- exactly the fail-open collapse this branch now closes.
    if inputs.selector.value == "absent":
        if inputs.wsl_install_detected is None:
            return _blocked(
                "selector absent and WSL install state is unknown (the install-detection "
                "probe could not determine an answer); cannot establish the authority basis "
                "for a native start.",
                named_probe="wsl_install_detected",
            )
        if inputs.wsl_install_detected:
            return GuardDecision(
                action="refuse_instruct",
                named_probe=None,
                message=(
                    "selector absent and a CivicCast WSL install is detected; "
                    "set ActiveRuntime or run `civiccast-runtime cutover-to-native` first."
                ),
                retry_seconds=None,
                state_name=None,
            )
        # wsl_install_detected is a CONFIRMED False here (D1): selector
        # "absent" + no WSL install detected -> continue as native (falls
        # through, same as selector "native").

    selector_is_native = inputs.selector.value == "native"

    # Step 5: A1 composition rule (D2).
    live_positive = inputs.a1.live_process == "positive"
    run_positive_not_native = inputs.a1.run_entry == "positive" and not selector_is_native
    if live_positive or run_positive_not_native:
        sub_signal = (
            "live keeper process" if live_positive else "keeper Run entry (selector != native)"
        )
        return _refuse(f"A1 activity detected ({sub_signal}): {inputs.a1.detail}", named_probe="A1")

    # Step 6: A2 positive.
    if inputs.a2.status == "positive":
        return _refuse(
            f"A2 in-distro service activity detected: {inputs.a2.detail}", named_probe="A2"
        )

    # Step 7: A3.
    if inputs.a3.status == "denied":
        return _refuse(
            f"A3 mutex denied (other side owns it): {inputs.a3.detail}", named_probe="A3"
        )
    if inputs.a3.status == "error":
        return _blocked(f"A3 mutex could not be evaluated: {inputs.a3.detail}", named_probe="A3")
    if inputs.a3.status == "acquired_abandoned" and inputs.a2.status == "unreadable":
        return _blocked(
            "A3 mutex was abandoned by its prior owner and A2 re-verification "
            f"could not confirm the service is inactive: {inputs.a2.detail}",
            named_probe="A2",
        )
    # a3 == "acquired_abandoned" with a2 != "unreadable" falls through here:
    # a2.status can only be "negative" at this point (positive was already
    # excluded at step 6) -- D4's mandatory re-verify passes; continue.

    # Degradations collected by steps 8-9 and resolved together at step 10, so
    # a station that is degraded on BOTH probes reports both rather than
    # whichever one happened to be checked first.
    degraded_probes: list[str] = []
    degraded_reasons: list[str] = []

    # Step 8: A2 unreadable.
    #
    # Chain I (disclosed interpretation #5): an EXPLICIT, validly-read
    # ActiveRuntime="native" is the authority basis for a native start -- the
    # very thing the blocked message says is missing -- so an A2 the guard
    # merely could not READ degrades the start instead of withholding the
    # station. Every other selector state keeps the original fail-closed row,
    # `selector_is_native` is exact-match only (`absent`-as-native is False
    # here by construction), and a READABLE A2 positive already refused at
    # step 6, so this can never authorize a start over a detected live WSL
    # transmitter.
    if inputs.a2.status == "unreadable":
        if not selector_is_native:
            return _blocked(
                f"A2 in-distro service status unreadable: {inputs.a2.detail}; "
                "transmission never starts on its absence.",
                named_probe="A2",
            )
        degraded_probes.append("A2")
        degraded_reasons.append(
            f"A2 in-distro service status unreadable ({inputs.a2.detail}); the explicitly "
            "written selector ActiveRuntime=native is the authority basis for this start"
        )

    # Step 9: probe-level errors on RELEVANT A1 sub-signals -> degraded start.
    # live_process errors always matter. run_entry errors matter exactly where
    # a run_entry POSITIVE would have mattered (D2: selector != native) --
    # under selector=native the Run entry is presence-only and stays ignored
    # (disclosed interpretation #3); under absent-as-native it is a live
    # refusal signal, so a failed scan degrades rather than silently
    # vanishing (round-2 re-verify panel, Major).
    run_entry_error_relevant = inputs.a1.run_entry == "error" and not selector_is_native
    if inputs.a1.live_process == "error" or run_entry_error_relevant:
        failed_scan = (
            "live-process scan"
            if inputs.a1.live_process == "error"
            else "Run-entry scan (selector != native)"
        )
        degraded_probes.append("A1")
        degraded_reasons.append(f"A1 {failed_scan} failed ({inputs.a1.detail})")

    # Step 10: start, degraded if any probe could not be trusted.
    if degraded_probes:
        return GuardDecision(
            action="start_degraded",
            named_probe="+".join(degraded_probes),
            message=(
                f"probe-degraded: {'; '.join(degraded_reasons)}; starting with reduced "
                "confidence, re-probe per D5."
            ),
            retry_seconds=None,
            state_name=None,
        )
    return GuardDecision(
        action="start",
        named_probe=None,
        message="All probes clear; starting.",
        retry_seconds=None,
        state_name=None,
    )


# ---------------------------------------------------------------------------
# GuardMonitor
# ---------------------------------------------------------------------------


class GuardMonitorStatus(BaseModel):
    """Supervisor-visible status of a running :class:`GuardMonitor`."""

    model_config = ConfigDict(extra="forbid")

    last_decision: GuardDecision | None = None
    last_evaluated_utc: str | None = None
    consecutive_blocked_probe_unavailable: int = 0
    alert: bool = False


class StopEventLike(Protocol):
    """Structural type for the loop's stop signal -- ``threading.Event``
    satisfies this; tests use lightweight fakes for deterministic, sleep-free
    "N synthetic intervals" runs."""

    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


SelectorReaderFn = Callable[[], SelectorRead]
InterlockReaderFn = Callable[[], InterlockRead]
A1ProbeFn = Callable[[], A1Result]
A2ProbeFn = Callable[[], A2Result]
A3ProbeFn = Callable[[], A3Result]
WslInstallDetectorFn = Callable[[], bool | None]
ClockFn = Callable[[], datetime]
OnStateChangeFn = Callable[[GuardDecision], None]


def _mid_operation_decision(decision: GuardDecision) -> GuardDecision:
    """D5's controlled-stop relabeling: a non-start decision that follows a
    start gets state_name="blocked_wsl_active" for the callback, UNLESS it
    is already blocked_probe_unavailable (which carries its own state_name).
    This is the mapping disclosed interpretation #4 in this module's
    docstring refers to."""

    if decision.action == "blocked_probe_unavailable":
        return decision
    return decision.model_copy(update={"state_name": "blocked_wsl_active"})


class GuardMonitor:
    """Continuous D5 enforcement: a library class ws5's supervisor consumes.

    NO supervisor lives here -- this class only decides and reports; it does
    not spawn, stop, or own any child process. Pure-python and fully
    unit-testable with fake probes on any OS (no Windows-only imports).

    Every probe is an injected zero-argument callable so the pure guard core
    stays testable without touching the registry, process table, or
    ``wsl.exe`` -- see ``win_probes.py`` for the real Windows-backed
    callables ws5 wires in.

    CC-WS4-004 fix (round 2, Major -- auditor panel): the real ``mutex``
    callable MUST be a single ``win_probes.RuntimeOwnerMutex`` instance's
    ``.probe`` bound method, NEVER ``.acquire`` directly. ``GuardMonitor``
    HOLDS that one mutex instance's ownership across its own lifetime
    (construct one ``RuntimeOwnerMutex``, call ``mutex.acquire()`` or
    equivalently let the first ``probe()`` do it, then wire
    ``mutex=mutex.probe`` here) -- ``.probe()`` confirms continued
    self-ownership on every subsequent evaluation WITHOUT reopening or
    re-waiting the kernel object. Wiring ``.acquire`` directly re-opens the
    object every 30s even when this process already owns it; an unelevated
    holder cannot reopen the production-DACL object it already owns
    (CreateMutex's second-open DACL check), so every evaluation after the
    first spuriously observes a denial and the monitor controlled-stops
    itself within one interval of starting. A genuinely LOST mutex (another
    process somehow acquired it) still surfaces through ``probe()`` as a
    real ``refuse``, which is correct -- ``probe()`` only skips the reopen
    when THIS instance still holds a live handle.
    """

    def __init__(
        self,
        *,
        selector_reader: SelectorReaderFn,
        a1_probe: A1ProbeFn,
        a2_probe: A2ProbeFn,
        mutex: A3ProbeFn,
        interlock_reader: InterlockReaderFn,
        wsl_install_detector: WslInstallDetectorFn,
        clock: ClockFn,
        interval_seconds: float = MONITOR_DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._selector_reader = selector_reader
        self._a1_probe = a1_probe
        self._a2_probe = a2_probe
        self._mutex = mutex
        self._interlock_reader = interlock_reader
        self._wsl_install_detector = wsl_install_detector
        self._clock = clock
        self.interval_seconds = interval_seconds
        self.status = GuardMonitorStatus()
        self.logs: list[str] = []
        self._last_action: GuardAction | None = None

    def _compose_inputs(self) -> GuardInputs:
        """F6 fix: EACH injected probe call is individually wrapped -- a
        raise from any one of them must never propagate out of this method
        (and therefore never out of evaluate_once/run), which is D5's
        continuous-enforcement guarantee: a single flaky probe cannot kill
        the monitor loop. Every raise maps to that probe's own
        non-authorizing result shape (unreadable/error/None as
        appropriate), carrying the exception text in the detail so it is
        still visible in status/logs -- decide() then applies its normal,
        already-tested precedence to whatever non-authorizing state comes
        out (e.g. an A1 raise still degrades per D3 row 9, it does not
        necessarily become blocked_probe_unavailable)."""

        try:
            selector = self._selector_reader()
        except Exception as exc:
            selector = SelectorRead(ok=False, value=None, detail=f"selector probe raised: {exc!r}")

        try:
            wsl_install_detected = self._wsl_install_detector()
        except Exception:
            wsl_install_detected = None

        try:
            a1 = self._a1_probe()
        except Exception as exc:
            a1 = A1Result(
                live_process="error", run_entry="error", detail=f"A1 probe raised: {exc!r}"
            )

        try:
            a2 = self._a2_probe()
        except Exception as exc:
            a2 = A2Result(status="unreadable", detail=f"A2 probe raised: {exc!r}")

        try:
            a3 = self._mutex()
        except Exception as exc:
            a3 = A3Result(status="error", detail=f"A3 probe raised: {exc!r}")

        try:
            interlock = self._interlock_reader()
        except Exception as exc:
            interlock = InterlockRead(
                status="unreadable", record=None, detail=f"interlock probe raised: {exc!r}"
            )

        return GuardInputs(
            selector=selector,
            wsl_install_detected=wsl_install_detected,
            a1=a1,
            a2=a2,
            a3=a3,
            interlock=interlock,
        )

    def _record(self, decision: GuardDecision) -> None:
        timestamp = self._clock().isoformat()
        self.status.last_decision = decision
        self.status.last_evaluated_utc = timestamp
        if decision.action == "blocked_probe_unavailable":
            self.status.consecutive_blocked_probe_unavailable += 1
        else:
            self.status.consecutive_blocked_probe_unavailable = 0
        self.status.alert = (
            self.status.consecutive_blocked_probe_unavailable >= ALERT_AFTER_CONSECUTIVE_BLOCKED
        )
        self.logs.append(
            f"{timestamp} action={decision.action} named_probe={decision.named_probe} "
            f"state_name={decision.state_name} message={decision.message}"
        )
        if self.status.alert:
            self.logs.append(
                f"{timestamp} ALERT: {self.status.consecutive_blocked_probe_unavailable} "
                "consecutive blocked_probe_unavailable results"
            )

    def evaluate_once(self) -> GuardDecision:
        """Compose GuardInputs from the injected probes, call decide(), log
        the transition, and update self.status."""

        decision = decide(self._compose_inputs())
        self._record(decision)
        self._last_action = decision.action
        return decision

    def pre_child_start(self) -> GuardDecision:
        """Same evaluation as evaluate_once -- the D5 pre-start hook the
        supervisor calls before every (re)start of a transmission child."""

        return self.evaluate_once()

    def run(self, stop_event: StopEventLike, on_state_change: OnStateChangeFn) -> None:
        """Evaluate every ``interval_seconds`` until ``stop_event`` is set.

        On a non-start decision immediately following a start (D5's
        controlled stop -- covers both a fresh positive activity signal
        (AC3) and a mid-operation selector flip to "wsl" (AC7), since both
        simply produce a non-start ``decide()`` result), fires
        ``on_state_change`` with the state relabeled per
        ``_mid_operation_decision`` -- within one evaluation of the
        transition, i.e. one interval.
        """

        while not stop_event.is_set():
            was_started = self._last_action in _STARTED_ACTIONS
            decision = self.evaluate_once()
            is_started = decision.action in _STARTED_ACTIONS
            if was_started and not is_started:
                on_state_change(_mid_operation_decision(decision))
            if stop_event.wait(self.interval_seconds):
                break


__all__ = [
    "A2_TIMEOUT_SECONDS",
    "ALERT_AFTER_CONSECUTIVE_BLOCKED",
    "BLOCKED_RETRY_SECONDS",
    "KEEPER_WSL_ARGV_MARKERS",
    "MAINTENANCE_KEY",
    "MAINTENANCE_VALUE_NAME",
    "MONITOR_DEFAULT_INTERVAL_SECONDS",
    "MUTEX_NAME",
    "MUTEX_SDDL",
    "RUNTIME_HOST_FLAG",
    "RUN_KEY_PATH",
    "RUN_VALUE_NAME",
    "SELECTOR_KEY",
    "SELECTOR_VALUE_NAME",
    "WSL_DISTRO_NAME",
    "GuardMonitor",
    "GuardMonitorStatus",
    "StopEventLike",
    "decide",
]
