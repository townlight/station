# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The supervisor's state vocabulary and its pure, total transition function.

This is the correctness core of the slice: no I/O, no Windows imports, no
process handles -- just ``(state, event) -> state`` and a few D5/D6 predicates,
so every claim about the supervisor's control logic is checkable in CI on any
OS (spec build-step 1: "child state machine (pure, CI-tested)").

Seven states (owner-approved deviation, 2026-07-20).
--------------------------------------------------
Spec ``spec-supervisor.md`` D6 enumerates FIVE states: ``starting / ready /
degraded / blocked_wsl_active / stopping``. This module carries SEVEN -- the
five plus two that other, already-merged specs mandate but D6's enum omits.
The addition is disclosed, not silent (specs/README ladder: surface, don't
diverge), and was surfaced for and received owner approval:

* ``blocked_probe_unavailable`` -- mandated by the dual-runtime guard (WS4,
  ``spec-dual-runtime-guard`` D3/AC9) and already the ``state_name`` the merged
  ``runtime_guard`` emits when a probe cannot be trusted. The supervisor must
  be able to *hold* that state, so it must be in the enum.
* ``maintenance`` -- the installer/migration read-only health mode
  (``spec-installer-lifecycle`` D3 / ``spec-migration`` D1): while the D7a
  maintenance interlock is HELD, bring up Postgres + control plane in a
  read-only posture and start NO media workers, leaving only when the interlock
  frees. D6's five states had nowhere to put this.

The two blocked-state string values are IDENTICAL to the guard's
``GuardDecision.state_name`` vocabulary, so a guard decision maps onto a
supervisor state with no translation table.

Event alphabet (a WS5 modeling choice, disclosed).
--------------------------------------------------
D6 defines the states and the readiness/dependency *semantics* but does not
enumerate a formal event alphabet -- that is an implementation choice. The
events below are the complete set of inputs that move the supervisor, each
traceable to a spec obligation (guard decisions -> D9/D3; interlock -> D7a;
restart storm -> D5; readiness/dependency -> D6; stop -> service SvcStop).
``supervisor_transition`` is TOTAL over the (state x event) product -- proven by
the P1 exhaustive-product test.

Precedence (design Sec.1): ``maintenance`` > ``blocked_*`` > ``degraded``.
Because the machine consumes one event at a time, precedence is encoded as
ordered guards: a higher-priority condition's event overrides regardless of the
lower-priority state it interrupts, and while the higher-priority state holds,
lower-priority events are ignored until it clears.

Maintenance exit carries a fresh guard verdict (WS5-RAT-002).
--------------------------------------------------------------
Leaving ``maintenance`` is the one transition that must NOT trust event
ordering. A ``guard_block_*`` that arrives while the interlock is HELD is
correctly ignored -- the freeze holds and nothing transmits in maintenance --
but that block must not be *erased* when the interlock frees. So the
interlock-release signal is not a bare event: it CARRIES the guard decision as
one of three composite events. ``core.py`` re-evaluates the guard synchronously
at the held->freed edge and emits exactly one of ``interlock_freed_clear`` /
``interlock_freed_blocked_wsl`` / ``interlock_freed_blocked_probe``. Only a
clear verdict reaches ``starting`` (a writer-capable state); a WSL-active or
probe-unavailable verdict routes straight to the matching blocked state. There
is therefore no path out of maintenance toward a transmitting state without a
current, positive guard authorization -- the fail-open the ratification found.

Four properties the tests pin (spec build-step 1):
* **P1 totality** -- every (state, event) yields a valid state.
* **P2 maintenance never starts workers** -- ``workers_permitted("maintenance")``
  is False (as it is for every non-serving state).
