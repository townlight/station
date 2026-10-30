# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Reclaim a relay's UDP port from an ORPHANED relay ffmpeg that still holds it (U31).

THE DEFECT (OBSERVED, station, 2026-09-25, ``supervisor.log`` 07:20:32 -
07:32:15, and the relay logs it left behind). The supervisor killed an unready
control plane; the three HLS relay ffmpegs that control plane had spawned
SURVIVED as orphans holding their relays' loopback UDP ports. Every supervised
relay spawned after that failed at once --

    [udp @ 000001c56c24ac00] bind failed: Error number -10048 occurred
    Error opening input file udp://127.0.0.1:18419?overrun_nonfatal=1&...

(``C:\\ProgramData\\CivicCast\\data\\egress\\public\\logs\\hls-relay.Web_preview_HLS.stderr.log.1``,
spawn=8) -- because ``-10048`` is ``WSAEADDRINUSE``: the orphan, not the new
child, owns the port. The new child exits immediately, the U12 restore path
respawns it on its 60 s backoff, and the orphan keeps the port. All three
channels stayed off air for ~11 minutes, until the orphans were killed by hand.

The primary fix for the orphan itself is
:mod:`civiccast.platform.child_containment` (the control plane puts itself in
its own anonymous kill-on-close job, so its children die with it). This module
is the DEFENCE IN DEPTH for the case that fix cannot cover: a machine whose
containment failed to establish (that failure is logged at ERROR, never
swallowed), a control-plane build older than the fix, or any other process on
the box that spawned a relay-shaped ffmpeg before this build started. In all of
those the port is held by a process that will never exit on its own, and no
amount of restarting the relay can clear it.

WHY THE PORT HOLDER CAN BE IDENTIFIED AT ALL. ``hls_relay_uri_for``
(``civiccast/egress/hls_relay.py:287``) derives the relay URI from the sink URI
alone -- ``udp://127.0.0.1:{_DEFAULT_PORT_BASE + sha256(sink_uri)[:2] % 500}`` --
so an orphan spawned by a previous control plane carries the SAME
``-i udp://127.0.0.1:<port>?overrun_nonfatal=1&fifo_size=50000000`` token the new
child would have used. That token is this module's identity match: the argv of
the process holding the port is compared against the exact URI this supervisor
is about to bind, so a kill is only ever issued against a process that is
demonstrably a relay for THIS (channel, sink) -- never against "whatever owns
port N".

MEASURED, so the mechanism is not assumed (2026-09-25, this box, scratch probes
under ``%TEMP%\\u31``): ``psutil.net_connections(kind="udp")`` DOES name the
owning pid of a bound UDP port on Windows (111 of 111 rows carried a pid, no
elevation), and it agreed exactly with a raw ``GetExtendedUdpTable`` /
``MIB_UDPROW_OWNER_PID`` call on the same port. ffmpeg has no launcher process
in front of it (unlike ``.venv\\Scripts\\python.exe``, which re-execs a second
interpreter), so the table names the ffmpeg pid directly.

WHAT IS DELIBERATELY NOT DONE HERE. Nothing in this module kills on the basis
of "an ffmpeg is on my port". Every refusal case in :func:`decide_reclaim` --
no owner, not an ffmpeg, an argv that is not this relay's, a pid this supervisor
is currently supervising -- returns a verdict that names the reason and the
caller logs it. A live relay of our OWN holding the port (two sinks whose URIs
hash to the same port: ``_PORT_RANGE`` is 500, so a station with many HLS sinks
can collide) must never be killed to make room; that is a port-collision bug to
report, not an orphan to reap.

Killing reuses :func:`civiccast.egress.process_identity.verify_and_kill_process`
rather than ``psutil.Process.kill`` directly, so the pid-reuse race is closed
the same way every other kill in this package closes it: the create time is
re-checked at kill time and a recycled pid is left alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from civiccast.egress.process_identity import verify_and_kill_process

_LOG = logging.getLogger(__name__)

