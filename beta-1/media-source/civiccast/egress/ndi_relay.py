# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Supervised BYO-NDI relay (issue #116, option c).

The station brings the NDI side themselves — the NDI runtime and an
NDI-capable FFmpeg build (mainline FFmpeg dropped the NDI muxer in 2019
after the NewTek license dispute, so CivicCast's bundled ffmpeg cannot
ship it). When `CIVICCAST_NDI_FFMPEG` points at that build and the channel
has an NDI name configured, this supervisor publishes the channel's
existing transport-stream output as an NDI source:

    <byo-ffmpeg> -i udp://127.0.0.1:<port> ... -pix_fmt uyvy422 -f libndi_newtek "<name>"

A separate supervised process on purpose: the BYO binary is not the
bundled encoder's binary, NDI needs a raw uyvy422 re-encode leg, and a
dying NDI relay must never take the cable channel off air. Everything the
channel plays (programs, slate, bulletins, join-in-progress) flows through
for free because the relay consumes the channel's output.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from civiccast.cable.ndi import (
    DEFAULT_NDI_FRAMERATE,
    DEFAULT_NDI_VIDEO_SIZE,
    NdiOutputError,
    NdiReadinessResult,
    _clean_ndi_name,
    check_ndi_runtime,
)
from civiccast.stream._ffmpeg import FfmpegResult

NdiRelayMode = Literal["inline", "off"]
NdiRelayState = Literal["off", "blocked", "running", "restarting", "stopped"]

_RESTART_BACKOFF_SECONDS = (5.0, 15.0, 60.0)
_BYO_BINARY_HINT = (
    "Set CIVICCAST_NDI_FFMPEG to the station's NDI-capable FFmpeg build. "
    "CivicCast's bundled ffmpeg cannot include the NDI muxer (NewTek "
    "license); see the NDI runbook section for where to get one."
)


@dataclass(frozen=True)
class NdiRelaySettings:
    """Environment-driven relay settings."""

    mode: NdiRelayMode = "inline"
    ffmpeg_path: str | None = None

    @classmethod
    def from_env(cls) -> NdiRelaySettings:
        raw_mode = os.environ.get("CIVICCAST_NDI_RELAY", "inline").strip().lower()
        if raw_mode not in ("inline", "off"):
            # Audit ENG-012: a typo used to silently ENABLE supervision.
            raise ValueError(f"CIVICCAST_NDI_RELAY must be 'inline' or 'off', got {raw_mode!r}.")
        mode: NdiRelayMode = "off" if raw_mode == "off" else "inline"
        ffmpeg_path = os.environ.get("CIVICCAST_NDI_FFMPEG") or None
        return cls(mode=mode, ffmpeg_path=ffmpeg_path)


class NdiRelayStatus(BaseModel):
    """Operator-facing relay state for one channel."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    ndi_name: str
    state: NdiRelayState
    pid: int | None = None
    restarts: int = 0
    last_error: str | None = None
    next_step: str = ""


# Latest relay status per channel, written by the automation pass and read
# by the staff API. Runtime-only on purpose — relay state is a live signal,
# not durable data.
_RELAY_STATUSES: dict[str, NdiRelayStatus] = {}


def set_relay_status(status: NdiRelayStatus) -> None:
    _RELAY_STATUSES[status.channel_id] = status


def get_relay_status(channel_id: str) -> NdiRelayStatus | None:
    return _RELAY_STATUSES.get(channel_id)


def drop_relay_status(channel_id: str) -> None:
    _RELAY_STATUSES.pop(channel_id, None)


def all_relay_statuses() -> list[NdiRelayStatus]:
    # Audit ENG-007: snapshot via items() - see sdi_relay.all_relay_statuses.
    return [status for _, status in sorted(_RELAY_STATUSES.items())]


def clear_relay_statuses() -> None:
    _RELAY_STATUSES.clear()


def build_ndi_relay_args(
    *,
    source_uri: str,
    ndi_name: str,
    video_size: str = DEFAULT_NDI_VIDEO_SIZE,
    framerate: str = DEFAULT_NDI_FRAMERATE,
    muxer: str = "libndi_newtek",
) -> list[str]:
    """FFmpeg args that republish the channel's TS output as an NDI source."""

    try:
        clean_name = _clean_ndi_name(ndi_name)
    except NdiOutputError as exc:
        raise ValueError(str(exc)) from exc
    return [
        "-i",
        source_uri,
        "-vf",
        f"scale={video_size},fps={framerate}",
        "-pix_fmt",
        "uyvy422",
        "-an",
        "-f",
        muxer,
        clean_name,
    ]


def _uncached_check_ndi_runtime(ffmpeg_path: str) -> NdiReadinessResult:
    def _runner(args: list[str]) -> FfmpegResult:
        completed = subprocess.run(  # noqa: S603 -- fixed args, no shell
            [ffmpeg_path, *args],
            capture_output=True,
            text=True,
            timeout=5,  # audit ENG-008 (SDI parity): a hung BYO binary must not stall the tick
            check=False,
        )
        return FfmpegResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    return check_ndi_runtime(ffmpeg_runner=_runner)


# S9-6 (parity with SDI audit ENG-008): the NDI readiness probe spawns a subprocess.
# Uncached it re-ran inside the automation tick on every spawn attempt — so a blocked
# NDI relay (no NDI runtime) churned ffmpeg every ~2s for the channel's whole life. The
# result changes only when the operator swaps the binary, so a per-binary TTL cache is
# safe. (SDI already had this; NDI was the missed half.)
_READINESS_CACHE: dict[str, tuple[NdiReadinessResult, float]] = {}
_READINESS_TTL_SECONDS = 300.0


