# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The Supervisor -- the orchestration core that drives the states.py machine
from real events (child readiness, guard decisions, the maintenance interlock,
a restart storm, a graceful stop). This is the highest-risk module of the WS5
slice, so it is built to the same discipline as the pure wave-1 modules it
consumes: EVERY side-effecting dependency is an injected seam, so the whole
control flow runs on Linux in CI with fakes and touches no subprocess, no
socket, and no Windows import.

What this module OWNS (design.md Sec.3 ``core.py`` + the ratification addendum):

* **The states.py machine.** ``_dispatch`` is the single place a
  ``SupervisorEvent`` is turned into a new ``SupervisorState`` via the pure
  ``supervisor_transition``; nothing else in the class mutates ``self._state``.
* **Guard integration (WS4 D9 + RAT-002).** ``pre_child_start`` gates every
  (re)start of the writer-capable control-plane child. On the maintenance
  interlock's held->freed edge, ``on_interlock_freed`` performs EXACTLY ONE
  synchronous ``GuardMonitor`` evaluation and emits the composite event that
  matches ``GuardDecision.state_name`` -- ``None`` -> ``interlock_freed_clear``;
  ``"blocked_wsl_active"`` -> ``interlock_freed_blocked_wsl``;
  ``"blocked_probe_unavailable"`` -> ``interlock_freed_blocked_probe`` -- and
  advances no writer-capable child before that evaluation (RAT-002).
* **Maintenance (RAT-001).** ``enter_maintenance`` drives ``interlock_held`` ->
  ``maintenance`` and launches the control-plane child in maintenance mode (env
  ``CIVICCAST_SUPERVISOR_MODE=maintenance`` + ``CIVICCAST_SUPERVISOR_MODE_CONTRACT=1``,
  set by ``children.control_plane_child_spec``). The maintenance-ready gate is
  satisfied ONLY when ``children.check_control_plane_maintenance_ready`` passes;
  otherwise the supervisor STAYS in maintenance (fail-closed).
* **Startup order / readiness (D5/D6).** ``start`` brings up postgres ->
  control plane in ``STARTUP_ORDER``, each gated by ``restart_eligible`` and
  polled ready via ``children.poll_until_ready``. A child that leaves ready is a
  controlled restart eligible only after its dependency re-enters ready. NATS
  JetStream was removed from the product (owner decision 2026-08-20, ADR
  0023) and was never a supervised child of its own -- ``STARTUP_ORDER`` is
  ``(postgres, control_plane)``.
* **Restart storm (D5).** ``evaluate_restart_storm`` demotes to ``degraded`` and
  fires an alert through the injected outbox seam.
* **Drain-all (RAT-004).** ``graceful_stop`` drives ``stop`` -> ``stopping`` and
  sends ``CTRL_BREAK`` to the control-plane process group so its uvicorn
  lifespan runs the daemon's ``stop_all_channels`` drain; the Job Object is the
  backstop only after the deadline.
* **Job Object containment (D3).** Direct children are assigned to the job via
  ``job_object.JobObjectController``; a straggler sweep runs before the first
  spawn.
* **Status snapshot** for the read tier of ``pipe_server`` (``status`` /
  ``version``).

What this module does NOT own (disclosed seams, wired at the service layer):

* The REAL child process I/O (spawn/terminate/CTRL_BREAK) is the injected
  ``ChildProcessRunner``; the real Win32 Job Object and named pipe are covered
  by the wave-1 ``*_win.py`` suites.
* The concrete alerting transport. ``core.py`` fires an abstract alert through
  the injected ``AlertOutbox`` seam; binding it to
  ``civiccast.alerting.store.record_alert_condition`` (and picking the
  ``AlertConditionKind``) is ``service.py``'s job -- that transport needs a live
  SQLAlchemy ``Session`` and cannot run in this pure CI context. See the
  DISCLOSED GAP note in the returned evidence.
"""

from __future__ import annotations

import contextlib
import logging
import random
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from civiccast.native.models import GuardDecision, InterlockStatus
from civiccast.native.runtime_guard import GuardMonitorStatus
from civiccast.native.supervisor.children import (
    AbortFn,
    AliveFn,
    ChildSpec,
    ControlPlaneHealthProbe,
    OllamaChildDecision,
    ProgressFn,
    ReadinessResult,
    backoff_with_jitter,
    check_control_plane_maintenance_ready,
    check_control_plane_ready,
    check_ollama_ready,
    check_postgres_ready,
    control_plane_child_spec,
    default_egress_work_dir,
    poll_until_ready,
    postgres_child_spec,
    read_postmaster_pid,
    restart_storm_check,
)
from civiccast.native.supervisor.config import STARTUP_ORDER, SupervisorConfig
from civiccast.native.supervisor.job_object import (
    JobObjectApi,
    JobObjectController,
    SweepOutcome,
    sweep_stragglers,
)
from civiccast.native.supervisor.states import (
    ChildState,
    SupervisorEvent,
    SupervisorState,
    restart_eligible,
    supervisor_transition,
    workers_permitted,
)

SUPERVISOR_VERSION = "ws5"
SUPERVISOR_PROTOCOL_VERSION = 1

# G2 (observability): core.py had ZERO logging before this -- run 17's
# never-attempted-child wedge (G1) was undiagnosable from logs alone and took
# a from-scratch repro script to prove. Each of the three call sites below
# emits WARNING exactly ONCE PER DISTINCT REASON (the last-logged reason is
# latched on the Supervisor instance; an unchanged reason on a later tick is
# NOT re-logged), so a stuck child gets one clear, persistent WARNING instead
# of either total silence or a 1 Hz log flood.
_LOGGER = logging.getLogger(__name__)

# The guard verdicts that AUTHORIZE a (writer-capable) child to start.
# ``start`` and ``start_degraded`` both authorize; every other action (refuse /
# blocked_probe_unavailable / never_start / refuse_instruct) withholds the
# start (D5/D9 pre-start gate).
_GUARD_START_ACTIONS: frozenset[str] = frozenset({"start", "start_degraded"})

# The one writer-capable direct child. postgres is infrastructure; only
# the control plane owns the media-worker/transmission surfaces, so only its
# NORMAL-mode start is gated by the runtime guard. The maintenance-mode control
# plane is read-only (workers never start there), so it is NOT writer-capable.
_WRITER_CAPABLE_CHILD = "control_plane"

# Task #57 D2: the OPTIONAL third child -- the local-AI runtime. Deliberately
# NOT in config.STARTUP_ORDER: it has no D6 dependency edge in either
# direction (postgres/control_plane neither need it nor feed it), and the
# ``children_ready`` gate must never wait on a child that may be legitimately
# skipped (binary or staged store absent -> degraded AI, service healthy).
_OLLAMA_CHILD = "ollama"


# ---------------------------------------------------------------------------
# Injected seams (Protocols) -- every side effect enters through one of these,
# which is what keeps the orchestration pure and CI-testable on any OS.
# ---------------------------------------------------------------------------


class ChildHandle(Protocol):
    """A live child process handle. Callers only ever read its ``pid`` and pass
    it back into the runner; the concrete handle type is the runner's."""

    @property
    def pid(self) -> int: ...


class ChildProcessRunner(Protocol):
    """The real child process I/O, behind an injectable seam. The production
    implementation (``service.py``) spawns with ``CREATE_NEW_PROCESS_GROUP`` for
    the control plane and sends ``CTRL_BREAK_EVENT`` to its group; tests inject a
    fake that records calls and never touches a real process."""

    def spawn(self, spec: ChildSpec) -> ChildHandle: ...
    def is_alive(self, handle: ChildHandle) -> bool: ...

    def cpu_seconds(self, handle: ChildHandle) -> float | None:
        """U31: the child's cumulative CPU seconds, or ``None`` when it cannot
        be sampled. The readiness poll's no-progress window is the only
        consumer: a sample that has not advanced for a whole window is what
        separates a WEDGED child from a slow one, so the seam lives here, on
        the runner that owns the OS handle, rather than reading the process
        table from the orchestrator. ``None`` means "no evidence" and is never
        read as a stall -- a fake handle with no process behind it returns
        ``None`` and keeps the poll on its plain wall-clock budget."""
        ...

    def send_ctrl_break(self, handle: ChildHandle) -> None: ...
    def terminate(self, handle: ChildHandle) -> None: ...

    def graceful_stop(self, handle: ChildHandle) -> object:
        """Issue the child's OWN graceful-stop action for ``handle`` (the
        production runner resolves it from the handle's spec: ``pg_ctl stop -D
        <data_dir> -m fast`` for postgres, bounded by the stop-command runner's
        own timeout). Needed on the seam because the postmaster-containment
        rollback must ask postgres to tear itself down -- ``terminate`` alone
        kills only the postmaster pid and reaps none of its background
        workers."""
        ...

    def open_existing(self, pid: int) -> ChildHandle:
        """CC-WS5-003: open a DURABLE, monitor/terminate/job-assignable handle to
        an ALREADY-RUNNING process by pid -- the PostgreSQL postmaster, whose
        ``pg_ctl start -w`` launcher has self-exited. The production runner
        (``service.py``) opens it via ``win32api.OpenProcess`` with the same
        access rights the Job Object assign needs; tests inject a fake that
        hands back a distinct handle for that pid and records the call."""
        ...


class GuardLike(Protocol):
    """The subset of ``runtime_guard.GuardMonitor`` the supervisor consumes.
    ``GuardMonitor`` satisfies this structurally; tests inject a lightweight
    fake with a preset decision."""

    status: GuardMonitorStatus

    def pre_child_start(self) -> GuardDecision: ...
    def evaluate_once(self) -> GuardDecision: ...


class AlertOutbox(Protocol):
    """The alerting seam. ``core.py`` fires an abstract operational condition;
    the concrete binding to ``civiccast.alerting`` (which needs a DB session) is
    the service layer's responsibility -- see the module docstring."""

    def fire(self, *, summary: str, detail: str) -> None: ...


class StopEventLike(Protocol):
    """The subset of ``threading.Event`` the run loop consumes: a blocking
    ``wait(timeout)`` that returns True once the event is set. ``threading.Event``
    satisfies this structurally; the run-loop tests inject a deterministic fake
    so the whole loop runs with no real clock and no real thread."""

    def wait(self, timeout: float | None = ...) -> bool: ...


