# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""ffprobe ingest pipeline for civiccast.schedule.

Sprint 0.3 — asset upload + ffprobe ingest. All ffprobe subprocess
calls in the schedule module go through ``run_ffprobe`` here. The
``validate_ingest`` gate rejects files whose codec or container format
are outside the supported set; callers translate ``UnsupportedFormatError``
to HTTP 422.

Supported set is intentionally narrow for v0.3; expansion is a Minor
finding queued in ``next-cleanup.md``. The gate fails CLOSED — if ffprobe
returns no video stream, the asset is rejected (spec §4.1 UX non-negotiable:
clear error messages on every failure state).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from civiccast.stream._ffmpeg import FfmpegResult, run_ffmpeg

__all__ = [
    "FfmpegNotFoundError",
    "FfprobeError",
    "FfprobeNotFoundError",
    "FfprobeResult",
    "UnsupportedFormatError",
    "check_ffprobe",
    "extract_thumbnail",
    "hash_file",
    "run_ffprobe",
    "validate_ingest",
]

_FFPROBE_EXECUTABLE = "ffprobe"
_FFMPEG_EXECUTABLE = "ffmpeg"
_FFPROBE_MIN_VERSION = (4, 4)
_THUMBNAIL_TIMEOUT_SECONDS = 30
_HASH_CHUNK_BYTES = 1024 * 1024

