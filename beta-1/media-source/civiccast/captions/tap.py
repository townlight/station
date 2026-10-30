# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Egress audio fork for live captions (Beta sprint B6, decision #1 option A).

The egress encoder is the single owner of the ffmpeg process graph (design
option 1 in ``docs/design/live-audio-caption-tap-deferral.md``). When a
caption tap directory is configured (``CIVICCAST_CAPTION_TAP_DIR``), the
persistent encoder gains one extra output: rolling mono 16 kHz signed-16-bit
WAV segments under ``<tap_root>/<channel_id>/chunk-NNNNNN.wav``. The caption
tap worker (:mod:`civiccast.captions.tap_worker`) consumes those segments and
feeds the existing live caption seam.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

TAP_SAMPLE_RATE_HZ = 16_000
TAP_SEGMENT_PATTERN = "chunk-%06d.wav"

__all__ = [
    "TAP_SAMPLE_RATE_HZ",
    "TAP_SEGMENT_PATTERN",
    "AudioTapPlan",
    "build_audio_tap_plan",
]


@dataclass(frozen=True)
class AudioTapPlan:
    """One channel's audio fork: where segments land and how long they are."""

    tap_dir: Path
    segment_seconds: float = 5.0

    def output_args(self) -> list[str]:
        """FFmpeg output args forking caption audio to rolling WAV segments.

        Creates the segment directory (ffmpeg does not), maps the first audio
        stream if present, downmixes to mono 16 kHz s16le — what the caption
        runtime consumes — and resets timestamps per segment so each WAV is
        self-contained.
        """

        self.tap_dir.mkdir(parents=True, exist_ok=True)
        return [
            "-map",
            "0:a:0?",
            "-ar",
            str(TAP_SAMPLE_RATE_HZ),
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            "-f",
            "segment",
            "-segment_time",
            str(self.segment_seconds),
            "-reset_timestamps",
            "1",
            str(self.tap_dir / TAP_SEGMENT_PATTERN),
        ]


def build_audio_tap_plan(channel_id: str) -> AudioTapPlan | None:
    """Build the channel's tap plan from the environment, or None when off.

    ``CIVICCAST_CAPTION_TAP_DIR`` is the tap root shared with the caption tap
    worker; each channel forks into its own subdirectory.
    ``CIVICCAST_CAPTION_TAP_SEGMENT_SECONDS`` tunes segment length (default 5).

    Item 91: ``CIVICCAST_CAPTION_TAP=off`` always means no tap, even if
    ``CIVICCAST_CAPTION_TAP_DIR`` is (still, or stray) set -- checked here
    directly rather than relying solely on callers to omit the directory, so
    this function stays the single place that decides "does this channel's
    audio get forked" no matter which caller reaches it (the GStreamer
    strategy's ``_with_audio_tap`` and any future caller alike).
    """

    if os.environ.get("CIVICCAST_CAPTION_TAP", "").strip().lower() == "off":
        return None
    root = os.environ.get("CIVICCAST_CAPTION_TAP_DIR", "").strip()
    if not root:
        return None
    raw_seconds = os.environ.get("CIVICCAST_CAPTION_TAP_SEGMENT_SECONDS", "").strip()
    if raw_seconds:
        try:
            segment_seconds = float(raw_seconds)
        except ValueError as exc:
            raise ValueError(
                f"CIVICCAST_CAPTION_TAP_SEGMENT_SECONDS must be a number; got {raw_seconds!r}."
            ) from exc
        if segment_seconds <= 0:
            raise ValueError("CIVICCAST_CAPTION_TAP_SEGMENT_SECONDS must be positive.")
    else:
        segment_seconds = 5.0
    return AudioTapPlan(tap_dir=Path(root) / channel_id, segment_seconds=segment_seconds)
