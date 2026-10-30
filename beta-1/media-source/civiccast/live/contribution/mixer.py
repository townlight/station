# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Compose admitted remote guest feeds into one broadcast-ready MPEG-TS feed."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from math import ceil, sqrt
from urllib.parse import urlsplit

from civiccast.stream._ffmpeg import FfmpegProcessHandle, start_ffmpeg


@dataclass(frozen=True)
class GuestFeed:
    endpoint: str
    muted: bool = False
    has_video: bool = True
    has_audio: bool = True


@dataclass
class GuestMixHandle:
    process: FfmpegProcessHandle
    errors: deque[str]
    drain_thread: threading.Thread

    def poll(self) -> int | None:
        return self.process.poll()

    def error_tail(self) -> str:
        return " | ".join(self.errors)[-1200:]

    def terminate(self) -> int | None:
        result = self.process.terminate()
        self.drain_thread.join(timeout=2)
        if self.process.stderr_pipe is not None:
            self.process.stderr_pipe.close()
        return result


def probe_guest_feed(endpoint: str, *, muted: bool = False) -> GuestFeed:
    """Identify available tracks before building a live mix."""
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
        raise ValueError("guest feed must be an RTSP endpoint")
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise RuntimeError("the bundled FFprobe runtime is unavailable")
    result = subprocess.run(  # noqa: S603 -- fixed argv, never shell
        [
            ffprobe,
            "-v",
            "error",
            "-rtsp_transport",
            "tcp",
            "-analyzeduration",
            "500000",
            "-probesize",
            "65536",
            "-timeout",
            "8000000",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "json",
            endpoint,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("guest media is not available from the relay")
    streams = json.loads(result.stdout).get("streams", [])
    tracks = {stream.get("codec_type") for stream in streams}
    if not tracks.intersection({"video", "audio"}):
        raise RuntimeError("guest media has no playable tracks")
    return GuestFeed(
        endpoint=endpoint,
        muted=muted,
        has_video="video" in tracks,
        has_audio="audio" in tracks,
    )


def start_guest_mix(feeds: list[GuestFeed], output_uri: str) -> GuestMixHandle:
    """Start the native-pack FFmpeg child under CivicCast's process containment."""
    process = start_ffmpeg(build_guest_mix_args(feeds, output_uri), stderr_pipe=True)
    errors: deque[str] = deque(maxlen=20)

    def drain() -> None:
        if process.stderr_pipe is not None:
            for line in process.stderr_pipe:
                errors.append(line.strip())

    thread = threading.Thread(target=drain, name="guest-mix-stderr", daemon=True)
    thread.start()
    return GuestMixHandle(process, errors, thread)


def wait_for_guest_mix(
    handle: GuestMixHandle, read_uri: str, *, timeout_seconds: float = 25.0
) -> None:
    """Wait until the relay serves both mixed tracks or the mixer exits."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise RuntimeError("the bundled FFprobe runtime is unavailable")
    deadline = time.monotonic() + timeout_seconds
    # FFmpeg opens and analyzes each live guest before it publishes. Probing
    # an absent relay path on every tick creates unnecessary SRT handshakes.
    first_probe_at = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        if handle.poll() is not None:
            handle.drain_thread.join(timeout=0.5)
            raise RuntimeError(
                "guest composition process exited before media was ready: " + handle.error_tail()
            )
        if time.monotonic() < first_probe_at:
            time.sleep(0.5)
            continue
        try:
            result = subprocess.run(  # noqa: S603 -- fixed argv, never shell
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-analyzeduration",
                    "500000",
                    "-probesize",
                    "65536",
                    "-timeout",
                    "5000000",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "json",
                    read_uri,
                ],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            if result.returncode == 0:
                streams = json.loads(result.stdout).get("streams", [])
                tracks = {stream.get("codec_type") for stream in streams}
                if {"video", "audio"} <= tracks:
                    return
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        time.sleep(1.0)
    raise RuntimeError(
        "guest composition did not produce video and audio in time: " + handle.error_tail()
    )


def build_guest_mix_args(
    feeds: list[GuestFeed],
    output_uri: str,
    *,
    width: int = 1280,
    height: int = 720,
    fps: int = 30,
) -> list[str]:
    """Build FFmpeg arguments for a tiled guest picture and mixed guest audio.

    The source codec can be H.264 or VP8 with Opus/AAC audio. The resulting
    MPEG-2/AAC transport stream uses only encoders in CivicCast's native pack.
    MediaMTX accepts it over SRT and serves the same path to CivicCast egress.
    """
    if not feeds:
        raise ValueError("a guest composition needs at least one feed")
    if width < 2 or height < 2 or fps < 1:
        raise ValueError("invalid guest composition dimensions or frame rate")
    if urlsplit(output_uri).scheme != "srt":
        raise ValueError("guest composition output must be SRT")

    columns = ceil(sqrt(len(feeds)))
    rows = ceil(len(feeds) / columns)
    cell_width = (width // columns) // 2 * 2
    cell_height = (height // rows) // 2 * 2
    if cell_width < 2 or cell_height < 2:
        raise ValueError("too many guest feeds for the chosen output dimensions")

    args = ["-hide_banner", "-loglevel", "error", "-nostdin"]
    filters: list[str] = []
    audible = max(1, sum(feed.has_audio and not feed.muted for feed in feeds))
    gain = 1 / sqrt(audible)
    for index, feed in enumerate(feeds):
        parsed = urlsplit(feed.endpoint)
        if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
            raise ValueError("guest feed must be an RTSP endpoint")
        if not feed.has_video and not feed.has_audio:
            raise ValueError("guest feed contains neither video nor audio")
        args.extend(
            [
                "-rtsp_transport",
                "tcp",
                "-analyzeduration",
                "500000",
                "-probesize",
                "65536",
                "-timeout",
                "10000000",
                "-i",
                feed.endpoint,
            ]
        )
        picture = (
            f"[{index}:v]scale={cell_width}:{cell_height}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={cell_width}:{cell_height}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={fps},setsar=1,setpts=PTS-STARTPTS[v{index}]"
            if feed.has_video
            else f"color=c=black:s={cell_width}x{cell_height}:r={fps}[v{index}]"
        )
        filters.append(picture)
        audio_source = (
            f"[{index}:a]aresample=48000:async=1:first_pts=0"
            if feed.has_audio
            else "anullsrc=channel_layout=stereo:sample_rate=48000"
        )
        filters.append(f"{audio_source},volume={0 if feed.muted else gain:.6f}[a{index}]")

    if len(feeds) == 1:
        filters.append("[v0]format=yuv420p[vout]")
        filters.append("[a0]anull[aout]")
    else:
        layout = "|".join(
            f"{index % columns * cell_width}_{index // columns * cell_height}"
            for index in range(len(feeds))
        )
        videos = "".join(f"[v{index}]" for index in range(len(feeds)))
        audios = "".join(f"[a{index}]" for index in range(len(feeds)))
        filters.append(
            f"{videos}xstack=inputs={len(feeds)}:layout={layout}:fill=black,format=yuv420p[vout]"
        )
        filters.append(
            f"{audios}amix=inputs={len(feeds)}:duration=longest:normalize=0,"
            "alimiter=limit=0.95[aout]"
        )

    args.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-r",
            str(fps),
            "-c:v",
            "mpeg2video",
            "-b:v",
            "5M",
            "-g",
            str(fps),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-f",
            "mpegts",
            "-shortest",
            output_uri,
        ]
    )
    return args
