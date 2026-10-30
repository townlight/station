# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The CivicCast-side contract for the self-hosted VDO.Ninja instance (S17).

CivicCast never links any VDO.Ninja code into this Apache tree (VDO.Ninja is
AGPL-3.0, run UNMODIFIED as a separate process — S17 §7). Its only integration
surface is:

* **URL minting** — deterministic VDO.Ninja room/guest/view URLs built from the
  room's opaque ``vdo_room_name`` and a single-use invite token. No socket is
  opened to mint a URL; the actual WebRTC negotiation is VDO.Ninja's, browser-
  side, via its IFRAME API (postMessage) embedded in the operator console.
* **diagnostics** — TURN reachability + VDO/coturn co-process health, delegated
  to an injected probe (the S9 supervisor lands in slice 3d; until then the
  probe is unwired and reports honestly "unavailable", never a false "reachable").

This module defines the contract (``VdoNinjaBridge`` protocol + result/error
types), a fail-closed ``NullVdoNinjaBridge`` for when the tier is not configured,
and ``UrlVdoNinjaBridge`` — the real, deterministic URL builder. Tests inject a
fake implementing the protocol.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, ConfigDict

from civiccast.live.contribution.models import ContributionRoom

# The compositor consumes a "solo view" of one guest's published stream; the
# guest opens a "publish into room" URL. Both are derived from the room token.


class GuestUrls(BaseModel):
    """The pair of VDO.Ninja URLs minted for one guest invite."""

    model_config = ConfigDict(extra="forbid")

    # What the GUEST opens in any browser (no install) — publishes their cam/mic
    # into the room, tagged by the single-use token as the stream id.
    view_url: str
    # What the COMPOSITOR (GStreamer wpesrc / OBS browser source, slice 3e)
    # renders — a solo view of just this guest's published stream.
    push_url: str


class VdoDiagnostics(BaseModel):
    """A read-only health snapshot for the operator diagnostics drawer (S17 §5).

    ``turn_reachable`` is the load-bearing commissioning signal (a guest behind
    NAT cannot join without it). The process-up flags come from the S9 co-process
    supervisor (slice 3d).

    ``turn_reachable`` reflects the most recent probe of ``turn_host``:
    ``turn_port`` regardless of ``coturn_process_up`` — the owner-approved
    "documented external TURN" posture (coturn has no native Windows build;
    see ``civiccast/installer/contribution_install.py``) means there is
    deliberately no LOCAL coturn process to supervise, but the configured
    external server's reachability is still the thing that matters to a
    guest behind NAT, and is still probed.

    ``turn_host`` / ``turn_port`` echo the effective, currently-configured
    TURN target (``CIVICCAST_TURN_HOST`` / ``CIVICCAST_TURN_PORT``, read at
    service start) so the operator console can show what's actually
    configured, not just whether it's reachable."""

    model_config = ConfigDict(extra="forbid")

    turn_reachable: bool = False
    turn_host: str | None = None
    turn_port: int | None = None
    vdo_process_up: bool = False
    coturn_process_up: bool = False
    relay_process_up: bool = False
    ice_summary: str = ""
    detail: str = ""


class VdoBridgeError(RuntimeError):
    """Raised when the VDO.Ninja tier is not configured / cannot mint a URL.

    Carries the failure TYPE only — never a token or a credential."""


def guest_stream_id(invite_token: str) -> str:
    """Stable media path that does not reveal the single-use join token."""
    return hashlib.sha256(invite_token.encode("utf-8")).hexdigest()[:32]


class VdoNinjaBridge(Protocol):
    """The contract the contribution service calls; a configured VDO instance
    (or a test fake) fulfils it."""

    def director_url(self, room: ContributionRoom) -> str:
        """The operator's director-view URL (embedded in the console iframe)."""
        ...

    def guest_urls(self, room: ContributionRoom, *, invite_token: str, role: str) -> GuestUrls:
        """Mint the guest publish URL + the compositor solo-view URL."""
        ...

    def diagnostics(self) -> VdoDiagnostics:
        """TURN reachability + VDO/coturn co-process health + ICE summary."""
        ...

    def test_turn_connectivity(self) -> VdoDiagnostics:
        """Probe TURN reachability RIGHT NOW (not the last background poll) and
        return the refreshed diagnostics. The operator console's "Test TURN
        connectivity" button calls this synchronously."""
        ...


