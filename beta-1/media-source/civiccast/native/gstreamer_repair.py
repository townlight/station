# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Operator recovery: repair the GStreamer runtime and restore full egress.

The degraded-mode state machine in
:func:`civiccast.native.station_runtime._resolve_gstreamer_egress_environment`
keeps a channel AIRING on the FFmpeg concat engine when the installed GStreamer
closure is found corrupt at station-environment build time. This module is the
matching RECOVERY action (owner ruling, degraded-mode tier 5): re-stage the
signed closure and restore GStreamer egress, WITHOUT a reinstall.

Two cases, two mechanisms -- the honest split the runtime layout forces:

* **The closure is HEALTHY again on disk** (the common transient cause: an
  on-access AV scanner quarantined a plugin DLL during boot and has since
  released it). No re-stage is needed -- :func:`assess_gstreamer_closure` proves
  it, and GStreamer egress is restored the moment the control-plane child
  environment is RE-DERIVED, because that derivation re-verifies the closure and
  stops injecting the FFmpeg override
  (``station_runtime.load_native_station_environment``). Re-derivation happens
  on a supervisor/service restart (the provider re-runs
  ``station_environment_for_python``); the supervisor's ``restart_control_plane``
  admin verb alone REUSES the env captured at construction and does not
  re-derive it, so it does not by itself flip the engine back -- documented so
  the operator console wires the correct action.

* **The closure has genuinely missing bytes.** Only the installer's signed
  re-stage can rebuild them (`native_repair.rs`), and it is Rust-only: no Python
  path re-verifies an ed25519-signed ``.ccpack`` or re-extracts it. GStreamer
  ships INSIDE the ``native-app-payload`` pack (it is not its own component), so
  "repair the closure" == "repair ``native-app-payload``".
  :func:`trigger_gstreamer_repair` launches that repair.

