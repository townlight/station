# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Supervised BYO-SDI relay (issue #117, option c).

The station brings the SDI side themselves — a DeckLink card with
Blackmagic's Desktop Video driver and an FFmpeg build compiled with
``--enable-decklink`` (the Blackmagic SDK license is theirs to accept;
mainline FFmpeg builds do not ship it). When ``CIVICCAST_SDI_FFMPEG``
points at that build and the channel has an SDI output device configured,
this supervisor feeds the channel's transport-stream output to the card:

    <byo-ffmpeg> -i udp://127.0.0.1:<port> ... -pix_fmt uyvy422 \
        -c:a pcm_s16le -ar 48000 -ac 2 -f decklink "<device>"

Same side-relay architecture as the NDI relay (#116) and for the same
reasons: the BYO binary is not the bundled encoder's, SDI needs a raw
uyvy422 re-encode leg (with embedded PCM audio), and a dying SDI feed must
never take the cable channel off air. Stations without a custom FFmpeg
build can use the OBS bridge instead — see the runbook.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from civiccast.egress.models import clean_relay_identifier
from civiccast.stream._ffmpeg import FfmpegResult

SdiRelayMode = Literal["inline", "off"]
SdiRelayState = Literal["off", "blocked", "running", "restarting", "stopped"]
SdiReadinessStatus = Literal["ok", "decklink_muxer_missing", "ffmpeg_unavailable"]

FfmpegRunner = Callable[[list[str]], FfmpegResult]

_RESTART_BACKOFF_SECONDS = (5.0, 15.0, 60.0)
_BYO_BINARY_HINT = (
    "Set CIVICCAST_SDI_FFMPEG to the station's DeckLink-capable FFmpeg "
    "build. CivicCast's bundled ffmpeg cannot include the decklink muxer "
    "(Blackmagic SDK license); see the SDI runbook section — or use the "
    "OBS bridge documented there instead."
)
_MUXER_HINT = (
    "Install or build an FFmpeg binary with --enable-decklink "
    "(Blackmagic Desktop Video SDK), then retry. Stations without a custom "
    "build can use the OBS bridge documented in the SDI runbook section."
)

DEFAULT_SDI_FRAMERATE = "30000/1001"
DEFAULT_SDI_VIDEO_SIZE = "1920x1080"


@dataclass(frozen=True)
class SdiRelaySettings:
    """Environment-driven relay settings."""

    mode: SdiRelayMode = "inline"
    ffmpeg_path: str | None = None

    @classmethod
    def from_env(cls) -> SdiRelaySettings:
        raw_mode = os.environ.get("CIVICCAST_SDI_RELAY", "inline").strip().lower()
        if raw_mode not in ("inline", "off"):
            # Audit ENG-012: a typo used to silently ENABLE supervision.
            raise ValueError(f"CIVICCAST_SDI_RELAY must be 'inline' or 'off', got {raw_mode!r}.")
        mode: SdiRelayMode = "off" if raw_mode == "off" else "inline"
        ffmpeg_path = os.environ.get("CIVICCAST_SDI_FFMPEG") or None
        return cls(mode=mode, ffmpeg_path=ffmpeg_path)


class SdiReadiness(BaseModel):
    """Whether the BYO ffmpeg build can drive a DeckLink output."""

    model_config = ConfigDict(extra="forbid")

    status: SdiReadinessStatus
    ffmpeg_detected: bool
    muxer_present: bool
    next_step: str = ""


class SdiRelayStatus(BaseModel):
    """Operator-facing relay state for one channel."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    device: str
    state: SdiRelayState
    pid: int | None = None
    restarts: int = 0
    last_error: str | None = None
    next_step: str = ""


# Latest relay status per channel — runtime-only, same posture as the NDI
# relay registry.
_RELAY_STATUSES: dict[str, SdiRelayStatus] = {}


def set_relay_status(status: SdiRelayStatus) -> None:
    _RELAY_STATUSES[status.channel_id] = status


def get_relay_status(channel_id: str) -> SdiRelayStatus | None:
    return _RELAY_STATUSES.get(channel_id)


def drop_relay_status(channel_id: str) -> None:
    _RELAY_STATUSES.pop(channel_id, None)


def all_relay_statuses() -> list[SdiRelayStatus]:
    # Audit ENG-007: items() materialized under the GIL is an atomic
    # snapshot; key-then-index could KeyError against the automation thread.
    return [status for _, status in sorted(_RELAY_STATUSES.items())]


def clear_relay_statuses() -> None:
    _RELAY_STATUSES.clear()


def _default_ffmpeg_runner_for(ffmpeg_path: str) -> FfmpegRunner:
    def _runner(args: list[str]) -> FfmpegResult:
        completed = subprocess.run(  # noqa: S603 -- fixed args, no shell
            [ffmpeg_path, *args],
            capture_output=True,
            text=True,
            timeout=5,  # audit ENG-008: a hung BYO binary must not stall the tick
            check=False,
        )
        return FfmpegResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    return _runner


def check_sdi_runtime(
    ffmpeg_path: str,
    *,
    ffmpeg_runner: FfmpegRunner | None = None,
) -> SdiReadiness:
    """Return whether the BYO ffmpeg build has the decklink muxer."""

    runner = ffmpeg_runner or _default_ffmpeg_runner_for(ffmpeg_path)
    try:
        result = runner(["-hide_banner", "-muxers"])
    except Exception:
        return SdiReadiness(
            status="ffmpeg_unavailable",
            ffmpeg_detected=False,
            muxer_present=False,
            next_step=_BYO_BINARY_HINT,
        )
    if result.returncode != 0:
        return SdiReadiness(
            status="ffmpeg_unavailable",
            ffmpeg_detected=True,
            muxer_present=False,
            next_step=(
                "The configured CIVICCAST_SDI_FFMPEG binary could not list "
                "muxers; repair or replace it, then retry."
            ),
        )
    output = f"{result.stdout}\n{result.stderr}"
    muxer_present = bool(re.search(r"^\s*E\s+decklink\b", output, flags=re.MULTILINE))
    if not muxer_present:
        return SdiReadiness(
            status="decklink_muxer_missing",
            ffmpeg_detected=True,
            muxer_present=False,
            next_step=_MUXER_HINT,
        )
    return SdiReadiness(status="ok", ffmpeg_detected=True, muxer_present=True)


# Audit ENG-008: the readiness probe spawns a subprocess; uncached it runs
# inside the automation tick on every spawn attempt and a hung BYO binary
# stalls every channel. Result changes only when the operator swaps the
# binary, so a TTL cache is safe.
_READINESS_CACHE: dict[str, tuple[SdiReadiness, float]] = {}
_READINESS_TTL_SECONDS = 300.0


def clear_readiness_cache() -> None:
    _READINESS_CACHE.clear()


def cached_check_sdi_runtime(
    ffmpeg_path: str,
    *,
    ffmpeg_runner: FfmpegRunner | None = None,
    monotonic: Callable[[], float] | None = None,
) -> SdiReadiness:
    """check_sdi_runtime with a per-binary TTL cache (audit ENG-008)."""

    import time

    now = (monotonic or time.monotonic)()
    cached = _READINESS_CACHE.get(ffmpeg_path)
    if cached is not None and now < cached[1]:
        return cached[0]
    readiness = check_sdi_runtime(ffmpeg_path, ffmpeg_runner=ffmpeg_runner)
    _READINESS_CACHE[ffmpeg_path] = (readiness, now + _READINESS_TTL_SECONDS)
    return readiness


def _clean_device_name(value: str) -> str:
    # Defense in depth: EgressConfig already enforces the same rules at the
    # API boundary via the SHARED clean_relay_identifier (audit Critical) -
    # this runtime check stays for non-config callers and pre-fix rows. The
    # shared rule rejects ALL C0 controls (the old local check missed \x01).
    try:
        cleaned = clean_relay_identifier(value, field_name="SDI output device")
    except ValueError as exc:
        raise ValueError(
            f"{exc} Use the exact name from `ffmpeg -sinks decklink` "
            "(e.g. 'DeckLink Mini Monitor 4K')."
        ) from exc
    assert cleaned is not None  # value is a str, never None here
    return re.sub(r"\s+", " ", cleaned)


def build_sdi_relay_args(
    *,
    source_uri: str,
    device: str,
    video_size: str = DEFAULT_SDI_VIDEO_SIZE,
    framerate: str = DEFAULT_SDI_FRAMERATE,
) -> list[str]:
    """FFmpeg args that feed the channel's TS output to a DeckLink card.

    SDI embeds audio, so unlike the NDI relay this keeps the channel audio
    as 48 kHz stereo PCM (what decklink output consumes).
    """

    clean_device = _clean_device_name(device)
    return [
        "-i",
        source_uri,
        "-vf",
        f"scale={video_size},fps={framerate}",
        "-pix_fmt",
        "uyvy422",
        "-c:a",
        "pcm_s16le",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-f",
        "decklink",
        clean_device,
    ]


def _default_process_starter(args: list[str]) -> Any:
    # Audit ENG-013: capture stderr so an exited relay can say why it died
    # (the encoder path has the same posture via _stderr_logs).
    stderr_file = tempfile.NamedTemporaryFile(  # noqa: SIM115 -- handle must outlive this scope; the child writes to it
        prefix="civiccast-sdi-relay-", suffix=".stderr.log", delete=False
    )
    process = subprocess.Popen(  # noqa: S603 -- fixed args, never shell
        args,
        stdout=subprocess.DEVNULL,
        stderr=stderr_file,
    )
    stderr_file.close()
    process.civiccast_stderr_path = stderr_file.name  # type: ignore[attr-defined]
    return process


def _read_stderr_tail(process: Any, *, max_chars: int = 400) -> str | None:
    """Last chunk of the child's captured stderr, if a capture exists."""

    path = getattr(process, "civiccast_stderr_path", None)
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    tail = text.strip()[-max_chars:]
    return tail or None


class SdiRelaySupervisor:
    """Poll-driven lifecycle for one channel's SDI relay process.

    Same shape as :class:`civiccast.egress.ndi_relay.NdiRelaySupervisor`:
    ``ensure_running()`` rides the channel-automation pass, restarts back
    off (5/15/60s), and readiness failures are honest ``blocked`` states.
    """

    def __init__(
        self,
        *,
        channel_id: str,
        device: str,
        source_uri: str,
        settings: SdiRelaySettings,
        readiness_checker: Callable[[str], SdiReadiness] | None = None,
        process_starter: Callable[[list[str]], Any] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        import time

        self.channel_id = channel_id
        self.device = device
        self.source_uri = source_uri
        self._settings = settings
        self._check_readiness = readiness_checker or cached_check_sdi_runtime
        self._start_process = process_starter or _default_process_starter
        self._monotonic = monotonic or time.monotonic
        self._process: Any | None = None
        self._restarts = 0
        self._next_start_at: float | None = None
        self._stopped = False
        self._status = SdiRelayStatus(channel_id=channel_id, device=device, state="off")

    def status(self) -> SdiRelayStatus:
        return self._status

    def ensure_running(self) -> SdiRelayStatus:
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
            if self._next_start_at is None:
                backoff = _RESTART_BACKOFF_SECONDS[
                    min(self._restarts, len(_RESTART_BACKOFF_SECONDS) - 1)
                ]
                self._next_start_at = self._monotonic() + backoff
            if self._monotonic() < self._next_start_at:
                # Audit ENG-013: say WHY it died, not just that it did.
                tail = _read_stderr_tail(self._process)
                detail = "SDI relay process exited; restart pending."
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
                    "last_error": f"SDI readiness: {readiness.status}",
                    "next_step": readiness.next_step,
                }
            )
            return self._status

        try:
            args = [
                self._settings.ffmpeg_path,
                *build_sdi_relay_args(source_uri=self.source_uri, device=self.device),
            ]
            self._process = self._start_process(args)
        except (ValueError, OSError) as exc:
            # Audit DOC-002: a bad device name (or an unspawnable binary) is
            # an honest, stable `blocked` - never a raise out of the
            # automation pass and never a restart strobe.
            self._status = self._status.model_copy(
                update={
                    "state": "blocked",
                    "pid": None,
                    "last_error": "SDI relay could not start.",
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
