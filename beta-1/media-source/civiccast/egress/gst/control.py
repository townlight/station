# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Gi-free control-command parsing for the playout worker (Windows + POSIX).

The per-channel worker reads newline-delimited commands from a control FIFO; this
parser is kept gi-free so it is unit-testable without GStreamer.

Two seamless mechanisms (both keep the persistent mux running — D-S1-6, never a
restart):

* ``swap <index>`` — switch the active source role among the *fixed* legs
  (program/slate/live). Drives the operator role controls (force-slate, live take).
* ``reload <path>`` — rebuild the *program* leg's content from a new serialized
  ``PlayoutGraph`` at ``<path>`` while output stays PLAYING. This is how the daemon
  applies a newly-due scheduled program without bouncing the encoder.
* ``caption <pts_ms> <dur_ms> <b64text>`` — push one timed-text caption cue into the
  live caption appsrc (S11a). The text is base64-encoded (the FIFO is newline-
  delimited and base64 has no spaces/newlines), so arbitrary caption text survives.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

ControlCommand = (
    tuple[Literal["swap"], int]
    | tuple[Literal["reload"], str]
    | tuple[Literal["stop"]]
    | tuple[Literal["caption"], int, int, str]
    | tuple[Literal["caption"], int, int, str, str]
    | tuple[Literal["emergency"], str]
)

LIVE_CAPTION_LEAD_MS = 250

#: Largest gap between a requested live-caption PTS and the pipeline's live edge
#: that still counts as "a genuinely future cue" rather than a timestamp from an
#: unrelated epoch.  The live caption tap numbers audio segments on an ABSOLUTE
#: program clock (``chunk_index * segment_seconds``) that grows for the life of
#: the station, while the caption appsrc's running time restarts with each
#: worker.  Without this bound the two clocks diverge without limit and every
#: cue is scheduled minutes-to-hours ahead of the video, so it never reaches the
#: emitted stream (measured on Blackwell 2026-09-16: a good cue at program time
#: 00:52:12 was pushed ~50.7 minutes ahead of a 90 s pipeline running time and
#: the decode-back saw only A/53 null padding).
#:
#: A cue is treated as a genuinely future live cue only while it leads the
#: pipeline by no more than this.  It must be small enough to catch a SHORT
#: restart, not just a long-running station: after a quick restart the tap's
#: absolute segment index can sit ~20 s ahead of the freshly restarted
#: pipeline, and a 30 s ceiling let that cue keep its PTS and air ~20 s late
#: forever (the offset never self-corrects).  Real ASR/first-buffer latency is
#: sub-second to low single-digit seconds, so 8 s clears every honest case while
#: still catching a restart-scale lag.
MAX_LIVE_CAPTION_FUTURE_LEAD_MS = 8_000


def install_unix_signal_handlers(
    glib: Any,
    *,
    signal_numbers: tuple[int, ...],
    quit_loop: Callable[[], None],
) -> bool:
    """Register GLib Unix-signal handlers only when the runtime provides them.

    PyGObject's native-Windows GLib binding has no ``unix_signal_add`` symbol.
    The worker is stopped through its named-pipe control channel on Windows, so
    skipping Unix signal registration there is intentional.  POSIX keeps
    the existing SIGINT/SIGTERM behavior.
    """

    unix_signal_add = getattr(glib, "unix_signal_add", None)
    if not callable(unix_signal_add):
        return False

    def _quit() -> bool:
        quit_loop()
        return False

    for signal_number in signal_numbers:
        unix_signal_add(glib.PRIORITY_DEFAULT, signal_number, _quit)
    return True


