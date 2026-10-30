# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The Windows service layer for the native supervisor (spec D1/D8).

This module is the OS-facing shim that the Windows Service Control Manager
drives. It owns four things and NOTHING that belongs to the pure ``core.py``
state machine:

* **The pywin32 ``ServiceFramework`` shim (D1/D8).** ``build_service_class``
  lazily constructs the ``CivicCastSupervisorService`` subclass so that
  ``import civiccast.native.supervisor.service`` succeeds on Linux (no
  ``win32serviceutil``/``servicemanager``/``win32service`` at module import --
  they are imported inside the factory, only ever called on Windows). The
  service's ``SvcDoRun`` acquires the singleton, configures logging, and runs a
  :class:`SupervisorService`; ``SvcStop`` requests a graceful stop.
* **The ``Global\\CivicCastSupervisorSingleton`` mutex.** Exactly ONE supervisor
  per station. The mutex REUSES ``win_probes.RuntimeOwnerMutex`` (the WS4 empirical
  Win32 path -- the DACL check happens on a second open, the abandoned-mutex
  classification, the handle-lifetime discipline) rather than hand-rolling a new
  ``CreateMutex`` path; it is parametrised with the singleton identity
  (``config.SINGLETON_MUTEX_NAME``) and the SAME restrictive SDDL as WS4's
  runtime-owner mutex (``config.SINGLETON_MUTEX_SDDL`` == ``MUTEX_SDDL``);
  its DACL grants SYSTEM, Administrators and the CivicCast supervisor service
  SID, without granting access to other local accounts.
* **The graceful stop chain (D5 graceful-stop + RAT-004 drain-all).**
  :meth:`SupervisorService.graceful_stop_all` stops children in reverse startup
  order and issues each child its OWN graceful action first
  (``children.graceful_stop_action`` via the runner): the control-plane child
  (stopped first) gets ``CTRL_BREAK`` so its uvicorn lifespan runs the daemon's
  ``stop_all_channels`` drain (RAT-004), while postgres gets its
  command-based stop (pg_ctl fast stop). Each
  child then has a 15s deadline (``config.graceful_stop_deadline_seconds``) to
  exit before ``TerminateProcess`` (D5). The Job Object kill-on-close remains the
  final backstop.
* **Logging.** A rotating ``supervisor.log`` (10 MiB x 10) plus per-child log
  paths under ``ProgramData\\CivicCast\\logs``.

Everything side-effecting is injected, so the stop-chain ordering, the
per-child deadline/terminate logic, the runner branching, the logging config,
and the singleton construction all run under pure fakes on Linux
(``tests/native/test_supervisor_service.py``); the real singleton mutex SDDL
readback and the ``ServiceFramework`` class SHAPE are proven against real Win32
in ``tests/native/test_supervisor_service_win.py``.

DISCLOSED SEAMS (owned by a higher integration layer, not this build unit):
* ``build_production_service`` binds the Windows-real seams it CAN (the child
  runner, the Job Object API, the interlock reader, the wall clock) but takes
  the guard monitor, the alerting outbox, and the readiness probes as injected
  parameters -- the guard's lifecycle, the alerting ``AlertConditionKind`` +
  live SQLAlchemy ``Session`` binding (which ``core.py`` explicitly deferred to
  this layer), and the concrete DB/socket/HTTP readiness probes are cross-module
  decisions outside this unit's file ownership.
* ``graceful_stop_all`` calls ``supervisor.graceful_stop()`` at the end for the
  state transition + explicit Job Object close. That method re-issues one
  ``CTRL_BREAK`` to the already-drained (and likely dead) control-plane handle
  before closing the job. This is made SAFE by the runner's ``_safe_ctrl_break``
  (a ``GenerateConsoleCtrlEvent`` against a gone process group would otherwise
  ``OSError`` and crash the stop path), but it is still redundant work -- the
  clean fix is a bare ``close_job`` seam on ``core.Supervisor``, which file
  ownership forbids adding here. Recommended follow-up.
