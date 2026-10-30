# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Survive a Windows reserved/excluded TCP port before ``pg_ctl start`` ever
runs (real-world LPM deployment failure, 2026-08-27, candidate 75cc13f, two
independent installer runs on the same box):

    could not bind IPv4 address "127.0.0.1": Permission denied
    FATAL:  could not create any TCP/IP sockets
    pg_ctl: could not start server

Both runs failed identically at ``d4-provision`` (installer exit 116, engine
rc 75) -- ``PROVISION-RECOVERY.md`` correctly named the exact pg_ctl/postgres
diagnostic (that recovery-document wiring, PR #51, worked; this module fixes
the underlying cause it could only describe, not survive). Windows returns
``WSAEACCES`` ("Permission denied") for a loopback bind almost exclusively
when the port sits inside an administratively EXCLUDED TCP port range --
Hyper-V/WSL's ``winnat`` service reserves dynamic port blocks at boot (they
move across reboots) -- or when security software blocks the bind outright.
Any program asking for that exact port fails identically; PostgreSQL is not
special here.

Two pure decision functions, each independently unit-testable with no real
socket/subprocess (mirrors :mod:`civiccast.native.provision.models`'s
evaluate_postgres_cluster convention: the DECISION is pure, only the I/O
around it is a seam):

* :func:`parse_excluded_port_ranges` -- turns ``netsh int ipv4 show
  excludedportrange protocol=tcp``'s text output into ``(start, end)`` pairs.
* :func:`select_provision_port` -- walks a candidate port list in order,
  skipping any candidate inside an excluded range, otherwise test-binding it
  via the injected ``test_bind`` seam; returns the first bindable candidate,
  or an honest ``no_candidate_available`` outcome naming every attempt.