ClockFn = Callable[[], float]
SleepFn = Callable[[float], None]
InterlockReaderFn = Callable[[], InterlockStatus]
# CC-WS5-003: resolve the durable PostgreSQL postmaster's pid (from
# ``<data_dir>/postmaster.pid``). Returns ``None`` when unresolvable so the
# postgres start fails CLOSED rather than monitoring the self-exited launcher.
PostmasterPidReaderFn = Callable[[], int | None]
PostgresProbeFn = Callable[[], bool]
HealthProbeFn = Callable[[], ControlPlaneHealthProbe]
# Task #57 D2: the OPTIONAL ollama child's seams. The spec provider is
# evaluated at every (re)start attempt at the SERVICE layer (the only layer
# allowed to stat the installed tree) and returns either a launchable spec or
# a skip decision; the probe is the bounded GET /api/version readiness check.
OllamaSpecProviderFn = Callable[[], OllamaChildDecision]
OllamaProbeFn = Callable[[], bool]
# The D5 jitter RNG seam: returns a value in ``[0.0, 1.0)`` (``random.random``'s
# contract). Injected so the backoff schedule is deterministic under test.
RngFn = Callable[[], float]


def _never_abort() -> bool:
    """The default :data:`AbortFn`: no stop has been requested, ever. Keeps every
    caller that does not wire the F1 stop seam (tests, the upgrade tooling) on
    exactly the pre-F1 behaviour."""

    return False


def _cpu_progress_reader(runner: ChildProcessRunner, handle: ChildHandle) -> ProgressFn:
    """U31: bind one child handle to the no-argument progress sample
    :func:`~civiccast.native.supervisor.children.poll_until_ready` takes --
    that child's cumulative CPU seconds, resampled on every poll tick through
    the runner (the seam that owns the OS handle)."""

    return lambda: runner.cpu_seconds(handle)


def _child_liveness_reader(runner: ChildProcessRunner, handle: ChildHandle) -> AliveFn:
    """U31: bind one child handle to the no-argument liveness sample
    :func:`~civiccast.native.supervisor.children.poll_until_ready` takes, so a
    child that dies mid-poll ends the poll instead of holding the budget."""

    return lambda: runner.is_alive(handle)


# ---------------------------------------------------------------------------
# Result / status models
# ---------------------------------------------------------------------------

ChildStartStatus = Literal[
    "started_ready", "guard_blocked", "not_eligible", "not_ready", "skipped", "aborted"
]


class ChildStartOutcome(BaseModel):
    """The outcome of one child (re)start attempt. ``guard_blocked`` means the
    writer-capable child's pre-start guard verdict withheld the start (it was
    never spawned); ``not_eligible`` means a D6 predecessor was not ready;
    ``skipped`` is the OPTIONAL ollama child's clean degrade (task #57 D2:
    binary or staged store absent -- nothing spawned, service healthy).
    ``aborted`` (F1) means a service STOP was requested while this start was in
    flight: nothing about the child's health was learned, so it is NOT a
    failure -- it must not arm the D5 backoff, must not log a readiness
    WARNING, and must not move the child's state."""

    model_config = ConfigDict(extra="forbid")

    name: str
    status: ChildStartStatus
    pid: int | None = None
    detail: str = ""
    guard_decision: GuardDecision | None = None


class ChildStatusView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    state: ChildState
    pid: int | None = None
    # Audit A3: when this child's most recent (re)start was WITHHELD by the
    # runtime guard, the withhold detail -- so a guard-blocked station is
    # visible in the ``status`` read tier, not silently ``starting`` forever.
    # ``None`` once a start succeeds.
    blocked_detail: str | None = None


class StatusSnapshot(BaseModel):
    """The read-tier snapshot the control pipe's ``status`` command returns."""

    model_config = ConfigDict(extra="forbid")

    state: SupervisorState
    workers_permitted: bool
    children: list[ChildStatusView]
    guard_last_action: str | None
    guard_state_name: str | None
    guard_alert: bool
    restart_count_window: int
    protocol_version: int
    version: str


# ---------------------------------------------------------------------------
# Per-child restart backoff state (D5)
# ---------------------------------------------------------------------------


@dataclass
class _Backoff:
    """Per-child D5 restart-backoff state. ``attempt`` is the count of
    consecutive spawn-then-fail retries (0 whenever the child last reached
    ready); ``next_retry_epoch`` is the earliest ``tick`` ``now`` at which the
    next retry may run. A child at ``attempt == 0`` / ``next_retry_epoch == 0.0``
    is retryable immediately -- a fresh death, or a child that has never failed
    a spawn, is never delayed. Only a genuine spawn-then-fail advances the
    schedule; a guard- or dependency-withheld start (nothing was spawned) does
    NOT, so a transiently-withheld restart is retried freely on the next tick
    (F-REV-3) rather than being penalised by a backoff it never earned."""

    attempt: int = 0
    next_retry_epoch: float = 0.0


# ---------------------------------------------------------------------------
# The Supervisor
# ---------------------------------------------------------------------------


