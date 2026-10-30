# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The real Windows-backed probes -- this module is the seam's real side.

Every registry/process/wsl.exe-touching function here takes injectable roots
or runners so the same code is exercised by fakes in unit tests and by the
real OS in ``tests/native/test_win_probes.py`` (``pytest.mark.skipif(os.name
!= "nt")``). Only ``psutil`` and ``subprocess`` are imported at module level
(both cross-platform); ``winreg``, ``win32event``, ``win32security``,
``win32api``, and ``pywintypes`` are imported LAZILY inside each function
that needs them, per the house rule that ``import civiccast.native.*`` must
succeed on Linux even though these functions are only ever callable on
Windows.

Disclosed empirical corrections (found by exercising the real Win32 APIs on
the dev box, not assumed from documentation -- see
``evidence/dev-box-probes.md`` for the full narrative):

- ``CreateMutex`` on an object that already exists performs a full DACL
  check against the CALLING token, even for the process that originally
  created it (a second handle-open, not the creation itself, is what the
  DACL gates). An unprivileged (non-SYSTEM, non-Administrators-enabled)
  caller gets ``ERROR_ACCESS_DENIED`` (winerror 5) at ``CreateMutex`` time,
  not a ``WAIT_TIMEOUT`` from ``WaitForSingleObject`` -- both are real
  denials and both are classified as ``MutexStatus="denied"`` here (AC4's
  "an unprivileged token attempting to acquire ... is DENIED").
- A named kernel mutex is destroyed the instant its last open handle
  closes -- if the only process holding it crashes, the object is gone, not
  "abandoned," unless some other handle keeps it alive. The dev-box
  real-fire abandoned-mutex evidence therefore uses a three-actor pattern
  (a witness process that just holds a handle open, a holder that
  acquires-then-dies, and the reacquirer) with a test-only permissive SDDL
  -- the *classification logic* (WAIT_ABANDONED -> acquired_abandoned) is
  proven for real; exercising it against the PRODUCTION restrictive SDDL
  needs a privileged/elevated two-process run and is recorded as PENDING.
- ``ConvertSecurityDescriptorToStringSecurityDescriptor`` normalizes
  ``GENERIC_ALL`` ("GA") to the object-specific access mask
  (``0x1f0001`` for a mutex) on readback -- the literal substring "GA" from
  the SDDL used to *create* the object does not reappear when *reading* it
  back. Tests assert on the SID markers (``;;;SY)``, ``;;;BA)``, absence of
  ``;;;WD``) rather than the literal "GA" text.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import psutil
from pydantic import ValidationError

from civiccast.native.models import (
    A1Result,
    A2Result,
    A3Result,
    InterlockRead,
    InterlockStatus,
    MaintenanceRecord,
    ProbeStatus,
    Selector,
    SelectorRead,
)
from civiccast.native.pg_ctl_exec import CapturedProcess, run_captured_argv
from civiccast.native.runtime_guard import (
    A2_TIMEOUT_SECONDS,
    KEEPER_WSL_ARGV_MARKERS,
    MAINTENANCE_KEY,
    MAINTENANCE_VALUE_NAME,
    MUTEX_NAME,
    MUTEX_SDDL,
    RUN_KEY_PATH,
    RUN_VALUE_NAME,
    RUNTIME_HOST_FLAG,
    SELECTOR_KEY,
    SELECTOR_VALUE_NAME,
    WSL_DISTRO_NAME,
)

if TYPE_CHECKING:
    import winreg as _winreg_types
    from collections.abc import Callable, Iterable

    _RegKeyType = _winreg_types.HKEYType | int
else:
    _RegKeyType = int

# F4 fix: a bare "wsl.exe" argv[0] is resolved by CreateProcess's DLL/EXE
# search order, which includes the current working directory ahead of
# System32 on older/unpatched search-order configurations -- a CWD-planted
# "wsl.exe" could be silently executed instead of the real one. Pin to the
# absolute System32 path so no search order is ever consulted. Both
# `detect_wsl_install` and `probe_indistro_services` use ``str(WSL_EXE)`` as
# argv[0]; a missing file at this path (uninstalled/corrupt WSL) surfaces as
# FileNotFoundError, which both functions already map to their existing
# unreadable/unknown-state paths (see F1).
WSL_EXE = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "wsl.exe"

# CC-WS4-001 round-3 (clean-machine cleanroom): the confirmatory
# WSL-SERVICE-presence signal consulted on detect_wsl_install's failure
# paths. Pinned to the absolute System32 path for the same CWD-hijack reason
# WSL_EXE is (F4) -- a bare "sc" argv[0] would be resolved via CreateProcess
# search order. WSL registers a Windows service under one of two names: the
# modern Store-packaged "WslService" (Win11 22H2+) and the legacy inbox
# "LxssManager". Either being registered in the Service Control Manager is
# proof WSL exists on the machine even when a `wsl.exe -l -q` invocation
# itself fails ambiguously. NOTE: the raw registry Services path
# (HKLM\SYSTEM\CurrentControlSet\Services\WslService) empirically reads
# ABSENT even while WslService is Running -- it is a Store-packaged service
# the SCM resolves but the raw key does not expose -- so this signal MUST go
# through the SCM (`sc query`), never winreg.
SC_EXE = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "sc.exe"
WSL_SERVICE_NAMES = ("WslService", "LxssManager")

# `sc query <name>` exits 0 when the service EXISTS (any run state) and 1060
# (ERROR_SERVICE_DOES_NOT_EXIST) when no such service is registered.
_ERROR_SERVICE_DOES_NOT_EXIST = 1060

# CC-WS4-002: the LocalSystem-safe equivalent of the `wsl.exe -l -q` distro
# inventory. wsl.exe itself categorically refuses to run as LocalSystem
# (exit -1, "Running WSL as local system is not supported. Error code:
# Wsl/WSL_E_LOCAL_SYSTEM_NOT_SUPPORTED"), so under the supervisor's real
# identity detect_wsl_install can never get an authoritative exit-0 read and
# always falls onto _confirm_absence_via_service -- which can prove WSL
# itself exists (via the SCM) but not that the CivicCast distro specifically
# is absent, leaving the guard fail-closed forever on a machine where WSL is
# installed but the CivicCast distro is not. WSL records each registered
# distribution under, per LOADED user hive, this Lxss subkey (one child key
# per distro, keyed by an opaque GUID, with a DistributionName REG_SZ value)
# -- readable via plain winreg under any identity including LocalSystem, and
# unable to hang the way a wsl.exe invocation can.
WSL_LXSS_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Lxss"