:func:`resolve_provision_port` is the thin, real-seam wrapper the CLI calls
(untested directly, same HARD RULE as ``seams.default_ensure_database``: no
real ``pg_ctl``/``psql``/postgres in the unit suite -- a real TCP bind test
and a real ``netsh`` query are NOT postgres, but the same "keep the real I/O
at the edge, prove the decision logic in isolation" shape applies).
"""

from __future__ import annotations

import re
import socket
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from civiccast.native.pg_ctl_exec import run_captured_argv

# ---------------------------------------------------------------------------
# netsh excluded-range parsing (pure)
# ---------------------------------------------------------------------------

#: Matches one data row of ``netsh int ipv4 show excludedportrange``'s table,
#: e.g. "     5432        5432" or "     50000       50059     *" (the
#: trailing "*" marks an administratively-persisted exclusion, per netsh's own
#: "* - Administered port exclusions." footer -- irrelevant to this parser,
#: which treats every listed range as excluded regardless of that flag).
#: Header/divider lines ("Start Port    End Port", "----------  --------",
#: "Protocol tcp Port Exclude Ranges") contain no two-integer pair and never
#: match.
_RANGE_LINE_RE = re.compile(r"^\s*(\d{1,5})\s+(\d{1,5})\s*\*?\s*$")

_MIN_PORT = 1
_MAX_PORT = 65535


def parse_excluded_port_ranges(netsh_output: str) -> tuple[tuple[int, int], ...]:
    """Parse ``netsh int ipv4 show excludedportrange protocol=tcp``'s stdout
    into ``(start, end)`` inclusive port-range pairs.

    Tolerant by construction: any line that is not exactly two whitespace-
    separated integers (optionally followed by a literal ``*``) is silently
    skipped -- this covers the banner line, the column header, the dashed
    divider, the trailing "* - Administered port exclusions." footnote, a
    blank line, and any locale/version variation in the surrounding text
    netsh does not document as stable. A malformed or out-of-range pair
    (start/end outside 1..65535, or start > end) is also skipped rather than
    raised -- this parser's job is "read what is unambiguously a range",
    never "validate netsh's own output"; a skipped line just means one fewer
    known exclusion, which :func:`select_provision_port` recovers from
    anyway via its own real test-bind of each candidate.
    """

    ranges: list[tuple[int, int]] = []
    for line in netsh_output.splitlines():
        match = _RANGE_LINE_RE.match(line)
        if match is None:
            continue
        start, end = int(match.group(1)), int(match.group(2))
        if not (_MIN_PORT <= start <= _MAX_PORT and _MIN_PORT <= end <= _MAX_PORT):
            continue
        if start > end:
            continue
        ranges.append((start, end))
    return tuple(ranges)


def port_in_excluded_ranges(port: int, ranges: Sequence[tuple[int, int]]) -> bool:
    """Whether ``port`` falls inside any ``(start, end)`` inclusive range."""

    return any(start <= port <= end for start, end in ranges)


# ---------------------------------------------------------------------------
# Candidate selection (pure; test_bind is the one injected seam)
# ---------------------------------------------------------------------------

PortAttemptOutcome = Literal["excluded", "bind_failed", "available"]

#: ``(host, port) -> (ok, detail)`` -- the one real-I/O seam this module's
#: decision function depends on. The real implementation
#: (:func:`real_test_bind_port`) opens and immediately closes a TCP listen
#: socket; tests inject a fake so the decision logic never needs a real
#: socket or a real Windows host.
TestBindFn = Callable[[str, int], tuple[bool, str]]


@dataclass(frozen=True)
class PortAttempt:
    """One candidate port's outcome, in the order it was tried -- carried on
    :class:`PortSelectionResult` so the recovery document and the journal can
    show the operator exactly what was tried and why each one failed."""

    port: int
    outcome: PortAttemptOutcome
    detail: str


#: The small, DOCUMENTED candidate list (task requirement: "a small
#: documented candidate list"). 5432 is always tried first regardless of this
#: tuple's own order (see :func:`select_provision_port`'s ``candidates``
#: contract) -- these are the alternates, chosen as ordinary, uncontroversial
#: PostgreSQL ports well clear of the Windows dynamic/ephemeral range
#: (49152-65535, where a winnat/Hyper-V exclusion is most commonly found) so
#: a real port-in-use collision is unlikely on top of the explicit
#: excluded-range check every candidate already gets.
DEFAULT_PORT_CANDIDATES: tuple[int, ...] = (5432, 5433, 5434, 5435, 5544)


@dataclass(frozen=True)
class PortSelectionResult:
    """The outcome of :func:`select_provision_port`: either a bindable
    ``port`` (``outcome == "selected"``) or an honest, fully-detailed refusal
    (``outcome == "no_candidate_available"``, ``port is None``) naming every
    candidate tried and why each one was rejected."""

    outcome: Literal["selected", "no_candidate_available"]
    port: int | None
    attempts: tuple[PortAttempt, ...] = field(default_factory=tuple)
    detail: str = ""
    #: The raw ``netsh int ipv4 show excludedportrange protocol=tcp`` text
    #: this selection was made against (set only by :func:`resolve_provision_port`;
    #: empty for a direct :func:`select_provision_port` call, which takes
    #: already-parsed ranges and never sees the raw text). Carried here --
    #: not just the parsed ranges -- so an honest recovery document can quote
    #: Windows's own exclusion table verbatim (task requirement: "Windows
    #: excluded ranges: <the netsh output>").
    netsh_raw_output: str = ""


def _ordered_candidates(preferred_port: int, candidates: Sequence[int]) -> list[int]:
    """``preferred_port`` first, then every OTHER candidate in ``candidates``'
    given order, never repeating a port. This is what lets the standard 5432
    always be tried first (task requirement) while still trying every
    documented alternate exactly once."""

    ordered = [preferred_port]
    ordered.extend(port for port in candidates if port != preferred_port)
    return ordered


def select_provision_port(
    *,
    host: str,
    preferred_port: int,
    candidates: Sequence[int],
    excluded_ranges: Sequence[tuple[int, int]],
    test_bind: TestBindFn,
) -> PortSelectionResult:
    """Pure decision function: try ``preferred_port`` then every OTHER port in
    ``candidates`` (in order, each exactly once), skipping any candidate
    inside ``excluded_ranges`` without even attempting a bind (a port Windows
    has already told us is administratively excluded needs no probe to
    confirm), and real-bind-testing every other candidate via the injected
    ``test_bind`` seam.

    Returns the FIRST candidate that both clears the excluded-range check and
    binds successfully. If every candidate is either excluded or fails to
    bind, returns ``no_candidate_available`` with the full, ordered attempt
    list -- naming every port tried and why -- so the caller can write an
    honest recovery document rather than a generic failure.
    """

    attempts: list[PortAttempt] = []
    for port in _ordered_candidates(preferred_port, candidates):
        if port_in_excluded_ranges(port, excluded_ranges):
            attempts.append(
                PortAttempt(
                    port=port,
                    outcome="excluded",
                    detail=(
                        f"port {port} falls inside a Windows-administered excluded TCP "
                        "port range (netsh int ipv4 show excludedportrange); not attempted"
                    ),
                )
            )
            continue
        ok, bind_detail = test_bind(host, port)
        if ok:
            attempts.append(PortAttempt(port=port, outcome="available", detail=bind_detail))
            return PortSelectionResult(
                outcome="selected",
                port=port,
                attempts=tuple(attempts),
                detail=f"selected port {port}: {bind_detail}",
            )
        attempts.append(PortAttempt(port=port, outcome="bind_failed", detail=bind_detail))

    tried = ", ".join(f"{a.port} ({a.outcome}: {a.detail})" for a in attempts)
    return PortSelectionResult(
        outcome="no_candidate_available",
        port=None,
        attempts=tuple(attempts),
        detail=f"no usable port on host {host!r}; every candidate was rejected -- {tried}",
    )


# ---------------------------------------------------------------------------
# Real seams: a real TCP bind test, a real netsh query.
# ---------------------------------------------------------------------------


def real_test_bind_port(host: str, port: int) -> tuple[bool, str]:
    """Real :data:`TestBindFn`: open a TCP socket, bind it to ``(host,
    port)``, and immediately close it. This is the SAME check the real
    ``pg_ctl``/postgres bind would perform, run ourselves first and cheaply
    (no cluster start, no data-directory I/O) so a reserved/excluded port is
    caught before ``pg_ctl start`` ever runs against it.

    Deliberately does NOT set ``SO_REUSEADDR`` -- that option can mask a
    genuine conflict on Windows (letting a bind through against a socket
    another process still holds), which would defeat the entire point of
    this probe: proving the port is ACTUALLY free, not merely usually free.

    Never raises: any ``OSError`` (permission denied, address in use, or any
    other bind failure) is caught and classified into the ``(False, detail)``
    return, carrying the OS's own ``winerror``/``errno`` when present -- the
    same class of error the real LPM failure surfaced (``WSAEACCES`` /
    "Permission denied").
    """

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)
        return (
            False,
            f"bind failed on {host}:{port} (winerror={winerror}, errno={exc.errno}): {exc}",
        )
    else:
        return True, f"bind succeeded on {host}:{port}"
    finally:
        sock.close()


#: netsh's own hard-coded argv -- TCP only (the product only ever binds TCP).
_NETSH_EXCLUDED_RANGE_ARGV = ["netsh", "int", "ipv4", "show", "excludedportrange", "protocol=tcp"]
_NETSH_TIMEOUT_SECONDS = 30.0


def run_netsh_show_excluded_port_ranges(*, timeout_seconds: float = _NETSH_TIMEOUT_SECONDS) -> str:
    """Real seam: ``netsh int ipv4 show excludedportrange protocol=tcp`` via
    the house file-backed bounded executor
    (:func:`civiccast.native.pg_ctl_exec.run_captured_argv` -- never a raw
    pipe-capturing ``subprocess.run``, per that module's proven Windows
    pipe-inheritance hang).

    A nonzero exit or undecodable output is NOT raised -- it returns an empty
    string, which :func:`resolve_provision_port` treats as "no known
    exclusions" (the excluded-range check is a strict ADDITIONAL signal on
    top of the real bind test every candidate still gets; losing it never
    blocks port selection, it only means one fewer port is pre-skipped before
    its own bind attempt is tried).
    """

    result = run_captured_argv(_NETSH_EXCLUDED_RANGE_ARGV, timeout_seconds=timeout_seconds)
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", errors="replace")


def resolve_provision_port(
    *,
    host: str,
    preferred_port: int,
    candidates: Sequence[int] = DEFAULT_PORT_CANDIDATES,
    netsh_output_provider: Callable[[], str] | None = None,
    test_bind: TestBindFn | None = None,
) -> PortSelectionResult:
    """Real-seam wrapper: fetch the current Windows excluded-port-range table
    (best-effort -- see :func:`run_netsh_show_excluded_port_ranges`'s
    docstring), parse it, and drive :func:`select_provision_port` with the
    real bind-test seam.

    ``netsh_output_provider``/``test_bind`` are injectable (tests supply
    fakes so this module's own unit suite never spawns ``netsh.exe`` or opens
    a real socket at this layer -- that proof lives in this module's own
    ``real_test_bind_port``/``parse_excluded_port_ranges`` tests instead,
    each exercised directly). The CLI (``civiccast.native.provision.
    __main__``) calls this function BY NAME so its own tests can monkeypatch
    the module attribute exactly like every other real-seam call in that
    file (``run_provision``, ``reset_cluster_credential``, ...).

    A failure to even RUN the netsh probe (subprocess error/timeout) is
    swallowed here into an empty exclusion table with the failure noted in
    the returned ``detail`` prefix -- port selection must never be blocked by
    the DIAGNOSTIC step itself failing; the real bind test on every candidate
    is what actually proves availability.
    """

    provider = netsh_output_provider or run_netsh_show_excluded_port_ranges
    try:
        raw_netsh_output = provider()
    except (OSError, subprocess.TimeoutExpired) as exc:
        raw_netsh_output = ""
        netsh_note = f"[netsh excluded-port-range query could not run: {exc}] "
    else:
        netsh_note = ""

    excluded_ranges = parse_excluded_port_ranges(raw_netsh_output)
    result = select_provision_port(
        host=host,
        preferred_port=preferred_port,
        candidates=candidates,
        excluded_ranges=excluded_ranges,
        test_bind=test_bind or real_test_bind_port,
    )
    return PortSelectionResult(
        outcome=result.outcome,
        port=result.port,
        attempts=result.attempts,
        detail=(netsh_note + result.detail) if netsh_note else result.detail,
        netsh_raw_output=raw_netsh_output,
    )


def format_excluded_ranges_for_operator(netsh_raw_output: str) -> str:
    """Render the raw ``netsh`` output for the operator recovery document --
    verbatim when present, an honest placeholder when the query itself could
    not be read (see :func:`resolve_provision_port`'s netsh-failure note)."""

    text = netsh_raw_output.strip()
    return text if text else "(the netsh excluded-port-range query could not be read)"


__all__ = [
    "DEFAULT_PORT_CANDIDATES",
    "PortAttempt",
    "PortAttemptOutcome",
    "PortSelectionResult",
    "TestBindFn",
    "format_excluded_ranges_for_operator",
    "parse_excluded_port_ranges",
    "port_in_excluded_ranges",
    "real_test_bind_port",
    "resolve_provision_port",
    "run_netsh_show_excluded_port_ranges",
    "select_provision_port",
]
