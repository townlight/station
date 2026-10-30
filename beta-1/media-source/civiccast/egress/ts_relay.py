# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Persistent per-channel TS relay: one mux session across encoder relaunches.

#151: on the legacy ffmpeg-concat engine every plan boundary/reload/crash
relaunches the encoder, which starts a fresh MPEG-TS session — new continuity
counters on every PID and a new ephemeral UDP source port. A cable plant's
TR 101 290 monitoring logs CC errors at every such splice (CA-8 capture:
``disc=2`` on every content pid, 3 source-port changes in 60s of filler).

The fix keeps a **channel-lifetime** TSDuck ``tsp`` process between the
encoder and the headend for ``udp-ts`` sinks:

    ffmpeg (relaunches freely) --> udp://127.0.0.1:<listen>
        tsp -I ip <listen> -P continuity --fix -P pcradjust
            -O ip <real destination> --local-port <pinned>
    --> headend sees ONE socket session, continuous CC, smoothed PCR

* ``continuity --fix`` rewrites continuity counters to be continuous across
  the splice; ``pcradjust`` re-stamps PCRs onto the (CBR) output position so
  the new encoder's PCR base does not present as a PCR discontinuity.
* ``--local-port`` pins the relay's outgoing source port, so receivers that
  track the sender tuple never see the session change.
* TSDuck is the station-brought/managed toolkit the compliance probe already
  uses (:func:`civiccast.egress.compliance.locate_tsduck`,
  ``CIVICCAST_TSDUCK_PATH``, or the managed pull).

Deployment posture: ``CIVICCAST_TS_RELAY`` = ``auto`` (default: relay when
``tsp`` is available, warn-and-pass-through otherwise) | ``on`` (require —
error log when unavailable, still pass through rather than kill the channel)
| ``off``. The relay only ever touches ``udp-ts`` sinks; every other sink
kind is untouched.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from civiccast.egress.compliance import locate_tsduck
from civiccast.egress.models import EgressConfig, EgressSinkSpec

_LOG = logging.getLogger(__name__)

_MODE_ENV = "CIVICCAST_TS_RELAY"
_BASE_PORT_ENV = "CIVICCAST_TS_RELAY_BASE_PORT"
_DEFAULT_BASE_PORT = 17800
# Pinned outgoing source ports live one block above the listen ports.
_LOCAL_PORT_OFFSET = 1000


def relay_mode() -> str:
    mode = os.environ.get(_MODE_ENV, "auto").strip().lower() or "auto"
    if mode not in ("auto", "on", "off"):
        raise ValueError(f"{_MODE_ENV} must be auto|on|off; got {mode!r}.")
    return mode


def build_tsp_relay_args(
    tsp_path: str,
    *,
    listen_port: int,
    dest_host: str,
    dest_port: int,
    local_out_port: int,
    rcvbuf_bytes: int = 16 * 1024 * 1024,
) -> list[str]:
    """The persistent relay pipeline: ip-in -> fix CC -> smooth PCR -> ip-out."""
    return [
        tsp_path,
        "--realtime",
        "-I",
        "ip",
        str(listen_port),
        "--local-address",
        "127.0.0.1",
        "--buffer-size",
        str(rcvbuf_bytes),
        "-P",
        "continuity",
        "--fix",
        "-P",
        "pcradjust",
        "-O",
        "ip",
        f"{dest_host}:{dest_port}",
        "--local-port",
        str(local_out_port),
    ]


@dataclass
class _Relay:
    listen_port: int
    dest: str
    process: subprocess.Popen[bytes]