# ---------------------------------------------------------------------------
# Selector I/O (D1)
# ---------------------------------------------------------------------------


def _hklm() -> int:
    import winreg

    return winreg.HKEY_LOCAL_MACHINE


def _civiccast_key_access(base_access: int) -> int:
    """F5 fix: every access to SOFTWARE\\CivicCast (selector + interlock)
    must OR in ``KEY_WOW64_64KEY``. Without it, a 32-bit (WOW64) process
    reading/writing this key is silently redirected by the registry
    redirector to the ``WOW6432Node`` shadow copy -- splitting
    ActiveRuntime/Maintenance from the 64-bit view every other component
    (the 64-bit installer, the patched keeper) reads, which would let a
    32-bit caller believe it holds/sees a different selector or interlock
    state than reality. The run-entry scan (HKEY_USERS) is exempt -- it is
    not under SOFTWARE\\CivicCast and per-hive Run keys are not split by
    WOW64 redirection the same way. Kept as its own tiny helper (rather than
    inlined at each call site) so it is independently unit-testable without
    weakening any of the real registry calls that use it.
    """

    import winreg

    return base_access | winreg.KEY_WOW64_64KEY


def read_selector(*, root: _RegKeyType | None = None, key_path: str = SELECTOR_KEY) -> SelectorRead:
    """Read HKLM\\SOFTWARE\\CivicCast\\ActiveRuntime (or an injected root/path).

    F6 fix: ``FileNotFoundError`` is the ONLY OSError that means readable
    absence (D1) -- every other OSError (PermissionError, or any other OS
    read failure) is UNREADABLE (ok=False), fail-closed, never silently
    treated as "absent". FileNotFoundError is a subclass of OSError, so it
    is caught first in each block.
    """

    import winreg

    resolved_root = _hklm() if root is None else root
    try:
        with winreg.OpenKey(
            resolved_root, key_path, 0, _civiccast_key_access(winreg.KEY_READ)
        ) as key:
            try:
                raw_value, value_type = winreg.QueryValueEx(key, SELECTOR_VALUE_NAME)
            except FileNotFoundError:
                return SelectorRead(
                    ok=True, value="absent", detail=f"{SELECTOR_VALUE_NAME} value not present"
                )
            except OSError as exc:
                return SelectorRead(
                    ok=False,
                    value=None,
                    detail=f"{SELECTOR_VALUE_NAME} unreadable (winerror={getattr(exc, 'winerror', None)}): {exc}",
                )
    except FileNotFoundError:
        return SelectorRead(ok=True, value="absent", detail=f"key {key_path} not present")
    except OSError as exc:
        return SelectorRead(
            ok=False,
            value=None,
            detail=f"key {key_path} unreadable (winerror={getattr(exc, 'winerror', None)}): {exc}",
        )

    if value_type != winreg.REG_SZ:
        return SelectorRead(
            ok=False,
            value=None,
            detail=f"unexpected registry type {value_type} for {SELECTOR_VALUE_NAME} (expected REG_SZ)",
        )
    if raw_value in ("native", "wsl"):
        return SelectorRead(
            ok=True,
            value=cast(Selector, raw_value),
            detail=f"read {SELECTOR_VALUE_NAME}={raw_value!r}",
        )
    return SelectorRead(
        ok=False, value=None, detail=f"unrecognized {SELECTOR_VALUE_NAME} value {raw_value!r}"
    )


def write_selector(
    value: Literal["native", "wsl"],
    *,
    root: _RegKeyType | None = None,
    key_path: str = SELECTOR_KEY,
) -> None:
    """Write ActiveRuntime -- admin-writable HKLM key; raises PermissionError
    with the OS's own message when the caller lacks write access."""

    import winreg

    resolved_root = _hklm() if root is None else root
    with winreg.CreateKeyEx(
        resolved_root, key_path, 0, _civiccast_key_access(winreg.KEY_SET_VALUE)
    ) as key:
        winreg.SetValueEx(key, SELECTOR_VALUE_NAME, 0, winreg.REG_SZ, value)


# ---------------------------------------------------------------------------
# Maintenance/freeze interlock I/O (D7a)
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def read_interlock(
    *, root: _RegKeyType | None = None, key_path: str = MAINTENANCE_KEY
) -> InterlockRead:
    """F6 fix: same FileNotFoundError-vs-other-OSError split as
    read_selector -- only a genuinely missing key/value is readable-free;
    any other OSError is UNREADABLE, fail-closed (D4: "a transmitter that
    can't check permission doesn't transmit")."""

    import winreg

    resolved_root = _hklm() if root is None else root
    try:
        with winreg.OpenKey(
            resolved_root, key_path, 0, _civiccast_key_access(winreg.KEY_READ)
        ) as key:
            try:
                raw_value, value_type = winreg.QueryValueEx(key, MAINTENANCE_VALUE_NAME)
            except FileNotFoundError:
                return InterlockRead(
                    status="free", record=None, detail=f"{MAINTENANCE_VALUE_NAME} value not present"
                )
            except OSError as exc:
                return InterlockRead(
                    status="unreadable",
                    record=None,
                    detail=f"{MAINTENANCE_VALUE_NAME} unreadable (winerror={getattr(exc, 'winerror', None)}): {exc}",
                )
    except FileNotFoundError:
        return InterlockRead(status="free", record=None, detail=f"key {key_path} not present")
    except OSError as exc:
        return InterlockRead(
            status="unreadable",
            record=None,
            detail=f"key {key_path} unreadable (winerror={getattr(exc, 'winerror', None)}): {exc}",
        )

    if value_type != winreg.REG_SZ:
        return InterlockRead(
            status="unreadable",
            record=None,
            detail=f"unexpected registry type {value_type} for {MAINTENANCE_VALUE_NAME} (expected REG_SZ)",
        )
    try:
        record = MaintenanceRecord.model_validate_json(raw_value)
    except (ValidationError, ValueError) as exc:
        return InterlockRead(
            status="unreadable", record=None, detail=f"malformed Maintenance JSON: {exc}"
        )

    status: InterlockStatus = "held" if record.state == "held" else "free"
    return InterlockRead(
        status=status,
        record=record,
        detail=f"Maintenance record generation={record.generation} state={record.state}",
    )


