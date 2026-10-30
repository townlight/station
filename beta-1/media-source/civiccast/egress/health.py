# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Health metric parsing for egress encoder logs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from civiccast.egress.models import EgressConfig, EgressState


@dataclass(frozen=True)
class EgressEncoderMetrics:
    """Encoder metrics parsed from FFmpeg progress output."""

    encoder_fps: float | None = None
    encoder_bitrate_kbps: float | None = None
    dropped_frames: int | None = None


#: D44 (real-hardware tester run, 2026-09-05): how much of the tail of a worker
#: stderr log is scanned for the newest metrics. The daemon reads this file
#: on every ~2s health tick (twice per tick, from two call sites), and the log
#: grows for the whole life of the channel -- reading it whole meant re-reading
#: and re-regexing an ever-growing file forever, a cost that showed up as
#: control-plane CPU in a long soak. Only the newest values are wanted and an
#: ffmpeg progress line is ~100 bytes, so 64 KiB is hundreds of lines of
#: history: far more than enough to find the last parseable one.
_TAIL_BYTES = 64 * 1024

_FPS_RE = re.compile(r"\bfps=\s*(?P<value>[0-9]+(?:\.[0-9]+)?)")
_BITRATE_RE = re.compile(r"\bbitrate=\s*(?P<value>[0-9]+(?:\.[0-9]+)?)\s*(?P<unit>[kKmM]?bits/s)")
_DROP_RE = re.compile(r"\bdrop(?:_frames)?=\s*(?P<value>[0-9]+)")

#: Item 82 round-3 (PR #183 review, BLOCKER item 1): the ONLY genuine evidence
#: that a GStreamer worker's pipeline actually reached PLAYING -- printed by
#: ``civiccast.egress.gst.engine.GstPlayoutEngine._await_playing`` exactly
#: once, on the success path, never on a timeout. A stable, parsed contract
#: (not just a human-readable log line): the daemon's alive-poll health check
#: (``EgressDaemon._poll_process``) greps for it instead of trusting
#: wall-clock seconds since the worker was spawned, which also counts
#: interpreter start + ``import gi``/``Gst.init`` + graph build + the preroll
#: wait itself -- none of which is air, and none of which is bounded by the
#: worker's own ``preroll_timeout_s``.
#:
#: Round-4 (PR #183 review, BLOCKER reproduced): the marker now optionally
#: carries the printing worker's own pid (``... pid=1234``,
#: ``GstPlayoutEngine._await_playing``'s ``os.getpid()``) so
#: ``worker_reached_playing`` can refuse to credit a CURRENT worker with a
#: PREVIOUS worker's marker even if both happen to land in the byte range it
#: scans (belt-and-braces on top of the spawn-offset anchor below -- see
#: that function's docstring for why the offset anchor alone was still
#: reproduced as a BLOCKER).
_PLAYING_REACHED_RE = re.compile(
    r"^CTRL preroll: reached PLAYING after [0-9]+(?:\.[0-9]+)?s(?: pid=(?P<pid>[0-9]+))?"
)

#: Item 84 Round-2 review BLOCKER: the ONLY genuine evidence that a GStreamer
#: worker's pipeline has actually pushed a TS buffer past the mux -- printed
#: by ``civiccast.egress.gst.engine.GstPlayoutEngine.
#: _maybe_print_first_output_marker`` exactly once, the moment the first
#: buffer/buffer-list is observed. Deliberately a SEPARATE, later piece of
#: evidence than ``_PLAYING_REACHED_RE`` above: the reviewer measured that
#: crediting the PLAYING marker alone as on-air evidence let a worker that
#: reaches PLAYING on every relaunch but never produces a buffer (at ANY
#: ``first_output_timeout_s`` from 65s through the 120s clamp ceiling) get
#: its crash-loop streak reset every alive-poll cycle -- the streak never
#: escalated to fallback slate (pinned at 1) no matter how the budget was
#: configured. ``worker_produced_output`` below is what
#: ``EgressDaemon._observed_on_air_evidence`` now requires for the GStreamer
#: strategy instead of ``worker_reached_playing`` -- PLAYING is kept as a log
#: signal only, never again as on-air evidence.
_FIRST_OUTPUT_RE = re.compile(
    r"^CTRL first-output: first buffer after [0-9]+(?:\.[0-9]+)?s(?: pid=(?P<pid>[0-9]+))?"
)