"""

from __future__ import annotations

import contextlib
import json
import logging
import logging.handlers
import os
import random
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from civiccast.db import connect_options
from civiccast.db.url import normalize_database_url
from civiccast.native.network import (
    NetworkSettings,
    NetworkSettingsStore,
    default_network_settings_path,
)
from civiccast.native.pg_ctl_exec import run_captured_argv
from civiccast.native.runtime_guard import GuardMonitor
from civiccast.native.supervisor.admin import (
    AdminCommandRouter,
    RuntimeTarget,
    build_command_handler,
)
from civiccast.native.supervisor.children import (
    ChildSpec,
    ControlPlaneHealthProbe,
    GracefulStopKind,
    OllamaChildDecision,
    graceful_stop_action,
    ollama_child_spec,
    postgres_child_spec,
    process_cpu_seconds,
    read_postmaster_pid,
)
from civiccast.native.supervisor.config import (
    CONTROL_PIPE_NAME,
    DISPLAY_NAME,
    SERVICE_NAME,
    SINGLETON_MUTEX_NAME,
    SINGLETON_MUTEX_SDDL,
    STARTUP_ORDER,
    SupervisorConfig,
)
from civiccast.native.supervisor.core import (
    AlertOutbox,
    ChildHandle,
    GuardLike,
    Supervisor,
)
from civiccast.native.supervisor.install_layout import (
    InstallLayout,
    default_log_root,
    ollama_model_store_candidates,
    resolve_install_layout,
)
from civiccast.native.supervisor.job_object import Win32JobObjectApi
from civiccast.native.supervisor.pipe_server import (
    CommandHandler,
    CommandQueue,
    Dispatcher,
    PipeCreateResult,
    PipeServer,
)
from civiccast.native.win_probes import (
    RuntimeOwnerMutex,
    detect_wsl_install,
    probe_indistro_services,
    probe_keeper,
    read_interlock,
    read_selector,
)
from civiccast.platform.child_logs import (
    CHILD_LOG_BACKUP_COUNT as _SHARED_CHILD_LOG_BACKUP_COUNT,
)
from civiccast.platform.child_logs import (
    CHILD_LOG_MAX_BYTES as _SHARED_CHILD_LOG_MAX_BYTES,
)
from civiccast.platform.child_logs import (
    CHILD_LOG_READ_SIZE as _SHARED_CHILD_LOG_READ_SIZE,
)
from civiccast.platform.child_logs import (
    BestEffortRotatingFileHandler as _SharedBestEffortRotatingFileHandler,
)
from civiccast.platform.child_logs import (
    drain_child_output as _shared_drain_child_output,
)

if TYPE_CHECKING:
    from civiccast.alerting.models import AlertConditionKind

_CONTROL_PLANE = "control_plane"
# Task #57 D2: the OPTIONAL local-AI runtime child (core._OLLAMA_CHILD's
# service-layer twin). Not in STARTUP_ORDER -- see core.py.
_OLLAMA_CHILD = "ollama"

# CC-WS5-003 / audit A1: the postmaster.pid file and the ``pg_ctl -D`` target
# must be the SAME directory. These RELATIVE defaults exist ONLY for direct
# unit-test construction of ``Win32ChildProcessRunner``; the PRODUCTION wiring
# (``build_production_service``) always derives ABSOLUTE paths from
# ``install_layout.resolve_install_layout`` -- under LocalSystem (CWD System32,
# stock PATH) a bare ``pg_ctl`` is FileNotFoundError and a relative ``pgdata``
# resolves against System32. (The old comment here claimed the absolute wiring
# was "Worker C's service build"; it had never landed -- audit A1 closed it.)
_POSTGRES_DATA_DIR = "pgdata"
_PG_CTL_PATH = "pg_ctl"

# Process access rights for the durable-postmaster handle opened by
# open_existing(): PROCESS_TERMINATE (0x0001) | PROCESS_SET_QUOTA (0x0100)
# | PROCESS_QUERY_INFORMATION (0x0400) -- mirrors job_object._PROCESS_ACCESS_FOR_JOB
# so the same handle can be monitored (GetExitCodeProcess), terminated, and is
# assign-capable. PROCESS_QUERY_INFORMATION is what poll() needs.
_PROCESS_ACCESS_FOR_POSTMASTER = 0x0001 | 0x0100 | 0x0400

# GetExitCodeProcess returns this while the process is still running.
_STILL_ACTIVE = 259

SERVICE_DESCRIPTION = (
    "Supervises the CivicCast native Windows runtime: postgres and the "
    "control-plane daemon, contained in a Job Object and gated by the WSL/native "
    "runtime interlock."
)

# ---------------------------------------------------------------------------
# Logging (rotating supervisor.log + per-child logs under ProgramData)
# ---------------------------------------------------------------------------

# Audit A1: the WELL-KNOWN default, kept as a constant for reference/export
# compatibility. The functions below resolve the log root at CALL time via
# ``install_layout.default_log_root()`` so the ``PROGRAMDATA`` env var is
# honored (this constant previously hardcoded the drive-letter path and
# diverged from every other ProgramData consumer in the package).
DEFAULT_LOG_ROOT = Path(r"C:\ProgramData\CivicCast\logs")
SUPERVISOR_LOG_NAME = "supervisor.log"
LOG_MAX_BYTES = _SHARED_CHILD_LOG_MAX_BYTES
LOG_BACKUP_COUNT = _SHARED_CHILD_LOG_BACKUP_COUNT
# Raw control-plane and PostgreSQL stdio is drained by the supervisor so the
# child can keep writing while Windows rotates the supervisor-owned file.
CHILD_LOG_MAX_BYTES = LOG_MAX_BYTES
CHILD_LOG_BACKUP_COUNT = LOG_BACKUP_COUNT
CHILD_LOG_READ_SIZE = _SHARED_CHILD_LOG_READ_SIZE
_ROTATED_CHILD_LOGS = frozenset({"control_plane", "postgres"})
LOGGER_NAME = "civiccast.native.supervisor"
#: The package-root logger ``configure_logging`` ALSO wires to supervisor.log.
#: Field finding (2026-08-30, real box): library code the supervisor host
#: process calls directly -- concretely ``station_runtime``'s orphaned-caption-
#: tier degrade WARNING inside ``default_dependency_provider`` ->
#: ``station_environment_for_python`` -- logs under its own module logger
#: (``civiccast.native.station_runtime``), which is NOT a descendant of
#: ``LOGGER_NAME``. Those records propagated to the root logger, which has no
#: handler in the service process (and a Windows service has no visible
#: stderr for ``lastResort``), so an operator-critical degrade warning reached
#: NO on-disk log. Handling the package root catches every ``civiccast.*``
#: record emitted in this process while children keep their own log files.
PACKAGE_LOGGER_NAME = "civiccast"

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def child_log_path(name: str, *, log_root: Path | str | None = None) -> Path:
    """The per-child log file path under ``<PROGRAMDATA>\\CivicCast\\logs`` (or
    an injected root). The production child runner redirects a child's
    stdout/stderr here; kept as a pure helper so the path convention is
    independently testable."""

    root = Path(log_root) if log_root is not None else default_log_root()
    return root / f"{name}.log"


_BestEffortRotatingFileHandler = _SharedBestEffortRotatingFileHandler


def _drain_child_output(stream: Any, handler: logging.Handler) -> None:
    """Continuously empty a child's merged stdout/stderr pipe into its sink.

    This runs on a daemon thread for the full lifetime of the inherited pipe,
    which can outlive the short-lived pg_ctl launcher. Bounded reads and no
    queue keep memory use fixed; output errors are discarded after the read so
    they cannot fill the OS pipe and stall the child. The service stop path
    never joins this thread.
    """

    _shared_drain_child_output(
        stream,
        handler,
        read_size=CHILD_LOG_READ_SIZE,
        logger_name="civiccast.native.supervisor.child_output",
    )


class _DurableRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A ``RotatingFileHandler`` that ``fsync``s the file after every record.

    ADJACENT DEFECT (2026-08-12, TESTER2 b5 evidence): during TESTER2's b5
    run, ``supervisor.log`` was independently observed at 0 bytes across a
    5+ hour service run -- yet the tester's evidence separately quotes ONE
    verbatim, correctly-formatted supervisor line (the ollama-skip WARNING),
    so it demonstrably reached a sink SOMEWHERE. The plain
    ``RotatingFileHandler`` this replaces calls ``flush()`` after every
    ``emit()`` (inherited from ``StreamHandler``), which pushes Python's
    OWN userspace buffer to the OS -- but never asks the OS to publish that
    write to the file's externally-visible size/extent. On Windows, NTFS
    updates a growing file's SIZE metadata in its directory entry lazily
    for a long-lived open handle; a separate process querying size via
    directory enumeration (which is what a simple "how many bytes is this
    file" check does -- e.g. ``Get-Item -Length`` / ``FileInfo.Length`` /
    ``os.path.getsize`` without opening the file) can see 0 (or a stale,
    smaller value) for a handle that stays open for a run's entire
    lifetime, exactly the shape ``supervisor.log`` has (the handler is
    never closed until ``logging.shutdown()`` at service stop). This
    explains BOTH halves of the evidence at once: the WARNING was written
    correctly (retrievable by any tool that actually reads the file's
    content rather than just its cached size, and by anyone who inspected
    it before the handle had been open long), and every SIZE-based
    zero-byte check taken later in the same run is consistent with the
    handle simply having stayed open the whole time.

    ``fsync`` (Windows: ``FlushFileBuffers`` via the C runtime) forces the
    OS to publish the write immediately, closing that gap. Negligible cost
    for a WARNING/INFO-dominated stream; the alternative -- closing and
    reopening the file handle after every record -- would cost far more and
    still race the same rotation logic this class inherits unmodified."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        stream = self.stream
        if stream is None:
            return
        # Best-effort durability step; never let it crash a log call (e.g. a
        # stream mid-close during shutdown, or fsync being unsupported for
        # this stream on the current platform).
        with contextlib.suppress(OSError, ValueError):
            os.fsync(stream.fileno())


def _attach_rotating_file_logger(
    *,
    log_root: Path | str | None,
    log_file_name: str,
    logger_names: tuple[str, ...],
) -> logging.Logger:
    """Shared implementation behind :func:`configure_logging` and
    :func:`configure_control_plane_logging`: attach ONE
    :class:`_DurableRotatingFileHandler` (10 MiB x 10, ``fsync``'d per
    record) at INFO to every logger named in ``logger_names``, creating
    ``log_root`` if needed. Idempotent: re-running replaces the handler(s) a
    prior call left behind rather than stacking a new one each call. Returns
    the first logger in ``logger_names``."""

    root = Path(log_root) if log_root is not None else default_log_root()
    root.mkdir(parents=True, exist_ok=True)

    loggers = [logging.getLogger(name) for name in logger_names]
    for logger in loggers:
        logger.setLevel(logging.INFO)
        logger.propagate = False
    # Replace any handlers a prior call left behind (idempotent). The ONE
    # handler instance is shared by every logger in ``loggers`` (below), so
    # it is removed from each before being closed exactly once.
    for owner in loggers:
        for handler in list(owner.handlers):
            owner.removeHandler(handler)
            if not any(handler in other.handlers for other in loggers):
                handler.close()

    file_handler = _DurableRotatingFileHandler(
        root / log_file_name,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    for logger in loggers:
        logger.addHandler(file_handler)
    return loggers[0]


def configure_logging(*, log_root: Path | str | None = None) -> logging.Logger:
    """Configure the supervisor's rotating file logger (10 MiB x 10) under the
    log root, creating the directory if needed. Idempotent: re-running replaces
    the logger's handlers rather than stacking a new file handler each call.

    Uses :class:`_DurableRotatingFileHandler` so every record is ``fsync``'d
    immediately -- see that class's docstring for the diagnosability defect
    this closes.

    Wires BOTH ``LOGGER_NAME`` (``civiccast.native.supervisor``) and
    ``PACKAGE_LOGGER_NAME`` (``civiccast``) to the SAME handler instance
    (never a second handler opening the same file, which would race rotation
    renames on Windows), so library records emitted in the supervisor HOST
    process -- e.g. station_runtime's orphaned-caption-tier degrade WARNING
    -- reach ``supervisor.log`` instead of a handlerless root logger. No
    duplicate lines: ``LOGGER_NAME``'s records stop at its own handler
    (``propagate=False``) and never climb to the package root.

    This function is called ONLY by the supervisor HOST process
    (``service_host.py``'s ``SvcDoRun``) -- the control-plane CHILD process
    (``python -m uvicorn civiccast.app:create_app``) is a separate OS
    process with its own unconfigured root logger and needs
    :func:`configure_control_plane_logging` instead; see that function's
    docstring for the diagnosability gap this split closes."""

    return _attach_rotating_file_logger(
        log_root=log_root,
        log_file_name=SUPERVISOR_LOG_NAME,
        logger_names=(LOGGER_NAME, PACKAGE_LOGGER_NAME),
    )


#: The control-plane child's OWN log file name -- deliberately distinct from
#: ``control_plane.log`` (``child_log_path("control_plane")``), which is the
#: child runner's raw OS-level redirect of this SAME process's stdout/stderr
#: (uvicorn's own access log). A SECOND ``RotatingFileHandler`` opening THAT
#: same path from inside the child would race the OS redirect's open handle
#: on Windows every time it rotates (the redirect handle can block the
#: rename); a distinct file name sidesteps that entirely.
CONTROL_PLANE_LOG_NAME = "control_plane-app.log"


def configure_control_plane_logging(*, log_root: Path | str | None = None) -> logging.Logger:
    """Configure the ``civiccast`` package logger's INFO file handler for the
    CONTROL-PLANE CHILD process -- the ``python -I -u -m uvicorn
    civiccast.app:create_app --factory`` process
    ``children.control_plane_child_spec`` spawns. Called from
    ``civiccast.app.create_app`` (the uvicorn factory entrypoint), guarded so
    it only runs when the process was actually launched by the supervisor --
    see that module's ``_maybe_configure_control_plane_logging``.

    Bug this closes (Gate A T4 diagnosis, 2026-09): :func:`configure_logging`
    is called ONLY by the supervisor HOST process
    (``service_host.py``'s ``SvcDoRun``); the control-plane child is a
    SEPARATE OS process with its own unconfigured root logger, so every
    ``civiccast.*`` INFO record the egress daemon and the FastAPI app emit
    (encoder command lines, pipeline state transitions, FALLBACK_SLATE
    reasons, ``last_error``) was silently dropped -- only WARNING+ reached
    ``control_plane.log``, and only via Python's handlerless-root
    ``lastResort`` writer. Gate A's T4 probe found
    ``engine_state=FALLBACK_SLATE`` on both beta.3 and beta.4 with no
    diagnostic trail explaining why.

    Writes to :data:`CONTROL_PLANE_LOG_NAME` (``control_plane-app.log``),
    NOT ``control_plane.log`` -- see that constant's docstring for why they
    must stay separate files."""

    return _attach_rotating_file_logger(
        log_root=log_root,
        log_file_name=CONTROL_PLANE_LOG_NAME,
        logger_names=(PACKAGE_LOGGER_NAME,),
    )


# ---------------------------------------------------------------------------
# Singleton mutex (REUSES win_probes.RuntimeOwnerMutex; no hand-rolled path)
# ---------------------------------------------------------------------------


def build_singleton_mutex(
    *, name: str = SINGLETON_MUTEX_NAME, sddl: str = SINGLETON_MUTEX_SDDL
) -> RuntimeOwnerMutex:
    """The ``Global\\CivicCastSupervisorSingleton`` mutex, built by REUSING the
    WS4 ``RuntimeOwnerMutex`` (its empirical Win32 cautions and handle-lifetime
    discipline) parametrised with the singleton identity and the restrictive
    SYSTEM+Administrators+service-SID DACL. ``name``/``sddl`` are injectable ONLY so a
    ``*_win.py`` test can exercise the real SDDL readback under a unique object
    name without contending for the real global singleton."""

    return RuntimeOwnerMutex(name=name, sddl=sddl)


# ---------------------------------------------------------------------------
# Concrete child-process runner (the service layer's real ChildProcessRunner)
# ---------------------------------------------------------------------------

PopenFactory = Callable[[ChildSpec, bool], Any]
CtrlBreakSender = Callable[[int], None]
StopCommandRunner = Callable[[list[str]], None]
# CC-WS5-003: open a poll/terminate-capable process object for an already-running
# pid (the durable PostgreSQL postmaster). Injectable so the runner's open_existing
# LOGIC is CI-testable on Linux with a fake; the default binds to win32api.OpenProcess.
OpenProcessFactory = Callable[[int], Any]


def _file_backed_popen_factory(
    spec: ChildSpec, new_process_group: bool, *, log_root: Path | str
) -> Any:
    """G3: the production spawn: ``subprocess.Popen`` of the child's argv with
    its env overlaid on the current environment, with stdout+stderr captured
    under the per-child log path (:func:`child_log_path`). When
    ``new_process_group`` (the control-plane child, per
    ``spec.new_process_group``) the child is given its OWN process group
    (``CREATE_NEW_PROCESS_GROUP``) so a ``CTRL_BREAK`` can later drain only
    its tree (RAT-004) without signalling the supervisor.

    ``control_plane`` and ``postgres`` stdout/stderr are captured through a
    continuously drained pipe and a supervisor-owned rotating file. This is
    necessary because an open redirect handle prevents safe log rotation on
    Windows. The daemon drainer reads fixed-size chunks, never queues output,
    continues reading through sink errors, and is not joined by the stop path.
    PostgreSQL's ``pg_ctl start`` passes its standard handles to the durable
    postmaster, so ``postgres.log`` continues receiving server output after
    the short-lived launcher exits. Other children retain direct file-backed
    capture because their logs are outside this bounded-growth change.

    WINDOWLESS (F-13, sandbox newcomer re-walk dd7f835f, 2026-08-01): every
    child also gets ``CREATE_NO_WINDOW``. The re-walk found a blank black
    console window titled ``...\\runtime\\python.exe`` left on the operator's
    desktop after install -- and it was not a stray helper, it WAS the live
    control plane (the uvicorn process serving 127.0.0.1:8000); closing it
    killed the station's API. A LocalSystem service has no console of its own
    for a child to inherit, so Windows allocates a NEW, visible one for a
    console-subsystem executable, and ``install_layout.resolve_install_layout``
    resolves ``python_path`` to ``<INSTDIR>\\runtime\\python.exe`` (console
    subsystem), not ``pythonw.exe``.

    Fixed HERE rather than by switching the interpreter to ``pythonw.exe``,
    deliberately: ``pythonw.exe`` has no valid stdout/stderr handles at all,
    which would silently defeat the output capture immediately above -- the
    exact regression G3 landed to prevent. ``CREATE_NO_WINDOW`` suppresses
    only the WINDOW; the child keeps the inherited pipe or file handles.
    ``getattr(..., 0)`` for the same reason as every other spawn site in this
    repo (``pg_ctl_exec``, ``installer/service``, ``provision/seams``,
    ``stream/_ffmpeg``, ``egress/gst/strategy``, ``certs/authority``,
    ``captions/runtime``): the constant does not exist off Windows, where the
    concept does not apply.

    Applies to EVERY child, not just the control plane. postgres.exe
    and ollama.exe are console-subsystem executables too; the
    re-walk only caught the control plane because that is the one whose window
    the operator was left looking at.

    ``CREATE_NO_WINDOW`` is retained for all children. ``stderr`` is merged
    into ``stdout`` for the bounded children so a single reader drains both
    streams and rotation has one owner.
    """

    import os
    import subprocess

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if new_process_group:
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    env = {**os.environ, **spec.env}
    log_path = child_log_path(spec.name, log_root=log_root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if spec.name in _ROTATED_CHILD_LOGS:
        handler = _BestEffortRotatingFileHandler(
            log_path,
            maxBytes=CHILD_LOG_MAX_BYTES,
            backupCount=CHILD_LOG_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        try:
            proc = subprocess.Popen(  # noqa: S603
                spec.argv,
                env=env,
                cwd=spec.cwd,
                creationflags=creationflags,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
            )
        except BaseException:
            handler.close()
            raise
        if proc.stdout is None:
            handler.close()
            with contextlib.suppress(Exception):
                proc.terminate()
            raise RuntimeError(f"stdout pipe missing for child {spec.name}")
        drainer = threading.Thread(
            target=_drain_child_output,
            args=(proc.stdout, handler),
            name=f"civiccast-log-{spec.name}",
            daemon=True,
        )
        try:
            drainer.start()
        except BaseException:
            handler.close()
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.stdout.close()
            raise
        # Kept on the Popen object for diagnostics and tests. Production stop
        # never waits for it: grandchildren can inherit the pipe handles.
        cast(Any, proc)._civiccast_log_drainer = drainer
        return proc

    log_handle = log_path.open("ab")  # closed in the finally block below
    try:
        return subprocess.Popen(  # noqa: S603
            spec.argv,
            env=env,
            cwd=spec.cwd,
            creationflags=creationflags,
            stdout=log_handle,
            stderr=log_handle,
        )
    finally:
        log_handle.close()


def _default_ctrl_break_sender(pid: int) -> None:
    """Deliver ``CTRL_BREAK`` to the process group whose leader is ``pid`` --
    the RAT-004 drain signal for the control-plane child (spawned with its own
    process group by :func:`_file_backed_popen_factory`)."""

    import os
    import signal

    os.kill(pid, signal.CTRL_BREAK_EVENT)


class _OpenedProcess:
    """CC-WS5-003: a ``Popen``-shaped view over a raw Win32 handle to an
    ALREADY-RUNNING process (the durable postmaster whose ``pg_ctl -w`` launcher
    self-exited). Exposes the ``pid`` / ``poll()`` / ``terminate()`` trio
    :class:`_ProcHandle` reaches through, so an opened postmaster handle is
    monitored and force-stopped by the exact same code path as a spawned child.
    The Win32 calls are lazily imported so this module still imports on Linux."""

    def __init__(self, pid: int, handle: Any) -> None:
        self._pid = pid
        self._handle = handle

    @property
    def pid(self) -> int:
        return self._pid

    def poll(self) -> int | None:
        """``None`` while the postmaster is alive, else its exit code -- the
        ``subprocess.Popen.poll`` contract ``_ProcHandle.is_alive`` relies on."""

        import win32process

        code = int(win32process.GetExitCodeProcess(self._handle))
        return None if code == _STILL_ACTIVE else code

    def terminate(self) -> None:
        """``TerminateProcess`` -- the D5 backstop force-stop for the postmaster
        (the graceful ``pg_ctl stop -m fast`` is issued first via the spec)."""

        import win32api

        win32api.TerminateProcess(self._handle, 1)


def _default_open_process(pid: int) -> _OpenedProcess:
    """Open a durable, monitor/terminate/assign-capable handle to ``pid`` via
    ``win32api.OpenProcess`` with :data:`_PROCESS_ACCESS_FOR_POSTMASTER` (mirrors
    ``job_object._PROCESS_ACCESS_FOR_JOB``). Lazily imported so this module
    imports on Linux; exercised against a real process on the owner VM."""

    import win32api

    handle = win32api.OpenProcess(_PROCESS_ACCESS_FOR_POSTMASTER, False, pid)
    return _OpenedProcess(pid, handle)


_STOP_COMMAND_OUTPUT_TAIL_CHARS = 2000
"""Cap on the stop-command output logged in G4(b)'s WARNING -- long enough to
carry a real diagnostic, bounded so a
runaway/chatty command can never flood the log."""

STOP_COMMAND_TIMEOUT_SECONDS = 10.0
"""Hard deadline for one child's graceful stop COMMAND
(:func:`_default_stop_command_runner`: ``pg_ctl stop -m fast``). Named, not
inlined, because it is also a term in the
stop watchdog's derivation (:data:`SVC_STOP_WATCHDOG_SECONDS`) -- a derivation
that restates its inputs as literals is a derivation that drifts."""


def _default_stop_command_runner(argv: list[str]) -> None:
    """Run a child's command-based graceful stop (``argv`` kind): postgres's
    ``pg_ctl stop -m fast``. Bounded so
    a hung stop command never blocks the D5 deadline loop that follows it.

    G4(b): the returncode is CAPTURED and CHECKED. Before that, the subprocess
    result was discarded entirely (``check=False`` and the return value never
    read), so a stop command that FAILED looked IDENTICAL, in every log, to
    one that actually worked. This does not change control flow: the existing D5
    deadline-then-``TerminateProcess`` fallback in ``_stop_child`` runs exactly
    as before regardless of this command's outcome (a WARNING here is diagnostic
    only, never a decision).

    F4 (2026-07-31): the capture is FILE-BACKED
    (``pg_ctl_exec.run_captured_argv``), not ``capture_output=True``. With
    pipes, ``subprocess.run``'s Windows timeout path does ``kill()`` and then an
    UNTIMED ``communicate()``, which blocks until every inherited write-end of
    those pipes is closed -- a grandchild holding one (``pg_ctl`` spawns the
    postmaster) turns the documented 10s bound
    into an unbounded wait, on the stop chain, inside the stop watchdog's
    budget. ``run_captured_argv`` gives the child real temp FILES (no pipe
    handles exist at all, so nothing can be waited on), bounds it with
    ``wait(timeout=...)`` against the process handle only, and kills the whole
    tree on expiry before re-raising ``TimeoutExpired`` -- which ``_stop_child``
    already classifies as a failed graceful action and escalates to
    ``TerminateProcess`` (audit A5)."""

    result = run_captured_argv(argv, timeout_seconds=STOP_COMMAND_TIMEOUT_SECONDS)
    if result.returncode != 0:
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        logging.getLogger(LOGGER_NAME).warning(
            "stop command %r exited %d (nonzero): %s",
            argv,
            result.returncode,
            output.strip()[-_STOP_COMMAND_OUTPUT_TAIL_CHARS:] or "<no output>",
        )


class _ProcHandle:
    """A live child handle wrapping the concrete ``Popen`` object and the
    ``ChildSpec`` it was spawned from (so the graceful-stop action can be
    resolved at stop time). Satisfies ``core.ChildHandle`` (exposes ``pid``);
    the runner reaches the underlying process through :attr:`proc`."""

    def __init__(self, proc: Any, spec: ChildSpec) -> None:
        self._proc = proc
        self._spec = spec

    @property
    def pid(self) -> int:
        return int(self._proc.pid)

    @property
    def proc(self) -> Any:
        return self._proc

    @property
    def spec(self) -> ChildSpec:
        return self._spec


class Win32ChildProcessRunner:
    """The concrete ``core.ChildProcessRunner`` for the service layer, extended
    with the per-child graceful-stop dispatch the D5/RAT-004 stop chain drives.
    The per-child branching (the control-plane child gets its own process group
    per ``spec.new_process_group``; graceful stop is ``CTRL_BREAK`` for the
    control plane and a bounded stop COMMAND for postgres, resolved by
    ``children.graceful_stop_action``) is pure over injectable seams, so the
    LOGIC is CI-testable on Linux; the default seams bind to the real
    ``subprocess``/``os.kill`` calls on Windows."""

    def __init__(
        self,
        *,
        popen_factory: PopenFactory | None = None,
        ctrl_break_sender: CtrlBreakSender | None = None,
        stop_command_runner: StopCommandRunner | None = None,
        open_process_factory: OpenProcessFactory | None = None,
        postgres_stop_spec: ChildSpec | None = None,
        log_root: Path | str | None = None,
    ) -> None:
        # G3: the log root the DEFAULT popen factory redirects each child's
        # stdout/stderr under -- the layout-resolved root when the caller
        # supplies one (``build_production_service`` passes
        # ``layout.log_root``), else the PROGRAMDATA-env-resolved default
        # (matching ``child_log_path``'s / ``configure_logging``'s own
        # fallback). Irrelevant when the caller injects its own
        # ``popen_factory`` (tests) -- the seam stays exactly as
        # injectable/fakeable as before (``PopenFactory`` is still the plain
        # 2-arg ``Callable[[ChildSpec, bool], Any]``).
        self._log_root = Path(log_root) if log_root is not None else default_log_root()
        self._popen_factory = popen_factory or (
            lambda spec, new_process_group: _file_backed_popen_factory(
                spec, new_process_group, log_root=self._log_root
            )
        )
        self._ctrl_break_sender = ctrl_break_sender or _default_ctrl_break_sender
        self._stop_command_runner = stop_command_runner or _default_stop_command_runner
        self._open_process_factory = open_process_factory or _default_open_process
        # CC-WS5-003: the spec carried by an opened postmaster handle, so the
        # graceful stop chain still issues ``pg_ctl stop -m fast`` against it (not
        # a bare TerminateProcess). Defaults to the production postgres spec; the
        # data_dir MUST match the postmaster_pid_reader's (see _POSTGRES_DATA_DIR).
        self._postgres_stop_spec = postgres_stop_spec or postgres_child_spec(
            pg_ctl_path=_PG_CTL_PATH, data_dir=_POSTGRES_DATA_DIR
        )

    def spawn(self, spec: ChildSpec) -> ChildHandle:
        # ``spec.new_process_group`` is the authoritative source (set by the
        # child-spec factory for the control plane only), not a name guess.
        proc = self._popen_factory(spec, spec.new_process_group)
        return _ProcHandle(proc, spec)

    def open_existing(self, pid: int) -> ChildHandle:
        """CC-WS5-003: open a durable handle to an already-running process (the
        PostgreSQL postmaster). The launcher (``pg_ctl -w``) self-exits, so the
        supervisor swaps this handle in to CONTAIN + MONITOR the real postmaster.
        The opened process is wrapped in the SAME ``_ProcHandle`` type a spawn
        returns, carrying the postgres stop spec so its graceful stop stays
        ``pg_ctl stop -m fast`` and its force-stop/liveness use the standard path."""

        proc = self._open_process_factory(pid)
        return _ProcHandle(proc, self._postgres_stop_spec)

    def is_alive(self, handle: ChildHandle) -> bool:
        return cast(_ProcHandle, handle).proc.poll() is None

    def cpu_seconds(self, handle: ChildHandle) -> float | None:
        """U31: the child's cumulative CPU seconds, via ``psutil`` on its pid --
        what the readiness poll's no-progress window samples. ``None`` when the
        sample cannot be taken (the process is gone, or its times are not
        readable): "no evidence", never a stall. Kept on the runner because
        this is the seam that owns the OS handle; the orchestrator never reads
        the process table itself (a fake handle must not be able to reach a
        REAL process that happens to share its pid)."""

        return process_cpu_seconds(cast(_ProcHandle, handle).pid)

    def send_ctrl_break(self, handle: ChildHandle) -> None:
        self._safe_ctrl_break(cast(_ProcHandle, handle).pid)

    def terminate(self, handle: ChildHandle) -> None:
        cast(_ProcHandle, handle).proc.terminate()

    def graceful_stop(self, handle: ChildHandle) -> GracefulStopKind:
        """Issue the child's OWN graceful-stop action (``children.graceful_stop_action``):
        ``CTRL_BREAK`` to the process group for the control plane (RAT-004
        drain-all), or the bounded stop command for postgres (pg_ctl fast
        stop). Returns the action kind performed; the D5 deadline
        + ``TerminateProcess`` force-fallback is the caller's (the stop chain's)."""

        proc_handle = cast(_ProcHandle, handle)
        action = graceful_stop_action(proc_handle.spec, pid=proc_handle.pid)
        if action.kind == "ctrl_break_event":
            self._safe_ctrl_break(proc_handle.pid)
        else:  # "argv" -- action.argv is guaranteed non-None by GracefulStopAction
            self._stop_command_runner(list(action.argv or []))
        return action.kind

    def _safe_ctrl_break(self, pid: int) -> None:
        """Best-effort ``CTRL_BREAK``. A control plane that already exited (a
        spawn/stop race, or the redundant re-drain from ``core.graceful_stop``
        after the service chain already stopped it) makes the underlying
        ``GenerateConsoleCtrlEvent`` fail with ``OSError`` -- swallow it: the goal
        (the process stopping) is already met, and the D5 deadline +
        ``TerminateProcess`` and the Job Object kill-on-close remain the ground
        truth. A signal that cannot be delivered must never crash the stop path."""

        with contextlib.suppress(OSError):
            self._ctrl_break_sender(pid)


# ---------------------------------------------------------------------------
# The D7 control pipe standup (reuses pipe_server; no hand-rolled pipe)
# ---------------------------------------------------------------------------


class ControlPipeLike(Protocol):
    """The control-pipe lifecycle the service run path drives: stand it up before
    the supervision loop, tear it down on the stop path. ``_ControlPipe``
    satisfies this; the run-loop tests inject a fake so the open-before /
    close-after wiring is proven with no Win32."""

    def open(self) -> Any: ...
    def close(self) -> None: ...


class _ControlPipe:
    """Bundles the D7 named-pipe server, its single serialized
    :class:`CommandQueue` (AC-N5), and the accept thread into one lifecycle
    object. ``open()`` starts the queue, creates the pipe (squat-detected --
    ``create_control_pipe`` never raises), and, when creation succeeds, runs the
    accept loop on a daemon thread. ``close()`` stops the loop, closes the pipe
    handle, and stops the queue.

    This class owns only the LIFECYCLE wiring (start/create/accept-loop/close),
    which is what runs on this box under fakes; the real per-connection Win32
    read/impersonate/reply I/O lives in ``pipe_server`` and is proven against a
    real pipe in ``tests/native/test_supervisor_pipe_server_win.py``.
    """

    def __init__(
        self,
        *,
        server: PipeServer,
        command_queue: CommandQueue,
        logger: logging.Logger | None = None,
    ) -> None:
        self._server = server
        self._queue = command_queue
        self._logger = logger or logging.getLogger(LOGGER_NAME)
        self._thread: threading.Thread | None = None
        self._running = False

    def open(self) -> PipeCreateResult:
        """Start the command queue and create the pipe. On a clean create, run
        the accept loop on a daemon thread; on a degraded create (a squatted
        name, D7) log and stay up without an accept loop -- children keep running,
        control is unavailable, matching the pipe_server degraded contract."""

        self._queue.start()
        result = self._server.create()
        if result.ok:
            self._running = True
            self._thread = threading.Thread(
                target=self._accept_loop,
                name="civiccast-supervisor-pipe-accept",
                daemon=True,
            )
            self._thread.start()
            self._logger.info("D7 control pipe opened: %s", result.detail)
        else:
            self._logger.error(
                "D7 control pipe not created (degraded=%s): %s", result.degraded, result.detail
            )
        return result

    def _accept_loop(self) -> None:
        # The flag is flipped from ANOTHER thread in ``close()`` and is re-read
        # (at runtime) at the loop head every iteration and in the except below.
        # An exception raised because ``close()`` closed the handle mid-accept is
        # expected teardown, so it is logged only when we are still meant to be
        # running -- one bad connection must never kill the loop.
        while self._running:
            try:
                self._server.accept_and_serve_one()
            except Exception:
                if self._running:
                    self._logger.exception("D7 control pipe accept loop error; continuing")

    def close(self) -> None:
        """Tear down on the stop path: stop accepting, close the pipe handle, and
        stop the command queue worker. Idempotent and never raises -- a control
        pipe that failed to open (no handle, queue never started) closes cleanly."""

        self._running = False
        with contextlib.suppress(Exception):
            self._server.close()
        self._queue.stop()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=5.0)


def build_control_pipe(
    command_handler: CommandHandler,
    *,
    name: str = CONTROL_PIPE_NAME,
    logger: logging.Logger | None = None,
) -> _ControlPipe:
    """Assemble the D7 control pipe: a single serialized :class:`CommandQueue`
    bound to ``command_handler`` (the supervisor's read-tier handler), a
    :class:`Dispatcher` (framing + per-command authorization + audit log), and a
    :class:`PipeServer` on the D7 pipe ``name``. Construction touches NO Win32 --
    ``PipeServer.create`` is only called at :meth:`_ControlPipe.open` time -- so
    this assembler runs at wiring time on any OS."""

    queue = CommandQueue(command_handler)
    dispatcher = Dispatcher(command_queue=queue)
    server = PipeServer(dispatcher, name=name)
    return _ControlPipe(server=server, command_queue=queue, logger=logger)


# ---------------------------------------------------------------------------
# The service orchestration (run loop + graceful stop chain)
# ---------------------------------------------------------------------------

StopOutcome = Literal["exited", "terminated"]


class ChildStopResult(BaseModel):
    """The result of stopping one child in the graceful stop chain.
    ``graceful_kind`` is the per-child graceful action issued first
    (``ctrl_break_event`` for the control plane -- the RAT-004 drain; ``argv``
    for postgres's command-based stop), or ``None`` when the graceful
    action itself FAILED (audit A5: e.g. the stop command raised) and the child
    went straight to ``TerminateProcess``. ``outcome`` is ``exited`` if the
    child left within the D5 deadline, or ``terminated`` if it overran (or its
    graceful action failed) and was force-stopped with ``TerminateProcess``."""

    model_config = ConfigDict(extra="forbid")

    name: str
    graceful_kind: GracefulStopKind | None
    outcome: StopOutcome
    detail: str


ClockFn = Callable[[], float]
SleepFn = Callable[[float], None]

# CC-WS5-007 part 3: control-pipe standup resilience.
EventLogFn = Callable[[str], None]
"""``(message) -> None`` -- write an operational failure to the Windows Event
Log. Production binds ``servicemanager.LogErrorMsg`` (lazy, Windows-only); the
run-loop tests inject a recorder so the retry/degrade path is proven with no
Win32."""

PIPE_STANDUP_MAX_ATTEMPTS = 3
"""D7: bounded retries for the control-pipe standup before DEGRADING (children
keep running, control unavailable). The pipe being down must never kill the
supervision loop."""

PIPE_STANDUP_BACKOFF_SECONDS = 2.0
"""Base inter-attempt backoff for the pipe standup (multiplied by the attempt
number for a simple bounded ramp). Applied through the injected ``sleep`` seam,
so tests advance a fake clock instead of waiting."""


def _default_event_log(message: str) -> None:
    """Best-effort Windows Event Log write for a control-pipe failure
    (``servicemanager.LogErrorMsg``). Lazily imports ``servicemanager`` so this
    module imports on Linux; falls back to the module logger when the service
    manager is unavailable (e.g. not running under the SCM), so a logging
    failure never becomes a second fault on the degrade path."""

    try:
        import servicemanager  # type: ignore[import-untyped]

        servicemanager.LogErrorMsg(f"CivicCastSupervisor control pipe: {message}")
    except Exception:
        logging.getLogger(LOGGER_NAME).error("EVENTLOG(fallback): %s", message)


# ---------------------------------------------------------------------------
# SvcStop watchdog (gauntlet run 17: SERVICE_STOP_PENDING forever)
# ---------------------------------------------------------------------------

_DB_CONNECT_TIMEOUT_SECONDS = 10
"""psycopg connect timeout for the supervisor's own DB engine (task #51: psycopg
v3 without one can hang for MINUTES on Windows). Also the LONGEST single
readiness-probe attempt in the product, so it is the F1 in-flight term below:
the other two probes are bounded at 2.0s each
(:data:`_HEALTH_HTTP_TIMEOUT_SECONDS`, :data:`_OLLAMA_VERSION_TIMEOUT_SECONDS`)."""

SVC_STOP_IN_FLIGHT_ITERATION_SECONDS = float(_DB_CONNECT_TIMEOUT_SECONDS) + 1.0
"""F1: the bound on the supervisor work already IN FLIGHT when ``SvcStop`` lands.

Before the F1 abort seam this term was UNBOUNDED in practice: nothing inside
``Supervisor.start()``/``tick()`` read the stop event, so a single iteration
could chain three readiness budgets (60 + 30 + 60 = 150s when F1 was written;
U31 has since raised the control plane's own budget to 180s, making the same
chain 300s) before the stop chain could even begin -- longer, on its own,
than this whole watchdog. With ``should_abort`` checked between children and
inside ``poll_until_ready``, the worst case collapses to whichever of these the
stop request lands just after:

  * a probe attempt already running -- at most one, bounded by the longest
    probe timeout (:data:`_DB_CONNECT_TIMEOUT_SECONDS`, 10s); or
  * a poll-interval ``sleep`` already running -- 1.0s
    (``poll_until_ready``'s ``poll_interval_seconds``), since a real
    ``time.sleep`` is not interruptible.

Summed rather than maxed (11s, not 10s) so the term stays a true upper bound
without depending on which of the two the stop happened to interrupt."""

SVC_STOP_WATCHDOG_SECONDS = 150
"""How long after ``SvcStop`` the service host may still be inside
``SvcDoRun`` before the watchdog force-exits it.

DERIVED, not picked: it must exceed the BOUNDED worst case of the whole stop
path, or a legitimately slow stop would be truncated by the last resort meant
to catch only UNBOUNDED ones. That worst case, from this file's own numbers:

  * the F1 in-flight term -- the supervisor iteration already running when the
    stop lands, now abortable: :data:`SVC_STOP_IN_FLIGHT_ITERATION_SECONDS` =
    11s (one 10s probe attempt + one 1.0s poll sleep).
  * ``graceful_stop_all`` stops up to THREE children (``control_plane``,
    ``ollama``, ``postgres``). Each one costs at most the graceful
    stop command's own deadline (:data:`STOP_COMMAND_TIMEOUT_SECONDS` = 10s)
    plus the D5 deadline poll --
    ``SupervisorConfig.graceful_stop_deadline_seconds`` = 15.0 with 1.0s
    ``_sleep`` granularity, so <=16s. 3 x 26s = 78s.
  * ``_ControlPipe.close()``: ``PipeServer.close()`` <=5s
    (``pipe_server.ACCEPT_SHUTDOWN_TIMEOUT_SECONDS``) + ``CommandQueue.stop()``
    join 5s + accept-thread join 5s = 15s.

  11 + 78 + 15 = 104s bounded worst case -> 150s, ~46s of headroom without
  being open-ended. (NATS JetStream was removed from the product -- owner
  decision 2026-08-20, ADR 0023 -- dropping one child from this chain; the
  constant is kept at 150s rather than retuned down, so the extra headroom is
  a deliberate safety margin, not a derivation gap. The original pre-F1
  comment said 119s because it silently assumed the in-flight term was zero;
  the term was in fact the biggest one in the system, at up to ~180s, which is
  how run 17's watchdog could fire MID-CHAIN and take the postgres cluster
  down uncleanly.)

This is deliberately LONGER than the ~30s a first reading suggests: run 17's
own evidence shows a single child consuming the full 15s deadline on a real
station, so a 30s budget would fire during an ordinary multi-child stop --
exactly the "must not fire during normal operation" rule this constant
exists to respect. Override with :data:`SVC_STOP_WATCHDOG_ENV_VAR` (<=0
disables the watchdog entirely)."""

SVC_STOP_WATCHDOG_ENV_VAR = "CIVICCAST_SUPERVISOR_STOP_WATCHDOG_SECONDS"
"""Env override for :data:`SVC_STOP_WATCHDOG_SECONDS`, read at arm time so an
operator can retune (or disable, with <=0) a wedged station without a rebuild."""

SVC_STOP_WATCHDOG_EXIT_CODE = 0
"""The host process's exit code when the watchdog force-exits: ZERO, deliberately.

F2 (BLOCKER, 2026-07-31). This was 88 -- a distinctive code chosen so a
force-exited host was identifiable from its exit status alone. That reasoning
ignored who READS the status: the SCM. The service is registered with
``sc failure ... actions= restart/5000/...`` AND ``sc failureflag 1``
(``native_service_registration.rs``), and ``failureflag 1`` makes the SCM apply
those failure actions to ANY nonzero exit, not just a crash. So a fired watchdog
-- which by definition fires while an uninstall or repair is trying to stop this
service -- got the service RESTARTED ~5 seconds later, straight into the
uninstaller's tree removal. The watchdog existed to unblock exactly that
uninstall; a nonzero code made it re-block it.

The diagnosis therefore lives where nothing acts on it automatically: the ERROR
line in ``supervisor.log`` and the Windows Event Log entry, both naming the
stop-chain breadcrumb (:attr:`SupervisorService.stop_position`). Reporting
SERVICE_STOPPED before this exit (see :class:`StopWatchdog`) is what makes 0 the
HONEST code -- the service really did reach STOPPED, just not by finishing its
own stop chain."""


def _force_exit_service_host(exit_code: int) -> None:
    """Terminate THIS process NOW, skipping interpreter shutdown entirely.

    ``os._exit`` (not ``sys.exit``) on purpose: the watchdog only ever runs
    because some thread is stuck in a non-returning call, and a normal exit
    would unwind through that same stuck path -- ``sys.exit`` raises
    ``SystemExit`` on the TIMER thread, which would not touch the wedged one.
    Logging is flushed first (``logging.shutdown`` closes every handler,
    including the supervisor.log file handler) so the watchdog's own explanation
    is on disk before the process disappears."""

    with contextlib.suppress(Exception):
        logging.shutdown()
    with contextlib.suppress(Exception):
        sys.stderr.flush()
    os._exit(exit_code)


class StopWatchdog:
    """Last-resort bound on ``SvcStop`` -> ``SvcDoRun`` returns.

    WHY (gauntlet run 17, 2026-07-31): the supervisor service wedged in
    ``SERVICE_STOP_PENDING`` with the SCM checkpoint stuck at 0x1 for 112+
    seconds, because ``SvcDoRun`` never returned. Everything bounded in the
    stop chain had already run; the unbounded step was a ``CloseHandle`` parked
    on the D7 accept thread's pipe handle (closed in ``pipe_server`` by F1).
    A service that cannot reach STOPPED is not just a stuck stop: it makes
    repair, uninstall, and every subsequent install fail. So the host now also
    carries a defense in depth that does not depend on having identified the
    right unbounded call.

    Armed by ``SvcStop``, disarmed by ``SvcDoRun``'s OUTERMOST ``finally`` --
    i.e. after the singleton release and every other teardown step, so it is
    genuinely the last thing standing and never pre-empts a step that would
    have completed. Firing is LOUD (ERROR to ``supervisor.log`` AND the Windows
    Event Log), never silent, and names the best available stop-chain position
    so the next investigator starts where this one stopped.

    F2 (BLOCKER, 2026-07-31): firing also REPORTS ``SERVICE_STOPPED`` to the SCM
    (``report_stopped``) BEFORE the force-exit, and exits ZERO. Without the
    report, the host vanished while the SCM still believed it was
    STOP_PENDING; with a nonzero exit, ``sc failureflag 1`` + ``actions=
    restart/5000`` had the SCM RESURRECT the service ~5s later, into the middle
    of the uninstall this watchdog fired to unblock. The report is best-effort
    (the handle may already be gone) and can never prevent the force-exit --
    that is the whole point of a last resort.

    OS-independent and fully injectable (``force_exit``, ``report_stopped``), so
    the fire path is unit-proven without actually killing the test runner."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        position: Callable[[], str],
        logger: logging.Logger | None = None,
        event_log: EventLogFn | None = None,
        force_exit: Callable[[int], None] = _force_exit_service_host,
        report_stopped: Callable[[], None] | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._position = position
        self._logger = logger or logging.getLogger(LOGGER_NAME)
        self._event_log = event_log or _default_event_log
        self._force_exit = force_exit
        self._report_stopped = report_stopped
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._fired = False

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._timer is not None

    @property
    def fired(self) -> bool:
        with self._lock:
            return self._fired

    def arm(self) -> None:
        """Start the countdown. Idempotent -- a second ``SvcStop`` (the SCM may
        send one) must not stack a second timer. A non-positive timeout means
        "disabled" and arms nothing."""

        if self._timeout_seconds <= 0:
            self._logger.warning(
                "stop watchdog DISABLED (timeout %.1fs <= 0); a wedged stop will not be bounded",
                self._timeout_seconds,
            )
            return
        with self._lock:
            if self._timer is not None:
                return
            timer = threading.Timer(self._timeout_seconds, self._fire)
            timer.name = "civiccast-supervisor-stop-watchdog"
            timer.daemon = True
            self._timer = timer
        timer.start()

    def disarm(self) -> None:
        """Cancel the countdown. Idempotent, and safe on a never-armed
        watchdog -- called unconditionally from ``SvcDoRun``'s outermost
        ``finally``, including on paths where ``SvcStop`` never ran."""

        with self._lock:
            timer = self._timer
            self._timer = None
        if timer is not None:
            timer.cancel()

    def _fire(self) -> None:
        with self._lock:
            if self._timer is None:
                return  # disarmed in the gap between expiry and this callback
            self._timer = None
            self._fired = True
        position = "unavailable"
        with contextlib.suppress(Exception):
            position = self._position()
        message = (
            f"STOP WATCHDOG FIRED: SvcDoRun did not return within "
            f"{self._timeout_seconds:.0f}s of SvcStop; last stop-chain position: "
            f"{position}. Reporting SERVICE_STOPPED and force-exiting the service "
            f"host so the SCM reaches STOPPED instead of wedging in STOP_PENDING. "
            f"THIS LINE IS THE ONLY RECORD: the host exits "
            f"{SVC_STOP_WATCHDOG_EXIT_CODE} on purpose (F2 -- a nonzero code makes "
            f"the SCM's configured failure actions restart the service straight "
            f"back into the uninstall this watchdog fired to unblock)."
        )
        with contextlib.suppress(Exception):
            self._logger.error("%s", message)
        with contextlib.suppress(Exception):
            self._event_log(message)
        # F2: tell the SCM the service has STOPPED before the process disappears.
        # Ordered AFTER the logging (the log line is the forensic record and must
        # survive even if this raises) and BEFORE the force-exit (after it there
        # is no process left to report anything). Best-effort: a failure here
        # must never stop the force-exit.
        if self._report_stopped is not None:
            with contextlib.suppress(Exception):
                self._report_stopped()
        self._force_exit(SVC_STOP_WATCHDOG_EXIT_CODE)


def build_stop_watchdog(
    position: Callable[[], str],
    *,
    logger: logging.Logger | None = None,
    report_stopped: Callable[[], None] | None = None,
) -> StopWatchdog:
    """Assemble the production :class:`StopWatchdog`, reading its timeout from
    :data:`SVC_STOP_WATCHDOG_ENV_VAR` (default
    :data:`SVC_STOP_WATCHDOG_SECONDS`) at ARM time, not import time.

    ``report_stopped`` is the F2 SCM status reporter -- both ServiceFramework
    subclasses (``build_service_class``'s and ``service_host``'s) pass a
    callable that reports ``SERVICE_STOPPED`` on their own service handle. It is
    optional only so a non-SCM caller (the pure tests) can build a watchdog
    without one."""

    return StopWatchdog(
        timeout_seconds=float(_read_int_env(SVC_STOP_WATCHDOG_ENV_VAR, SVC_STOP_WATCHDOG_SECONDS)),
        position=position,
        logger=logger,
        report_stopped=report_stopped,
    )


class SupervisorService:
    """Drives a ``core.Supervisor`` through its service lifetime: ``run`` brings
    it up and blocks until a stop is requested, then runs the graceful stop
    chain. Every dependency (the supervisor, the child runner, the clock/sleep,
    the config, the logger) is injected, so the run loop and the stop chain are
    exercised end to end with fakes on any OS.
    """

    def __init__(
        self,
        *,
        supervisor: Supervisor,
        runner: Win32ChildProcessRunner | Any,
        config: SupervisorConfig,
        clock: ClockFn = time.monotonic,
        sleep: SleepFn = time.sleep,
        logger: logging.Logger | None = None,
        control_pipe: ControlPipeLike | None = None,
        event_log: EventLogFn | None = None,
        pipe_standup_max_attempts: int = PIPE_STANDUP_MAX_ATTEMPTS,
        pipe_standup_backoff_seconds: float = PIPE_STANDUP_BACKOFF_SECONDS,
        stop_event: Event | None = None,
    ) -> None:
        self._supervisor = supervisor
        self._runner = runner
        self._config = config
        self._clock = clock
        self._sleep = sleep
        self._logger = logger or logging.getLogger(LOGGER_NAME)
        self._control_pipe = control_pipe
        self._event_log = event_log or _default_event_log
        self._pipe_standup_max_attempts = pipe_standup_max_attempts
        self._pipe_standup_backoff_seconds = pipe_standup_backoff_seconds
        # F1: the stop Event may be INJECTED so the SAME object is both what this
        # run loop waits on and what the ``Supervisor``'s ``should_abort`` seam
        # polls (``build_production_service`` creates one Event and wires both
        # halves). One object means ``request_stop`` cannot be observable to one
        # half and invisible to the other. Defaulting to a fresh Event keeps
        # every caller that does not need the abort seam unchanged.
        self._stop_event = stop_event if stop_event is not None else Event()
        # Stop-chain breadcrumb. Written by the run/stop path, read by whoever
        # has to explain a stop that did not finish -- today that is the
        # SvcStop watchdog (:class:`StopWatchdog`), whose ONE log line is the
        # only forensic record a force-exited host leaves behind. A plain str
        # attribute is enough: single writer (the run thread), single reader
        # (the watchdog timer thread), and a torn read is impossible for a
        # rebound Python attribute.
        self._stop_position = "not-stopping"

    @property
    def stop_position(self) -> str:
        """Where the stop chain last got to, as a short human-readable token.
        ``not-stopping`` until the run loop leaves ``Supervisor.run``."""

        return self._stop_position

    def request_stop(self) -> None:
        """Signal the run loop to begin the graceful stop chain (called from
        the service's ``SvcStop`` on the SCM's stop thread)."""

        self._stop_position = "stop-requested"
        self._stop_event.set()

    def run(self) -> None:
        """Stand up the D7 control pipe, drive the supervisor's reconciliation
        loop (``Supervisor.run``: boot to ready, then tick until the stop event
        is set -- design.md:60), then, on the stop path, run the graceful stop
        chain and tear the control pipe down. This is what ``SvcDoRun`` drives.

        The supervision loop lives in ``core.py`` (design.md:56); this layer no
        longer bare-``start()``s and idles -- it delegates the loop and owns only
        the OS-facing standup/teardown around it."""

        self._logger.info("CivicCast supervisor service starting up")
        # CC-WS5-007 part 3: the control-pipe standup is RESILIENT -- it retries
        # with bounded backoff, logs any failure to the Windows Event Log, and
        # DEGRADES (returns without a live pipe) rather than propagating. A pipe
        # that will not stand up must never stop the supervision loop below.
        self._standup_control_pipe()
        try:
            self._supervisor.run(self._stop_event)
        finally:
            self._logger.info("stop requested; running graceful stop chain")
            self._stop_position = "graceful-stop-chain"
            self.graceful_stop_all()
            if self._control_pipe is not None:
                self._stop_position = "control-pipe-close"
                with contextlib.suppress(Exception):
                    self._control_pipe.close()
            self._stop_position = "stop-chain-complete"
        self._logger.info("CivicCast supervisor service stopped")

    def _standup_control_pipe(self) -> None:
        """Stand up the D7 control pipe with bounded retry + degrade (D7 / RAT):
        on each attempt, ``open()`` may RAISE (a transient Win32 standup fault)
        or return a degraded ``ok=False`` create (a squatted name). Either is
        logged to the Windows Event Log seam and retried up to
        ``pipe_standup_max_attempts`` with a bounded backoff (through the injected
        ``sleep``). If every attempt fails, the service DEGRADES -- it logs the
        exhaustion and returns, letting ``run()`` proceed into the supervision
        loop WITHOUT a live control pipe (children keep running; control is
        unavailable). This method never raises: a control-pipe failure must not
        kill supervision.

        A create that returns without an ``ok`` attribute (e.g. a lifecycle fake
        that yields ``None``) is treated as success -- only an explicit
        ``ok=False`` is the degraded-create retry signal. The per-connection
        accept-loop resilience (one bad client never kills the loop) lives in
        ``_ControlPipe._accept_loop``; this is the standup half."""

        if self._control_pipe is None:
            return
        max_attempts = self._pipe_standup_max_attempts
        for attempt in range(1, max_attempts + 1):
            try:
                result = self._control_pipe.open()
            except Exception as exc:
                self._log_pipe_failure(f"standup attempt {attempt}/{max_attempts} raised: {exc}")
            else:
                # No ``ok`` attribute -> success (lifecycle fakes); only an
                # explicit ok=False (squat/degraded create) triggers a retry.
                if getattr(result, "ok", True) is not False:
                    if attempt > 1:
                        self._logger.info("D7 control pipe stood up on attempt %d", attempt)
                    return
                self._log_pipe_failure(
                    f"standup attempt {attempt}/{max_attempts} degraded: "
                    f"{getattr(result, 'detail', '')}"
                )
            if attempt < max_attempts:
                self._sleep(self._pipe_standup_backoff_seconds * attempt)
        self._log_pipe_failure(
            "standup exhausted retries; DEGRADED -- supervision continues without control"
        )

    def _log_pipe_failure(self, message: str) -> None:
        """Log a control-pipe standup failure to both the module logger and the
        Windows Event Log seam. Best-effort: the Event Log write never re-raises
        (see ``_default_event_log``)."""

        self._logger.error("D7 control pipe: %s", message)
        with contextlib.suppress(Exception):
            self._event_log(message)

    def graceful_stop_all(self) -> list[ChildStopResult]:
        """The D5 + RAT-004 graceful stop chain. Children are stopped in reverse
        startup order (``control_plane`` -> ``postgres``, with the optional
        ``ollama`` child inserted right after the control plane -- see below).
        Each child
        is issued its OWN graceful-stop action first (``children.graceful_stop_action``
        via the runner): the control-plane child (first) gets a ``CTRL_BREAK`` so
        its uvicorn lifespan drains all channels (RAT-004), while postgres gets
        its command-based stop (pg_ctl fast stop). Every child
        that overruns its 15s deadline is then force-stopped with
        ``TerminateProcess`` (D5). The Job Object kill-on-close (via
        ``supervisor.graceful_stop``) is the final backstop."""

        handles = self._supervisor.handles()
        results: list[ChildStopResult] = []
        # Task #57 D2: the OPTIONAL ollama child joins the chain right after
        # the control plane (its only consumer -- the CP drains first so its
        # in-flight summary/translation requests are not cut mid-drain), and
        # only when it actually has a live handle (a skipped/unconfigured
        # child is silently absent, not a warning).
        stop_order = list(reversed(STARTUP_ORDER))
        if _OLLAMA_CHILD in handles:
            stop_order.insert(1, _OLLAMA_CHILD)
        for name in stop_order:
            handle = handles.get(name)
            if handle is None:
                self._logger.warning("stop: child %s has no live handle; skipping", name)
                continue
            # Audit A5: a PER-CHILD exception boundary. A failing stop for one
            # child (an unexpected runner fault beyond the graceful-action
            # boundary inside _stop_child) must never skip the REMAINING
            # children's stops -- terminate that child and keep the chain going.
            self._stop_position = f"stopping-child:{name}"
            try:
                results.append(self._stop_child(name, handle))
            except Exception as exc:
                self._logger.exception("stop: stopping %s failed; terminating and continuing", name)
                with contextlib.suppress(Exception):
                    self._runner.terminate(handle)
                results.append(
                    ChildStopResult(
                        name=name,
                        graceful_kind=None,
                        outcome="terminated",
                        detail=f"{name}: stop chain fault {exc!r}; terminated and continued",
                    )
                )
        # State transition (-> stopping) + explicit Job Object kill-on-close
        # backstop. See the module docstring's disclosed redundancy note. Also
        # inside the A5 boundary: the backstop failing must not escape run()'s
        # finally into SvcDoRun.
        self._stop_position = "job-object-backstop"
        try:
            self._supervisor.graceful_stop()
        except Exception:
            self._logger.exception("stop: supervisor.graceful_stop backstop failed; continuing")
        return results

    def _stop_child(self, name: str, handle: ChildHandle) -> ChildStopResult:
        # Audit A5: the graceful action itself may fail (FileNotFoundError from
        # a stop command that is not on the stock LocalSystem PATH,
        # subprocess.TimeoutExpired from a hung one). That must fall through to
        # TerminateProcess for THIS child, never propagate up the stop chain.
        try:
            kind: GracefulStopKind | None = self._runner.graceful_stop(handle)
        except Exception as exc:
            self._logger.warning(
                "stop: graceful action for %s failed (%r); falling through to TerminateProcess",
                name,
                exc,
            )
            with contextlib.suppress(Exception):
                self._runner.terminate(handle)
            return ChildStopResult(
                name=name,
                graceful_kind=None,
                outcome="terminated",
                detail=f"{name}: graceful stop action failed ({exc!r}); terminated",
            )
        self._logger.info("stop: issued %s graceful action for %s", kind, name)
        deadline_seconds = self._config.graceful_stop_deadline_seconds
        deadline = self._clock() + deadline_seconds
        while self._runner.is_alive(handle):
            if self._clock() >= deadline:
                self._logger.warning(
                    "stop: %s overran the %.0fs graceful deadline; TerminateProcess",
                    name,
                    deadline_seconds,
                )
                self._runner.terminate(handle)
                return ChildStopResult(
                    name=name,
                    graceful_kind=kind,
                    outcome="terminated",
                    detail=f"{name} did not exit within {deadline_seconds}s; terminated",
                )
            self._sleep(1.0)
        return ChildStopResult(
            name=name,
            graceful_kind=kind,
            outcome="exited",
            detail=f"{name} exited within the {deadline_seconds}s graceful deadline",
        )


# ---------------------------------------------------------------------------
# Admin-verb runtime-selector seams (CC-WS5-007 runtime_set). Lazily bound to
# the WS4 registry selector so this module still imports on Linux -- the
# read/write only touch HKLM when a runtime_set command actually arrives.
# ---------------------------------------------------------------------------


def _production_selector_reader() -> str | None:
    """Read the current D-runtime selector (``HKLM\\SOFTWARE\\CivicCast\\
    ActiveRuntime``) as ``"native"``/``"wsl"``, or ``None`` when it is
    absent/unreadable (so runtime_set applies rather than no-ops). ``winreg`` is
    imported inside ``read_selector`` (win_probes), so this is Linux-safe until
    called on the VM."""

    from civiccast.native.win_probes import read_selector

    read = read_selector()
    if read.ok and read.value in ("native", "wsl"):
        return read.value
    return None


def _production_selector_writer(value: RuntimeTarget) -> None:
    """Write the D-runtime selector (admin-writable HKLM key). Lazily imports
    ``write_selector`` (win_probes) so this module imports on Linux; the write
    happens only when an authorized runtime_set applies a real change."""

    from civiccast.native.win_probes import write_selector

    write_selector(value)


# ---------------------------------------------------------------------------
# Production wiring (binds the Windows-real seams; injects the pending ones)
# ---------------------------------------------------------------------------

# Task #57 D2: the runtime base URL the app-side ollama clients dial
# (civiccast/ai_runtime/ollama_client.py DEFAULT_OLLAMA_BASE_URL,
# ``http://127.0.0.1:11434`` -- summary/ollama.py and translate/ollama.py
# both default to it and the control-plane env carries no override). The
# child spec and the readiness probe are BOTH derived from it so they can
# never disagree with the consumer.
_OLLAMA_VERSION_TIMEOUT_SECONDS = 2.0


def _default_ollama_version_probe() -> bool:
    """Bounded ``GET /api/version`` against the runtime ollama base URL --
    the same readiness gate the installer's production self-test uses
    (``main.rs`` ``NativeOllamaSelfTestServer::wait_until_ready``). Fails
    closed on any error, never raises (``children.check_ollama_ready`` is
    the wrapping gate)."""

    from civiccast.ai_runtime.ollama_client import DEFAULT_OLLAMA_BASE_URL

    url = DEFAULT_OLLAMA_BASE_URL.rstrip("/") + "/api/version"
    try:
        with urllib.request.urlopen(url, timeout=_OLLAMA_VERSION_TIMEOUT_SECONDS) as resp:  # noqa: S310  # nosec B310 - DEFAULT_OLLAMA_BASE_URL is the literal http://127.0.0.1:11434
            return int(getattr(resp, "status", 0) or 0) == 200
    except Exception:
        return False


def build_ollama_spec_provider(
    layout: InstallLayout,
    *,
    logger: logging.Logger | None = None,
    extra_env: dict[str, str] | None = None,
) -> Callable[[], OllamaChildDecision]:
    """Task #57 D2: decide, at each (re)start attempt, whether the OPTIONAL
    ollama child can launch. Launchable ONLY when the reviewed binary
    (``layout.ollama_exe_path`` -- ``native_activation.rs``'s staged runtime
    layout) AND a staged model store (``install_layout.
    ollama_model_store_candidates``: the activation flow's composed
    ``models\\ollama`` first, then the acquisition flow's
    ``packs\\local-ai-model\\models``; a store counts only when its
    ``manifests\\`` subtree exists) are actually present. Anything missing ->
    a skip decision with the exact reason (degraded AI, service healthy),
    logged ONCE per distinct reason so a skipping station shows one clear
    line, not one per restart attempt."""

    log = logger or logging.getLogger(LOGGER_NAME)
    last_skip: dict[str, str] = {}

    def _skip(detail: str) -> OllamaChildDecision:
        if last_skip.get("detail") != detail:
            last_skip["detail"] = detail
            log.warning("ollama child skipped (degraded AI, service healthy): %s", detail)
        return OllamaChildDecision(spec=None, detail=detail)

    def provider() -> OllamaChildDecision:
        exe = layout.ollama_exe_path
        if not exe.is_file():
            return _skip(f"ollama binary absent at {exe}")
        store = next(
            (
                candidate
                for candidate in ollama_model_store_candidates(layout)
                if (candidate / "manifests").is_dir()
            ),
            None,
        )
        if store is None:
            return _skip(
                "no staged ollama model store at "
                + " or ".join(str(c) for c in ollama_model_store_candidates(layout))
            )
        return OllamaChildDecision(
            spec=ollama_child_spec(
                ollama_exe_path=str(exe),
                models_dir=str(store),
                extra_env=extra_env,
            ),
            detail=f"serving staged store {store}",
        )

    return provider


def build_control_plane_media_env(
    layout: InstallLayout,
    *,
    logger: logging.Logger | None = None,
    inherited_path: str | None = None,
) -> dict[str, str]:
    """The control plane's only working on-air path (the installer rehearsal
    flow) shells out to ffmpeg/ffprobe by BARE NAME (``civiccast/stream/
    _ffmpeg.py``'s ``_FFMPEG_EXECUTABLE = "ffmpeg"``; ``check_ffmpeg`` /
    ``run_ffmpeg`` resolve it against ``PATH``). Under the SCM the control
    plane's PATH is whatever ``_file_backed_popen_factory`` hands it -- the
    supervisor's own stock LocalSystem PATH, which the installer writes NO
    changes to -- so a bare ``ffmpeg``/``ffprobe`` is FileNotFoundError
    unless the staged ``dependencies\\ffmpeg\\bin`` directory
    (``layout.ffmpeg_bin_dir`` -- ``native_activation.rs``'s
    ``validate_staged_runtime_layout`` convention) is somewhere on that PATH.

    Launchable ONLY when BOTH staged binaries exist (``layout.
    ffmpeg_exe_path`` / ``layout.ffprobe_exe_path``) -- pure existence gate,
    mirroring :func:`build_ollama_spec_provider`'s skip pattern: missing
    binaries return an EMPTY dict (the control plane keeps resolving ffmpeg
    off whatever PATH it already has -- the pre-fix behaviour, degraded media
    handling, service still healthy) plus exactly one clear log line naming
    the missing directory. Unlike ``build_ollama_spec_provider`` this helper
    is called ONCE per service build (not once per restart tick), so no
    per-reason dedup latch is needed to keep the log to one line.

    CRITICAL (``_file_backed_popen_factory``, ``env = {**os.environ,
    **spec.env}``): a bare ``PATH`` key placed in ``spec.env`` REPLACES the
    inherited PATH rather than extending it -- ``spec.env`` wins the merge
    entirely for any key it sets. This function therefore reads
    ``os.environ`` HERE, in the host process, and returns the FULL prepended
    string (``ffmpeg_bin_dir`` + ``os.pathsep`` + the inherited PATH), never
    a bare directory -- so the composed PATH the child actually receives
    still carries everything the stock LocalSystem PATH carried, plus
    ffmpeg's directory in front. Scoped to this one dict, merged into the
    control-plane child's env only (:func:`build_production_service`); the
    supervisor's own process environment and the machine-wide PATH are never
    touched.

    ``inherited_path`` is the PATH this composition must build ON TOP OF, and
    it is what the Gate A T4 root cause (2026-09) turned on. Because this dict
    is merged LAST, its ``PATH`` wins outright -- and reading only
    ``os.environ`` here meant the composed value was the SUPERVISOR's stock
    LocalSystem PATH, silently discarding the caller's
    ``station_environment_for_python`` PATH and with it
    ``<runtime>\\dependencies\\gstreamer\\bin``. The control plane and the
    GStreamer playout worker it spawns then ran with no GStreamer bin on PATH,
    so girepository could not resolve ``gstreamer-1.0-0.dll``'s symbols and
    ``import gi.repository.Gst`` died with ``TypeError: must be an interface``
    on every clean box (a machine with a system-wide GStreamer install on PATH
    -- a dev box -- masked it completely). Callers pass the PATH they already
    composed; ``None`` keeps the old ``os.environ`` behaviour for callers that
    have no PATH of their own.
    """

    log = logger or logging.getLogger(LOGGER_NAME)
    if not (layout.ffmpeg_exe_path.is_file() and layout.ffprobe_exe_path.is_file()):
        log.warning(
            "ffmpeg/ffprobe not staged at %s (degraded media handling, "
            "control-plane rehearsal/live paths that shell out to ffmpeg "
            "will fail; service healthy)",
            layout.ffmpeg_bin_dir,
        )
        return {}
    if inherited_path is None:
        inherited_path = os.environ.get("PATH", "")
    composed_path = (
        f"{layout.ffmpeg_bin_dir}{os.pathsep}{inherited_path}"
        if inherited_path
        else str(layout.ffmpeg_bin_dir)
    )
    return {"PATH": composed_path}


def build_production_service(
    logger: logging.Logger,
    *,
    guard: GuardLike,
    alert_outbox: AlertOutbox,
    postgres_probe: Callable[[], bool],
    health_probe: Callable[[], ControlPlaneHealthProbe],
    ollama_probe: Callable[[], bool] | None = None,
    config: SupervisorConfig | None = None,
    program_data_root: str | None = None,
    control_plane_env: dict[str, str] | None = None,
    layout: InstallLayout | None = None,
    db_host: str = "127.0.0.1",
    db_port: int = 5432,
    network_settings: NetworkSettings | None = None,
) -> SupervisorService:
    """Assemble a production :class:`SupervisorService`. Binds the Windows-real
    seams this layer owns -- the concrete child runner, the Job Object API, the
    registry interlock reader, and the wall clock -- and takes the guard monitor,
    the alerting outbox, and the readiness probes as injected parameters (their
    lifecycle / DB-session / probe-transport decisions live outside this build
    unit; see the module docstring's disclosed-seams note).

    Audit A1: every child-spec, stop-command, and pid-reader path is ABSOLUTE,
    derived from ``layout`` (default: ``resolve_install_layout`` over
    ``sys.executable`` + ``%PROGRAMDATA%``). Under LocalSystem (CWD System32,
    stock PATH -- the installer writes no PATH changes) the previous bare
    ``pg_ctl``/``python`` and relative ``pgdata`` made every child spawn
    FileNotFoundError or a System32-relative cluster path.

    ``db_host``/``db_port`` (real-world LPM deployment fix, 2026-08-27): the
    ONLY caller is :func:`default_dependency_provider`, which parses these out
    of the SAME ``DATABASE_URL`` it builds ``postgres_probe`` against -- the
    D4-provisioned ``DatabaseUrl`` registry value is this station's single
    source of truth for the port PostgreSQL actually listens on (D4 may have
    fallen back off the standard 5432 -- see
    ``civiccast.native.provision.port_select``). Before this fix, these two
    parameters did not exist at all and ``Supervisor`` silently used its own
    ``"127.0.0.1"``/``5432`` defaults for the postgres child's LAUNCH argv
    (``postgres_child_spec``'s ``-o "-p <port> -h <host>"``) regardless of
    what the registry actually said -- ``postgres_probe`` (a live SELECT 1)
    correctly dialed the configured port, so the divergence was invisible
    until a station whose D4 run picked a NON-standard port (this fix's other
    half) started a postgres server pinned to the wrong one and every
    subsequent connection failed."""

    cfg = config or SupervisorConfig()
    active_network_settings = network_settings or NetworkSettings()
    network_runtime = active_network_settings.runtime_values()
    # F1: ONE stop Event, shared by the run loop (which waits on it) and the
    # supervisor (whose ``should_abort`` seam polls it from inside start()/tick()
    # and every readiness poll). Created here, before both, because the
    # Supervisor is constructed before the SupervisorService that owns the loop.
    stop_event = Event()
    layout = layout or resolve_install_layout(program_data_root=program_data_root)
    postgres_data_dir = str(layout.postgres_data_dir)
    # Operator media (uploads/recordings) does NOT get a supervisor-side eager
    # mkdir -- investigation finding (see build_control_plane_media_env's
    # neighbor tests): build_production_service is PURE wiring with no real
    # filesystem I/O (test_build_production_service_wires_program_data_root_for_egress_workdir
    # / test_build_production_service_factory_assembles_deps_and_calls_build
    # pin this with a nonexistent ``Z:\`` drive -- an eager mkdir here breaks
    # both). ``CIVICCAST_EGRESS_WORK_DIR`` -- the AC this upload-dir wiring
    # mirrors -- gets the SAME non-treatment: the supervisor never creates
    # it either; ``civiccast/egress/automation.py``'s ``build_channel_automation``
    # creates it lazily, INSIDE the control-plane child process, at actual
    # app startup. The upload dir gets the identical lazy app-side creation
    # (``civiccast/schedule/router.py``'s ``incoming_dir.mkdir(parents=True,
    # exist_ok=True)`` at upload time, which via ``parents=True`` creates the
    # base dir too) -- "follow that same mechanism" means follow it exactly,
    # including the "the supervisor does not touch this path" half.
    # Task media-wiring: merge the layout-derived ffmpeg PATH prepend into the
    # control-plane child's env -- see build_control_plane_media_env's
    # docstring for the popen-factory PATH-replacement hazard this avoids.
    # Applied AFTER the caller's control_plane_env (station_environment_for_python
    # may itself carry a bare, non-prepended "PATH" key) so the ffmpeg-aware
    # composition always wins for that key; every other caller-supplied key
    # is preserved untouched. The composition is built ON TOP OF the caller's
    # own PATH when it has one -- Gate A T4 root cause: composing over
    # os.environ instead dropped the GStreamer bin dir that
    # station_environment_for_python had just put there, and the playout
    # worker died at `import gi.repository.Gst`. See
    # build_control_plane_media_env's docstring.
    control_plane_env = dict(control_plane_env or {})
    control_plane_env.update(
        {
            "CIVICCAST_ACTIVE_NETWORK_SETTINGS": active_network_settings.model_dump_json(),
            "CIVICCAST_PUBLIC_BASE_URL": network_runtime.public_base_url or "",
            "CIVICCAST_TRUSTED_PROXY_CIDRS_EXACT": ",".join(network_runtime.trusted_proxy_cidrs),
        }
    )
    control_plane_env.update(
        build_control_plane_media_env(
            layout, logger=logger, inherited_path=control_plane_env.get("PATH")
        )
    )
    runner = Win32ChildProcessRunner(
        # The stop-command spec must target the SAME absolute pg_ctl + data dir
        # the launch spec uses (audit A1: the relative default resolves against
        # System32 under the SCM).
        postgres_stop_spec=postgres_child_spec(
            pg_ctl_path=str(layout.pg_ctl_path), data_dir=postgres_data_dir
        ),
        # G3: the layout-resolved log root (not a re-derived PROGRAMDATA-env
        # read), matching every other layout-derived path this build already
        # threads through (audit A1's "every ... path is ABSOLUTE, derived
        # from layout" rule).
        log_root=layout.log_root,
    )
    supervisor = Supervisor(
        config=cfg,
        guard=guard,
        job_api=Win32JobObjectApi(),
        runner=runner,
        alert_outbox=alert_outbox,
        postgres_probe=postgres_probe,
        health_probe=health_probe,
        clock=time.monotonic,
        sleep=time.sleep,
        interlock_reader=lambda: read_interlock().status,
        # F1 (BLOCKER): the abort seam. ``Event.is_set`` is a bound method on the
        # SAME event the service run loop waits on, so a ``request_stop`` on the
        # SCM's stop thread is visible INSIDE an in-flight start()/tick() --
        # between children and inside every readiness poll -- instead of only
        # when the loop next comes around.
        should_abort=stop_event.is_set,
        rng=random.random,
        # Task #57 D2: the OPTIONAL ollama child -- launched only when the
        # staged binary + model store exist (build_ollama_spec_provider);
        # readiness is GET /api/version against the SAME base URL the app-side
        # clients dial. Always wired: an install without the local-AI pack
        # skips cleanly with one logged line (degraded AI, service healthy).
        ollama_spec_provider=build_ollama_spec_provider(layout, logger=logger),
        ollama_probe=ollama_probe or _default_ollama_version_probe,
        # CC-WS5-003: resolve the durable postmaster's pid from the SAME data_dir
        # the postgres child launches with (the absolute layout cluster dir), so
        # the supervisor contains + monitors the postmaster, not the self-exiting
        # ``pg_ctl -w`` launcher. The runner's open_existing opens that pid.
        postmaster_pid_reader=lambda: read_postmaster_pid(postgres_data_dir),
        postgres_data_dir=postgres_data_dir,
        pg_ctl_path=str(layout.pg_ctl_path),
        # Real-world LPM deployment fix (2026-08-27): thread the SAME
        # host/port default_dependency_provider parsed out of DATABASE_URL
        # (the D4-provisioned single source of truth) into the postgres
        # child's own launch argv, so a station whose port fell back off the
        # standard 5432 starts postgres on the port it was actually
        # provisioned with, not a silently wrong default.
        db_host=db_host,
        db_port=db_port,
        control_plane_host=network_runtime.upstream_host,
        control_plane_port=network_runtime.upstream_port,
        python_path=str(layout.python_path),
        # CC-WS5-003 review-fix: preserve the egress-work-dir security wiring.
        # The layout kwargs must ADD to, not REPLACE, the existing
        # program_data_root pass-through (it feeds the control plane's
        # CIVICCAST_EGRESS_WORK_DIR resolution via default_egress_work_dir).
        program_data_root=program_data_root,
        control_plane_env=control_plane_env,
    )
    # CC-WS5-007: wire the mutating admin verbs (start/stop/restart/drain/
    # runtime_set) to real, idempotent supervisor actions behind the pipe's
    # existing admin-tier authz gate. build_command_handler composes the read
    # tier (status/version -> core.command_handler) with the admin router into
    # the single serialized CommandQueue handler (AC-N5 holds across both tiers).
    admin_router = AdminCommandRouter(
        supervisor=supervisor,
        selector_reader=_production_selector_reader,
        selector_writer=_production_selector_writer,
        logger=logger,
    )
    command_handler = build_command_handler(supervisor, admin_router)
    control_pipe = build_control_pipe(command_handler, logger=logger)
    return SupervisorService(
        supervisor=supervisor,
        runner=runner,
        config=cfg,
        clock=time.monotonic,
        sleep=time.sleep,
        logger=logger,
        control_pipe=control_pipe,
        # F1: the same Event the supervisor's should_abort seam reads.
        stop_event=stop_event,
    )


# ---------------------------------------------------------------------------
# The pywin32 ServiceFramework shim (D1/D8) -- built lazily so this module
# imports on Linux.
# ---------------------------------------------------------------------------


def build_service_class(
    *,
    service_factory: Callable[[logging.Logger], SupervisorService],
    logging_configurator: Callable[[], logging.Logger] = configure_logging,
    singleton_factory: Callable[[], RuntimeOwnerMutex] = build_singleton_mutex,
) -> type:
    """Lazily construct the ``CivicCastSupervisorService`` ``ServiceFramework``
    subclass (D1/D8). The pywin32 service modules are imported HERE, not at
    module import, so ``import civiccast.native.supervisor.service`` succeeds on
    Linux; this factory is only ever called on Windows (by the installer / SCM
    entry point, or by the ``*_win.py`` shape test).

    ``SvcDoRun`` acquires the singleton (a second live supervisor is refused and
    the service exits cleanly), configures logging, and runs the service built
    by ``service_factory``; ``SvcStop`` requests the graceful stop chain and
    signals the SCM stop event."""

    import servicemanager
    import win32event
    import win32service  # type: ignore[import-untyped]
    import win32serviceutil  # type: ignore[import-untyped]

    class CivicCastSupervisorService(win32serviceutil.ServiceFramework):  # type: ignore[misc]
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        # Class-level default: the win-only tests construct this class through
        # ``__new__`` (bypassing the SCM-only ServiceFramework ``__init__``), so
        # the watchdog slot must exist without ``__init__`` having run.
        _stop_watchdog: StopWatchdog | None = None

        def __init__(self, args: list[str]) -> None:
            super().__init__(args)
            self._svc_stop_event = win32event.CreateEvent(None, 0, 0, None)
            self._service: SupervisorService | None = None
            self._singleton: RuntimeOwnerMutex | None = None

        def _stop_chain_position(self) -> str:
            service = self._service
            return "service-not-built" if service is None else service.stop_position

        def _report_service_stopped(self) -> None:
            """F2: the watchdog's SCM report. A force-exit that never told the
            SCM the service STOPPED leaves it believing STOP_PENDING; combined
            with the registered failure actions (restart/5000 + failureflag 1)
            that is how a fired watchdog resurrected the service into the
            uninstall it fired to unblock."""

            self.ReportServiceStatus(win32service.SERVICE_STOPPED)

        def _arm_stop_watchdog(self) -> None:
            if self._stop_watchdog is not None:
                return  # a second SvcStop must not stack a second timer
            watchdog = build_stop_watchdog(
                self._stop_chain_position, report_stopped=self._report_service_stopped
            )
            self._stop_watchdog = watchdog
            watchdog.arm()

        def _disarm_stop_watchdog(self) -> None:
            watchdog = self._stop_watchdog
            self._stop_watchdog = None
            if watchdog is not None:
                watchdog.disarm()

        def SvcStop(self) -> None:  # noqa: N802 (pywin32-mandated method name)
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            # Armed BEFORE request_stop: from this instant the host has a
            # bounded obligation to reach STOPPED (run-17 wedge, see StopWatchdog).
            self._arm_stop_watchdog()
            if self._service is not None:
                self._service.request_stop()
            win32event.SetEvent(self._svc_stop_event)

        def SvcDoRun(self) -> None:  # noqa: N802 (pywin32-mandated method name)
            # The watchdog disarm is the OUTERMOST finally on purpose: it must
            # cover every other teardown step in this method (including
            # ``singleton.release()`` and the early singleton-refusal return),
            # so the watchdog is genuinely the last resort and never fires for
            # work that was still going to finish.
            try:
                logger = logging_configurator()
                singleton = singleton_factory()
                acquired = singleton.acquire()
                if acquired.status not in ("acquired", "acquired_abandoned"):
                    logger.error(
                        "singleton not acquired (%s): %s -- another supervisor owns the station",
                        acquired.status,
                        acquired.detail,
                    )
                    servicemanager.LogErrorMsg(
                        f"CivicCastSupervisor: singleton not acquired ({acquired.status}); exiting"
                    )
                    return
                self._singleton = singleton
                try:
                    self._service = service_factory(logger)
                    self._service.run()
                finally:
                    singleton.release()
            finally:
                self._disarm_stop_watchdog()

    return CivicCastSupervisorService


# ---------------------------------------------------------------------------
# The installable SCM entry point (CC-WS5-007 part 1) -- built lazily so this
# module imports on Linux; pywin32 is only touched when the SCM verbs actually
# run (never at import, never in the pure tests).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductionDependencies:
    """The cross-module seams ``build_production_service`` takes as injected
    parameters (service docstring's disclosed-seams note): the guard monitor,
    the alerting outbox, the readiness probes, and the ProgramData root. Bundled
    so the SCM entry point's ``service_factory`` can assemble them once (on the
    VM, inside ``SvcDoRun``) and hand them to ``build_production_service``."""

    guard: GuardLike
    alert_outbox: AlertOutbox
    postgres_probe: Callable[[], bool]
    health_probe: Callable[[], ControlPlaneHealthProbe]
    program_data_root: str | None = None
    control_plane_env: dict[str, str] = field(default_factory=dict)
    # Task #57 D2: the ollama readiness probe (GET /api/version). ``None``
    # lets build_production_service fall back to its default probe.
    ollama_probe: Callable[[], bool] | None = None
    # Real-world LPM deployment fix (2026-08-27): the host/port
    # default_dependency_provider parsed out of DATABASE_URL, so the postgres
    # child's launch argv agrees with the D4-provisioned DatabaseUrl (the
    # single source of truth) instead of build_production_service's own
    # "127.0.0.1"/5432 defaults.
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    network_settings: NetworkSettings = field(default_factory=NetworkSettings)


DependencyProvider = Callable[[], ProductionDependencies]
ServiceFactory = Callable[[logging.Logger], SupervisorService]
ServiceClassBuilder = Callable[..., type]
CommandLineHandler = Callable[[type, list[str] | None], int]


# CC-WS5-007: the supervisor restart-escalation AlertConditionKind. The
# alerting store's ``record_alert_condition`` docstring names "S9's
# restart-escalation proof event" as its first upstream producer; ``service-down``
# is that S9 reliability condition (alerting/models.py line ~64, the same kind
# ``delivery.py``/``resource_sampler.py`` raise for "the CivicCast egress service
# is not running") -- the supervisor's D5 restart-storm escalation is exactly that
# operational condition. Held as a module constant (not inlined) so the
# ``AlertConditionKind`` literal is stated once, next to the rationale.
_SUPERVISOR_ALERT_KIND: AlertConditionKind = "service-down"

# Provider ENV keys + their production defaults (read at call time in the host
# process, never at import). DATABASE_URL is the ONLY required one.
_ENV_CONTROL_PLANE_URL = "CIVICCAST_CONTROL_PLANE_URL"
_DEFAULT_CONTROL_PLANE_URL = "http://127.0.0.1:8000"
_HEALTH_HTTP_TIMEOUT_SECONDS = 2.0
# The control-plane /health body fields ControlPlaneHealthProbe accepts beyond
# the always-present status_code (children.ControlPlaneHealthProbe; extra fields
# are forbidden by the model, so only these are copied through).
_HEALTH_BODY_FIELDS = ("mode", "workers_started", "mutating_disabled", "mode_contract")


class _AlertingOutbox:
    """Concrete :class:`~civiccast.native.supervisor.core.AlertOutbox` binding.
    ``core.py`` fires an abstract operational condition; this layer owns the live
    SQLAlchemy ``Session`` + ``AlertConditionKind`` binding it explicitly deferred
    here. Each ``fire`` opens a session from the injected factory, records the
    supervisor restart-escalation condition via
    ``civiccast.alerting.store.record_alert_condition``, commits, and closes --
    self-contained so a single alert can never leak a session."""

    def __init__(self, session_factory: Callable[[], Session], resource_ref: str) -> None:
        self._session_factory = session_factory
        self._resource_ref = resource_ref

    def fire(self, *, summary: str, detail: str) -> None:
        """Record the alert condition -- and NEVER propagate (audit A4). An
        alert INSERT against a schema-less or degraded DB raising here would
        escalate the alert moment into a service crash (fire is called from
        ``core.tick`` -> ``run`` -> ``SvcDoRun`` with no boundary above); the
        alert transport failing is logged and swallowed, supervision continues."""

        try:
            # Imported lazily so the provider's module graph stays light and the
            # alerting package is only touched when an alert actually fires.
            from civiccast.alerting.store import record_alert_condition

            session = self._session_factory()
            try:
                record_alert_condition(
                    session,
                    kind=_SUPERVISOR_ALERT_KIND,
                    resource_ref=self._resource_ref,
                    source_section="supervisor",
                    summary=summary,
                    detail=detail,
                )
                session.commit()
            finally:
                session.close()
        except Exception:
            logging.getLogger(LOGGER_NAME).exception(
                "alert delivery failed (summary=%r); continuing -- alerting must never "
                "kill the supervisor",
                summary,
            )


def default_dependency_provider() -> ProductionDependencies:
    """Assemble the REAL production dependencies in the host process (called by
    the module-level ``service_host`` factory inside ``SvcDoRun`` -- CC-WS5-007).

    CONSTRUCTION is Win32-free and CI-constructible on Linux: the guard's
    ``win_probes`` callables and the ``RuntimeOwnerMutex().probe`` bound method are
    passed BY REFERENCE and NOT invoked here (per the CC-WS4-004 caution -- the
    first ``probe()`` acquires the mutex lazily in the host process, and every
    other probe only fires when the guard evaluates), so no ``winreg``/pywin32/
    ``wsl.exe`` call happens at build time. Only ``DATABASE_URL`` is a hard-fail:
    a real supervisor needs its DB to bind the alerting ``Session``. The live
    SCM/LocalSystem/Session-0 run plus real DB/control-plane I/O stay
    owner-VM/cleanroom bound (``evidence/PENDING.md``)."""

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError(
            "DATABASE_URL is unset or empty; the production supervisor dependency "
            "provider needs a database URL to bind the alerting Session. Set "
            "DATABASE_URL in the service environment before running under the SCM."
        )

    network_settings = NetworkSettingsStore(default_network_settings_path()).load()
    network_runtime = network_settings.runtime_values()

    # Real-world LPM deployment fix (2026-08-27): parse the postgres host/port
    # out of the SAME DATABASE_URL every other seam below connects to, the
    # SAME way civiccast.native.upgrade.pg_lifecycle.derive_pg_lifecycle_paths
    # already does for the D3 upgrade engine -- D4 provisioning's DatabaseUrl
    # registry value is this station's single source of truth for the actual
    # postgres port (it may have fallen back off the standard 5432; see
    # civiccast.native.provision.port_select), and until this fix nothing
    # threaded that value into the postgres CHILD's own launch argv --
    # postgres_probe (a live SELECT 1, below) correctly dialed the configured
    # port, but build_production_service silently used its own "127.0.0.1"/
    # 5432 defaults to actually START the server, so a station whose port
    # fell back would start postgres on the WRONG port forever. An unparsable
    # database_url falls back to those same defaults silently here -- it is
    # diagnosed loudly elsewhere (postgres_probe/engine connect below fail
    # with the real cause).
    db_host = "127.0.0.1"
    db_port = 5432
    with contextlib.suppress(Exception):
        parsed_db_url = make_url(normalize_database_url(database_url))
        db_host = parsed_db_url.host or db_host
        db_port = parsed_db_url.port or db_port

    # DB engine + Session factory (same create_engine pattern as app.py).
    # normalize_database_url rewrites a bare `postgresql://` scheme (which
    # SQLAlchemy maps to the psycopg2 dialect, never installed -- ADR 0008
    # ships psycopg v3 only) to `postgresql+psycopg://`; this is the exact
    # URL the installer persists to the registry (beta BLOCKER #51).
    engine = create_engine(
        normalize_database_url(database_url),
        future=True,
        pool_pre_ping=True,
        # A hang here blocks SERVICE START under the SCM (task #51):
        # psycopg v3 without connect_timeout can hang minutes on Windows.
        # The stop watchdog's F1 in-flight term is DERIVED from
        # _DB_CONNECT_TIMEOUT_SECONDS, so the connect bound must be pinned to
        # that same constant. The explicit override exists precisely so
        # CIVICCAST_DB_CONNECT_TIMEOUT tuning cannot desynchronize them --
        # sol audit 2026-08-09 reproduced watchdog force-exit during a
        # legitimate stop chain at env=60 (F1=11, worst-case 180 > 150).
        **connect_options(database_url, timeout_seconds=_DB_CONNECT_TIMEOUT_SECONDS),
    )
    session_factory = sessionmaker(engine, future=True)

    # Guard: real production win_probes callables + a single RuntimeOwnerMutex's
    # bound .probe -- all BY REFERENCE (constructed here, only called when the
    # guard evaluates in the host process). RuntimeOwnerMutex() construction is
    # Win32-free (build_singleton_mutex is already Linux-tested).
    guard = GuardMonitor(
        selector_reader=read_selector,
        a1_probe=probe_keeper,
        a2_probe=probe_indistro_services,
        mutex=RuntimeOwnerMutex().probe,
        interlock_reader=read_interlock,
        wsl_install_detector=detect_wsl_install,
        clock=lambda: datetime.now(UTC),
    )

    # Alerting outbox bound to the live Session factory + this station's host id.
    outbox = _AlertingOutbox(session_factory, socket.gethostname())

    default_host = network_runtime.upstream_host
    if ":" in default_host:
        default_host = f"[{default_host}]"
    default_control_plane_url = f"http://{default_host}:{network_runtime.upstream_port}"
    control_plane_url = (
        os.environ.get(_ENV_CONTROL_PLANE_URL, "").strip() or default_control_plane_url
    )

    def postgres_probe() -> bool:
        """Cheap DB liveness (``SELECT 1``). G2: deliberately does NOT catch here
        -- ``check_postgres_ready`` (children.py) already wraps this call in a
        fail-closed ``except Exception`` and formats the exception TEXT into
        ``ReadinessResult.detail``. A redundant catch here would swallow that
        exception into a bare ``False``, destroying the detail the readiness gate
        (and G2's logging) needs to diagnose a real failure (run 17's diagnosis
        was impossible from logs partly because of exactly this). Any error
        still fails closed -- it just fails closed ONE layer up, with detail
        intact."""

        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        return True

    def health_probe() -> ControlPlaneHealthProbe:
        """``GET <control-plane>/health`` via stdlib ``urllib`` (no new dep),
        parsed into a ``ControlPlaneHealthProbe``. Any error yields a probe with
        the HTTP code (or 0) so the readiness gate fails CLOSED, never raises."""

        health_url = control_plane_url.rstrip("/") + "/health"
        try:
            # The control-plane URL is operator-overridable (an environment
            # variable with a loopback default), so the scheme is NOT a
            # compile-time constant here. urlopen honours file:/ and every
            # other scheme urllib knows; a mis-set variable would turn a
            # health probe into a local file read whose contents then get
            # JSON-parsed as a health body. Reject anything but HTTP(S)
            # before opening it. Raising inside this try is deliberate --
            # the except below already fails the probe CLOSED.
            if urllib.parse.urlparse(health_url).scheme not in ("http", "https"):
                raise ValueError(f"refusing non-HTTP control-plane URL: {health_url!r}")
            with urllib.request.urlopen(health_url, timeout=_HEALTH_HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310  # nosec B310 - scheme checked immediately above
                status_code = int(getattr(resp, "status", 0) or 0)
                raw_body = resp.read()
        except urllib.error.HTTPError as exc:
            return ControlPlaneHealthProbe(status_code=int(exc.code))
        except Exception:
            return ControlPlaneHealthProbe(status_code=0)

        body_fields: dict[str, Any] = {}
        try:
            parsed = json.loads(raw_body)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            for key in _HEALTH_BODY_FIELDS:
                if key in parsed:
                    body_fields[key] = parsed[key]
        try:
            return ControlPlaneHealthProbe(status_code=status_code, **body_fields)
        except Exception:
            # A malformed body (wrong field types) must not raise out of the
            # probe -- fail closed on the status_code alone.
            return ControlPlaneHealthProbe(status_code=status_code)

    # Convention fix (audit A1 pass): ``program_data_root`` here feeds
    # ``core.Supervisor`` -> ``children.default_egress_work_dir``, which appends
    # ``\CivicCast\data\egress`` ITSELF -- so this must be the ProgramData ROOT.
    # The old provider passed ``<pd>\CivicCast`` and produced a doubled
    # ``...\CivicCast\CivicCast\data\egress`` egress work dir in the control
    # plane's env. ``station_environment_for_python`` uses the OTHER convention
    # (it wants the ``<pd>\CivicCast`` data root), so that path is built
    # separately below.
    pd_root = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    program_data_root = pd_root
    civiccast_data_root = str(Path(pd_root) / "CivicCast")
    control_plane_env: dict[str, str] = {}
    embedded_python = Path(sys.executable)
    if embedded_python.parent.name.casefold() == "runtime":
        # Audit A2: a FRESH install is provisioned but not yet ACTIVATED --
        # station-set.json / the activation receipt / the caption model do not
        # exist yet. That must NOT kill the service (this provider runs inside
        # SvcDoRun with no boundary above): catch the distinct typed
        # ``NativeStationNotActivatedError`` (station_runtime), log one clear
        # line, and start WITHOUT the ACTIVATED station env overlay so the
        # supervisor and its children come up and the service stays RUNNING. A
        # CORRUPT activated station (the parent
        # NativeStationConfigurationError) still fails loud -- fail-closed on
        # tampering, tolerant only of not-yet.
        #
        # Chain L (TESTER2 request-0050c: install PASS, service RUNNING,
        # /health 200, /operator/ 404). That degrade used to hand the child an
        # EMPTY env, which threw away two things that were already true and had
        # nothing to do with activation: where the packaged operator console
        # and resident portal are (they arrive with the native-app-payload pack
        # and are on disk from pack-staging time), and the installer's setup
        # nonce (persisted at D4 provision time). So a freshly installed
        # station served no front door AND could not have completed setup
        # through one -- the exact state TESTER2 reported. It now starts with
        # the PRE-ACTIVATION overlay, which carries those and deliberately
        # withholds CIVICCAST_NATIVE_STATION so
        # ``installer/service.py``'s ``_native_station_activated`` keeps
        # failing closed.
        from civiccast.native.station_runtime import (
            NativeStationNotActivatedError,
            pre_activation_control_plane_environment,
            station_environment_for_python,
        )

        try:
            control_plane_env = station_environment_for_python(
                embedded_python,
                program_data_root=civiccast_data_root,
            )
        except NativeStationNotActivatedError:
            control_plane_env = pre_activation_control_plane_environment()
            logging.getLogger(LOGGER_NAME).warning(
                "station not yet activated; supervisor starting in pre-activation mode "
                "(packaged portals and the setup handoff are still served so first-run "
                "setup can be completed)"
            )

    control_plane_env.update(
        {
            "CIVICAST_ACTIVE_NETWORK_SETTINGS": network_settings.model_dump_json(),
            "CIVICAST_PUBLIC_BASE_URL": network_runtime.public_base_url or "",
            "CIVICAST_TRUSTED_PROXY_CIDRS_EXACT": ",".join(network_runtime.trusted_proxy_cidrs),
        }
    )

    return ProductionDependencies(
        guard=guard,
        alert_outbox=outbox,
        postgres_probe=postgres_probe,
        health_probe=health_probe,
        program_data_root=program_data_root,
        control_plane_env=control_plane_env,
        # Task #57 D2: readiness for the OPTIONAL ollama child -- the bounded
        # GET /api/version against the app-side clients' base URL.
        ollama_probe=_default_ollama_version_probe,
        # Real-world LPM deployment fix (2026-08-27): the postgres child's
        # launch host/port, parsed from the SAME DATABASE_URL above -- see
        # this function's docstring.
        db_host=db_host,
        db_port=db_port,
        network_settings=network_settings,
    )


def _read_int_env(name: str, default: int) -> int:
    """Read an int ENV var, falling back to ``default`` when unset or non-numeric
    (a garbled port must not crash provider construction)."""

    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def build_production_service_factory(
    *, dependency_provider: DependencyProvider = default_dependency_provider
) -> ServiceFactory:
    """Build the ``service_factory`` the SCM entry point wires into the
    ServiceFramework class: a ``(logger) -> SupervisorService`` that pulls the
    guard/probes/outbox from ``dependency_provider`` and calls
    ``build_production_service``. The provider is a seam so the assembly logic is
    unit-proven with fakes while the concrete provider stays the VM integration
    (``default_dependency_provider``)."""

    def factory(logger: logging.Logger) -> SupervisorService:
        deps = dependency_provider()
        return build_production_service(
            logger,
            guard=deps.guard,
            alert_outbox=deps.alert_outbox,
            postgres_probe=deps.postgres_probe,
            health_probe=deps.health_probe,
            ollama_probe=deps.ollama_probe,
            program_data_root=deps.program_data_root,
            control_plane_env=deps.control_plane_env,
            db_host=deps.db_host,
            db_port=deps.db_port,
            network_settings=deps.network_settings,
        )

    return factory


def _run_service_command_line(service_class: type, argv: list[str] | None) -> int:
    """Hand the built ServiceFramework class + the SCM argv to
    ``win32serviceutil.HandleCommandLine`` -- the pywin32 dispatcher that
    implements ``install``/``update``/``start``/``stop``/``remove`` and the
    ``SvcDoRun`` service-host mode. Lazily imports pywin32 (Windows-only); the
    pure tests inject a fake handler instead. Real SCM registration is VM-bound
    (``evidence/PENDING.md``)."""

    import win32serviceutil

    return int(win32serviceutil.HandleCommandLine(service_class, argv=argv) or 0)


def _default_service_class_builder(*, service_factory: ServiceFactory) -> type:
    """The DEFAULT (production) SCM class provider. On Windows the SCM-registered
    class MUST be the MODULE-LEVEL, import-resolvable
    ``service_host.CivicCastSupervisorService`` (CC-WS5-007): a function-local
    class persists a class string the separate SCM host process cannot resolve,
    and a ``service_factory`` CLOSURE cannot cross into that host process. The
    host class therefore builds its production dependencies IN the host process
    via its OWN module-level provider; the ``service_factory`` argument is
    accepted only for seam-compatibility with ``build_service_class`` (which the
    pure tests still inject) and is intentionally unused here -- the closure is
    exactly what cannot cross the process boundary. ``service_host`` imports
    pywin32 at module load, so it is imported LAZILY here (never at ``service``
    module load) to keep this module importable on Linux."""

    from civiccast.native.supervisor import service_host

    return service_host.CivicCastSupervisorService


def main(
    argv: list[str] | None = None,
    *,
    service_factory: ServiceFactory | None = None,
    class_builder: ServiceClassBuilder = _default_service_class_builder,
    command_line_handler: CommandLineHandler = _run_service_command_line,
) -> int:
    """CC-WS5-007 part 1: the installable service entry point
    (``python -m civiccast.native.supervisor.service <verb>``). Registers the
    MODULE-LEVEL ``service_host.CivicCastSupervisorService`` ServiceFramework
    subclass (import-resolvable by the separate SCM host, which builds the
    production dependencies IN the host process -- CC-WS5-007) and dispatches the
    SCM verbs through ``win32serviceutil.HandleCommandLine``, so
    ``install``/``update``/``start``/``stop``/``remove`` all work and the SCM can
    host the service (``SvcDoRun``).

    ``service_factory``/``class_builder``/``command_line_handler`` are injectable
    seams so the wiring (which class, which argv, which factory) is proven on any
    OS without pywin32; the DEFAULT ``class_builder`` returns the module-level
    host class. The real SCM registration + Session-0 boot is the owner VM proof
    (disclosed)."""

    factory = service_factory or build_production_service_factory()
    service_class = class_builder(service_factory=factory)
    return command_line_handler(service_class, argv)


__all__ = [
    "CONTROL_PLANE_LOG_NAME",
    "DEFAULT_LOG_ROOT",
    "LOG_BACKUP_COUNT",
    "LOG_MAX_BYTES",
    "PACKAGE_LOGGER_NAME",
    "SERVICE_DESCRIPTION",
    "ChildStopResult",
    "CommandLineHandler",
    "ControlPipeLike",
    "DependencyProvider",
    "EventLogFn",
    "ProductionDependencies",
    "ServiceClassBuilder",
    "ServiceFactory",
    "SupervisorService",
    "Win32ChildProcessRunner",
    "build_control_pipe",
    "build_control_plane_media_env",
    "build_ollama_spec_provider",
    "build_production_service",
    "build_production_service_factory",
    "build_service_class",
    "build_singleton_mutex",
    "child_log_path",
    "configure_control_plane_logging",
    "configure_logging",
    "default_dependency_provider",
    "main",
]


if __name__ == "__main__":  # pragma: no cover - the real SCM/CLI entry, VM-bound
    import sys

    sys.exit(main())