def take_interlock(
    owner_run_id: str,
    *,
    root: _RegKeyType | None = None,
    key_path: str = MAINTENANCE_KEY,
    clock: Callable[[], str] | None = None,
) -> MaintenanceRecord:
    """Acquire the D7a interlock. Raises RuntimeError if already held or
    unreadable (fail-closed: an interlock we can't prove is free is not
    safe to take)."""

    import winreg

    resolved_root = _hklm() if root is None else root
    current = read_interlock(root=resolved_root, key_path=key_path)
    if current.status in ("held", "unreadable"):
        raise RuntimeError(f"cannot take interlock: {_held_interlock_detail(current)}")

    next_generation = (current.record.generation + 1) if current.record is not None else 1
    now = (clock or _utc_now_iso)()
    record = MaintenanceRecord(
        v=1,
        state="held",
        generation=next_generation,
        owner_run_id=owner_run_id,
        taken_utc=now,
        released_utc=None,
        # <installer-path-audit BL-05> See MaintenanceRecord.owner_pid.
        owner_pid=os.getpid(),
    )
    with winreg.CreateKeyEx(
        resolved_root, key_path, 0, _civiccast_key_access(winreg.KEY_SET_VALUE)
    ) as key:
        winreg.SetValueEx(key, MAINTENANCE_VALUE_NAME, 0, winreg.REG_SZ, record.model_dump_json())
    return record


def _pid_is_alive(pid: int) -> bool | None:
    """True/False if the PID's liveness is knowable, None if it is not.

    Used ONLY to make a held interlock's error message honest (BL-05); never
    to decide that an interlock may be taken. ``psutil`` is already a runtime
    dependency of this module.
    """

    try:
        return psutil.pid_exists(pid)
    except Exception:
        return None


def _held_interlock_detail(read: InterlockRead) -> str:
    """The refusal text for an interlock that cannot be taken.

    <installer-path-audit BL-05> "cannot take interlock: held" told an
    operator nothing about WHO holds it or whether that holder still exists.
    A killed engine leaves ``state="held"`` forever, and every subsequent
    upgrade then dies at step 1 -> exit 10 -> NSIS 124 (which, post-#143,
    aborts the install): a permanent, unexplained wedge. This names the
    holder, when it took the interlock, and -- when the record carries a PID
    -- whether that process is still alive, so a genuinely stale interlock is
    an EXPLICIT, loud finding rather than something the next run silently
    steals or dies on.
    """

    record = read.record
    if record is None or record.state != "held":
        return read.detail
    parts = [
        f"held by run {record.owner_run_id!r}",
        f"taken {record.taken_utc}",
        f"generation {record.generation}",
    ]
    if record.owner_pid is None:
        parts.append(
            "no owning pid was recorded (an older build took it), so its liveness cannot be checked"
        )
    else:
        alive = _pid_is_alive(record.owner_pid)
        if alive is True:
            parts.append(f"owning process pid={record.owner_pid} is STILL RUNNING")
        elif alive is False:
            parts.append(
                f"owning process pid={record.owner_pid} is GONE -- this interlock is STALE. "
                "It is not taken automatically, because doing so could tear the interlock out "
                "from under a live upgrade. Clear "
                f"HKLM\\{MAINTENANCE_KEY}\\{MAINTENANCE_VALUE_NAME} deliberately, after "
                "confirming no upgrade is in progress, then re-run setup"
            )
        else:
            parts.append(f"owning process pid={record.owner_pid} liveness could not be checked")
    return "; ".join(parts)


def release_interlock(
    *,
    owner_run_id: str | None = None,
    root: _RegKeyType | None = None,
    key_path: str = MAINTENANCE_KEY,
    clock: Callable[[], str] | None = None,
) -> MaintenanceRecord:
    """Release the D7a interlock (idempotent if already released). Raises
    RuntimeError if nothing is there to release or the record is unreadable.

    <installer-path-audit BL-05> ``owner_run_id``, when supplied, must MATCH
    the held record's owner. Without that check this function verified only
    that a record existed and was ``held``, so the D3 orchestrator's
    unconditional release on the rollback path could release ANOTHER run's
    interlock: run A is inside ``migrate()``, run B (a second setup.exe, a
    retry, an operator double-click) fails to take the interlock, funnels into
    ``_rollback``, and releases A's. The supervisor then re-permits writers
    against a schema mid-migration, B reports ROLLED_BACK/exit 10 and A
    reports COMPLETE/exit 0 -- both journals internally consistent, both wrong
    about the machine.

    Left optional so a deliberate operator-side release (a recovery tool, the
    documented manual step in UPGRADE-RECOVERY.md) can still clear an
    interlock whose owner is gone.
    """

    import winreg

    resolved_root = _hklm() if root is None else root
    current = read_interlock(root=resolved_root, key_path=key_path)
    if current.record is None:
        raise RuntimeError(f"cannot release interlock: {current.detail}")
    if current.record.state == "released":
        return current.record
    if owner_run_id is not None and current.record.owner_run_id != owner_run_id:
        raise RuntimeError(
            "refusing to release the D7a maintenance interlock: it is held by run "
            f"{current.record.owner_run_id!r} (taken {current.record.taken_utc}), not by this "
            f"run {owner_run_id!r}. Releasing another run's interlock would re-permit writers "
            "against a database that run may be mid-migration on."
        )

    now = (clock or _utc_now_iso)()
    record = current.record.model_copy(update={"state": "released", "released_utc": now})
    with winreg.CreateKeyEx(
        resolved_root, key_path, 0, _civiccast_key_access(winreg.KEY_SET_VALUE)
    ) as key:
        winreg.SetValueEx(key, MAINTENANCE_VALUE_NAME, 0, winreg.REG_SZ, record.model_dump_json())
    return record


# ---------------------------------------------------------------------------
# A1: keeper-activity probe
# ---------------------------------------------------------------------------