#: Round-4 (PR #183 review, BLOCKER reproduced): bound on how many bytes past
#: a worker's spawn offset ``worker_reached_playing`` /
#: ``read_ffmpeg_encoder_metrics_since`` will scan. Round-3's fix greped the
#: marker with NO byte bound past the offset at all in early drafts of this
#: fix, which the reviewer flagged as a new unbounded-read risk on a worker
#: that never reaches PLAYING and just keeps growing its log forever (the
#: exact D44 cost problem the OLD 64 KiB tail window existed to prevent, now
#: reintroduced from the other end of the file). 4 MiB comfortably covers the
#: marker even from a chatty worker (D44: an ffmpeg-style progress line is
#: ~100 bytes; GStreamer's own startup chatter before PLAYING is far less
#: than this in every real run captured so far) while still being a hard
#: ceiling on control-plane read cost per health tick.
_SPAWN_SCAN_LIMIT_BYTES = 4 * 1024 * 1024
_SPAWN_SCAN_CHUNK_BYTES = 256 * 1024


def _read_from_offset(
    path: Path, offset: int, *, limit_bytes: int = _SPAWN_SCAN_LIMIT_BYTES
) -> str | None:
    """Read up to ``limit_bytes`` starting at ``offset`` bytes into ``path``,
    in bounded chunks (never the whole file at once). Round-4 (PR #183
    review, BLOCKER reproduced): this is the anchor that replaces the old
    tail-window read for on-air evidence -- ``offset`` is the byte size of
    the log at the CURRENT worker's spawn time
    (``EgressDaemon._stderr_spawn_offset``), so a previous worker's lines
    (all of which sit before that offset, because ``strategy.py`` /
    ``_ffmpeg.py`` open this fixed per-channel log in APPEND mode and never
    truncate it per spawn) are never read at all -- not filtered out after
    the fact, never read in the first place.

    If the file is smaller than ``offset`` (the log was rotated or
    truncated out from under the daemon between spawn and this read), that
    offset can no longer mean anything -- treated as a fresh file and read
    from byte 0 instead of raising or returning nothing.

    Missing/unreadable file -> ``None`` (same fail-closed contract as the
    tail-window read this replaces)."""

    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            effective_offset = offset if size >= offset else 0
            handle.seek(effective_offset)
            chunks: list[bytes] = []
            remaining = limit_bytes
            while remaining > 0:
                chunk = handle.read(min(_SPAWN_SCAN_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
    except OSError:
        return None
    return raw.decode("utf-8", errors="replace")


def worker_reached_playing(path: Path, *, offset: int = 0, expected_pid: int | None = None) -> bool:
    """Return whether the CURRENT worker's stderr log shows real evidence it
    reached the PLAYING state (see ``_PLAYING_REACHED_RE``) -- as opposed to
    merely having not exited yet, which proves nothing about whether it ever
    produced output.

    Round-4 (PR #183 review, BLOCKER reproduced): round-3's version scanned
    a fixed tail window with NO spawn anchor. Because ``strategy.py`` /
    ``_ffmpeg.py`` open this fixed per-channel log in APPEND mode and never
    truncate it per spawn, ONE worker ever reaching PLAYING left the marker
    sitting in the log forever -- every later worker spawned on the same
    channel was "confirmed on air" on its very first poll tick, even one
    that never once reached PLAYING itself (measured: 40 relaunches, streak
    pinned at 1). Two fixes, belt and braces:

    * ``offset`` anchors the read to bytes at or after the CURRENT worker's
      own spawn point (``EgressDaemon._stderr_spawn_offset``) via
      ``_read_from_offset`` -- a previous worker's marker sits before this
      offset and is never read, let alone matched. The old fixed tail
      window is gone entirely: the marker is the OLDEST line a worker
      prints, so a worker whose own startup chatter crosses 64 KiB before
      the first observing tick would never have latched under the old
      window even absent the append-log bug.
    * ``expected_pid``, when given, is compared against the worker's own pid
      captured in the marker itself (``... pid=1234`` --
      ``GstPlayoutEngine._await_playing``'s ``os.getpid()``) -- a second,
      independent check that does not depend on the byte offset being
      right. A marker line with no ``pid=`` group (an older worker binary,
      or a hand-written test fixture) is accepted on offset evidence alone,
      since ``expected_pid`` has nothing to compare against.

    Missing/unreadable log -> False, same fail-closed default as no
    evidence at all."""

    text = _read_from_offset(path, offset)
    if text is None:
        return False
    for line in text.splitlines():
        match = _PLAYING_REACHED_RE.match(line)
        if match is None:
            continue
        pid_group = match.group("pid")
        if expected_pid is not None and pid_group is not None and int(pid_group) != expected_pid:
            continue
        return True
    return False


def worker_produced_output(path: Path, *, offset: int = 0, expected_pid: int | None = None) -> bool:
    """Return whether the CURRENT worker's stderr log shows real evidence it
    pushed at least one output buffer past the mux (see ``_FIRST_OUTPUT_RE``)
    -- the GStreamer-strategy on-air evidence ``EgressDaemon.
    _observed_on_air_evidence`` requires instead of ``worker_reached_playing``
    (item 84 Round-2 review BLOCKER).

    ``worker_reached_playing`` alone was NOT sufficient evidence of on-air
    output: reaching PLAYING (even ``NO_PREROLL``) is not proof a single
    buffer ever crossed the mux, and item 84 measured the exact consequence
    of treating it as such -- a worker that reaches PLAYING on every single
    relaunch but never actually produces output (at ANY
    ``first_output_timeout_s`` value, 65s through the 120s clamp ceiling)
    got its crash-loop streak reset by the alive-poll path on every cycle,
    and never escalated to fallback slate (streak pinned at 1) no matter how
    long the budget ran. This function requires the LATER, stronger
    ``CTRL first-output: ...`` marker instead -- printed by
    ``GstPlayoutEngine._maybe_print_first_output_marker`` exactly once, the
    moment the mux's pad probe actually observes a buffer.

    Same anchoring contract as ``worker_reached_playing`` (see that
    function's docstring for the full round-4 append-log rationale this
    mirrors exactly): ``offset`` anchors the read to the CURRENT worker's own
    spawn point so a PREVIOUS worker's marker (which always sits before that
    offset in the shared, append-mode, never-truncated-per-spawn log) is
    never read at all; ``expected_pid``, when given, is compared against the
    marker's own ``pid=`` group as a second, independent check that holds
    even if the byte offset were ever wrong. Unlike the legacy PLAYING
    marker's optional pid group (kept for older-binary/hand-written-fixture
    tolerance), this NEW marker always carries a pid -- every current
    printer of it supplies one -- but the regex still tolerates a fixture
    that omits it, matching on offset evidence alone in that case, same
    fail-open-on-missing-pid posture as the PLAYING check.

    Missing/unreadable log -> False, same fail-closed default as
    ``worker_reached_playing``."""

    text = _read_from_offset(path, offset)
    if text is None:
        return False
    for line in text.splitlines():
        match = _FIRST_OUTPUT_RE.match(line)
        if match is None:
            continue
        pid_group = match.group("pid")
        if expected_pid is not None and pid_group is not None and int(pid_group) != expected_pid:
            continue
        return True
    return False


def parse_ffmpeg_encoder_metrics_line(line: str) -> EgressEncoderMetrics:
    """Parse one FFmpeg progress/status line into health metrics."""

    fps = _parse_float(_FPS_RE.search(line))
    bitrate = _parse_bitrate_kbps(_BITRATE_RE.search(line))
    dropped = _parse_int(_DROP_RE.search(line))
    return EgressEncoderMetrics(
        encoder_fps=fps,
        encoder_bitrate_kbps=bitrate,
        dropped_frames=dropped,
    )


def _latest_metrics_from_text(text: str) -> EgressEncoderMetrics:
    """Fold every parseable FFmpeg progress line in ``text`` into the newest
    values seen for each field. Shared by ``read_latest_ffmpeg_encoder_metrics``
    (tail-window read) and ``read_ffmpeg_encoder_metrics_since`` (spawn-offset
    read) -- both just need "the newest value in whatever text I handed you",
    they differ only in which bytes of the log they hand over."""

    latest = EgressEncoderMetrics()
    for line in text.splitlines():
        parsed = parse_ffmpeg_encoder_metrics_line(line)
        latest = EgressEncoderMetrics(
            encoder_fps=parsed.encoder_fps
            if parsed.encoder_fps is not None
            else latest.encoder_fps,
            encoder_bitrate_kbps=parsed.encoder_bitrate_kbps
            if parsed.encoder_bitrate_kbps is not None
            else latest.encoder_bitrate_kbps,
            dropped_frames=parsed.dropped_frames
            if parsed.dropped_frames is not None
            else latest.dropped_frames,
        )
    return latest


def read_latest_ffmpeg_encoder_metrics(
    path: Path, *, tail_bytes: int = _TAIL_BYTES
) -> EgressEncoderMetrics:
    """Return the newest parseable FFmpeg metrics from a stderr log.

    Only the last ``tail_bytes`` of the file are read (D44) -- the newest
    values are all this returns, and the log is polled every couple of seconds
    for the whole life of a channel. A partial first line from cutting
    mid-line is harmless: it is parsed like any other line and simply
    contributes whichever fields survived the cut, and is superseded by every
    complete line after it.

    This is the SINK-HEALTH reader (``EgressDaemon._health_metrics`` /
    ``build_default_sink_health``) -- it deliberately keeps the tail-window
    semantics: sink health only ever wants "is the encoder moving media right
    now", so a previous worker's stale progress lines sitting earlier in an
    append-mode log are naturally superseded by the current worker's own
    newer lines by the time either one prints anything.  For on-air EVIDENCE
    (a new worker must never be credited with a previous worker's progress
    before it prints any of its own) use ``read_ffmpeg_encoder_metrics_since``
    instead -- see that function's docstring for the round-4 BLOCKER this
    split closes.
    """

    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            if tail_bytes >= 0 and size > tail_bytes:
                handle.seek(size - tail_bytes)
            else:
                handle.seek(0)
            raw = handle.read()
    except OSError:
        # Missing, locked, or unreadable: no metrics, same as before.
        return EgressEncoderMetrics()
    return _latest_metrics_from_text(raw.decode("utf-8", errors="replace"))


def read_ffmpeg_encoder_metrics_since(path: Path, *, offset: int = 0) -> EgressEncoderMetrics:
    """Return the newest parseable FFmpeg metrics printed AT OR AFTER
    ``offset`` bytes into ``path`` -- the FFmpeg-side on-air evidence
    counterpart to ``worker_reached_playing``'s spawn anchor.

    Round-4 (PR #183 review, BLOCKER reproduced): ``_ffmpeg.py`` opens the
    channel's fixed ``ffmpeg.stderr.log`` in APPEND mode and never truncates
    it per spawn (same root cause as the GStreamer side). Before this split,
    ``EgressDaemon._observed_on_air_evidence`` read this log with
    ``read_latest_ffmpeg_encoder_metrics``'s tail window, which could still
    surface a PREVIOUS worker's stale fps/bitrate lines as "real progress"
    for a brand-new, not-yet-encoding worker whenever those stale lines
    happened to fall inside the tail window. Anchoring to the current
    worker's own spawn offset (``EgressDaemon._stderr_spawn_offset``, via
    ``_read_from_offset``) closes that the same way the GStreamer marker
    check is closed: a previous worker's lines sit before the offset and are
    never read at all.
    """

    text = _read_from_offset(path, offset)
    if text is None:
        return EgressEncoderMetrics()
    return _latest_metrics_from_text(text)


def build_default_sink_health(
    *,
    config: EgressConfig,
    metrics: EgressEncoderMetrics,
    state: EgressState,
) -> dict[str, bool]:
    """Build conservative sink health when no transport-specific provider exists.

    External sinks need receiver or transport acknowledgement from a provider.
    Local file-like sinks can be treated as locally healthy once the encoder is
    configured, because they do not have a far-end connection to prove.

    Audit QA-004 (S8): the progress check is **state-aware**. A fire-and-forget UDP
    sink is judged "connected" by encoder progress ONLY when ``state == "ON_AIR"`` —
    then no progress is a real, observable stall (False). When the channel is idling
    on a slate (or otherwise not on air) the encoder emits no/stale fps/bitrate and
    there is no far-end to disprove, so the sink is healthy by default (True). Before
    this fix, a stale ``fps=0`` line on a slating channel flipped a TSDuck-clean UDP
    sink to ``False`` for hours, training operators to ignore the flag.
    """

    require_progress = state == "ON_AIR"
    metrics_available = metrics.encoder_fps is not None or metrics.encoder_bitrate_kbps is not None
    if require_progress:
        # On air: a UDP sink is connected iff the encoder is actually moving media;
        # on air with no measurable progress is a genuine problem worth surfacing.
        udp_ok = encoder_has_progress(metrics) if metrics_available else False
    else:
        # Idle on slate / not on air: no far-end to disprove → healthy by default.
        udp_ok = True
    health: dict[str, bool] = {}
    for sink in config.sinks:
        if sink.kind == "file":
            health[sink.label] = True
            continue
        if sink.kind == "local-ts" and urlsplit(sink.uri).scheme.lower() == "file":
            health[sink.label] = True
            continue
        health[sink.label] = False if sink.kind in {"srt", "rtmp", "sdi"} else udp_ok
    return health


def encoder_has_progress(metrics: EgressEncoderMetrics) -> bool:
    """Return whether FFmpeg metrics show active media movement."""

    return (
        metrics.encoder_fps is not None
        and metrics.encoder_fps > 0
        and metrics.encoder_bitrate_kbps is not None
        and metrics.encoder_bitrate_kbps > 0
    )


def _parse_float(match: re.Match[str] | None) -> float | None:
    if match is None:
        return None
    return float(match.group("value"))


def _parse_int(match: re.Match[str] | None) -> int | None:
    if match is None:
        return None
    return int(match.group("value"))


def _parse_bitrate_kbps(match: re.Match[str] | None) -> float | None:
    if match is None:
        return None
    value = float(match.group("value"))
    unit = match.group("unit").lower()
    if unit.startswith("mbits"):
        return value * 1000
    if unit.startswith("bits"):
        return value / 1000
    return value