#: ffmpeg's own words when a UDP bind loses to another process (OBSERVED, the
#: station's relay log quoted in the module docstring). ``-10048`` is
#: ``WSAEADDRINUSE``; ffmpeg prints the negative form on Windows, so both signs
#: are matched rather than assuming one.
_BIND_FAILURE_MARKERS: tuple[bytes, ...] = (
    b"bind failed: Error number -10048",
    b"bind failed: Error number 10048",
)

#: How much of the relay log's tail to read. The bind failure is printed within
#: the first few hundred bytes of ffmpeg's banner, so this is generous; it also
#: bounds the read on a 5 MiB log whose tail is a busy segment-muxing stream.
BIND_FAILURE_TAIL_BYTES = 64 * 1024


def relay_log_shows_bind_failure(
    path: Path | None, *, tail_bytes: int = BIND_FAILURE_TAIL_BYTES
) -> bool:
    """Did this relay child fail to bind its UDP input (``WSAEADDRINUSE``)?

    ``path`` is the child's own U24 log (``hls_relay.HlsRelaySupervisor.
    _relay_log_path``). That file always belongs to the CURRENT (i.e. last)
    child: it is rotated at every spawn, and the spawn that just failed wrote
    its header before the child existed. Reading its tail is therefore evidence
    about the child that just died, not about an earlier generation.

    Fails CLOSED: a missing path, a missing file, an unreadable file, or any
    other OSError returns ``False`` ("no evidence of a bind failure"). The
    consequence of a false negative is the current, bounded 60 s retry cadence
    -- the consequence of a false positive would be killing a process on
    evidence we do not have.
    """
    if path is None:
        return False
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            tail = handle.read()
    except OSError:
        return False
    return any(marker in tail for marker in _BIND_FAILURE_MARKERS)


def udp_port_of(relay_uri: str) -> int | None:
    """The local UDP port of a relay URI, or ``None`` when it is not one.

    ``udp://127.0.0.1:18419`` -> ``18419``. ``None`` for anything else (a
    different scheme, a missing port, a malformed port): the caller must not
    guess a port to go looking for.
    """
    try:
        parts = urlsplit(relay_uri)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "udp" or port is None:
        return None
    return port


@dataclass(frozen=True)
class PortOwner:
    """The process holding a UDP port, as the OS connection table describes it.

    ``cmdline`` is a tuple rather than a list so a verdict over it is a verdict
    over an immutable snapshot -- the check and the kill are separated by a
    syscall each, and a mutable snapshot invites a race-shaped bug.

    ``created_at`` is ``psutil``'s create time, carried verbatim to
    :func:`verify_and_kill_process` so it can refuse a recycled pid.
    """

    pid: int
    name: str
    exe: str
    cmdline: tuple[str, ...]
    created_at: float


@dataclass(frozen=True)
class ReclaimVerdict:
    """Whether the port holder may be killed, and the reason either way.

    ``reason`` is a complete clause the caller logs verbatim ("refusing to kill
    pid 1234 (not ffmpeg: 'nginx.exe')"), so a refusal an operator has to act on
    is legible without reading this module.
    """

    reclaim: bool
    reason: str


def _looks_like_ffmpeg(owner: PortOwner) -> bool:
    """Is the holder an ffmpeg binary, judged by its image path (or name).

    ``ffmpeg.exe`` (and ``ffmpeg`` on a POSIX box) qualify; anything else does
    not, including a shell or a python that merely has "ffmpeg" in its
    arguments -- the argv check below is a separate, additional gate.
    """
    for candidate in (owner.exe, owner.name):
        if not candidate:
            continue
        stem = Path(candidate).name.lower()
        if stem.startswith("ffmpeg"):
            return True
    return False


def _carries_this_relay(owner: PortOwner, relay_uri: str) -> bool:
    """Does the holder's command line name THIS relay's UDP input?

    The spawn argv passes the relay URI with ffmpeg's UDP options appended
    (``<relay_uri>?overrun_nonfatal=1&fifo_size=50000000`` -- see
    ``hls_relay._start_relay_locked``), so both the bare URI and the suffixed
    form count; nothing else does.
    """
    return any(token == relay_uri or token.startswith(f"{relay_uri}?") for token in owner.cmdline)


