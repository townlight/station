# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Bounded delivery receipts on the emitted MPEG-TS clock; no audio retention."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TypedDict

# Pinned GStreamer 1.28.5 tsmux.c CLOCK_BASE, added to every written PES PTS/DTS.
# https://github.com/GStreamer/gstreamer/blob/1.28.5/subprojects/gst-plugins-bad/gst/mpegtsmux/tsmux/tsmux.c
GST_TS_CLOCK_BASE_SECONDS = 3600.0
TS_CLOCK_WRAP_SECONDS = (1 << 33) / 90000.0


class _DeliveryPage(TypedDict):
    text: str
    start_seconds: float
    end_seconds: float


class CaptionDeliveryClock:
    """Record only accepted appsrc pages, with their actual scheduled PTS.

    The worker resets the file on start. Keep at most 512 pages/180 seconds;
    these are expected deliveries, never proof that captions survived the mux.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.pages: list[_DeliveryPage] = []
        self._publish()

    def record(self, text: str, start_seconds: float, duration_seconds: float) -> None:
        self.pages.append(
            {
                "text": text,
                "start_seconds": start_seconds + GST_TS_CLOCK_BASE_SECONDS,
                "end_seconds": start_seconds + duration_seconds + GST_TS_CLOCK_BASE_SECONDS,
            }
        )
        cutoff = start_seconds + GST_TS_CLOCK_BASE_SECONDS - 180
        self.pages = [p for p in self.pages if float(p["end_seconds"]) > cutoff][-512:]
        self._publish()

    def _publish(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(self.pages), encoding="utf-8")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)


def captured_video_pts_window(path: Path) -> tuple[float, float] | None:
    """Read video PES PTS from this capture, unwrapping the 33-bit TS clock.

    Only packet headers are inspected. A bounded capture must contain a real
    video PES timestamp; absent/corrupt clock metadata never produces a pass.
    """
    stamps: list[float] = []
    if path.stat().st_size > 64 * 1024 * 1024:
        return None
    with path.open("rb") as stream:
        while packet := stream.read(188):
            if len(packet) != 188 or packet[0] != 0x47:
                return None
            if not packet[1] & 0x40 or not packet[3] & 0x10:
                continue
            offset = 4
            if packet[3] & 0x20:
                offset += 1 + packet[4]
            pes = packet[offset:]
            if len(pes) < 14 or pes[:3] != b"\x00\x00\x01" or not 0xE0 <= pes[3] <= 0xEF:
                continue
            if not pes[7] & 0x80 or pes[8] < 5:
                continue
            p = pes[9:14]
            pts = (
                (((p[0] >> 1) & 7) << 30)
                | (p[1] << 22)
                | ((p[2] >> 1) << 15)
                | (p[3] << 7)
                | (p[4] >> 1)
            )
            seconds = pts / 90000.0
            if stamps:
                seconds += (
                    round((stamps[-1] - seconds) / TS_CLOCK_WRAP_SECONDS) * TS_CLOCK_WRAP_SECONDS
                )
            stamps.append(seconds)
    return (min(stamps), max(stamps)) if stamps else None
