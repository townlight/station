# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Job Object containment for the native supervisor (spec D3).

The supervisor's containment guarantee (AC4: "kill the SUPERVISOR mid-playout
-> Job Object kills the tree, no orphan postgres.exe/python
workers survive") rests on THREE Win32 primitives, wrapped here so no other
module in this package touches ``win32job``/``win32api`` directly:

1. ``CreateJobObject`` + ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` -- the job's
   last handle closing (including the implicit close on process exit/crash,
   which the kernel always performs) kills every process still assigned to
   it. Breakaway is left DISABLED (D3): ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` /
   ``_SILENT_BREAKAWAY_OK`` are never set, so a descendant cannot detach
   itself from the containment tree. Workers spawned by the control-plane
   daemon (D2) are captured automatically as descendants of the
   ``postgres``/``control_plane`` direct children this module assigns -- the
   supervisor never assigns a worker pid itself.
2. ``AssignProcessToJobObject`` -- ``JobObjectController.assign_child(pid)``,
   the seam ``core.py``'s child-lifecycle code calls once per direct child
   after it launches.
3. ``OpenJobObject`` + ``TerminateJobObject`` by the well-known job name --
   the D3 "straggler sweep... run before spawning (defense in depth)". The
   common case is kill-on-close having already reaped everything, so
   ``OpenJobObject`` fails with ``ERROR_FILE_NOT_FOUND`` and there is nothing
   to sweep; a same-named job surviving past its creator (some handle
   somewhere kept it alive -- e.g. a crash sequence that raced the kernel's
   own handle-table teardown, or a future code path that duplicates the
   handle) is the defense-in-depth case: reopen it by NAME, list its current
   member pids, ``TerminateJobObject`` them, and close. This is the
   Windows-native form of "sweep by job-name"; it needs no process-marker
   scanning (no ``psutil`` environ/cmdline scan, which can itself fail across
   privilege/session boundaries) because the job object itself is the
   authoritative, kernel-maintained member list -- a strictly stronger
   source of truth than a marker guess.

Pure-testable seam (D3 build note: "assign/sweep control logic with a fake
job API"). ``JobObjectApi`` is the injectable Protocol; ``JobObjectController``
(assign) and :func:`sweep_stragglers` (sweep) are pure control logic over it,
exercised in ``tests/native/test_supervisor_job_object.py`` with a fake that
never touches Win32, on any OS. ``Win32JobObjectApi`` is the one real
implementation, and it is the ONLY thing in this module that imports
``win32job``/``win32api``/``pywintypes`` -- always lazily, inside each method,
per the house rule that ``import civiccast.native.*`` must succeed on Linux.
``tests/native/test_supervisor_job_object_win.py`` (``win`` in the filename
per the D3/CI naming convention, ``pytest.mark.skipif(os.name != "nt")``)
exercises it against real Win32 objects and a real spawned child tree.

Empirical corrections applied from ``win_probes.py``'s module docstring
(disclosed there as house-wide, not WS4-specific):

- **Handle lifetime.** The process handle ``win32api.OpenProcess`` opens
  purely to make ``AssignProcessToJobObject``/``IsProcessInJob`` calls is
  closed immediately after each call (``finally``), never parked on
  ``self`` -- the job keeps its own reference to the assigned process; an
  extra open handle per call would leak one kernel handle per assign, exactly
  the class of bug ``RuntimeOwnerMutex`` was fixed for (round-2 audit,
  ``win_probes.py:625-628``).
- **Explicit SD, never the default DACL (F-JOB-1).** The job is a GLOBAL
  named object; the program discipline (spec D7 for the pipe; the singleton
  and runtime-owner mutexes) is an EXPLICIT security descriptor, never the
  token-default DACL. ``create_job`` builds a ``SECURITY_ATTRIBUTES`` from
  ``JOB_OBJECT_SDDL`` (identical to the mutex SDDL: SYSTEM +
  BUILTIN\\Administrators GENERIC_ALL, nobody else) so an unprivileged token
  can neither open nor ``TerminateJobObject`` the station's process tree.
  ``read_dacl_sddl`` reads it back for the ``*_win.py`` SD proof; note
  ``ConvertSecurityDescriptorToStringSecurityDescriptor`` normalizes "GA" to
  the object-specific mask on readback, so that test asserts on the SID
  markers (``;;;SY)``, ``;;;BA)``, absence of ``;;;WD``), not the literal "GA".