def _process_matches_wsl_keeper(name: str, cmdline: list[str]) -> bool:
    basename = name.lower()
    if basename not in ("wsl.exe", "wsl"):
        return False
    return all(marker in cmdline for marker in KEEPER_WSL_ARGV_MARKERS)


def _scan_live_processes(process_iter: Callable[[], Iterable[Any]]) -> tuple[ProbeStatus, str]:
    try:
        processes = process_iter()
    except Exception as exc:
        return "error", f"process scan failed to start: {exc}"

    try:
        for proc in processes:
            try:
                name = proc.name()
                cmdline = proc.cmdline()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            if any(RUNTIME_HOST_FLAG in arg for arg in cmdline):
                pid = getattr(proc, "pid", "?")
                return "positive", f"process pid={pid} cmdline carries {RUNTIME_HOST_FLAG}"
            if _process_matches_wsl_keeper(name, cmdline):
                pid = getattr(proc, "pid", "?")
                return (
                    "positive",
                    f"wsl.exe keeper process pid={pid} matches all keeper argv markers",
                )
    except Exception as exc:
        return "error", f"process scan failed mid-iteration: {exc}"

    return "negative", "no live keeper process found"


_ERROR_NO_MORE_ITEMS = 259


def scan_run_entries(*, users_root: _RegKeyType | None = None) -> tuple[ProbeStatus, str]:
    """Enumerate loaded-hive Run entries for the keeper autostart marker.

    ``users_root`` defaults to HKEY_USERS; tests inject HKEY_CURRENT_USER
    (or any root) with a fake subkey standing in for one "loaded hive" --
    winreg treats every HKEY_* root uniformly for EnumKey/OpenKey, so the
    same enumeration code exercises real and fake roots identically.

    F3 fix: ``winreg.EnumKey`` signals BOTH "no more subkeys" (winerror=259,
    ERROR_NO_MORE_ITEMS) and every other enumeration failure (key deleted
    mid-scan, access denied, ...) via the same ``OSError`` type. Only
    winerror=259 is the real end-of-list sentinel; any other OSError is a
    genuine scan-level failure and must surface as "error", never be
    silently conflated with a clean negative (mirrors
    ``_scan_live_processes``'s two-level error/end structure).
    """

    import winreg

    resolved_root = winreg.HKEY_USERS if users_root is None else users_root
    matches: list[str] = []
    try:
        with winreg.OpenKey(resolved_root, "") as root_key:
            index = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(root_key, index)
                except OSError as exc:
                    winerror = getattr(exc, "winerror", None)
                    if winerror == _ERROR_NO_MORE_ITEMS:
                        break
                    return (
                        "error",
                        f"HKEY_USERS scan failed enumerating subkey at index {index} "
                        f"(winerror={winerror}): {exc}",
                    )
                index += 1
                if subkey_name.endswith("_Classes"):
                    continue
                run_path = f"{subkey_name}\\{RUN_KEY_PATH}"
                try:
                    with winreg.OpenKey(resolved_root, run_path) as run_key:
                        value, value_type = winreg.QueryValueEx(run_key, RUN_VALUE_NAME)
                except OSError:
                    continue
                if value_type == winreg.REG_SZ and RUNTIME_HOST_FLAG in value:
                    matches.append(subkey_name)
    except OSError as exc:
        return "error", f"HKEY_USERS scan failed: {exc}"

    if matches:
        return (
            "positive",
            f"Run entry '{RUN_VALUE_NAME}' carrying {RUNTIME_HOST_FLAG} found under hive(s): {', '.join(matches)}",
        )
    return "negative", "no loaded-hive Run entry carries the runtime-host flag"


def probe_keeper(
    *,
    process_iter: Callable[[], Iterable[Any]] = psutil.process_iter,
    run_entry_scanner: Callable[[], tuple[ProbeStatus, str]] | None = None,
) -> A1Result:
    """A1: live keeper process (Windows-side wsl.exe argv or the runtime-host
    flag) or keeper Run entry in any LOADED hive."""

    live_status, live_detail = _scan_live_processes(process_iter)
    scanner = run_entry_scanner or scan_run_entries
    run_status, run_detail = scanner()
    return A1Result(
        live_process=live_status,
        run_entry=run_status,
        detail=f"live_process: {live_detail}; run_entry: {run_detail}",
    )


# ---------------------------------------------------------------------------
# A2: in-distro CivicCast service activity probe
# ---------------------------------------------------------------------------


def _decode_wsl_output(data: bytes) -> str:
    """wsl.exe's OWN diagnostic messages (e.g. "distro not found") come back
    UTF-16LE; a successfully piped-through Linux command's stdout is
    ordinary UTF-8. Heuristically detect UTF-16LE (null bytes in the first
    chunk) and decode accordingly -- tolerant of either, per the brief."""

    if not data:
        return ""
    if b"\x00" in data[:64]:
        try:
            return data.decode("utf-16-le", errors="replace")
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


def _run_probe_argv(
    argv: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]],
    timeout: float = A2_TIMEOUT_SECONDS,
) -> CapturedProcess:
    """C3 fix (2026-07-31): the ONE execution path for every guard-probe
    child (``wsl.exe``, ``sc.exe``) -- file-backed capture with a hard
    deadline and a kill-tree on expiry, via
    :func:`civiccast.native.pg_ctl_exec.run_captured_argv`.

    Why: these probes previously used ``subprocess.run(capture_output=True,
    timeout=5)``. ``wsl.exe`` demonstrably spawns helper processes
    (``wslhost.exe``) that inherit the pipe write-handles, and on Windows a
    lingering descendant keeps ``communicate()`` draining those pipes past
    ANY ``timeout=`` (the live-proven pg_ctl mechanism, Sandbox runs 14/15
    -- see pg_ctl_exec's module docstring). On the SERVICE path these
    probes run on the single supervision thread inside
    ``guard.pre_child_start``/``evaluate_once``, so one hang blocks the
    supervisor's run loop AND its graceful-stop chain until the SCM stop
    itself times out. File-backed capture waits on the process handle only;
    the expiry kill-tree reaps the helpers too.

    This changes HOW output is captured, never WHAT is decided: the
    returned :class:`CapturedProcess` carries the same
    returncode/stdout-bytes/stderr-bytes surface the probes already
    classify, and ``TimeoutExpired``/``OSError`` propagate unchanged into
    each probe's existing (audited) failure classification. An injected
    ``runner`` (every existing test fake) is invoked by ``run_captured_argv``
    with the same file-backed keyword shape, its ``CompletedProcess``-style
    ``.stdout``/``.stderr`` honored via the legacy-compat read."""

    return run_captured_argv(argv, timeout_seconds=timeout, runner=runner)