HAZARD (why the re-stage is launched DETACHED, never awaited in-process): the
installer repair path stops the ``CivicCastSupervisor`` service and
``remove_dir_all``s ``<install_root>/runtime`` -- the very tree whose
``python.exe`` runs the supervisor and this control-plane process (its
``ServiceQuiescenceAuthority`` is a ``TreeRebuildAuthority`` that stops the
service before any destructive rebuild, and it prints a "service stopped, not
restarted" note). A process that awaited it would be deleting the ground under
its own feet. So the repair is spawned as a DETACHED, job-object-breakaway
process that OUTLIVES this one; on the service's next start the provider
re-derives the environment, re-verifies the now-healthy closure, and GStreamer
egress AUTO-RESTORES.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from civiccast.native.station_runtime import reverify_gstreamer_closure
from civiccast.native.supervisor.install_layout import resolve_install_root

_LOG = logging.getLogger(__name__)

#: The Tauri installer/bootstrap binary (``tauri.native.conf.json``
#: ``mainBinaryName = "CivicCast Native"``) that exposes ``--civiccast-repair``.
INSTALLER_BINARY_NAME = "CivicCast Native.exe"

#: The pack component the GStreamer closure is composed into
#: (``native_pack_staging::APP_PAYLOAD_COMPONENT``); repairing it repairs the
#: closure at ``<install_root>/runtime/dependencies/gstreamer``.
APP_PAYLOAD_COMPONENT = "native-app-payload"

REPAIR_FLAG = "--civiccast-repair"
REQUIRE_COMPONENT_FLAG = "--require-component"

#: ``(argv) -> pid``. The detached-spawn seam, injectable for tests.
RepairLauncher = Callable[[list[str]], int]


@dataclass(frozen=True)
class ClosureAssessment:
    """Whether the installed GStreamer closure verifies clean right now."""

    healthy: bool
    detail: str


@dataclass(frozen=True)
class RepairTrigger:
    """The outcome of a recovery attempt -- what was found, and what (if
    anything) was launched to fix it."""

    triggered: bool
    healthy: bool
    installer_binary: Path | None
    argv: tuple[str, ...]
    pid: int | None
    detail: str
    remedy: str = field(default="")


def gstreamer_runtime_root(install_root: Path) -> Path:
    """``<install_root>/runtime`` -- the ``version_root`` that directly contains
    ``dependencies/gstreamer`` (see ``station_runtime`` / ``gstreamer_runtime``)."""

    return install_root / "runtime"


def assess_gstreamer_closure(
    install_root: Path,
    *,
    verifier: Callable[[Path], bool] = reverify_gstreamer_closure,
) -> ClosureAssessment:
    """Re-verify the installed GStreamer closure in place (non-destructive)."""

    root = gstreamer_runtime_root(install_root)
    if not (root / "dependencies" / "gstreamer").is_dir():
        return ClosureAssessment(
            healthy=False,
            detail=f"no GStreamer closure directory at {root / 'dependencies' / 'gstreamer'}",
        )
    if verifier(root):
        return ClosureAssessment(healthy=True, detail=f"GStreamer closure at {root} verifies clean")
    return ClosureAssessment(
        healthy=False,
        detail=f"GStreamer closure at {root} is still corrupt or partial",
    )


def resolve_installer_binary(install_root: Path) -> Path | None:
    """Locate ``CivicCast Native.exe`` (the install root, then its parent)."""

    for candidate in (
        install_root / INSTALLER_BINARY_NAME,
        install_root.parent / INSTALLER_BINARY_NAME,
    ):
        if candidate.is_file():
            return candidate
    return None


def build_repair_command(
    installer_binary: Path,
    install_root: Path,
    *,
    installer_dir: Path | None = None,
) -> list[str]:
    """The scoped one-shot repair argv:
    ``"CivicCast Native.exe" --civiccast-repair <root> --require-component native-app-payload``.
    ``--require-component`` scopes the repair to the app-payload/GStreamer
    closure rather than the full ``DEFAULT_REQUIRED_COMPONENTS`` set."""

    argv = [
        str(installer_binary),
        REPAIR_FLAG,
        str(install_root),
        REQUIRE_COMPONENT_FLAG,
        APP_PAYLOAD_COMPONENT,
    ]
    if installer_dir is not None:
        argv += ["--installer-dir", str(installer_dir)]
    return argv


def _detached_launch(argv: list[str]) -> int:
    """Spawn the repair DETACHED so it outlives this process, breaking away
    from the supervisor's kill-on-close Job Object (else the job teardown the
    repair itself triggers would take the repair down with it). Windowless, own
    process group, no inherited std handles. ``getattr(..., 0)`` keeps every
    Windows-only creation flag a harmless no-op off Windows."""

    creationflags = 0
    for name in (
        "DETACHED_PROCESS",
        "CREATE_NEW_PROCESS_GROUP",
        "CREATE_BREAKAWAY_FROM_JOB",
        "CREATE_NO_WINDOW",
    ):
        creationflags |= getattr(subprocess, name, 0)
    proc = subprocess.Popen(  # noqa: S603
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )
    return int(proc.pid)


def trigger_gstreamer_repair(
    *,
    install_root: Path | None = None,
    verifier: Callable[[Path], bool] = reverify_gstreamer_closure,
    binary_resolver: Callable[[Path], Path | None] = resolve_installer_binary,
    launcher: RepairLauncher = _detached_launch,
) -> RepairTrigger:
    """Repair the GStreamer runtime and restore full egress (recovery tier 5).

    Re-verifies the closure first. If it is already HEALTHY, launches NOTHING
    (no destructive re-stage) and reports that GStreamer egress restores when
    the control-plane environment is re-derived on a supervisor restart. If it
    is still corrupt, launches the installer's signed, scoped re-stage DETACHED
    (see the module hazard note) and reports it. Never raises: a recovery action
    that faulted must report, not crash the caller (a staff endpoint)."""

    root = install_root if install_root is not None else resolve_install_root()

    assessment = assess_gstreamer_closure(root, verifier=verifier)
    if assessment.healthy:
        detail = (
            f"{assessment.detail}; no re-stage needed. GStreamer egress restores on the "
            "next control-plane environment re-derivation (a supervisor/service restart "
            "re-verifies the closure and stops injecting the FFmpeg override)."
        )
        _LOG.info("GStreamer recovery: %s", detail)
        return RepairTrigger(
            triggered=False,
            healthy=True,
            installer_binary=None,
            argv=(),
            pid=None,
            detail=detail,
            remedy="already-healthy",
        )

    binary = binary_resolver(root)
    if binary is None:
        detail = (
            f"installer binary {INSTALLER_BINARY_NAME!r} not found under {root}; cannot "
            "re-stage the signed closure on-box. Re-run the full installer to repair."
        )
        _LOG.error("GStreamer recovery not triggered: %s", detail)
        return RepairTrigger(
            triggered=False,
            healthy=False,
            installer_binary=None,
            argv=(),
            pid=None,
            detail=detail,
            remedy="installer-missing",
        )

    argv = build_repair_command(binary, root)
    try:
        pid = launcher(argv)
    except Exception as exc:
        detail = f"failed to launch GStreamer runtime repair: {exc}"
        _LOG.exception("GStreamer recovery launch failed")
        return RepairTrigger(
            triggered=False,
            healthy=False,
            installer_binary=binary,
            argv=tuple(argv),
            pid=None,
            detail=detail,
            remedy="launch-failed",
        )

    detail = (
        f"GStreamer runtime repair launched detached (pid {pid}). It stops the "
        "CivicCastSupervisor service, re-stages the signed native-app-payload closure "
        "from the on-disk pack, and on the service's next start the environment is "
        "re-derived, the healthy closure re-verifies, and GStreamer egress auto-restores. "
        "No reinstall."
    )
    _LOG.warning("GStreamer recovery: %s", detail)
    return RepairTrigger(
        triggered=True,
        healthy=False,
        installer_binary=binary,
        argv=tuple(argv),
        pid=pid,
        detail=detail,
        remedy="restage-launched",
    )