* **P3 dependent restart ordering** -- a child is restart-eligible only after
  ALL its D6 predecessors are ``ready`` (D6: "a controlled restart AFTER the
  dependency re-enters ready").
* **P4 no blocked -> ready shortcut** -- from either blocked state, no event
  reaches ``ready`` directly; release goes ``blocked_* --guard_clear-->
  starting --children_ready--> ready``, so readiness is always re-proven.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, get_args

SupervisorState = Literal[
    "starting",
    "ready",
    "degraded",
    "blocked_wsl_active",
    "blocked_probe_unavailable",
    "maintenance",
    "stopping",
]
"""The overall supervisor state. The two ``blocked_*`` values equal
``runtime_guard`` ``GuardDecision.state_name`` verbatim."""

SupervisorEvent = Literal[
    "start",
    "children_ready",
    "dependency_lost",
    "restart_storm",
    "recovered",
    "guard_clear",
    "guard_block_probe",
    "guard_block_wsl",
    "interlock_held",
    "interlock_freed_clear",
    "interlock_freed_blocked_wsl",
    "interlock_freed_blocked_probe",
    "stop",
]
"""The complete input alphabet -- see the module docstring for the spec
obligation each event traces to."""

ChildState = Literal["pending", "stopped", "starting", "ready", "stopping", "failed"]
"""Per-direct-child lifecycle (postgres / control_plane). Used by the D6
dependency-ordering predicate; the workers are not children (D2).

``pending`` (G1) is the BOOT-TIME init for every ``STARTUP_ORDER`` child: it
means "never yet attempted this service run" and is retry-eligible, exactly
like ``starting`` / ``failed`` -- distinct from ``stopped``, which means
"deliberately not running" (the ollama optional child's clean skip; a
controlled stop) and is EXEMPT from the tick loop's automatic retry. Before
this state existed, ``STARTUP_ORDER`` children booted straight into
``stopped``, so a child that ``Supervisor.start()`` never reached (because an
earlier child missed its readiness budget) was indistinguishable from a
deliberately-stopped child -- ``_needs_restart`` skipped it forever, even
after the blocking dependency recovered (run 17: control_plane never even
attempted in 15 minutes despite postgres recovering)."""

# States in which media workers (owned by the control-plane daemon, D2) are
# permitted to be running. Everything else -- maintenance (read-only),
# either blocked state (transmission halted), starting (children not yet up),
# and stopping (draining) -- forbids them. ``degraded`` keeps serving (the
# service stays up and only alerts), so workers remain permitted there.
_WORKER_SERVING_STATES: frozenset[str] = frozenset({"ready", "degraded"})

_BLOCKED_STATES: frozenset[str] = frozenset({"blocked_wsl_active", "blocked_probe_unavailable"})


def supervisor_transition(state: SupervisorState, event: SupervisorEvent) -> SupervisorState:
    """The pure, total supervisor transition. See the module docstring for the
    precedence rationale; the ordering of the guards below IS the precedence.
    """

    # ``stopping`` is absorbing: once a graceful stop begins, nothing (not a
    # freed interlock, not a guard clear) pulls the service back to running.
    if state == "stopping":
        return "stopping"

    # STOP always wins from any live state -> begin the graceful stop chain.
    if event == "stop":
        return "stopping"

    # Precedence tier 1 -- maintenance dominates blocked_* and degraded: an
    # interlock-held event moves us to maintenance from ANY live state.
    if event == "interlock_held":
        return "maintenance"

    # While in maintenance the interlock-release CARRIES a fresh guard verdict
    # (see the module docstring, WS5-RAT-002): only a clear verdict leaves to
    # ``starting``; a block verdict routes to the matching blocked state so a
    # condition masked during the freeze is re-sampled, never erased by event
    # ordering. Every other event is ignored -- maintenance holds until the
    # installer/migration releases the interlock.
    if state == "maintenance":
        if event == "interlock_freed_clear":
            return "starting"
        if event == "interlock_freed_blocked_wsl":
            return "blocked_wsl_active"
        if event == "interlock_freed_blocked_probe":
            return "blocked_probe_unavailable"
        return "maintenance"

    # Precedence tier 2 -- blocked_* dominates degraded: a fresh guard block
    # moves us to the named blocked state from any non-maintenance live state.
    # The latest guard decision wins, so the two blocked states can replace each
    # other (e.g. a definite wsl-active read giving way to a can't-read read).
    # A block verdict carried by an interlock-release outside maintenance (a
    # release we were not holding) halts identically -- a fresh block always
    # wins, however it arrives.
    if event in ("guard_block_wsl", "interlock_freed_blocked_wsl"):
        return "blocked_wsl_active"
    if event in ("guard_block_probe", "interlock_freed_blocked_probe"):
        return "blocked_probe_unavailable"

    # From a blocked state, ONLY a start-authorizing guard decision releases it,
    # and only to ``starting`` -- never straight to ``ready`` (P4). Any other
    # event is held while blocked.
    if state in _BLOCKED_STATES:
        if event == "guard_clear":
            return "starting"
        return state

    # Normal lifecycle (starting / ready / degraded), no maintenance, no active
    # block. A restart storm demotes any of them to ``degraded`` (D5).
    if event == "restart_storm":
        return "degraded"

    if state == "starting":
        # Children finished coming up in D6 order -> ready. Everything else
        # (a guard clear, a freed interlock we were not holding, a recovered
        # signal, an isolated dependency loss during bring-up) leaves us
        # starting.
        if event == "children_ready":
            return "ready"
        return "starting"

    if state == "ready":
        # A direct child left ready -> re-establish readiness (D6 controlled
        # restart; the per-child ordering is enforced by ``restart_eligible``).
        if event == "dependency_lost":
            return "starting"
        return "ready"

    # state == "degraded"
    if event == "recovered":
        return "ready"
    return "degraded"


def workers_permitted(state: SupervisorState) -> bool:
    """Whether media workers may be running in ``state`` (D2 workers are owned
    by the control-plane daemon; this is the supervisor-level gate). False in
    ``maintenance`` is property P2, but the predicate is defined positively over
    the serving states so the property is not a special-case."""

    return state in _WORKER_SERVING_STATES


def restart_eligible(
    child: str, child_states: Mapping[str, ChildState], order: Sequence[str]
) -> bool:
    """D6 dependency ordering (property P3): ``child`` may (re)start only once
    every predecessor of it in ``order`` is ``ready``.

    The first entry in ``order`` (postgres) has no predecessor and is therefore
    always eligible. A child not present in ``order`` is treated as having no
    ordering constraint (eligible) -- callers pass ``STARTUP_ORDER`` and only
    ask about its members, but the predicate stays total rather than raising.
    A predecessor missing from ``child_states`` counts as NOT ready (fail
    closed: an unknown dependency does not authorize a dependent start).
    """

    if child not in order:
        return True
    index = order.index(child)
    return all(child_states.get(predecessor) == "ready" for predecessor in order[:index])


def is_restart_storm(
    restart_epochs: Sequence[float], now: float, window_seconds: float, threshold: int
) -> bool:
    """D5 restart-storm test: True iff at least ``threshold`` restarts fall
    within the trailing ``window_seconds`` ending at ``now`` (inclusive on the
    window's leading edge). Pure over an explicit list of restart timestamps so
    the ``>= 5 / 10 min -> degraded`` rule is exercised without a real clock."""

    cutoff = now - window_seconds
    recent = sum(1 for epoch in restart_epochs if cutoff <= epoch <= now)
    return recent >= threshold


def backoff_base_seconds(attempt: int, initial_seconds: float, max_seconds: float) -> float:
    """D5 exponential backoff BASE delay (jitter applied separately by the
    caller with an injected RNG). ``attempt`` is 0-indexed: attempt 0 returns
    ``initial_seconds``, doubling each attempt, capped at ``max_seconds``.
    ``attempt`` below 0 is clamped to 0 rather than producing a sub-initial
    delay."""

    exponent = max(attempt, 0)
    return min(initial_seconds * (2.0**exponent), max_seconds)


def _all_states() -> tuple[SupervisorState, ...]:
    """All SupervisorState values (drives the P1 exhaustive-product test)."""

    return get_args(SupervisorState)


def _all_events() -> tuple[SupervisorEvent, ...]:
    """All SupervisorEvent values (drives the P1 exhaustive-product test)."""

    return get_args(SupervisorEvent)


__all__ = [
    "ChildState",
    "SupervisorEvent",
    "SupervisorState",
    "backoff_base_seconds",
    "is_restart_storm",
    "restart_eligible",
    "supervisor_transition",
    "workers_permitted",
]
