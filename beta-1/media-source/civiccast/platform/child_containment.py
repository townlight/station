# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Control-plane-owned containment for the control plane's long-lived children.

THE DEFECT (OBSERVED, station, 2026-09-25, ``supervisor.log`` 07:20:32 -
07:32:15). The supervisor killed an unready control plane; the three HLS relay
ffmpegs that control plane had spawned SURVIVED as orphans holding the relays'
UDP ports. Every supervised relay after that failed at once with ``[udp @ ...]
bind failed: Error number -10048`` and retried on a 60 s backoff, so all three
channels stayed off air until the orphans were killed by hand (spawn=9, all
channels ok at 07:32:15 -- eleven minutes after the install).

WHY INHERITED MEMBERSHIP IN THE SUPERVISOR'S JOB IS NOT ENOUGH.
``civiccast.native.supervisor.job_object`` gives the supervisor a named job
with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` and no breakaway, and children
inherit it -- that is spec D3's containment, and it is not what failed.
``KILL_ON_JOB_CLOSE`` fires when the LAST HANDLE to the job closes, not when a
member process dies. The supervisor keeps its job handle open for its own
lifetime, so terminating one member (the control plane) reaps nothing: the
member's own children -- which inherited membership from it -- are left running
in a job whose handle is still held elsewhere. Measured on this box with real
processes (topology ``harness --job A--> middle --> grandchild``, 2026-09-25):

    middle relies on inherited membership in A only:
        TerminateProcess(middle) -> grandchild ALIVE at +2s; the grandchild
        only dies when job A's handle is closed.
    middle creates its own nested kill-on-close job B and self-assigns:
        TerminateProcess(middle) -> grandchild DEAD within 2s.

THE MECHANISM (what this module does). The control plane, at startup, creates
its own ANONYMOUS job with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` and assigns
ITSELF to it. Nested jobs are supported on Windows 8+, so this composes with
the supervisor's job rather than fighting it: the control plane's job becomes
the innermost job of the tree, every process the control plane spawns inherits
it (no breakaway is requested anywhere in an ordinary spawn path), and because
the control plane holds the ONLY handle to that job, its death -- graceful
exit, crash, or ``TerminateProcess`` -- closes the last handle and the kernel
reaps the whole subtree. That is the same kernel behaviour the earlier
experiment measured, applied one level down.

WHY SELF-ASSIGNMENT RATHER THAN ASSIGNING EACH CHILD AT ITS SPAWN SITE. One
call covers every long-lived child the control plane will ever have, including
the ones it does not spawn directly: HLS relay ffmpegs
(``civiccast.stream._ffmpeg.start_ffmpeg``), the per-channel GStreamer playout
workers (``civiccast.egress.gst.strategy``), the TS/NDI/SDI relays
(``civiccast.egress.ts_relay`` / ``ndi_relay`` / ``sdi_relay``), contribution
coprocesses (``civiccast.live.contribution.coprocess``) and their own
grandchildren. Per-spawn-site assignment would have to be repeated at each of
those funnels and would silently miss the next one added.

WHY THE JOB IS ANONYMOUS (never named). Two control-plane processes can
overlap during a restart. A named job would let the second process's children
join the first process's job (and, worse, let either hold a handle that keeps
the other's kill-on-close from ever firing). An anonymous job is owned by
exactly one process and cannot be opened by anyone else.

