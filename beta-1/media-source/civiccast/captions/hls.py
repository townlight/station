# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""HLS WebVTT publication helpers for caption cues."""

from __future__ import annotations

import re
from dataclasses import dataclass
from math import ceil
from pathlib import Path

from civiccast.captions.models import CaptionCue
from civiccast.captions.webvtt import render_webvtt
from civiccast.stream.config import HLS_SEGMENT_DURATION, SLATE_RENDITION
from civiccast.stream.manifest import (
    ManifestRendition,
    ManifestSubtitleTrack,
    write_multivariant_manifest,
)
from civiccast.stream.packager import SlateOnlyResult, VodPackageResult

__all__ = [
    "CaptionHlsTrack",
    "CaptionHlsTrackOutput",
    "attach_caption_tracks_to_package",
    "write_hls_caption_track",
]


@dataclass(frozen=True)
class CaptionHlsTrack:
    """A reviewed caption track to publish beside HLS video renditions."""

    cues: list[CaptionCue]
    language: str = "en"
    name: str = "English"
    default: bool = True
    autoselect: bool = True


@dataclass(frozen=True)
class CaptionHlsTrackOutput:
    """Files written for one HLS WebVTT caption track."""

    playlist_path: Path
    playlist_uri: str
    segment_paths: list[Path]
    manifest_track: ManifestSubtitleTrack


def write_hls_caption_track(
    track: CaptionHlsTrack,
    output_dir: Path,
    *,
    segment_duration: int = HLS_SEGMENT_DURATION,
) -> CaptionHlsTrackOutput:
    """Write one segmented WebVTT subtitle playlist under ``output_dir``."""
    if segment_duration <= 0:
        raise ValueError("segment_duration must be greater than 0.")

    safe_language = _safe_path_part(track.language)
    track_dir = output_dir / "captions" / safe_language
    track_dir.mkdir(parents=True, exist_ok=True)

    cues = sorted(track.cues, key=lambda cue: (cue.start_seconds, cue.end_seconds, cue.cue_id))
    segment_paths: list[Path] = []
    playlist_lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{segment_duration}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]

    if cues:
        first_segment = 0
        last_segment = max(
            first_segment,
            ceil(cues[-1].end_seconds / segment_duration) - 1,
        )
        for segment_index in range(first_segment, last_segment + 1):
            segment_start = segment_index * segment_duration
            segment_end = segment_start + segment_duration
            segment_cues = [
                cue
                for cue in cues
                if cue.start_seconds < segment_end and cue.end_seconds > segment_start
            ]
            segment_path = track_dir / f"seg{segment_index:03d}.vtt"
            segment_path.write_text(render_webvtt(segment_cues), encoding="utf-8")
            segment_paths.append(segment_path)
            playlist_lines.extend(
                [
                    f"#EXTINF:{segment_duration:.3f},",
                    segment_path.name,
                ]
            )

    playlist_lines.append("#EXT-X-ENDLIST")
    playlist_path = track_dir / "playlist.m3u8"
    playlist_path.write_text("\n".join(playlist_lines) + "\n", encoding="utf-8")
    playlist_uri = f"captions/{safe_language}/playlist.m3u8"
    manifest_track = ManifestSubtitleTrack(
        playlist_uri=playlist_uri,
        language=track.language,
        name=track.name,
        default=track.default,
        autoselect=track.autoselect,
    )
    return CaptionHlsTrackOutput(
        playlist_path=playlist_path,
        playlist_uri=playlist_uri,
        segment_paths=segment_paths,
        manifest_track=manifest_track,
    )


def attach_caption_tracks_to_package(
    package: VodPackageResult | SlateOnlyResult,
    tracks: list[CaptionHlsTrack],
    *,
    segment_duration: int = HLS_SEGMENT_DURATION,
) -> list[CaptionHlsTrackOutput]:
    """Write caption tracks and rewrite the package multivariant manifest."""
    if not tracks:
        raise ValueError("At least one caption track is required.")

    outputs = [
        write_hls_caption_track(track, package.output_dir, segment_duration=segment_duration)
        for track in tracks
    ]
    manifest_renditions = _manifest_renditions_for_package(package)
    write_multivariant_manifest(
        manifest_renditions,
        package.manifest_path,
        subtitle_tracks=[output.manifest_track for output in outputs],
    )
    return outputs


def _manifest_renditions_for_package(
    package: VodPackageResult | SlateOnlyResult,
) -> list[ManifestRendition]:
    if isinstance(package, VodPackageResult):
        return [
            ManifestRendition(
                config=rendition.config,
                playlist_uri=f"{rendition.config.name}/playlist.m3u8",
            )
            for rendition in package.renditions
        ]
    return [
        ManifestRendition(
            config=SLATE_RENDITION,
            playlist_uri="slate/playlist.m3u8",
        )
    ]


def _safe_path_part(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-").lower()
    return cleaned or "und"
