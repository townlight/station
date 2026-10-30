# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""CC-WS5-007 part 2: the admin-verb composition layer.

``authz.py`` decides WHETHER a mutating verb is allowed (the two-tier,
per-command gate the pipe server enforces by impersonating the caller). This
module answers the orthogonal question CC-WS5-007 opened: once a verb is
authorized, WHAT does it do? Before this, ``core.command_handler`` served only
``status``/``version`` and RAISED for every admin verb, so an authorized
``stop``/``start``/``restart``/``drain``/``runtime_set`` reached a dead end.

:class:`AdminCommandRouter` wires each authorized admin verb to a real
supervisor action with IDEMPOTENT semantics (D5 graceful drain-all for
stop/drain; a guarded bring-up for start; a guarded controlled restart of the
control plane; a validate-then-apply of the D-runtime selector for
runtime_set). Every action returns a structured :class:`AdminActionResult`
(``verb`` + ``applied``/``noop``/``refused`` + resulting ``state`` + ``detail``)
so an operator (or the identity-verifying control client) can tell a real change
from a no-op.

This layer does NOT re-implement authorization -- it runs BEHIND the pipe's
existing admin-tier gate. :func:`build_command_handler` composes the read tier
(``core.command_handler``: status/version) with this router (the mutating tier)
into the single ``CommandHandler`` the pipe's serialized ``CommandQueue`` runs,
so AC-N5 (one command at a time) still holds across both tiers.

