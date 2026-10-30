# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Configuration model and fixed identity constants for the native supervisor.

Pure data + validation only -- no I/O and no Windows imports, so this module
loads and its parameters are checkable on any OS. Two kinds of thing live here:

1. **WS5-defined identity constants (owner-surfaced).** No upstream spec fixes
   the service's string identity or the singleton mutex name -- spec
   ``spec-supervisor.md`` D1/D3/D7 describe *that* there is a service and a
   ``Global\\`` singleton mutex, but not their exact strings. They are declared
   here once and were surfaced for owner approval (recorded in
   ``civiccast-ws-handoff-artifacts/ws5-owner-decisions-2026-07-20.md``,
   approved 2026-07-20). The singleton's DACL is deliberately identical to
   WS4's runtime-owner mutex per D3/D7's "same explicit SD" clause, so it is
   imported from ``runtime_guard`` rather than re-typed (one source of truth).

2. **The pure D5/D6 parameters** the state logic and (later) the child
   lifecycle are defined in terms of: backoff schedule, restart-storm window,
   graceful-stop deadline, readiness budgets. Cross-field invariants that a
   single ``Field`` constraint cannot express (e.g. ``backoff_max`` must not be
   below ``backoff_initial``) are enforced by an after-validator so a
   nonsensical config fails loudly at construction, matching the house
   ``extra="forbid"`` fail-closed posture.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from civiccast.native.runtime_guard import MONITOR_DEFAULT_INTERVAL_SECONDS, MUTEX_SDDL

# ---------------------------------------------------------------------------
# Identity constants (WS5-defined; owner-approved 2026-07-20).
# ---------------------------------------------------------------------------

SERVICE_NAME = "CivicCastSupervisor"
DISPLAY_NAME = "CivicCast Native Supervisor"
EVENT_LOG_SOURCE = "CivicCastSupervisor"
SINGLETON_MUTEX_NAME = r"Global\CivicCastSupervisorSingleton"
# spec D3/D7 "same explicit SD" clause: SYSTEM, BUILTIN\Administrators and
# the CivicCast supervisor service SID receive GENERIC_ALL; other local
# accounts do not. This is identical to WS4's ``runtime_guard.MUTEX_SDDL`` and
# prevents an unrelated local process from forging singleton ownership or
# holding the station offline.
SINGLETON_MUTEX_SDDL = MUTEX_SDDL

# Control pipe identity (D7). Frame cap is a config field (below) so tests can
# exercise the boundary without a 16 KiB literal scattered across modules.
CONTROL_PIPE_NAME = r"\\.\pipe\civiccast-supervisor"

# D6 startup order: each entry depends on ALL predecessors being ``ready``
# before it may (re)start. ``postgres`` has no dependency. Media workers are
# owned by the control-plane daemon (D2), NOT the supervisor, so they are
# deliberately absent from this supervisor-child ordering. NATS JetStream was
# removed from the product (owner decision 2026-08-20; see ADR 0023) -- the
# platform substrate is the in-process broker only, so it has no supervised
# child and is absent here.
STARTUP_ORDER: tuple[str, ...] = ("postgres", "control_plane")


class SupervisorConfig(BaseModel):
    """The pure D5/D6 parameters. Defaults are the spec values verbatim.

    ``extra="forbid"`` so an unknown key (typo, schema drift) fails at parse
    time rather than being silently ignored -- the same fail-closed contract
    every other native model carries.
    """

    model_config = ConfigDict(extra="forbid")

    # D5 restart backoff: exponential 1s -> 30s with +/-20% jitter. The jitter
    # is applied at call time with an injected RNG (impure, tested separately);
    # this model carries only the pure schedule parameters.
    backoff_initial_seconds: float = Field(default=1.0, gt=0)
    backoff_max_seconds: float = Field(default=30.0, gt=0)
    backoff_jitter_fraction: float = Field(default=0.20, ge=0, le=1)

    # D5 restart storm: >= threshold restarts within the window -> ``degraded``
    # (service stays up, alert fires).
    restart_storm_threshold: int = Field(default=5, ge=1)
    restart_storm_window_seconds: float = Field(default=600.0, gt=0)

    # D5 graceful stop: deadline per child, then ``TerminateProcess``.
    graceful_stop_deadline_seconds: float = Field(default=15.0, gt=0)

    # D6 readiness budget for the first child (postgres ``SELECT 1``).
    postgres_ready_budget_seconds: float = Field(default=60.0, gt=0)

    # D9 guard-monitor evaluation interval (defaults to WS4's monitor default so
    # the two halves of the guard integration tick in lockstep unless overridden).
    guard_interval_seconds: float = Field(default=MONITOR_DEFAULT_INTERVAL_SECONDS, gt=0)

    # D7 control-pipe frame cap: oversized frame -> close connection.
    control_pipe_frame_cap_bytes: int = Field(default=16 * 1024, gt=0)

    # Task #57 D2 / chain H1: how often a SKIPPED optional child (the local-AI
    # runtime) re-evaluates its launch prerequisites. A skip is NOT a durable
    # install property: the first-run acquisition flow downloads the model
    # store into ProgramData WHILE the supervisor is already running, so a
    # station that booted without a store must notice the store appearing
    # without waiting for a service restart. Throttled (not per-tick) so a
    # permanently AI-less station re-stats the tree once a minute, not once a
    # second.
    optional_child_recheck_seconds: float = Field(default=60.0, gt=0)

    @model_validator(mode="after")
    def _check_backoff_bounds(self) -> SupervisorConfig:
        if self.backoff_max_seconds < self.backoff_initial_seconds:
            raise ValueError(
                "backoff_max_seconds "
                f"({self.backoff_max_seconds}) must be >= backoff_initial_seconds "
                f"({self.backoff_initial_seconds})"
            )
        return self


__all__ = [
    "CONTROL_PIPE_NAME",
    "DISPLAY_NAME",
    "EVENT_LOG_SOURCE",
    "SERVICE_NAME",
    "SINGLETON_MUTEX_NAME",
    "SINGLETON_MUTEX_SDDL",
    "STARTUP_ORDER",
    "SupervisorConfig",
]