- **Empirically verified, not merely assumed** (spike transcript in this
  unit's evidence): ``QueryInformationJobObject(..., JobObjectBasicProcessIdList)``
  returns a plain tuple of ints (``(pid, ...)``), not a dict keyed by a
  ``"ProcessIdList"`` field as the MSDN struct name might suggest --
  confirmed on this dev box before being relied on in
  :meth:`Win32JobObjectApi.list_job_process_ids`.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict

from civiccast.native.runtime_guard import MUTEX_SDDL

_LOGGER = logging.getLogger(__name__)

# D3 identity: the well-known job name the straggler sweep reopens by. No
# spec text names this string (same "owner-surfaced, no spec fixes it" class
# as ``config.SERVICE_NAME``/``SINGLETON_MUTEX_NAME``); kept local to this
# module rather than added to ``config.py`` to stay inside this unit's scope.
JOB_OBJECT_NAME = r"Global\CivicCastSupervisorJob"

# The job is a GLOBAL named kernel object, so an unprivileged local process
# could ``OpenJobObject`` it by name and ``TerminateJobObject`` the station's
# whole process tree. The program discipline is "explicit security descriptor,
# never the default DACL" (spec D7 for the pipe; the singleton and
# runtime-owner mutexes carry explicit SDs). So the job is created with the
# SAME explicit SDDL as those mutexes: SYSTEM + BUILTIN\Administrators
# GENERIC_ALL, nobody else -- an unprivileged token can neither open nor
# terminate it. (Coordinator review finding F-JOB-1.)
JOB_OBJECT_SDDL = MUTEX_SDDL  # "D:P(A;;GA;;;SY)(A;;GA;;;BA)"

# Process access rights required for AssignProcessToJobObject/IsProcessInJob,
# spelled out explicitly (house style per the D7 pipe-mask precedent) rather
# than pulled from win32con, so this module's only lazy Windows imports stay
# win32job/win32api/pywintypes:
#   PROCESS_TERMINATE (0x0001) | PROCESS_SET_QUOTA (0x0100)
#   | PROCESS_QUERY_INFORMATION (0x0400)
_PROCESS_ACCESS_FOR_JOB = 0x0001 | 0x0100 | 0x0400

# ERROR_ACCESS_DENIED. Windows returns this from AssignProcessToJobObject when
# the target process is ALREADY a member of the job being assigned to -- which
# is the normal state for a grandchild that inherited membership from a
# contained parent (pg_ctl -> postmaster). See JobObjectController.assign_child.
_ERROR_ALREADY_IN_JOB = 5

# JOB_OBJECT_QUERY (0x0004) | JOB_OBJECT_TERMINATE (0x0008) -- the access
# mask OpenJobObject needs to list members and terminate the found job.
_JOB_OBJECT_QUERY = 0x0004
_JOB_OBJECT_TERMINATE = 0x0008

# winerror.ERROR_FILE_NOT_FOUND -- OpenJobObject's expected, common failure
# when no straggler job survives (kill-on-close already did its work).
_ERROR_FILE_NOT_FOUND = 2


class JobObjectApi(Protocol):
    """The Win32 Job Object operations this module needs, behind an
    injectable seam. ``JobObjectController`` and :func:`sweep_stragglers` are
    pure over this Protocol; only :class:`Win32JobObjectApi` touches real
    Win32. Handles are typed ``object`` -- callers never introspect them,
    only pass them back into this same Protocol's other methods.
    """

    def create_job(self, name: str) -> object:
        """``CreateJobObject(None, name)`` -- default DACL (see module
        docstring: no explicit SD is spec-mandated for the Job Object)."""
        ...

    def configure_kill_on_close_no_breakaway(self, handle: object) -> None:
        """Set ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` and explicitly clear
        both breakaway-permitting flags (D3)."""
        ...

    def assign_process(self, handle: object, pid: int) -> None:
        """``AssignProcessToJobObject`` for ``pid``."""
        ...

    def is_process_in_any_job(self, pid: int) -> bool:
        """``IsProcessInJob(process, None)`` -- is the process a member of ANY
        job, ours or foreign? Distinguishes a membership-collision
        ``ACCESS_DENIED`` (pid already in some other job) from a genuine
        permission failure, which the provenance-gated acceptance in
        :meth:`JobObjectController.assign_child` must never swallow."""
        ...

    def is_process_in_job(self, handle: object, pid: int) -> bool:
        """``IsProcessInJob`` -- used by the win-only test to prove
        assignment actually took."""
        ...

    def close_job(self, handle: object) -> None:
        """Close the job's handle -- the last handle closing (including the
        kernel's own close on process exit/crash) triggers kill-on-close
        (D3's containment guarantee, AC4)."""
        ...

    def open_existing_job(self, name: str) -> object | None:
        """``OpenJobObject(JOB_OBJECT_QUERY | JOB_OBJECT_TERMINATE, False,
        name)`` -- ``None`` on ``ERROR_FILE_NOT_FOUND`` (the common, expected
        case: no straggler job survives). A non-``None`` return means some
        handle somewhere kept a same-named job from a prior run alive."""
        ...

    def list_job_process_ids(self, handle: object) -> list[int]:
        """``QueryInformationJobObject(..., JobObjectBasicProcessIdList)`` --
        the pids currently assigned to ``handle``."""
        ...

    def terminate_job(self, handle: object, exit_code: int) -> None:
        """``TerminateJobObject`` -- kills every process still in the job."""
        ...


class JobObjectController:
    """The supervisor's per-run Job Object owner: create-once, assign every
    direct child (postgres/control_plane -- D2 workers are captured
    automatically as control-plane descendants and are never assigned here
    directly), and own the handle whose :meth:`close` triggers kill-on-close.

    Pure control logic over an injected :class:`JobObjectApi` -- no Win32
    call happens in this class itself.
    """

    def __init__(self, *, api: JobObjectApi, name: str = JOB_OBJECT_NAME) -> None:
        self._api = api
        self._name = name
        self._handle: object | None = None
        self._assigned_pids: list[int] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_created(self) -> bool:
        return self._handle is not None

    @property
    def assigned_pids(self) -> tuple[int, ...]:
        return tuple(self._assigned_pids)

    def ensure_job(self) -> object:
        """Create the job exactly once per controller instance and apply the
        D3 limits. Idempotent: a second call returns the same handle without
        any further Win32 call. Returns the handle."""

        if self._handle is None:
            handle = self._api.create_job(self._name)
            self._api.configure_kill_on_close_no_breakaway(handle)
            self._handle = handle
        return self._handle

    def assign_child(self, pid: int, *, accept_foreign_job_membership: bool = False) -> None:
        """Assign ``pid`` to the job, creating the job first if this is the
        first child of this run. Idempotent per pid -- assigning the same pid
        a second time is a no-op past the first call, so a caller that
        retries after a transient readiness-probe failure never re-issues
        the underlying ``AssignProcessToJobObject`` call for a pid already
        captured.

        A child spawned BY an already-contained child inherits this job's
        membership before we ever see its pid, and Windows answers
        ``AssignProcessToJobObject`` for a process that is already in the
        target job with ``ERROR_ACCESS_DENIED`` (5). That is the postgres
        RESTART path: ``pg_ctl`` runs inside the job and the postmaster it
        spawns is already contained, but its pid is new, so the
        ``_assigned_pids`` guard above misses and the raw call faults.

        Measured 2026-08-08 (Sandbox, candidate 923fad14): killing the
        postmaster put the supervisor into a permanent restart loop --
        PostgreSQL came up cleanly every time ("database system is ready to
        accept connections") and was then torn down because containment
        "faulted"; 13 attempts across 5 minutes, service still reporting
        RUNNING, nothing listening, no recovery. Evidence:
        ``.civiccast-sandbox-preflight/adversarial/evidence/`` --
        ``adv-recovery.json`` plus ``logs-after-kill/supervisor.log``.

        The D3 containment invariant is NOT relaxed here: ACCESS_DENIED is
        accepted only when ``IsProcessInJob`` positively confirms the pid is
        already inside THIS job. Any other error code, and any case where
        membership cannot be confirmed, still raises and still fails closed.
        """

        if pid in self._assigned_pids:
            return
        handle = self.ensure_job()
        try:
            self._api.assign_process(handle, pid)
        except Exception as exc:  # see below; re-raised unless positively handled
            # The catch is deliberately by winerror, NOT by exception type.
            # ``pywintypes.error`` is NOT an ``OSError`` subclass (MRO: error ->
            # Exception -> BaseException), which ``core.py``'s CC-WS5-015 comment
            # already records -- so ``except OSError`` would never fire on the
            # real Win32 path and this whole guard would be inert in production
            # while passing a test whose fake raised ``OSError``.
            #
            # Nothing is swallowed on the strength of the type: the ONLY path
            # that does not re-raise requires winerror == ERROR_ACCESS_DENIED
            # AND a positive ``IsProcessInJob``. Every other exception, and any
            # unconfirmed membership, propagates unchanged so ``core.py``'s
            # existing fail-closed handling still sees the original error.
            winerror = getattr(exc, "winerror", None)
            if winerror is None and exc.args:
                winerror = exc.args[0]
            if winerror != _ERROR_ALREADY_IN_JOB:
                raise
            if not self._api.is_process_in_job(handle, pid):
                # CC-PG-JOB (2026-08-14, TESTER4 forensics): the postmaster is
                # launched by pg_ctl, and pg_ctl on Windows places its children
                # in its OWN restricted-process job. On a FAST postgres start
                # (already-initialized data dir -- a reinstall over preserved
                # ProgramData, or any service restart) that job is still
                # un-collapsible when this assign runs, and Windows answers
                # ACCESS_DENIED for the cross-job assignment. Field-proven on a
                # clean SYSTEM/SCM start (evidence
                # tester4-kit-recovery-0132/10-request-0136-part1-job-membership
                # .json: postmaster in a foreign job, supervisor and pg_ctl in
                # none). Acceptance is deliberately NARROW: only the caller
                # that resolved this pid from OUR data dir's ``postmaster.pid``
                # opts in, and only when the pid is POSITIVELY a member of some
                # job (a genuine permission failure stays fatal). Containment
                # duty then rests on the tracked durable handle + the graceful
                # stop chain (pg_ctl stop tears down postgres's own tree); this
                # is a documented, reviewed relaxation of the D3 assign
                # invariant for exactly this one provenance.
                if not accept_foreign_job_membership:
                    raise
                if not self._api.is_process_in_any_job(pid):
                    raise
                _LOGGER.warning(
                    "accepting foreign-job membership for pid %s (winerror=%s): %s",
                    pid,
                    winerror,
                    self.containment_diagnostics(pid),
                )
        self._assigned_pids.append(pid)

    def containment_diagnostics(self, pid: int) -> str:
        """Best-effort forensics for an ``AssignProcessToJobObject`` fault on
        ``pid``: whether the pid is a member of THIS job right now, and which
        pids the well-known named job currently contains. The field failure
        this serves (postgres reinstall over preserved ProgramData ->
        ACCESS_DENIED loop) has two live hypotheses -- the postmaster inherited
        membership in a FOREIGN job, or our own named job survived with stale
        members -- and this string is what separates them on the next
        reproduction: ``in_this_job=False`` with the pid also absent from the
        named job's members proves the foreign-job case, because same-job
        membership is already positively accepted by ``assign_child``.

        Diagnostics must never make a failure path worse: every query is
        individually guarded, and an unqueryable fact is reported as such
        rather than raised."""

        parts: list[str] = []
        if self._handle is None:
            parts.append("in_this_job=unknown (no live job handle)")
        else:
            try:
                parts.append(f"in_this_job={self._api.is_process_in_job(self._handle, pid)}")
            except Exception as exc:  # forensics never raise
                parts.append(f"in_this_job=unqueryable ({exc!r})")
        try:
            existing = self._api.open_existing_job(self._name)
        except Exception as exc:  # forensics never raise
            parts.append(f"named_job={self._name!r} unqueryable ({exc!r})")
            return "; ".join(parts)
        if existing is None:
            parts.append(f"named_job={self._name!r} not openable (no such job)")
            return "; ".join(parts)
        try:
            parts.append(f"named_job_member_pids={self._api.list_job_process_ids(existing)}")
        except Exception as exc:  # forensics never raise
            parts.append(f"named_job_member_pids=unqueryable ({exc!r})")
        finally:
            # Close the reopened handle, best effort -- forensics never raise.
            with contextlib.suppress(Exception):
                self._api.close_job(existing)
        return "; ".join(parts)

    def close(self) -> None:
        """Close the job handle -- kill-on-close reaps every remaining
        assigned child (D3/AC4). Safe to call when nothing was ever
        created (a no-op), and safe to call twice."""

        if self._handle is not None:
            self._api.close_job(self._handle)
            self._handle = None
            self._assigned_pids = []


class SweepOutcome(BaseModel):
    """The result of :func:`sweep_stragglers`. ``found_existing_job=False``
    is the common, expected outcome (kill-on-close already reaped
    everything); a caller logs this at debug, not warning, level."""

    model_config = ConfigDict(extra="forbid")

    found_existing_job: bool
    terminated_pids: list[int]
    detail: str


def sweep_stragglers(
    api: JobObjectApi, *, name: str = JOB_OBJECT_NAME, exit_code: int = 1
) -> SweepOutcome:
    """D3 defense-in-depth: before spawning ANY child this run, check whether
    a same-named job object from a prior supervisor instance is still alive.
    If :meth:`JobObjectApi.open_existing_job` finds nothing (the expected
    common case -- kill-on-close already did its work), this is a clean
    no-op. If it finds a surviving job, every pid still listed as a member is
    a straggler: list them, ``TerminateJobObject`` (kills the whole
    membership at once, not a per-pid loop), then close the reopened handle.
    """

    handle = api.open_existing_job(name)
    if handle is None:
        return SweepOutcome(
            found_existing_job=False,
            terminated_pids=[],
            detail=f"no existing job named {name!r}; nothing to sweep",
        )

    pids = api.list_job_process_ids(handle)
    api.terminate_job(handle, exit_code)
    api.close_job(handle)
    return SweepOutcome(
        found_existing_job=True,
        terminated_pids=pids,
        detail=f"existing job {name!r} found with {len(pids)} straggler pid(s); terminated and closed",
    )


class Win32JobObjectApi:
    """The real Win32 side of :class:`JobObjectApi` (spec D3):
    ``CreateJobObject`` / ``AssignProcessToJobObject`` / ``OpenJobObject`` /
    ``TerminateJobObject`` via pywin32's ``win32job`` + ``win32api``, imported
    LAZILY inside every method so this module (and ``civiccast.native.*``
    generally) imports cleanly on Linux. See the module docstring for the
    empirical corrections applied (handle lifetime; the process-id-list
    tuple shape)."""

    def __init__(self, *, sddl: str = JOB_OBJECT_SDDL) -> None:
        # F-JOB-1: the SD applied to a created job. Defaults to the restrictive
        # production SDDL (SYSTEM + Administrators only). Injectable ONLY so a
        # ``*_win.py`` test that must reopen the job by name from an UNELEVATED
        # test runner can create it with a permissive test SD -- production
        # never overrides it; the real sweep is SYSTEM->SYSTEM (a prior
        # LocalSystem instance's job reopened by a new LocalSystem instance,
        # both granted by the restrictive SDDL). That the restrictive SDDL
        # DENIES an unprivileged open is exactly the F-JOB-1 hardening, proven
        # by the DACL-readback test.
        self._sddl = sddl

    def create_job(self, name: str) -> object:
        import win32job
        import win32security

        # F-JOB-1: explicit SD, never the default DACL -- see JOB_OBJECT_SDDL.
        # Built exactly like win_probes.RuntimeOwnerMutex (win_probes.py:595-598).
        security_descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            self._sddl, win32security.SDDL_REVISION_1
        )
        security_attributes = win32security.SECURITY_ATTRIBUTES()
        security_attributes.SECURITY_DESCRIPTOR = security_descriptor
        return win32job.CreateJobObject(security_attributes, name)

    def read_dacl_sddl(self, handle: object) -> str:
        """Read back the job's DACL as an SDDL string -- the SD proof the
        ``*_win.py`` test checks for the SYSTEM + Administrators SIDs and the
        absence of an Everyone (``;;;WD``) ACE. ``GENERIC_ALL`` normalizes to
        the object-specific access mask on readback (win_probes empirical
        caution), so the test asserts on SID markers, not the literal "GA"."""

        import win32security

        security_descriptor = win32security.GetSecurityInfo(
            handle, win32security.SE_KERNEL_OBJECT, win32security.DACL_SECURITY_INFORMATION
        )
        return cast(
            str,
            win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
                security_descriptor,
                win32security.SDDL_REVISION_1,
                win32security.DACL_SECURITY_INFORMATION,
            ),
        )

    def configure_kill_on_close_no_breakaway(self, handle: object) -> None:
        import win32job

        info = win32job.QueryInformationJobObject(
            handle, win32job.JobObjectExtendedLimitInformation
        )
        limits = info["BasicLimitInformation"]
        limits["LimitFlags"] = (
            limits["LimitFlags"] | win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ) & ~(
            win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK | win32job.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
        )
        info["BasicLimitInformation"] = limits
        win32job.SetInformationJobObject(handle, win32job.JobObjectExtendedLimitInformation, info)

    def assign_process(self, handle: object, pid: int) -> None:
        import win32api
        import win32job

        process_handle = win32api.OpenProcess(_PROCESS_ACCESS_FOR_JOB, False, pid)
        try:
            win32job.AssignProcessToJobObject(handle, process_handle)
        finally:
            # Handle-lifetime discipline (module docstring): this handle
            # exists only to make the assignment call; the job keeps its own
            # reference to the process, so close ours immediately rather
            # than leaking one kernel handle per assign_child call.
            win32api.CloseHandle(process_handle)

    def is_process_in_job(self, handle: object, pid: int) -> bool:
        import win32api
        import win32job

        process_handle = win32api.OpenProcess(_PROCESS_ACCESS_FOR_JOB, False, pid)
        try:
            return cast(bool, win32job.IsProcessInJob(process_handle, handle))
        finally:
            win32api.CloseHandle(process_handle)

    def is_process_in_any_job(self, pid: int) -> bool:
        # ``IsProcessInJob`` with a null job handle answers "is this process in
        # ANY job?" -- the documented Win32 semantic the provenance-gated
        # acceptance relies on. Handle closed immediately (win_probes lifetime
        # rule), same as is_process_in_job above.
        import win32api
        import win32job

        process_handle = win32api.OpenProcess(_PROCESS_ACCESS_FOR_JOB, False, pid)
        try:
            return cast(bool, win32job.IsProcessInJob(process_handle, None))
        finally:
            win32api.CloseHandle(process_handle)

    def close_job(self, handle: object) -> None:
        import win32api

        win32api.CloseHandle(handle)

    def open_existing_job(self, name: str) -> object | None:
        import pywintypes
        import win32job

        try:
            return cast(
                object,
                win32job.OpenJobObject(_JOB_OBJECT_QUERY | _JOB_OBJECT_TERMINATE, False, name),
            )
        except pywintypes.error as exc:
            if exc.winerror == _ERROR_FILE_NOT_FOUND:
                return None
            raise

    def list_job_process_ids(self, handle: object) -> list[int]:
        import win32job

        pid_list: Any = win32job.QueryInformationJobObject(
            handle, win32job.JobObjectBasicProcessIdList
        )
        return [cast(int, pid) for pid in pid_list]

    def terminate_job(self, handle: object, exit_code: int) -> None:
        import win32job

        win32job.TerminateJobObject(handle, exit_code)


def create_production_controller(*, name: str = JOB_OBJECT_NAME) -> JobObjectController:
    """Wire the real Win32 API into a :class:`JobObjectController`. The only
    place ``Win32JobObjectApi`` is instantiated for production use -- keeps
    ``core.py`` free of any direct Win32 import."""

    return JobObjectController(api=Win32JobObjectApi(), name=name)


__all__ = [
    "JOB_OBJECT_NAME",
    "JobObjectApi",
    "JobObjectController",
    "SweepOutcome",
    "Win32JobObjectApi",
    "create_production_controller",
    "sweep_stragglers",
]
