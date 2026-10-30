# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""CSV transcript export for committed caption cues."""

from __future__ import annotations

import csv
from io import StringIO

from civiccast.captions import CaptionCue

_FIELDNAMES = [
    "cue_id",
    "start_seconds",
    "end_seconds",
    "start_timestamp",
    "end_timestamp",
    "text",
    "confidence",
    "low_confidence",
]


def render_transcript_csv(cues: list[CaptionCue]) -> str:
    """Render committed caption cues as stable-column CSV text."""

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=_FIELDNAMES, lineterminator="\n")
    writer.writeheader()
    for cue in cues:
        writer.writerow(
            {
                "cue_id": cue.cue_id,
                "start_seconds": _format_seconds(cue.start_seconds),
                "end_seconds": _format_seconds(cue.end_seconds),
                "start_timestamp": _format_timestamp(cue.start_seconds),
                "end_timestamp": _format_timestamp(cue.end_seconds),
                "text": cue.text,
                "confidence": _format_confidence(cue.confidence),
                "low_confidence": str(cue.low_confidence).lower(),
            }
        )
    return output.getvalue()


def _format_seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _format_confidence(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _format_timestamp(value: float) -> str:
    millis = round(value * 1000)
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"
