# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The D7 control pipe: ``\\\\.\\pipe\\civiccast-supervisor``.

Split per the design (``design.md`` §3, ``pipe_server.py`` package note) into
a PURE half and a real-Win32 half, so the load-bearing logic -- framing,
per-command authorization routing, audit-log wiring, and the single
serialized command queue (AC-N5) -- is exercised by
``tests/native/test_supervisor_pipe_server.py`` on every OS in CI, while only
the actual ``CreateNamedPipe``/``ImpersonateNamedPipeClient`` calls are
Windows-only (``tests/native/test_supervisor_pipe_server_win.py``, real pipe,
no fakes).

**RAT-003 (authoritative for the access mask)** --
``design-addendum-ratification.md`` -- fixes the exact security descriptor:

    D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x120083;;;AU)(D;;FA;;;NU)

SYSTEM + BUILTIN\\Administrators GENERIC_ALL; Authenticated Users EXACTLY
``0x00120083`` = ``FILE_READ_DATA | FILE_WRITE_DATA | FILE_READ_ATTRIBUTES |
READ_CONTROL | SYNCHRONIZE`` -- enough for a duplex status/version
request+reply round trip, and explicitly NOT ``FILE_CREATE_PIPE_INSTANCE``
(0x4): an Authenticated-Users caller can talk to the pipe but can never
create a competing instance (squat) of it. Explicit DENY for NETWORK
(local-only). ``FILE_FLAG_FIRST_PIPE_INSTANCE`` stays set for DETECTION, not
prevention: a name pre-created by a rogue process makes ``CreateNamedPipe``
fail ``ERROR_ACCESS_DENIED`` -- handled here as a `degraded` signal (log the
owning PID), never an uncaught crash (spec D7).

Framing (D7): JSON-lines, ``{"v":1,...}``, 16 KiB cap; malformed or oversized
-> close the connection, no partial/best-effort parse.

Authorization (D7, two-tier, PER COMMAND not per pipe): the server
impersonates the client (``ImpersonateNamedPipeClient``), extracts the
token's ENABLED group SIDs, and calls
:func:`civiccast.native.supervisor.authz.authorize` -- the real public API,
unmodified. ``status``/``version`` need Authenticated Users;
``start``/``stop``/``restart``/``drain``/``runtime_set`` need
``BUILTIN\\Administrators`` or SYSTEM. Every command that was actually
authorized AND is mutating is audit-logged with the caller SID before it
runs.

Serialization (AC-N5): every authorized command -- from every connection --
funnels through one :class:`CommandQueue` worker thread, so two concurrent
conflicting commands (e.g. ``start`` and ``stop`` arriving on different
connections at the same instant) can never interleave into torn state.

Windows-only imports (``pywintypes``, ``win32api``, ``win32file``,
``win32pipe``, ``win32security``) are LAZY, inside the functions that need
them -- this module imports cleanly on Linux.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from civiccast.native.service_identity import SERVICE_SID
from civiccast.native.supervisor.authz import authorize
from civiccast.native.supervisor.config import CONTROL_PIPE_NAME

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RAT-003 authoritative constants
# ---------------------------------------------------------------------------

CONTROL_PIPE_SDDL = (
    f"D:P(D;;FA;;;NU)(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;{SERVICE_SID})(A;;0x120083;;;AU)"
)
"""SYSTEM + BUILTIN\\Administrators GENERIC_ALL; Authenticated Users EXACTLY
the read/write-without-create-instance mask below; explicit DENY NETWORK.
RAT-003 in ``design-addendum-ratification.md`` is authoritative for this
string -- do not "simplify" it to GENERIC_READ/GENERIC_WRITE, which would
silently re-grant ``FILE_CREATE_PIPE_INSTANCE``."""

