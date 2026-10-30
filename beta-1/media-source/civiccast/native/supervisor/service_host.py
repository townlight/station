# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""CC-WS5-007 (round 4): the MODULE-LEVEL, import-resolvable SCM service host.

**Windows-only.** This module imports pywin32 at the TOP, so it MUST NOT be
imported at module-load time by ``service.py`` (which stays Linux-importable) --
``service.py`` imports it LAZILY inside ``main()`` on Windows, and the SCM host
process (``pythonservice.exe``) imports it to resolve the persisted service
class string. No Linux-run test imports this module.

Beta BLOCKER #48 (2026-07-30): this docstring previously claimed ``SvcDoRun``
"resolves the persisted service config" -- it did not; nothing bridged the
installer-persisted ``DatabaseUrl`` registry value into the service process's
environment, so ``default_dependency_provider`` always found ``DATABASE_URL``
empty and crashed the service on every real install (Sandbox gauntlet run 11:
pywin32 exit 0x20000001, SCM auto-restarts twice, then STOPPED/1066). Fixed:
``SvcDoRun`` now calls
:func:`civiccast.native.supervisor.service_env.ensure_database_url_env` FIRST,
before the singleton mutex or ``_service_factory`` run -- it reads
``HKLM\\SOFTWARE\\CivicCast\\Native\\DatabaseUrl`` via ``winreg`` ONLY when
``DATABASE_URL`` is not already set in the environment (an explicit env var
always wins, e.g. an operator/test override), and raises a precise, fail-loud
error naming both the registry path and the env var (never the URL value,
which carries the DB password) when neither source has one. See
``service_env.py`` for the cross-platform-importable bridge implementation and
its constants (pinned to the Rust writer in ``native_service_registration.rs``).

Why this module exists (the CC-WS5-007 critical fix)
----------------------------------------------------
The earlier ``service.build_service_class`` created ``CivicCastSupervisorService``
as a **function-local** class (qualname
``build_service_class.<locals>.CivicCastSupervisorService``). pywin32 persists a
service class string of ``module + cls.__name__`` -- i.e.
``civiccast.native.supervisor.service.CivicCastSupervisorService`` -- but there
is NO module-global class of that name, so when the separate SCM host process
imports the module and resolves the persisted string it finds NOTHING and the
service cannot start. Separately, the ``service_factory`` was a CLOSURE built in
the INSTALLER process; the SCM host is a SEPARATE process that cannot receive
that closure (``default_dependency_provider`` would then raise). Both are fixed
here:

* the service class is **module-level** (``CivicCastSupervisorService`` below),
  so its persisted class string
  (``civiccast.native.supervisor.service_host.CivicCastSupervisorService``)
  resolves from a fresh import in the host process; and
* its ``SvcDoRun`` builds the production dependencies **IN this host process**
  via the module-level :data:`_service_factory` (the production assembly, NOT an
  installer closure).

Everything the class needs to build the service (the singleton mutex, the
logging configurator, the production service factory) is referenced as a
module-level name, so a Windows-only test can substitute fakes (via
``monkeypatch``) and drive ``SvcDoRun`` through ``__new__`` -- proving the
in-process assembly and the fail-loudly-on-a-raising-provider path -- without
registering or running under the real SCM.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import servicemanager  # type: ignore[import-untyped]
import win32event
import win32service  # type: ignore[import-untyped]
import win32serviceutil  # type: ignore[import-untyped]

from civiccast.native.supervisor import install_layout, start_failure_marker
from civiccast.native.supervisor.config import DISPLAY_NAME, SERVICE_NAME
from civiccast.native.supervisor.service import (
    SERVICE_DESCRIPTION,
    ServiceFactory,
    StopWatchdog,
    SupervisorService,
    build_production_service_factory,
    build_singleton_mutex,
    build_stop_watchdog,
    configure_logging,
)
from civiccast.native.supervisor.service_env import ensure_database_url_env

if TYPE_CHECKING:
    from civiccast.native.win_probes import RuntimeOwnerMutex


# The production service factory, built ONCE at module load (a pure closure --
# ``default_dependency_provider`` is not called until the factory runs). It is
# invoked IN the host process by ``SvcDoRun`` so the concrete dependencies are
# assembled where the service actually runs, never as a cross-process closure.
# A module-level name so a win-only test can monkeypatch it with a fake provider.
_service_factory: ServiceFactory = build_production_service_factory()

# Where `record_start_failure`/`record_start_success` (start_failure_marker)
# read and write their counter/marker -- a module-level SEAM, the SAME shape
# as `_service_factory` above, so a win-only test can monkeypatch it to an
# isolated tmp_path root instead of ever touching this machine's REAL
# `%PROGRAMDATA%\CivicCast` (which, on a station that is actually
# crash-looping, is live state a test run must never write into).
_civiccast_data_root_provider: Callable[[], Path] = install_layout.default_civiccast_data_root