def align_live_caption_pts_ms(
    *,
    requested_pts_ms: int,
    running_time_ms: int,
    stream_position_ms: int = 0,
    lead_ms: int = LIVE_CAPTION_LEAD_MS,
    max_future_lead_ms: int = MAX_LIVE_CAPTION_FUTURE_LEAD_MS,
) -> int:
    """Keep a live cue in the future even when ASR completed after its source PTS.

    Caption sidecars retain the source-audio timestamps.  The live appsrc cannot
    attach a cue to video buffers that already left the mux, so transport PTS is
    clamped to a small lead over the pipeline's current running time.

    A cue modestly ahead of the live edge keeps its original PTS.  A cue FAR
    ahead, however, is not genuinely "in the future" -- it is a timestamp from a
    different epoch.  The live tap numbers its audio segments by an ABSOLUTE
    program clock (``chunk_index * segment_seconds``), which keeps growing for
    the life of the station, while this appsrc's running time restarts with each
    worker.  Measured on Blackwell 2026-09-16: a real cue at 00:52:12 (3,132,120
    ms of program clock) was pushed against a pipeline running time of ~90 s, so
    the buffer was scheduled ~50.7 MINUTES ahead.  It could never reach the
    emitted stream inside the 6 s decode-back window, which is why the stream
    carried only A/53 null padding while the sidecar held a good cue.

    Anything beyond ``max_future_lead_ms`` is therefore rebased onto the live
    edge instead of preserved.  The bound is deliberately far larger than any
    real ASR/first-buffer latency (which is seconds) yet far smaller than the
    minutes-to-hours drift an absolute program clock produces.
    """

    if requested_pts_ms < 0 or running_time_ms < 0 or stream_position_ms < 0 or lead_ms < 0:
        raise ValueError("live caption timing values must be non-negative")
    if max_future_lead_ms < lead_ms:
        raise ValueError("max_future_lead_ms must be at least lead_ms")
    live_edge_ms = running_time_ms + lead_ms
    # An epoch offset stays constant as the pipeline ages. Comparing the lead
    # with uptime would let that same stale offset back through later (a 20 s
    # offset was rebased at startup but preserved after 20 s). Always apply the
    # configured lead limit. Keep the prior accepted buffer's end as a floor:
    # already-enqueued stream data cannot safely be moved backwards here.
    lead_over_edge_ms = requested_pts_ms - live_edge_ms
    if lead_over_edge_ms > max_future_lead_ms:
        return max(live_edge_ms, stream_position_ms)
    return max(requested_pts_ms, live_edge_ms, stream_position_ms)


def caption_gap_window_ms(
    *,
    stream_position_ms: int,
    running_time_ms: int,
) -> tuple[int, int] | None:
    """Return the next monotonic sparse-stream GAP window, if one is due."""

    if stream_position_ms < 0 or running_time_ms < 0:
        raise ValueError("live caption timing values must be non-negative")
    if running_time_ms <= stream_position_ms:
        return None
    return stream_position_ms, running_time_ms - stream_position_ms


def parse_control_line(line: str) -> ControlCommand | None:
    """Parse one control line; return a tuple, or None for blank/unknown.

    ``"swap <index>"`` → ``("swap", index)`` · ``"reload <path>"`` →
    ``("reload", path)`` · ``"stop"`` → ``("stop",)`` · ``"caption <pts_ms> <dur_ms>
    <b64text>"`` → ``("caption", pts_ms, dur_ms, b64text)``. The reload path is taken
    as the whole remainder of the line, so a path containing spaces is preserved.
    """
    parts = line.strip().split(None, 1)  # verb + remainder (at most one split)
    if not parts:
        return None
    verb = parts[0].lower()
    if verb == "swap" and len(parts) == 2 and parts[1].strip().isdigit():
        return ("swap", int(parts[1].strip()))
    if verb == "reload" and len(parts) == 2 and parts[1].strip():
        return ("reload", parts[1].strip())
    if verb == "emergency" and len(parts) == 2 and parts[1].strip():
        return ("emergency", parts[1].strip())
    if verb == "caption" and len(parts) == 2:
        fields = parts[1].split()
        if len(fields) in (3, 4) and fields[0].isdigit() and fields[1].isdigit() and fields[2]:
            if len(fields) == 4:
                return ("caption", int(fields[0]), int(fields[1]), fields[2], fields[3])
            return ("caption", int(fields[0]), int(fields[1]), fields[2])
        return None
    if verb == "stop" and len(parts) == 1:
        return ("stop",)
    return None