AUTHENTICATED_USERS_ACCESS_MASK = 0x00120083
"""``FILE_READ_DATA (0x1) | FILE_WRITE_DATA (0x2) | FILE_READ_ATTRIBUTES
(0x80) | READ_CONTROL (0x20000) | SYNCHRONIZE (0x100000)``. Explicitly NOT
``FILE_CREATE_PIPE_INSTANCE (0x4)``, not ``FILE_WRITE_ATTRIBUTES``, not
generic write. A real client (status/version tier) must request exactly
this mask -- requesting ``GENERIC_READ | GENERIC_WRITE`` asks for more than
the AU ACE grants (``GENERIC_WRITE`` alone implies
``FILE_WRITE_ATTRIBUTES``/``FILE_APPEND_DATA``, which are withheld) and
``CreateFile`` is denied; proven for real in
``tests/native/test_supervisor_pipe_server_win.py``."""

FRAME_CAP_BYTES = 16 * 1024
"""D7: 16 KiB frame cap. Matches
``SupervisorConfig.control_pipe_frame_cap_bytes``'s default (config.py) --
kept as an independent constant here rather than instantiating a full
``SupervisorConfig`` just to read one field; both are pinned to the same
spec value."""

_FILE_CREATE_PIPE_INSTANCE = 0x4  # documentation constant only, see the mask note above

_ERROR_ACCESS_DENIED = 5

ACCEPT_SHUTDOWN_TIMEOUT_SECONDS = 5.0
"""Hard upper bound on how long :meth:`PipeServer.close` may take to end an
in-flight accept/serve, measured end to end. Split into the two phases below.
Pinned by
``test_close_ends_a_never_connected_accept_thread_within_the_bound``."""

_ACCEPT_SHUTDOWN_UNBLOCK_SECONDS = 3.0
"""Phase 1 of the bound: how long ``close()`` waits for the accept thread to
leave ``ConnectNamedPipe`` after the self-connect below wakes it."""

_ACCEPT_SHUTDOWN_DISCONNECT_SECONDS = (
    ACCEPT_SHUTDOWN_TIMEOUT_SECONDS - _ACCEPT_SHUTDOWN_UNBLOCK_SECONDS
)
"""Phase 2 of the bound: how long ``close()`` waits after
``DisconnectNamedPipe`` releases a ``serve_connection`` parked in ``ReadFile``."""

# ---------------------------------------------------------------------------
# Pure framing (D7: JSON-lines, {"v":1}, 16 KiB cap, malformed/oversized -> close)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedFrame:
    """The result of :func:`parse_frame`. ``ok=False`` always carries a
    human-readable ``close_reason`` -- the caller's contract is "close the
    connection", never "try to recover a partial frame"."""

    ok: bool
    payload: dict[str, Any] | None
    close_reason: str | None


