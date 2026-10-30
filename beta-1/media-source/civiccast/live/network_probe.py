# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Production network-reachability probe for the live pre-flight.

Bug B3 (field evidence, native beta candidate #17): the pre-flight
"Network reachable" check has always required a caller-supplied
``network_reachable`` value (``civiccast.live.preflight.PreflightInputs``).
The only caller wired up -- the operator's Run Meeting screen
(``LiveRoomScreen.tsx``) -- always submitted ``None``. There was no
"probe" button anywhere to run instead, so the check reported
``network.not_probed`` -- an internal reason code, not a sentence a
volunteer operator could act on -- on every single pre-flight run,
forever. The evaluator was correct to fail closed; nothing in the
product ever answered the question it was asking.

This module is that answer. It attempts a short, bounded TCP connection
to a small set of well-known, highly available hosts and reports whether
any of them answered -- the same "is this box on the internet" question
an operator would otherwise answer by opening a browser tab. It never
raises for probe-outcome reasons; ``(bool, message)`` is the contract,
matching :data:`civiccast.live.preflight.NetworkProbe`.
"""

from __future__ import annotations

import os
import socket

__all__ = [
    "DEFAULT_NETWORK_PROBE_TIMEOUT_SECONDS",
    "NetworkProbeFn",
    "build_network_probe",
    "probe_network_reachable",
]

from collections.abc import Callable

NetworkProbeFn = Callable[[], "tuple[bool, str | None]"]

#: Wall-clock ceiling per target. Short enough that an operator clicking
#: "Run pre-flight" gets an answer quickly even when every target is
#: unreachable; long enough for a real TCP handshake over a slow WAN link.
DEFAULT_NETWORK_PROBE_TIMEOUT_SECONDS = 3.0

# Well-known, highly-available anycast resolvers. Plain TCP connect (no
# HTTP, no TLS, no DNS lookup of a hostname) -- this only answers "can this
# box open a TCP socket to the internet," not "is any particular CivicCast
# dependency up." Two independent operators (Cloudflare, Google) so one
# provider's own outage doesn't read as this station being offline.
_PROBE_TARGETS: tuple[tuple[str, int], ...] = (
    ("1.1.1.1", 443),
    ("8.8.8.8", 443),
)


def _timeout_from_env(default: float) -> float:
    raw = os.environ.get("CIVICCAST_NETWORK_PROBE_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def probe_network_reachable(*, timeout_seconds: float) -> tuple[bool, str | None]:
    """Return whether this station can currently reach the internet.

    Tries each target in :data:`_PROBE_TARGETS` in order and returns as
    soon as one answers. Returns ``(False, message)`` naming what was
    tried when none answer, so the pre-flight message is actionable
    instead of a bare "unreachable."
    """

    last_error: str | None = None
    for host, port in _PROBE_TARGETS:
        try:
            with socket.create_connection((host, port), timeout=timeout_seconds):
                return True, f"Reached {host}:{port} over the internet."
        except OSError as exc:
            last_error = str(exc)
            continue
    tried = ", ".join(f"{host}:{port}" for host, port in _PROBE_TARGETS)
    detail = f" ({last_error})" if last_error else ""
    return False, (
        f"Could not reach the internet (tried {tried}{detail}). Check the station's "
        "network cable or Wi-Fi connection, then run pre-flight again."
    )


def build_network_probe(*, timeout_seconds: float | None = None) -> NetworkProbeFn:
    """Build the callable ``PreflightEvaluator`` expects for its network check.

    ``civiccast.app._resolve_preflight_evaluator`` calls this with no
    arguments so every real station probes for itself.
    ``CIVICCAST_NETWORK_PROBE_TIMEOUT_SECONDS`` tunes the per-target
    timeout without a code change.
    """

    resolved_timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else _timeout_from_env(DEFAULT_NETWORK_PROBE_TIMEOUT_SECONDS)
    )

    def _probe() -> tuple[bool, str | None]:
        return probe_network_reachable(timeout_seconds=resolved_timeout)

    return _probe