def clear_readiness_cache() -> None:
    _READINESS_CACHE.clear()


def cached_check_ndi_runtime(
    ffmpeg_path: str,
    *,
    monotonic: Callable[[], float] | None = None,
    probe: Callable[[str], NdiReadinessResult] | None = None,
) -> NdiReadinessResult:
    """``_uncached_check_ndi_runtime`` with a per-binary TTL cache (S9-6 / ENG-008).
    ``probe`` overrides the subprocess probe for deterministic tests."""
    import time

    now = (monotonic or time.monotonic)()
    cached = _READINESS_CACHE.get(ffmpeg_path)
    if cached is not None and now < cached[1]:
        return cached[0]
    readiness = (probe or _uncached_check_ndi_runtime)(ffmpeg_path)
    _READINESS_CACHE[ffmpeg_path] = (readiness, now + _READINESS_TTL_SECONDS)
    return readiness


def _default_process_starter(args: list[str]) -> Any:
    # Audit ENG-013: capture stderr so an exited relay can say why it died.
    stderr_file = tempfile.NamedTemporaryFile(  # noqa: SIM115 -- handle must outlive this scope; the child writes to it
        prefix="civiccast-ndi-relay-", suffix=".stderr.log", delete=False
    )
    process = subprocess.Popen(  # noqa: S603 -- fixed args, never shell
        args,
        stdout=subprocess.DEVNULL,
        stderr=stderr_file,
    )
    stderr_file.close()
    process.civiccast_stderr_path = stderr_file.name  # type: ignore[attr-defined]
    return process


class NdiRelaySupervisor:
    """Poll-driven lifecycle for one channel's NDI relay process.

    ``ensure_running()`` is called from the channel-automation pass — the
    same cadence that keeps the channel itself alive. Restarts back off
    (5s / 15s / 60s) so a broken BYO binary cannot crash-loop hot.
    """

    def __init__(
        self,
        *,
        channel_id: str,
        ndi_name: str,
        source_uri: str,
        settings: NdiRelaySettings,
        readiness_checker: Callable[[str], NdiReadinessResult] | None = None,
        process_starter: Callable[[list[str]], Any] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        import time

        self.channel_id = channel_id
        self.ndi_name = ndi_name
        self.source_uri = source_uri
        self._settings = settings
        self._check_readiness = readiness_checker or cached_check_ndi_runtime
        self._start_process = process_starter or _default_process_starter
        self._monotonic = monotonic or time.monotonic
        self._process: Any | None = None
        self._restarts = 0
        self._next_start_at: float | None = None
        self._stopped = False
        self._status = NdiRelayStatus(channel_id=channel_id, ndi_name=ndi_name, state="off")

    def status(self) -> NdiRelayStatus:
        return self._status

    def ensure_running(self) -> NdiRelayStatus:
        if self._settings.mode == "off" or self._stopped:
            self._status = self._status.model_copy(
                update={"state": "stopped" if self._stopped else "off", "pid": None}
            )
            return self._status

        if self._settings.ffmpeg_path is None:
            self._status = self._status.model_copy(
                update={"state": "blocked", "pid": None, "next_step": _BYO_BINARY_HINT}
            )
            return self._status

        if self._process is not None and self._process.poll() is None:
            self._status = self._status.model_copy(
                update={
                    "state": "running",
                    "pid": self._process.pid,
                    "restarts": self._restarts,
                }
            )
            return self._status

        if self._process is not None:
            # The relay died; schedule a backed-off restart.
            if self._next_start_at is None:
                backoff = _RESTART_BACKOFF_SECONDS[
                    min(self._restarts, len(_RESTART_BACKOFF_SECONDS) - 1)
                ]
                self._next_start_at = self._monotonic() + backoff
            if self._monotonic() < self._next_start_at:
                # Audit ENG-013: say WHY it died, not just that it did.
                from civiccast.egress.sdi_relay import _read_stderr_tail

                tail = _read_stderr_tail(self._process)
                detail = "NDI relay process exited; restart pending."
                if tail:
                    detail = f"{detail} ffmpeg said: {tail}"
                self._status = self._status.model_copy(
                    update={
                        "state": "restarting",
                        "pid": None,
                        "restarts": self._restarts,
                        "last_error": detail,
                    }
                )
                return self._status
            self._process = None
            self._next_start_at = None
            self._restarts += 1

        readiness = self._check_readiness(self._settings.ffmpeg_path)
        if readiness.status != "ok":
            self._status = self._status.model_copy(
                update={
                    "state": "blocked",
                    "pid": None,
                    "last_error": f"NDI readiness: {readiness.status}",
                    "next_step": readiness.next_step,
                }
            )
            return self._status

        try:
            args = [
                self._settings.ffmpeg_path,
                *build_ndi_relay_args(
                    source_uri=self.source_uri,
                    ndi_name=self.ndi_name,
                    muxer=readiness.supported_muxer or "libndi_newtek",
                ),
            ]
            self._process = self._start_process(args)
        except (ValueError, OSError) as exc:
            # Audit DOC-002 twin: bad name / unspawnable binary = honest,
            # stable blocked - never a raise out of the automation pass.
            self._status = self._status.model_copy(
                update={
                    "state": "blocked",
                    "pid": None,
                    "last_error": "NDI relay could not start.",
                    "next_step": str(exc),
                }
            )
            return self._status
        self._status = self._status.model_copy(
            update={
                "state": "running",
                "pid": self._process.pid,
                "restarts": self._restarts,
                "last_error": None,
                "next_step": "",
            }
        )
        return self._status

    def stop(self) -> None:
        self._stopped = True
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
        self._process = None
        self._status = self._status.model_copy(update={"state": "stopped", "pid": None})
