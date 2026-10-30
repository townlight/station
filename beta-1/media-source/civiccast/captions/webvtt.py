# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""WebVTT rendering for stable caption cues."""

from __future__ import annotations

from html import escape

from civiccast.captions.models import CaptionCue


def format_webvtt_timestamp(seconds: float) -> str:
    """Format seconds as a WebVTT timestamp."""

    total_ms = round(seconds * 1000)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def render_webvtt(cues: list[CaptionCue]) -> str:
    """Render a WebVTT document from stable cues."""

    lines = ["WEBVTT", ""]
    for cue in sorted(cues, key=lambda item: (item.start_seconds, item.end_seconds, item.cue_id)):
        lines.extend(
            [
                cue.cue_id,
                f"{format_webvtt_timestamp(cue.start_seconds)} --> "
                f"{format_webvtt_timestamp(cue.end_seconds)}",
                escape(cue.text, quote=False),
                "",
            ]
        )
    return "\n".join(lines)
