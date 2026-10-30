# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S17 remote-contribution co-process supervision (build step 9 slice 3d).

The self-hosted VDO.Ninja signalling server and the coturn TURN server run as
**separate processes** (S17 §7 — VDO.Ninja stays arms-length AGPL). They join the
same co-process supervision discipline as the NDI relay (``egress/ndi_relay.py``):

* a poll-driven ``ensure_running()`` ticked by a ``ThreadSupervisor`` loop,
* backed-off restarts so a broken binary cannot crash-loop hot,
* identity-safe reaping (``verify_and_kill_process`` — never kill a recycled pid),
* gated by ``CIVICCAST_REMOTE_CONTRIBUTION`` (off by default; a typo must NOT
  silently enable it — the ENG-012 lesson),
* and a fail-closed ``diagnostics()`` (VdoDiagnostics) feeding the operator
  drawer + the URL bridge's diagnostics probe.

A dying VDO/coturn process never takes the channel off-air — the engine swaps
back to program/filler (S17 §6); the supervisor just restarts it and (via an
injected ``alert_hook``, wired to S8 in slice 3d-iii) raises a co-process-down /
TURN-unreachable alert.

The live spawn of the real binaries is an LPM rung-2 proof (S17 §8 item 4); the
supervisor logic is proven here with injected process starters + probes.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shlex
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from civiccast.egress.process_identity import verify_and_kill_process
from civiccast.installer.mediamtx_install import relay_paths, relay_verified
from civiccast.live.contribution.bridge import VdoDiagnostics

_LOG = logging.getLogger(__name__)

CoprocessState = Literal["off", "blocked", "running", "restarting", "stopped"]

# An alert hook: (condition_kind, detail). Wired to S8 record_alert_condition in
# slice 3d-iii; None in the meantime (the supervisor still restarts + logs).
AlertHook = Callable[[str, str], None]

_RESTART_BACKOFF_SECONDS = (5.0, 15.0, 60.0)
_TURN_PROBE_TIMEOUT = 2.0

# S8 condition kinds this tier emits (the strings the alert hub records).
ALERT_COPROCESS_DOWN = "remote-contribution-coprocess-down"
ALERT_TURN_UNREACHABLE = "remote-contribution-turn-unreachable"
ALERT_GUEST_DROP = "remote-contribution-guest-drop"


@dataclass(frozen=True)
class ContributionCoprocessSettings:
    """Environment-driven settings for the VDO.Ninja + coturn co-processes."""

    enabled: bool = False
    vdo_command: tuple[str, ...] | None = None
    coturn_command: tuple[str, ...] | None = None
    mediamtx_command: tuple[str, ...] | None = None
    turn_host: str = "127.0.0.1"
    turn_port: int = 3478
    poll_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> ContributionCoprocessSettings:
        raw = os.environ.get("CIVICCAST_REMOTE_CONTRIBUTION", "off").strip().lower()
        if raw not in ("on", "off"):
            # ENG-012 parity: a typo must never silently flip supervision ON.
            raise ValueError(f"CIVICCAST_REMOTE_CONTRIBUTION must be 'on' or 'off', got {raw!r}.")
        enabled = raw == "on"
        vdo = os.environ.get("CIVICCAST_VDO_COMMAND")
        coturn = os.environ.get("CIVICCAST_COTURN_COMMAND")
        mediamtx = os.environ.get("CIVICCAST_MEDIAMTX_COMMAND")
        relay_command: tuple[str, ...] | None = tuple(shlex.split(mediamtx)) if mediamtx else None
        if relay_command is None and enabled and relay_verified():
            executable, config = relay_paths()
            relay_command = (str(executable), str(config))
        try:
            poll = float(os.environ.get("CIVICCAST_REMOTE_CONTRIBUTION_POLL", "10"))
        except ValueError as exc:
            raise ValueError("CIVICCAST_REMOTE_CONTRIBUTION_POLL must be a number.") from exc
        try:
            turn_port = int(os.environ.get("CIVICCAST_TURN_PORT", "3478"))
        except ValueError as exc:
            raise ValueError("CIVICCAST_TURN_PORT must be an integer.") from exc
        return cls(
            enabled=enabled,
            vdo_command=tuple(shlex.split(vdo)) if vdo else None,
            coturn_command=tuple(shlex.split(coturn)) if coturn else None,
            mediamtx_command=relay_command,
            turn_host=os.environ.get("CIVICCAST_TURN_HOST", "127.0.0.1"),
            turn_port=turn_port,
            poll_seconds=poll,
        )


