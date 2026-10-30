# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Run ``pg_ctl`` without the Windows pipe-inheritance hang.

THE DEFECT THIS EXISTS TO PREVENT (live-proven twice, Sandbox runs 14 and
15, 2026-07-31): ``subprocess.run(argv, capture_output=True)`` on
``pg_ctl start`` blocks essentially forever on Windows. ``pg_ctl`` spawns
the ``postgres`` server as a detached child that INHERITS the parent's
stdout/stderr pipe write-handles; ``pg_ctl`` itself exits promptly, but
Python's ``communicate()`` keeps reading the pipes until every write handle
closes -- which is never, while the server lives. A ``timeout=`` does not
save the caller: on ``TimeoutExpired``, CPython kills the (already-exited)
``pg_ctl`` and then drains the pipes AGAIN, blocking on the same inherited
handles. Run 15 burned a 45-minute fresh-install timeout on exactly this;
run 14's ">25-minute idle engine" was the same mechanism one module over.

THE FIX: capture to real temp FILES, never pipes. ``subprocess.run`` then
waits on the *process handle* only; the server inheriting a file handle is
harmless. The blessed argv (built solely by
``civiccast.native.supervisor.children``) is passed through untouched.

Both pg_ctl execution sites use this module:
``civiccast.native.provision.seams`` (the DATABASE_READY step) and
``civiccast.native.upgrade.pg_lifecycle`` (the D3 scoped lifecycle).
"""

from __future__ import annotations

import contextlib
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_TAIL_CHARS = 1500

#: The genuine ``subprocess.run`` object, captured at import. Distinguishes
#: the production path (real execution -> Popen + kill-tree, below) from the
#: repo's established test seam of monkeypatching ``run`` on the one shared
#: ``subprocess`` module object (see ``run_pg_ctl_argv``'s docstring) -- a
#: patched ``subprocess.run`` is a FAKE and must be invoked (with the same
#: file-backed keyword shape) instead of spawning anything real.
_REAL_SUBPROCESS_RUN = subprocess.run

#: Bounded reap after a kill-tree: the tree was just force-killed, so the
#: direct child's process handle should signal almost immediately; this only
#: guards against pathological cases and never blocks the caller for long.
_KILL_REAP_SECONDS = 10.0


@dataclass(frozen=True)
class PgCtlResult:
    """What a caller needs: the exit code and the captured output tail."""

    returncode: int
    output_tail: str


def run_pg_ctl_argv(
    argv: list[str],
    *,
    timeout_seconds: float,
    runner: Callable[..., Any] | None = None,
) -> PgCtlResult:
    """Execute a pg_ctl argv with FILE-backed capture (see module docstring).

    Raises the runner's own ``OSError`` / ``subprocess.TimeoutExpired``
    unchanged -- callers already handle both. ``runner`` is injectable for
    tests; when omitted it resolves ``subprocess.run`` at CALL time (never a
    frozen default argument) so the existing test seams -- monkeypatching
    ``run`` on the one shared ``subprocess`` module object -- keep working.
    """

    if runner is None:
        runner = subprocess.run
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with (
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as out,
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err,
    ):
        # argv is built from pinned config by children.py; runner is
        # injectable, so S603 doesn't fire on this call shape.
        result = runner(
            argv,
            stdout=out,
            stderr=err,
            timeout=timeout_seconds,
            check=False,
            creationflags=creationflags,
        )
        out.seek(0)
        err.seek(0)
        err_text = err.read().strip()
        out_text = out.read().strip()
    # WHY concatenate rather than prefer stderr: on Windows, `pg_ctl start`
    # run WITHOUT `-l` merges the postmaster's own stderr into pg_ctl's
    # STDOUT (there is no `-l` logfile to redirect the server's output to,
    # so it inherits pg_ctl's handles); on failure pg_ctl itself writes only
    # two generic, useless lines to STDERR ("pg_ctl: could not start
    # server" / "Examine the log output."). The old `err_text or out_text`
    # precedence therefore deterministically picked the two useless STDERR
    # lines and discarded the server's real FATAL diagnosis living in
    # out_text -- forensically proven against a live failure. Chronological
    # both-streams concatenation keeps whichever stream actually carries the
    # diagnosis, in every combination.
    tail = "\n".join(part for part in (out_text, err_text) if part)[-_TAIL_CHARS:]
    if not tail:
        # Compat with legacy fake runners that return capture_output-style
        # .stderr/.stdout strings instead of writing the passed files.
        legacy = getattr(result, "stderr", "") or getattr(result, "stdout", "") or ""
        if isinstance(legacy, str):
            tail = legacy.strip()[-_TAIL_CHARS:]
    return PgCtlResult(returncode=result.returncode, output_tail=tail)


# ---------------------------------------------------------------------------
# Generalized file-backed capture for OTHER install-time / probe children
# (initdb, psql, wsl.exe, sc.exe -- C2/C3, 2026-07-31): same pipe-hang
# defect class as pg_ctl (a child that spawns lingering descendants keeps
# capture_output's pipe write-handles open forever; timeout= alone does not
# save the caller), so the same FILE-backed fix, plus a kill-tree on expiry
# because wsl.exe demonstrably leaves helper processes (wslhost.exe) behind.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapturedProcess:
    """Separate raw stdout/stderr byte streams plus the exit code -- callers
    like ``civiccast.native.win_probes`` need the BYTES (UTF-16LE detection)
    and both streams individually, unlike :class:`PgCtlResult`'s single
    text tail."""

    returncode: int
    stdout: bytes
    stderr: bytes


def kill_process_tree(pid: int) -> None:
    """Best-effort forced kill of ``pid`` AND every live descendant.

    Used on capture-timeout expiry so a hung probe/provision child (and the
    helpers it spawned -- e.g. wsl.exe's wslhost.exe) cannot outlive the
    deadline holding inherited handles. Every error that means "already
    gone" is swallowed; this function never raises for a race with normal
    process exit."""

    import psutil

    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    try:
        descendants = root.children(recursive=True)
    except psutil.NoSuchProcess:
        descendants = []
    for proc in [*descendants, root]:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            continue


def _coerce_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")
    return b""


def run_captured_argv(
    argv: list[str],
    *,
    timeout_seconds: float,
    env: dict[str, str] | None = None,
    runner: Callable[..., Any] | None = None,
    popen: Callable[..., Any] | None = None,
    kill_tree: Callable[..., Any] | None = None,
) -> CapturedProcess:
    """Execute ``argv`` with FILE-backed capture, a HARD deadline, and a
    kill-tree on expiry (module docstring's mechanism, generalized).

    Production path (``runner`` omitted / the genuine ``subprocess.run``,
    and no injected ``popen``): the child runs under ``subprocess.Popen``
    with real temp FILES for stdout/stderr, ``wait(timeout=...)`` bounds it
    against the *process handle* only, and on ``TimeoutExpired`` the WHOLE
    process tree is force-killed (:func:`kill_process_tree`) before the
    exception propagates unchanged -- descendants inheriting the file
    handles are harmless (no pipe drain), but they are still reaped so a
    hung ``wsl.exe``/``initdb`` cannot leak workers past the deadline.

    Test seams (either works; both keep execution file-backed):

    * ``runner``: a ``subprocess.run``-shaped callable (the same seam every
      probe already exposes, and the same shared-module monkeypatch
      ``run_pg_ctl_argv`` honors). It is invoked with the file-backed
      keyword shape (``stdout=``/``stderr=`` open files, ``timeout=``,
      ``check=False``); a legacy fake returning ``capture_output``-style
      ``.stdout``/``.stderr`` values (bytes or str) is honored when it
      wrote nothing to the files.
    * ``popen`` / ``kill_tree``: fake process factories for proving the
      timeout + kill-tree path without a real child.

    Raises ``subprocess.TimeoutExpired`` / ``OSError`` (incl.
    ``FileNotFoundError``) unchanged -- callers already classify both.
    """

    resolved_runner = runner if runner is not None else subprocess.run
    kill = kill_tree if kill_tree is not None else kill_process_tree
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        result: object | None = None
        if popen is not None or resolved_runner is _REAL_SUBPROCESS_RUN:
            popen_factory = popen if popen is not None else subprocess.Popen
            # argv comes from pinned config at every call site; factory is
            # injectable, so S603 doesn't fire on this call shape.
            proc = popen_factory(
                argv,
                stdout=out,
                stderr=err,
                stdin=subprocess.DEVNULL,
                env=env,
                creationflags=creationflags,
            )
            try:
                returncode = proc.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                kill(proc.pid)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=_KILL_REAP_SECONDS)
                raise
        else:
            result = resolved_runner(
                argv,
                stdout=out,
                stderr=err,
                env=env,
                timeout=timeout_seconds,
                check=False,
                creationflags=creationflags,
            )
            returncode = result.returncode
        out.seek(0)
        err.seek(0)
        stdout = out.read()
        stderr = err.read()
    if not stdout and not stderr and result is not None:
        # Compat with legacy fake runners that return capture_output-style
        # .stdout/.stderr values instead of writing the passed files.
        stdout = _coerce_bytes(getattr(result, "stdout", b""))
        stderr = _coerce_bytes(getattr(result, "stderr", b""))
    return CapturedProcess(returncode=returncode, stdout=stdout, stderr=stderr)
