# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Live-HLS relay for the GStreamer engine: real segments + a real manifest.

DEFECT A (found live, staff-token repro): the GStreamer sink bridge
(``civiccast.egress.gst.bridge``) accepted the ``hls`` sink kind and crashed
the channel on ``start`` -- ``EgressSinkKind`` advertised it, the config API
accepted it with 200 OK, and ``sink_element_spec`` had no branch for it. This
module is the other half of the real fix: it makes an ``hls`` sink on the
GStreamer engine genuinely produce a servable manifest + segments.

**Current architecture.** The current closure includes ``hlssink3``. The relay
remains responsible for caption-preserving HLS, publication/segment lifetime
and restart recovery; plugin availability alone does not replace those paths.
See ``docs/ops/sprint6-gstreamer-alternatives.md`` for the current decision.

**Historical runtime limitation.** The task that opened this defect
assumed the shipped runtime carries ``hlssink2``. It does not — verified
empirically against the real installed closure
(``C:\\Program Files\\CivicCast (Native)\\runtime\\dependencies\\gstreamer\\lib\\gstreamer-1.0``):
``gst-inspect-1.0`` lists no ``hlssink``/``hlssink2``/``hlssink3`` element at
all (no ``gsthls*.dll`` was shipped then) and no ``splitmuxsink``/``multifilesink``
(no ``gstmultifile.dll``). The only HLS-shaped element present is
``avmux_hls`` (gst-libav wrapping FFmpeg's HLS muxer, rank ``marginal``), and
a live pipeline test (``avmux_hls ! filesink``, both audio+video fed, run to
EOS) wrote **zero files** — gst-libav's generic muxer wrapper funnels all
output through one src pad and cannot drive FFmpeg's own multi-file segment
I/O, a known limitation of that wrapper. So no *pure* GStreamer element
chain can produce real HLS output with this runtime's plugin set; a branch
that just returns an ``ElementSpec`` naming one of those elements would look
fixed and still not serve residents anything.

**What actually works.** ``civiccast.egress.sinks.HlsSink`` already writes
correct, sliding-window live HLS (``playlist.m3u8`` + ``seg%09d.ts``, 2s
segments, a 12s/6-segment window) via FFmpeg's real ``-f hls`` muxer for the
legacy ffmpeg-concat engine, and ``civiccast.stream.media_router``'s
``/media/live/{channel_id}/...`` route already serves exactly that directory
layout (resolved from the channel's ``hls`` sink URI). That combination is
proven; this module makes the GStreamer engine land bytes there too, instead
of reinventing the muxer:

    GStreamer program mux (already-proven MPEG-TS branch)
        --> udp://127.0.0.1:<relay-port>            (an ordinary local-ts sink)
    HlsRelaySupervisor's supervised ffmpeg child
        -i udp://127.0.0.1:<relay-port> ! HLS output with publisher-owned pruning
        --> <hls sink's configured directory>/seg%09d.ts
                                              + /playlist.mux.m3u8   (private)
    HlsRelaySupervisor's manifest publisher thread (U51)
        playlist.mux.m3u8 --in-place rewrite--> <dir>/playlist.m3u8   (advertised)

The relay removes FFmpeg's independent delete_segments flag. The publisher
keeps files named by both the public and staging playlists, then prunes a
retired segment only after the last public playlist that named it could have
expired (that playlist's measured EXTINF duration plus the segment's measured
duration). Unadvertised files older than the current staging window are
eligible for pruning even if publication is stalled, which bounds hidden mux
output.

``civiccast.stream.media_router`` resolves the channel's *configured* ``hls``
sink URI (the directory), not the internal relay plumbing, so the rewritten
in-memory ``local-ts`` sink this module hands the graph builder is invisible to
every reader except the graph. It snapshots the advertised playlist bytes under
the publisher's in-process lock, then closes the file before response delivery;
segments keep their existing file response path.

Mirrors ``civiccast.egress.ts_relay.TsRelaySupervisor`` in shape (guarded
per-channel supervised co-process dict, idempotent ``apply``/``stop_channel``/
``stop_all``) so the two relays read the same at a glance.

**Beta.10 live finding (2026-09-23): process-alive != emitting.** A relay
ffmpeg child can stay a live pid while its ``playlist.m3u8`` stops advancing
(the live government channel froze at ``seg000001130.ts`` for minutes while
both the relay pid and the main encoder's ``CTRL output`` counters kept
running). ``is_alive`` alone cannot see that, so the daemon kept reporting the
``hls`` sink ``connected`` off the MAIN encoder's UDP progress. The progress
half below adds an mtime+segment-identity check and a bounded, opt-in
self-heal. It deliberately does NOT diagnose the upstream UDP starvation that
stops ffmpeg writing; it only makes a stalled-but-alive relay truthful and
recoverable.

**Beta.10 live finding (2026-09-24, U12): a (re)start can lock the sink to
audio-only.** The government relay was restarted mid-stream by the bounded
stall self-heal above (18:46:32, right after the channel cut to its fallback
slate for a schedule gap). From then on its newest segments probed as AUDIO
ONLY while the public/education relays stayed H264+AAC -- for hours, until the
channel itself was restarted; the same shape hit education at 13:39:35 and
government at 15:49:21 that day. Mechanism (reproduced with real ffmpeg
against a live UDP input): ffmpeg fixes its OUTPUT stream set at probe time,
so a child that starts while its input carries no video inside the probe
window builds an audio-only output and never adds video when video arrives.
The widened 6M probe only narrows the window in which that can happen; it
cannot remove it, and it does not notice that the result is unwatchable.

**The same lock runs the other way (coordinator audit, U12 fix).** An output
whose stream set is fixed at probe time can just as easily be fixed to
video-only: U13 measured the government relay's post-self-heal segments at
18:47:44-18:47:56 carrying TS PID 256 (video) and no PID 257 (audio) at all.
A municipal channel with picture and no sound fails acceptance exactly like
one with sound and no picture, so the rule below is symmetric over the stream
kinds the sink is configured to carry:

1. the relay's output mapping REQUIRES every kind the sink carries
   (:func:`_relay_map_args`), so a child that probes an input missing any of
   them fails fast instead of silently serving a half-program window -- and the
   supervisor's bounded restart then retries until the program is whole;
2. the post-(re)start verification (:meth:`HlsRelaySupervisor.
   maybe_restore_missing_streams`) requires BOTH a video and an audio stream in
   the newest served segment for a sink that carries both, and judges each kind
   it actually requires.

A sink that carries only one kind is judged only on that kind, and an
audio-only sink (``EgressSink.carries_video`` False) keeps its historical argv
and is never judged at all. Today every shipped sink carries both; the
``carries_video``/``carries_audio`` flags exist so a future one can say
otherwise (see ``civiccast/egress/sinks.py``).

**Beta.10 live finding (2026-09-25, U24): the relay child's stderr went to the
null device, so two incidents left no evidence.** (1) On slate entry at
00:51:26 the education channel's output went +19.75 s A/V out of sync *inside
the relay*; U21 could not decide where the jump is born because the child's own
``timestamp discontinuity``/``Non-monotonous DTS`` lines had been discarded.
(2) At 01:04 a freshly restarted government relay wrote nothing for ~2 minutes
while the engine was producing; U21 recorded INFERRED ("its input was not
delivering") with no child-side evidence. ``_default_starter`` called
``start_ffmpeg(args)`` with no ``stderr_path``, which is
``subprocess.DEVNULL``.

The supervisor now captures each child's stderr into a bounded, per-(channel,
sink) file next to the channel's other egress logs:

* **path** — ``<log_root>/<channel_id>/logs/hls-relay.<sink label>.stderr.log``
  where ``log_root`` is the daemon's ``work_dir`` (the same directory the
  encoder's ``ffmpeg.stderr.log``/``gst-worker.stderr.log`` live under). On an
  installed station that is
  ``C:\\ProgramData\\CivicCast\\data\\egress\\<channel_id>\\logs\\``.
* **one header line per spawn** — ``[hls-relay] pid=… utc=… channel=… sink=…
  spawn=<ordinal> reason=<start|rebind|uri-change|respawn-dead|self-heal|
  stream-restore> argv=[…]``, written before the child starts and stamped with
  the pid in place once it does. ``reason`` plus the spawn ordinal is this
  layer's session identity: the relay is (re)bound *before* the worker that
  will feed it exists, so no worker pid/session id is knowable here.
* **bounded** — current + one previous (``…log.1``), rotated at spawn only,
  each ≤ :data:`HLS_RELAY_LOG_CAP_BYTES` (5 MiB); a file over the cap is
  rewritten to its header plus the newest
  :data:`HLS_RELAY_LOG_TAIL_BYTES` (the drain thread does this itself, and
  :meth:`HlsRelaySupervisor.maybe_trim_logs` retries it on each poll).
  The numbers are measured, not assumed: the default level was run against a
  fixture built to reproduce the live backward-PTS jump and produced ~1.4 KiB
  per ``timestamp discontinuity`` event, so 5 MiB is ~3,700 events and the
  worst-case retained set is 30 MiB on a three-channel station -- an 8-hour run
  cannot fill a disk. Raw table and method:
  ``civiccast-ds-oversight/staging/U24/evidence/loglevel/summary.json``.
* **the parent owns the file (U24.1)** — the child's stderr goes to a pipe and
  one daemon thread per child drains it into the log. The first cut gave the
  child a real append-mode file handle and had the parent trim that file in
  place; on Windows that cannot bound anything, because an inherited handle
  keeps its own position, so the child's next write lands at its old offset and
  the gap is zero-filled. Measured against the real starter: a 5,663-byte log
  rewritten to 2,007 bytes was 5,882 bytes a second later, with 3,656 NULs. See
  :class:`_RelayLogWriter`.
* **Windows** — a file another handle holds open cannot be renamed
  (``WinError 32``; Python opens with share read+write, no share delete), so
  rotation still happens at spawn, after the predecessor has been terminated
  and its drain thread closed (the child never holds the file at all now).
  Neither the live child's own logging nor the relay's replaceability depends on
  the log file: the drain thread discards on any write error rather than
  stalling the child.

**Beta.10 live finding (2026-09-26, U51): a reader holding the manifest froze
every channel, and every start inherited the previous session's window.** Two
robustness defects, one commit each.

*Item 1 — fossil segments.* Nothing ever cleared a channel's HLS directory, and
because the muxer is spawned with ``append_list`` the next child CONTINUES the
sequence and playlist it finds there. Measured on the live station: 35-43 stale
``seg*.ts`` per channel, accumulated since 09-19 across every restart, all of
them sitting in front of the fresh window. The fix is a wipe at SPAWN, before
the child exists (see :func:`_clear_hls_window`); locked files are skipped and
logged, never fatal.

*Item 2 — a held manifest froze publishing, silently.* ffmpeg's hls muxer
publishes ``playlist.m3u8`` temp-file-then-RENAME on every update, and a rename
needs DELETE access to the existing file. At 10:36:53 one reader holding ONE
channel's manifest denied that rename and ALL THREE channels' manifests stopped
advancing for viewers for ~50 s, while every relay child stayed a live pid and
every segment kept advancing and not one stderr line mentioned error, fail or
denied. Measured on a replay with the station's own ffmpeg (commands and raw
output in ``civiccast-ds-oversight/reports/U51.md``): the rename is blocked
46 of 47 attempts against the reader shape that blocks it, ``+temp_file``
cannot avoid a rename that already happens unconditionally on this build, a
FRESH child pointed at the held directory stalls identically (so no relay-side
watchdog that restarts only the child could end it), and an IN-PLACE rewrite of
the held file succeeds 47 of 47 -- the operation needs write access, not delete
access. So the
muxer is given a private staging name no reader knows
(``HlsSink.mux_playlist_name``) and :class:`_ManifestPublisher` republishes the
advertised name from it in place. The served name did not change: readers, the
router and the daemon's stale-playlist discard still see ``playlist.m3u8``.
"""

from __future__ import annotations

import codecs
import contextlib
import hashlib
import io
import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, RLock, Thread
from typing import IO, Protocol

from civiccast.egress.models import EgressConfig, EgressSinkSpec
from civiccast.egress.relay_reclaim import (
    PortOwnerApi,
    PsutilPortOwnerApi,
    decide_reclaim,
    relay_log_shows_bind_failure,
    udp_port_of,
)
from civiccast.egress.sinks import HlsSink
from civiccast.stream import live_manifest_lock
from civiccast.stream._ffmpeg import FfmpegNotFoundError, FfmpegProcessHandle, start_ffmpeg

_LOG = logging.getLogger(__name__)

_HLS_SEGMENT_NAME = re.compile(r"seg\d{9}\.ts\Z")


@dataclass(frozen=True)
class _ClearedHlsWindow:
    """Prior advertised references and bounded retirement state for a fresh relay."""

    published_segments: dict[str, float]
    retired_segment_names: frozenset[str]
    retired_grace_s: float
    next_start_number: int | None


def _playlist_segment_durations(data: bytes) -> dict[str, float] | None:
    """Return this local HLS playlist's safe segment names and EXTINF durations.

    ``None`` means malformed or unsupported input. The publisher uses this
    before it emits or prunes anything, so an untrusted URI can never turn into
    a path outside the sink directory and a partial muxer write cannot retire
    files based on an incomplete window.
    """
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    if not lines or lines[0].strip() != "#EXTM3U":
        return None

    durations: dict[str, float] = {}
    pending_duration: float | None = None
    for raw_line in lines[1:]:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            if pending_duration is not None:
                return None
            try:
                duration = float(line.removeprefix("#EXTINF:").split(",", 1)[0])
            except ValueError:
                return None
            if not math.isfinite(duration) or duration <= 0:
                return None
            pending_duration = duration
            continue
        if line.startswith("#"):
            continue
        if (
            pending_duration is None
            or Path(line).name != line
            or _HLS_SEGMENT_NAME.fullmatch(line) is None
            or line in durations
        ):
            return None
        durations[line] = pending_duration
        pending_duration = None
    if pending_duration is not None:
        return None
    return durations


def _relay_hls_output_args(hls_sink: HlsSink, *, start_number: int | None = None) -> list[str]:
    """Give segment deletion to the publisher that owns the served playlist."""
    args = hls_sink.output_args()
    flags_index = args.index("-hls_flags") + 1
    flags = [flag for flag in args[flags_index].split("+") if flag != "delete_segments"]
    if "temp_file" not in flags:
        flags.append("temp_file")
    args[flags_index] = "+".join(flags)
    if start_number is not None:
        args[-1:-1] = ["-start_number", str(start_number)]
    return args


#: U24 per-relay-child stderr capture. Two files are kept per (channel, sink):
#: the current child's and its immediate predecessor's, rotated at spawn.
HLS_RELAY_LOG_PREFIX = "hls-relay."
HLS_RELAY_LOG_SUFFIX = ".stderr.log"
#: Hard cap for ONE of those files, 5 MiB.
#:
#: Derivation (measured, see ``civiccast-ds-oversight/staging/U24/evidence/loglevel/summary.json``):
#: ffmpeg runs at its default ``info`` level -- no ``-loglevel`` is passed --
#: and a fixture built to reproduce the live backward-PTS jump (one segment
#: concatenated with itself, fed to this module's real argv) produced 5,276
#: bytes total for one jump and 30,744 for nineteen, i.e. ~1.4 KiB per
#: ``timestamp discontinuity`` event once the fixed startup banner is divided
#: out. 5 MiB is therefore ~3,700 such events. A child would have to log one
#: every ~8 s for a full 8-hour run to reach the cap, and a healthy child logs
#: none at all, so the cap only ever engages on a genuinely storming child --
#: which is what it is for. The retained set is 2 files x 5 MiB per
#: (channel, sink); a three-channel station with one sink each holds at most
#: 30 MiB of relay logs, so an 8-hour run cannot fill a disk.
HLS_RELAY_LOG_CAP_BYTES = 5 * 1024 * 1024
#: What an over-cap file is trimmed down to on the next poll: the spawn header
#: (the argument/session evidence) plus this much of the newest output (the
#: incident's tail). At the same measured ~1.4 KiB per event, 1 MiB keeps
#: several hundred events while bounding the rewrite cost of each trim.
HLS_RELAY_LOG_TAIL_BYTES = 1024 * 1024
#: Longest sink-label stem kept in a log filename; the label reaches us from
#: operator config, so it is sanitized and bounded before it becomes a path.
HLS_RELAY_LOG_STEM_MAX = 40
#: Fixed-width pid field in the spawn header. The header is written BEFORE the
#: spawn (so a spawn that never returns still has its evidence), then the pid
#: is stamped over this field in place. 10 digits covers every Windows pid.
_LOG_PID_FIELD = "pid="
_LOG_PID_WIDTH = 10
_LOG_PID_PLACEHOLDER = "-" * _LOG_PID_WIDTH

_PORT_BASE_ENV = "CIVICCAST_HLS_RELAY_BASE_PORT"
#: ffprobe binary name, resolved from PATH at probe time. Declared locally
#: rather than importing ``civiccast.stream._ffmpeg``'s private
#: ``_FFPROBE_EXECUTABLE``: this module already owns its own ffmpeg argv.
_FFPROBE_EXECUTABLE = "ffprobe"
_DEFAULT_PORT_BASE = 18_000
_PORT_RANGE = 500
_UDP_INPUT_ARGS = (
    "-fflags",
    "+genpts",
    # Widened start-time probe window (beta.10 HLS video-loss): with 2,000,000
    # a relay that starts while its loopback UDP TS is audio-only can lock to an
    # audio-only stream inventory and keep emitting AAC-only HLS even after
    # H.264 video arrives (isolated synthetic A/B: 2M -> AAC-only; 6M -> H264/AAC).
    # 6,000,000 is the smallest tested value that restores video; larger values
    # also work but increase time-to-first-segment. NOTE: this is proven only in
    # isolation on a synthetic delayed-video TS; it is NOT proof that live worker
    # loopback input was audio-only at relay start, nor that it fixes the live
    # symptom.
    "-analyzeduration",
    "6000000",
    "-probesize",
    "6000000",
)
#: How long a live relay may go with an UNCHANGED last segment + playlist
#: before the window is reported (and optionally healed) as stalled. The live
#: symptom was minutes, so 30s is far above a healthy 2s segment cadence and
#: every real muxer hiccup, while still bounding a visible freeze.
_DEFAULT_STALL_BOUND_S = 30.0


#: Output-stream mapping for a relay child (U12).
#:
#: ``-map 0:v:0`` (and ``-map 0:a:0`` for a sink that carries audio) is
#: deliberately NOT optional: ffmpeg fixes its output stream set at probe
#: time, so a child that starts while the loopback input is missing a kind
#: builds a half-program HLS output and never adds the missing kind when it
#: arrives (the live government symptom in both directions -- audio-only at
#: 18:46:32, video-only at 18:47:44-18:47:56). Requiring the map turns that
#: silent half-window into a prompt child failure -- measured against real
#: ffmpeg 8.1.1: ``Stream map '0:v:0' matches no streams`` + "Error opening
#: output files", exit -22, no window written -- which the supervisor's
#: bounded restore below then retries until the program is whole.
#:
#: With ANY ``-map`` present ffmpeg's default stream selection is disabled, so
#: every kind the sink carries must be named explicitly. A kind the sink does
#: not carry is left optional (``?``) so a one-kind input stays relay-able
#: instead of fatal, and an audio-only sink keeps the historical mapping-less
#: argv byte-for-byte. Proven with real ffmpeg in
#: ``tests/egress/test_hls_relay_video_lock.py``.
def _relay_map_args(*, carries_video: bool, carries_audio: bool) -> tuple[str, ...]:
    """The ``-map`` arguments that force this sink's required stream kinds."""
    if not carries_video:
        # Audio-only sink: the historical argv, no -map at all (and ffmpeg's
        # default selection still picks the audio stream).
        return ()
    if not carries_audio:
        # Video-only sink: audio stays optional so an audio-less input is
        # relay-able rather than fatal.
        return ("-map", "0:v:0", "-map", "0:a:0?")
    return ("-map", "0:v:0", "-map", "0:a:0")


#: U12 restart cadence for a relay that cannot carry the program's required
#: stream kinds. The first ``_STREAM_FAST_ATTEMPTS`` restarts are prompt (the
#: missing kind is usually a slate/source transition away), then the cadence
#: drops to ``_DEFAULT_STREAM_RETRY_SLOW_DELAY_S`` and each further attempt
#: logs at ERROR -- a sink that can never satisfy the requirement must not
#: restart-storm, but must keep trying and keep saying so.
_DEFAULT_STREAM_RETRY_DELAY_S = 5.0
_DEFAULT_STREAM_RETRY_SLOW_DELAY_S = 60.0
_STREAM_FAST_ATTEMPTS = 3
#: Bound on one segment's ffprobe (U12 verification). A served 2s segment is
#: read locally, so this only ever bounds a hung probe.
_SEGMENT_PROBE_TIMEOUT_S = 10.0


def hls_relay_uri_for(sink_uri: str, *, base_port: int | None = None) -> str:
    """Deterministic loopback URI for one hls sink's GStreamer->ffmpeg relay tap.

    Pure function of ``sink_uri`` (the sink's configured directory) so
    ``sink_element_spec`` (a pure, side-effect-free element-graph builder) and
    :class:`HlsRelaySupervisor` (which owns the actual relay subprocess) agree
    on the same port without sharing mutable state. Collisions are possible in
    principle (two channels' directories hashing to the same offset) but the
    500-port range makes that vanishingly unlikely for a station's channel
    count, and a collision would surface immediately as a health/proof
    mismatch rather than silently — same acceptable-risk posture as this
    codebase's other hash-derived allocations.
    """
    if base_port is not None:
        base = base_port
    else:
        base = int(os.environ.get(_PORT_BASE_ENV, "").strip() or _DEFAULT_PORT_BASE)
    digest = hashlib.sha256(sink_uri.encode("utf-8")).digest()
    offset = int.from_bytes(digest[:2], "big") % _PORT_RANGE
    return f"udp://127.0.0.1:{base + offset}"


def _playlist_path_for(sink_uri: str) -> Path:
    """The manifest path this sink's relay serves (mirrors ``HlsSink``).

    U51: the ADVERTISED manifest -- the one viewers, the router and the CDN
    publisher read -- not the muxer's private staging playlist. Progress and
    health must be judged on what residents actually get: watching the staging
    file instead would report a healthy relay while a wedged publisher served
    a frozen window.
    """
    sink = HlsSink(EgressSinkSpec(kind="hls", label="_progress", uri=sink_uri))
    return Path(sink.manifest_target())


def _clear_hls_window(hls_sink: HlsSink) -> _ClearedHlsWindow:
    """Withdraw the old playlist and seed a fresh publisher's retirement state.

    U51 item 1. Nothing ever cleared that directory: the muxer's own
    ``delete_segments`` only prunes behind ITS OWN window, so every relay start
    left its final window on disk, and a station restarted a dozen times since
    09-19 carried 35-43 stale ``seg*.ts`` per channel. Worse than wasted disk,
    the next child is spawned with ``append_list`` -- it CONTINUES the sequence
    and the playlist it finds there, so the fossils are what a fresh session
    appends behind.

    Fresh/dead cleanup happens at the SPAWN, before the child exists (never at
    ``apply`` time, which is idempotent and re-entered for a live relay). The
    advertised playlist is withdrawn under the same lock used by HTTP readers
    and the publisher. Every safe numeric segment is retained: references from
    the old playlist get their measured lifetime, while unreferenced numeric
    files get one conservative window of grace in case a prior snapshot still
    names them. The private staging playlist and ``*.tmp`` debris are cleared.
    The new child starts beyond all retained segment numbers so it cannot
    overwrite a file an old client uses.

    A file locked by an external reader can NEVER fail the start: on Windows a
    held file blocks its unlink (``WinError 32``), and a relay that refused to
    start because a viewer held a segment would trade a leaked file for a dead
    channel. Locked files are skipped, the rest are still cleared, and the skip
    is logged.
    """
    directory = Path(hls_sink.manifest_target()).parent
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(hls_sink.manifest_target())
    previous_segments: dict[str, float] = {}
    retired_segment_names: set[str] = set()
    removed = 0
    locked: list[Path] = []

    # Withdraw before unlinking any media. A route already copying the old
    # playlist finishes first; later route reads wait and then see no playlist.
    with live_manifest_lock.LIVE_MANIFEST_LOCK:
        try:
            previous_data = manifest_path.read_bytes()
        except FileNotFoundError:
            previous_segments = {}
        except OSError:
            previous_segments = {}
        else:
            parsed_segments = _playlist_segment_durations(previous_data)
            previous_segments = {} if parsed_segments is None else parsed_segments

        try:
            manifest_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            locked.append(manifest_path)
        else:
            removed += 1

        candidates: list[Path] = []
        for pattern in ("seg*.ts", "*.tmp", hls_sink.mux_playlist_name):
            try:
                candidates.extend(sorted(directory.glob(pattern)))
            except OSError:
                continue

        highest_segment: int | None = None
        for path in candidates:
            segment_match = _HLS_SEGMENT_NAME.fullmatch(path.name)
            if segment_match is not None:
                sequence = int(path.name[3:12])
                highest_segment = max(highest_segment or sequence, sequence)
                if path.name in previous_segments:
                    continue
                # Keep numeric files for a bounded grace. They may be named by
                # a playlist snapshot that was served before this withdrawal,
                # even when they were already absent from the last manifest.
                retired_segment_names.add(path.name)
                continue
            try:
                if not path.is_file():
                    continue
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                locked.append(path)
            else:
                removed += 1

    if removed:
        _LOG.info(
            "HLS relay start: withdrew the old playlist and cleared %d stale file(s) from %s.",
            removed,
            directory,
        )
    if locked:
        _LOG.warning(
            "HLS relay start: could not remove %d file(s) from %s (held by another "
            "process; they are left in place and the relay starts with a new segment "
            "number anyway): %s.",
            len(locked),
            directory,
            ", ".join(str(path) for path in locked),
        )
    measured_previous_grace_s = sum(previous_segments.values()) + max(
        previous_segments.values(), default=0.0
    )
    one_window_grace_s = (
        hls_sink.playlist_size * hls_sink.segment_seconds + hls_sink.segment_seconds
    )
    return _ClearedHlsWindow(
        published_segments=previous_segments,
        retired_segment_names=frozenset(retired_segment_names),
        retired_grace_s=max(measured_previous_grace_s, one_window_grace_s),
        next_start_number=None if highest_segment is None else highest_segment + 1,
    )


def _last_segment(path: Path) -> str | None:
    """The final ``.ts`` name in a playlist, or None if there is no window yet.

    This is the ONLY signal used for progress. Manifest SIZE is deliberately
    excluded: ``#EXT-X-MEDIA-SEQUENCE`` / ``PROGRAM-DATE-TIME`` / tag edits can
    change the byte count while the last segment stays identical, and treating
    that as progress would falsely reset the freshness clock on a frozen
    window (audit finding 1). ``mtime`` is excluded for the same reason.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    last_segment = ""
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.endswith(".ts"):
            last_segment = stripped
            break
    return last_segment or None


def _last_complete_segment(path: Path) -> str | None:
    """The newest segment of a playlist that has stopped growing, or None.

    ``_last_segment`` answers "which entry is the window's newest", which is what
    progress tracking wants. THIS answers "which entry is CLOSED", which is what
    MEASURING wants: on a live window the muxer publishes the final entry's name
    the moment it opens that file, so the final ``.ts`` is still being appended
    to and only its first packets are settled. The entry before it is therefore
    the newest COMPLETE one. Under ``#EXT-X-ENDLIST`` the window is closed and
    its final entry is itself complete -- which is also what a closed-window
    writer means by publishing the tag -- so that entry is used directly.

    Correct under either playlist-write behaviour: a writer that publishes an
    entry only once it is closed makes the second-to-last merely one segment
    older -- still a complete, measurable segment, just not the newest.

    Same "no answer" contract as its siblings below: an unreadable playlist, a
    window with no ``.ts`` entry yet, and a live window holding only ONE entry
    (that entry is the one being written) are all ``None`` -- ignorance, never a
    fault.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    closed = False
    segments: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXT-X-ENDLIST"):
            closed = True
        elif stripped.endswith(".ts"):
            segments.append(stripped)
    if not segments:
        return None
    if closed:
        return segments[-1]
    if len(segments) < 2:
        return None
    return segments[-2]


def _segment_stream_kinds(path: Path) -> frozenset[str] | None:
    """Which stream kinds (``video``/``audio``) does THIS served segment carry?

    Tri-state by presence (U12): a set is the answer ffprobe actually gave --
    possibly the empty set, which is itself a positive finding -- and ``None``
    means "no answer": the segment is not on disk yet (a window can name a
    segment ffmpeg is still closing), ffprobe is absent, or it failed/timed
    out. ``None`` never counts as a fault: the supervisor only restarts a
    relay on POSITIVE evidence that its output is missing a kind, so a broken
    or missing probe tool cannot itself become a restart source.

    A set rather than a bool because the requirement is symmetric over the
    kinds the sink carries: U13 measured a live relay serving video-only
    segments (TS PID 256, no PID 257), the mirror image of the audio-only
    lock, and a bool would have to be asked twice and conjoined.

    Set rather than the module-local reuse of
    ``civiccast.stream._ffmpeg.probe_video_dimensions`` (which answers ``None``
    for both "no video" and "could not tell") because the two must not be
    conflated here: one is the live fault, the other is ignorance.
    """
    if not path.is_file():
        return None
    if shutil.which(_FFPROBE_EXECUTABLE) is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603
            [
                _FFPROBE_EXECUTABLE,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SEGMENT_PROBE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if completed.returncode != 0:
        return None
    return frozenset(line.strip() for line in completed.stdout.splitlines() if line.strip())


def _seconds_from_pts_time(value: object) -> float | None:
    """One packet's ``pts_time`` as float seconds, or None if ffprobe gave none.

    ffprobe's JSON prints ``pts_time`` as a STRING (``"0.033367"``) and as
    ``"N/A"`` for a packet whose timestamp it could not express; neither the
    string form nor the unparsable one is a fault here.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _segment_first_packet_pts(path: Path) -> tuple[float, float] | None:
    """``(video_pts, audio_pts)`` of THIS segment's first packets, or None.

    BETA.10 U16 item B: the output-side A/V check measures the RELATIONSHIP
    between the two streams, so a one-kind segment is not a measurement at all
    -- a single kind has nothing to be out of step WITH. Every other outcome is
    also ``None``: the segment is not on disk, ffprobe is absent, the probe
    failed or timed out, its JSON did not parse, or a stream's first packet
    carried no usable timestamp. Same tri-state contract as
    ``_segment_stream_kinds``: ``None`` is ignorance, never a fault, and never
    on its own a reason to restart anything. The caller is responsible for the
    policy (how big a difference is a desync, how many probes in a row matter).

    Per kind this is the first packet, in file order, whose timestamp ffprobe
    could express: a first packet it could not timestamp is skipped rather than
    discarding the whole measurement, which cannot mask a desync of seconds
    (segments here are ~2s long and a late stream's first packet is the one the
    desync is measured from).

    ONE ffprobe call answers both kinds, because two calls could straddle a
    window roll and report two different segments' first packets as if they
    belonged together. That is why this asks for JSON rather than the flat
    ``csv`` of ``_segment_stream_kinds``: the answer pairs a stream identity
    with a timestamp, which a column-order-dependent csv would have to be read
    back positionally.
    """
    if not path.is_file():
        return None
    if shutil.which(_FFPROBE_EXECUTABLE) is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603
            [
                _FFPROBE_EXECUTABLE,
                "-v",
                "error",
                "-show_entries",
                "stream=index,codec_type:packet=stream_index,pts_time",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SEGMENT_PROBE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None

    kinds: dict[int, str] = {}
    streams = payload.get("streams")
    if isinstance(streams, list):
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            index = stream.get("index")
            kind = stream.get("codec_type")
            if isinstance(index, int) and isinstance(kind, str):
                kinds[index] = kind

    first: dict[str, float] = {}
    packets = payload.get("packets")
    if isinstance(packets, list):
        for packet in packets:
            if not isinstance(packet, dict):
                continue
            index = packet.get("stream_index")
            if not isinstance(index, int):
                continue
            kind = kinds.get(index)
            if kind not in {"video", "audio"} or kind in first:
                continue
            seconds = _seconds_from_pts_time(packet.get("pts_time"))
            if seconds is None:
                continue
            first[kind] = seconds

    if "video" not in first or "audio" not in first:
        return None
    return (first["video"], first["audio"])


def _sink_required_kinds(source_uri: str) -> frozenset[str]:
    """The stream kinds this ``hls`` sink's program must carry (U12).

    Empty means "never judged": an audio-only sink (``carries_video`` False)
    keeps its historical argv and the supervisor must not restart it for a
    stream it was never configured to carry. Otherwise the requirement is
    every kind the sink is declared to carry -- both video AND audio for every
    sink shipped today (see ``EgressSink`` in ``civiccast/egress/sinks.py``).
    """
    sink = HlsSink(EgressSinkSpec(kind="hls", label="_kinds", uri=source_uri))
    if not sink.carries_video:
        return frozenset()
    required = {"video"}
    if sink.carries_audio:
        required.add("audio")
    return frozenset(required)


def _describe_kinds(kinds: frozenset[str]) -> str:
    """Stable operator-facing wording for a kind set (``video+audio``, ``audio``)."""
    return "+".join(sorted(kinds))


def _relay_log_component(value: str, fallback: str) -> str:
    """Turn a config-supplied string into one safe path component.

    The value comes from channel config, so it is treated as hostile: anything
    outside ``[A-Za-z0-9._-]`` collapses to ``_``, and leading/trailing dots,
    underscores and dashes are stripped so it can never produce a ``..`` (or a
    dot-prefixed) component. Bounded to :data:`HLS_RELAY_LOG_STEM_MAX`
    characters so a long value cannot push the station's log path past a
    Windows path limit. Production channel ids and sink labels are already
    plain names (``education``, ``Web``), so for them this is a no-op and the
    log lands exactly where the channel's other egress logs live.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return (cleaned or fallback)[:HLS_RELAY_LOG_STEM_MAX]


def _relay_log_stem(label: str) -> str:
    """The file-name stem for a sink label (see :func:`_relay_log_component`)."""
    return _relay_log_component(label, "sink")


def _relay_log_header(
    *,
    channel_id: str,
    sink_label: str,
    spawn: int,
    reason: str,
    args: list[str],
) -> bytes:
    """One spawn, one line: who, when, why, and the exact argv.

    The pid is left as a fixed-width placeholder and stamped in place once the
    child exists (see :meth:`HlsRelaySupervisor._stamp_relay_log_pid`) so the
    header is on disk even when the spawn itself fails. Field order is fixed
    and ``pid`` comes first, so a reader (or a parser) can find the pid field
    without knowing the rest of the line. Values that reach this line from
    config are collapsed to one line first: a channel id containing a newline
    must not be able to forge a second header.
    """
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    fields = {
        "utc": stamp,
        "channel": _one_line(channel_id),
        "sink": _one_line(sink_label),
        "spawn": str(spawn),
        "reason": reason,
    }
    prefix = f"[hls-relay] {_LOG_PID_FIELD}{_LOG_PID_PLACEHOLDER}"
    rest = " ".join(f"{key}={value}" for key, value in fields.items())
    return f"{prefix} {rest} argv={json.dumps(list(args))}\n".encode()


def _one_line(value: str) -> str:
    """Collapse a config-derived string to a single log-safe field."""
    return " ".join(value.split()) or "-"


#: U24.1: the most characters one drain read will buffer. Bounds what a single
#: pathological (newline-free) ffmpeg line can make the drain thread hold; a
#: longer line is appended in consecutive pieces, in order, so no byte is lost.
_RELAY_LOG_READ_CHARS = 64 * 1024

#: U24.1: how long a teardown waits for a relay's drain thread to see EOF.
#: The child is already dead by then -- ``terminate`` waits for it -- so the
#: pipe is at EOF and this returns immediately. The bound exists only so a
#: wedged disk write can never hold a channel's stop (or its next spawn) open.
_RELAY_LOG_DRAIN_JOIN_S = 2.0

#: U51: how often a relay checks its muxer's private staging playlist for a new
#: window and republishes the advertised one. The muxer cuts a 2 s segment, so
#: this is ~8 checks per published window: low enough to cost a station nothing
#: (one ``stat`` per tick, one small read+write only when the window changed)
#: and fast enough that the served manifest is never more than a fraction of a
#: segment behind the muxer. Captured by :class:`_ManifestPublisher` at
#: construction, so a test can shrink it (the ``HLS_RELAY_LOG_CAP_BYTES`` seam).
_MANIFEST_PUBLISH_INTERVAL_S = 0.25

#: U51: how long a teardown waits for that publisher thread. It sleeps in
#: ``Event.wait``, so a stop is prompt; the bound exists so a wedged disk write
#: can never hold a channel's stop -- or its next spawn -- open, exactly like
#: ``_RELAY_LOG_DRAIN_JOIN_S`` above.
_MANIFEST_PUBLISH_JOIN_S = 2.0


def _relay_log_header_line(path: Path) -> bytes:
    """The log's first line, as bytes: the spawn header a trim must preserve.

    Read from the file rather than remembered, so a trim preserves whatever is
    actually on disk -- including the pid the supervisor stamped in place after
    the spawn (see :meth:`HlsRelaySupervisor._stamp_relay_log_pid`).
    """
    try:
        with path.open("rb") as handle:
            line = handle.readline(_RELAY_LOG_READ_CHARS)
    except OSError:
        return b""
    if not line:
        return b""
    return line if line.endswith(b"\n") else line + b"\n"


class _RelayLogWriter:
    """Owns one relay child's stderr: the pipe's read end and the log file.

    U24.1 replaced "the child writes the file, the parent trims it" with "the
    parent writes the file". The old shape cannot work on Windows: the child
    inherits a raw file handle whose position is its own, so a parent-side
    rewrite (truncate, then write the newest tail) leaves the child appending at
    its OLD offset, and the gap between the two is zero-filled. The measured
    repro against the real starter -- ``before 5663 after_trim 2007 1s later
    5809 final 5882 NULs 3656 first_nul_at 2007`` -- is a file that grew back
    past its pre-trim size with 3,656 NULs in it: the cap bounded nothing, the
    trim fired every tick, and the retained bytes were mostly padding.

    So the child gets ``subprocess.PIPE`` instead, and this object runs the one
    thread that:

    * reads the pipe in bounded chunks and appends them to the log file it owns
      -- the only handle on that file anywhere in the system, which is exactly
      what makes a rewrite (or a rename) safe here and unsafe in the parent's
      hands while a child still holds one (``WinError 32``);
    * enforces the cap itself, in that thread, by rewriting the file as ``spawn
      header + newest tail`` -- the same retained set the supervisor used to
      produce, now actually bounded;
    * never lets the log stall the relay: if the file cannot be opened, or a
      write fails, it keeps reading and discards, and warns once per episode.
      A thread that does nothing but read and append always keeps up with
      ffmpeg's stderr, which is why a pipe is safe here and an undrained pipe is
      not.

    Lifecycle: :meth:`start` once, :meth:`close` at teardown. The thread
    references only this object -- never the supervisor, never the relay record
    -- so it keeps no channel state alive, and it ends by itself at EOF of the
    pipe (the child exited) even if nobody ever calls :meth:`close`.
    """

    def __init__(
        self,
        path: Path,
        *,
        cap_bytes: int = HLS_RELAY_LOG_CAP_BYTES,
        tail_bytes: int = HLS_RELAY_LOG_TAIL_BYTES,
    ) -> None:
        self._path = path
        self._cap_bytes = cap_bytes
        self._tail_bytes = tail_bytes
        # Reentrant: :meth:`_drain` holds it and may call :meth:`trim_if_over_cap`,
        # which takes it again.
        self._lock = RLock()
        self._handle: IO[str] | None = None
        # An estimate between trims (it resyncs from ``stat`` on every check);
        # its only job is to decide when a cap CHECK is worth a syscall.
        self._bytes = 0
        self._trims = 0
        self._thread: Thread | None = None
        #: True after a write failure: the episode is warned once and every
        #: later line is discarded rather than re-warning per line.
        self._discarding = False

    # --- lifecycle -------------------------------------------------------------------

    def start(self, stream: IO[str]) -> None:
        """Begin draining ``stream`` (the child's stderr pipe) into the log."""
        thread = Thread(
            target=self._drain,
            args=(stream,),
            name=f"hls-relay-log:{self._path.name}",
            # Daemon so a drain wedged on a dead disk can never hold interpreter
            # shutdown open. The channel's own teardown still joins it explicitly.
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the drain thread to end. True when it has ended."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def close(self, *, timeout: float = _RELAY_LOG_DRAIN_JOIN_S) -> None:
        """Join the drain thread and release the log file. Safe to call twice.

        The child is terminated before this is called, so the pipe is at EOF and
        the join returns at once; ``timeout`` is the bound on a wedged disk
        write, not the expected wait.
        """
        if not self.join(timeout):
            _LOG.warning(
                "HLS relay stderr log %s: the drain thread did not stop within %.1fs; "
                "the log file is released anyway and the relay is NOT affected.",
                self._path,
                timeout,
            )
        with self._lock:
            handle, self._handle = self._handle, None
        _close_quietly(handle)

    # --- draining --------------------------------------------------------------------

    def _drain(self, stream: IO[str]) -> None:
        """Read the pipe to EOF, appending to the log. Never raises, never stalls."""
        try:
            buffer = getattr(stream, "buffer", None)
            read1 = getattr(buffer, "read1", None)
            if callable(read1):
                reader = read1
                decoder = io.IncrementalNewlineDecoder(
                    codecs.getincrementaldecoder("utf-8")("replace"),
                    translate=True,
                )
            else:
                reader = stream.readline
                decoder = None
            while True:
                try:
                    chunk = reader(_RELAY_LOG_READ_CHARS)
                except (OSError, ValueError):
                    break
                text = decoder.decode(chunk, final=not chunk) if decoder is not None else chunk
                if text:
                    with self._lock:
                        if self._handle is None and not self._discarding:
                            self._handle = self._open_append()
                        handle = self._handle
                        if handle is not None:
                            try:
                                handle.write(text)
                                self._bytes += len(text)
                            except OSError:
                                self._note_write_failure()
                            else:
                                if self._bytes > self._cap_bytes:
                                    self.trim_if_over_cap()
                if not chunk:
                    break
        finally:
            _close_quietly(stream)
            with self._lock:
                handle, self._handle = self._handle, None
            _close_quietly(handle)

    def _open_append(self) -> IO[str] | None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # ``newline="\n"``: this file is a log, and a CRLF written by the
            # parent's text layer would not match what the child's own handle
            # produced on the same file before U24.1.
            handle = self._path.open("a", encoding="utf-8", errors="replace", newline="\n")
        except OSError:
            _LOG.warning(
                "HLS relay stderr log %s could not be opened; the relay still runs and "
                "its child's diagnostics are discarded until the next spawn.",
                self._path,
                exc_info=True,
            )
            self._discarding = True
            return None
        try:
            self._bytes = self._path.stat().st_size
        except OSError:
            self._bytes = 0
        return handle

    def _note_write_failure(self) -> None:
        """One warning per episode; the relay is never blocked by its own log."""
        if self._discarding:
            return
        self._discarding = True
        _close_quietly(self._handle)
        self._handle = None
        _LOG.warning(
            "HLS relay stderr log %s could not be written; the relay keeps running and "
            "its child's stderr is now discarded. The trim is not the relay's.",
            self._path,
            exc_info=True,
        )

    # --- bounding --------------------------------------------------------------------

    def trim_if_over_cap(self) -> bool:
        """Enforce the cap in place. True when the file was rewritten.

        Holds the writer's lock, so the drain thread and the daemon's tick can
        both call it and exactly one rewrite happens. Closing our own handle
        first is what makes the rewrite safe, and is the whole point of the
        parent owning the file -- the trim never touches the child.
        """
        with self._lock:
            handle = self._handle
            if handle is None:
                return False
            try:
                size = self._path.stat().st_size
            except OSError:
                return False
            if size <= self._cap_bytes:
                return False
            _close_quietly(handle)
            self._handle = None
            trimmed = self._rewrite_to_tail(size)
            self._handle = self._open_append()
            try:
                self._bytes = self._path.stat().st_size
            except OSError:
                self._bytes = 0
            if trimmed:
                self._trims += 1
            return trimmed

    def _rewrite_to_tail(self, size: int) -> bool:
        """Rewrite the file as ``header + newest tail``. True on success."""
        path = self._path
        header = _relay_log_header_line(path)
        try:
            with path.open("rb") as reader:
                actual_size = reader.seek(0, os.SEEK_END)
                reader.seek(-min(actual_size, self._tail_bytes), os.SEEK_END)
                tail = reader.read()
            # Drop the first (very likely partial) line so the retained tail
            # starts on a real line boundary.
            newline = tail.find(b"\n")
            if newline != -1:
                tail = tail[newline + 1 :]
            with path.open("wb") as writer:
                writer.write(header)
                writer.write(tail)
        except OSError:
            _LOG.warning(
                "HLS relay stderr log %s could not be trimmed at %d bytes; it keeps "
                "growing until the next spawn rotates it. The relay child was NOT "
                "restarted.",
                path,
                size,
                exc_info=True,
            )
            return False
        _LOG.warning(
            "HLS relay stderr log %s passed its %d-byte cap (%d bytes); trimmed to the "
            "spawn header plus the newest %d bytes. The relay child was NOT restarted; "
            "if this repeats every tick the child is logging a storm -- read the file.",
            path,
            self._cap_bytes,
            size,
            self._tail_bytes,
        )
        return True

    @property
    def trims(self) -> int:
        """How many times this writer has trimmed its log."""
        return self._trims

    @property
    def path(self) -> Path:
        return self._path


def _close_quietly(handle: IO[str] | None) -> None:
    """Close a log handle, ignoring a failure: a log must never raise at teardown."""
    if handle is None:
        return
    with contextlib.suppress(OSError):
        handle.close()


class _ManifestPublisher:
    """Mirrors a relay's private staging playlist onto the advertised manifest.

    U51 item 2. FFmpeg's hls muxer publishes ``playlist.m3u8`` temp-file-then
    **rename** on every update on this build, and a rename needs DELETE access
    to the existing file. A reader holding the advertised manifest therefore
    denies it (``WinError 5`` / ``WinError 32``) and the muxer silently stops
    updating the manifest -- measured live on 2026-09-26 (10:36:53-10:37:48):
    one reader holding one channel's ``playlist.m3u8`` froze ALL THREE
    channels for viewers for ~50 s while every relay child stayed alive and
    every segment kept advancing. The child never reported an error, so
    nothing downstream could see it.

    No flag can fix that (``+temp_file`` cannot avoid a rename it already
    performs -- measured) and no watchdog can fix it either (a FRESH child
    pointed at a directory whose manifest is held stalls identically -- M4), so
    the relay stops asking the muxer to publish the advertised name at all. The
    muxer writes :attr:`HlsSink.mux_playlist_name`, which no reader knows, and
    this thread republishes :attr:`HlsSink.manifest_name` from it.

    "Republish" is the whole point: the advertised file is rewritten **in
    place** (``r+b`` + write + truncate), which needs only write access, not
    delete access, and is therefore immune to a held handle -- the same
    operation measured at 47 of 47 against the reader that blocked 46 of 47
    renames.
    In-place is also the only torn-read-free option: a plain truncate-then-write
    exposed 32 empty reads in 275,011; the ``r+b`` shape exposed 0 in 290,508.
    The temp-then-rename fallback is kept for the two cases in-place cannot
    serve (no advertised file yet, or a share mode that denies write), and it
    is no worse than the pre-U51 behaviour it replaces.

    Reading the staging file costs this design nothing, because ffmpeg's write
    of it is atomic on this build -- measured over one 14 s replay: 7 file ids
    for 7 updates, the size never shrinking under a single id, and the file
    never absent between updates -- so what this thread reads is always a WHOLE
    window, never a half-written one.

    Contract, modeled on :class:`_RelayLogWriter`:

    * it publishes on CHANGE, never on a timer -- a quiet muxer means no disk
      traffic (a rewrite nobody asked for is also a needless window on the
      served file);
    * it never UN-publishes: a staging file that vanishes (a wipe, a sibling
      session) leaves the last window served, because blanking the manifest
      would turn a transient gap into a viewer-visible 404;
    * it never raises and never stops for a failed tick -- a failed read or
      write is retried next tick, warned once per episode;
    * segment deletion is serialized with publication and waits out the
      measured lifetime of the last public playlist that named each segment;
    * the thread references only this object -- never the supervisor, never the
      relay record -- so it keeps no channel state alive.
    """

    def __init__(
        self,
        *,
        manifest_path: Path,
        staging_path: Path,
        interval_s: float = _MANIFEST_PUBLISH_INTERVAL_S,
        clock: Callable[[], float] | None = None,
        previous_window: _ClearedHlsWindow | None = None,
    ) -> None:
        self._manifest_path = manifest_path
        self._staging_path = staging_path
        self._interval_s = interval_s
        self._stop = Event()
        self._thread: Thread | None = None
        self._clock: Callable[[], float] = clock or time.monotonic
        self._published_segments = self._read_published_segments()
        self._retired_until: dict[str, float] = {}
        now = self._clock()
        if previous_window is not None:
            self._remember_retired_window(previous_window.published_segments, now=now)
            for name in previous_window.retired_segment_names:
                self._retired_until[name] = max(
                    self._retired_until.get(name, 0.0),
                    now + previous_window.retired_grace_s,
                )
        self._remember_existing_orphans()
        #: What the staging file looked like when this publisher started. A
        #: surviving prior-session staging window is never advertised as the
        #: new session's window -- only what changes from now on is.
        self._last = self._signature()
        self._failing = False

    # --- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        """Begin publishing. One thread per relay, daemon for the same reason
        the log drain is: a wedged disk must never hold interpreter shutdown."""
        thread = Thread(
            target=self._run,
            name=f"hls-manifest-publish:{self._manifest_path.parent.name}",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def close(self, *, timeout: float = _MANIFEST_PUBLISH_JOIN_S) -> None:
        """Stop publishing. Safe to call twice."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is None:
            return
        thread.join(timeout)
        if thread.is_alive():
            _LOG.warning(
                "HLS manifest publisher for %s did not stop within %.1fs; the relay is "
                "NOT affected and the thread is abandoned (daemon).",
                self._manifest_path,
                timeout,
            )

    # --- the loop --------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self._publish_once()
            except Exception:  # a publisher must never die
                self._warn_once()

    def _publish_once(self) -> None:
        signature = self._signature()
        if signature is None or signature == self._last:
            # No staging window yet (the muxer's first cut is a segment away) or
            # nothing changed -- publish NOTHING, and never un-publish.
            self._prune_segments()
            return
        try:
            data = self._staging_path.read_bytes()
        except OSError:
            # The muxer replaced it between the stat and the read, or the file
            # is not readable right now. Retried next tick; never fatal.
            return
        segments = _playlist_segment_durations(data)
        if segments is None or any(
            not (self._manifest_path.parent / name).is_file() for name in segments
        ):
            # Do not replace a working public window with a partial muxer write
            # or a playlist whose segment has already disappeared. Retried next
            # tick; the muxer no longer deletes segments behind this publisher.
            self._warn_once()
            return
        if not self._write_manifest(data):
            self._warn_once()
            self._prune_segments()
            return
        self._remember_retired_segments(segments)
        self._published_segments = segments
        self._last = signature
        self._failing = False
        self._prune_segments()

    def _read_published_segments(self) -> dict[str, float] | None:
        try:
            data = self._manifest_path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError:
            return None
        return _playlist_segment_durations(data)

    def _remember_existing_orphans(self) -> None:
        """Give files from a prior publisher one observed-window grace period.

        A relay can be rebound while preserving its HLS directory. This new
        publisher has no in-memory retirement timestamps from its predecessor,
        so it conservatively retains unlisted files for the actual duration of
        the last public playlist plus its longest segment.
        """
        published = self._published_segments
        if published is None:
            return
        now = self._clock()
        grace = sum(published.values()) + max(published.values(), default=0.0)
        if not self._staging_path.is_file():
            active_staging: dict[str, float] = {}
        else:
            try:
                staging = _playlist_segment_durations(self._staging_path.read_bytes())
            except OSError:
                staging = None
            if staging is None:
                return
            active_staging = staging
            grace = max(grace, sum(staging.values()) + max(staging.values(), default=0.0))
        active = set(published) | set(active_staging)
        try:
            candidates = self._manifest_path.parent.glob("seg*.ts")
            for path in candidates:
                if _HLS_SEGMENT_NAME.fullmatch(path.name) is not None and path.name not in active:
                    self._retired_until[path.name] = max(
                        self._retired_until.get(path.name, 0.0), now + grace
                    )
        except OSError:
            return

    def _remember_retired_segments(self, current: dict[str, float]) -> None:
        previous = self._published_segments
        now = self._clock()
        if previous is None:
            # The old public file was unreadable at publisher start. Keep every
            # existing segment that is not in the new window for one complete
            # observed new-window lifetime before pruning it.
            grace = sum(current.values()) + max(current.values(), default=0.0)
            try:
                for path in self._manifest_path.parent.glob("seg*.ts"):
                    if (
                        _HLS_SEGMENT_NAME.fullmatch(path.name) is not None
                        and path.name not in current
                    ):
                        self._retired_until[path.name] = max(
                            self._retired_until.get(path.name, 0.0), now + grace
                        )
            except OSError:
                pass
        else:
            self._remember_retired_window(previous, now=now, except_names=set(current))
        for name in current:
            self._retired_until.pop(name, None)

    def _remember_retired_window(
        self,
        segments: dict[str, float],
        *,
        now: float,
        except_names: set[str] | None = None,
    ) -> None:
        lifetime = sum(segments.values())
        for name, duration in segments.items():
            if except_names is not None and name in except_names:
                continue
            self._retired_until[name] = max(
                self._retired_until.get(name, 0.0), now + lifetime + duration
            )

    def _prune_segments(self) -> None:
        """Remove only files outside both live windows and every client grace."""
        published = self._published_segments
        if published is None:
            return
        try:
            staging = _playlist_segment_durations(self._staging_path.read_bytes())
        except OSError:
            return
        if staging is None or any(
            not (self._manifest_path.parent / name).is_file() for name in staging
        ):
            return
        active = set(published) | set(staging)
        staged_sequences = [int(name[3:12]) for name in staging]
        if not staged_sequences:
            return
        oldest_staged_sequence = min(staged_sequences)
        now = self._clock()
        try:
            for path in self._manifest_path.parent.glob("seg*.ts"):
                match = _HLS_SEGMENT_NAME.fullmatch(path.name)
                if match is None or path.name in active:
                    continue
                retired_until = self._retired_until.get(path.name)
                if retired_until is not None and retired_until > now:
                    continue
                sequence = int(match.group(0)[3:12])
                # A segment at or newer than the staging playlist may already
                # be open or renamed by the muxer while its playlist update is
                # still in flight. Never prune it, even if a same-named file
                # was an orphan recorded when this publisher started.
                if sequence >= oldest_staged_sequence:
                    continue
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    self._warn_once()
                    continue
                self._retired_until.pop(path.name, None)
        except OSError:
            self._warn_once()

    # --- the write -------------------------------------------------------------------

    def _signature(self) -> tuple[int, int] | None:
        """The staging file's identity, or None when it is absent/unreadable."""
        try:
            stat = self._staging_path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _write_manifest(self, data: bytes) -> bool:
        """Publish ``data`` as the advertised manifest. False means it did not land."""
        with live_manifest_lock.LIVE_MANIFEST_LOCK:
            return self._write_manifest_locked(data)

    def _write_manifest_locked(self, data: bytes) -> bool:
        try:
            # In place preserves U51's Windows behavior: a reader holding the
            # file cannot block the write with a replace/delete sharing lock.
            # The live route holds this same lock only while it reads the bytes,
            # then closes the file before sending the response to a slow client.
            with self._manifest_path.open("r+b") as handle:
                handle.write(data)
                handle.truncate()
            return True
        except FileNotFoundError:
            pass
        except OSError:
            return self._replace_manifest(data)
        try:
            # Nothing published yet, so there is no old file to preserve and no
            # reader mid-read of one.
            with self._manifest_path.open("wb") as handle:
                handle.write(data)
            return True
        except OSError:
            return self._replace_manifest(data)

    def _replace_manifest(self, data: bytes) -> bool:
        """The pre-U51 shape: write a temp file, then rename it over the target.

        Only reached when in-place cannot work -- no delete access on the
        target, or no write access to it. It has the pre-U51 failure mode (a
        held target blocks the rename) but never the new one, so it is a floor,
        not a regression.
        """
        temp_path = self._manifest_path.with_name(self._manifest_path.name + ".publish.tmp")
        try:
            temp_path.write_bytes(data)
            temp_path.replace(self._manifest_path)
            return True
        except OSError:
            with contextlib.suppress(OSError):
                temp_path.unlink()
            return False

    def _warn_once(self) -> None:
        """Warn once per failure episode: the loop retries, so a per-tick warning
        would be a log storm for a condition (a hostile reader) that persists."""
        if self._failing:
            return
        self._failing = True
        _LOG.warning(
            "HLS manifest publisher for %s could not publish the muxer's staging "
            "window to %s; the relay keeps running and this is retried every tick. "
            "Players may be serving a stale window until it succeeds.",
            self._staging_path,
            self._manifest_path,
        )


class _Ffmpeg(Protocol):
    def poll(self) -> int | None: ...
    def terminate(self, *, grace_seconds: float = 5.0) -> int | None: ...


@dataclass
class _Relay:
    source_uri: str  # the hls sink's original directory URI (identity + restart-on-change key)
    relay_uri: str  # the local udp:// the GStreamer branch actually writes to
    process: _Ffmpeg
    #: Last-observed last-segment NAME for THIS relay child, plus the monotonic
    #: time it was observed. Only the segment name counts as progress (audit
    #: finding 1); size/tag edits do not. Reset whenever the child is
    #: (re)started so a new child is never judged against its predecessor.
    progress_segment: str | None = None
    progress_at: float | None = None
    #: One-shot latch for the current stall episode. Set when a self-heal
    #: restart is attempted so a second poll in the same episode cannot spawn
    #: another child (no restart storm). It is cleared ONLY when the served
    #: window genuinely advances BEYOND the pre-heal baseline (audit finding 4):
    #: a frozen playlist left on disk must not read as progress just because the
    #: replacement child started, or the heal would re-arm every bound and
    #: become a restart storm.
    #:
    #: The latch carries onto the HEAL's own replacement child (see
    #: ``maybe_self_heal_stalled``), but not across a U21 drop: a caller that
    #: reaches ``apply(new_session=True)`` discards this whole record, so the
    #: rebind's fresh child starts with ``heal_attempted=False`` and the frozen
    #: playlist re-arms exactly one more heal per INCARNATION. That is bounded
    #: by the caller, not here -- U30's rolling-hour
    #: ``_FREEZE_ESCALATION_RESTART_BUDGET`` in ``civiccast.egress.daemon``.
    heal_attempted: bool = False
    #: The last-segment name observed at the moment a heal was attempted. The
    #: latch above clears only once progress moves past THIS value.
    heal_baseline_segment: str | None = None
    #: Monotonic time this child was spawned. The startup grace is measured
    #: from here -- NOT from ``progress_at`` -- so a relay that was healthy for
    #: a long time and then stalled is still healable (its grace is long past),
    #: while a just-(re)started child is never killed before it can write.
    started_at: float | None = None
    #: Monotonic time this child last had a readable playlist. Used by the
    #: never-emitted path (audit finding 3): a producing channel whose relay has
    #: never written a window past its startup grace must be reported unhealthy.
    first_seen_at: float | None = None
    #: U12 stream-carry watch. ``streams_verified`` latches once THIS child's
    #: newest served segment is measured to carry every kind its sink requires
    #: -- the check then stops (one ffprobe per child, not one per tick).
    #: ``streams_probed_segment`` remembers which segment name that probe
    #: looked at, so a static window is not re-probed every tick.
    #: ``stream_fault_attempts`` counts this (channel, sink)'s failed restarts
    #: across children; ``stream_retry_not_before`` is the monotonic time the
    #: next attempt may run (the backoff).
    streams_verified: bool = False
    streams_probed_segment: str | None = None
    stream_fault_attempts: int = 0
    stream_retry_not_before: float | None = None
    #: U24: this child's stderr drain, which owns the log file. ``None`` means
    #: the operator configured no log root (the child's stderr is discarded,
    #: exactly as before U24) or the starter is a double that returns no pipe.
    #: U24.1: the child no longer holds the file -- the writer does -- so the
    #: cap trim is the writer's, and the trim can never leave the child
    #: appending past a rewrite into a zero-filled hole.
    log_writer: _RelayLogWriter | None = None
    #: U51: this child's manifest publisher -- the thread that mirrors the
    #: muxer's private staging playlist onto the advertised ``playlist.m3u8``.
    #: ``None`` only for a relay that never started (the record does not exist).
    publisher: _ManifestPublisher | None = None
    #: U31 orphan reclaim. ``orphan_reclaim_attempted`` is the one-shot latch for
    #: THIS child: a child's log is probed for a bind failure once and never
    #: again, so a poll tick cannot re-read and re-kill in a loop.
    #: ``orphan_reclaim_attempts`` is the episode counter (carried onto the
    #: replacement exactly like ``stream_fault_attempts``) that bounds how many
    #: reclaims may jump the restart backoff; see
    #: :meth:`HlsRelaySupervisor._maybe_reclaim_orphan`.
    orphan_reclaim_attempted: bool = False
    orphan_reclaim_attempts: int = 0


class HlsRelaySupervisor:
    """Owns one channel-lifetime ffmpeg HLS-mux relay per (channel, hls sink).

    ``apply(config)`` is called from the daemon's ``_start``/``_try_content_reload``
    the same way ``TsRelaySupervisor.apply`` is (see ``civiccast.egress.daemon``):
    it ensures each configured ``hls`` sink has a live relay child, then returns
    a config where that sink is rewritten to an ordinary ``local-ts`` UDP sink
    aimed at the relay's loopback port — so the GStreamer graph builder
    (``civiccast.egress.gst.bridge.sink_element_spec``) never needs special
    hls wiring on the hot path; it just builds the udpsink it already knows
    how to build. The rewrite is in-memory only (never persisted back to the
    store), exactly like the TS relay's URI rewrite.
    """

    def __init__(
        self,
        *,
        starter: Callable[..., _Ffmpeg] | None = None,
        base_port: int | None = None,
        stall_bound_s: float | None = None,
        segment_probe: Callable[[Path], frozenset[str] | None] | None = None,
        stream_retry_delay_s: float | None = None,
        stream_retry_slow_delay_s: float | None = None,
        log_root: Path | str | None = None,
        port_owner: PortOwnerApi | None = None,
    ) -> None:
        self._starter = starter or _default_starter
        self._base_port = base_port
        self._guard = Lock()
        self._relays: dict[str, _Relay] = {}
        self._unavailable_logged = False
        # U31: who holds a UDP port, and how to kill them safely. Injected the
        # same way ``starter`` is; the production implementation reads the OS
        # UDP connection table and kills through ``verify_and_kill_process``.
        self._port_owner_api: PortOwnerApi = port_owner or PsutilPortOwnerApi()
        # U24: where per-(channel, sink) relay stderr logs live. ``None`` keeps
        # the pre-U24 behaviour (child stderr discarded). The daemon passes its
        # authoritative ``work_dir`` per ``apply`` call -- see ``apply``.
        self._log_root: Path | None = Path(log_root) if log_root is not None else None
        # U24.1: captured at construction. A test can shrink them by
        # monkeypatching the module constants before the supervisor is built, so
        # a REAL child can be driven through a trim in a second or two instead of
        # megabytes; production never widens them.
        self._log_cap_bytes = HLS_RELAY_LOG_CAP_BYTES
        self._log_tail_bytes = HLS_RELAY_LOG_TAIL_BYTES
        # U51: captured at construction, same seam and same reason as the two
        # above -- a test drives the real publish loop in tens of milliseconds
        # instead of waiting out the production cadence.
        self._manifest_publish_interval_s = _MANIFEST_PUBLISH_INTERVAL_S
        # Per-(channel, sink) spawn ordinal, the session identity this layer
        # can honestly stamp in the header (the relay is (re)bound BEFORE the
        # worker that will feed it exists, so no worker pid is knowable here).
        self._spawn_counts: dict[str, int] = {}
        self._stall_bound_s = stall_bound_s if stall_bound_s is not None else _DEFAULT_STALL_BOUND_S
        # U12 seams, injected the same way ``starter`` is: the probe is a real
        # ffprobe by default, and the two delays default to the shipped cadence.
        self._segment_probe = segment_probe or _segment_stream_kinds
        self._stream_retry_delay_s = (
            stream_retry_delay_s
            if stream_retry_delay_s is not None
            else _DEFAULT_STREAM_RETRY_DELAY_S
        )
        self._stream_retry_slow_delay_s = (
            stream_retry_slow_delay_s
            if stream_retry_slow_delay_s is not None
            else _DEFAULT_STREAM_RETRY_SLOW_DELAY_S
        )
        # Monotonic clock, injectable for tests. Must be the SAME clock domain
        # the caller passes as ``now`` to note_progress/progress_stale/self-heal;
        # the daemon uses its own ``_monotonic`` for exactly that reason.
        self._clock: Callable[[], float] = time.monotonic

    def apply(
        self,
        config: EgressConfig,
        *,
        new_session: bool = False,
        log_root: Path | str | None = None,
    ) -> EgressConfig:
        """Return a config whose ``hls`` sinks point at their channel-lifetime relay.

        Idempotent per (channel_id, sink.label): the relay is reused across
        repeated ``apply`` calls for the SAME worker session. A sink whose relay
        could not be started (ffmpeg missing) is returned UNCHANGED — still
        declared ``hls``, so ``sink_element_spec``'s own hls branch builds a
        udpsink to the same deterministic port; nothing is listening there, but
        the channel still starts (degraded: no live HLS, everything else on the
        channel keeps working) rather than crashing.

        BETA.10 U21: ``new_session=True`` means the caller is starting a GENUINE
        new worker session (first start, crash relaunch, output-desync guard
        restart, slate->program restart) and the channel's existing relay
        child(ren) are torn down before the sinks are walked, so the child that
        ends up serving the new session was spawned FOR it. Relay reuse is only
        safe while the writer it serves is the same writer: the relay's own
        timeline outlives its writer, so a child that was corrected for a
        previous session's PTS jump keeps serving that offset for as long as the
        child lives — which is exactly how a desync becomes permanent
        (education channel, 2026-09-25 00:51:26). The replacement is spawned on
        the SAME deterministic port (see :func:`hls_relay_uri_for`), so the
        config already handed to the engine graph stays valid across the rebind.

        ``new_session=False`` is the in-place reload path
        (``EgressDaemon._request_reload``): the worker is not being restarted,
        so its relay must not be either.

        U24: ``log_root`` is where per-(channel, sink) relay stderr logs are
        written -- in production the daemon's own ``work_dir``, so the relay's
        child log sits beside the channel's ``ffmpeg``/``gst-worker`` logs (see
        the module docstring for the installed path). It is remembered for the
        life of this supervisor (later respawns, including daemon-driven
        self-heals that never call ``apply``, must land in the same place); a
        later call with an explicit ``log_root`` replaces it.
        """
        if log_root is not None:
            self._log_root = Path(log_root)
        rebind_keys: set[str] = set()
        preserved_window_keys: set[str] = set()
        if new_session:
            # Capture each sink's live state before teardown: a live child's
            # window is still the current broadcast during a worker crash-
            # relaunch, while a dead/fresh child's files are stale. The lock is
            # released before teardown because it is non-reentrant and the
            # spawn path below takes it again.
            prefix = f"{config.channel_id}|"
            hls_uris = {
                f"{config.channel_id}|{sink.label}": sink.uri
                for sink in config.sinks
                if sink.kind == "hls"
            }
            with self._guard:
                relays = {
                    key: relay for key, relay in self._relays.items() if key.startswith(prefix)
                }
                rebind_keys = set(relays)
                preserved_window_keys = {
                    key
                    for key, relay in relays.items()
                    if hls_uris.get(key) == relay.source_uri and relay.process.poll() is None
                }
            self._drop_channel_relays(config.channel_id)
        if not any(sink.kind == "hls" for sink in config.sinks):
            return config
        new_sinks: list[EgressSinkSpec] = []
        changed = False
        for sink in config.sinks:
            if sink.kind != "hls":
                new_sinks.append(sink)
                continue
            relay_uri = self._ensure_relay(
                config.channel_id,
                sink,
                reason="rebind" if f"{config.channel_id}|{sink.label}" in rebind_keys else "start",
                preserve_window=f"{config.channel_id}|{sink.label}" in preserved_window_keys,
            )
            if relay_uri is None:
                new_sinks.append(sink)
                continue
            changed = True
            new_sinks.append(sink.model_copy(update={"kind": "local-ts", "uri": relay_uri}))
        if not changed:
            return config
        return config.model_copy(update={"sinks": new_sinks})

    def _ensure_relay(
        self,
        channel_id: str,
        sink: EgressSinkSpec,
        *,
        reason: str,
        preserve_window: bool = False,
    ) -> str | None:
        """Reuse this (channel, sink)'s live relay, or spawn its one replacement.

        ``reason`` names the caller's intent for the header line
        (``start``/``rebind``); when a predecessor really was torn down here,
        the reason narrows to what actually happened to it (``uri-change``
        when the sink's directory moved, ``respawn-dead`` when the child had
        already exited).
        """
        key = f"{channel_id}|{sink.label}"
        relay_uri = hls_relay_uri_for(sink.uri, base_port=self._base_port)
        with self._guard:
            relay = self._relays.get(key)
            if relay is not None and relay.source_uri == sink.uri and relay.process.poll() is None:
                return relay.relay_uri
            if relay is not None:
                self._terminate_relay(relay)
                self._relays.pop(key, None)
                reason = "uri-change" if relay.source_uri != sink.uri else "respawn-dead"
            return self._start_relay_locked(
                key,
                channel_id,
                sink,
                relay_uri,
                reason=reason,
                preserve_window=preserve_window,
            )

    def _start_relay_locked(
        self,
        key: str,
        channel_id: str,
        sink: EgressSinkSpec,
        relay_uri: str,
        *,
        reason: str,
        preserve_window: bool = False,
    ) -> str | None:
        """Spawn one relay child and register it. Caller MUST hold ``self._guard``.

        Every spawn path funnels through here so the process count for one
        (channel, sink) key is structurally bounded at one: the caller has
        already terminated any predecessor before reaching this helper.

        U24: the child's stderr is captured here, and only here, because this is
        the only place a child is ever created. The header line is written
        BEFORE the spawn and the log is rotated BEFORE that, while the
        predecessor's handles are provably closed (the caller terminated it) --
        Windows refuses to rename a file another process holds open
        (``WinError 32``), and the predecessor held its stderr file for its
        whole life.
        """
        hls_sink = HlsSink(sink)
        previous_window: _ClearedHlsWindow | None = None
        start_number: int | None = None
        if not preserve_window:
            # Withdraw the old public window before starting a fresh muxer.
            # Referenced and orphaned numeric segments stay available through
            # their measured or conservative grace periods.
            previous_window = _clear_hls_window(hls_sink)
            start_number = previous_window.next_start_number
        args = [
            *_UDP_INPUT_ARGS,
            "-i",
            f"{relay_uri}?overrun_nonfatal=1&fifo_size=50000000",
            # U12: the relay REQUIRES every stream kind this sink carries, so a
            # child that probes a loopback input missing any of them fails fast
            # instead of emitting a half-program (audio-only OR video-only) HLS
            # window forever. A one-kind or audio-only sink keeps today's argv.
            *_relay_map_args(
                carries_video=hls_sink.carries_video,
                carries_audio=hls_sink.carries_audio,
            ),
            *_relay_hls_output_args(hls_sink, start_number=start_number),
        ]
        # A live relay's window remains part of the same broadcast across an
        # encoder crash-relaunch, so preserve it while rebinding the child.
        log_path = self._relay_log_path(channel_id, sink.label)
        pid_offset: int | None = None
        if log_path is not None:
            prepared = self._begin_relay_log(
                log_path,
                key=key,
                channel_id=channel_id,
                sink_label=sink.label,
                reason=reason,
                args=args,
            )
            if prepared is not None:
                # The header bytes are on disk; the writer reads them from
                # there, so a trim can never rewrite a stale copy.
                _, pid_offset = prepared
        try:
            if log_path is None or pid_offset is None:
                # No log root configured (or the log could not be opened): the
                # pre-U24 call shape, and the child's stderr goes to the null
                # device exactly as it did before U24.
                process = self._starter(args)
            else:
                # U24.1: the child writes to a PIPE and this process owns the
                # file. The starter hands back the pipe's read end on the
                # handle; we start the drain thread below. A pipe was the
                # pre-U24.1 worry (a full one blocks ffmpeg) and it is exactly
                # why the drain thread exists -- it reads and appends and
                # nothing else, so it cannot fall behind.
                process = self._starter(args, stderr_path=log_path)
        except FfmpegNotFoundError:
            if not self._unavailable_logged:
                self._unavailable_logged = True
                _LOG.error(
                    "HLS relay could not start for %s (sink %r -> %s): ffmpeg is not "
                    "available. The channel still starts, but no live HLS window is "
                    "served for this sink until ffmpeg is installed/repaired.",
                    channel_id,
                    sink.label,
                    sink.uri,
                )
            return None
        except OSError:
            _LOG.exception(
                "HLS relay failed to start for %s (sink %r -> %s); no live HLS window "
                "is served for this sink until the next start/reload.",
                channel_id,
                sink.label,
                sink.uri,
            )
            return None
        writer: _RelayLogWriter | None = None
        if log_path is not None and pid_offset is not None:
            # The pid is stamped BEFORE the drain starts: the header is already
            # on disk (written before the spawn, so a child that fails to start
            # still leaves its record), and nothing else can be writing that
            # offset. Only then does the thread take the file over -- it is the
            # sole writer from here on, which is what makes its own cap trim
            # safe (U24.1).
            self._stamp_relay_log_pid(log_path, pid_offset, process)
            stream = getattr(process, "stderr_pipe", None)
            if stream is not None:
                writer = _RelayLogWriter(
                    log_path,
                    cap_bytes=self._log_cap_bytes,
                    tail_bytes=self._log_tail_bytes,
                )
                writer.start(stream)
        # U51 item 2: the muxer above was handed the PRIVATE staging playlist, so
        # from here on this thread is the one thing that publishes the file
        # viewers read. Started only after a child really exists -- a degraded
        # start (no ffmpeg) must not leave a publisher running for a channel
        # that is not publishing anything. Its baseline is captured HERE, after
        # the wipe: a fossil the wipe could not remove is never advertised.
        publisher = _ManifestPublisher(
            manifest_path=Path(hls_sink.manifest_target()),
            staging_path=Path(hls_sink.connect_target()),
            interval_s=self._manifest_publish_interval_s,
            previous_window=previous_window,
        )
        publisher.start()
        now = self._clock()
        self._relays[key] = _Relay(
            source_uri=sink.uri,
            relay_uri=relay_uri,
            process=process,
            progress_segment=None,
            progress_at=None,
            heal_attempted=False,
            heal_baseline_segment=None,
            started_at=now,
            first_seen_at=now,
            log_writer=writer,
            publisher=publisher,
        )
        _LOG.info(
            "HLS relay up for %s (sink %r): %s -> %s.",
            channel_id,
            sink.label,
            relay_uri,
            sink.uri,
        )
        return relay_uri

    def _respawn_locked(
        self, key: str, channel_id: str, relay: _Relay, *, reason: str
    ) -> _Relay | None:
        """Terminate one relay child and spawn its single replacement.

        Caller MUST hold ``self._guard``. Every restart path (the stall heal
        and the U12 video restore) funnels through here so the one-child-per-
        (channel, sink) invariant is structurally preserved. Returns the new
        :class:`_Relay`, or ``None`` when the replacement could not be spawned
        (ffmpeg gone) -- in which case the key is left untracked and the sink
        falls to the existing dead-relay/health path. ``reason`` is the
        U24 header's word for why this child exists (``self-heal``,
        ``stream-restore``), so the log says which supervisor path replaced it.
        """
        sink = EgressSinkSpec(kind="hls", label=key.split("|", 1)[1], uri=relay.source_uri)
        self._terminate_relay(relay)
        self._relays.pop(key, None)
        relay_uri = hls_relay_uri_for(relay.source_uri, base_port=self._base_port)
        if self._start_relay_locked(key, channel_id, sink, relay_uri, reason=reason) is None:
            return None
        return self._relays.get(key)

    # --- U24: per-(channel, sink) relay stderr capture ---------------------------------
    #
    # U24.1: THIS PROCESS owns the file. The child writes to a pipe and a
    # per-child daemon thread (:class:`_RelayLogWriter`) drains it into the
    # per-channel log. The first cut had the child hold the file and the parent
    # trim it in place, which cannot bound anything on Windows: an inherited
    # handle keeps its own position, so a parent-side rewrite leaves the child
    # appending at its old offset and the gap is zero-filled -- the file size
    # stays equal to the child's total bytes ever written. See the class
    # docstring for the measured repro.
    #
    # Rotation still happens at SPAWN, once the predecessor has been terminated
    # and its writer closed (Windows refuses to rename a file another handle
    # holds open -- WinError 32), and the hard cap is now the writer's own
    # in-place rewrite, which only the writer's single handle can see.

    def _relay_log_path(self, channel_id: str, sink_label: str) -> Path | None:
        """This (channel, sink)'s relay log, or ``None`` when no root is set.

        ``<log_root>/<channel>/logs/hls-relay.<sink>.stderr.log`` -- the same
        ``<channel>/logs`` directory the channel's ``gst-worker.stderr.log`` and
        ``ffmpeg.stderr.log`` already live in, so an operator reads one place.
        """
        if self._log_root is None:
            return None
        return (
            self._log_root
            / _relay_log_component(channel_id, "channel")
            / "logs"
            / f"{HLS_RELAY_LOG_PREFIX}{_relay_log_stem(sink_label)}{HLS_RELAY_LOG_SUFFIX}"
        )

    def _begin_relay_log(
        self,
        path: Path,
        *,
        key: str,
        channel_id: str,
        sink_label: str,
        reason: str,
        args: list[str],
    ) -> tuple[bytes, int] | None:
        """Rotate, then write this spawn's header. Returns ``(header, pid offset)``.

        Called with ``self._guard`` held and BEFORE the child is spawned, so the
        predecessor (terminated by the caller) is not holding the file. The pid
        cannot be known yet -- the header is written with a fixed-width
        placeholder and stamped in place once the process exists, which means a
        child that fails to spawn still leaves its header on disk.
        """
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_relay_log(path)
            spawn = self._spawn_counts.get(key, 0) + 1
            self._spawn_counts[key] = spawn
            header = _relay_log_header(
                channel_id=channel_id,
                sink_label=sink_label,
                spawn=spawn,
                reason=reason,
                args=args,
            )
            with path.open("ab") as handle:
                handle.write(header)
        except OSError:
            _LOG.warning(
                "HLS relay for %s (sink %r) could not open its stderr log at %s; the "
                "relay still runs, but its own diagnostics are discarded until the "
                "next spawn.",
                channel_id,
                sink_label,
                path,
                exc_info=True,
            )
            return None
        return header, header.index(_LOG_PID_FIELD.encode("ascii")) + len(_LOG_PID_FIELD)

    def _rotate_relay_log(self, path: Path) -> None:
        """Shift the running log aside to ``.1``, discarding any older one.

        One previous generation is kept -- enough to read the child that was
        serving when the incident started, without unbounded history. The
        predecessor's handles MUST already be closed (see the caller); Windows
        raises ``WinError 32`` otherwise, which is swallowed as a warning
        because a lost rotation must never cost the channel its relay.
        """
        try:
            previous = path.with_name(path.name + ".1")
            previous.unlink(missing_ok=True)
            if path.exists():
                path.rename(previous)
        except OSError:
            _LOG.warning(
                "HLS relay stderr log %s could not be rotated; the new child appends "
                "to the existing file instead.",
                path,
                exc_info=True,
            )

    def _stamp_relay_log_pid(self, path: Path, offset: int, process: _Ffmpeg) -> None:
        """Write the child's real pid over the header's placeholder.

        Best-effort: the header is already on disk, and a pid that cannot be
        stamped is a missing field, not a lost spawn record.
        """
        pid = getattr(process, "pid", None)
        if not isinstance(pid, int):
            return
        try:
            with path.open("r+b") as handle:
                handle.seek(offset)
                handle.write(f"{pid:0{_LOG_PID_WIDTH}d}".encode("ascii"))
        except OSError:
            _LOG.warning(
                "HLS relay pid could not be stamped into %s at offset %d; the header "
                "keeps its placeholder.",
                path,
                offset,
                exc_info=True,
            )

    def maybe_trim_logs(self, channel_id: str) -> int:
        """Trim this channel's relay logs to the cap. Returns how many were trimmed.

        Called from the daemon's relay poll (the same tick that already watches
        the relays), never from the spawn path, so the trim cannot lengthen a
        restart. ``self._guard`` is taken because the relay records it reads are
        mutated by spawns -- but only to take the writers out; the rewrite itself
        happens under each writer's own lock, because the drain thread enforces
        the same cap on its own and exactly one of them may rewrite.

        U24.1: this is now a belt to the writer's braces. The writer trims
        in-thread the moment its own byte count crosses the cap, so by the time
        a tick runs there is usually nothing left to do. It stays because the
        writer's check can only run when it appends: a file left over the cap
        with the child gone quiet (a trim that hit a transient disk error, or a
        rotation that failed and left the predecessor's file in place) would
        otherwise sit there until the next spawn. This tick retries it, and its
        return value is the count the daemon reports.
        """
        if self._log_root is None:
            return 0
        with self._guard:
            writers = [
                relay.log_writer
                for key, relay in self._relays.items()
                if key.split("|", 1)[0] == channel_id and relay.log_writer is not None
            ]
        return sum(1 for writer in writers if writer.trim_if_over_cap())

    def _terminate_relay(self, relay: _Relay) -> None:
        """Terminate one relay child, then release its stderr log and publisher.

        Both halves of the order are load-bearing. The child must be dead before
        the drain thread is joined, or the join waits on a pipe that is still
        open. And the writer must release the file before the next spawn rotates
        this same path: where the child used to hold the handle that made the
        rename impossible (``WinError 32``), the parent holds it now.

        ``close`` is bounded (``_RELAY_LOG_DRAIN_JOIN_S``), and the child is
        already reaped by the time it runs, so the drain thread sees EOF at once
        and this costs nothing on the hot restart path.

        The U51 publisher goes last, for the same reason as the writer: its
        ``close`` is a prompt ``Event`` wake, and the child -- whose staging
        playlist it mirrors -- is already dead, so there is nothing left to
        publish. Ordering it before the child would leave the last window the
        muxer wrote unpublished; ordering it after costs one bounded join on the
        hot path and keeps the teardown a mirror of the spawn.
        """
        relay.process.terminate()
        writer, relay.log_writer = relay.log_writer, None
        if writer is not None:
            writer.close()
        publisher, relay.publisher = relay.publisher, None
        if publisher is not None:
            publisher.close()

    def _stream_retry_blocked(self, relay: _Relay, *, now: float) -> bool:
        """Is this relay inside the backoff window of its last stream attempt?"""
        return relay.stream_retry_not_before is not None and now < relay.stream_retry_not_before

    def _attempt_stream_restart(
        self, key: str, channel_id: str, relay: _Relay, *, now: float, reason: str
    ) -> bool:
        """Restart a child that cannot carry a required stream kind, bounded.

        Caller MUST hold ``self._guard``. Returns True when a replacement was
        spawned. The attempt count and the next-allowed time are carried onto
        the replacement (the counters belong to the (channel, sink) fault
        episode, not to one child), so a sink whose input never carries a
        required kind converges on one attempt per
        ``stream_retry_slow_delay_s`` -- logged at ERROR -- rather than a
        restart storm. The child that cannot carry the program is never left to
        serve: this is the "restart it again promptly (bounded, logged) until
        the program is whole" half of U12.
        """
        attempts = relay.stream_fault_attempts + 1
        delay = (
            self._stream_retry_delay_s
            if attempts <= _STREAM_FAST_ATTEMPTS
            else self._stream_retry_slow_delay_s
        )
        log = _LOG.warning if attempts <= _STREAM_FAST_ATTEMPTS else _LOG.error
        log(
            "HLS relay for %s (sink %r) %s; restarting the relay child to restore "
            "the missing stream (attempt %d; if it fails again the next attempt is "
            "in %.0fs).",
            channel_id,
            key.split("|", 1)[1],
            reason,
            attempts,
            delay,
        )
        replacement = self._respawn_locked(key, channel_id, relay, reason="stream-restore")
        if replacement is None:
            _LOG.error(
                "HLS relay for %s (sink %r) could not be restarted after %s; the "
                "hls sink has no live window until the next start/reload succeeds.",
                channel_id,
                key.split("|", 1)[1],
                reason,
            )
            return False
        replacement.stream_fault_attempts = attempts
        replacement.stream_retry_not_before = now + delay
        return True

    # --- U31: reclaim a port an orphaned relay ffmpeg still holds ----------------------

    def _live_relay_pids(self, *, exclude: _Relay) -> set[int]:
        """Pids of the relay children this supervisor is currently running.

        Never kill targets: two sinks whose source URIs collide on one relay
        port (``_PORT_RANGE`` is 500, so a station with many HLS sinks can
        collide) would otherwise have this supervisor kill its OWN healthy
        child to make room for the other -- turning a port collision into a
        restart loop. ``exclude`` is the already-dead relay being replaced,
        whose stale pid must not protect anything.
        """
        pids: set[int] = set()
        for relay in self._relays.values():
            if relay is exclude:
                continue
            pid = getattr(relay.process, "pid", None)
            if isinstance(pid, int):
                pids.add(pid)
        return pids

    def _maybe_reclaim_orphan(
        self, key: str, channel_id: str, relay: _Relay, *, now: float
    ) -> bool:
        """Kill the orphaned relay ffmpeg holding this relay's port, then respawn.

        Caller MUST hold ``self._guard``. Reached only from the EXITED branch of
        :meth:`maybe_restore_missing_streams`, for a child that has already
        died with a bind failure in its own log. Returns True when an orphan
        was killed AND a replacement was spawned.

        THE BOUND. This is the one restart path allowed to jump the backoff:
        waiting 60 s to rebind a port held by a process that will never release
        it is precisely the ~11-minute outage this fixes (the station,
        2026-09-25). It therefore carries its own bound --
        ``orphan_reclaim_attempts`` is copied onto each replacement (the
        counter belongs to the (channel, sink) fault episode, not to one
        child), and once it reaches ``_STREAM_FAST_ATTEMPTS`` the ordinary
        cadence governs and a reclaim waits its turn like any other restart. A
        port held by a process :func:`decide_reclaim` refuses to kill therefore
        converges on one probe per backoff window rather than a spawn storm.
        """
        if relay.orphan_reclaim_attempted:
            return False
        attempts = relay.orphan_reclaim_attempts
        if attempts >= _STREAM_FAST_ATTEMPTS and self._stream_retry_blocked(relay, now=now):
            return False
        port = udp_port_of(relay.relay_uri)
        if port is None:
            return False
        sink_label = key.split("|", 1)[1]
        if not relay_log_shows_bind_failure(self._relay_log_path(channel_id, sink_label)):
            return False
        # The probe has happened; latch it before acting on it. A refusal below
        # must not be re-probed every tick -- that would re-scan the OS
        # connection table for the life of the channel.
        relay.orphan_reclaim_attempted = True
        owner = self._port_owner_api.find_owner(port)
        verdict = decide_reclaim(
            owner,
            relay_uri=relay.relay_uri,
            excluded_pids=self._live_relay_pids(exclude=relay),
        )
        if not verdict.reclaim:
            if owner is not None:
                # Only worth a log line when there really is a holder: "nothing
                # owns that port" is the ordinary case of a relay that died for
                # some other reason.
                _LOG.warning(
                    "HLS relay for %s (sink %r) cannot bind %s and its port holder "
                    "may not be reaped: %s.",
                    channel_id,
                    sink_label,
                    relay.relay_uri,
                    verdict.reason,
                )
            return False
        if owner is None:
            # Unreachable: decide_reclaim refuses a None owner. Kept so the
            # narrowing below is explicit rather than an assertion in product
            # code.
            return False
        if not self._port_owner_api.kill(owner):
            _LOG.warning(
                "HLS relay for %s (sink %r): %s could not be killed (it may have "
                "exited first, or it is owned by another user); the relay keeps its "
                "existing retry cadence.",
                channel_id,
                sink_label,
                verdict.reason,
            )
            return False
        _LOG.warning(
            "HLS relay for %s (sink %r): reaped orphaned relay ffmpeg pid %d (%s) "
            "holding %s; restarting the relay now.",
            channel_id,
            sink_label,
            owner.pid,
            owner.name,
            relay.relay_uri,
        )
        replacement = self._respawn_locked(key, channel_id, relay, reason="orphan-reclaim")
        if replacement is None:
            _LOG.error(
                "HLS relay for %s (sink %r) could not be restarted after reaping pid %d; "
                "the hls sink has no live window until the next start/reload succeeds.",
                channel_id,
                sink_label,
                owner.pid,
            )
            return False
        replacement.orphan_reclaim_attempts = attempts + 1
        delay = (
            self._stream_retry_delay_s
            if attempts + 1 <= _STREAM_FAST_ATTEMPTS
            else self._stream_retry_slow_delay_s
        )
        replacement.stream_retry_not_before = now + delay
        return True

    def maybe_restore_missing_streams(
        self,
        channel_id: str,
        *,
        now: float,
        producing: bool,
        startup_grace_s: float,
    ) -> bool:
        """U12: make a relay carry every kind its sink requires, or restart it.

        Two fault shapes, one bounded recovery:

        * **the child exited.** The relay argv requires each kind the sink
          carries (:func:`_relay_map_args`), so a child that probed an input
          missing any of them exits immediately instead of serving a
          half-program window (measured: real ffmpeg 8.1.1, "Stream map
          '0:v:0' matches no streams", no window written). Before this method,
          nothing respawned it: a dead relay was only recovered by the
          channel's next full encoder start/reload, which for an ON_AIR
          channel can be hours -- the live government relay sat audio-only/
          dead from 18:46:32 until the channel was restarted.
        * **the child is alive but its newest served segment is missing a
          required kind.** The belt to that braces (a child can in principle
          still be emitting a half-program window if its argv did not require
          the kind, or if a mapped stream's packets never arrive): the segment
          is probed once per child, and a positive verdict -- no video, or no
          audio for a sink that carries audio -- restarts the child.

        Gating, deliberately asymmetric:

        * the EXITED shape is recovered regardless of ``producing`` -- there is
          no healthy child to protect, and a channel that is still starting is
          exactly when a half-program probe is most likely;
        * the ALIVE shape requires ``producing`` (the caller's own on-air
          evidence latch), the child's own ``startup_grace_s``, and a sink that
          requires at least one kind, mirroring :meth:`maybe_self_heal_stalled`:
          a STARTING/no-source channel legitimately has no window yet and must
          never be restarted for it.

        A sink that requires no kind (the audio-only historic argv) is never
        judged here: it keeps its output, and only the EXITED shape (an
        ordinary relay death) applies to it. Returns True when at least one
        replacement was spawned.
        """
        with self._guard:
            targets = [
                (key, relay)
                for key, relay in self._relays.items()
                if key.startswith(f"{channel_id}|")
            ]
            if not targets:
                return False
            restored = False
            for key, relay in targets:
                playlist = _playlist_path_for(relay.source_uri)
                if relay.process.poll() is not None:
                    # U31 first: if this child died because an ORPHANED relay
                    # ffmpeg of a previous control plane still holds its UDP
                    # port, the restart below would fail the same way forever.
                    # Reaping the holder is the only thing that can bring the
                    # sink back, so it is attempted before the ordinary restart
                    # -- and only for a child whose own log shows the bind
                    # failure, never on suspicion.
                    if self._maybe_reclaim_orphan(key, channel_id, relay, now=now):
                        restored = True
                        continue
                    if self._stream_retry_blocked(relay, now=now):
                        continue
                    restored |= self._attempt_stream_restart(
                        key,
                        channel_id,
                        relay,
                        now=now,
                        reason="exited without a live window",
                    )
                    continue
                if not producing or relay.streams_verified:
                    continue
                required = _sink_required_kinds(relay.source_uri)
                if not required:
                    continue
                anchor = relay.started_at if relay.started_at is not None else relay.first_seen_at
                if anchor is not None and now - anchor < startup_grace_s:
                    continue
                segment = _last_segment(playlist)
                if segment is None or segment == relay.streams_probed_segment:
                    continue
                kinds = self._segment_probe(playlist.parent / segment)
                relay.streams_probed_segment = segment
                if kinds is None:
                    # Cannot tell (segment still being written, ffprobe absent or
                    # unhappy): claim nothing and never restart on ignorance.
                    continue
                missing = required - kinds
                if not missing:
                    relay.streams_verified = True
                    relay.stream_fault_attempts = 0
                    relay.stream_retry_not_before = None
                    continue
                if self._stream_retry_blocked(relay, now=now):
                    continue
                restored |= self._attempt_stream_restart(
                    key,
                    channel_id,
                    relay,
                    now=now,
                    reason=(
                        f"is serving a window missing its {_describe_kinds(missing)} "
                        f"stream ({segment} carries only {_describe_kinds(kinds) or 'nothing'})"
                    ),
                )
            return restored

    def is_alive(self, channel_id: str) -> bool | None:
        """Liveness of this channel's relay child(ren) (MAJOR M1).

        Before this method existed, nothing polled the relay subprocess after
        ``_ensure_relay`` started it: a relay that died later (disk full,
        ``ffmpeg`` missing/removed mid-run, OOM-killed) stayed dead until the
        channel's NEXT full encoder start/reload happened to call ``apply()``
        again (which does self-heal by restarting a dead relay lazily) --
        for a long-running ON_AIR channel that can be hours or days, all the
        while the GStreamer program mux keeps muxing fine and
        ``civiccast.egress.health.build_default_sink_health`` keeps reporting
        the ``hls`` sink healthy from the MAIN encoder's UDP send progress
        alone, blind to whether anything is still listening on the relay's
        loopback port. Returns ``None`` when no relay is currently tracked
        for this channel (not using an ``hls`` sink, or a relay was never
        successfully started) -- callers must not treat that as "dead",
        only "not applicable". Returns ``True``/``False`` (all-tracked-relays
        alive / at least one exited) when this channel has at least one
        tracked relay.

        NOTE: this is process liveness ONLY. A relay that is alive but has
        stopped writing segments is still ``True`` here -- see
        :meth:`progress_stale` for the emitting check the daemon layers on top.
        """
        with self._guard:
            relays = [
                relay for key, relay in self._relays.items() if key.startswith(f"{channel_id}|")
            ]
            if not relays:
                return None
            return all(relay.process.poll() is None for relay in relays)

    def note_progress(self, channel_id: str, *, now: float) -> None:
        """Anchor/refresh this channel's per-relay progress baseline.

        Called by the daemon each tick. Idempotent and read-only with respect to
        the child process. Progress is the last segment NAME only (audit
        finding 1): a manifest whose size/tags changed but whose final ``.ts``
        is identical is NOT progress.

        The stall latch clears ONLY when the window advances past the segment
        recorded at the last heal attempt (audit finding 4). Otherwise a frozen
        playlist left on disk would read as "progress" the instant its
        replacement child starts, re-arming the heal every bound.
        """
        with self._guard:
            for key, relay in self._relays.items():
                if not key.startswith(f"{channel_id}|"):
                    continue
                segment = _last_segment(_playlist_path_for(relay.source_uri))
                if segment is None:
                    # No readable window yet: never treated as progress.
                    continue
                if segment != relay.progress_segment:
                    if relay.heal_attempted and segment != relay.heal_baseline_segment:
                        # Genuinely advanced beyond the pre-heal baseline: the
                        # episode is over and a future stall may heal again.
                        relay.heal_attempted = False
                        relay.heal_baseline_segment = None
                    relay.progress_segment = segment
                    relay.progress_at = now
                elif relay.progress_at is None:
                    # First observation of an already-static window: anchor the
                    # baseline without claiming progress.
                    relay.progress_at = now

    def progress_stale(self, channel_id: str, *, now: float) -> bool | None:
        """True when ANY live relay for this channel has stopped advancing.

        Returns ``None`` only when the question genuinely does not apply: no
        relay tracked for the channel, or every live relay has no readable
        window yet AND has not yet exceeded its startup grace while the caller
        has confirmed production (see :meth:`never_emitted`). Returns ``False``
        while every live relay is fresh.

        ANY-relay semantics (audit finding 2): with two HLS sinks, one stalled
        and one advancing, the stalled sink must still be reported, so the
        per-relay verdicts are OR-combined, never AND. A dead relay contributes
        nothing here -- that is :meth:`is_alive`'s signal.
        """
        with self._guard:
            relays = [
                relay for key, relay in self._relays.items() if key.startswith(f"{channel_id}|")
            ]
            if not relays:
                return None
            saw_live_without_window = False
            for relay in relays:
                if relay.process.poll() is not None:
                    continue
                if relay.progress_segment is None or relay.progress_at is None:
                    saw_live_without_window = True
                    continue
                if now - relay.progress_at > self._stall_bound_s:
                    return True
            if saw_live_without_window:
                return None
            # Every relay is alive with a window and none is stale.
            return False

    def heal_frozen_seconds(self, channel_id: str, *, now: float) -> float | None:
        """How long this channel's window has been frozen SINCE a failed self-heal.

        This is the daemon's "the relay self-heal has already been tried and it
        did not work" measurement, and its clock is exact rather than estimated:
        :meth:`maybe_self_heal_stalled` stamps the replacement child's
        ``progress_at`` at the heal, and :meth:`note_progress` refreshes
        ``progress_at`` only when the served segment NAME changes. So for a
        still-frozen window ``now - progress_at`` IS the time since the heal,
        and a window that genuinely advances past the pre-heal baseline clears
        ``heal_attempted`` (audit finding 4) and stops being reported here.

        ANY-relay semantics, mirroring :meth:`progress_stale`: with two HLS
        sinks, one healed-and-frozen, that sink must still be reported, so the
        per-relay verdicts are max-combined (the largest freeze wins, since
        that is the one an operator needs to hear about).

        Return contract, and why it is NOT :meth:`is_alive`'s tri-state: this
        method answers a MEASUREMENT question, not a health question, so
        ``None`` means "no live child of this channel is in a failed-heal
        state -- or none is tracked" and every caller treats it identically
        (do nothing, and make no health claim). Contrast ``is_alive``, whose
        ``None`` means "not applicable" and must never be read as "dead".
        Dead relay children contribute nothing (that is ``is_alive``'s signal),
        so a child that exited after its heal cannot hold a freeze open.
        """
        with self._guard:
            relays = [
                relay for key, relay in self._relays.items() if key.startswith(f"{channel_id}|")
            ]
            frozen: float | None = None
            for relay in relays:
                if relay.process.poll() is not None:
                    continue
                if not relay.heal_attempted:
                    continue
                anchor = relay.progress_at
                if anchor is None:
                    anchor = (
                        relay.started_at if relay.started_at is not None else relay.first_seen_at
                    )
                if anchor is None:
                    continue
                elapsed = now - anchor
                if elapsed < 0:
                    # Clock moved backwards relative to the stamp: never report
                    # a negative freeze (the daemon's bound is compared against
                    # this, and a negative would silently never escalate).
                    continue
                if frozen is None or elapsed > frozen:
                    frozen = elapsed
            return frozen

    def never_emitted(self, channel_id: str, *, now: float, startup_grace_s: float) -> bool | None:
        """True when ANY live relay has produced NO window past its OWN grace.

        Audit finding 3: a relay that never writes its first ``playlist.m3u8``
        used to stay ``None`` (unknown) forever, so a producing channel whose
        relay never emitted was permanently invisible.

        Audit follow-up (any-sink fault): with two HLS sinks, an early version
        returned ``None`` as soon as ANY sibling had a window, hiding the sink
        that never emitted. This is per-sink and ANY-based, mirroring
        :meth:`progress_stale`: a single window-less live relay that is past
        ITS OWN startup grace is a fault even while a sibling is healthy. Each
        relay is judged against its own spawn time, so one young relay cannot
        mask an old, never-emitting sibling (or vice versa).

        Returns ``None`` only when no relay is tracked, or when every live
        relay without a window is still inside its own grace (legitimate
        STARTING/no-source), or when no live relay lacks a window.
        """
        with self._guard:
            relays = [
                relay for key, relay in self._relays.items() if key.startswith(f"{channel_id}|")
            ]
            if not relays:
                return None
            live = [relay for relay in relays if relay.process.poll() is None]
            if not live:
                return None
            for relay in live:
                if relay.progress_segment is not None:
                    continue  # this sink has a readable window
                anchor = relay.started_at if relay.started_at is not None else relay.first_seen_at
                if anchor is None or now - anchor >= startup_grace_s:
                    return True  # this sink never emitted and is past its grace
            return None

    def maybe_self_heal_stalled(
        self,
        channel_id: str,
        *,
        now: float,
        producing: bool,
        startup_grace_s: float,
    ) -> bool:
        """Restart a stalled-but-alive relay at most once per stall episode.

        Conservative by construction -- returns True only when ALL hold:
          * a relay is tracked and its child is still alive,
          * ``producing`` is True (the caller has confirmed the channel is
            actually emitting; a STARTING/no-source channel never heals),
          * the window has been unchanged for longer than ``stall_bound_s``,
          * the relay child has itself been running longer than
            ``startup_grace_s`` (a just-(re)started child is never killed),
          * no heal has already been attempted in THIS episode.

        The replacement is spawned through the same single-slot path as every
        other ``apply`` (predecessor terminated first), so one (channel, sink)
        can never accumulate more than one child. Restarting DOES break viewer
        continuity for that sink's window: the muxer restarts its segment
        numbering from whatever ffmpeg chooses, so this is a deliberate
        last-resort recovery, not a seamless splice -- the caller logs it.
        """
        with self._guard:
            targets = [
                (key, relay)
                for key, relay in self._relays.items()
                if key.startswith(f"{channel_id}|")
            ]
            if not targets:
                return False
            if not producing:
                return False
            healed = False
            for key, relay in targets:
                if relay.process.poll() is not None:
                    continue
                if relay.heal_attempted:
                    continue
                anchor = relay.started_at if relay.started_at is not None else relay.first_seen_at
                if anchor is not None and now - anchor < startup_grace_s:
                    continue
                if relay.progress_segment is None or relay.progress_at is None:
                    # Never emitted a window at all (audit finding 3): a
                    # producing channel past its grace. Recover it the same way
                    # a stalled window is recovered.
                    baseline_segment = None
                    reason = "has produced no live window"
                elif now - relay.progress_at > self._stall_bound_s:
                    baseline_segment = relay.progress_segment
                    reason = f"has served no new segment for {now - relay.progress_at:.1f}s"
                else:
                    continue
                _LOG.warning(
                    "HLS relay for %s (sink %r) %s; restarting the relay child to "
                    "re-establish its window (viewer discontinuity possible).",
                    channel_id,
                    key.split("|", 1)[1],
                    reason,
                )
                new_relay = self._respawn_locked(key, channel_id, relay, reason="self-heal")
                if new_relay is None:
                    # Could not respawn (ffmpeg gone): report no heal and leave
                    # the sink to the existing dead-relay/health path.
                    healed = False
                    continue
                # Keep the episode latched on the NEW relay (audit finding 4):
                # until the served window advances past the pre-heal baseline,
                # a frozen playlist left on disk must not clear the latch.
                new_relay.heal_attempted = True
                new_relay.heal_baseline_segment = baseline_segment
                new_relay.progress_segment = baseline_segment
                new_relay.progress_at = now
                healed = True
            return healed

    def _drop_channel_relays(self, channel_id: str) -> int:
        """Terminate and forget every relay child tracked for ``channel_id``.

        Returns how many children were torn down. The single teardown seam for
        two callers: :meth:`stop_channel` (the channel is stopping) and
        :meth:`apply` with ``new_session=True`` (the channel is starting a
        genuinely new worker session). ``terminate`` is synchronous and bounded
        (``FfmpegProcessHandle.terminate``), so when this returns the UDP port is
        released and a replacement started immediately after can bind it.
        """
        with self._guard:
            keys = [key for key in self._relays if key.startswith(f"{channel_id}|")]
            for key in keys:
                relay = self._relays.pop(key)
                self._terminate_relay(relay)
        return len(keys)

    def stop_channel(self, channel_id: str) -> None:
        """Tear down a channel's HLS relay(s) (channel stop, not encoder relaunch)."""
        self._drop_channel_relays(channel_id)

    def stop_all(self) -> None:
        with self._guard:
            for relay in self._relays.values():
                self._terminate_relay(relay)
            self._relays.clear()


def _default_starter(args: list[str], *, stderr_path: Path | None = None) -> FfmpegProcessHandle:
    """Spawn the shipped relay child, optionally capturing its stderr (U24).

    U24.1: when a path is given the child's stderr goes to a PIPE, not to that
    file, and the returned handle carries the pipe's read end on
    ``stderr_pipe``. ``stderr_path`` is still the path the caller named (the
    supervisor has already rotated it and written this spawn's header into it);
    it is what the supervisor's drain thread writes to. The child never touches
    it -- which is the whole fix: the child cannot hold a handle to a file this
    process needs to rewrite.

    Without a path the child's stderr goes to ``subprocess.DEVNULL``, exactly as
    it did before U24 (and as every test double that takes no ``stderr_path``
    expects).
    """
    return start_ffmpeg(args, stderr_path=stderr_path, stderr_pipe=stderr_path is not None)
