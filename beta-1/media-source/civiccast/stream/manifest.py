# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""HLS multivariant manifest assembly.

Builds RFC 8216-compliant HLS multivariant playlists from ``RenditionConfig``
descriptors. The slate entry is always present per ADR 0007; the content
entries are whichever rungs the packager selected for the source, which is
the full four-rung ladder for a 1080p-or-larger source and fewer for a
smaller one (CivicCast never upscales — see
:func:`civiccast.stream.config.select_ladder`).

Slate failover (Sprint 0.3 cleanup batch G — ADR 0007 v0.3 amendment):

  The slate is now declared as an alternate VIDEO rendition group via
  ``EXT-X-MEDIA TYPE=VIDEO,GROUP-ID="slate-fallback"`` and excluded from
  the standard ABR set. Each content ``EXT-X-STREAM-INF`` references the
  slate group with ``VIDEO="content"`` so a compliant HLS player can
  switch tracks within the rendition group when a content variant
  becomes unplayable. The slate's ``EXT-X-MEDIA`` carries
  ``DEFAULT=NO,AUTOSELECT=NO`` so clients only select it when explicitly
  directed (player API or hard failover).

  The v0.2 mechanism — advertise the slate as a STREAM-INF at 50 Mbps so
  estimate-matching ABR clients never pick it — is preserved as a
  belt-and-suspenders fallback inside ``build_multivariant_manifest``
  for older clients that ignore EXT-X-MEDIA failover groups. Modern
  clients (hls.js, native iOS/Safari, ExoPlayer) honor EXT-X-MEDIA and
  use the proper failover path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from civiccast.stream.config import HLS_VERSION, RenditionConfig

__all__ = [
    "SLATE_FAILOVER_GROUP_ID",
    "SUBTITLE_GROUP_ID",
    "ManifestRendition",
    "ManifestSubtitleTrack",
    "build_multivariant_manifest",
    "write_multivariant_manifest",
]

# GROUP-ID under which the slate is declared as an alternate VIDEO
# rendition. Content STREAM-INF entries reference it via VIDEO="content"
# so the slate sits in the same rendition group as the content streams
# and serves as a failover destination (HLS RFC 8216 §4.3.4.1).
SLATE_FAILOVER_GROUP_ID = "content"
SUBTITLE_GROUP_ID = "subtitles"


@dataclass(frozen=True)
class ManifestRendition:
    """Resolved rendition entry ready for manifest inclusion.

    ``playlist_uri`` is the URI embedded in the manifest — either a
    relative path (for local/CDN-relative manifests) or an absolute URL
    (for CDN-absolute manifests).
    """

    config: RenditionConfig
    playlist_uri: str  # relative or absolute URI for the playlist


@dataclass(frozen=True)
class ManifestSubtitleTrack:
    """Resolved WebVTT subtitle track ready for manifest inclusion."""

    playlist_uri: str
    language: str = "en"
    name: str = "English"
    group_id: str = SUBTITLE_GROUP_ID
    default: bool = True
    autoselect: bool = True


def _is_slate(r: ManifestRendition) -> bool:
    """Detect the slate rendition by name. ADR 0007 names the slate
    rendition ``"slate"`` and forbids any other rendition from sharing
    the name; the test suite enforces that contract."""
    return r.config.name == "slate"