def _default_wsl_service_present(
    *, runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run
) -> bool | None:
    """SCM-backed confirmatory signal for ``detect_wsl_install``'s failure
    paths (CC-WS4-001 round-3, clean-machine cleanroom).

    Queries the Service Control Manager -- NOT winreg (the raw
    ``HKLM\\SYSTEM\\CurrentControlSet\\Services\\WslService`` path reads
    absent even when WslService is Running, because it is a Store-packaged
    service the SCM resolves but the raw key does not expose) -- for a
    registered WSL service. ``sc query <name>`` exits 0 when the service
    EXISTS in any state and 1060 (ERROR_SERVICE_DOES_NOT_EXIST) when it does
    not. Both candidate names (modern ``WslService``, legacy
    ``LxssManager``) are checked.

    Returns:
      * ``True`` the instant ANY WSL service name resolves (exit 0) -- WSL
        exists on this machine.
      * ``False`` only when EVERY candidate name is a definite
        does-not-exist (1060) -- no WSL service is registered at all.
      * ``None`` on any other exit code, timeout, or OSError -- an SCM query
        we could not conclusively read is ambiguous and stays fail-closed.

    The subprocess ``runner`` is injectable (mirroring ``detect_wsl_install``)
    so this classifier is exercised deterministically by fakes; the real
    ``subprocess.run`` default is only invoked on Windows.
    """

    for name in WSL_SERVICE_NAMES:
        try:
            completed = _run_probe_argv([str(SC_EXE), "query", name], runner=runner)
        except (subprocess.TimeoutExpired, OSError):
            return None
        if completed.returncode == 0:
            return True
        if completed.returncode != _ERROR_SERVICE_DOES_NOT_EXIST:
            return None
    return False


def scan_registered_distros(*, users_root: _RegKeyType | None = None) -> bool | None:
    """CC-WS4-002: is the CivicCast WSL distro (``WSL_DISTRO_NAME``)
    registered under ANY loaded hive's Lxss subtree
    (``WSL_LXSS_KEY_PATH``)? The LocalSystem-safe, registry-backed
    equivalent of the question ``wsl.exe -l -q`` answers -- see
    ``WSL_LXSS_KEY_PATH``'s module comment for why this exists (wsl.exe
    refuses to run as LocalSystem at all).

    Modeled closely on ``scan_run_entries``: ``users_root`` defaults to
    ``winreg.HKEY_USERS``; tests inject any root standing in for one or more
    "loaded hives". Uses the same ``_ERROR_NO_MORE_ITEMS`` (259) sentinel to
    distinguish clean end-of-enumeration from a genuine scan failure, and
    skips subkey names ending in ``"_Classes"`` for the same reason
    ``scan_run_entries`` does (those are the per-hive Classes shadow, never a
    real loaded-profile hive).

    Returns a TRI-STATE:
      * ``True`` -- ``WSL_DISTRO_NAME`` was found as the ``DistributionName``
        value of some ``{guid}`` subkey under some hive's Lxss key.
      * ``False`` -- every loaded hive enumerated cleanly (top-level
        HKEY_USERS scan, each hive's Lxss key open/enumerate, each
        ``{guid}`` subkey's DistributionName read) and none contained that
        distro name.
      * ``None`` -- the scan could not be trusted: the top-level HKEY_USERS
        enumeration itself failed, or opening/enumerating some hive's Lxss
        subtree (the Lxss key itself, a ``{guid}`` subkey, or its
        DistributionName value) failed with anything other than a clean
        not-found.

    Per this file's established convention (F6/F3), ``FileNotFoundError`` is
    the ONLY OSError that means readable absence -- a missing Lxss key under
    a hive (that hive simply has no WSL distros) or a ``{guid}`` subkey
    lacking a DistributionName value are both clean, expected absences and
    do not disqualify the scan. Every OTHER OSError (permission denied, a
    key vanishing mid-scan, ...) is untrusted and immediately downgrades the
    whole scan to ``None`` -- a scan that could not read everything it tried
    to read must never be reported as a confident ``False``.

    Where there is no registry at all (any non-Windows host -- CI's
    platform-independent lane runs this module on Linux), the answer is
    ``None`` for exactly the reason above: the scan could not be performed,
    so it cannot be reported as a confident ``False``. Raising
    ``ModuleNotFoundError`` out of a tri-state probe whose entire contract is
    "never fail loudly, downgrade to UNKNOWN" would be the one failure mode
    it is written to prevent -- and it did, breaking
    ``test_phase5_verify_rejects_truncated_evidence_on_resume`` on Linux via
    ``detect_wsl_install``.
    """

    try:
        import winreg
    except ModuleNotFoundError:
        return None

    resolved_root = winreg.HKEY_USERS if users_root is None else users_root
    try:
        with winreg.OpenKey(resolved_root, "") as root_key:
            index = 0
            while True:
                try:
                    hive_name = winreg.EnumKey(root_key, index)
                except OSError as exc:
                    winerror = getattr(exc, "winerror", None)
                    if winerror == _ERROR_NO_MORE_ITEMS:
                        break
                    return None
                index += 1
                if hive_name.endswith("_Classes"):
                    continue

                lxss_path = f"{hive_name}\\{WSL_LXSS_KEY_PATH}"
                try:
                    with winreg.OpenKey(resolved_root, lxss_path) as lxss_key:
                        guid_index = 0
                        while True:
                            try:
                                guid_name = winreg.EnumKey(lxss_key, guid_index)
                            except OSError as exc:
                                winerror = getattr(exc, "winerror", None)
                                if winerror == _ERROR_NO_MORE_ITEMS:
                                    break
                                return None
                            guid_index += 1
                            guid_path = f"{lxss_path}\\{guid_name}"
                            try:
                                with winreg.OpenKey(resolved_root, guid_path) as guid_key:
                                    try:
                                        distro_name, value_type = winreg.QueryValueEx(
                                            guid_key, "DistributionName"
                                        )
                                    except FileNotFoundError:
                                        continue
                            except FileNotFoundError:
                                continue
                            if value_type == winreg.REG_SZ and distro_name == WSL_DISTRO_NAME:
                                return True
                except FileNotFoundError:
                    continue
    except OSError:
        return None

    return False