class NullVdoNinjaBridge:
    """Default bridge when the remote-contribution tier is NOT configured.

    Fails closed: URL minting raises (so an operator can never mint a join link
    to a VDO that isn't there), and diagnostics report everything down with a
    clear reason. The console renders this as 'remote contribution not
    configured' (S17 §5) rather than a dead UI that looks functional."""

    _UNAVAILABLE = "VDO.Ninja remote-contribution tier is not configured"

    def director_url(self, room: ContributionRoom) -> str:
        raise VdoBridgeError(self._UNAVAILABLE)

    def guest_urls(self, room: ContributionRoom, *, invite_token: str, role: str) -> GuestUrls:
        raise VdoBridgeError(self._UNAVAILABLE)

    def diagnostics(self) -> VdoDiagnostics:
        return VdoDiagnostics(detail=self._UNAVAILABLE)

    def test_turn_connectivity(self) -> VdoDiagnostics:
        # Nothing to test — there is no supervisor to probe with.
        return VdoDiagnostics(detail=self._UNAVAILABLE)


class UrlVdoNinjaBridge:
    """Deterministic VDO.Ninja URL builder against a self-hosted instance.

    URL minting opens NO socket — the URLs are pure functions of the room token +
    invite token, exactly what the VDO.Ninja IFRAME API expects. ``diagnostics``
    delegates to an injected probe (the S9 supervisor, slice 3d); with no probe
    wired it reports honestly unavailable rather than a false 'reachable'."""

    def __init__(
        self,
        base_url: str,
        *,
        diagnostics_probe: Callable[[], VdoDiagnostics] | None = None,
        connectivity_test: Callable[[], VdoDiagnostics] | None = None,
        mediamtx_url: str | None = None,
    ) -> None:
        base = base_url.strip().rstrip("/")
        if not base:
            # An empty base would mint relative junk URLs that silently fail in
            # the guest's browser — refuse at construction (the wiring picks
            # NullVdoNinjaBridge when no base is configured).
            raise VdoBridgeError("UrlVdoNinjaBridge requires a non-empty base_url")
        self._base = base
        if mediamtx_url is not None:
            mediamtx_url = mediamtx_url.strip().rstrip("/")
            parsed = urlsplit(mediamtx_url)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise VdoBridgeError("MediaMTX needs a public HTTP or HTTPS base URL")
        self._mediamtx_url = mediamtx_url
        self._diagnostics_probe = diagnostics_probe
        self._connectivity_test = connectivity_test

    def director_url(self, room: ContributionRoom) -> str:
        return f"{self._base}/?{urlencode({'director': room.vdo_room_name})}"

    def guest_urls(self, room: ContributionRoom, *, invite_token: str, role: str) -> GuestUrls:
        # With MediaMTX the stream ID is also a readable media path; the join
        # capability must not become that path. Legacy VDO-only links keep the
        # historical ID until their sessions expire.
        stream_id = guest_stream_id(invite_token) if self._mediamtx_url else invite_token
        view_args = {"room": room.vdo_room_name, "push": stream_id, "label": role}
        if self._mediamtx_url:
            view_args["mediamtx"] = self._mediamtx_url
        view_q = urlencode(view_args)
        # Compositor renders a solo view of just this guest's stream.
        push_q = urlencode({"view": stream_id, "room": room.vdo_room_name, "solo": "1"})
        return GuestUrls(
            view_url=f"{self._base}/?{view_q}",
            push_url=f"{self._base}/?{push_q}",
        )

    def diagnostics(self) -> VdoDiagnostics:
        if self._diagnostics_probe is None:
            return VdoDiagnostics(
                detail="VDO/coturn health probe not wired (S9 supervisor, slice 3d)"
            )
        return self._diagnostics_probe()

    def test_turn_connectivity(self) -> VdoDiagnostics:
        if self._connectivity_test is None:
            return VdoDiagnostics(
                detail="TURN connectivity test not wired (S9 supervisor, slice 3d)"
            )
        return self._connectivity_test()


__all__ = [
    "GuestUrls",
    "NullVdoNinjaBridge",
    "UrlVdoNinjaBridge",
    "VdoBridgeError",
    "VdoDiagnostics",
    "VdoNinjaBridge",
    "guest_stream_id",
]
