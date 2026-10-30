# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Slate variant generator.

Produces a short looping HLS stream (the 5th lowest-bandwidth variant in
every output) from synthetic ffmpeg lavfi sources — no input video file
required.  The player falls back to this variant when all content variants
fail to load (ADR 0007 §Slate fallback shape).

Implementation notes:
- The slate is always generated independently of content encoding, so a
  broken input file cannot prevent the slate from existing.
- ``drawtext`` requires a font; this module tries drawtext first and falls
  back to a plain color slate if drawtext fails (e.g. CI without fontconfig).
- Output bytes are deterministic for fixed parameters (content-addressable
  for CDN cache efficiency).
"""

from __future__ import annotations

from pathlib import Path

from civiccast.stream._ffmpeg import FfmpegError, run_ffmpeg
from civiccast.stream.config import (
    HLS_SEGMENT_DURATION,
    SLATE_BG_COLOR,
    SLATE_DURATION_SECONDS,
    SLATE_RENDITION,
    SLATE_TEXT,
)

__all__ = ["SlateError", "generate_slate"]


class SlateError(RuntimeError):
    """Failed to generate the slate variant."""


def generate_slate(output_dir: Path) -> Path:
    """Generate the slate HLS playlist and segments under ``output_dir/slate/``.

    Returns the path to the slate's ``playlist.m3u8``.

    Raises ``SlateError`` if ffmpeg cannot produce the slate.
    Raises ``FfmpegNotFoundError`` if ffmpeg is not installed.
    """
    slate_dir = output_dir / SLATE_RENDITION.name
    slate_dir.mkdir(parents=True, exist_ok=True)
    playlist_path = slate_dir / "playlist.m3u8"

    # Try with drawtext first (branded slate with error message).
    # Fall back to plain color if drawtext fails (missing fontconfig / font).
    try:
        _generate_slate_with_text(slate_dir, playlist_path)
    except FfmpegError:
        _generate_slate_plain_color(slate_dir, playlist_path)

    if not playlist_path.exists():
        raise SlateError(
            f"Slate playlist not created at {playlist_path}. "
            "Check ffmpeg installation and codec support."
        )
    return playlist_path


# ---------------------------------------------------------------------------
# Internal generators
# ---------------------------------------------------------------------------


def _generate_slate_with_text(slate_dir: Path, playlist_path: Path) -> None:
    """Attempt branded slate with drawtext overlay."""
    c = SLATE_RENDITION
    video_input = (
        f"color=c={SLATE_BG_COLOR}:size={c.width}x{c.height}"
        f":rate=25:duration={SLATE_DURATION_SECONDS}"
    )
    drawtext_filter = (
        f"drawtext=expansion=none:text='{SLATE_TEXT}':"
        "fontsize=14:fontcolor=white:box=1:boxcolor=black@0.4:boxborderw=4:"
        "x=(w-text_w)/2:y=(h-text_h)/2"
    )

    # ffmpeg requires ALL inputs before any output options. Both lavfi
    # sources (color video + silent audio) are declared up front; the -vf
    # drawtext filter and encode args are output-side and follow.
    result = run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            video_input,
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=22050:cl=mono",
            "-vf",
            drawtext_filter,
            *_encode_output_args(c, slate_dir, playlist_path),
        ]
    )
    if result.returncode != 0:
        raise FfmpegError(
            f"Slate-with-text generation failed (rc={result.returncode})",
            returncode=result.returncode,
            stderr=result.stderr,
        )


def _generate_slate_plain_color(slate_dir: Path, playlist_path: Path) -> None:
    """Plain-color slate fallback — no drawtext, no font dependency."""
    c = SLATE_RENDITION
    video_input = (
        f"color=c={SLATE_BG_COLOR}:size={c.width}x{c.height}"
        f":rate=25:duration={SLATE_DURATION_SECONDS}"
    )

    result = run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            video_input,
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=22050:cl=mono",
            *_encode_output_args(c, slate_dir, playlist_path),
        ]
    )
    if result.returncode != 0:
        raise SlateError(
            f"Plain-color slate fallback also failed (rc={result.returncode}). "
            f"ffmpeg stderr: {result.stderr[-500:]}"
        )


def _encode_output_args(
    c: object,  # RenditionConfig — typed as object to avoid circular import risk
    output_dir: Path,
    playlist_path: Path,
) -> list[str]:
    """Output-only ffmpeg args for slate rendition.

    Inputs (video lavfi + audio lavfi) MUST be declared by the caller before
    these args. Mixing input declarations into output args caused early
    builds to attach -profile:v to the audio input instead of the encoder
    output (ffmpeg argument-parsing rule: input options apply to the next
    -i; output options apply to the next output file).
    """
    from civiccast.stream.config import RenditionConfig

    assert isinstance(c, RenditionConfig)
    segment_pattern = str(output_dir / "seg%03d.ts")
    return [
        # Video codec
        "-c:v",
        "h264",
        "-profile:v",
        c.h264_profile,
        "-b:v",
        f"{c.video_bitrate_kbps}k",
        "-maxrate",
        f"{c.video_bitrate_kbps}k",
        "-bufsize",
        f"{c.video_bitrate_kbps * 2}k",
        # Audio codec (input #1 is the silent anullsrc declared by caller)
        "-c:a",
        "aac",
        "-b:a",
        f"{c.audio_bitrate_kbps}k",
        "-ar",
        "22050",
        # Trim audio to match video duration (anullsrc is infinite by default)
        "-shortest",
        # HLS muxer
        "-hls_time",
        str(HLS_SEGMENT_DURATION),
        "-hls_playlist_type",
        "vod",
        "-hls_flags",
        "independent_segments",
        "-hls_segment_filename",
        segment_pattern,
        str(playlist_path),
    ]