The supervisor and the selector I/O are injected seams, so the idempotence and
refuse branches all run on Linux in CI with fakes -- no Win32, no registry, no
pipe (``tests/native/test_supervisor_admin.py``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from civiccast.native.supervisor.core import ChildStartOutcome
from civiccast.native.supervisor.pipe_server import CommandHandler
from civiccast.native.supervisor.states import SupervisorState, workers_permitted

logger = logging.getLogger(__name__)

# The runtime the D-runtime selector may hold (``win_probes`` selector value
# vocabulary, minus ``absent``: a legal *target* is only ever a concrete
# runtime). ``runtime_set`` refuses anything outside this set.
RuntimeTarget = Literal["native", "wsl"]
_RUNTIME_TARGETS: frozenset[str] = frozenset({"native", "wsl"})

AdminVerb = Literal["start", "stop", "restart", "drain", "runtime_set"]
_ADMIN_VERBS: frozenset[str] = frozenset({"start", "stop", "restart", "drain", "runtime_set"})
_READ_VERBS: frozenset[str] = frozenset({"status", "version"})

AdminOutcome = Literal["applied", "noop", "refused"]
"""``applied`` = the verb changed state; ``noop`` = the request was already
satisfied (idempotent); ``refused`` = the verb is illegal from the current state
or its argument failed validation."""

# The serving states in which a fresh full bring-up (``start``) would wrongly
# re-sweep and re-spawn children: workers_permitted(state) is True in exactly
# ``ready``/``degraded``. Kept as the positive predicate so "start is a noop
# while serving" is defined over the same vocabulary the state machine uses.

# States that OWN the control-plane lifecycle themselves (shutdown / guard block
# / maintenance interlock), so a manual start/restart must NOT reach in.
_LIFECYCLE_HELD_STATES: frozenset[str] = frozenset(
    {"stopping", "blocked_wsl_active", "blocked_probe_unavailable", "maintenance"}
)


class AdminActionResult(BaseModel):
    """The structured result of one admin verb, returned as the pipe reply's
    ``data`` payload. ``outcome`` distinguishes a real mutation from an
    idempotent no-op and from a refusal, which the read-only ``state`` alone
    could not."""

    model_config = ConfigDict(extra="forbid")

    verb: str
    outcome: AdminOutcome
    state: str
    detail: str


class AdminSupervisor(Protocol):
    """The narrow slice of ``core.Supervisor`` the admin router drives. A
    Protocol (not the concrete class) so the router's idempotence/refuse logic
    is exercised with a lightweight fake in CI."""

    @property
    def state(self) -> SupervisorState: ...
    def start(self) -> None: ...
    def graceful_stop(self) -> None: ...
    def restart_control_plane(self) -> ChildStartOutcome: ...


SelectorReader = Callable[[], str | None]
"""Read the current D-runtime selector (``"native"``/``"wsl"``), or ``None``
when it is absent/unreadable -- either way not equal to a concrete target, so
runtime_set applies rather than no-ops."""

SelectorWriter = Callable[[RuntimeTarget], None]


class AdminCommandRouter:
    """Routes each authorized admin verb to an idempotent supervisor action.

    Runs on the pipe's single ``CommandQueue`` worker thread (AC-N5) BEHIND the
    admin-tier authorization gate -- it assumes the verb is already authorized
    (the pipe server denied it otherwise) and is responsible only for applying
    it correctly and idempotently.
    """

    def __init__(
        self,
        *,
        supervisor: AdminSupervisor,
        selector_reader: SelectorReader,
        selector_writer: SelectorWriter,
        logger: logging.Logger | None = None,
    ) -> None:
        self._supervisor = supervisor
        self._selector_reader = selector_reader
        self._selector_writer = selector_writer
        self._logger = logger or logging.getLogger(__name__)

    def handle(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply ``command`` and return the :class:`AdminActionResult` payload.
        Fail-closed: a command that is not one of this router's admin verbs
        raises (defence in depth behind the pipe's authz, mirroring
        ``core.command_handler``)."""

        if command in ("stop", "drain"):
            return self._drain(command).model_dump()
        if command == "start":
            return self._start().model_dump()
        if command == "restart":
            return self._restart().model_dump()
        if command == "runtime_set":
            return self._runtime_set(payload).model_dump()
        raise ValueError(
            f"admin router does not serve {command!r}; "
            "read-tier commands are served by core.command_handler"
        )

    # -- stop / drain -----------------------------------------------------

    def _drain(self, verb: str) -> AdminActionResult:
        """RAT-004 graceful drain-all. Idempotent: a drain issued while already
        stopping is a no-op (``stopping`` is the absorbing terminal state in
        states.py, so a second drain would be torn, redundant work)."""

        state = self._supervisor.state
        if state == "stopping":
            return AdminActionResult(
                verb=verb,
                outcome="noop",
                state=state,
                detail="already stopping; graceful drain-all is a no-op",
            )
        self._supervisor.graceful_stop()
        new_state = self._supervisor.state
        self._logger.info("admin %s: graceful drain-all issued (state=%s)", verb, new_state)
        return AdminActionResult(
            verb=verb,
            outcome="applied",
            state=new_state,
            detail="graceful drain-all issued (RAT-004)",
        )

    # -- start ------------------------------------------------------------

    def _start(self) -> AdminActionResult:
        """Bring the station up if it is not already serving. Idempotent: a
        start issued while already serving (``ready``/``degraded``) is a no-op --
        a full ``start()`` there would re-sweep and re-spawn healthy children --
        and (CC-WS5-017) a start issued while the machine is ALREADY mid-bring-up
        (``starting``) is likewise a no-op: the bring-up is in progress, so a
        second full ``start()`` would re-sweep and re-spawn the children coming
        up (a transient double-spawn), which the reconciliation loop would then
        have to unwind. Refused while the lifecycle is held by shutdown, a guard
        block, or the maintenance interlock (those states own the control plane;
        a manual start must not reach in)."""

        state = self._supervisor.state
        if workers_permitted(state):
            return AdminActionResult(
                verb="start",
                outcome="noop",
                state=state,
                detail=f"already serving ({state}); start is a no-op",
            )
        if state == "starting":
            return AdminActionResult(
                verb="start",
                outcome="noop",
                state=state,
                detail="bring-up already in progress (starting); start is a no-op",
            )
        if state in _LIFECYCLE_HELD_STATES:
            return AdminActionResult(
                verb="start",
                outcome="refused",
                state=state,
                detail=f"start refused: {state} owns the control-plane lifecycle",
            )
        self._supervisor.start()
        new_state = self._supervisor.state
        self._logger.info("admin start: bring-up issued (state=%s)", new_state)
        return AdminActionResult(
            verb="start",
            outcome="applied",
            state=new_state,
            detail="bring-up issued",
        )

    # -- restart ----------------------------------------------------------

    def _restart(self) -> AdminActionResult:
        """Guard-gated controlled restart of the control plane. Refused while the
        lifecycle is held (shutdown / guard block / maintenance). Otherwise the
        D9 ``pre_child_start`` guard still governs the writer-capable CP: a
        guard-withheld (or dependency-withheld) restart is reported ``refused``,
        never a false ``applied``. Idempotent-safe: each call performs at most
        one clean controlled restart (no handle accumulation)."""

        state = self._supervisor.state
        if state in _LIFECYCLE_HELD_STATES:
            return AdminActionResult(
                verb="restart",
                outcome="refused",
                state=state,
                detail=f"restart refused: {state} owns the control-plane lifecycle",
            )
        outcome = self._supervisor.restart_control_plane()
        new_state = self._supervisor.state
        if outcome.status == "started_ready":
            self._logger.info("admin restart: control plane restarted (state=%s)", new_state)
            return AdminActionResult(
                verb="restart",
                outcome="applied",
                state=new_state,
                detail=f"control plane restarted: {outcome.detail}",
            )
        self._logger.warning(
            "admin restart: control plane restart withheld (%s): %s",
            outcome.status,
            outcome.detail,
        )
        return AdminActionResult(
            verb="restart",
            outcome="refused",
            state=new_state,
            detail=f"control plane restart withheld ({outcome.status}): {outcome.detail}",
        )

    # -- runtime_set ------------------------------------------------------

    def _runtime_set(self, payload: dict[str, Any]) -> AdminActionResult:
        """Validate + apply the D-runtime selector. Refuses an illegal target (a
        value that is not a known runtime, or a missing one) and NEVER writes it.
        Idempotent: setting the selector to the value it already holds is a
        no-op (no redundant registry write)."""

        target = payload.get("runtime")
        state = self._supervisor.state
        if target not in _RUNTIME_TARGETS:
            return AdminActionResult(
                verb="runtime_set",
                outcome="refused",
                state=state,
                detail=f"runtime_set refused: illegal target {target!r} (expected native/wsl)",
            )
        current = self._selector_reader()
        if current == target:
            return AdminActionResult(
                verb="runtime_set",
                outcome="noop",
                state=state,
                detail=f"runtime selector already {target!r}; no write",
            )
        # ``target`` is a member of _RUNTIME_TARGETS, i.e. a RuntimeTarget.
        self._selector_writer(target)
        self._logger.info("admin runtime_set: selector %r -> %r", current, target)
        return AdminActionResult(
            verb="runtime_set",
            outcome="applied",
            state=state,
            detail=f"runtime selector set {current!r} -> {target!r}",
        )


def build_command_handler(
    supervisor: AdminSupervisor, admin_router: AdminCommandRouter
) -> CommandHandler:
    """Compose the read tier (``supervisor.command_handler``: status/version)
    with the admin tier (``admin_router``) into the single ``CommandHandler``
    the pipe's serialized ``CommandQueue`` runs. Read verbs go to the pure
    snapshot handler; everything else goes to the router (which itself
    fail-closes on a verb it does not serve). Because both tiers run on the ONE
    queue worker, AC-N5's "no two commands interleave" holds across the tiers,
    not just within one."""

    read_handler = supervisor.command_handler  # type: ignore[attr-defined]

    def handler(command: str, payload: dict[str, Any]) -> dict[str, Any]:
        if command in _READ_VERBS:
            result: dict[str, Any] = read_handler(command, payload)
            return result
        return admin_router.handle(command, payload)

    return handler


__all__ = [
    "AdminActionResult",
    "AdminCommandRouter",
    "AdminOutcome",
    "AdminSupervisor",
    "AdminVerb",
    "RuntimeTarget",
    "SelectorReader",
    "SelectorWriter",
    "build_command_handler",
]
