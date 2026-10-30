# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""VOD packager: encode input video → adaptive HLS ladder + slate.

Public API:

    result = pack_vod_asset(input_path, output_dir)
    # → VodPackageResult with manifest_path + renditions list

    fallback = pack_slate_fallback(output_dir)
    # → SlateOnlyResult for the broken-media orchestrator path (see below)

Broken-media orchestration pattern (Sprint 0.2):
  1. Generate slate first (always — never blocked by a bad input file).
  2. Try full encode. If it raises ``PackagingError``, the caller invokes
     ``pack_slate_fallback`` to write a slate-only manifest.
  3. The portal player receives a valid manifest in every code path.

Live ingest is Sprint 0.4; this module is VOD-only until then.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from civiccast.stream._ffmpeg import (
    FfmpegError,
    FfmpegNotFoundError,
    probe_video_dimensions,
    run_ffmpeg,
)
from civiccast.stream.config import (
    ABR_LADDER,
    HLS_SEGMENT_DURATION,
    SLATE_RENDITION,
    RenditionConfig,
    select_ladder,
)
from civiccast.stream.manifest import ManifestRendition, write_multivariant_manifest
from civiccast.stream.slate import generate_slate

__all__ = [
    "PackagingError",
    "RenditionOutput",
    "SlateOnlyResult",
    "VodPackageResult",
    "pack_slate_fallback",
    "pack_vod_asset",
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenditionOutput:
    """A successfully encoded HLS rendition."""

    config: RenditionConfig
    playlist_path: Path  # absolute local path to the variant playlist


@dataclass(frozen=True)
class VodPackageResult:
    """Result of a successful ``pack_vod_asset`` call.

    ``renditions`` holds the content renditions selected for this source
    (see :func:`civiccast.stream.config.select_ladder` — a source smaller
    than the ladder's top rung gets fewer than four, because CivicCast never
    upscales) followed by the slate, ordered highest-to-lowest bandwidth.
    The slate is always last.
    """

    manifest_path: Path
    renditions: list[RenditionOutput]
    output_dir: Path

    @property
    def slate(self) -> RenditionOutput:
        """The slate rendition (always the last entry)."""
        return self.renditions[-1]


@dataclass(frozen=True)
class SlateOnlyResult:
    """Result of ``pack_slate_fallback`` — a single-variant manifest pointing to the slate."""

    manifest_path: Path
    slate_playlist_path: Path
    output_dir: Path


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PackagingError(RuntimeError):
    """An input file could not be encoded.

    Callers should catch this and invoke ``pack_slate_fallback`` to ensure
    the portal serves a valid (slate-only) manifest.
    """

    def __init__(
        self,
        message: str,
        *,
        rendition: str | None = None,
        ffmpeg_stderr: str | None = None,
    ) -> None:
        super().__init__(message)
        self.rendition = rendition
        self.ffmpeg_stderr = ffmpeg_stderr


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def pack_vod_asset(
    input_path: Path,
    output_dir: Path,
    *,
    trim_in_seconds: float | None = None,
    trim_out_seconds: float | None = None,
    source_width: int | None = None,
    source_height: int | None = None,
    progress_callback: Callable[[str, str], None] | None = None,
) -> VodPackageResult:
    """Encode ``input_path`` to an HLS package (content ladder + slate).

    Steps:
    1. Validate the input file is readable.
    2. Generate the slate (always first — independent of input validity).
    3. Select the content ladder for this source — CivicCast never upscales,
       so a source shorter than the ladder's top rung produces fewer than
       four content renditions (see
       :func:`civiccast.stream.config.select_ladder`).
    4. Encode the selected content renditions.
    5. Write the multivariant manifest.

    Args:
        input_path: Path to the source video file.
        output_dir: Directory where HLS output is written.
                    Created if it does not exist.
        trim_in_seconds: Optional fractional start time, in seconds.
        trim_out_seconds: Optional fractional end time, in seconds.
        source_width: Source pixel width, when the caller already probed it
                      (the asset row carries ``width_px``/``height_px`` from
                      ingest). Saves a redundant ffprobe. When either
                      dimension is omitted the packager probes the input
                      itself, and falls back to the full ladder if the probe
                      cannot answer.
        source_height: Source pixel height — see ``source_width``.
        progress_callback: Called with (rendition_name, ffmpeg_stderr_line)
                           during encoding. Sprint 0.2: called after each
                           rendition completes, not in real time.

    Raises:
        PackagingError: The input could not be encoded (caller should invoke
                        ``pack_slate_fallback`` to serve a valid manifest).
        FfmpegNotFoundError: ffmpeg is not on PATH.
        FileNotFoundError: ``input_path`` does not exist.
    """
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")
    if not input_path.is_file():
        raise ValueError(f"Input path is not a file: {input_path}")
    if trim_in_seconds is not None and trim_in_seconds < 0:
        raise ValueError("trim_in_seconds must be greater than or equal to 0.")
    if trim_out_seconds is not None and trim_out_seconds <= 0:
        raise ValueError("trim_out_seconds must be greater than 0.")
    if (
        trim_in_seconds is not None
        and trim_out_seconds is not None
        and trim_in_seconds >= trim_out_seconds
    ):
        raise ValueError("trim_in_seconds must be strictly less than trim_out_seconds.")

    output_dir.mkdir(parents=True, exist_ok=True)

    # Slate is always generated first — independent of input file validity.
    slate_playlist = generate_slate(output_dir)
    slate_output = RenditionOutput(config=SLATE_RENDITION, playlist_path=slate_playlist)

    # Pick the ladder before encoding: renditions taller than the source cost
    # a full large-frame encode and add no detail, so they are dropped and the
    # top rung is pinned to the source's own resolution instead.
    if source_width is None or source_height is None:
        probed = probe_video_dimensions(input_path)
        if probed is not None:
            source_width, source_height = probed
    ladder = select_ladder(
        source_width=source_width, source_height=source_height, ladder=ABR_LADDER
    )

    # Encode content renditions.
    content_outputs: list[RenditionOutput] = []
    for rendition_config in ladder:
        playlist_path = _encode_rendition(
            input_path,
            output_dir,
            rendition_config,
            trim_in_seconds=trim_in_seconds,
            trim_out_seconds=trim_out_seconds,
            progress_callback=progress_callback,
        )
        content_outputs.append(
            RenditionOutput(config=rendition_config, playlist_path=playlist_path)
        )

    # Content renditions first (highest→lowest), slate last.
    all_renditions = [*content_outputs, slate_output]

    # Build manifest with relative playlist URIs (CDN-agnostic).
    manifest_renditions = [
        ManifestRendition(
            config=r.config,
            playlist_uri=f"{r.config.name}/playlist.m3u8",
        )
        for r in all_renditions
    ]
    manifest_path = write_multivariant_manifest(
        manifest_renditions,
        output_dir / "playlist.m3u8",
    )

    # Record the range actually encoded, independent of later asset edits.
    (output_dir / "playback-timeline.json").write_text(
        json.dumps(
            {
                "version": 1,
                "source_start_seconds": trim_in_seconds or 0.0,
                "source_end_seconds": trim_out_seconds,
            }
        ),
        encoding="utf-8",
    )

    return VodPackageResult(
        manifest_path=manifest_path,
        renditions=all_renditions,
        output_dir=output_dir,
    )


def pack_slate_fallback(output_dir: Path) -> SlateOnlyResult:
    """Write a slate-only manifest for the broken-media fallback path.

    Generates the slate if not already present, then writes a manifest
    whose single variant is the slate.  Called by the orchestrator when
    ``pack_vod_asset`` raises ``PackagingError``.

    Raises:
        SlateError: Slate could not be generated.
        FfmpegNotFoundError: ffmpeg is not on PATH.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    slate_playlist_path = output_dir / SLATE_RENDITION.name / "playlist.m3u8"
    if not slate_playlist_path.exists():
        slate_playlist_path = generate_slate(output_dir)

    manifest_renditions = [
        ManifestRendition(
            config=SLATE_RENDITION,
            playlist_uri=f"{SLATE_RENDITION.name}/playlist.m3u8",
        )
    ]
    manifest_path = write_multivariant_manifest(
        manifest_renditions,
        output_dir / "playlist.m3u8",
    )

    return SlateOnlyResult(
        manifest_path=manifest_path,
        slate_playlist_path=slate_playlist_path,
        output_dir=output_dir,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _encode_rendition(
    input_path: Path,
    output_dir: Path,
    config: RenditionConfig,
    *,
    trim_in_seconds: float | None = None,
    trim_out_seconds: float | None = None,
    progress_callback: Callable[[str, str], None] | None = None,
) -> Path:
    """Encode one ABR rendition. Returns the playlist path.

    Raises ``PackagingError`` on ffmpeg failure.
    """
    rendition_dir = output_dir / config.name
    rendition_dir.mkdir(parents=True, exist_ok=True)
    playlist_path = rendition_dir / "playlist.m3u8"
    segment_pattern = str(rendition_dir / "seg%03d.ts")

    # Scale with letterbox/pillarbox so aspect ratio is never distorted.
    # force_original_aspect_ratio=decrease: scale down to fit; pad to exact size.
    scale_filter = (
        f"scale={config.width}:{config.height}"
        ":force_original_aspect_ratio=decrease,"
        f"pad={config.width}:{config.height}:(ow-iw)/2:(oh-ih)/2"
    )

    captured_lines: list[str] = []

    def _capture(line: str) -> None:
        captured_lines.append(line)
        if progress_callback is not None:
            progress_callback(config.name, line)

    input_args: list[str] = []
    if trim_in_seconds is not None:
        input_args.extend(["-ss", f"{trim_in_seconds:.3f}"])
    input_args.extend(["-i", str(input_path)])

    duration_args: list[str] = []
    if trim_out_seconds is not None:
        start = trim_in_seconds or 0.0
        duration_args.extend(["-t", f"{trim_out_seconds - start:.3f}"])

    try:
        result = run_ffmpeg(
            [
                *input_args,
                # Video
                "-vf",
                scale_filter,
                "-c:v",
                "h264",
                # Force yuv420p — required for HLS browser playback compatibility,
                # and the h264 'high' profile rejects 4:4:4 chroma subsampling.
                "-pix_fmt",
                "yuv420p",
                "-profile:v",
                config.h264_profile,
                "-b:v",
                f"{config.video_bitrate_kbps}k",
                "-maxrate",
                f"{config.video_bitrate_kbps}k",
                "-bufsize",
                f"{config.video_bitrate_kbps * 2}k",
                # Audio
                "-c:a",
                "aac",
                "-b:a",
                f"{config.audio_bitrate_kbps}k",
                *duration_args,
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
            ],
            progress_callback=_capture,
        )
    except FfmpegNotFoundError:
        raise  # let it propagate — operator needs to install ffmpeg
    except FfmpegError as exc:
        raise PackagingError(
            f"Encoding failed for rendition '{config.name}': {exc}",
            rendition=config.name,
            ffmpeg_stderr=exc.stderr,
        ) from exc

    if result.returncode != 0:
        raise PackagingError(
            f"ffmpeg exited {result.returncode} encoding '{config.name}'",
            rendition=config.name,
            ffmpeg_stderr=result.stderr,
        )

    return playlist_path
