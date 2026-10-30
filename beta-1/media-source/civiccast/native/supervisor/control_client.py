# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
r"""CC-WS5-007 part 4: the identity-verifying control client for the D7 pipe.

The pipe SERVER already verifies the CLIENT: it impersonates the caller
(``ImpersonateNamedPipeClient``) and gates each command through ``authz``. This
module is the REVERSE, anti-squat direction the server cannot provide for
itself: before it sends an admin command, the CLIENT verifies the SERVER's pipe
owner is SYSTEM or ``BUILTIN\\Administrators``.

Why it matters: the D7 pipe SDDL keeps ``FILE_CREATE_PIPE_INSTANCE`` off the
Authenticated-Users ACE, so an unprivileged process cannot create a SECOND
instance of an existing pipe -- but if the real supervisor is not yet up, an
unprivileged process CAN create the name FIRST (a squat). A client that blindly
connects to ``\\.\pipe\civiccast-supervisor`` could then hand a squatter a
``stop``/``runtime_set``, or be phished by a fake ``status``. The fix: read the
server pipe object's OWNER SID (``GetSecurityInfo`` OWNER info) and REFUSE to
transact unless it is a trusted system owner. The real supervisor's pipe,
created by the LocalSystem service, is owned by SYSTEM; a squatter's pipe is
owned by the squatting user.

The owner read and the wire transport are injected seams, so the
accept-trusted / refuse-squatted decision runs on Linux in CI with fakes
(``tests/native/test_supervisor_control_client.py``). The REAL named-pipe round
trip + a real server owner SID is VM-bound (a real running server to connect to
is itself the venue dependency; disclosed in ``evidence/PENDING.md``). Windows
imports (``pywintypes``/``win32file``/``win32security``) are LAZY -- this module
imports on Linux.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from civiccast.native.service_identity import SERVICE_SID
from civiccast.native.supervisor.config import CONTROL_PIPE_NAME
from civiccast.native.supervisor.pipe_server import (
    AUTHENTICATED_USERS_ACCESS_MASK,
    FRAME_CAP_BYTES,
    encode_frame,
    parse_frame,
)

logger = logging.getLogger(__name__)

# The pipe object OWNER SIDs a real supervisor server can have. The production
# supervisor runs as LocalSystem (ADR-0021) so its pipe is SYSTEM-owned; an
# elevated interactive admin run would be Administrators-owned. Anything else is
# a squat and is refused. (Mirrors ``authz._ADMIN_GROUPS`` -- SYSTEM +
# Administrators -- but this is the OWNER of the server object, not the caller's
# token, so it lives here rather than in the pure caller-authz module.)
_TRUSTED_OWNER_SIDS: frozenset[str] = frozenset({"S-1-5-18", "S-1-5-32-544", SERVICE_SID})

# The single live handle is opened with the RAT-003 Authenticated-Users mask
# (``AUTHENTICATED_USERS_ACCESS_MASK``), which INCLUDES READ_CONTROL (0x20000):
# an ordinary client can therefore both read the server pipe's OWNER security
# information AND read/write the request on that SAME open, with no elevated
# right -- the basis for the TOCTOU-safe verify-then-send on one handle.


class IdentityVerdict(BaseModel):
    """The result of :meth:`ControlClient.verify_server_identity`: whether the
    server pipe's owner is trusted, the owner SID that was read (``None`` if it
    could not be read), and a human-readable reason."""

    model_config = ConfigDict(extra="forbid")

    trusted: bool
    owner_sid: str | None
    detail: str


class ControlServerUntrustedError(RuntimeError):
    """Raised when a command is attempted against a control pipe whose server
    owner is NOT a trusted system owner (a squat, or an unreadable owner). The
    command is NEVER written to the wire -- the squatter receives nothing."""

    def __init__(self, verdict: IdentityVerdict) -> None:
        self.verdict = verdict
        super().__init__(verdict.detail)


def is_trusted_owner(owner_sid: str | None) -> bool:
    """Whether ``owner_sid`` is a trusted control-pipe server owner (SYSTEM or
    BUILTIN\\Administrators). Fail-closed: an unreadable/absent owner
    (``None``) is NOT trusted."""

    return owner_sid in _TRUSTED_OWNER_SIDS


OwnerSidReader = Callable[[], str | None]
"""``() -> owner SID string | None``. Reads the server pipe object's OWNER SID;
``None`` when the pipe cannot be opened / the owner cannot be read (fail-closed
-> untrusted)."""

ControlTransport = Callable[[dict[str, Any]], dict[str, Any]]
"""``(request envelope) -> response envelope``. Writes one framed request to the
pipe and returns the parsed reply. The real transport is Windows-only; the unit
tests inject a fake."""


class ControlConnection(Protocol):
    """CC-WS5-007-TOCTOU: ONE live control-pipe connection. The owner SID is read
    from, and the request is written to, the SAME handle -- so no squatter can
    recreate the pipe between a verify-open and a send-open. ``_Win32ControlConnection``
    satisfies this in production; the unit tests inject a fake that fails if the
    verify and the send do not run on the same open."""

    def owner_sid(self) -> str | None: ...
    def send(self, request: dict[str, Any]) -> dict[str, Any]: ...
    def close(self) -> None: ...


ConnectionFactory = Callable[[], ControlConnection]
"""``() -> ControlConnection``. Opens the server pipe ONCE and returns a live
connection whose owner is verified and (if trusted) written on the same handle.
The real factory is Windows-only; the unit tests inject a fake."""


class ControlClient:
    """A control-pipe client that verifies the server's identity before every
    command. Two seam shapes:

    * **Single-handle (production, TOCTOU-safe):** an injected
      ``connection_factory`` opens the server pipe ONCE; the owner SID is read
      from that live handle and, only if trusted, the request is written on the
      SAME handle. There is no verify-on-one-handle / send-on-another window a
      squatter could exploit (CC-WS5-007-TOCTOU).
    * **Split decision seams (pure tests):** an injected ``owner_sid_reader`` +
      ``transport`` keep the accept-trusted / refuse-squatted DECISION a
      falsifiable property with no Win32.

    Either way a command refuses (raises :class:`ControlServerUntrustedError`)
    unless the owner is trusted, so an admin verb can never reach a squatted
    pipe. The typed verb methods
    (``status``/``version``/``start``/``stop``/``restart``/``drain``/
    ``runtime_set``) build the ``{"v":1,...}`` request envelope."""

    def __init__(
        self,
        *,
        owner_sid_reader: OwnerSidReader | None = None,
        transport: ControlTransport | None = None,
        connection_factory: ConnectionFactory | None = None,
        name: str = CONTROL_PIPE_NAME,
        logger: logging.Logger | None = None,
    ) -> None:
        if connection_factory is None and (owner_sid_reader is None or transport is None):
            raise ValueError(
                "ControlClient needs either a single-handle connection_factory "
                "(TOCTOU-safe production path) or BOTH owner_sid_reader and "
                "transport (the split decision seams for the pure tests)"
            )
        self._owner_sid_reader = owner_sid_reader
        self._transport = transport
        self._connection_factory = connection_factory
        self._name = name
        self._logger = logger or logging.getLogger(__name__)

    def _verdict(self, owner_sid: str | None) -> IdentityVerdict:
        """Decide whether ``owner_sid`` is a trusted system owner, with a
        human-readable reason. Shared by the single-handle path and the split
        ``verify_server_identity`` so the accept/refuse decision is identical."""

        trusted = is_trusted_owner(owner_sid)
        if trusted:
            detail = f"server pipe {self._name!r} owned by trusted SID {owner_sid}"
        elif owner_sid is None:
            detail = f"server pipe {self._name!r} owner unreadable; refusing (fail-closed)"
        else:
            detail = (
                f"server pipe {self._name!r} owned by UNTRUSTED SID {owner_sid} "
                "(possible squat); refusing"
            )
        return IdentityVerdict(trusted=trusted, owner_sid=owner_sid, detail=detail)

    def verify_server_identity(self) -> IdentityVerdict:
        """Read the server pipe's OWNER SID (via the split ``owner_sid_reader``
        seam) and decide whether it is a trusted system owner. Never raises: an
        unreadable owner is a fail-closed ``trusted=False`` verdict, not an
        exception. Only meaningful on a split-seam client; the single-handle
        production path verifies the owner INSIDE ``_send`` on the live handle."""

        if self._owner_sid_reader is None:
            raise RuntimeError(
                "verify_server_identity is the split-seam decision path; the "
                "single-handle client verifies the owner on the live send handle"
            )
        return self._verdict(self._owner_sid_reader())

    def _send(self, command: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Verify the server identity, then send ``command``. Refuses (raises
        :class:`ControlServerUntrustedError`) BEFORE anything is written if the
        owner is untrusted -- so a squatted server never receives the command.

        Single-handle production path: open ONCE, read the owner from that live
        handle, and (only if trusted) write the request on the SAME handle
        (CC-WS5-007-TOCTOU). Split-seam path (pure tests): verify via
        ``owner_sid_reader``, then send via ``transport``."""

        request: dict[str, Any] = {"v": 1, "cmd": command}
        if extra:
            request.update(extra)
        if self._connection_factory is not None:
            return self._send_single_handle(command, request)
        verdict = self.verify_server_identity()
        if not verdict.trusted:
            self._logger.warning("control client refusing %r: %s", command, verdict.detail)
            raise ControlServerUntrustedError(verdict)
        assert self._transport is not None  # guaranteed by __init__ when no factory
        return self._transport(request)

    def _send_single_handle(self, command: str, request: dict[str, Any]) -> dict[str, Any]:
        """CC-WS5-007-TOCTOU: open the pipe ONCE, read the owner from that live
        handle, and -- only if trusted -- write the request + read the reply on
        the SAME handle, closing it once. A squatter cannot recreate the pipe
        between the verify and the send because there is only one open."""

        assert self._connection_factory is not None  # single-handle path only
        connection = self._connection_factory()
        try:
            verdict = self._verdict(connection.owner_sid())
            if not verdict.trusted:
                self._logger.warning("control client refusing %r: %s", command, verdict.detail)
                raise ControlServerUntrustedError(verdict)
            return connection.send(request)
        finally:
            connection.close()

    # -- read tier --------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return self._send("status")

    def version(self) -> dict[str, Any]:
        return self._send("version")

    # -- admin tier -------------------------------------------------------

    def start(self) -> dict[str, Any]:
        return self._send("start")

    def stop(self) -> dict[str, Any]:
        return self._send("stop")

    def restart(self) -> dict[str, Any]:
        return self._send("restart")

    def drain(self) -> dict[str, Any]:
        return self._send("drain")

    def runtime_set(self, runtime: str) -> dict[str, Any]:
        """Set the D-runtime selector on the server (the admin router validates
        the target and refuses an illegal one)."""

        return self._send("runtime_set", {"runtime": runtime})


