# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""ABR ladder constants and rendition configuration.

These are the Sprint 0.2 defaults. Per-channel ladder overrides land at
Sprint 0.3 with the schedule module (spec §8.2 "ABR ladder is configurable
per channel").  Do not import mutable state from this module — every value
here is frozen at import time.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class RenditionConfig:
    """Immutable description of a single HLS rendition."""

    name: str
    width: int
    height: int
    video_bitrate_kbps: int
    audio_bitrate_kbps: int
    h264_profile: str  # "high" | "main" | "baseline"
    h264_codec_string: str  # RFC 6381 codec string for the HLS manifest CODECS attribute
    advertised_bandwidth_bps_override: int | None = None

    @property
    def bandwidth_bps(self) -> int:
        """Total real bitrate in bits/sec, derived from configured kbps."""
        return (self.video_bitrate_kbps + self.audio_bitrate_kbps) * 1000

    @property
    def manifest_bandwidth_bps(self) -> int:
        """Value to advertise in the HLS BANDWIDTH attribute.

        Defaults to the real bandwidth. The slate intentionally inflates this
        so estimate-matching ABR clients never select it as a "good" choice
        — see ADR 0007 amendment "Slate failover mechanism (v0.2)".
        """
        if self.advertised_bandwidth_bps_override is not None:
            return self.advertised_bandwidth_bps_override
        return self.bandwidth_bps

    @property
    def resolution_str(self) -> str:
        """HLS RESOLUTION attribute value, e.g. '1920x1080'."""
        return f"{self.width}x{self.height}"


# ---------------------------------------------------------------------------
# ABR ladder — 4 content renditions (spec §8.2, ADR 0007)
# ---------------------------------------------------------------------------
# Bitrates tuned for 2026 residential and mobile network conditions:
#   - 1080p: home broadband (median US ≈ 250 Mbps)
#   - 720p/480p: LTE / mid-range 5G (real-world 5-15 Mbps)
#   - 240p: cellular in poor coverage
#
# H.264 codec strings: avc1.{profile_idc:02x}{constraints:02x}{level_idc:02x}
#   high @ level 4.0  → 0x64 0x00 0x28 → avc1.640028
#   main @ level 3.1  → 0x4d 0x40 0x1f → avc1.4d401f
#   baseline @ level 3.0 → 0x42 0x00 0x1e → avc1.42001e

ABR_LADDER: tuple[RenditionConfig, ...] = (
    RenditionConfig(
        name="1080p",
        width=1920,
        height=1080,
        video_bitrate_kbps=4500,
        audio_bitrate_kbps=128,
        h264_profile="high",
        h264_codec_string="avc1.640028",
    ),
    RenditionConfig(
        name="720p",
        width=1280,
        height=720,
        video_bitrate_kbps=2500,
        audio_bitrate_kbps=128,
        h264_profile="main",
        h264_codec_string="avc1.4d401f",
    ),
    RenditionConfig(
        name="480p",
        width=854,
        height=480,
        video_bitrate_kbps=1000,
        audio_bitrate_kbps=96,
        h264_profile="main",
        h264_codec_string="avc1.4d401f",
    ),
    RenditionConfig(
        name="240p",
        width=426,
        height=240,
        video_bitrate_kbps=350,
        audio_bitrate_kbps=64,
        h264_profile="baseline",
        h264_codec_string="avc1.42001e",
    ),
)

# ---------------------------------------------------------------------------
# Slate rendition — always the 5th (lowest) variant (ADR 0007)
# ---------------------------------------------------------------------------
# 426x240, 200 kbps video, 32 kbps audio (or muted with periodic 1 Hz beep),
# H.264 baseline, 2-second segments matching content variants.
# The player falls back here when ALL content variants fail to play.

SLATE_RENDITION = RenditionConfig(
    name="slate",
    width=426,
    height=240,
    video_bitrate_kbps=200,
    audio_bitrate_kbps=32,
    h264_profile="baseline",
    h264_codec_string="avc1.42001e",
    # Advertise the slate at a bandwidth higher than the highest content
    # variant (1080p at 4628000 bps). HLS players estimate-match toward
    # the rendition closest to current measured bandwidth; advertising
    # 50 Mbps means no realistic client will ever pick the slate as a
    # primary choice. The slate is reached only when ALL content variants
    # fail to load — which is exactly the fallback semantic ADR 0007 names.
    # See ADR 0007 §"Slate failover mechanism (v0.2 amendment)".
    advertised_bandwidth_bps_override=50_000_000,
)

# ---------------------------------------------------------------------------
# Ladder selection — never upscale
# ---------------------------------------------------------------------------


def select_ladder(
    *,
    source_width: int | None,
    source_height: int | None,
    ladder: tuple[RenditionConfig, ...] = ABR_LADDER,
) -> tuple[RenditionConfig, ...]:
    """Return the subset of ``ladder`` worth encoding for this source.

    A rendition taller than the source invents pixels: it costs the full
    encode of a large frame, produces a bigger file, and carries no detail
    the source did not already have. Packaging a 640x360 clip through the
    unfiltered ladder spends roughly 80% of its wall time on the upscaled
    1080p and 720p rungs alone (measured on the Gate-A sample clip: 18.4s
    unfiltered vs 3.4s filtered), which is what pushed
    ``POST /api/staff/assets/{id}/package`` past its callers' timeouts.

    The rules, in order:

    * **Dimensions unknown** (either is ``None`` or non-positive) — return
      ``ladder`` unchanged. Never guess; the previous behaviour is the safe
      default when ffprobe could not read the source.
    * **Source at or above the ladder's top rung** — return ``ladder``
      unchanged. Nothing upscales, and the ladder's top rung is a deliberate
      product cap: a 4K source still publishes at 1080p and below.
    * **Source height matches a rung exactly** — return the rungs at or below
      it. The top tier already carries the source's full detail.
    * **Source falls between rungs** (or below every rung) — return the rungs
      strictly below it, plus one rung at the source's own resolution so the
      top tier is neither upscaled nor needlessly downscaled. That rung
      inherits its bitrate, profile and codec string from the shortest rung
      the source outgrew, and is named for its own height (``"360p"`` for a
      640x360 source).

    Returned renditions keep ``ladder``'s highest-to-lowest ordering. The
    slate is not part of the ladder and is unaffected.
    """
    if not ladder:
        return ladder
    if not source_width or not source_height or source_width <= 0 or source_height <= 0:
        return ladder

    # H.264 with yuv420p chroma subsampling requires even dimensions.
    native_width = max(2, source_width - (source_width % 2))
    native_height = max(2, source_height - (source_height % 2))

    oversized = [rendition for rendition in ladder if rendition.height > native_height]
    if not oversized:
        return ladder

    kept = tuple(rendition for rendition in ladder if rendition.height <= native_height)
    if kept and max(rendition.height for rendition in kept) == native_height:
        return kept

    template = min(oversized, key=lambda rendition: rendition.height)
    name = f"{native_height}p"
    if any(rendition.name == name for rendition in kept):
        name = "source"
    native = replace(template, name=name, width=native_width, height=native_height)
    return (native, *kept)


# ---------------------------------------------------------------------------
# HLS packaging constants
# ---------------------------------------------------------------------------

HLS_SEGMENT_DURATION: int = 2  # seconds (matches ABR variants for clean failover)
HLS_VERSION: int = 3  # minimum HLS spec version required by CivicCast manifests

# The slate is a short looping video; 30 seconds gives 15 x 2-second segments.
SLATE_DURATION_SECONDS: int = 30

# CivicCast brand color for the slate background (#1a2744 — dark navy blue).
SLATE_BG_COLOR: str = "0x1a2744"
SLATE_TEXT: str = "We are experiencing technical difficulties."

# Minimum ffmpeg version supported (major, minor).
FFMPEG_MIN_VERSION: tuple[int, int] = (4, 4)