class Supervisor:
    """Owns the states.py machine and drives it from injected real-world events.

    Every dependency that would otherwise perform I/O -- the guard, the child
    runner, the Job Object API, the readiness probes, the clock/sleep pair, the
    alert outbox, and the interlock reader -- is injected, so the orchestration
    logic is exercised end to end with fakes on any OS.
    """

    def __init__(
        self,
        *,
        config: SupervisorConfig,
        guard: GuardLike,
        job_api: JobObjectApi,
        runner: ChildProcessRunner,
        alert_outbox: AlertOutbox,
        postgres_probe: PostgresProbeFn,
        health_probe: HealthProbeFn,
        clock: ClockFn,
        sleep: SleepFn,
        interlock_reader: InterlockReaderFn,
        should_abort: AbortFn | None = None,
        rng: RngFn = random.random,
        postmaster_pid_reader: PostmasterPidReaderFn | None = None,
        ollama_spec_provider: OllamaSpecProviderFn | None = None,
        ollama_probe: OllamaProbeFn | None = None,
        program_data_root: str | None = None,
        postgres_data_dir: str = "pgdata",
        pg_ctl_path: str = "pg_ctl",
        db_host: str = "127.0.0.1",
        db_port: int = 5432,
        python_path: str = "python",
        control_plane_env: Mapping[str, str] | None = None,
        control_plane_host: str = "127.0.0.1",
        control_plane_port: int = 8000,
    ) -> None:
        self._config = config
        self._guard = guard
        self._job_api = job_api
        self._job = JobObjectController(api=job_api)
        self._runner = runner
        self._alert = alert_outbox
        self._postgres_probe = postgres_probe
        self._health_probe = health_probe
        self._clock = clock
        self._sleep = sleep
        self._interlock_reader = interlock_reader
        # F1 (BLOCKER, 2026-07-31): "has a stop been requested?", checked between
        # children and inside every readiness poll. The service layer binds this
        # to the SAME ``threading.Event`` its run loop waits on
        # (``SupervisorService``), so ``request_stop`` is observable INSIDE a
        # start()/tick() iteration instead of only between iterations. Before
        # this, one tick could chain four readiness budgets (60+30+30+60s) with
        # the stop event already set, and the 150s stop watchdog fired
        # mid-chain -- force-exiting the host, closing the Job Object, and
        # hard-killing postgres into an unclean cluster.
        self._should_abort: AbortFn = should_abort or _never_abort
        # CC-WS5-016 (TESTER4 adversarial review of PR #389): the OTHER half of
        # the stop signal. ``_should_abort`` is bound to the service's
        # ``threading.Event``, which ONLY the SCM stop path sets
        # (``SupervisorService.request_stop``). The authorized control-pipe
        # ``stop``/``drain`` verb reaches ``graceful_stop()`` DIRECTLY through
        # ``AdminCommandRouter._drain`` and never touches that event, so the two
        # nominally equivalent stop mechanisms presented DIFFERENT abort signals
        # to reconciliation: an in-flight tick that had already passed the outer
        # state gate kept spawning children after the operator had moved the
        # machine to ``stopping``. ``_stop_intent`` is set by ``graceful_stop``
        # itself, so it is set no matter WHICH path asked, and
        # ``_abort_requested`` is the single signal every reconciliation path
        # consults. Terminal: ``stopping`` is the absorbing state, so this is
        # never cleared.
        self._stop_intent = False
        # Serializes the stop transition against the decide-and-spawn critical
        # section. A flag alone cannot close the race the review named: the
        # pipe-command thread can set stop and CLOSE the Job Object between a
        # reconciliation thread's abort check and its ``assign_child``, faulting
        # the run loop. Reentrant because the locked sections call injected
        # seams. Held only across state transitions, spawn+assign, and
        # ``_job.close()`` -- NEVER across a readiness poll, so a stop can never
        # queue behind a 60s budget.
        self._lifecycle_lock = threading.RLock()
        # Control-pipe commands run on their own worker thread while run()/tick()
        # reconcile children on the service thread. Serialize complete child
        # start/readiness sequences so a tick cannot replace another thread's
        # still-starting child.
        self._reconciliation_lock = threading.RLock()
        self._rng = rng
        # CC-WS5-003: the durable-postmaster pid resolver. Defaults to reading
        # ``<postgres_data_dir>/postmaster.pid`` -- the SAME data_dir the postgres
        # child spec launches with (``_spec_for("postgres")`` below), so the
        # pidfile read and the ``pg_ctl -D`` target can never diverge.
        self._postmaster_pid_reader: PostmasterPidReaderFn = (
            postmaster_pid_reader
            if postmaster_pid_reader is not None
            else (lambda: read_postmaster_pid(self._postgres_data_dir))
        )
        # Task #57 D2: the OPTIONAL ollama child. Both seams or neither -- a
        # provider without a readiness probe could only ever fail its poll
        # (fail loud at construction instead of a silent never-ready child).
        if (ollama_spec_provider is None) != (ollama_probe is None):
            raise ValueError("ollama_spec_provider and ollama_probe must be provided together")
        self._ollama_spec_provider = ollama_spec_provider
        self._ollama_probe = ollama_probe

        self._program_data_root = program_data_root
        self._postgres_data_dir = postgres_data_dir
        self._pg_ctl_path = pg_ctl_path
        self._db_host = db_host
        self._db_port = db_port
        self._python_path = python_path
        self._control_plane_env = dict(control_plane_env or {})
        self._cp_host = control_plane_host
        self._cp_port = control_plane_port

        self._state: SupervisorState = "starting"
        # G1 (BLOCKER, run 17): STARTUP_ORDER children boot into 'pending' --
        # never yet attempted this service run, and retry-eligible -- NOT
        # 'stopped' ('stopped' means deliberately not running and is exempt
        # from _needs_restart's automatic retry). A child start() never even
        # reaches (an earlier child missed its readiness budget) must still be
        # picked up by the tick loop's recovery path once its dependency is
        # ready; booting straight into 'stopped' made that child
        # indistinguishable from a deliberate stop and it was NEVER retried.
        # The optional ollama child boots into 'stopped' too, but it is NOT
        # exempt from retry the way a deliberate stop is: a skip is re-evaluated
        # on the throttled cadence in _recheck_skipped_optional_child (B5 field
        # defect, TESTER2 2026-08-12 -- the skip's prerequisites are acquired at
        # RUNTIME by the first-run download flow, so "absent at boot" is not
        # "absent forever"). The pairing of 'stopped' with a _start_blocked entry
        # is what marks it re-checkable.
        self._child_states: dict[str, ChildState] = dict.fromkeys(STARTUP_ORDER, "pending")
        if self._ollama_spec_provider is not None:
            self._child_states[_OLLAMA_CHILD] = "stopped"
        # Audit A3: per-child detail of a guard-WITHHELD start, surfaced in
        # status_snapshot (ChildStatusView.blocked_detail); cleared on the next
        # successful start of that child.
        self._start_blocked: dict[str, str] = {}
        self._handles: dict[str, ChildHandle] = {}
        self._backoff: dict[str, _Backoff] = {name: _Backoff() for name in STARTUP_ORDER}
        if self._ollama_spec_provider is not None:
            self._backoff[_OLLAMA_CHILD] = _Backoff()
        self._restart_epochs: list[float] = []
        self._prev_interlock: InterlockStatus | None = None
        self._swept = False
        # CC-WS5-001: the mode of the CURRENTLY-live control plane, so
        # maintenance reconciliation can tell a writer-capable (normal) CP that
        # must be replaced from a read-only maintenance CP that must be left
        # alone. None when no control-plane handle is live.
        self._cp_mode: Literal["normal", "maintenance"] | None = None
        # CC-WS5-002: the ``now`` of the last continuous dual-runtime guard
        # evaluation, so the in-tick guard check is throttled to
        # ``guard_interval_seconds``. -inf so the first serving tick always
        # evaluates.
        self._last_guard_check_epoch: float = float("-inf")
        # B5 field defect (TESTER2 2026-08-12): the ``now`` of the last
        # re-evaluation of a SKIPPED optional child's launch prerequisites, so
        # the re-check is throttled to ``optional_child_recheck_seconds``. -inf
        # so the first serving tick after a skip re-evaluates immediately (the
        # store may have landed DURING bring-up).
        self._last_optional_recheck_epoch: float = float("-inf")
        # G2: latches of the last-logged reason per warning site, so each is
        # emitted exactly once per DISTINCT reason (not once per tick). Keyed by
        # child name where the reason is per-child (the start()-bail and
        # not-ready-restart sites); a single slot for the guard-withheld site
        # since only the one writer-capable child is ever gated by it.
        self._last_logged_start_bail: str | None = None
        self._last_logged_guard_withheld: dict[str, str] = {}
        self._last_logged_restart_not_ready: dict[str, str] = {}

    # -- state accessors ---------------------------------------------------

    @property
    def state(self) -> SupervisorState:
        return self._state

    def workers_permitted(self) -> bool:
        """Whether media workers may run in the current state (states.py P2:
        False in maintenance / blocked / starting / stopping)."""

        return workers_permitted(self._state)

    def child_state(self, name: str) -> ChildState:
        return self._child_states[name]

    def handles(self) -> dict[str, ChildHandle]:
        """A copy of the live child handles (name -> handle)."""

        return dict(self._handles)

    def force_state(self, state: SupervisorState) -> None:
        """Set the machine state directly. Used by the service layer to
        re-hydrate the machine on crash recovery (and by tests to reach a
        precise starting state); NOT a transition, so it bypasses
        ``supervisor_transition`` deliberately."""

        self._state = state

    def _dispatch(self, event: SupervisorEvent) -> SupervisorState:
        """The single seam through which an event mutates the state -- every
        transition goes through the pure ``supervisor_transition``."""

        self._state = supervisor_transition(self._state, event)
        return self._state

    # -- child spec + readiness wiring ------------------------------------

    def _spec_for(self, name: str, *, maintenance: bool = False) -> ChildSpec:
        if name == "postgres":
            return postgres_child_spec(
                pg_ctl_path=self._pg_ctl_path,
                data_dir=self._postgres_data_dir,
                host=self._db_host,
                port=self._db_port,
            )
        if name == "control_plane":
            mode: Literal["normal", "maintenance"] = "maintenance" if maintenance else "normal"
            return control_plane_child_spec(
                python_path=self._python_path,
                host=self._cp_host,
                port=self._cp_port,
                mode=mode,
                egress_work_dir=default_egress_work_dir(program_data_root=self._program_data_root),
                extra_env=self._control_plane_env,
            )
        raise ValueError(f"unknown child {name!r}")

    def _readiness_check_for(
        self, name: str, *, maintenance: bool = False
    ) -> Callable[[], ReadinessResult]:
        if name == "postgres":
            return lambda: check_postgres_ready(self._postgres_probe)
        if name == "control_plane":
            if maintenance:
                return lambda: check_control_plane_maintenance_ready(self._health_probe)
            return lambda: check_control_plane_ready(self._health_probe)
        if name == _OLLAMA_CHILD:
            probe = self._ollama_probe
            assert probe is not None  # enforced pairwise in __init__
            return lambda: check_ollama_ready(probe)
        raise ValueError(f"unknown child {name!r}")

    @staticmethod
    def _is_writer_capable(name: str, maintenance: bool) -> bool:
        # Only the NORMAL-mode control plane transmits; the maintenance-mode
        # control plane is read-only and therefore not writer-capable.
        return name == _WRITER_CAPABLE_CHILD and not maintenance

    # -- Job Object containment (D3) --------------------------------------

    def _abort_requested(self) -> bool:
        """CC-WS5-016: the ONE stop signal every reconciliation path consults.

        True once EITHER stop mechanism has begun: the SCM path (which sets the
        service's stop event, read through the ``_should_abort`` seam) or the
        control-pipe ``stop``/``drain`` path (which calls ``graceful_stop``
        directly and sets ``_stop_intent``). Before this existed only the first
        was observable, so an authorized pipe drain could not stop reconciliation
        from spawning children underneath it."""

        return self._stop_intent or self._should_abort()

    def _sweep_once(self) -> SweepOutcome:
        """D3 straggler sweep before the first spawn of this run (idempotent per
        supervisor instance)."""

        outcome = sweep_stragglers(self._job_api)
        self._swept = True
        return outcome

    # -- single-child (re)start (D5/D6 + D9 guard gate) -------------------

    def start_child(self, name: str, *, maintenance: bool = False) -> ChildStartOutcome:
        """(Re)start one direct child: sweep-before-first-spawn (D3), D6
        dependency-eligibility, the D9 pre-start guard gate for the
        writer-capable child, spawn + Job Object assignment (D3), then a bounded
        readiness poll (D6). Pure over the injected seams. Start/readiness work
        shares a reentrant lock with service reconciliation and admin restarts."""

        with self._reconciliation_lock:
            return self._start_child(name, maintenance=maintenance)

    def _start_child(self, name: str, *, maintenance: bool = False) -> ChildStartOutcome:
        if not self._swept:
            self._sweep_once()

        if not restart_eligible(name, self._child_states, STARTUP_ORDER):
            return ChildStartOutcome(
                name=name,
                status="not_eligible",
                detail=f"{name}: a D6 predecessor is not ready; restart withheld",
            )

        if name == _OLLAMA_CHILD:
            # Task #57 D2: the OPTIONAL child's spec comes from the service
            # layer's provider, re-evaluated at every (re)start attempt. A skip
            # decision (binary/staged store absent) leaves the child cleanly
            # ``stopped``, records the reason for the status read tier, and
            # spawns nothing.
            #
            # B5 field defect (TESTER2 2026-08-12): that skip is NOT final for
            # the service run. It used to be -- the absence was documented as "a
            # durable install property, not a transient fault" -- and that
            # premise is false. Chain H1 has the FIRST-RUN acquisition flow
            # download the model store into ProgramData while the supervisor is
            # already running, so the prerequisites routinely appear MINUTES
            # after boot. TESTER2's station logged this skip once at service
            # registration (02:35 UTC, no store yet), staged a complete store at
            # 07:23 UTC, and then sat at 0 ollama processes / 0 listeners on
            # :11434 because ``_needs_restart`` exempts ``stopped`` from retry
            # and nothing ever called the provider again. An external reboot at
            # 07:32 -- a FRESH process re-running this very code path against
            # the SAME disk -- brought the child up immediately (ollama pid 7856
            # parented to supervisor pid 4444, listener 127.0.0.1:11434), which
            # is the control proving the disk was fine and only the in-process
            # latch was wrong. ``_recheck_skipped_optional_child`` now re-runs
            # this decision on a throttled cadence so no reboot is required.
            if self._ollama_spec_provider is None:
                raise ValueError("ollama child requested but no spec provider is configured")
            ollama_decision = self._ollama_spec_provider()
            if ollama_decision.spec is None:
                detail = f"ollama: skipped -- {ollama_decision.detail}"
                self._start_blocked[_OLLAMA_CHILD] = detail
                self._child_states[_OLLAMA_CHILD] = "stopped"
                return ChildStartOutcome(name=name, status="skipped", detail=detail)
            spec = ollama_decision.spec
        else:
            spec = self._spec_for(name, maintenance=maintenance)

        if self._is_writer_capable(name, maintenance):
            decision = self._guard.pre_child_start()
            if decision.action not in _GUARD_START_ACTIONS:
                detail = f"{name}: guard verdict {decision.action!r} withheld the start"
                # Audit A3: a guard-WITHHELD start must stay RETRYABLE on the
                # tick loop. Pre-set ``starting`` (mirroring the restart paths
                # in _guard_reconcile_while_blocked / _resume_to_serving) --
                # leaving the boot-time ``stopped`` here meant ``_needs_restart``
                # never retried it, so a guard block at bring-up wedged the
                # service RUNNING forever with a dead station. Nothing was
                # spawned, so the D5 backoff stays untouched (F-REV-3: a
                # withheld start is retried freely on the next eligible tick,
                # never penalised by a backoff it never earned). The withhold is
                # recorded for the status read tier (blocked_detail).
                self._start_blocked[name] = detail
                self._child_states[name] = "starting"
                self._log_guard_withheld(name, decision)
                return ChildStartOutcome(
                    name=name,
                    status="guard_blocked",
                    detail=detail,
                    guard_decision=decision,
                )
            if decision.action == "start_degraded":
                # Chain I: the guard authorized this start on an EXPLICIT
                # ActiveRuntime=native (or an A1 sub-signal it could not scan)
                # while one of its probes could not be read. The start is not
                # withheld, but the unreadability must not vanish either --
                # this WARNING is the operator-visible record of exactly which
                # probe was untrusted and why.
                self._log_guard_degraded(name, decision)

        # CC-WS5-016: the decide-and-spawn critical section. The abort re-check
        # and the spawn+assign are ATOMIC against graceful_stop's transition and
        # its _job.close(): without this, an operator stop arriving between the
        # check and the assign could close the Job Object underneath a live
        # spawn, faulting the run loop. Nothing here blocks -- the readiness poll
        # is deliberately left outside.
        with self._lifecycle_lock:
            if self._abort_requested():
                # A stop began while this start was being decided. Nothing has
                # been spawned, so there is nothing to stop and no backoff to
                # arm -- F1's ``aborted`` contract exactly.
                return ChildStartOutcome(
                    name=name,
                    status="aborted",
                    detail=f"{name}: a service stop was requested; start withheld before spawn",
                )
            handle = self._runner.spawn(spec)
            # The guard authorized this spawn (or the child is not
            # writer-capable): the child is no longer guard-withheld (audit A3
            # status visibility).
            self._start_blocked.pop(name, None)
            # G2: a spawn attempt is happening -- clear the guard-withheld latch
            # so a LATER withhold (even with an identical reason string) is
            # logged again as a fresh incident rather than staying silenced by a
            # stale latch from before this successful spawn.
            self._last_logged_guard_withheld.pop(name, None)
            self._handles[name] = handle
            self._child_states[name] = "starting"
            if name == _WRITER_CAPABLE_CHILD:
                # Track the mode of the live control plane (CC-WS5-001).
                # start_child only ever launches the control plane in NORMAL mode
                # (the read-only maintenance CP goes through
                # _start_control_plane_maintenance), but the branch stays honest
                # about the maintenance flag.
                self._cp_mode = "maintenance" if maintenance else "normal"
            # CC-WS5-003: postgres launches via ``pg_ctl start -D <data_dir> -w``
            # -- a SHORT-LIVED launcher that self-exits once the postmaster is
            # up. The DURABLE process to contain + monitor is the postmaster (its
            # pid lives in ``<data_dir>/postmaster.pid``), which is not known
            # until readiness. So postgres does NOT assign the launcher here; it
            # resolves, assigns, and SWAPS IN the postmaster AFTER the readiness
            # poll below. The control plane is a durable DIRECT child ->
            # keep assign-on-spawn. Inside the lock with the spawn (CC-WS5-016)
            # so spawn and containment are one indivisible step against
            # ``_job.close()``.
            if name != "postgres":
                self._job.assign_child(handle.pid)

        result = poll_until_ready(
            self._readiness_check_for(name, maintenance=maintenance),
            budget_seconds=spec.readiness_budget_seconds,
            clock=self._clock,
            sleep=self._sleep,
            should_abort=self._abort_requested,
            # U31: a spec that carries a stall window (only the control plane
            # does today) also gets the two seams that let the poll tell "slow"
            # from "wedged" -- its cumulative CPU time (which moves only when
            # the child's own threads run) and its liveness. A spec without a
            # window passes None for both and keeps the exact pre-U31 call.
            progress=(
                _cpu_progress_reader(self._runner, handle)
                if spec.readiness_stall_seconds is not None
                else None
            ),
            stall_seconds=spec.readiness_stall_seconds,
            liveness=(
                _child_liveness_reader(self._runner, handle)
                if spec.readiness_stall_seconds is not None
                else None
            ),
        )
        if result.outcome == "aborted":
            # F1: a stop was requested mid-poll. The child was SPAWNED and its
            # handle is already tracked above, so the service's graceful stop
            # chain will stop it; nothing was learned about its readiness, so
            # leave its state exactly where the spawn left it (``starting``) and
            # report ``aborted`` -- never ``not_ready``, which would arm a D5
            # backoff and log a readiness WARNING for a stop we asked for.
            return ChildStartOutcome(
                name=name, status="aborted", pid=handle.pid, detail=result.detail
            )
        if result.outcome != "ready":
            # Not ready within budget: the child stays non-ready (fail-closed); a
            # maintenance child in particular keeps the freeze held.
            self._child_states[name] = "failed"
            return ChildStartOutcome(
                name=name, status="not_ready", pid=handle.pid, detail=result.detail
            )

        if name == "postgres":
            # CC-WS5-003: readiness proved the postmaster is up. Resolve its real
            # pid, open a durable handle to IT, assign THAT to the Job Object, and
            # replace the launcher handle (which self-exits) with the postmaster
            # handle so liveness monitoring watches the durable process. If the
            # pidfile is unresolvable, FAIL CLOSED -- do NOT accept ready while
            # only the self-exiting launcher is contained (that is exactly the
            # unprovable-containment + relaunch-storm defect this fix closes).
            postmaster_pid = self._postmaster_pid_reader()
            if postmaster_pid is None:
                self._child_states["postgres"] = "failed"
                return ChildStartOutcome(
                    name=name,
                    status="not_ready",
                    pid=handle.pid,
                    detail=(
                        "postgres reported ready but postmaster.pid was unresolvable; "
                        "failing closed rather than monitoring the self-exited pg_ctl launcher"
                    ),
                )
            # CC-WS5-015: open + contain the postmaster INSIDE an exception
            # boundary. A real ``win32api.OpenProcess`` /
            # ``AssignProcessToJobObject`` fault raises (``pywintypes.error`` is
            # NOT an ``OSError`` subclass, so a narrow catch would still escape) --
            # any such fault here must FAIL CLOSED (postgres not_ready, retryable
            # on a later tick) rather than escape ``start_child`` -> ``tick`` ->
            # ``run`` and EXIT supervision. The tracked handle is SWAPPED IN only
            # AFTER ``assign_child`` succeeds, so a failed assign never leaves the
            # postmaster tracked-but-uncontained; a fault rolls the opened handle
            # back (terminate) so nothing is left tracked and nothing leaks.
            try:
                postmaster = self._runner.open_existing(postmaster_pid)
            except Exception as exc:
                self._child_states["postgres"] = "failed"
                return ChildStartOutcome(
                    name=name,
                    status="not_ready",
                    pid=handle.pid,
                    detail=(
                        f"postgres reported ready but opening the postmaster (pid "
                        f"{postmaster_pid}) faulted: {exc!r}; failing closed"
                    ),
                )
            try:
                # CC-PG-JOB: the postmaster's pid was resolved from OUR data
                # dir's ``postmaster.pid`` (the provenance), and pg_ctl's own
                # restricted-process job makes a cross-job ACCESS_DENIED the
                # NORMAL outcome on a fast start -- so this one call site opts
                # into the provenance-gated foreign-membership acceptance. All
                # other children keep the strict D3 assign.
                self._job.assign_child(postmaster.pid, accept_foreign_job_membership=True)
            except Exception as exc:
                # Forensics FIRST, while the postmaster is still alive: the
                # fault's winerror plus job-membership facts. The field
                # ACCESS_DENIED loop (reinstall over preserved ProgramData) has
                # two live hypotheses -- foreign-job inheritance vs our own
                # surviving job -- and only a capture AT the fault separates
                # them; after the rollback below the evidence is gone.
                # Same dual extraction as ``JobObjectController.assign_child``:
                # ``pywintypes.error`` carries ``winerror``; a plain ``OSError``
                # (and the test fakes) carry the code in ``args[0]``.
                winerror = getattr(exc, "winerror", None)
                if winerror is None and exc.args:
                    winerror = exc.args[0]
                _LOGGER.warning(
                    "postgres postmaster (pid %s) containment fault forensics: winerror=%s; %s",
                    postmaster.pid,
                    winerror,
                    self._job.containment_diagnostics(postmaster.pid),
                )
                # Roll back via postgres's OWN stop first: with no Job Object
                # over it, ``terminate`` kills only the postmaster pid and
                # reaps none of its background workers, so every retry of this
                # path stranded an orphan set still holding the shared-memory
                # segment -- the observed "pre-existing shared memory block is
                # still in use" that poisons later attempts. ``pg_ctl stop``
                # makes the postmaster tear down its own children; the
                # terminate below remains as the force fallback (a no-op once
                # the graceful stop has landed).
                with contextlib.suppress(Exception):
                    self._runner.graceful_stop(postmaster)
                with contextlib.suppress(Exception):
                    self._runner.terminate(postmaster)
                self._child_states["postgres"] = "failed"
                return ChildStartOutcome(
                    name=name,
                    status="not_ready",
                    pid=handle.pid,
                    detail=(
                        f"postgres reported ready but Job Object containment of the "
                        f"postmaster (pid {postmaster.pid}) faulted: {exc!r}; failing "
                        "closed (opened handle rolled back)"
                    ),
                )
            # Contained: NOW commit the swap (never before assign_child succeeds).
            self._handles["postgres"] = postmaster
            self._child_states["postgres"] = "ready"
            return ChildStartOutcome(
                name=name, status="started_ready", pid=postmaster.pid, detail=result.detail
            )

        self._child_states[name] = "ready"
        return ChildStartOutcome(
            name=name, status="started_ready", pid=handle.pid, detail=result.detail
        )

    def _log_guard_withheld(self, name: str, decision: GuardDecision) -> None:
        """G2(b): WARNING at the guard-withheld branch, with the guard's own
        reason. Latched PER CHILD: re-logged only when the withheld reason for
        that child actually changes (e.g. a different guard action), so a
        guard block that holds steady across many ticks logs once, not once
        per tick."""

        reason = f"{name}:{decision.action}:{decision.message}"
        if self._last_logged_guard_withheld.get(name) == reason:
            return
        self._last_logged_guard_withheld[name] = reason
        _LOGGER.warning(
            "child %s start withheld by guard: action=%s message=%s",
            name,
            decision.action,
            decision.message,
        )

    def _log_guard_degraded(self, name: str, decision: GuardDecision) -> None:
        """Chain I: the structured WARNING for a guard verdict that AUTHORIZED
        the start while naming a probe it could not trust.

        Deliberately NOT latched (unlike ``_log_guard_withheld``): a withhold
        repeats on every retry tick and needs latching to stay readable, but a
        degraded start happens once per spawn attempt, and every one of them is
        a distinct incident an operator should be able to count.

        Fields are the guard's own vocabulary -- action, named probe, reason --
        so a log line can be matched without parsing prose.
        """

        _LOGGER.warning(
            "child %s started with a degraded guard verdict: action=%s named_probe=%s message=%s",
            name,
            decision.action,
            decision.named_probe,
            decision.message,
        )

    def try_restart_child(self, name: str) -> ChildStartOutcome:
        """D6 controlled restart of a child that left ready. Eligibility (all
        predecessors ready) is enforced inside ``start_child``, so a caller can
        attempt a restart and get ``not_eligible`` back rather than a partial
        start."""

        return self.start_child(name)

    # -- full startup (D5/D6 order) ---------------------------------------

    def start(self) -> None:
        """Bring up postgres, then -- CC-WS5-001 -- poll+ENFORCE the
        maintenance interlock BEFORE the writer-capable control plane, then the
        control plane, in ``STARTUP_ORDER``.

        A boot-HELD interlock must NEVER leave a normal (writer-capable) control
        plane live: the interlock is read after the infra child (postgres,
        which is not writer-capable and comes up regardless) and before the
        control plane. If it is HELD, the machine enters ``maintenance`` and the
        control plane is launched in READ-ONLY maintenance mode
        (:meth:`_start_control_plane_maintenance`) -- the normal writer CP is
        never spawned. If it is free, the control plane starts normally and, once
        every child is ready, ``children_ready`` reaches ``ready``. If any child
        fails to reach ready, bring-up halts and the machine stays where it is
        (readiness is re-proven on the next attempt).

        F1: bring-up is ABORTABLE. ``self._should_abort`` is checked BEFORE each
        child (and, via ``poll_until_ready``, inside each readiness poll), so a
        stop requested during bring-up returns from here after at most ONE
        in-flight probe attempt instead of after every remaining child's full
        readiness budget (60s + 30s + 60s when F1 was written, chained into a
        single uninterruptible ~150s stretch -- U31 has since raised the control
        plane's own budget to 180s, making that same chain 300s -- which blew
        the 150s stop watchdog mid-chain and hard-killed the postgres cluster).

        Startup and reconciliation share a reentrant lock so control-pipe
        commands cannot start a child concurrently with this bring-up."""

        with self._reconciliation_lock:
            self._start()

    def _start(self) -> None:
        self._sweep_once()
        # Task #57 D2: the OPTIONAL ollama child first -- it has no D6 edge to
        # any other child, must come up (or skip cleanly) even in
        # pre-activation/maintenance boots, and its outcome NEVER gates the
        # bring-up below (a not-ready ollama is retried by the tick loop's
        # recovery path; a SKIPPED one is re-evaluated by that same loop's
        # _recheck_skipped_optional_child on the throttled cadence).
        if self._ollama_spec_provider is not None:
            if self._abort_bring_up("ollama"):
                return
            self.start_child(_OLLAMA_CHILD)
        # Infra (postgres) is not writer-capable -> it comes up regardless
        # of the interlock. STARTUP_ORDER[-1] is the writer-capable control plane,
        # gated by the interlock check below.
        for name in STARTUP_ORDER[:-1]:
            if self._abort_bring_up(name):
                return
            outcome = self.start_child(name)
            if outcome.status == "aborted":
                return
            if outcome.status != "started_ready":
                self._log_start_bail(outcome)
                return
        # CC-WS5-001: enforce the interlock BEFORE the writer child. poll_interlock
        # dispatches ``interlock_held`` (-> maintenance) on a boot-HELD read; a
        # free read is a no-op edge. No writer-capable CP is ever spawned while
        # the interlock is held.
        if self._abort_bring_up(STARTUP_ORDER[-1]):
            return
        if self.poll_interlock() == "interlock_held":
            self._start_control_plane_maintenance()
            return
        outcome = self.start_child(STARTUP_ORDER[-1])
        if outcome.status == "aborted":
            return
        if outcome.status != "started_ready":
            self._log_start_bail(outcome)
            return
        # G2: bring-up completed -- clear the bail latch so a LATER start()
        # bail (a fresh service run) is not silenced by a stale latch.
        self._last_logged_start_bail = None
        self._dispatch("children_ready")

    def _abort_bring_up(self, next_child: str) -> bool:
        """F1: True (and one INFO line) when a stop has been requested and the
        named child must therefore NOT be started. INFO, not WARNING: an
        aborted bring-up is a stop doing what it was asked to do, not a
        fault -- the WARNING vocabulary here (``_log_start_bail``) means "this
        station failed to come up", which would be a false alarm."""

        if not self._abort_requested():
            return False
        _LOGGER.info("bring-up aborted before child %s: a service stop was requested", next_child)
        return True

    def _log_start_bail(self, outcome: ChildStartOutcome) -> None:
        """G2(a): WARNING at ``start()``'s early bail -- the exact silence that
        made run 17 (G1) undiagnosable from logs. Latched: re-logged only when
        the reason (child + status + detail) actually CHANGES, so a repeatedly
        re-attempted start() (this method is only called from ``start()``
        itself, never the tick loop) never floods the log with an identical
        line."""

        reason = f"{outcome.name}:{outcome.status}:{outcome.detail}"
        if reason == self._last_logged_start_bail:
            return
        self._last_logged_start_bail = reason
        _LOGGER.warning(
            "startup halted at child %s: status=%s detail=%s",
            outcome.name,
            outcome.status,
            outcome.detail,
        )

    # -- maintenance (RAT-001) --------------------------------------------

    def enter_maintenance(self) -> ReadinessResult:
        """Drive ``interlock_held`` -> ``maintenance`` and launch the control
        plane in maintenance mode. Returns the maintenance-readiness result: the
        gate is satisfied ONLY when ``check_control_plane_maintenance_ready``
        passes; otherwise the supervisor stays in ``maintenance`` (fail-closed)."""

        with self._reconciliation_lock:
            return self._enter_maintenance()

    def _enter_maintenance(self) -> ReadinessResult:
        self._dispatch("interlock_held")
        if not self._swept:
            self._sweep_once()
        # Read-path infrastructure comes up normally (no guard gate on infra).
        self.start_child("postgres")
        return self._start_control_plane_maintenance()

    def _start_control_plane_maintenance(self) -> ReadinessResult:
        if not restart_eligible("control_plane", self._child_states, STARTUP_ORDER):
            return ReadinessResult(
                outcome="not_ready",
                detail="control_plane dependencies not ready; maintenance freeze holds",
            )
        spec = self._spec_for("control_plane", maintenance=True)
        # The maintenance control plane is read-only -> no writer-capable guard
        # gate (and RAT-002 forbids advancing a writer-capable child before the
        # interlock-freed re-evaluation; this launch starts NO workers).
        # CC-WS5-016: the same decide-and-spawn critical section as start_child --
        # the maintenance control plane is a spawn site too, and an operator
        # stop/drain during a maintenance boot must not race its Job Object
        # assignment against _job.close().
        with self._lifecycle_lock:
            if self._abort_requested():
                return ReadinessResult(
                    outcome="not_ready",
                    detail=(
                        "a service stop was requested; maintenance control-plane "
                        "start withheld before spawn"
                    ),
                )
            handle = self._runner.spawn(spec)
            self._handles["control_plane"] = handle
            self._child_states["control_plane"] = "starting"
            self._cp_mode = "maintenance"
            self._job.assign_child(handle.pid)
        return self._poll_control_plane_maintenance_ready()

    def _poll_control_plane_maintenance_ready(self) -> ReadinessResult:
        """Re-poll the fail-closed maintenance-readiness gate for the EXISTING
        maintenance control plane WITHOUT spawning a new one. Used both after a
        maintenance (re)spawn and, on subsequent held ticks, to re-sample a still-
        unattested maintenance CP -- so a live-but-unready maintenance CP is
        re-polled, never duplicate-spawned (CC-WS5-001). In maintenance the child
        is only marked ready when the fail-closed gate is satisfied; otherwise it
        stays 'starting' and the freeze holds."""

        spec = self._spec_for("control_plane", maintenance=True)
        cp = self._handles.get("control_plane")
        result = poll_until_ready(
            lambda: check_control_plane_maintenance_ready(self._health_probe),
            budget_seconds=spec.readiness_budget_seconds,
            clock=self._clock,
            sleep=self._sleep,
            # F1: the maintenance gate polls the SAME budget on the stop path (a
            # held-interlock tick calls this), so it gets the same abort seam.
            # An aborted poll leaves the CP 'starting' -- exactly the fail-closed
            # non-ready state a non-'ready' outcome already produced, so the
            # freeze still holds.
            should_abort=self._abort_requested,
            # U31: the maintenance CP is the SAME cold Python start as the normal
            # one, so it gets the same progress/liveness seams and the same stall
            # window. A maintenance CP that has died mid-poll fails at once
            # instead of holding the freeze for the whole budget.
            progress=(_cpu_progress_reader(self._runner, cp) if cp is not None else None),
            stall_seconds=spec.readiness_stall_seconds,
            liveness=(_child_liveness_reader(self._runner, cp) if cp is not None else None),
        )
        self._child_states["control_plane"] = "ready" if result.outcome == "ready" else "starting"
        return result

    # -- interlock edge handling (RAT-002) --------------------------------

    def on_interlock_freed(self) -> SupervisorEvent:
        """RAT-002 (load-bearing): on the interlock held->freed edge, perform
        EXACTLY ONE synchronous guard evaluation and emit the composite event
        matching ``GuardDecision.state_name``. Only a clear verdict routes to
        ``starting`` (a writer-capable state); a block verdict routes straight
        to the matching blocked state, so a condition masked during the freeze is
        re-sampled, never erased by event ordering. This method advances NO
        writer-capable child -- it only re-evaluates and dispatches."""

        decision = self._guard.evaluate_once()
        event = _interlock_freed_event(decision)
        self._dispatch(event)
        if event != "interlock_freed_clear":
            # CC-WS5-012: a block verdict at the maintenance-release edge halts
            # native transmission -- controlled-stop the (read-only maintenance)
            # control plane so no native process runs while WSL is authoritative
            # or a probe cannot be trusted. Mirrors the continuous-guard block
            # path (``_guard_block_if_dual_runtime`` -> ``_stop_control_plane``).
            # A clear verdict instead resumes via ``_resume_to_serving`` in the
            # tick loop. Infra (postgres) is untouched.
            self._stop_control_plane()
        return event

    def poll_interlock(self) -> SupervisorEvent | None:
        """Read the interlock and, on an edge, drive the machine. ``->held``
        dispatches ``interlock_held`` (maintenance); a ``held->free`` edge runs
        the RAT-002 re-evaluation. An ``unreadable`` read never frees a held
        interlock (fail-closed: a transmitter that can't confirm the freeze
        lifted keeps it held). Returns the event dispatched, or ``None`` for no
        edge."""

        status = self._interlock_reader()
        prev = self._prev_interlock
        self._prev_interlock = status

        if status == "held" and prev != "held":
            self._dispatch("interlock_held")
            return "interlock_held"
        if prev == "held" and status == "free":
            return self.on_interlock_freed()
        return None

    # -- restart storm (D5) -----------------------------------------------

    def record_restart(self, epoch: float) -> None:
        """Record one child restart timestamp for the D5 restart-storm window."""

        self._restart_epochs.append(epoch)

    def evaluate_restart_storm(self, *, now: float) -> bool:
        """D5: if the restart history crosses the storm threshold within the
        window, demote to ``degraded`` (the service stays up) and fire an alert
        through the injected outbox seam. Returns whether a storm was detected."""

        storm = restart_storm_check(self._restart_epochs, now=now, config=self._config)
        if storm:
            self._dispatch("restart_storm")
            self._alert.fire(
                summary="supervisor restart storm",
                detail=(
                    f"{len(self._restart_epochs)} child restarts within the "
                    f"{self._config.restart_storm_window_seconds}s window "
                    f"(threshold {self._config.restart_storm_threshold}); "
                    "demoted to degraded"
                ),
            )
        return storm

    # -- dependency loss (D6) ---------------------------------------------

    def on_dependency_lost(self, name: str) -> None:
        """A direct child left ready. Mark it failed and drive ``dependency_lost``
        (ready -> starting); the dependent controlled restart is then eligible
        only after the dependency re-enters ready (``restart_eligible``)."""

        self._child_states[name] = "failed"
        self._dispatch("dependency_lost")

    # -- the reconciliation run loop (D9; design.md:56/60) ----------------

    def run(self, stop_event: StopEventLike, *, poll_interval_seconds: float = 1.0) -> None:
        """The production supervision loop (design.md:56 "core.py owns ... run()
        loop", design.md:60 "service.SvcDoRun -> Supervisor.run"). Boot the
        children to ``ready`` (D5/D6) via :meth:`start`, then reconcile once per
        ``poll_interval_seconds`` until ``stop_event`` is set. Single-threaded and
        pure over the injected seams (clock/sleep/runner/interlock/guard), so the
        whole loop runs in CI with fakes; the service layer runs the graceful
        per-child stop chain after this returns."""

        self.start()
        while not stop_event.wait(poll_interval_seconds):
            self.tick(now=self._clock())

    def tick(self, *, now: float) -> None:
        """ONE reconciliation iteration -- the fake-testable unit. Drive the
        maintenance interlock edges (held->maintenance, held->free with a fresh
        guard verdict, RAT-002), then reconcile child liveness (detect death,
        restart, escalate a storm). ``starting`` is a recovery-IN-PROGRESS state
        and is reconciled too, so a restart the guard/readiness withheld on an
        earlier tick is RETRIED rather than wedged forever (F-REV-3). A
        ``blocked_*`` / ``stopping`` state does no child churn here -- the guard
        and interlock own those transitions.

        F1: a tick that begins after a stop was requested does NOTHING (the
        service is about to run the graceful stop chain, and every action here
        either spawns something that must then be stopped or spends a readiness
        budget on the stop's critical path). A stop that arrives DURING a tick
        is honoured between children and inside the readiness poll, so the
        in-flight iteration is bounded by ONE probe attempt. The complete pass
        is serialized with external start/restart commands."""

        with self._reconciliation_lock:
            self._tick(now=now)

    def _tick(self, *, now: float) -> None:
        if self._abort_requested():
            return
        event = self.poll_interlock()
        if self._state == "maintenance":
            self._reconcile_maintenance()
            return
        if event == "interlock_freed_clear":
            self._resume_to_serving()
            return
        # CC-WS5-012: reconcile the guard WHILE blocked so a CLEARED block does
        # not WEDGE the machine forever. A start-authorizing verdict dispatches
        # guard_clear (blocked_* -> starting) and marks the writer-capable CP
        # restartable; the shared recovery path below then brings it back up
        # (guard-gated), so no second interlock cycle is needed. A still-non-start
        # verdict holds (or re-routes) the block -- never a false clear. Returns
        # early unless the block cleared to ``starting``.
        if self._state in (
            "blocked_wsl_active",
            "blocked_probe_unavailable",
        ) and not self._guard_reconcile_while_blocked(now):
            return
        # CC-WS5-002: run the CONTINUOUS dual-runtime guard while serving, BEFORE
        # child recovery (a block must pre-empt a restart). Throttled to
        # guard_interval_seconds; a non-start verdict blocks and controlled-stops
        # the writer, so a native writer can never coexist with an activated WSL
        # runtime (charter item-4 exclusivity).
        if self._state in ("ready", "degraded") and self._guard_block_if_dual_runtime(now):
            return
        if self._state in ("ready", "degraded", "starting"):
            self._recover_dead_children(now)

    def _guard_block_if_dual_runtime(self, now: float) -> bool:
        """CC-WS5-002 throttled continuous guard. Returns True (a block was
        applied, so child recovery is skipped this tick) iff the guard was DUE
        (>= guard_interval_seconds since the last check) AND returned a NON-start
        verdict. On a block it dispatches the matching ratified ``guard_block_*``
        event (-> ``blocked_wsl_active`` / ``blocked_probe_unavailable``) and
        controlled-stops the writer-capable control plane. A start verdict, or a
        check not yet due, is a no-op (returns False). Throttling keeps a single
        tick to at most one guard evaluation and does not disturb RAT-002's
        one-eval-per-edge on the interlock-freed transition (that path returns
        before this check)."""

        if now - self._last_guard_check_epoch < self._config.guard_interval_seconds:
            return False
        self._last_guard_check_epoch = now
        decision = self._guard.evaluate_once()
        if decision.action in _GUARD_START_ACTIONS:
            return False
        self._dispatch(_guard_block_event(decision))
        self._stop_control_plane()
        return True

    def _guard_reconcile_while_blocked(self, now: float) -> bool:
        """CC-WS5-012 blocked-state RECOVERY LIVENESS. Reconcile the runtime guard
        while the machine is in a ``blocked_*`` state so a CLEARED block recovers
        instead of wedging forever (the round-3 fix corrected the block ROUTING;
        this closes the liveness hole the auditor found -- a blocked state never
        re-evaluated the guard, so a transient WSL/probe block that cleared left
        the control plane down until a service restart or a second interlock
        cycle).

        Reuses the SAME throttle as the serving-state continuous guard
        (``_last_guard_check_epoch`` + ``guard_interval_seconds``), so a blocked
        machine evaluates at most one verdict per interval. RAT-002's separate
        one-eval-per-release-edge (``on_interlock_freed``) is untouched.

        Returns True iff the block CLEARED: a start-authorizing verdict dispatches
        ``guard_clear`` (``blocked_* -> starting``) and marks the writer-capable
        control plane ``starting`` (restartable, no live handle -- it was
        controlled-stopped when the block engaged), so the shared recovery path
        restarts it guard-gated on this same tick. Returns False when the check is
        not yet due, or the verdict is STILL a non-start: the block holds,
        re-routed to the other blocked kind if the shape changed (a WSL block
        giving way to a probe-unavailable), NEVER cleared on a non-start verdict
        (P4: release always goes through ``starting``, re-proving readiness)."""

        if now - self._last_guard_check_epoch < self._config.guard_interval_seconds:
            return False
        self._last_guard_check_epoch = now
        decision = self._guard.evaluate_once()
        if decision.action in _GUARD_START_ACTIONS:
            self._dispatch("guard_clear")  # blocked_* -> starting
            # The CP was controlled-stopped when the block engaged; mark it
            # ``starting`` (not ``stopped``) so ``_needs_restart`` retries it in
            # the recovery path -- the CC-WS5-012 restartability rule.
            self._child_states["control_plane"] = "starting"
            return True
        # Still a non-start verdict: hold the block, re-routing wsl<->probe if the
        # shape changed. The CP is already down, so no second controlled-stop.
        self._dispatch(_guard_block_event(decision))
        return False

    def _reconcile_maintenance(self) -> None:
        """Hold the maintenance posture with a READ-ONLY control plane, and
        guarantee no writer-capable CP ever survives here (CC-WS5-001):

        * A NORMAL (writer-capable) control plane that is live in maintenance --
          e.g. a ready->held transition, or a boot race -- is TERMINATED and
          REPLACED by a maintenance-mode CP (a writer must never run while the
          interlock is held).
        * A live maintenance CP is NEVER duplicate-spawned: while its handle is
          still alive it is only re-polled for readiness (no handle accumulation
          over repeated held ticks -- the old reconcile respawned an unattested
          maintenance CP every tick).
        * Only when no live CP handle exists is a maintenance CP (re)launched.

        Infra (postgres) is already up from boot and is never respawned."""

        cp = self._handles.get("control_plane")
        if self._cp_mode == "normal" and cp is not None:
            # A normal writer-capable CP is live in maintenance -> replace it.
            self._stop_control_plane()
            self._start_control_plane_maintenance()
            return
        if cp is not None and self._runner.is_alive(cp):
            # A live maintenance CP already exists: re-poll readiness only (never
            # spawn a second one) until the fail-closed attestation gate passes.
            if self._child_states["control_plane"] != "ready":
                self._poll_control_plane_maintenance_ready()
            return
        # No live CP handle (missing or dead): (re)launch the maintenance CP.
        self._start_control_plane_maintenance()

    def _stop_control_plane(self) -> None:
        """Controlled stop of the control plane: terminate its process, drop the
        handle, mark it stopped, and clear the tracked CP mode. Used to replace a
        normal CP in maintenance (CC-WS5-001) and to halt the writer when the
        continuous guard blocks mid-operation (CC-WS5-002). Infra (postgres)
        is untouched."""

        cp = self._handles.get("control_plane")
        if cp is not None:
            self._runner.terminate(cp)
            self._handles.pop("control_plane", None)
        self._child_states["control_plane"] = "stopped"
        self._cp_mode = None

    def _resume_to_serving(self) -> None:
        """F-REV-2 fix: correctly resume to serving after a maintenance-exit
        clear verdict. Postgres is STILL alive+ready (it was never
        stopped in maintenance) -> it is NOT respawned. Only the control
        plane was maintenance-mode; restart it in NORMAL mode (the writer-capable
        ``pre_child_start`` guard gate applies), then, if every child is ready,
        dispatch ``children_ready`` to reach ``ready``. If the guard withholds
        the normal-mode start, the control plane stays down and the machine holds
        at ``starting`` (fail-closed) until a later tick's guard clears."""

        cp = self._handles.get("control_plane")
        if cp is not None:
            self._runner.terminate(cp)
            self._handles.pop("control_plane", None)
        # CC-WS5-012 restartability: mark the control plane ``starting`` (a
        # recovery-in-progress state), NOT ``stopped``, BEFORE the guarded
        # (re)start. If the guard WITHHOLDS the normal-mode start, ``start_child``
        # returns without spawning and leaves this ``starting`` with no live
        # handle -> ``_needs_restart`` is True, so a later serving/starting tick
        # retries it once the guard clears. A ``stopped`` child would have
        # ``_needs_restart`` return False and wedge the machine in ``starting``
        # forever (the maintenance-release twin of the CC-WS5-012 false-clear).
        self._child_states["control_plane"] = "starting"
        outcome = self.start_child("control_plane")
        if outcome.status == "started_ready" and all(
            self._child_states[name] == "ready" for name in STARTUP_ORDER
        ):
            self._dispatch("children_ready")

    def _recover_dead_children(self, now: float) -> None:
        """AC2/AC3 + CC-WS5-005: reconcile child liveness. For each child in D6
        order:

        * **New death** -- a child that WAS ready is now gone: mark it lost
          (``on_dependency_lost``: ready->starting, child failed), count it ONCE
          (``record_restart``, so the D5 storm window counts deaths not retry
          ticks), and mark every DOWNSTREAM child for a controlled restart so it
          rebinds to the replacement dependency (CC-WS5-005 sub-fix 3, D6).
        * **Recovery attempt** -- a child that is not ``ready``-and-alive (dead,
          OR alive-but-unready because a readiness poll timed out and left it
          wedged, CC-WS5-005 sub-fix 1) is (re)started, but only once its
          per-child jittered backoff permits (``now >= next_retry_epoch``,
          CC-WS5-005 sub-fix 2) -- a live-unready stale process is terminated
          first. A restart the guard or a D6 predecessor withheld does NOT arm
          the backoff (nothing was spawned), so it is retried freely on the next
          tick rather than wedging in ``starting`` (F-REV-3).

        The storm demotion + alert fires ONLY on the edge into ``degraded``
        (``restart_storm_check`` has no edge detection, so re-evaluating it every
        tick while already degraded would flood the alert transport, F-REV-4).
        Finally, if recovery re-readied every child, return to serving:
        ``children_ready`` from ``starting`` (bring-up completed), or the
        ``recovered`` event when a child returned to FRESH readiness while we were
        already ``degraded`` (CC-WS5-005 sub-fix 4: degraded->ready, dispatched
        only on a genuine recovery, never speculatively)."""

        was_degraded = self._state == "degraded"
        recovered_fresh = False
        # B5 field defect: a SKIPPED optional child re-evaluates its launch
        # prerequisites here, BEFORE the liveness loop below, so a store that
        # landed since the last check is picked up on this same iteration.
        self._recheck_skipped_optional_child(now)
        # Task #57 D2: the OPTIONAL ollama child is reconciled by the SAME
        # death-detect/backoff-restart loop, appended AFTER the D6-ordered
        # children -- but its death is NOT a dependency loss (nothing is
        # downstream of it and the machine state must not leave ready over a
        # degraded-AI child); it is marked failed, counted into the D5 storm
        # window (a crash-looping AI runtime surfaces as degraded + alert
        # through the existing mechanism), and restarted under its own backoff.
        names = (
            (*STARTUP_ORDER, _OLLAMA_CHILD)
            if self._ollama_spec_provider is not None
            else STARTUP_ORDER
        )
        for name in names:
            # F1: BETWEEN children. Recovery is the other place one iteration can
            # chain several readiness budgets (each _attempt_restart runs a full
            # start_child), so the stop request is honoured between every child
            # as well as inside every poll. Returning here (rather than breaking
            # to the tail) deliberately skips the storm evaluation and the
            # children_ready/recovered dispatch: neither is a true reading of a
            # reconciliation that was cut short.
            if self._abort_requested():
                _LOGGER.info(
                    "child recovery aborted before child %s: a service stop was requested", name
                )
                return
            handle = self._handles.get(name)
            if (
                handle is not None
                and not self._runner.is_alive(handle)
                and self._child_states[name] == "ready"
            ):
                if name == _OLLAMA_CHILD:
                    self._child_states[name] = "failed"
                    self.record_restart(now)
                else:
                    # A NEW death (was ready, now gone): mark lost, count once,
                    # and schedule the dependents for a rebind restart (D6).
                    self.on_dependency_lost(name)
                    self.record_restart(now)
                    self._mark_downstream_for_restart(name)

            if (
                self._needs_restart(name)
                and now >= self._backoff[name].next_retry_epoch
                and self._attempt_restart(name, now)
            ):
                recovered_fresh = True

        # Fire the storm demotion + alert ONLY on the edge into degraded.
        if self._state != "degraded":
            self.evaluate_restart_storm(now=now)

        if all(self._child_states[name] == "ready" for name in STARTUP_ORDER):
            if self._state == "starting":
                self._dispatch("children_ready")
            elif was_degraded and recovered_fresh:
                # Return from degraded only AFTER a real fresh recovery (never
                # speculatively) -- states.py: recovered (degraded -> ready).
                self._dispatch("recovered")

    def _recheck_skipped_optional_child(self, now: float) -> None:
        """B5 field defect (TESTER2 2026-08-12): re-evaluate a SKIPPED optional
        ollama child's launch prerequisites on a throttled cadence, so a model
        store that appears AFTER bring-up starts the runtime without a service
        restart or a reboot.

        Why this cannot be folded into ``_needs_restart``: ``stopped`` must stay
        exempt from the automatic per-tick retry (that exemption is what makes
        ``stopped`` mean "deliberately not running" for every other child, and
        re-stat'ing the install tree every second on a station that will never
        have a local AI runtime is exactly the cost the original design was
        avoiding). So the re-check is a SEPARATE, throttled path with three
        narrow gates:

        * the optional child is configured at all (``_ollama_spec_provider``);
        * it is ``stopped`` **and** carries a ``_start_blocked`` entry -- the
          pairing that uniquely marks "the provider skipped it", as distinct
          from the boot-time ``stopped`` before any attempt and from any future
          deliberate operator stop (which records no ``_start_blocked``, so this
          path will never resurrect it);
        * ``optional_child_recheck_seconds`` has elapsed since the last
          evaluation.

        This does NOT spawn anything and does NOT call the provider itself. All
        it does is move the child out of ``stopped`` into ``starting`` -- the
        same retry-eligible state Audit A3 uses for a guard-withheld start -- so
        that THIS tick's ``_needs_restart``/``_attempt_restart`` pass below asks
        the provider through the one existing spawn path. That keeps a single
        spawn site, with its D5 backoff arming, storm accounting and not-ready
        logging intact, instead of a second one racing it inside the same tick
        (which would burn two readiness budgets back to back and count two
        restarts for one real attempt).

        If the provider still skips, ``start_child`` puts the child straight
        back to ``stopped`` and re-records the (possibly changed) reason for the
        status read tier, and ``_attempt_restart`` leaves the backoff untouched
        because nothing was spawned. The service-layer provider dedupes its
        WARNING per distinct reason, so a permanently AI-less station logs one
        line, not one per minute, and is never demoted to ``degraded`` for
        lacking an optional runtime.

        CC-WS5-016: the admission decision is taken under the lifecycle lock and
        re-checks ``_abort_requested`` there, so this helper observes an operator
        stop the instant it begins. Without it, an authorized control-pipe
        ``stop``/``drain`` could move the machine to ``stopping`` after an
        in-flight tick had passed its outer state gate, and this helper would
        still promote the child to ``starting`` and let the loop below spawn
        Ollama into a shutting-down supervisor (TESTER4 review of PR #389)."""

        with self._lifecycle_lock:
            if self._abort_requested():
                return
            if self._ollama_spec_provider is None:
                return
            if self._child_states[_OLLAMA_CHILD] != "stopped":
                return
            if _OLLAMA_CHILD not in self._start_blocked:
                return
            if (
                now - self._last_optional_recheck_epoch
                < self._config.optional_child_recheck_seconds
            ):
                return
            self._last_optional_recheck_epoch = now
            self._child_states[_OLLAMA_CHILD] = "starting"

    def _needs_restart(self, name: str) -> bool:
        """Whether ``name`` needs a (re)start this reconciliation: it is NOT both
        ``ready`` and alive, and it is not a child left deliberately ``stopped``.
        Covers a dead handle, a live-but-unready wedge (CC-WS5-005 sub-fix 1), a
        downstream child marked for a rebind restart (handle popped), and (G1) a
        ``pending`` child that ``start()`` never even reached because an earlier
        STARTUP_ORDER predecessor missed its readiness budget -- ``pending`` is
        retry-eligible (falls through this ``stopped`` exemption exactly like
        ``starting`` / ``failed``), so it is picked up here as soon as
        ``restart_eligible`` allows once the predecessor recovers.

        The ``stopped`` exemption stands unchanged. A SKIPPED optional child is
        also ``stopped`` and so is also exempt here -- it is revived instead by
        :meth:`_recheck_skipped_optional_child`, on a throttled cadence rather
        than this per-tick path."""

        state = self._child_states[name]
        if state == "stopped":
            return False
        handle = self._handles.get(name)
        alive = handle is not None and self._runner.is_alive(handle)
        return not (state == "ready" and alive)

    def _mark_downstream_for_restart(self, name: str) -> None:
        """CC-WS5-005 sub-fix 3 (D6): when a dependency is (re)started after a
        death, terminate every DOWNSTREAM child (later in ``STARTUP_ORDER``) that
        is still alive and mark it ``failed`` so it is restarted, in order, bound
        to the replacement dependency. Terminating drops the stale handle; the
        per-child ``restart_eligible`` gate holds each dependent until its own
        predecessor is ready again."""

        index = STARTUP_ORDER.index(name)
        for downstream in STARTUP_ORDER[index + 1 :]:
            handle = self._handles.get(downstream)
            if handle is not None and self._runner.is_alive(handle):
                self._runner.terminate(handle)
            self._handles.pop(downstream, None)
            self._child_states[downstream] = "failed"

    def _attempt_restart(self, name: str, now: float) -> bool:
        """Attempt one (re)start of ``name`` and maintain its D5 backoff. A
        live-but-unready stale process is terminated first (CC-WS5-005 sub-fix 1).
        On success the backoff RESETS (attempt 0, retryable immediately) and the
        child's return to FRESH readiness is reported (the ``bool`` return). A
        genuine spawn-then-fail (``not_ready``) ARMS the jittered backoff so a
        crash-looping child is not respawned every tick (CC-WS5-005 sub-fix 2); a
        guard- or dependency-withheld start (nothing spawned: ``guard_blocked`` /
        ``not_eligible``) leaves the backoff untouched so it is retried freely on
        the next eligible tick (F-REV-3)."""

        # F1: never begin a restart once a stop has been requested -- the child
        # this would spawn is one the stop chain must then immediately stop
        # again, and its readiness poll would add a budget to the stop's
        # critical path. Nothing was spawned, so the backoff stays untouched.
        if self._abort_requested():
            _LOGGER.info("restart of child %s withheld: a service stop was requested", name)
            return False

        handle = self._handles.get(name)
        if handle is not None and self._runner.is_alive(handle):
            # Terminate the stale live-unready process before respawning.
            self._runner.terminate(handle)
            self._handles.pop(name, None)

        outcome = self.try_restart_child(name)
        backoff = self._backoff[name]
        if outcome.status == "started_ready":
            backoff.attempt = 0
            backoff.next_retry_epoch = 0.0
            # G2: clear the not-ready latch on recovery so a LATER not_ready
            # (even with an identical detail string) logs again as a fresh
            # incident rather than staying silenced by a stale pre-recovery
            # latch.
            self._last_logged_restart_not_ready.pop(name, None)
            return True
        if outcome.status == "not_ready":
            backoff.attempt += 1
            backoff.next_retry_epoch = now + backoff_with_jitter(
                backoff.attempt,
                initial_seconds=self._config.backoff_initial_seconds,
                max_seconds=self._config.backoff_max_seconds,
                jitter_fraction=self._config.backoff_jitter_fraction,
                rng=self._rng,
            )
            self._log_restart_not_ready(name, outcome)
        return False

    def _log_restart_not_ready(self, name: str, outcome: ChildStartOutcome) -> None:
        """G2(c): WARNING on a ``not_ready`` restart outcome, with the readiness
        detail (which, after the G2 service.py wrapper fix, now carries the real
        exception text instead of a swallowed generic failure). Latched PER
        CHILD: a crash-looping child that fails the SAME way every backoff cycle
        logs once, not once per attempt; a NEW detail (a different failure mode)
        re-logs."""

        reason = f"{name}:{outcome.detail}"
        if self._last_logged_restart_not_ready.get(name) == reason:
            return
        self._last_logged_restart_not_ready[name] = reason
        _LOGGER.warning(
            "restart of child %s not ready: detail=%s",
            name,
            outcome.detail,
        )

    # -- graceful drain-all (RAT-004) -------------------------------------

    def graceful_stop(self) -> None:
        """Drive ``stop`` -> ``stopping`` and send ``CTRL_BREAK`` to the
        control-plane process group so its uvicorn lifespan runs the daemon's
        ``stop_all_channels`` drain. Wait on observed process exit up to the D5
        graceful deadline; the Job Object is the backstop that reaps whatever
        survives (RAT-004: Job Object is backstop only, not the primary drain)."""

        # CC-WS5-016: publish stop INTENT and make the state transition
        # atomically, before anything else. Whichever path called us -- SCM stop,
        # or the authorized control-pipe stop/drain verb, which reaches here
        # DIRECTLY via AdminCommandRouter._drain and sets no service event -- a
        # concurrent reconciliation thread now observes the stop through
        # _abort_requested. Taking the lock also means a decide-and-spawn section
        # already in flight completes before the machine leaves the serving
        # states, so no spawn can straddle this transition.
        with self._lifecycle_lock:
            self._stop_intent = True
            self._dispatch("stop")
            control_plane = self._handles.get("control_plane")
        # The drain wait runs OUTSIDE the lock: it can burn the full D5 graceful
        # deadline, and holding the lock across it would stall an in-flight
        # reconciliation thread for that entire window to no purpose -- the
        # intent flag above has already closed the spawn door.
        if control_plane is not None:
            self._runner.send_ctrl_break(control_plane)
            deadline = self._clock() + self._config.graceful_stop_deadline_seconds
            while self._runner.is_alive(control_plane):
                if self._clock() >= deadline:
                    break
                self._sleep(1.0)
        # Backstop: closing the Job Object triggers kill-on-close on any process
        # still contained (D3/AC4). Under the lock so the close can never
        # interleave with a spawn's ``assign_child`` (the review's "no Job Object
        # assignment after close").
        with self._lifecycle_lock:
            self._job.close()

    # -- admin tier action: controlled control-plane restart (CC-WS5-007) -

    def restart_control_plane(self) -> ChildStartOutcome:
        """CC-WS5-007 admin ``restart`` action: a controlled restart of the
        control-plane child. Terminates the live CP handle (if any), drops it,
        marks the child ``starting``, and re-runs the guard-gated
        ``start_child`` -- so the D9 ``pre_child_start`` gate still governs
        whether the writer-capable CP may come back (a guard-withheld restart
        returns ``guard_blocked`` and brings no writer up). Idempotent-safe:
        each call performs at most one clean controlled restart (the old handle
        is always terminated + popped before the respawn, so handles never
        accumulate). Infra (postgres) is untouched -- this restarts ONLY
        the control plane. Mirrors the CP-restart step of ``_resume_to_serving``;
        used by the service layer's admin router, never by the tick loop."""

        with self._reconciliation_lock:
            cp = self._handles.get("control_plane")
            if cp is not None:
                self._runner.terminate(cp)
                self._handles.pop("control_plane", None)
            # ``starting`` (not ``stopped``) so a guard-withheld start leaves the CP
            # restartable by a later reconciliation tick (the CC-WS5-012 rule).
            self._child_states["control_plane"] = "starting"
            return self.start_child("control_plane")

    # -- read tier (pipe_server status/version) ---------------------------

    def status_snapshot(self) -> StatusSnapshot:
        """The read-tier snapshot the control pipe's ``status`` command serves.

        CC-WS5-010 (KeyError race, CLOSED): the pipe's ``CommandQueue`` runs this
        on its own worker thread while ``run()``/``tick()`` mutate these same
        fields on the loop thread. The old build did a membership test then an
        index (``name in self._handles`` ... ``self._handles[name]``); a
        concurrent handle removal BETWEEN those two lines raised ``KeyError``,
        which the ``CommandQueue`` surfaced as a transient ``status:error``. The
        fix snapshots every mutable structure into a LOCAL once at the top
        (copy-then-build): each ``dict(...)`` copy is atomic under the GIL, and
        the ``ChildStatusView`` list + ``StatusSnapshot`` are built from the
        LOCAL copies ONLY (never re-reading ``self._...``), so no
        test/index pair can tear -> no ``KeyError``.

        RESIDUAL (F-REV-5, still disclosed, no crash): the several locals are
        captured microseconds apart, so a cross-field skew can still momentarily
        differ (e.g. ``state`` already advanced to ``starting`` while
        ``child_states`` still shows a stale ``ready``). Impact is bounded to a
        momentarily-inconsistent operator status VIEW that self-corrects on the
        next query -- the supervision logic itself is unaffected. A lock is
        deliberately NOT taken: holding it across a ``tick()`` that blocks in
        ``poll_until_ready`` would stall status queries for up to a child's
        readiness budget (~60s) -- a worse trade than the momentary skew."""

        # Copy-then-build: one atomic (GIL) copy of each mutable structure, then
        # index ONLY the locals below -- never ``self._...`` -- so a concurrent
        # handle removal cannot race a membership/index pair into a KeyError.
        handles = dict(self._handles)
        child_states = dict(self._child_states)
        start_blocked = dict(self._start_blocked)
        state = self._state
        last = self._guard.status.last_decision
        alert = self._guard.status.alert
        restart_n = len(self._restart_epochs)

        # Task #57 D2: the OPTIONAL ollama child appears in the read tier ONLY
        # when it is configured (a provider is wired), so unconfigured
        # deployments/tests keep the exact three-row snapshot. A skipped child
        # shows ``stopped`` with the skip reason in blocked_detail.
        names = (
            (*STARTUP_ORDER, _OLLAMA_CHILD)
            if self._ollama_spec_provider is not None
            else STARTUP_ORDER
        )
        children = [
            ChildStatusView(
                name=name,
                state=child_states.get(name, "stopped"),
                pid=(handles[name].pid if name in handles else None),
                blocked_detail=start_blocked.get(name),
            )
            for name in names
        ]
        return StatusSnapshot(
            state=state,
            workers_permitted=workers_permitted(state),
            children=children,
            guard_last_action=(last.action if last is not None else None),
            guard_state_name=(last.state_name if last is not None else None),
            guard_alert=alert,
            restart_count_window=restart_n,
            protocol_version=SUPERVISOR_PROTOCOL_VERSION,
            version=SUPERVISOR_VERSION,
        )

    def command_handler(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        """The ``pipe_server.CommandQueue`` handler for the read tier. ``status``
        returns the snapshot; ``version`` returns the protocol/version pair. The
        mutating admin-tier commands (start/stop/restart/drain/runtime_set) are
        wired to real actions at the service layer, not here."""

        if command == "status":
            return self.status_snapshot().model_dump()
        if command == "version":
            return {
                "protocol_version": SUPERVISOR_PROTOCOL_VERSION,
                "version": SUPERVISOR_VERSION,
            }
        raise ValueError(
            f"core command_handler does not serve {command!r}; "
            "mutating commands are wired at the service layer"
        )


def _interlock_freed_event(decision: GuardDecision) -> SupervisorEvent:
    """Map a guard verdict at the interlock held->freed edge onto exactly one
    composite event (RAT-002). Routes on ``decision.action`` -- NOT
    ``state_name`` -- EXACTLY mirroring ``_guard_block_event`` (CC-WS5-012).

    This is safety-critical and the twin of the CC-WS5-002 fix: the raw
    ``GuardMonitor.evaluate_once`` the supervisor consumes at the freed edge
    returns a real WSL/never_start block as ``action="never_start"`` (or
    ``"refuse"``) with ``state_name=None`` -- the ``blocked_wsl_active`` relabel
    is applied only inside ``GuardMonitor.run`` (``_mid_operation_decision``),
    which this path does not use. The old ``state_name``-router mapped that
    ``None`` to ``interlock_freed_clear`` -- a FALSE CLEAR that advanced
    ``maintenance`` -> ``starting``, terminated the maintenance CP, had
    ``start_child`` correctly withhold the writer, and wedged the machine in
    ``starting`` forever. Routing on action closes that:

    * ``action`` in ``_GUARD_START_ACTIONS`` (``start`` / ``start_degraded``)
      -> ``interlock_freed_clear`` (the only authorization to a writer-capable
      state);
    * ``action == "blocked_probe_unavailable"`` -> ``interlock_freed_blocked_probe``;
    * every other (non-start) action -> ``interlock_freed_blocked_wsl``
      (``never_start`` / ``refuse`` / ``refuse_instruct``). Fail-CLOSED to the
      WSL block -- a non-start verdict never advances a writer-capable state,
      however its ``state_name`` happens to be labeled."""

    if decision.action in _GUARD_START_ACTIONS:
        return "interlock_freed_clear"
    if decision.action == "blocked_probe_unavailable":
        return "interlock_freed_blocked_probe"
    return "interlock_freed_blocked_wsl"


def _guard_block_event(decision: GuardDecision) -> SupervisorEvent:
    """Map a NON-start guard verdict observed mid-operation (CC-WS5-002) onto the
    matching ratified ``guard_block_*`` event.

    This mirrors ``runtime_guard._mid_operation_decision`` EXACTLY: a non-start
    verdict is the WSL-active block UNLESS it is the distinct
    ``blocked_probe_unavailable`` verdict. Routing on ``action`` (not on
    ``state_name``) is deliberate and safety-critical: the raw
    ``GuardMonitor.evaluate_once`` the supervisor consumes returns a mid-operation
    WSL flip as ``action="refuse", state_name=None`` (the WSL-active relabel is
    applied only inside ``GuardMonitor.run``). Routing on ``state_name`` would
    therefore raise on a real WSL flip and crash the tick loop (fail-OPEN). Any
    unrecognised non-start action falls through to the WSL block -- fail-CLOSED
    (halt transmission) rather than guess a resume."""

    if decision.action == "blocked_probe_unavailable":
        return "guard_block_probe"
    return "guard_block_wsl"


__all__ = [
    "SUPERVISOR_PROTOCOL_VERSION",
    "SUPERVISOR_VERSION",
    "AlertOutbox",
    "ChildHandle",
    "ChildProcessRunner",
    "ChildStartOutcome",
    "ChildStatusView",
    "GuardLike",
    "StatusSnapshot",
    "StopEventLike",
    "Supervisor",
]