class TsRelaySupervisor:
    """Owns one channel-lifetime ``tsp`` relay per (channel, udp-ts destination)."""

    def __init__(
        self,
        *,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        locate: Callable[[], object] = locate_tsduck,
    ) -> None:
        self._popen = popen
        self._locate = locate
        self._guard = threading.Lock()
        self._relays: dict[str, _Relay] = {}
        self._next_port = int(os.environ.get(_BASE_PORT_ENV, "").strip() or str(_DEFAULT_BASE_PORT))
        self._free_ports: list[int] = []
        self._tsp_path: str | None = None
        self._tsp_resolved = False
        self._unavailable_logged = False

    # -- availability ---------------------------------------------------------

    def _tsp(self) -> str | None:
        if not self._tsp_resolved:
            status = self._locate()
            self._tsp_path = (
                getattr(status, "path", None) if getattr(status, "installed", False) else None
            )
            self._tsp_resolved = True
        return self._tsp_path

    def _active(self) -> bool:
        mode = relay_mode()
        if mode == "off":
            return False
        tsp = self._tsp()
        if tsp is None:
            if not self._unavailable_logged:
                self._unavailable_logged = True
                if mode == "on":
                    _LOG.error(
                        "CIVICCAST_TS_RELAY=on but TSDuck (tsp) is not available — "
                        "udp-ts output falls back to direct encoder output, so "
                        "encoder relaunches WILL reset the TS session at the "
                        "headend (#151). Install TSDuck or set CIVICCAST_TSDUCK_PATH."
                    )
                else:
                    _LOG.warning(
                        "TS relay auto mode: TSDuck (tsp) not found — udp-ts "
                        "output is direct from the encoder; relaunches reset the "
                        "TS session at the headend (#151). Install TSDuck to "
                        "enable seamless splices."
                    )
            return False
        return True

    # -- the public seam -------------------------------------------------------

    def apply(self, config: EgressConfig) -> EgressConfig:
        """Return a config whose udp-ts sinks point at channel-lifetime relays.

        Idempotent per (channel, destination): the same relay (and its pinned
        ports) is reused across every encoder relaunch — that is the whole
        point. Non-udp-ts sinks and non-relay deployments pass through
        untouched (the returned object is the SAME config instance then).
        """
        if not self._active():
            return config
        if not any(sink.kind == "udp-ts" for sink in config.sinks):
            return config
        new_sinks: list[EgressSinkSpec] = []
        changed = False
        for sink in config.sinks:
            if sink.kind != "udp-ts":
                new_sinks.append(sink)
                continue
            local_uri = self._ensure_relay(config.channel_id, sink)
            if local_uri is None:
                new_sinks.append(sink)
                continue
            changed = True
            new_sinks.append(sink.model_copy(update={"uri": local_uri}))
        if not changed:
            return config
        return config.model_copy(update={"sinks": new_sinks})

    def _ensure_relay(self, channel_id: str, sink: EgressSinkSpec) -> str | None:
        parsed = urlsplit(sink.uri)
        if not parsed.hostname or not parsed.port:
            _LOG.warning(
                "TS relay skipped for %s: udp-ts uri %r has no host:port.",
                channel_id,
                sink.uri,
            )
            return None
        key = f"{channel_id}|{parsed.hostname}:{parsed.port}"
        with self._guard:
            relay = self._relays.get(key)
            if relay is not None and relay.process.poll() is None:
                return self._local_uri(relay.listen_port, parsed.query)
            if relay is not None:
                listen_port = relay.listen_port
            elif self._free_ports:
                listen_port = self._free_ports.pop()  # reuse a stopped channel's port
            else:
                listen_port = self._next_port
                self._next_port += 1
            tsp = self._tsp()
            assert tsp is not None  # _active() gated
            args = build_tsp_relay_args(
                tsp,
                listen_port=listen_port,
                dest_host=parsed.hostname,
                dest_port=parsed.port,
                local_out_port=listen_port + _LOCAL_PORT_OFFSET,
            )
            try:
                # S603: fixed argv from the resolved tsp binary + validated URI parts.
                process = self._popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                _LOG.exception(
                    "TS relay failed to start for %s -> %s; udp-ts falls back to "
                    "direct encoder output.",
                    channel_id,
                    sink.uri,
                )
                return None
            self._relays[key] = _Relay(listen_port=listen_port, dest=sink.uri, process=process)
            _LOG.info(
                "TS relay up for %s: 127.0.0.1:%d -> %s (pinned source port %d).",
                channel_id,
                listen_port,
                f"{parsed.hostname}:{parsed.port}",
                listen_port + _LOCAL_PORT_OFFSET,
            )
            return self._local_uri(listen_port, parsed.query)

    @staticmethod
    def _local_uri(listen_port: int, query: str) -> str:
        base = f"udp://127.0.0.1:{listen_port}"
        return f"{base}?{query}" if query else base

    def stop_channel(self, channel_id: str) -> None:
        """Tear down a channel's relays (channel stop, not encoder relaunch)."""
        with self._guard:
            for key in [k for k in self._relays if k.startswith(f"{channel_id}|")]:
                relay = self._relays.pop(key)
                relay.process.terminate()
                self._free_ports.append(relay.listen_port)

    def stop_all(self) -> None:
        with self._guard:
            for relay in self._relays.values():
                relay.process.terminate()
            self._relays.clear()