class CivicCastSupervisorService(win32serviceutil.ServiceFramework):  # type: ignore[misc]
    """The MODULE-LEVEL ``ServiceFramework`` subclass the SCM registers and hosts
    (D1/D8). Because it is defined at module scope, pywin32's persisted class
    string (``civiccast.native.supervisor.service_host.CivicCastSupervisorService``)
    resolves from a fresh import in the SCM host process -- the CC-WS5-007 fix.

    ``SvcDoRun`` acquires the ``Global\\CivicCastSupervisorSingleton`` mutex (a
    second live supervisor is refused and the service exits cleanly), configures
    logging, builds the production :class:`SupervisorService` IN THIS host process
    (via the module-level :data:`_service_factory` -- the production assembly, NOT
    an installer closure), and runs it; ``SvcStop`` requests the graceful stop
    chain and signals the SCM stop event."""

    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = DISPLAY_NAME
    _svc_description_ = SERVICE_DESCRIPTION

    # Class-level default so instances built through ``__new__`` (how the
    # win-only tests construct this class without an SCM handle) still have the
    # slot ``SvcStop``/``SvcDoRun`` read.
    _stop_watchdog: StopWatchdog | None = None

    def __init__(self, args: list[str]) -> None:
        super().__init__(args)
        self._svc_stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._service: SupervisorService | None = None
        self._singleton: RuntimeOwnerMutex | None = None

    def _stop_chain_position(self) -> str:
        """Best available answer to "where did the stop chain get to?", read by
        the watchdog's one log line if it ever has to force-exit this host."""

        service = self._service
        return "service-not-built" if service is None else service.stop_position

    def _report_service_stopped(self) -> None:
        """F2 (BLOCKER, 2026-07-31): the watchdog's SCM report, made just before
        it force-exits this host. Without it the process vanished while the SCM
        still had the service in STOP_PENDING, and -- because registration
        applies ``sc failure ... actions= restart/5000`` with ``sc failureflag
        1`` (failure actions on ANY nonzero exit) -- the old nonzero watchdog
        exit code had the SCM restart the service ~5s later, into the middle of
        the uninstall the watchdog fired to unblock. Reporting STOPPED and
        exiting 0 is the pair that fixes it."""

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
        # Armed BEFORE request_stop: from this instant the host has a BOUNDED
        # obligation to reach STOPPED. Gauntlet run 17 showed what the absence
        # of that bound costs -- STOP_PENDING with SCM checkpoint 0x1 for 112+
        # seconds, which then failed repair (79), uninstall (82, tree left
        # behind) and every subsequent install (120). See
        # ``service.StopWatchdog``.
        self._arm_stop_watchdog()
        if self._service is not None:
            self._service.request_stop()
        win32event.SetEvent(self._svc_stop_event)

    def SvcDoRun(self) -> None:  # noqa: N802 (pywin32-mandated method name)
        # The watchdog disarm is the OUTERMOST finally on purpose: it must cover
        # every other teardown step in this method (``singleton.release()``, the
        # early singleton-refusal return, and a raising ``ensure_database_url_env``
        # /factory), so the watchdog is genuinely the LAST resort and can never
        # pre-empt a step that was still going to finish.
        try:
            from civiccast.native.identity_migration import MIGRATION_ENV, migrate_old_account

            if os.environ.get(MIGRATION_ENV) == "1":
                migrate_old_account()
                return
            # Beta BLOCKER #48: bridge the installer-persisted registry DatabaseUrl
            # into this process's environment BEFORE anything else runs, so
            # _service_factory's default_dependency_provider (service.py) finds
            # DATABASE_URL populated. Raises loudly (uncaught, like a raising
            # provider below) naming the registry path + env var, never the value,
            # if neither source has one -- surfaced by pywin32 in the Event Log.
            ensure_database_url_env()
            logger = configure_logging()
            # Adjacent diagnosability fix (2026-08-12, TESTER2 b5 evidence):
            # an unconditional canary line, independent of what any child or
            # skip-dedup path does or doesn't log. TESTER2's entire b5 run
            # left supervisor.log looking empty to size-based checks with no
            # way to tell "the file sink never worked this run" apart from
            # "it worked but nothing has needed to log since." This line
            # answers that question every single run, at the earliest
            # possible point after the file sink is wired -- before the
            # singleton, before any child is even considered.
            log_paths = [getattr(handler, "baseFilename", None) for handler in logger.handlers]
            logger.info(
                "supervisor logging initialized (pid %s, sinks %s)",
                os.getpid(),
                [path for path in log_paths if path],
            )
            singleton = build_singleton_mutex()
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
                # Build the production dependencies IN THIS host process (not from a
                # cross-process closure): a raising provider therefore fails LOUDLY
                # here rather than silently short-circuiting a missing closure.
                #
                # Field evidence (candidate 4eca729, 2026-08-29): a raising
                # provider crashed the host with NOTHING recorded beyond the
                # "logging initialized" canary above -- the exception propagated
                # straight past this frame to the SCM, which only the Windows
                # Event Log ever saw. `record_start_failure` logs the REAL
                # reason to supervisor.log before this re-raises (behavior is
                # otherwise byte-for-byte unchanged: still fails loud, still
                # exits, still lets the SCM's own restart policy decide what
                # happens next), and once the SAME failure has recurred
                # `CONSECUTIVE_FAILURE_THRESHOLD` times running, it also
                # writes an operator-readable marker document so a crash loop
                # is never silent even to someone who never checks the Event
                # Log at all. `record_start_success` clears both the instant a
                # start actually works again.
                civiccast_data_root = _civiccast_data_root_provider()
                try:
                    self._service = _service_factory(logger)
                except Exception as error:
                    start_failure_marker.record_start_failure(civiccast_data_root, logger, error)
                    raise
                start_failure_marker.record_start_success(civiccast_data_root)
                self._service.run()
            finally:
                singleton.release()
        finally:
            self._disarm_stop_watchdog()


def main(argv: list[str] | None = None) -> int:
    """The Windows SCM entry point that registers the MODULE-LEVEL service class
    with ``win32serviceutil.HandleCommandLine`` -- so the persisted class string
    is the import-resolvable module-global the SCM host can find. Actually running
    under the SCM (Session-0 boot, LocalSystem) is the owner VM proof
    (``evidence/PENDING.md``)."""

    return int(win32serviceutil.HandleCommandLine(CivicCastSupervisorService, argv=argv) or 0)


__all__ = [
    "CivicCastSupervisorService",
    "main",
]


if __name__ == "__main__":  # pragma: no cover - the real SCM/CLI entry, VM-bound
    import sys

    sys.exit(main())