def parse_frame(raw: bytes, *, cap_bytes: int = FRAME_CAP_BYTES) -> ParsedFrame:
    """Parse one newline-delimited JSON-lines frame (the newline itself is
    NOT included in ``raw``). Total: never raises: every failure mode
    (oversized, invalid UTF-8, invalid JSON, wrong shape, wrong/missing
    envelope version, missing/non-string ``cmd``) returns
    ``ParsedFrame(ok=False, ...)`` so the caller's only job is "if not
    ok: close the connection" (D7)."""

    if len(raw) > cap_bytes:
        return ParsedFrame(
            ok=False, payload=None, close_reason=f"oversized frame ({len(raw)} > {cap_bytes} bytes)"
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return ParsedFrame(
            ok=False, payload=None, close_reason=f"malformed frame: invalid utf-8 ({exc})"
        )

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        return ParsedFrame(
            ok=False, payload=None, close_reason=f"malformed frame: invalid json ({exc})"
        )

    if not isinstance(obj, dict):
        return ParsedFrame(
            ok=False, payload=None, close_reason="malformed frame: top-level value is not an object"
        )

    if obj.get("v") != 1:
        return ParsedFrame(
            ok=False,
            payload=None,
            close_reason=f"malformed frame: unsupported/missing v={obj.get('v')!r}",
        )

    cmd = obj.get("cmd")
    if not isinstance(cmd, str):
        return ParsedFrame(
            ok=False, payload=None, close_reason="malformed frame: missing/non-string cmd"
        )

    return ParsedFrame(ok=True, payload=obj, close_reason=None)


def encode_frame(obj: dict[str, Any]) -> bytes:
    """The write-side counterpart of :func:`parse_frame`: one compact
    JSON-lines frame, newline-terminated."""

    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


ResponseStatus = Literal["ok", "denied", "error"]


def build_response(
    payload: dict[str, Any],
    *,
    status: ResponseStatus,
    detail: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ``{"v":1,...}`` reply envelope for a request ``payload``
    already known to be a valid parsed frame. Echoes ``id`` back when the
    caller supplied one (bounded-retry correlation, matching the D2 worker
    envelope's shape); omits it otherwise rather than inventing one."""

    response: dict[str, Any] = {
        "v": 1,
        "cmd": payload.get("cmd"),
        "result": status,
        "detail": detail,
    }
    if result is not None:
        response["data"] = result
    request_id = payload.get("id")
    if request_id is not None:
        response["id"] = request_id
    return response


# ---------------------------------------------------------------------------
# AC-N5: a single serialized command queue
# ---------------------------------------------------------------------------

CommandHandler = Callable[[str, dict[str, Any]], dict[str, Any]]
"""``(command, payload) -> result``. Runs on the queue's single worker
thread -- never called concurrently with itself, which is exactly the
AC-N5 guarantee this module exists to provide."""


@dataclass
class _QueuedCommand:
    command: str
    payload: dict[str, Any]
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: BaseException | None = None


class CommandQueue:
    """AC-N5: "concurrent conflicting commands are serialized, no torn
    state". Every :meth:`submit` call -- regardless of which connection or
    thread called it -- is executed one at a time, in submission order, by
    a single dedicated worker thread. Callers block on their OWN command's
    completion only; they never see another caller's partial state because
    the handler for command N+1 never starts until command N's handler has
    fully returned (or raised).
    """

    def __init__(self, handler: CommandHandler) -> None:
        self._handler = handler
        self._items: queue.Queue[_QueuedCommand] = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()

    def start(self) -> None:
        """Idempotent: a second ``start()`` on an already-running queue is
        a no-op, not a second worker thread (which would defeat AC-N5)."""

        with self._lifecycle_lock:
            if self._thread is None:
                self._stop_event.clear()
                thread = threading.Thread(
                    target=self._run, name="civiccast-supervisor-pipe-cmdq", daemon=True
                )
                self._thread = thread
                thread.start()

    def stop(self, *, timeout: float | None = 5.0) -> None:
        """Signal the worker to stop after its current item (if any) and
        join it. Safe to call on a never-started or already-stopped queue
        (zero-channels/zero-commands clean no-op)."""

        self._stop_event.set()
        with self._lifecycle_lock:
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=timeout)

    def submit(
        self, command: str, payload: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Enqueue ``command`` and block until the worker thread has run it
        (or the queue times out first). Starts the worker lazily on first
        use so a bare ``CommandQueue(handler)`` is usable without a
        separate ``start()`` call."""

        if self._thread is None:
            self.start()
        item = _QueuedCommand(command=command, payload=payload)
        self._items.put(item)
        if not item.done.wait(timeout=timeout):
            raise TimeoutError(f"command {command!r} did not complete within {timeout}s")
        if item.error is not None:
            raise item.error
        assert item.result is not None  # invariant: done implies result-or-error was set
        return item.result

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._items.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                item.result = self._handler(item.command, item.payload)
            except (
                BaseException
            ) as exc:  # surfaced to the submitter via item.error, never swallowed
                item.error = exc
            finally:
                item.done.set()


# ---------------------------------------------------------------------------
# Dispatcher: framing + per-command authorization + audit log + queue
# ---------------------------------------------------------------------------

AuditLogFn = Callable[[str, str], None]
"""``(caller_sid, command) -> None``."""


def _default_audit_log(caller_sid: str, command: str) -> None:
    logger.warning(
        "D7 control pipe: mutating command %r authorized for caller SID %s", command, caller_sid
    )


@dataclass(frozen=True)
class DispatchOutcome:
    action: Literal["reply", "close"]
    response: dict[str, Any] | None
    close_reason: str | None


class Dispatcher:
    """Ties :func:`parse_frame`, :func:`civiccast.native.supervisor.authz.
    authorize`, the D7 audit-log requirement, and :class:`CommandQueue`
    together. Pure with respect to I/O: the caller supplies the already-
    extracted ``groups``/``caller_sid`` for the connection (real extraction
    is :func:`impersonate_and_extract`, Windows-only, below) and gets back a
    ``DispatchOutcome`` telling it whether to write a reply or close the
    connection -- no pipe handle touched here, which is what makes this
    class fully testable on Linux.
    """

    def __init__(
        self,
        *,
        command_queue: CommandQueue,
        audit_log: AuditLogFn | None = None,
        command_timeout_seconds: float | None = 30.0,
    ) -> None:
        self._queue = command_queue
        self._audit_log = audit_log or _default_audit_log
        self._command_timeout_seconds = command_timeout_seconds

    def handle_frame(
        self,
        raw: bytes,
        *,
        groups: frozenset[str],
        caller_sid: str,
        cap_bytes: int = FRAME_CAP_BYTES,
    ) -> DispatchOutcome:
        frame = parse_frame(raw, cap_bytes=cap_bytes)
        if not frame.ok:
            return DispatchOutcome(action="close", response=None, close_reason=frame.close_reason)

        payload = frame.payload
        assert payload is not None  # invariant of ParsedFrame(ok=True, ...)
        command = cast(str, payload["cmd"])

        decision = authorize(command, groups)
        if not decision.allowed:
            return DispatchOutcome(
                action="reply",
                response=build_response(payload, status="denied", detail=decision.reason),
                close_reason=None,
            )

        if decision.is_mutating:
            self._audit_log(caller_sid, command)

        try:
            result = self._queue.submit(command, payload, timeout=self._command_timeout_seconds)
        except Exception as exc:  # reported to the caller as an error reply, connection stays open
            return DispatchOutcome(
                action="reply",
                response=build_response(payload, status="error", detail=str(exc)),
                close_reason=None,
            )

        return DispatchOutcome(
            action="reply",
            response=build_response(payload, status="ok", detail="applied", result=result),
            close_reason=None,
        )


# ---------------------------------------------------------------------------
# Real Win32 layer (lazy imports; module import must succeed on Linux)
# ---------------------------------------------------------------------------


def _win32() -> tuple[Any, Any, Any, Any]:
    import pywintypes
    import win32file
    import win32pipe
    import win32security

    return pywintypes, win32file, win32pipe, win32security


_KNOWN_GROUP_SIDS: dict[str, str] = {
    "S-1-5-11": "authenticated_users",
    "S-1-5-32-544": "administrators",
    "S-1-5-18": "system",
    "S-1-5-4": "interactive",
}
"""Maps the well-known SIDs the D7 authorization model cares about onto
``authz.KnownGroup``. Any other group the caller's token carries is simply
not represented in the ``frozenset`` passed to ``authorize`` -- fail-closed
by omission, matching ``authz``'s own fail-closed posture."""


@dataclass(frozen=True)
class PipeCreateResult:
    """The outcome of :func:`create_control_pipe`. ``degraded=True`` is the
    D7 fail-closed path for a pre-created (squatted) name -- ``ok`` is
    always ``False`` when ``degraded`` is ``True``; the two are never both
    ``True``, and neither being ``True`` at once with ``ok=False`` means an
    unrelated ``CreateNamedPipe`` failure (also not a crash, just not the
    squat case)."""

    ok: bool
    handle: Any | None
    degraded: bool
    detail: str
    owner_pid: int | None = None


def create_control_pipe(
    *,
    name: str = CONTROL_PIPE_NAME,
    sddl: str = CONTROL_PIPE_SDDL,
    frame_cap_bytes: int = FRAME_CAP_BYTES,
) -> PipeCreateResult:
    """``CreateNamedPipe`` with the RAT-003 explicit SDDL and
    ``FILE_FLAG_FIRST_PIPE_INSTANCE`` (squat detection). Never raises: an
    ``ERROR_ACCESS_DENIED`` (the name was pre-created by another process)
    is caught and reported as ``degraded=True`` with a best-effort
    ``owner_pid`` -- per D7, "log + Event Log entry naming the owning PID,
    enter degraded (children keep running; control unavailable), retry
    with backoff." Retry/backoff and Event Log writing are core.py/
    service.py's job (they own the supervisor's logging/lifecycle); this
    function's contract stops at "tell the caller what happened, never
    crash".
    """

    pywintypes, _win32file, win32pipe, win32security = _win32()
    try:
        security_descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            sddl, win32security.SDDL_REVISION_1
        )
        security_attributes = win32security.SECURITY_ATTRIBUTES()
        security_attributes.SECURITY_DESCRIPTOR = security_descriptor
        handle = win32pipe.CreateNamedPipe(
            name,
            win32pipe.PIPE_ACCESS_DUPLEX | win32pipe.FILE_FLAG_FIRST_PIPE_INSTANCE,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            win32pipe.PIPE_UNLIMITED_INSTANCES,
            frame_cap_bytes,
            frame_cap_bytes,
            0,
            security_attributes,
        )
    except pywintypes.error as exc:
        if exc.winerror == _ERROR_ACCESS_DENIED:
            owner_pid = _find_owning_pid(name)
            logger.error(
                "D7 control pipe %r: ACCESS_DENIED creating it -- name already exists "
                "(possible squat), owner_pid=%s; entering degraded",
                name,
                owner_pid,
            )
            return PipeCreateResult(
                ok=False,
                handle=None,
                degraded=True,
                detail=f"ACCESS_DENIED creating {name!r}: name already exists (possible squat)",
                owner_pid=owner_pid,
            )
        return PipeCreateResult(
            ok=False, handle=None, degraded=False, detail=f"CreateNamedPipe({name!r}) failed: {exc}"
        )

    return PipeCreateResult(
        ok=True, handle=handle, degraded=False, detail=f"control pipe {name!r} created"
    )


def _find_owning_pid(name: str) -> int | None:
    """Best-effort: open the pre-existing pipe as an ordinary client (query
    access only) and ask Windows for its server's PID
    (``GetNamedPipeServerProcessId``). Returns ``None`` on any failure --
    this is diagnostic-only, never load-bearing for the degraded decision
    itself."""

    pywintypes, win32file, win32pipe, _win32security = _win32()
    try:
        handle = win32file.CreateFile(name, 0, 0, None, win32file.OPEN_EXISTING, 0, None)
    except pywintypes.error:
        return None
    try:
        return int(win32pipe.GetNamedPipeServerProcessId(handle))
    except pywintypes.error:
        return None
    finally:
        win32file.CloseHandle(handle)


def read_pipe_dacl_sddl(handle: Any) -> str:
    """Read back the pipe's DACL as an SDDL string -- the SD-readback proof
    ``tests/native/test_supervisor_pipe_server_win.py`` checks the AU ACE
    mask and the absence of an Everyone (``;;;WD``) ACE against. Note (per
    WS4's ``win_probes`` empirical corrections, which apply equally here):
    ``ConvertSecurityDescriptorToStringSecurityDescriptor`` normalizes
    ``GENERIC_ALL`` to the object-specific mask on readback, so the SYSTEM/
    Administrators ACEs come back as ``FA`` (File-All), not the literal
    ``GA`` used to create them -- tests assert on the SID markers, not the
    literal rights string, for those two ACEs."""

    _pywintypes, _win32file, _win32pipe, win32security = _win32()
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


def impersonate_and_extract(handle: Any) -> tuple[frozenset[str], str]:
    """D7: ``ImpersonateNamedPipeClient`` + ``TokenGroups`` extraction ->
    the pure ``KnownGroup`` vocabulary :func:`civiccast.native.supervisor.
    authz.authorize` consumes, plus the caller's SID string for the D7
    audit-log requirement. Only ENABLED groups count -- a UAC-filtered
    admin token carries ``BUILTIN\\Administrators`` as
    ``SE_GROUP_USE_FOR_DENY_ONLY``, which MUST NOT be treated as
    membership (this is the exact split-token case
    ``test_real_token_extraction_yields_a_coherent_caller_identity``
    exercises for real on a UAC-split host). MUST be called only after at
    least one successful
    ``ReadFile`` on this connection -- Win32 requirement:
    ``ImpersonateNamedPipeClient`` before any read fails with winerror
    1368 ("Unable to impersonate using a named pipe until data has been
    read from that pipe."). ``RevertToSelf`` always runs, even on error,
    so impersonation never leaks onto the server thread beyond this call.
    """

    _pywintypes, _win32file, _win32pipe, win32security = _win32()
    import win32api

    win32security.ImpersonateNamedPipeClient(handle)
    try:
        thread_token = win32security.OpenThreadToken(
            win32api.GetCurrentThread(), win32security.TOKEN_QUERY, True
        )
        try:
            user_sid, _attr = win32security.GetTokenInformation(
                thread_token, win32security.TokenUser
            )
            caller_sid = cast(str, win32security.ConvertSidToStringSid(user_sid))
            groups: set[str] = set()
            if caller_sid == SERVICE_SID:
                groups.add("station_service")
            for sid, attributes in win32security.GetTokenInformation(
                thread_token, win32security.TokenGroups
            ):
                if not (attributes & win32security.SE_GROUP_ENABLED):
                    continue
                known = _KNOWN_GROUP_SIDS.get(win32security.ConvertSidToStringSid(sid))
                if known is not None:
                    groups.add(known)
            return frozenset(groups), caller_sid
        finally:
            win32api.CloseHandle(thread_token)
    finally:
        win32security.RevertToSelf()


class PipeServer:
    """Owns the real control-pipe lifecycle: :meth:`create` (with squat
    detection, never raises), :meth:`accept_and_serve_one` (blocks for one
    client connection, serves it fully through a shared ``Dispatcher`` --
    and therefore a shared ``CommandQueue``, which is what makes AC-N5's
    serialization hold across every connection, not just within one), and
    :meth:`close`. Retry-with-backoff on a degraded create, and running the
    accept loop for the supervisor's whole lifetime, are core.py/
    service.py's job -- this class's contract stops at "one call handles
    one connection correctly."

    SHUTDOWN (2026-07-31, gauntlet run 17 wedge): ``ConnectNamedPipe(handle,
    None)`` on a blocking-mode pipe is a SYNCHRONOUS, NON-CANCELLABLE wait, and
    ``CloseHandle`` on the handle it is parked on does NOT release it. That was
    measured on this product's own OS build, not inferred from documentation --
    a probe that closed the handle from a second thread never returned from
    ``CloseHandle`` itself (evidence: the run-17 wedge, where the service sat in
    ``SERVICE_STOP_PENDING`` with checkpoint 0x1 for 112+ seconds because
    ``_ControlPipe.close() -> PipeServer.close() -> CloseHandle`` was parked on
    exactly this handle). So ``close()`` no longer closes a handle an accept
    thread may still be parked on. Instead it: sets the ``_closing`` flag,
    CONNECTS ONE THROWAWAY CLIENT TO ITS OWN PIPE NAME (:meth:`_unblock_accept`)
    -- the documented way to release a synchronous ``ConnectNamedPipe`` -- then
    waits, bounded, for the accept thread to leave the serving section before
    closing the handle. Overlapped I/O with an event pair was considered and
    rejected: ``FILE_FLAG_OVERLAPPED`` on the server handle would also make
    every ``ReadFile``/``WriteFile`` in :func:`serve_connection` overlapped,
    rewriting the load-bearing, real-Win32-proven per-connection path for a
    shutdown-only concern.
    """

    def __init__(
        self,
        dispatcher: Dispatcher,
        *,
        name: str = CONTROL_PIPE_NAME,
        sddl: str = CONTROL_PIPE_SDDL,
        frame_cap_bytes: int = FRAME_CAP_BYTES,
    ) -> None:
        self._dispatcher = dispatcher
        self._name = name
        self._sddl = sddl
        self._frame_cap_bytes = frame_cap_bytes
        self._handle: Any = None
        # Set by close(), read by the accept thread at every point it could
        # otherwise go back to blocking. Never cleared: close() is terminal.
        self._closing = threading.Event()
        # Held for the WHOLE of one accept+serve. close() acquiring it is the
        # proof that the accept thread is genuinely out of ConnectNamedPipe /
        # serve_connection and that CloseHandle is therefore safe.
        self._serving = threading.Lock()
        # Makes close() itself idempotent under concurrent callers.
        self._close_lock = threading.Lock()

    def create(self) -> PipeCreateResult:
        result = create_control_pipe(
            name=self._name, sddl=self._sddl, frame_cap_bytes=self._frame_cap_bytes
        )
        if result.ok:
            self._handle = result.handle
        return result

    def accept_and_serve_one(self) -> None:
        """Block until exactly one client connects, serve that connection
        to completion (possibly many request/reply frames), then return.
        Kept as a single call -- rather than an unbounded ``while True``
        accept loop -- so tests can drive one connection deterministically;
        the supervisor's real accept loop is `while running:
        server.accept_and_serve_one()`.

        Returns immediately (no block, no raise) once :meth:`close` has been
        called and the handle is still present, so a caller's ``while running:
        accept_and_serve_one()`` loop unwinds instead of re-parking on a handle
        that is about to go away. The pre-existing ``RuntimeError`` for "never
        created" (handle is ``None``) is unchanged, and so is the per-connection
        behavior: the dispatch/authorize/reply path is byte-for-byte the same
        :func:`serve_connection` call it always was."""

        pywintypes, _win32file, win32pipe, _win32security = _win32()
        if self._handle is None:
            raise RuntimeError("create() must succeed before accept_and_serve_one()")
        if self._closing.is_set():
            return
        with self._serving:
            handle = self._handle
            if handle is None or self._closing.is_set():
                return
            try:
                win32pipe.ConnectNamedPipe(handle, None)
            except pywintypes.error:
                # A close() racing in is expected teardown, not a fault; any
                # other ConnectNamedPipe failure still propagates exactly as
                # it did before (the accept loop logs and continues).
                if self._closing.is_set():
                    return
                raise
            if self._closing.is_set():
                # What we just accepted is _unblock_accept's throwaway client.
                # Hand the instance back rather than running the real
                # read/impersonate/dispatch path against a dead peer.
                with contextlib.suppress(pywintypes.error):
                    win32pipe.DisconnectNamedPipe(handle)
                return
            serve_connection(handle, self._dispatcher, cap_bytes=self._frame_cap_bytes)

    def _unblock_accept(self) -> None:
        """Open one throwaway client against our OWN pipe name and immediately
        close it, so a thread parked in the synchronous, non-cancellable
        ``ConnectNamedPipe`` returns. Requests exactly
        :data:`AUTHENTICATED_USERS_ACCESS_MASK` -- the narrowest mask the
        RAT-003 descriptor grants every tier, so this works whether the host
        runs the supervisor as SYSTEM or a test runs it as an ordinary user.

        Best effort by design: a failure here (most often ``ERROR_PIPE_BUSY``
        when a REAL client already owns the only instance) is not an error --
        it just means no thread is parked in ``ConnectNamedPipe``, and
        :meth:`close`'s ``DisconnectNamedPipe`` phase handles that case."""

        pywintypes, win32file, _win32pipe, _win32security = _win32()
        try:
            probe = win32file.CreateFile(
                self._name,
                AUTHENTICATED_USERS_ACCESS_MASK,
                0,
                None,
                win32file.OPEN_EXISTING,
                0,
                None,
            )
        except pywintypes.error as exc:
            logger.debug("D7 control pipe: shutdown self-connect did not open (%s)", exc)
            return
        with contextlib.suppress(pywintypes.error):
            win32file.CloseHandle(probe)

    def close(self) -> None:
        """End the accept loop and release the pipe, within
        :data:`ACCEPT_SHUTDOWN_TIMEOUT_SECONDS`. Idempotent: a second call (and
        a call on a server that was never created, or whose ``create()``
        degraded) is a clean no-op. Never raises.

        Deliberately does NOT ``CloseHandle`` while the accept thread may still
        be parked on that handle -- see the class docstring for the measured
        reason. If neither the self-connect nor ``DisconnectNamedPipe`` gets the
        accept thread out within the bound, the handle is ABANDONED (logged at
        ERROR) rather than closed: leaking one handle in a process that is
        already exiting is strictly better than wedging the stop path, which is
        the exact failure this method exists to end."""

        with self._close_lock:
            self._closing.set()
            handle = self._handle
            if handle is None:
                return
            pywintypes, win32file, win32pipe, _win32security = _win32()

            self._unblock_accept()
            acquired = self._serving.acquire(timeout=_ACCEPT_SHUTDOWN_UNBLOCK_SECONDS)
            if not acquired:
                # The accept thread is past ConnectNamedPipe and parked in
                # serve_connection's ReadFile. DisconnectNamedPipe from this
                # thread is the documented way to release that read.
                with contextlib.suppress(pywintypes.error):
                    win32pipe.DisconnectNamedPipe(handle)
                acquired = self._serving.acquire(timeout=_ACCEPT_SHUTDOWN_DISCONNECT_SECONDS)

            self._handle = None
            if not acquired:
                logger.error(
                    "D7 control pipe: accept thread did not release within %.1fs; ABANDONING the "
                    "pipe handle unclosed rather than blocking the stop path in CloseHandle",
                    ACCEPT_SHUTDOWN_TIMEOUT_SECONDS,
                )
                return
            try:
                with contextlib.suppress(Exception):
                    win32file.CloseHandle(handle)
            finally:
                self._serving.release()


def serve_connection(
    handle: Any, dispatcher: Dispatcher, *, cap_bytes: int = FRAME_CAP_BYTES
) -> None:
    """Blocking read/dispatch/reply loop for one already-connected duplex
    pipe handle. Reads newline-delimited JSON frames, impersonates the
    client ONCE per connection (right after the first successful read --
    see :func:`impersonate_and_extract`'s docstring for why it can't
    happen earlier) and reuses the extracted groups/SID for every
    subsequent frame on this connection, and closes on the first
    malformed/oversized frame (D7). Always ``DisconnectNamedPipe``s on
    the way out, even on error, so the pipe instance is reusable for the
    next client.
    """

    pywintypes, win32file, win32pipe, _win32security = _win32()

    buffer = b""
    groups: frozenset[str] | None = None
    caller_sid = ""
    try:
        while True:
            try:
                _, chunk = win32file.ReadFile(handle, 4096)
            except pywintypes.error:
                return  # client disconnected
            if not chunk:
                return

            buffer += chunk
            if len(buffer) > cap_bytes and b"\n" not in buffer:
                logger.warning(
                    "D7 control pipe: oversized frame before newline; closing connection"
                )
                return

            if groups is None:
                groups, caller_sid = impersonate_and_extract(handle)

            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                outcome = dispatcher.handle_frame(
                    line, groups=groups, caller_sid=caller_sid, cap_bytes=cap_bytes
                )
                if outcome.action == "close":
                    logger.warning("D7 control pipe: closing connection (%s)", outcome.close_reason)
                    return
                assert (
                    outcome.response is not None
                )  # invariant of DispatchOutcome(action="reply", ...)
                win32file.WriteFile(handle, encode_frame(outcome.response))
    finally:
        with contextlib.suppress(pywintypes.error):
            win32pipe.DisconnectNamedPipe(handle)


__all__ = [
    "ACCEPT_SHUTDOWN_TIMEOUT_SECONDS",
    "AUTHENTICATED_USERS_ACCESS_MASK",
    "CONTROL_PIPE_SDDL",
    "FRAME_CAP_BYTES",
    "AuditLogFn",
    "CommandHandler",
    "CommandQueue",
    "DispatchOutcome",
    "Dispatcher",
    "ParsedFrame",
    "PipeCreateResult",
    "PipeServer",
    "ResponseStatus",
    "build_response",
    "create_control_pipe",
    "encode_frame",
    "impersonate_and_extract",
    "parse_frame",
    "read_pipe_dacl_sddl",
    "serve_connection",
]