def decide_reclaim(
    owner: PortOwner | None,
    *,
    relay_uri: str,
    excluded_pids: frozenset[int] | set[int] = frozenset(),
) -> ReclaimVerdict:
    """May this port holder be killed so the relay can bind? PURE -- no I/O.

    Every gate must pass; the first one that fails names the refusal. The
    ``relay_uri`` argument is the URI this supervisor is about to bind, so the
    match is "this exact relay's input", never "some ffmpeg".
    """
    if owner is None:
        return ReclaimVerdict(
            reclaim=False,
            reason="nothing in the UDP connection table owns that port",
        )
    if owner.pid <= 0:
        return ReclaimVerdict(
            reclaim=False, reason=f"the port owner reports a non-pid ({owner.pid})"
        )
    if owner.pid in excluded_pids:
        return ReclaimVerdict(
            reclaim=False,
            reason=(
                f"pid {owner.pid} is a relay this supervisor is still running "
                "(a port collision between two sinks, not an orphan)"
            ),
        )
    if not _looks_like_ffmpeg(owner):
        return ReclaimVerdict(
            reclaim=False,
            reason=(
                f"pid {owner.pid} is not an ffmpeg (exe={owner.exe or owner.name or 'unknown'!r})"
            ),
        )
    if not _carries_this_relay(owner, relay_uri):
        return ReclaimVerdict(
            reclaim=False,
            reason=(
                f"pid {owner.pid} is an ffmpeg, but its command line does not name "
                f"{relay_uri} (it is some other process's ffmpeg)"
            ),
        )
    return ReclaimVerdict(
        reclaim=True,
        reason=f"pid {owner.pid} is an orphaned relay ffmpeg holding {relay_uri}",
    )


class PortOwnerApi(Protocol):
    """The OS seam: who holds a UDP port, and kill them.

    ``PsutilPortOwnerApi`` is the production implementation; the pure tests in
    ``tests/egress/test_hls_relay_orphan_reclaim.py`` drive the supervisor's
    wiring with a fake, and the real-process case runs against the real one.
    """

    def find_owner(self, port: int) -> PortOwner | None: ...

    def kill(self, owner: PortOwner) -> bool: ...


class PsutilPortOwnerApi:
    """``psutil``'s UDP connection table, plus the package's TOCTOU-safe kill.

    Both imports are lazy (``psutil`` has no inline types and is a platform
    dependency of this package's process handling, not of importing it), and
    every probe failure is swallowed to ``None``/``False``: an unreadable
    connection table must degrade to "no evidence", never take down a poll tick.
    """

    def find_owner(self, port: int) -> PortOwner | None:
        import psutil

        try:
            connections = psutil.net_connections(kind="udp")
        # Broad by design: psutil raises AccessDenied on some hardened hosts and
        # OSError on others, and this runs inside the relay poll tick where an
        # exception would cost the channel its health check.
        except Exception:
            _LOG.debug("UDP connection table unavailable; no port owner probed.", exc_info=True)
            return None
        for connection in connections:
            local = getattr(connection, "laddr", None)
            if getattr(local, "port", None) != port:
                continue
            pid = getattr(connection, "pid", None)
            if not isinstance(pid, int) or pid <= 0:
                continue
            owner = self._describe(pid)
            if owner is not None:
                return owner
        return None

    def _describe(self, pid: int) -> PortOwner | None:
        """Turn a pid into a snapshot, or ``None`` when it cannot be trusted.

        ``create_time`` is REQUIRED: without it the kill cannot be made
        pid-reuse-safe, so a process whose create time cannot be read is not a
        kill target at all. ``exe``/``cmdline`` are best-effort (both raise
        AccessDenied for a process owned by another user) -- an empty cmdline
        simply fails the argv gate downstream, which is the safe direction.
        """
        import psutil

        try:
            process = psutil.Process(pid)
            created_at = process.create_time()
            name = process.name()
            try:
                exe = process.exe()
            except Exception:
                exe = ""
            try:
                cmdline = tuple(process.cmdline())
            except Exception:
                cmdline = ()
        except Exception:
            return None
        return PortOwner(pid=pid, name=name, exe=exe, cmdline=cmdline, created_at=created_at)

    def kill(self, owner: PortOwner) -> bool:
        return verify_and_kill_process(owner.pid, owner.created_at)