def build_multivariant_manifest(
    renditions: list[ManifestRendition],
    *,
    subtitle_tracks: list[ManifestSubtitleTrack] | None = None,
) -> str:
    """Return a multivariant HLS manifest string.

    The output declares the slate as an ``EXT-X-MEDIA TYPE=VIDEO`` alternate
    rendition referenced by every content ``EXT-X-STREAM-INF`` via
    ``VIDEO="content"``, giving compliant HLS players a true failover path
    within a single rendition group. The slate ALSO appears as a
    STREAM-INF entry at the inflated 50 Mbps BANDWIDTH so older clients
    that ignore EXT-X-MEDIA still cannot select it as a primary choice.

    Args:
        renditions: All renditions to include. Content variants are
                    typically passed highest-to-lowest bandwidth; the
                    slate is identified by ``config.name == "slate"`` and
                    placed in its own rendition group.
        subtitle_tracks: Optional WebVTT subtitle playlists to advertise via
                         ``EXT-X-MEDIA TYPE=SUBTITLES``.

    Returns:
        Complete multivariant manifest as a string, LF line endings,
        terminated by a single trailing LF.
    """
    if not renditions:
        raise ValueError("Cannot build a manifest with zero renditions.")

    content_renditions = [r for r in renditions if not _is_slate(r)]
    slate_rendition = next((r for r in renditions if _is_slate(r)), None)
    subtitle_tracks = subtitle_tracks or []

    lines: list[str] = [
        "#EXTM3U",
        f"#EXT-X-VERSION:{HLS_VERSION}",
        "",
    ]

    if slate_rendition is not None:
        # EXT-X-MEDIA descriptor for the slate as alternate VIDEO rendition.
        # DEFAULT=NO + AUTOSELECT=NO mean the player ignores this rendition
        # for normal selection; it becomes the failover destination when
        # all content URIs in the same GROUP-ID fail. NAME and LANGUAGE
        # are required-or-recommended attributes for TYPE=VIDEO/AUDIO.
        lines.append(
            f'#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="{SLATE_FAILOVER_GROUP_ID}",'
            f'NAME="Slate fallback",DEFAULT=NO,AUTOSELECT=NO,'
            f'URI="{slate_rendition.playlist_uri}"'
        )
        lines.append("")

    for track in subtitle_tracks:
        lines.append(
            f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="{track.group_id}",'
            f'LANGUAGE="{track.language}",NAME="{track.name}",'
            f"DEFAULT={'YES' if track.default else 'NO'},"
            f"AUTOSELECT={'YES' if track.autoselect else 'NO'},"
            f'URI="{track.playlist_uri}"'
        )
    if subtitle_tracks:
        lines.append("")

    for r in content_renditions:
        c = r.config
        codecs = f"{c.h264_codec_string},mp4a.40.2"
        # VIDEO="content" associates this STREAM-INF with the slate's
        # rendition group, giving the player a defined failover target
        # when this variant's segments stop loading.
        lines.append(
            f"#EXT-X-STREAM-INF:BANDWIDTH={c.manifest_bandwidth_bps},"
            f"RESOLUTION={c.resolution_str},"
            f'CODECS="{codecs}",'
            f'VIDEO="{SLATE_FAILOVER_GROUP_ID}"'
            f"{_subtitle_attr(subtitle_tracks)}"
        )
        lines.append(r.playlist_uri)

    if slate_rendition is not None:
        # Belt-and-suspenders: keep the inflated-BANDWIDTH STREAM-INF for
        # older clients that ignore EXT-X-MEDIA. They still cannot select
        # the slate as a primary because the advertised 50 Mbps exceeds
        # every realistic connection speed.
        c = slate_rendition.config
        codecs = f"{c.h264_codec_string},mp4a.40.2"
        lines.append(
            f"#EXT-X-STREAM-INF:BANDWIDTH={c.manifest_bandwidth_bps},"
            f"RESOLUTION={c.resolution_str},"
            f'CODECS="{codecs}",'
            f'VIDEO="{SLATE_FAILOVER_GROUP_ID}"'
            f"{_subtitle_attr(subtitle_tracks)}"
        )
        lines.append(slate_rendition.playlist_uri)

    return "\n".join(lines) + "\n"


def write_multivariant_manifest(
    renditions: list[ManifestRendition],
    output_path: Path,
    *,
    subtitle_tracks: list[ManifestSubtitleTrack] | None = None,
) -> Path:
    """Write the multivariant manifest to ``output_path`` and return it."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        build_multivariant_manifest(renditions, subtitle_tracks=subtitle_tracks),
        encoding="utf-8",
    )
    return output_path


def _subtitle_attr(subtitle_tracks: list[ManifestSubtitleTrack]) -> str:
    if not subtitle_tracks:
        return ""
    return f',SUBTITLES="{subtitle_tracks[0].group_id}"'