def _confirm_absence_via_service(
    wsl_service_present: Callable[[], bool | None] | None,
    *,
    distro_scanner: Callable[[], bool | None] | None = None,
) -> bool | None:
    """CC-WS4-001 round-3 discriminator, consulted on every
    ``detect_wsl_install`` path where ``wsl.exe -l -q`` could not produce a
    trustworthy inventory (nonzero exit, undecodable output, invocation
    error/timeout).

    Only a DEFINITE "no WSL service registered" (the confirmatory signal
    returning ``False``) downgrades the unknown into a genuine ``False``
    ("WSL is not installed on this machine -- no CivicCast distro can be
    registered because there is no WSL at all"). A service that IS present,
    or a service check that is itself ambiguous (``None``), both preserve the
    CC-WS4-001 fail-closed ``None`` -- a failed ``wsl.exe`` invocation is
    never trusted as "no WSL".

    The default classifier is resolved by name at call time (not bound as a
    default argument) so tests can monkeypatch the module attribute for the
    transitive ``probe_indistro_services`` path.

    CC-WS4-002: the SCM can only prove *WSL itself* exists or does not -- on
    a machine where WSL IS installed (SCM says present, or the SCM check is
    itself ambiguous) it has nothing to say about whether the CivicCast
    distro specifically is registered, which used to leave every such
    machine stuck at fail-closed ``None`` forever (confirmed live: under the
    interactive user ``detect_wsl_install`` correctly read ``False``; under
    LocalSystem -- where ``wsl.exe`` categorically refuses to run -- it read
    ``None``, and the guard never started the control plane). Fixed: on
    every path that does NOT already resolve to a definite ``False`` from
    the SCM signal, consult ``scan_registered_distros`` (the
    LocalSystem-safe registry inventory) before giving up. If the distro
    scan itself definitely proves the CivicCast distro is not registered
    (``False``), that is dispositive -- no CivicCast WSL runtime can be
    transmitting -- and this now returns ``False`` where it previously
    returned ``None``. If the distro scan returns ``True`` (distro IS
    registered; the failure is elsewhere) or ``None`` (the scan itself could
    not be trusted), the CC-WS4-001 fail-closed ``None`` is preserved exactly
    as before. The distro scanner is likewise resolved by name at call time
    (mirroring ``wsl_service_present``) so tests can drive it
    deterministically; the injectable ``distro_scanner`` keyword is additive
    and every existing positional call site (``_confirm_absence_via_service(
    wsl_service_present)``) is unaffected.
    """

    probe = wsl_service_present if wsl_service_present is not None else _default_wsl_service_present
    if probe() is False:
        return False

    scan = distro_scanner if distro_scanner is not None else scan_registered_distros
    return False if scan() is False else None


def detect_wsl_install(
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    wsl_service_present: Callable[[], bool | None] | None = None,
) -> bool | None:
    """Is the CivicCast WSL distro registered at all (``wsl.exe -l -q``)?
    Used both as A2's own "no distro registered" classification input and
    as ``GuardInputs.wsl_install_detected``.

    F1 fix: this used to fail-open -- a timeout or OSError silently became a
    definite ``False``, which meant "the probe couldn't run" was
    indistinguishable from "we confirmed no WSL install exists", and that
    unknown state authorized decide()'s D1 "absent + no WSL install =>
    continue as native" row. Returns ``None`` (UNKNOWN) on
    TimeoutExpired/OSError/any other undecodable failure -- a ``False``
    result is now ONLY ever returned when ``wsl.exe -l -q`` actually ran and
    the CivicCast distro name is confirmed absent from its output.

    CC-WS4-001 fix (round 2, Critical): the F1 fix above only caught the
    *exception* path -- it still ignored ``CompletedProcess.returncode``
    entirely. A runner that RAN (no exception) but exited nonzero (e.g.
    ``wsl.exe -l -q`` returning "Access is denied") produced an
    empty/garbage stdout; ``WSL_DISTRO_NAME in output`` on that empty
    string is trivially ``False`` -- silently promoting a FAILED invocation
    into a CONFIRMED "no install" (the auditor's literal executed repro:
    A2's service query times out AND the confirmatory ``-l -q`` exits 1,
    which used to authorize ``decide()``'s selector-absent native-start
    row). Fixed: ``False`` is returned ONLY when returncode == 0 AND the
    decoded output contains no U+FFFD replacement character -- a clean
    decode under either UTF-16LE or UTF-8 is the positive "this was a
    trustworthy inventory" signal a garbled/binary byte stream fails under
    ``errors="replace"``. EVERY nonzero exit (regardless of what its stdout
    happens to contain -- even a distro name appearing in a failed
    invocation's echoed-back output is not trusted), timeout, invocation
    error, or undecodable output is an UNKNOWN inventory.

    CC-WS4-001 fix (round 3, Critical -- clean-machine cleanroom): on a
    genuinely WSL-less Windows box, ``wsl.exe`` is an OS inbox stub that
    exits NONZERO ("The Windows Subsystem for Linux is not installed"). The
    round-2 fix (correctly) refused to trust that nonzero exit as "no WSL",
    so it returned ``None`` -- but that fail-closed the dual-runtime guard
    and the native runtime never started on the very clean box it targets.
    Fixed WITHOUT reopening the fail-open: on every unknown-inventory path
    (nonzero exit, undecodable output, invocation error/timeout) the result
    is now routed through ``_confirm_absence_via_service`` -- a robust,
    non-localized WSL-SERVICE-presence signal read from the Service Control
    Manager (``sc query WslService`` / ``LxssManager``; see
    ``_default_wsl_service_present``). Only a DEFINITE "no WSL service
    registered" downgrades the unknown into a genuine ``False`` (WSL truly
    absent, native may start). A WSL service that IS present, or a service
    check that is itself ambiguous, both preserve the CC-WS4-001 ``None``.
    The exit-0 + clean-decode path is UNCHANGED and authoritative
    (``return WSL_DISTRO_NAME in output``) -- it never consults the service
    seam.

    CC-WS4-002: the supervisor runs as LocalSystem, and current WSL versions
    categorically refuse that identity -- ``wsl.exe -l -q`` exits -1 with
    "Running WSL as local system is not supported." -- so under LocalSystem
    this function NEVER takes the exit-0 authoritative path; it always falls
    through to ``_confirm_absence_via_service``. On a machine where WSL IS
    installed (but the CivicCast distro is not), the SCM signal alone could
    only prove WSL exists, never that the CivicCast distro specifically is
    absent, so this stayed a fail-closed ``None`` forever and the control
    plane never started (confirmed live: same call returns ``False`` under
    the interactive user, ``None`` under LocalSystem). Fixed by having
    ``_confirm_absence_via_service`` additionally consult
    ``scan_registered_distros`` -- a registry-backed, LocalSystem-safe distro
    inventory -- before giving up; see that function's docstring for the
    full discriminator logic. This function's own decision tree is
    otherwise unchanged: it still only ever calls
    ``_confirm_absence_via_service`` on the same three non-authoritative
    paths (exception, nonzero exit, undecodable output) it already did.
    """

    try:
        completed = _run_probe_argv([str(WSL_EXE), "-l", "-q"], runner=runner)
    except (subprocess.TimeoutExpired, OSError):
        return _confirm_absence_via_service(wsl_service_present)
    if completed.returncode != 0:
        return _confirm_absence_via_service(wsl_service_present)
    output = _decode_wsl_output(completed.stdout).replace("\x00", "")
    if "�" in output:
        return _confirm_absence_via_service(wsl_service_present)
    return WSL_DISTRO_NAME in output