WHY ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` IS DELIBERATELY LEFT SET -- measured,
not assumed. ``civiccast.native.gstreamer_repair._detached_launch`` spawns the
installer's GStreamer re-stage with ``CREATE_BREAKAWAY_FROM_JOB`` FROM THE
CONTROL PLANE, because that repair stops the service and deletes the runtime
tree the control plane is running out of; it must outlive its parent. A
kill-on-close job that does not permit breakaway makes that ``CreateProcess``
fail outright, which would trade an orphan bug for a broken recovery tier.
Measured 2026-09-25 (three modes, real processes):

    control plane in the supervisor's job only (today's product):
        breakaway spawn ok
    control plane in its own job B WITHOUT breakaway allowed:
        PermissionError winerror=5 -- the repair launch fails
    control plane in its own job B WITH breakaway allowed:
        breakaway spawn ok (same as today)

Breakaway is not a hole in the containment: a child is only spared the job if
its ``CreateProcess`` explicitly asks for it, and the one call site that does
is documented to outlive the control plane. Ordinary spawns inherit the job.

FAILURE POSTURE. This never raises. A station whose containment could not be
established must still start and air -- that is the lesser evil -- but it is
not silent: the returned :class:`ContainmentStatus` carries the reason and the
caller logs it at ERROR, because the consequence is exactly the orphaned-relay
state this module exists to prevent.

This module is Windows-only by nature and imports pywin32 lazily inside
:class:`Win32ContainmentApi`'s methods, so importing it is harmless on any OS.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Protocol, cast

_LOG = logging.getLogger(__name__)

# Process access rights for AssignProcessToJobObject / IsProcessInJob, spelled
# out explicitly (house style per the D7 pipe-mask precedent) rather than
# pulled from win32con: PROCESS_TERMINATE (0x0001) | PROCESS_SET_QUOTA (0x0100)
# | PROCESS_QUERY_INFORMATION (0x0400). PROCESS_SET_QUOTA is the one that
# matters here -- AssignProcessToJobObject fails with ERROR_ACCESS_DENIED
# (winerror 5) without it, which at the call site is indistinguishable from the
# ERROR_ALREADY_IN_JOB = 5 that means something else entirely.
_PROCESS_ACCESS_FOR_JOB = 0x0001 | 0x0100 | 0x0400


@dataclass(frozen=True)
class ContainmentStatus:
    """Whether this process now owns a kill-on-close job for its children.

    ``active`` is not a claim about intent -- it is set only after
    ``IsProcessInJob`` positively confirms the assignment took effect. A
    ``False`` status is a real degradation and ``detail`` says why; it is
    logged at ERROR by the caller, never swallowed.
    """

    active: bool
    detail: str


class ContainmentApi(Protocol):
    """The Win32 seam. Every method is exercised by a fake in the pure tests
    and by real pywin32 calls in the ``*_win.py`` tests."""

    def is_windows(self) -> bool: ...

    def create_kill_on_job_close_job(self) -> object: ...

    def assign_current_process(self, job: object) -> None: ...

    def current_process_in_job(self, job: object) -> bool: ...

    def close_handle(self, handle: object) -> None: ...


class Win32ContainmentApi:
    """The real Win32 side: ``CreateJobObject`` / ``SetInformationJobObject`` /
    ``AssignProcessToJobObject`` / ``IsProcessInJob`` via pywin32's ``win32job``
    + ``win32api``, imported LAZILY inside every method so this module imports
    cleanly on Linux."""

    def is_windows(self) -> bool:
        return os.name == "nt"

    def create_kill_on_job_close_job(self) -> object:
        import win32job

        # Anonymous, never named: see the module docstring -- a named job would
        # be shareable across overlapping control-plane processes. The name is
        # the EMPTY STRING, not None: pywin32's CreateJobObject requires a
        # string there and rejects None outright ("TypeError: None is not a
        # valid string in this context", measured 2026-09-25 with pywin32 311;
        # an empty name is the object manager's unnamed-object case, producing
        # a fresh job on every call rather than opening an existing one).
        handle = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(
            handle, win32job.JobObjectExtendedLimitInformation
        )
        limits = info["BasicLimitInformation"]
        # KILL_ON_JOB_CLOSE is the whole point. BREAKAWAY_OK is set on purpose
        # (measured -- see the module docstring): it keeps the tier-5 detached
        # GStreamer repair launchable from this process. SILENT_BREAKAWAY_OK is
        # left clear (it is 0 on a fresh job), so children are captured unless
        # they explicitly ask to leave.
        limits["LimitFlags"] = (
            limits["LimitFlags"]
            | win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | win32job.JOB_OBJECT_LIMIT_BREAKAWAY_OK
        )
        info["BasicLimitInformation"] = limits
        win32job.SetInformationJobObject(handle, win32job.JobObjectExtendedLimitInformation, info)
        return handle

    def assign_current_process(self, job: object) -> None:
        import win32api
        import win32job

        process_handle = win32api.OpenProcess(_PROCESS_ACCESS_FOR_JOB, False, os.getpid())
        try:
            win32job.AssignProcessToJobObject(job, process_handle)
        finally:
            # Handle-lifetime discipline (same as job_object.py): this handle
            # exists only to make the call; the job keeps its own reference.
            win32api.CloseHandle(process_handle)

    def current_process_in_job(self, job: object) -> bool:
        import win32api
        import win32job

        process_handle = win32api.OpenProcess(_PROCESS_ACCESS_FOR_JOB, False, os.getpid())
        try:
            return cast(bool, win32job.IsProcessInJob(process_handle, job))
        finally:
            win32api.CloseHandle(process_handle)

    def close_handle(self, handle: object) -> None:
        import win32api

        win32api.CloseHandle(handle)


#: The status of this process's own containment, latched on first success or
#: first failure so a second call in the same process is a no-op (and never
#: creates a second job for the same process).
_STATUS: ContainmentStatus | None = None

#: Job handles must stay OPEN for the lifetime of this process: closing the
#: last handle to a KILL_ON_JOB_CLOSE job containing this process would kill
#: this process. Appending to a module-level list is what keeps the pywin32
#: handle object from being garbage-collected (and thus closed) -- dropping
#: the reference to a job handle is exactly the bug this module prevents.
_RETAINED_HANDLES: list[object] = []

_LOCK = threading.Lock()


def _default_api() -> ContainmentApi:
    return Win32ContainmentApi()


def contain_own_descendants(*, api: ContainmentApi | None = None) -> ContainmentStatus:
    """Put this process in its own anonymous kill-on-close Job Object.

    Call once per process, at startup, before any long-lived child is spawned
    (``civiccast.app._maybe_contain_own_descendants`` does exactly that, under
    the ``CIVICCAST_SUPERVISED`` guard, in the same place and for the same
    reason as the control plane's log configuration).

    Idempotent: the result is latched, so only the first call does work.
    Passing ``api`` bypasses the latch and always runs the sequence -- that is
    the pure tests' seam, and it is not used in production.

    Never raises. Returns a :class:`ContainmentStatus`; ``active=False`` means
    this process's children are NOT guaranteed to die with it, and the caller
    is expected to have logged the ``detail`` at ERROR.
    """

    if api is not None:
        return _contain(api)
    global _STATUS
    with _LOCK:
        if _STATUS is None:
            _STATUS = _contain(_default_api())
        return _STATUS


def _contain(api: ContainmentApi) -> ContainmentStatus:
    if not api.is_windows():
        return ContainmentStatus(
            active=False,
            detail="not Windows: this containment is a Windows Job Object mechanism",
        )
    try:
        job = api.create_kill_on_job_close_job()
    # Broad on purpose: ``pywintypes.error`` is NOT an ``OSError`` subclass, so
    # a narrow catch would let the real Win32 fault escape and take the control
    # plane's startup down with it.
    except Exception as exc:
        return ContainmentStatus(
            active=False,
            detail=f"CreateJobObject/SetInformationJobObject failed: {exc!r}",
        )
    try:
        api.assign_current_process(job)
        if api.current_process_in_job(job):
            # Keep the handle referenced -> keep the job alive -> keep
            # kill-on-close armed. Never closed by design.
            _RETAINED_HANDLES.append(job)
            return ContainmentStatus(
                active=True,
                detail=(
                    "control plane owns an anonymous kill-on-close job; every child "
                    "it spawns is reaped with it"
                ),
            )
    # Same broad catch, same reason as above -- and the handle is closed before
    # returning, so a failed containment never leaves a job alive.
    except Exception as exc:
        api.close_handle(job)
        return ContainmentStatus(active=False, detail=f"AssignProcessToJobObject failed: {exc!r}")
    # Assignment reported no error but membership could not be confirmed: do
    # not claim a guarantee that IsProcessInJob just contradicted.
    api.close_handle(job)
    return ContainmentStatus(
        active=False,
        detail="AssignProcessToJobObject succeeded but IsProcessInJob reports the "
        "control plane is not in the new job",
    )


def containment_status() -> ContainmentStatus | None:
    """The latched status, or ``None`` if :func:`contain_own_descendants` has
    not been called in this process. Diagnostics only."""

    return _STATUS