# Codecs and format tokens accepted by the validation gate.
# Expand at v0.4+ with operator feedback; current set covers the
# formats operators encounter day-to-day on civic meeting recordings.
SUPPORTED_VIDEO_CODECS: frozenset[str] = frozenset({"h264", "hevc", "av1", "vp9", "vp8", "prores"})
SUPPORTED_AUDIO_CODECS: frozenset[str] = frozenset(
    {"aac", "mp3", "ac3", "eac3", "opus", "flac", "vorbis", "pcm_s16le", "pcm_s24le"}
)
# ffprobe format_name is a comma-separated list of matching muxers.
# We accept if ANY of these tokens appears in the format_name string.
SUPPORTED_FORMAT_TOKENS: frozenset[str] = frozenset(
    {"mp4", "mov", "matroska", "webm", "avi", "mpegts"}
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FfprobeNotFoundError(RuntimeError):
    """ffprobe binary not found on PATH."""


class FfmpegNotFoundError(RuntimeError):
    """ffmpeg binary not found on PATH."""


class FfprobeError(RuntimeError):
    """ffprobe subprocess returned a non-zero exit code or unparseable output."""

    def __init__(self, message: str, returncode: int = -1, stderr: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class UnsupportedFormatError(ValueError):
    """Asset file uses a codec or container not in the supported set.

    Carries ``reason`` for operator-readable error messages (surfaced as
    HTTP 422 detail by the upload router).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FfprobeResult:
    """Parsed output of ``ffprobe -print_format json -show_streams -show_format``.

    All fields are nullable — callers must treat None as "ffprobe could not
    determine this value" rather than "the file lacks this property."
    ``raw`` carries the full parsed JSON dict for debugging / future fields.
    """

    duration_seconds: int | None
    codec_video: str | None
    codec_audio: str | None
    width_px: int | None
    height_px: int | None
    bitrate_bps: int | None
    format_name: str | None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_ffprobe(path: Path) -> FfprobeResult:
    """Run ffprobe on ``path`` and return a parsed :class:`FfprobeResult`.

    Raises :class:`FfprobeNotFoundError` if the binary is absent.
    Raises :class:`FfprobeError` on non-zero exit or JSON parse failure.
    """
    if shutil.which(_FFPROBE_EXECUTABLE) is None:
        raise FfprobeNotFoundError(
            "ffprobe not found on PATH. "
            "Install ffmpeg (which ships ffprobe) and verify with 'civiccast doctor'."
        )

    cmd = [
        _FFPROBE_EXECUTABLE,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]

    completed = subprocess.run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if completed.returncode != 0:
        raise FfprobeError(
            f"ffprobe exited with code {completed.returncode} on {path.name}",
            returncode=completed.returncode,
            stderr=completed.stderr,
        )

    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise FfprobeError(f"ffprobe output for {path.name} is not valid JSON") from exc

    return _parse_ffprobe_json(data)


def hash_file(path: Path) -> str:
    """Return the ``sha256:<hex>`` content digest of the file at ``path``.

    Streamed in fixed-size chunks so multi-gigabyte video files don't load
    into memory at once. Format matches ``civiccast.records`` (see
    ``records/rfc3161.py:_digest`` / ``records/timestamp.py:_digest``) so a
    digest computed here is directly comparable to any records-domain
    digest of the same bytes.
    """
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def extract_thumbnail(path: Path, dest: Path, *, at_seconds: float = 1.0) -> None:
    """Extract one representative frame from ``path`` as a JPEG at ``dest``.

    Uses the same subprocess conventions as :func:`run_ffprobe` (no shell,
    captured output, replace-on-decode-error). ``at_seconds`` seeks before
    decoding (fast, ffmpeg ``-ss`` before ``-i``) so a still near the start
    of long recordings doesn't require decoding the whole file. ffmpeg's
    fast seek does not clamp: seeking past the file's actual duration
    fails outright rather than landing on the last frame, so a clip
    shorter than ``at_seconds`` (or an off-by-a-hair mismatch between
    ffprobe's reported duration and ffmpeg's seek target) retries once at
    frame 0 before giving up.

    Raises :class:`FfmpegNotFoundError` if the binary is absent.
    Raises :class:`FfprobeError` if both the seek attempt and the frame-0
    retry fail (reusing :class:`FfprobeError`: same "a subprocess-based
    media tool failed" shape, no need for a sibling type).
    Bounded by ``_THUMBNAIL_TIMEOUT_SECONDS`` per attempt — ingest is a
    synchronous request path (no job queue exists in this codebase; see
    ``civiccast.platform.worker_runtime.ThreadSupervisor`` for the only
    background-work primitive, which is poll-loop shaped, not job-shaped),
    so a hung ffmpeg process must not hang the request forever.
    """
    if shutil.which(_FFMPEG_EXECUTABLE) is None:
        raise FfmpegNotFoundError(
            "ffmpeg not found on PATH. Install ffmpeg and verify with 'civiccast doctor'."
        )

    last: FfmpegResult | None = None
    for seek in (max(0.0, at_seconds), 0.0):
        # ADR 0007: the invocation goes through the civiccast.stream._ffmpeg
        # wrapper (run_ffmpeg prepends the binary + -y), not a direct subprocess
        # call, so command construction and the shell=False posture live in one
        # audited place. args are the post-binary arguments only.
        args = ["-ss", str(seek), "-i", str(path), "-frames:v", "1", "-q:v", "3", str(dest)]
        try:
            last = run_ffmpeg(args, timeout=_THUMBNAIL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise FfprobeError(
                f"thumbnail render timed out after {_THUMBNAIL_TIMEOUT_SECONDS}s on {path.name}"
            ) from exc

        if last.returncode == 0 and dest.exists():
            return
        if seek == 0.0:
            break  # already retried; fall through to the error below

    assert last is not None  # the loop always runs at least once
    raise FfprobeError(
        f"thumbnail render exited with code {last.returncode} on {path.name}",
        returncode=last.returncode,
        stderr=last.stderr,
    )


def validate_ingest(result: FfprobeResult) -> None:
    """Raise :class:`UnsupportedFormatError` if the asset is outside the accepted set.

    Gate fails CLOSED: no video stream → rejected. Unknown codec → rejected.
    This is the validation gate that surfaces as HTTP 422 in the upload router.
    """
    if result.codec_video is None:
        raise UnsupportedFormatError(
            "No video stream detected. CivicCast only accepts video files."
        )

    if result.codec_video not in SUPPORTED_VIDEO_CODECS:
        raise UnsupportedFormatError(
            f"Video codec '{result.codec_video}' is not supported. "
            f"Supported codecs: {', '.join(sorted(SUPPORTED_VIDEO_CODECS))}."
        )

    if result.codec_audio is not None and result.codec_audio not in SUPPORTED_AUDIO_CODECS:
        raise UnsupportedFormatError(
            f"Audio codec '{result.codec_audio}' is not supported. "
            f"Supported audio codecs: {', '.join(sorted(SUPPORTED_AUDIO_CODECS))}."
        )

    if result.format_name is None:
        raise UnsupportedFormatError(
            "Could not detect container format. Ensure the file is not corrupted."
        )

    format_tokens = {tok.strip() for tok in result.format_name.split(",")}
    if not format_tokens & SUPPORTED_FORMAT_TOKENS:
        raise UnsupportedFormatError(
            f"Container format '{result.format_name}' is not supported. "
            f"Supported containers: MP4, MOV, MKV/WebM, AVI, MPEG-TS."
        )


def check_ffprobe() -> tuple[str, bool] | None:
    """Return (version_string, is_supported) or None if ffprobe is not on PATH.

    Called by ``civiccast doctor`` to report ffprobe availability and version.
    Version string is the raw ffprobe version token; ``is_supported`` is True
    when the major.minor >= _FFPROBE_MIN_VERSION (4.4).
    """
    if shutil.which(_FFPROBE_EXECUTABLE) is None:
        return None

    completed = subprocess.run(  # noqa: S603
        [_FFPROBE_EXECUTABLE, "-version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        return None

    version_str = _parse_ffprobe_version(completed.stdout + "\n" + completed.stderr)
    if version_str is None:
        return "unknown", True

    return version_str, _version_is_supported(version_str)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_ffprobe_json(data: dict[str, Any]) -> FfprobeResult:
    """Extract typed fields from the ``ffprobe -print_format json`` dict."""
    streams: list[dict[str, Any]] = data.get("streams", [])
    fmt: dict[str, Any] = data.get("format", {})

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration_seconds: int | None = None
    raw_duration = fmt.get("duration") or (video_stream.get("duration") if video_stream else None)
    if raw_duration is not None:
        with suppress(ValueError, TypeError):
            duration_seconds = int(float(raw_duration))

    bitrate_bps: int | None = None
    raw_bitrate = fmt.get("bit_rate")
    if raw_bitrate is not None:
        with suppress(ValueError, TypeError):
            bitrate_bps = int(raw_bitrate)

    width_px: int | None = video_stream.get("width") if video_stream else None
    height_px: int | None = video_stream.get("height") if video_stream else None

    return FfprobeResult(
        duration_seconds=duration_seconds,
        codec_video=video_stream.get("codec_name") if video_stream else None,
        codec_audio=audio_stream.get("codec_name") if audio_stream else None,
        width_px=width_px,
        height_px=height_px,
        bitrate_bps=bitrate_bps,
        format_name=fmt.get("format_name"),
        raw=data,
    )


def _parse_ffprobe_version(output: str) -> str | None:
    """Extract the version token from 'ffprobe -version' output."""
    import re

    match = re.search(r"ffprobe version\s+(\S+)", output, re.IGNORECASE)
    return match.group(1) if match else None


def _version_is_supported(version_str: str) -> bool:
    """True if version_str >= _FFPROBE_MIN_VERSION. Unknown → True (warn, don't block)."""
    import re

    clean = re.sub(r"^[^\d]*", "", version_str)
    clean = re.split(r"[-+]", clean)[0]
    parts = clean.split(".")
    try:
        major = int(parts[0]) if len(parts) > 0 else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        return (major, minor) >= _FFPROBE_MIN_VERSION
    except ValueError:
        return True