def _classify_absence_via_detect(
    *, runner: Callable[..., subprocess.CompletedProcess[bytes]], because: str
) -> A2Result:
    """F1: the shared fail-closed classifier for every path in
    ``probe_indistro_services`` where the systemctl call itself could not
    produce a trustworthy answer (it errored, timed out, or reported the
    distro missing). Consults the tri-state ``detect_wsl_install`` and only
    ever returns "negative" when that confirmatory read is a DEFINITE
    ``False`` -- ``True`` (registered, but the query still failed --
    ambiguous) and ``None`` (install state itself unknown) both stay
    "unreadable". This is the fix for the original fail-open bug: a bare
    timeout could never again be silently promoted into "no distro
    registered".
    """

    installed = detect_wsl_install(runner=runner)
    if installed is False:
        return A2Result(status="negative", detail="no CivicCast distro registered")
    if installed is True:
        return A2Result(
            status="unreadable",
            detail=f"distro {WSL_DISTRO_NAME} is registered but {because}",
        )
    return A2Result(
        status="unreadable",
        detail=f"WSL install state unknown (confirmatory detect_wsl_install could not determine it) while {because}",
    )


def probe_indistro_services(
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    timeout: float = A2_TIMEOUT_SECONDS,
) -> A2Result:
    """A2: `wsl -d <distro> --user root --exec systemctl is-active
    'civiccast*'`, 5s bound. FAIL-CLOSED on anything but a clearly readable
    active/inactive response.

    F1 fix: every failure path below (the systemctl call itself
    erroring/timing out, or its output reporting the distro not found)
    routes through ``_classify_absence_via_detect`` rather than assuming
    "unreadable" or "negative" outright -- "negative" is now reserved for
    the case where a confirmatory ``detect_wsl_install`` call DEFINITELY
    proves no CivicCast distro is registered.
    """

    try:
        completed = _run_probe_argv(
            [
                str(WSL_EXE),
                "-d",
                WSL_DISTRO_NAME,
                "--user",
                "root",
                "--exec",
                "systemctl",
                "is-active",
                "civiccast*",
            ],
            runner=runner,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _classify_absence_via_detect(
            runner=runner, because=f"wsl.exe timed out after {timeout}s"
        )
    except FileNotFoundError:
        return _classify_absence_via_detect(runner=runner, because="wsl.exe was not found on PATH")
    except OSError as exc:
        return _classify_absence_via_detect(
            runner=runner, because=f"wsl.exe invocation failed: {exc}"
        )

    stdout = _decode_wsl_output(completed.stdout)
    stderr = _decode_wsl_output(completed.stderr)
    combined = f"{stdout}{stderr}"

    if (
        "WSL_E_DISTRO_NOT_FOUND" in combined
        or "no distribution with the supplied name" in combined.lower()
    ):
        return _classify_absence_via_detect(
            runner=runner, because=f"wsl.exe reports distro not found: {combined.strip()}"
        )

    lines = [line.strip().lower() for line in stdout.splitlines() if line.strip()]
    if any(line == "active" for line in lines):
        return A2Result(status="positive", detail=f"civiccast* service active: {stdout.strip()}")
    if lines and all(line in ("inactive", "failed", "unknown") for line in lines):
        return A2Result(status="negative", detail=f"civiccast* service inactive: {stdout.strip()}")
    if completed.returncode == 0 and not lines:
        return A2Result(
            status="negative", detail="civiccast* service query returned no active units"
        )

    # CC-CLEANROOM-001: on a PRISTINE box wsl.exe is the App-Execution-Alias
    # stub -- every invocation (this systemctl call included) exits nonzero
    # with a localized "WSL is not installed" diagnostic that matches none of
    # the shapes above. A bare unreadable here fails the guard closed
    # (blocked_probe_unavailable) and the product cannot boot on its own
    # target machine. Route the fallthrough through the confirmatory
    # tri-state instead: locale-proof (keys on the SCM-backed
    # detect_wsl_install, never on message text), and it can only flip to
    # "negative" on a DEFINITE no-WSL read -- every ambiguous state stays
    # fail-closed unreadable, preserving F1.
    return _classify_absence_via_detect(
        runner=runner,
        because=(
            f"the systemctl query produced unrecognized wsl.exe output "
            f"(exit={completed.returncode}): stdout={stdout.strip()!r} stderr={stderr.strip()!r}"
        ),
    )


# ---------------------------------------------------------------------------
# A3: Global\CivicCastRuntimeOwner mutex (D4)
# ---------------------------------------------------------------------------

_ERROR_ACCESS_DENIED = 5


class RuntimeOwnerMutex:
    """The Windows named mutex fast-path between the native supervisor and
    the patched keeper. Injectable name/sddl for tests (production defaults
    are the module constants)."""

    def __init__(self, *, name: str = MUTEX_NAME, sddl: str = MUTEX_SDDL) -> None:
        self._name = name
        self._sddl = sddl
        self._handle: Any = None
        self._owns = False

    def acquire(self, timeout_ms: int = 0) -> A3Result:
        import pywintypes
        import win32event
        import win32security

        try:
            security_descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
                self._sddl, win32security.SDDL_REVISION_1
            )
            security_attributes = win32security.SECURITY_ATTRIBUTES()
            security_attributes.SECURITY_DESCRIPTOR = security_descriptor
            handle = win32event.CreateMutex(security_attributes, False, self._name)
        except pywintypes.error as exc:
            if exc.winerror == _ERROR_ACCESS_DENIED:
                return A3Result(
                    status="denied",
                    detail=f"mutex '{self._name}': access denied opening it (unprivileged token; DACL enforced)",
                )
            return A3Result(status="error", detail=f"CreateMutex failed: {exc}")

        self._handle = handle
        try:
            wait_result = win32event.WaitForSingleObject(handle, timeout_ms)
        except pywintypes.error as exc:
            self._close_handle()
            return A3Result(status="error", detail=f"WaitForSingleObject failed: {exc}")

        if wait_result == win32event.WAIT_OBJECT_0:
            self._owns = True
            return A3Result(status="acquired", detail=f"mutex '{self._name}' acquired")
        if wait_result == win32event.WAIT_ABANDONED:
            self._owns = True
            return A3Result(
                status="acquired_abandoned",
                detail=f"mutex '{self._name}' acquired; previous owner terminated without releasing",
            )
        # Non-owning outcomes must not park an open handle on self._handle:
        # a probe caller that never release()s, or __enter__'s raise path,
        # would leak one kernel handle per attempt (round-2 re-verify panel,
        # Major). Without ownership there is no reason to keep the handle.
        if wait_result == win32event.WAIT_TIMEOUT:
            self._close_handle()
            return A3Result(status="denied", detail=f"mutex '{self._name}' held by another process")
        self._close_handle()
        return A3Result(
            status="error", detail=f"WaitForSingleObject returned unexpected code {wait_result}"
        )

    def probe(self, timeout_ms: int = 0) -> A3Result:
        """CC-WS4-004 fix (round 2, Major -- auditor panel): the lifetime-
        safe ownership check GuardMonitor's real A3 callable must be wired
        to, NOT ``.acquire`` directly. The round-1 bug: every 30s
        re-evaluation called ``self._mutex()`` (bound to ``.acquire``),
        which always CreateMutex-opens and WaitForSingleObject-waits again,
        even when this same instance already owns the object -- an
        unelevated holder cannot reopen the production-DACL object it
        already owns (see the module docstring's first empirical
        correction: a second open performs a full DACL check against the
        calling token), so evaluation 2 spuriously observed a denial and
        the monitor controlled-stopped itself within one interval of
        starting.

        Fixed: if this instance currently owns the mutex (``self._owns``
        AND a live ``self._handle``), report stable self-ownership WITHOUT
        reopening or re-waiting the kernel object at all. Otherwise (the
        first-ever probe, OR a defensive case where ``self._owns`` was
        somehow left ``True`` with no live handle -- should not happen,
        ``release()`` always clears both together, but a genuine loss of
        ownership there is a genuine refusal, not something to paper over
        by trusting ``_owns`` alone) attempt exactly ONE real acquire. The
        exclusivity assertion against the other runtime therefore still
        happens exactly once, on the first probe -- not on every
        subsequent evaluation.
        """

        if self._owns and self._handle is not None:
            return A3Result(
                status="acquired",
                detail=f"mutex '{self._name}' self-owned (held since first probe; not reopened)",
            )
        return self.acquire(timeout_ms)

    def _close_handle(self) -> None:
        import win32api

        if self._handle is not None:
            win32api.CloseHandle(self._handle)
            self._handle = None

    def release(self) -> None:
        import win32api
        import win32event

        if self._owns and self._handle is not None:
            win32event.ReleaseMutex(self._handle)
            self._owns = False
        if self._handle is not None:
            win32api.CloseHandle(self._handle)
            self._handle = None

    def read_dacl_sddl(self) -> str:
        """Read back the mutex's DACL as an SDDL string -- the SD proof
        tests/native/test_win_probes.py checks for the SYSTEM + Administrators
        SIDs and the absence of an Everyone ACE."""

        import win32security

        if self._handle is None:
            raise RuntimeError("mutex not acquired; call acquire() first")
        security_descriptor = win32security.GetSecurityInfo(
            self._handle, win32security.SE_KERNEL_OBJECT, win32security.DACL_SECURITY_INFORMATION
        )
        return cast(
            str,
            win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
                security_descriptor,
                win32security.SDDL_REVISION_1,
                win32security.DACL_SECURITY_INFORMATION,
            ),
        )

    def __enter__(self) -> RuntimeOwnerMutex:
        """F7 fix: a context manager that silently "succeeds" while not
        actually owning the mutex is a lie an ownership check downstream
        would trust. Raise loudly, naming the A3 status, when acquire()
        comes back "denied" (the other side owns it) or "error" (ownership
        could not be arbitrated at all) -- both are non-ownership outcomes.
        "acquired" and "acquired_abandoned" both pass through unchanged:
        the caller of an abandoned acquire still owes decide()'s mandatory
        A2 re-verify (D4) -- this context manager only guards against the
        two outright non-ownership outcomes, it does not itself perform
        that re-verify.
        """

        result = self.acquire()
        if result.status in ("denied", "error"):
            raise RuntimeError(
                f"RuntimeOwnerMutex '{self._name}' not acquired ({result.status}): {result.detail}"
            )
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


__all__ = [
    "SC_EXE",
    "WSL_EXE",
    "WSL_LXSS_KEY_PATH",
    "WSL_SERVICE_NAMES",
    "RuntimeOwnerMutex",
    "detect_wsl_install",
    "probe_indistro_services",
    "probe_keeper",
    "read_interlock",
    "read_selector",
    "release_interlock",
    "scan_registered_distros",
    "scan_run_entries",
    "take_interlock",
    "write_selector",
]