class CoprocessStatus(BaseModel):
    """Operator-facing state for one supervised co-process."""

    model_config = ConfigDict(extra="forbid")

    name: str
    state: CoprocessState
    pid: int | None = None
    restarts: int = 0
    last_error: str | None = None
    next_step: str = ""


def _default_process_starter(args: list[str]) -> Any:
    return subprocess.Popen(  # noqa: S603 -- fixed args, never shell
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def _process_create_time(pid: int) -> float | None:
    """Best-effort create_time for identity-safe reaping; None if psutil/the
    process is unavailable (stop() then falls back to a plain terminate)."""
    try:
        import psutil

        return psutil.Process(pid).create_time()
    except Exception:
        return None


def _default_turn_probe(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=_TURN_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


class _SupervisedCoprocess:
    """Poll-driven lifecycle for one co-process (VDO.Ninja or coturn).

    ``ensure_running()`` is idempotent and never raises: it returns the current
    status. Restarts back off (5/15/60s). ``stop()`` reaps by verified identity
    (pid + create_time) so a recycled pid is never killed."""

    def __init__(
        self,
        *,
        name: str,
        command: tuple[str, ...] | None,
        process_starter: Callable[[list[str]], Any] | None = None,
        monotonic: Callable[[], float] | None = None,
        alert_hook: AlertHook | None = None,
    ) -> None:
        import time

        self._name = name
        self._command = list(command) if command else None
        self._start_process = process_starter or _default_process_starter
        self._monotonic = monotonic or time.monotonic
        self._alert_hook = alert_hook
        self._process: Any | None = None
        self._created_at: float | None = None
        self._restarts = 0
        self._next_start_at: float | None = None
        self._stopped = False
        self._status = CoprocessStatus(name=name, state="off")

    @property
    def status(self) -> CoprocessStatus:
        return self._status

    @property
    def running(self) -> bool:
        return self._status.state == "running"

    def ensure_running(self) -> CoprocessStatus:
        if self._stopped:
            self._status = self._status.model_copy(update={"state": "stopped", "pid": None})
            return self._status
        if self._command is None:
            self._status = self._status.model_copy(
                update={
                    "state": "blocked",
                    "pid": None,
                    "next_step": f"Set the {self._name} launch command (S3 commissioning).",
                }
            )
            return self._status

        if self._process is not None and self._process.poll() is None:
            self._status = self._status.model_copy(
                update={"state": "running", "pid": self._process.pid, "restarts": self._restarts}
            )
            return self._status

        if self._process is not None:
            # It died — schedule a backed-off restart and alert once.
            if self._next_start_at is None:
                backoff = _RESTART_BACKOFF_SECONDS[
                    min(self._restarts, len(_RESTART_BACKOFF_SECONDS) - 1)
                ]
                self._next_start_at = self._monotonic() + backoff
                self._emit_alert(ALERT_COPROCESS_DOWN, f"{self._name} exited; restart pending.")
            if self._monotonic() < self._next_start_at:
                self._status = self._status.model_copy(
                    update={
                        "state": "restarting",
                        "pid": None,
                        "restarts": self._restarts,
                        "last_error": f"{self._name} process exited; restart pending.",
                    }
                )
                return self._status
            self._process = None
            self._created_at = None
            self._next_start_at = None
            self._restarts += 1

        try:
            self._process = self._start_process(list(self._command))
        except OSError as exc:
            self._status = self._status.model_copy(
                update={
                    "state": "blocked",
                    "pid": None,
                    "last_error": f"{self._name} could not start.",
                    "next_step": str(exc),
                }
            )
            return self._status
        self._created_at = _process_create_time(self._process.pid)
        self._status = self._status.model_copy(
            update={
                "state": "running",
                "pid": self._process.pid,
                "restarts": self._restarts,
                "last_error": None,
                "next_step": "",
            }
        )
        return self._status

    def stop(self) -> None:
        self._stopped = True
        proc = self._process
        if proc is not None and proc.poll() is None:
            if self._created_at is not None:
                verify_and_kill_process(proc.pid, self._created_at)
            else:  # no identity captured — best-effort terminate
                with contextlib.suppress(Exception):
                    proc.terminate()
        self._process = None
        self._created_at = None
        self._status = self._status.model_copy(update={"state": "stopped", "pid": None})

    def _emit_alert(self, kind: str, detail: str) -> None:
        if self._alert_hook is None:
            return
        try:
            self._alert_hook(kind, detail)
        except Exception:
            _LOG.warning("Contribution alert hook failed for %s.", kind)


class ContributionCoprocessSupervisor:
    """Supervises the VDO.Ninja + coturn co-processes and answers diagnostics."""

    def __init__(
        self,
        settings: ContributionCoprocessSettings,
        *,
        process_starter: Callable[[list[str]], Any] | None = None,
        monotonic: Callable[[], float] | None = None,
        turn_probe: Callable[[str, int], bool] | None = None,
        alert_hook: AlertHook | None = None,
        mix_health_check: Callable[[], None] | None = None,
    ) -> None:
        self._settings = settings
        self._turn_probe = turn_probe or _default_turn_probe
        self._alert_hook = alert_hook
        self._mix_health_check = mix_health_check
        self._last_turn_reachable: bool | None = None
        self._vdo = _SupervisedCoprocess(
            name="vdo-ninja",
            command=settings.vdo_command,
            process_starter=process_starter,
            monotonic=monotonic,
            alert_hook=alert_hook,
        )
        self._coturn = _SupervisedCoprocess(
            name="coturn",
            command=settings.coturn_command,
            process_starter=process_starter,
            monotonic=monotonic,
            alert_hook=alert_hook,
        )
        self._mediamtx = _SupervisedCoprocess(
            name="mediamtx",
            command=settings.mediamtx_command,
            process_starter=process_starter,
            monotonic=monotonic,
            alert_hook=alert_hook,
        )

    def ensure_running(self) -> None:
        """One supervision tick: keep both co-processes up, probe TURN.

        The TURN probe runs whenever there's something meaningful to check:
        either the locally-supervised coturn process is up (Linux/macOS,
        ``CIVICCAST_COTURN_COMMAND`` set), OR no local coturn is configured
        at all (the owner-approved "documented external TURN" posture --
        coturn has no native Windows build, see
        ``civiccast/installer/contribution_install.py``). In the second
        case there is deliberately no local process to supervise, but the
        configured external server's reachability is still exactly what
        determines whether a guest behind NAT can join, so it must still be
        probed. It's ONLY skipped while a local coturn IS configured but
        hasn't come up yet (nothing to probe against yet)."""
        self._vdo.ensure_running()
        if self._settings.mediamtx_command is not None:
            self._mediamtx.ensure_running()
        coturn_status = self._coturn.ensure_running()
        if coturn_status.state == "running" or self._settings.coturn_command is None:
            reachable = self._turn_probe(self._settings.turn_host, self._settings.turn_port)
            # Alert only on a transition into unreachable (the hub de-dupes too).
            if not reachable and self._last_turn_reachable is not False:
                self._emit_alert(
                    ALERT_TURN_UNREACHABLE,
                    f"TURN {self._settings.turn_host}:{self._settings.turn_port} is "
                    "unreachable; guests behind NAT cannot connect.",
                )
            self._last_turn_reachable = reachable
        if self._mix_health_check is not None:
            self._mix_health_check()

    def run_forever(self, *, poll_seconds: float, stop_event: Any) -> None:
        """ThreadSupervisor loop shape — tick until stopped, survive exceptions."""
        while not stop_event.is_set():
            try:
                self.ensure_running()
            except Exception:
                _LOG.exception("Contribution co-process supervision tick failed.")
            stop_event.wait(poll_seconds)

    def diagnostics(self) -> VdoDiagnostics:
        if not self._settings.enabled:
            return VdoDiagnostics(
                detail="Remote contribution is disabled (CIVICCAST_REMOTE_CONTRIBUTION=off)."
            )
        vdo_up = self._vdo.running
        coturn_up = self._coturn.running
        external_turn = self._settings.coturn_command is None
        # turn_reachable reflects the most recent probe regardless of whether a
        # LOCAL coturn process is supervised -- under the external-TURN posture
        # (coturn_command is None) there never is one, but the configured
        # external server's reachability is still the load-bearing signal.
        turn_reachable = bool(self._last_turn_reachable)
        parts = [f"vdo={self._vdo.status.state}"]
        parts.append(f"relay={self._mediamtx.status.state}")
        parts.append(
            f"coturn={'external (documented)' if external_turn else self._coturn.status.state}"
        )
        if coturn_up or external_turn:
            if self._last_turn_reachable is None:
                parts.append("turn=not yet probed")
            else:
                parts.append(f"turn={'reachable' if turn_reachable else 'unreachable'}")
        # A healthy station is "vdo up, and either a local coturn is up or TURN
        # is documented-external" -- external posture never being reported as
        # a co-process outage is the whole point of PR #9's decision.
        healthy = vdo_up and (coturn_up or external_turn)
        return VdoDiagnostics(
            turn_reachable=turn_reachable,
            turn_host=self._settings.turn_host,
            turn_port=self._settings.turn_port,
            vdo_process_up=vdo_up,
            coturn_process_up=coturn_up,
            relay_process_up=self._mediamtx.running,
            ice_summary="; ".join(parts),
            detail="" if healthy else "One or more co-processes are not running.",
        )

    def test_turn_connectivity(self) -> VdoDiagnostics:
        """Probe TURN reachability RIGHT NOW (bypassing the poll cadence) and
        return refreshed diagnostics. Does not emit an alert -- an operator-
        initiated test result is surfaced directly in the response, not
        routed through the alert hub."""
        self._last_turn_reachable = self._turn_probe(
            self._settings.turn_host, self._settings.turn_port
        )
        return self.diagnostics()

    def stop(self) -> None:
        self._vdo.stop()
        self._mediamtx.stop()
        self._coturn.stop()

    def _emit_alert(self, kind: str, detail: str) -> None:
        if self._alert_hook is None:
            return
        try:
            self._alert_hook(kind, detail)
        except Exception:
            _LOG.warning("Contribution alert hook failed for %s.", kind)


# --- active-supervisor holder (runtime-only, like ndi_relay's status registry) ---

_ACTIVE_SUPERVISOR: ContributionCoprocessSupervisor | None = None


def set_active_supervisor(supervisor: ContributionCoprocessSupervisor | None) -> None:
    global _ACTIVE_SUPERVISOR
    _ACTIVE_SUPERVISOR = supervisor


def clear_active_supervisor() -> None:
    set_active_supervisor(None)


def contribution_diagnostics_snapshot() -> VdoDiagnostics:
    """The UrlVdoNinjaBridge diagnostics probe — reads the active supervisor, or
    reports honestly that supervision is not running (never a false 'reachable')."""
    sup = _ACTIVE_SUPERVISOR
    if sup is None:
        return VdoDiagnostics(detail="Co-process supervision is not running.")
    return sup.diagnostics()


def contribution_turn_connectivity_test() -> VdoDiagnostics:
    """The UrlVdoNinjaBridge connectivity-test callable — the operator
    console's "Test TURN connectivity" button reaches this through the
    ``POST /api/staff/contribution/diagnostics/turn-test`` route. Runs an
    immediate probe rather than waiting for the next background poll tick."""
    sup = _ACTIVE_SUPERVISOR
    if sup is None:
        return VdoDiagnostics(detail="Co-process supervision is not running.")
    return sup.test_turn_connectivity()


__all__ = [
    "ALERT_COPROCESS_DOWN",
    "ALERT_GUEST_DROP",
    "ALERT_TURN_UNREACHABLE",
    "AlertHook",
    "ContributionCoprocessSettings",
    "ContributionCoprocessSupervisor",
    "CoprocessStatus",
    "clear_active_supervisor",
    "contribution_diagnostics_snapshot",
    "contribution_turn_connectivity_test",
    "set_active_supervisor",
]