# ---------------------------------------------------------------------------
# Production seams (lazy pywin32; the real round trip is VM-bound, disclosed)
# ---------------------------------------------------------------------------


class ControlClientTransportError(RuntimeError):
    """A wire-level failure talking to the (already identity-verified) server:
    the connection dropped, the reply was oversized, or it was malformed. Kept
    distinct from :class:`ControlServerUntrustedError` (an identity refusal), which
    happens BEFORE anything is sent."""


class _Win32ControlConnection:
    """CC-WS5-007-TOCTOU: ONE live server-pipe handle. Opened once (with EXACTLY
    the RAT-003 Authenticated-Users mask, which includes ``READ_CONTROL`` so the
    SAME open can both read the owner SID and read/write the request), the owner
    is read from and the request is written to this one handle -- closing the
    verify-open / send-open race the old split reader+transport left. Lazily
    imports pywin32 so this module imports on Linux; the real round trip against a
    running SYSTEM-owned server is VM-bound (``evidence/PENDING.md``).

    A pipe that cannot be opened yields ``handle=None``: ``owner_sid`` then reads
    ``None`` (fail-closed -> untrusted), the client refuses, and ``send`` is never
    reached."""

    def __init__(self, name: str) -> None:
        import pywintypes
        import win32file

        self._name = name
        try:
            self._handle: Any | None = win32file.CreateFile(
                name, AUTHENTICATED_USERS_ACCESS_MASK, 0, None, win32file.OPEN_EXISTING, 0, None
            )
        except pywintypes.error:
            self._handle = None

    def owner_sid(self) -> str | None:
        """Read the OWNER SID from the live handle via ``GetSecurityInfo`` (OWNER
        info). ``None`` (fail-closed -> untrusted) when the pipe could not be
        opened or the owner cannot be read."""

        if self._handle is None:
            return None
        import pywintypes
        import win32security

        try:
            security_descriptor = win32security.GetSecurityInfo(
                self._handle,
                win32security.SE_KERNEL_OBJECT,
                win32security.OWNER_SECURITY_INFORMATION,
            )
            owner_sid = security_descriptor.GetSecurityDescriptorOwner()
            if owner_sid is None:
                return None
            result: str = win32security.ConvertSidToStringSid(owner_sid)
            return result
        except pywintypes.error:
            return None

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        """Write one framed request to the SAME live handle the owner was verified
        on, then read and parse the reply. Only reached after a trusted-owner
        verdict, so a squatter never receives a command."""

        import pywintypes
        import win32file

        if self._handle is None:  # pragma: no cover - refused before send when None
            raise ControlClientTransportError("control pipe is not open")
        win32file.WriteFile(self._handle, encode_frame(request))
        buffer = b""
        while b"\n" not in buffer:
            try:
                _, chunk = win32file.ReadFile(self._handle, 4096)
            except pywintypes.error as exc:
                raise ControlClientTransportError(f"read failed: {exc}") from exc
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > FRAME_CAP_BYTES:
                raise ControlClientTransportError("oversized reply frame")
        line = buffer.split(b"\n", 1)[0]
        frame = parse_frame(line)
        if not frame.ok or frame.payload is None:
            raise ControlClientTransportError(f"malformed reply: {frame.close_reason}")
        return frame.payload

    def close(self) -> None:
        """Close the one handle (idempotent-safe: a never-opened connection closes
        cleanly)."""

        if self._handle is None:
            return
        import win32file

        win32file.CloseHandle(self._handle)
        self._handle = None


def _default_connection_factory(name: str) -> ConnectionFactory:
    """Build the production single-handle connection factory bound to ``name``.
    Construction touches NO Win32 -- the pipe is only opened when a command is
    actually sent (on the VM) -- so this runs at wiring time on any OS."""

    def factory() -> ControlConnection:
        return _Win32ControlConnection(name)

    return factory


def build_control_client(*, name: str = CONTROL_PIPE_NAME) -> ControlClient:
    """Assemble a production :class:`ControlClient` bound to the D7 pipe name with
    the TOCTOU-safe single-handle connection factory (open once, verify the owner
    on that live handle, send on the same handle). Construction touches NO Win32
    -- the connection is only opened when a command is actually sent (on the VM)
    -- so this assembler runs at wiring time on any OS."""

    return ControlClient(connection_factory=_default_connection_factory(name), name=name)


__all__ = [
    "ConnectionFactory",
    "ControlClient",
    "ControlClientTransportError",
    "ControlConnection",
    "ControlServerUntrustedError",
    "ControlTransport",
    "IdentityVerdict",
    "OwnerSidReader",
    "build_control_client",
    "is_trusted_owner",
]
