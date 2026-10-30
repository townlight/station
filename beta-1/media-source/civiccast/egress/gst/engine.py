# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Live GStreamer playout engine (S15). Imports ``gi``, which native
Windows supplies through the pinned ``gstreamer-*`` PyPI wheels.

Builds the persistent pipeline from a gi-free ``PlayoutGraph`` via element factories
+ ``set_property`` (never string ``parse_launch`` — audit FINDING-002), hot-swaps the
active source through a pluggable ``SwapController`` (default ``InputSelectorSwap`` —
the Stage-0-validated mechanism), and tears down time-bounded so playout can never
hang (the 6h Stage-0 teardown deadlock). The teardown wait is finite; a dedicated
worker process (slice 3) calls ``stop(force_exit_on_hang=True)`` as the hard backstop.
"""

from __future__ import annotations

import base64
import contextlib
import faulthandler
import json
import math
import os
import re
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import replace
from itertools import count, pairwise
from pathlib import Path
from typing import Any, ClassVar

# R3 banner-PNG cleanup: the per-call unique filename ``bridge.py``'s
# ``graphics_overlay_leg_from_config`` renders (``graphics-overlay-lower-third.
# <uuid4-hex>.png``). Deletion of an overlay layer's image file is gated on this
# exact pattern so cleanup can NEVER touch an operator-configured, persistent
# image (e.g. a station-bug/logo ``image_path`` from config) -- only a file this
# module itself renders per-start()/per-reload matches.
_STALE_BANNER_PNG_RE = re.compile(
    r"^(?:graphics-overlay-lower-third|emergency-overlay)\.[0-9a-f]{32}\.png$"
)

try:  # package context (see the sibling-import note further down for why both forms)
    from civiccast.egress.gst.decode_policy import (
        demote_hardware_decoders,
        prefer_cpu_decoders_by_default,
    )
except ImportError:  # standalone context: the gst dir is on sys.path
    from decode_policy import (  # type: ignore[import-not-found,no-redef]
        demote_hardware_decoders,
        prefer_cpu_decoders_by_default,
    )

# MUST run before ``gi``/GStreamer is imported below: GST_PLUGIN_FEATURE_RANK is read
# during registry scan, which happens inside Gst.init().
prefer_cpu_decoders_by_default()

from civiccast.native.gstreamer_runtime import bootstrap_installed_gstreamer_runtime  # noqa: E402

bootstrap_installed_gstreamer_runtime()

import gi  # type: ignore[import-not-found] # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # type: ignore[import-not-found] # noqa: E402

try:  # package context — the native Windows line reaches this branch too:
    # the bundled GStreamer runtime (bootstrapped above) makes the `gi` import
    # succeed there, so `worker.py` must import these modules through the SAME
    # package path or the two halves bind two distinct copies of `PlaylistLeg`
    # (see worker.py's import note — that mismatch was the Gate A T4 defect).
    from civiccast.egress.gst.audio_tap import RollingWavSegmentWriter
    from civiccast.egress.gst.caption_flow import CaptionGapGate
    from civiccast.egress.gst.graph import (
        AudioTapLeg,
        CaptionEmbedLeg,
        ElementSpec,
        GraphicsOverlayLayer,
        GraphicsOverlayLeg,
        PlaylistLeg,
        PlayoutGraph,
        SecondaryAudioLeg,
        SourceLeg,
        coerce_serialized_property,
        graph_from_json,
        source_leg_is_clock_timed,
    )
except (
    ImportError
):  # standalone context: the POSIX/Windows GStreamer test adds the gst dir to sys.path
    from audio_tap import RollingWavSegmentWriter  # type: ignore[import-not-found,no-redef]
    from caption_flow import CaptionGapGate  # type: ignore[import-not-found,no-redef]
    from graph import (  # type: ignore[import-not-found,no-redef]
        AudioTapLeg,
        CaptionEmbedLeg,
        ElementSpec,
        GraphicsOverlayLayer,
        GraphicsOverlayLeg,
        PlaylistLeg,
        PlayoutGraph,
        SecondaryAudioLeg,
        SourceLeg,
        coerce_serialized_property,
        graph_from_json,
        source_leg_is_clock_timed,
    )

try:
    from civiccast.egress.gst.control import (
        LIVE_CAPTION_LEAD_MS,
        align_live_caption_pts_ms,
        caption_gap_window_ms,
        install_unix_signal_handlers,
        parse_control_line,
    )
except ImportError:
    from control import (  # type: ignore[import-not-found,no-redef]
        LIVE_CAPTION_LEAD_MS,
        align_live_caption_pts_ms,
        caption_gap_window_ms,
        install_unix_signal_handlers,
        parse_control_line,
    )

try:
    from civiccast.egress.gst.reload_policy import (
        reload_id_from_sidecar_path,
        reload_switch_is_deferred,
    )
except ImportError:
    from reload_policy import (  # type: ignore[import-not-found,no-redef]
        reload_id_from_sidecar_path,
        reload_switch_is_deferred,
    )

# Item 85: gi-free exit-code contract with the daemon (same reasoning as the
# reload_policy/decode_policy siblings above -- this module must stay
# importable both in package form and by-path, see worker.py's docstring).
try:
    from civiccast.egress.gst.exit_codes import GST_RELOAD_COMMIT_TIMEOUT_EXIT_CODE
except ImportError:
    from exit_codes import (  # type: ignore[import-not-found,no-redef]
        GST_RELOAD_COMMIT_TIMEOUT_EXIT_CODE,
    )


# Item 88 (measured in sandbox run 17, soak-a6d7871-20260906-213332Z, Opus
# diagnosis): the caption-audio-tap fork must NEVER be able to take the
# channel off air. ``caption_audio_tap_queue`` used to be a plain (default,
# NON-leaky) queue: once the appsink callback's own blocking I/O (fixed in
# ``audio_tap.py`` -- see that module's docstring) fell behind, this queue
# filled, backpressure propagated upstream through the tee it forks from,
# and the mux's audio pad -- fed by the SAME tee -- starved, stopping real
# TS output. ``leaky=2`` (``GST_QUEUE_LEAK_DOWNSTREAM``) makes the queue
# drop its OLDEST buffered data instead of ever blocking the tee once it
# fills.
#
# Round-2 review correction: ``max-size-buffers=200`` below is NOT a
# "deeper" cap -- it is GStreamer's own stock ``queue`` default, kept
# explicit only for clarity and so the unit test in
# ``test_gst_engine_audio_tap_specs.py`` has a concrete value to assert
# against. The actual load-bearing change is ``max-size-time=0`` (disabling
# the stock 1-SECOND default): this tap's audio format (F32LE, 44.1 kHz,
# stereo, 8192-byte buffers measured live) takes ~4.6s to fill 200 buffers
# (~1.6 MB) -- comfortably under the stock 10 MB ``max-size-bytes`` default,
# which is INTENTIONALLY left untouched below as a memory guard of last
# resort (a future format change to much larger buffers should still leak
# rather than grow this queue unbounded). Left at its 1s stock default,
# ``max-size-time`` -- not buffer count or bytes -- would have been the
# FIRST limit reached (1s of buffering, not 4.6s), making the queue leak
# far sooner than intended and defeating the "real headroom for a
# slow-but-recovering consumer" this fix exists to provide. This queue
# should never actually leak in normal operation; it exists as the backstop
# of last resort once the writer-side fix (bounded, drop-oldest queue +
# off-streaming-thread I/O, see ``audio_tap.py``) is already in place.
_CAPTION_AUDIO_TAP_QUEUE_LEAK_DOWNSTREAM = 2
_CAPTION_AUDIO_TAP_QUEUE_MAX_SIZE_BUFFERS = 200  # GStreamer's own stock default
_CAPTION_AUDIO_TAP_QUEUE_MAX_SIZE_TIME = 0  # disables the stock 1s default -- the real fix
_CAPTION_AUDIO_TAP_QUEUE_MAX_SIZE_BYTES = 10_485_760  # GStreamer's own stock 10 MB default
_CAPTION_AUDIO_TAP_APPSINK_MAX_BUFFERS = 32


def _audio_tap_element_specs() -> tuple[ElementSpec, ...]:
    """The caption-audio-tap fork's element chain, source-tee to appsink.

    Pulled out as a pure, gi-free function (no ``Gst`` element is actually
    constructed here -- ``ElementSpec`` is a plain dataclass) so its element
    ordering and leaky/drop properties are unit-testable without a real
    GStreamer install. See the module-level comment above this function and
    ``audio_tap.py``'s docstring for why every property here is load-bearing
    for item 88."""
    return (
        ElementSpec(
            "queue",
            "caption_audio_tap_queue",
            props={
                "leaky": _CAPTION_AUDIO_TAP_QUEUE_LEAK_DOWNSTREAM,
                "max-size-buffers": _CAPTION_AUDIO_TAP_QUEUE_MAX_SIZE_BUFFERS,
                "max-size-time": _CAPTION_AUDIO_TAP_QUEUE_MAX_SIZE_TIME,
                "max-size-bytes": _CAPTION_AUDIO_TAP_QUEUE_MAX_SIZE_BYTES,
            },
        ),
        ElementSpec("audioconvert", "caption_audio_tap_convert"),
        ElementSpec("audioresample", "caption_audio_tap_resample"),
        ElementSpec(
            "capsfilter",
            "caption_audio_tap_caps",
            props={"caps": ("audio/x-raw,format=S16LE,rate=16000,channels=1,layout=interleaved")},
        ),
        ElementSpec(
            "appsink",
            "caption_audio_tap_sink",
            props={
                "emit-signals": True,
                "sync": False,
                "max-buffers": _CAPTION_AUDIO_TAP_APPSINK_MAX_BUFFERS,
                # Item 88: was ``False`` -- a non-dropping appsink is exactly
                # as capable of backing up the tee as the non-leaky queue
                # above was. ``drop=True`` bounds this sink's own contribution
                # to the same failure mode the queue fix addresses.
                "drop": True,
            },
        ),
    )


class PrerollTimeoutError(RuntimeError):
    """The pipeline did not reach PLAYING within the configured preroll bound.

    Distinct from a bare ``RuntimeError`` (item 82, sandbox run 13 evidence) so
    ``worker.py`` can exit with ``GST_PREROLL_TIMEOUT_EXIT_CODE`` — a distinct
    code the daemon's relaunch path (``EgressDaemon._relaunch_after_crash`` /
    ``_begin_relaunch``) reads to treat a slow-but-progressing preroll under
    CPU load as a slow start, not a crash, instead of counting every such exit
    toward the crash-loop force-fallback-slate streak like an ordinary crash.
    """


# Item 82: how long ``_await_playing`` waits for the PLAYING transition before
# giving up. 30s default — generous enough to ride out ordinary CPU-load
# preroll jitter (the measured failure was a 5.0s bound tripping under load),
# never so long that a genuinely wedged pipeline hangs the worker
# indefinitely (the time-bounded-teardown audit finding M1 this method
# already exists for). Configurable per-instance and via env var for ops
# tuning without a code change; clamped to [5, 45]s.
#
# Round-2 review BLOCKER (Opus, PR #183): the upper bound is load-bearing, not
# cosmetic. ``EgressDaemon._relaunch_after_crash`` treats a worker that stayed
# up >= ``_RESTART_STREAK_RESET_UPTIME_S`` (60s) as having had a "healthy run"
# and resets the crash-loop streak to 0 — an unclamped
# ``CIVICCAST_GST_PREROLL_TIMEOUT_S >= 60`` would make a worker that ALWAYS
# preroll-times-out still measure >= 60s of "uptime" on every single exit
# (it dies right at its own configured bound), so the streak would reset on
# every crash and NEVER reach ``_LIVE_SOURCE_FAILURE_FALLBACK_STREAK`` — a
# genuinely dead source would relaunch forever instead of ever landing on
# fallback slate (measured: 40 relaunches, streak stuck at 0). 45s keeps the
# preroll bound comfortably below the 60s reset threshold with margin for
# poll/scheduling jitter, AND the daemon's crash-path exempts a
# ``GST_PREROLL_TIMEOUT_EXIT_CODE`` exit from that healthy-uptime reset
# outright (see ``_relaunch_after_crash``'s own docstring).
#
# Round-3 correction: this clamp and the crash-path exemption above are NOT
# sufficient on their own -- the daemon's SEPARATE alive-poll reset
# (``EgressDaemon._poll_process``) used to fire on wall-clock seconds since
# the worker was SPAWNED, which also counts interpreter start + ``import
# gi``/``Gst.init`` + graph build + this preroll wait itself, none of which is
# air. That overhead is NOT bounded by ``preroll_timeout_s`` and can push a
# worker's total spawn-to-exit lifetime past 60s even while it never once
# reaches PLAYING (measured: alive for 60-62s of poll ticks then a
# preroll-timeout exit, streak stuck at 1, never escalates in 40 cycles) --
# so the 45s clamp and the crash-path exemption alone did NOT close the hole
# either one, individually or together. The real fix is
# ``_await_playing`` emitting a stderr marker only on an ACTUAL PLAYING
# transition (see the ``CTRL preroll: reached PLAYING`` print below), which
# ``EgressDaemon._poll_process`` looks for via
# ``civiccast.egress.health.worker_reached_playing`` and only starts the 60s
# healthy-uptime clock from the moment that evidence is first observed —
# never from spawn time.
#
# Round-4 correction (PR #183 review, BLOCKER REPRODUCED): round-3's fix was
# ITSELF not sufficient -- the marker text above is real, but the daemon's
# per-channel stderr log is a single FIXED PATH opened in APPEND mode and
# never truncated per spawn (``_default_worker_launcher`` in ``strategy.py``,
# ``start_ffmpeg`` in ``_ffmpeg.py`` on the FFmpeg side). Once ANY worker on a
# channel ever printed this marker, it sat in the log forever, and
# ``worker_reached_playing``'s old fixed tail-window read had no way to tell
# "this worker's own marker" apart from "a previous worker's marker still in
# the tail window" -- every later worker was "confirmed on air" on its very
# first poll tick, so the 60s healthy-uptime clock became a spawn clock again
# (measured: 40 relaunches, streak pinned at 1 -- the exact round-2 symptom
# this whole fix chain exists to close, reproduced through the round-3 fix
# unchanged). Closed with two independent anchors, belt and braces:
# ``EgressDaemon._stderr_spawn_offset`` records the log's byte SIZE at this
# worker's own spawn, and ``worker_reached_playing`` /
# ``read_ffmpeg_encoder_metrics_since`` (``civiccast.egress.health``) scan
# ONLY bytes at or after that offset -- a previous worker's marker, which
# always sits before it, is never read at all, not merely filtered out after
# the fact; AND the marker line now carries THIS worker's own pid (see the
# ``os.getpid()`` print below), which the daemon requires to match its
# currently-tracked process before crediting the marker, independent of
# whether the byte offset happens to be right. The fixed 64 KiB tail window
# is gone from this check entirely -- the marker is the OLDEST line a worker
# ever prints, so a small tail window was never actually safe against a
# worker whose own startup chatter grew past it before the first observing
# poll tick, append-log bug aside.
_DEFAULT_PREROLL_TIMEOUT_S = 30.0
_MIN_PREROLL_TIMEOUT_S = 5.0
_MAX_PREROLL_TIMEOUT_S = 45.0
_PREROLL_TIMEOUT_ENV_VAR = "CIVICCAST_GST_PREROLL_TIMEOUT_S"
# How often _await_playing logs the pipeline's still-waiting state while a
# preroll is in flight, so a SLOW preroll is visible on stderr (and in the
# daemon's stderr-tail last_error) well before it either finishes or times
# out — before this fix a preroll wedged anywhere under the bound was
# completely silent until it either succeeded or the worker died.
_PREROLL_POLL_INTERVAL_S = 5.0

# Item 84 (measured in sandbox run 15, soak-fcfcb81-20260906-183448Z, and in
# three seamless-OFF runs): every fresh worker printed
# ``CTRL preroll: reached PLAYING after 0.3s`` (a real, fast PLAYING
# transition -- ``_await_playing`` above is not wrong) immediately followed by
# ``CTRL stall: no output for 10s - quitting for daemon restart`` from
# ``_check_stall`` below. ``_arm_stall_watchdog`` arms that 10s bound the
# instant PLAYING is reached, but PLAYING (even NO_PREROLL) is not evidence a
# single buffer has actually crossed the mux -- under start-up load (a
# concurrent ``ffmpeg -threads 1 h264_mf + loudnorm`` conform, a ~10s
# synchronous content-reload source preparation on the automation thread,
# live caption-tap overload) the FIRST output buffer can legitimately take
# much longer than 10s even though the worker is perfectly healthy and would
# have produced output shortly after. There was no separate bound for
# "time to first output" -- only the post-first-buffer stall bound, applied
# from the moment PLAYING happened instead of from the moment output actually
# started.
#
# This gives "never produced a single buffer" its own, much more generous
# budget than "stopped producing buffers after already airing" -- the two are
# different failure modes (a genuinely wedged/dead pipeline vs one merely
# slow to warm up) and conflating them under one 10s bound killed healthy
# workers. 45s default -- comfortably covers the measured start-up
# contention above with margin; configurable via env var for ops tuning
# without a code change; clamped to [10, 120]s (see the module-level
# ``math.isfinite`` guard rationale on ``_resolve_preroll_timeout_s``, which
# this function mirrors exactly).
_DEFAULT_FIRST_OUTPUT_TIMEOUT_S = 45.0
_MIN_FIRST_OUTPUT_TIMEOUT_S = 10.0
_MAX_FIRST_OUTPUT_TIMEOUT_S = 120.0
_FIRST_OUTPUT_TIMEOUT_ENV_VAR = "CIVICCAST_GST_FIRST_OUTPUT_TIMEOUT_S"

# Item 84c (measured in sandbox run 17, soak-a6d7871-20260906-213332Z, Opus
# diagnosis): the ``CTRL first-output: first buffer after 0.0s`` marker was a
# TAUTOLOGY, not evidence. The persistent output half's ``queue -> udpsink``
# sink chain is ASYNC (``sync=False``/async sink semantics on the UDP leg),
# so the pipeline cannot reach PLAYING at all before at least one buffer --
# typically PAT/PMT/SDT table buffers plus the very first media buffer --
# has already prerolled through the mux. ``_arm_stall_watchdog`` used to
# latch ``_first_output_seen = self._output_buffers > 0`` at arm time, which
# is true for essentially every worker that ever reaches PLAYING, marker
# text and all -- exactly the escalation-defeating symptom item 84c exists
# to close (a worker whose REAL media output later stalls still printed
# "first buffer after 0.0s" and looked like on-air evidence to the daemon).
#
# Fix: snapshot the counter at arm time (``_output_buffers_at_arm``) and
# require the count to exceed that snapshot by at least this many buffers,
# observed strictly AFTER arming, before crediting real first output. 2
# rather than 1 -- a single post-arm buffer could still be a trailing
# PAT/PMT/SDT table refresh (mpegtsmux re-emits them periodically,
# independent of media content) rather than genuine media flow; requiring a
# SECOND post-arm buffer is cheap insurance against exactly that coincidence
# without meaningfully delaying real on-air detection (media buffers advance
# far more often than table refreshes once a source is actually flowing).
_FIRST_OUTPUT_MIN_BUFFERS_AFTER_ARM = 2

# Round-2 finding 4: the audio tap writer's OWN close budget on ``stop()``,
# deliberately independent of the pipeline teardown deadline it used to share.
# Closing a rolling WAV segment is a flush + header rewrite + rename of an
# already-open local file, so two seconds is generous for the work while still
# being far too short to reintroduce the unbounded-stop hazard ``teardown_
# timeout_s`` exists to prevent. Still floored by ``teardown_timeout_s`` so an
# operator who deliberately configures a very fast stop keeps one.
_AUDIO_TAP_CLOSE_TIMEOUT_S = 2.0

# Round-2 finding 3: how long ``_dispose_source_leg`` waits for a retiring leg's
# element to actually reach NULL after ``set_state`` answered ASYNC. Source legs
# are bins, and a bin winding its children down returns ASYNC by contract, so a
# bounded wait -- not an immediate "incomplete cleanup" verdict -- is the correct
# reading of that return.
_LEG_NULL_ASYNC_WAIT_S = 2.0


def _drop_everything_probe(_pad: object, _info: object) -> object:
    """Round-2 finding 1: a pad probe that discards everything.

    Installed on an ABORTED reload leg's own src pads just before its hold probes
    are lifted, so that leg's data can never enter the ``input-selector`` at all.
    See ``_abort_pending_reload`` for why that matters."""
    return Gst.PadProbeReturn.DROP


def _drop_past_switch_point_probe(
    pad: Gst.Pad, info: Gst.PadProbeInfo, pending: dict[str, Any]
) -> Gst.PadProbeReturn:
    """U56: the retiring leg's fence at a seamless change, bounded by VALUE.

    ``_arm_old_selector_cutoff`` installs this on both outgoing pads. It used to
    install the unbounded ``_drop_everything_probe`` above, so the retiring leg's
    output stopped dead at the instant the probe was armed. The rebase reference
    that same commit is built from is ``max(observed outgoing ends)`` -- the
    LATER of the two streams, because the new leg may never start EARLIER than
    where either stream stopped. On the live station the outgoing video's
    delivery point trails the outgoing audio's by ~0.64 s (285
    ``rebase-reference`` samples, all three channels: median +0.643 s, 98.2%
    within +0.5..+0.9 s), so an unbounded fence threw away the video that should
    have carried the picture from the video end up to the switch point. Audio
    playout continued and the mux got no video for ~0.64 s; the emitted HLS
    reported it as ``segment 60 PTS delta 171000 ticks != expected 108000``.

    Bounding the fence by the switch point passes exactly the outgoing tail the
    rebase reference already accounts for, and still drops everything past it --
    so the new leg's offset is unchanged: nothing beyond the bound can ever
    cross, which keeps ``max(observed)`` the bound it was computed as.

    Everything that is not a single BUFFER -- an event, a buffer list -- is
    still dropped whole. That is not incidental: the retiring leg's EOS crossing
    the selector is the U30 defect this probe also exists to close. A buffer the
    segment cannot place is dropped too: an unmeasurable buffer is an unbounded
    one, and the pre-U56 all-drop answer is the safe one.
    """
    state = pending.get("outgoing_end", {}).get(pad)
    cutoff = pending.get("old_tail_cutoff_ns")
    if state is None or cutoff is None:
        return Gst.PadProbeReturn.DROP
    if not info.type & Gst.PadProbeType.BUFFER:
        return Gst.PadProbeReturn.DROP
    buffer = info.get_buffer()
    if buffer is None:
        return Gst.PadProbeReturn.DROP
    computed = GstPlayoutEngine._buffer_end_running_time(pad, buffer, state["segment"])
    if computed is None:
        return Gst.PadProbeReturn.DROP
    end, _segment = computed
    return Gst.PadProbeReturn.OK if end <= cutoff else Gst.PadProbeReturn.DROP


def _mux_tail_drop_decision(
    pad: Gst.Pad, info: Gst.PadProbeInfo, pending: dict[str, Any]
) -> Gst.PadProbeReturn:
    """U56 round 6: the switch-point bound as the MUX fence must apply it.

    ``_drop_past_switch_point_probe`` is the SELECTOR-side fence: the retiring
    leg's own ingress, where every buffer belongs to the leg whose segment the
    fence holds, so "a buffer the segment cannot place is an unbounded one" is
    true there and dropping it is the safe answer.

    On the mux sink pad the same branch is the defect. By the time this fence is
    armed the incoming leg is about to take the pad, and the incoming leg's audio
    restarts its own timestamps at the head of its own file. MEASURED, on C6
    plus accounting only (``tree_fix12``): this fence took 82 decisions in the
    reddening (audio-EOS-first, offset-incoming) arm -- 49 of them the retiring
    audio's own tail, with exactly the ends the CLEAN arm's 49 drops carry
    (2.602666 -> 3.626666) -- and 33 that CANNOT BE PLACED AT ALL, pts 0.000 to
    0.683, i.e. before the retiring segment's own start of 0.700. All 33 landed
    at or after the changeover segment crossed. They are the incoming leg's
    audio head: 33 frames = 0.683 s of pts, and the arm emits a 0.610656 s audio
    hole where the clean arm emits 0.021344 s.

    So: the same bound, read the same way, with the one branch that only makes
    sense UPSTREAM of the changeover inverted -- a buffer this leg's segment
    cannot place is a buffer of the OTHER leg, and it is kept. Anything that is
    not a single buffer is kept too: this pad carries the mux's own events, and
    the fence is installed BUFFER-typed precisely so it never decides one.

    Fail-open on a missing bound, deliberately: this probe cannot be installed
    without ``old_tail_cutoff_ns`` (``_arm_mux_tail_cutoff`` returns early), and
    the pre-U56 all-drop answer is the one defect the fence exists to remove.
    """
    state = pending.get("outgoing_end", {}).get(pad)
    cutoff = pending.get("old_tail_cutoff_ns")
    if state is None or cutoff is None:
        return Gst.PadProbeReturn.OK
    if not info.type & Gst.PadProbeType.BUFFER:
        return Gst.PadProbeReturn.OK
    buffer = info.get_buffer()
    if buffer is None:
        return Gst.PadProbeReturn.OK
    computed = GstPlayoutEngine._buffer_end_running_time(pad, buffer, state["segment"])
    if computed is None:
        return Gst.PadProbeReturn.OK
    end, _segment = computed
    return Gst.PadProbeReturn.OK if end <= cutoff else Gst.PadProbeReturn.DROP


def _mux_tail_segment_probe(
    pad: Gst.Pad, info: Gst.PadProbeInfo, pending: dict[str, Any]
) -> Gst.PadProbeReturn:
    """U56 rounds 6-7: the changeover segment RECORDS the origin boundary.

    GStreamer serialises events and buffers on one pad, so a SEGMENT crossing a
    fenced pad is an exact origin boundary rather than a proxy for one: every
    buffer the retiring leg sends crossed before it, and every buffer of the leg
    that replaces it crosses after it. MEASURED on the round-6 accounting arm:
    the retiring audio's last arrival reaches the fence's target (pts 4.284000 ->
    end 3.605333, the target to the nanosecond) at the instant the changeover
    segment crosses -- drop stamp 248520.046 against segment stamp 248520.046 --
    and the incoming head starts 16 ms later, entirely after it.

    Round 6 released the fence here on that reasoning. Round 7 measured the
    reasoning's PREMISE false on the LIVE C7 bytes (``u56r7-tree_fix13``): the
    retiring audio's tail is NOT already gone. The fence's own arm had already
    counted buffers popped into the mpegtsmux aggregator queue when the commit
    crossed (``stage=rebase-drain-wait pads=sink_66=3``, 5 seen), and the last of
    them airs at emitted 3602.560 -- 0.093 s PAST the switch point --
    ``EMIT audio back=1 minstep=-0.093344s @ 3602.560->3602.467``, the backward
    step the relay rejects as 'Invalid timestamps'.

    Those buffers are inside the mux before this fence can judge them, which is
    also why REMOVING the release changes nothing: ``tree_fix18f`` holds the
    fence at the boundary and emits byte-identical join numbers to C7 (AF
    n=414, maxfwd=0.733333, minstep=-0.093344; VF n=436, minstep=-0.093344).
    The release is therefore kept as measured-inert, NOT as the fix.

    The fix is upstream of the mux: U56 round 7 removed the encoder chain's
    ``videorate`` (see ``graph.encode_chain_specs``), the one element that saw
    both legs and carried its output grid frontier across the changeover. That
    moves the switch point two frames later and cuts the surviving back step to
    0.027 s (``EMIT audio minstep=-0.026678s``), inside this fence's own 0.030 s
    bar.

    The DECISION still protects the incoming head: ``_mux_tail_drop_decision``
    keeps every buffer it cannot place in the retiring segment
    (``_buffer_end_running_time`` -> None) and every buffer whose end is inside
    the bound, which is exactly the shape of an incoming head. The release only
    shortens how long the fence holds.

    The event is passed through (OK, never DROP): the segment must reach the mux.
    A segment matching the leg the fence is holding is not a boundary -- a
    re-sent sticky event is not a new origin -- so the release is keyed on the
    (start, base) pair actually changing. With no held segment to compare
    against, any segment releases: an unanchored fence may not claim an origin.
    """
    if pending.get("mux_tail_released"):
        return Gst.PadProbeReturn.OK
    event = info.get_event()
    if event is None or event.type != Gst.EventType.SEGMENT:
        return Gst.PadProbeReturn.OK
    with contextlib.suppress(Exception):
        incoming = event.parse_segment()
        state = (pending.get("outgoing_end") or {}).get(pad) or {}
        held = state.get("segment")
        if held is None or (
            int(incoming.start) != int(held.start) or int(incoming.base) != int(held.base)
        ):
            pending["mux_tail_released"] = True
            print(
                f"CTRL reload diagnostic: stage=mux-tail-released-by-segment "
                f"pad={pad.get_name()} "
                f"incoming_start={incoming.start} incoming_base={incoming.base} "
                f"held_start={None if held is None else int(held.start)} "
                f"held_base={None if held is None else int(held.base)} "
                f"reload_id={pending.get('txn_id')}",
                file=sys.stderr,
                flush=True,
            )
    return Gst.PadProbeReturn.OK


def _mux_tail_cutoff_probe(
    pad: Gst.Pad, info: Gst.PadProbeInfo, pending: dict[str, Any]
) -> Gst.PadProbeReturn:
    """U56 round 5: the same value bound, one hop downstream, on the AUDIO pad.

    ``_drop_past_switch_point_probe`` guards the selector's sink pads -- the
    retiring leg's own ingress, which is UPSTREAM of the 1.0 s non-leaky
    ``input-selector`` isolation queue. A leg that raced ahead of the commit has
    its whole post-switch-point tail sitting INSIDE that queue by the time the
    fence is armed, and nothing upstream of a queued buffer can drop it.
    MEASURED on the LIVE bytes (``u56z15-fix7``): zero audio decisions are ever
    taken at the selector-side fence, while ~48 audio buffers cross to the mux
    during the drain -- the retiring audio's last two air past the switch point
    and the emitted audio steps back by the trim (``-1.072 s``, the C5 RED).

    This probe puts the SAME bound, read from the SAME one number
    (``pending["old_tail_cutoff_ns"]``), on the last pad the audio crosses before
    the mux, which is DOWNSTREAM of that queue.

    BUFFER only, deliberately: this pad is the live mux's own flow, and the
    events crossing it belong to the mux, not to the retiring leg. The
    selector-side fence must also carry events (its EOS drop is the U30 fix),
    which is why the two are typed differently. It is installed on the AUDIO pad
    only -- see ``_arm_mux_tail_cutoff``.

    Round 6: the decision is ``_mux_tail_drop_decision``, not the selector-side
    ``_drop_past_switch_point_probe``, and a second probe
    (``_mux_tail_segment_probe``) releases the fence at the changeover segment.
    Both exist because this pad, unlike the selector's, goes on carrying the
    INCOMING leg after the switch -- see those two docstrings for the measured
    numbers.

    The arrival is recorded before the decision, because the release gate reads
    the retiring tail's ARRIVALS: the fence eats them, so an empty pad no longer
    means the retiring leg has been consumed.
    """
    pending["mux_last_arrival_t"] = time.monotonic()
    if pending.get("mux_tail_released"):
        return Gst.PadProbeReturn.OK
    decision = _mux_tail_drop_decision(pad, info, pending)
    with contextlib.suppress(Exception):
        state = pending.get("outgoing_end", {}).get(pad)
        buffer = info.get_buffer()
        if state is not None and buffer is not None:
            computed = GstPlayoutEngine._buffer_end_running_time(pad, buffer, state["segment"])
            if computed is not None:
                seen = pending.setdefault("mux_tail_seen", {})
                if computed[0] > seen.get(pad.get_name(), -1):
                    seen[pad.get_name()] = computed[0]
    return decision


# U16: the two streams a playout leg carries, in the order a leg declares them --
# ``new_src_pads`` is built as ``(out_pad, audio_out_pad)`` and ``selector_sink_pads``
# is likewise video-first, so the index of a pad in either list IS its stream label.
_NEW_LEG_STREAM_LABELS = ("video", "audio")
_DIAGNOSTIC_NONE = "none"


def _seconds_or_none(value_ns: int | None) -> str:
    """A nanosecond count as ``1.100``, or ``none`` when nothing was measured.

    Diagnostics print ``none`` rather than inventing a zero: a measured 0 and an
    absent measurement mean opposite things at a rebase boundary, and U16's whole
    problem was that the logs could not tell them apart. Note the ``is None``
    test -- ``0`` is a real measurement and must render as ``0.000``.
    """
    if value_ns is None:
        return _DIAGNOSTIC_NONE
    return f"{int(value_ns) / Gst.SECOND:.3f}"


# Item 84c addendum: how often ``_check_stall`` prints the
# ``CTRL output: <N> buffers (+<delta>) since PLAYING`` progress line -- a
# bounded, one-line-per-interval breadcrumb so the NEXT soak shows exactly
# when output stopped (sandbox run 17 had no such signal; the only evidence
# of the item 88 stall was TSDuck's after-the-fact silence and the eventual
# watchdog kill 10s later).
_OUTPUT_PROGRESS_INTERVAL_S = 5.0

# U59 round 4: the changeover leg-rate diagnostic. Live, 2026-09-27 (the
# reload_id=6 government changeover) the AIRING video collapsed to ~23.00 fps
# across the retiring leg's last ~21s while the audio never moved at all. Round
# 3 established the media is clean at every offset and that ordinary 8x-1080p
# contention dips BOTH streams together (measured ~0.92 nominal each), which is
# a different shape from that one. The mechanism is still unknown, so this is an
# INSTRUMENT, not a guard: it changes nothing about what the pipeline does, and
# prints one WARN line per episode carrying the evidence the next occurrence
# needs. Floors are the brief's: video below 0.9x nominal for 2 consecutive CTRL
# output polls WHILE audio stays at or above 0.97x, i.e. one stream starving
# against a healthy other, never a box-wide stall (which the U34 per-stream
# watchdog already owns).
_U59_RATE_ENV_VAR = "CIVICAST_GST_LEG_RATE_DIAG"
# Nominal rates are DERIVED from the leg's own capsfilters at fire time (the
# video framerate and the audio sample rate the leg was built against); these are
# only the fallback for a leg whose caps are not negotiable at the read.
_U59_RATE_NOMINAL_VIDEO_PER_S = 30.0
_U59_RATE_NOMINAL_AUDIO_PER_S = 48000.0 / 1024.0  # AAC: 1024 samples per buffer
_U59_RATE_VIDEO_FLOOR = 0.9
_U59_RATE_AUDIO_FLOOR = 0.97
_U59_RATE_STREAK_POLLS = 2
_U59_RATE_WARN_INTERVAL_S = 10.0
_U59_RATE_RECENT_FIRE_S = 60.0
# The rung the rate is judged on, in preference order: ``sel`` (what the channel
# is AIRING) first -- see the diagnostic block for why ``in`` is the wrong
# witness. Both streams are read from the SAME rung, so the video/audio
# comparison is like-for-like.
_U59_RATE_RUNG_PREFERENCE = ("sel", "queue", "in")
_U59_QOS_WINDOW_S = 30.0
_U59_QOS_HISTORY = 64
_U59_RATE_KEEP_LEGS = 3

# U59 round 4b: the delivery-LAG judge, the second instrument for the same
# defect. Round 4 judges the on-air leg's INSTANTANEOUS rate against a 0.9x
# floor, which a sustained-but-mild shortfall never crosses -- live 2026-09-28
# 06:24 (public, reload_id=16) drained video at ~0.967x nominal for ~60s and
# emitted a 2.000s picture hole, while its per-poll rate sat just AT the floor.
# A shortfall that mild cannot be caught by any rate floor that is also quiet on
# healthy legs; what it does move, without bound, is the ACCUMULATED difference
# between the seconds of audio and the seconds of video the leg has delivered.
# This judge measures exactly that: the growth of that lag across a rolling
# window, which is drift-immune (a 29.97fps leg's 0.1% mismatch moves it 0.06s
# per 60s window, 16x under the bound) and, because both streams are counted in
# their OWN nominal seconds, blind by construction to a box-wide contention dip
# (both streams at 0.92 nominal give a lag growth of exactly 0).
#
# Measured on the real excerpts (see reports/U59.md round 5): across ~6000s of
# C9 polling and 418s of C11, a healthy leg's 60s-window growth never exceeded
# 0.32s; the two occurrences measured +4.57s (C9) and +2.36s (C11). The bound
# sits between them with ~3x margin on both sides.
_U59_LAG_WINDOW_S = 60.0
_U59_LAG_BOUND_S = 1.0
_U59_LAG_STREAK_POLLS = 2
# How many (time, lag) samples the window is reconstructed from. Samples are
# pruned by TIME first (see ``_u59_prune_lag_samples``): everything inside the
# window, plus the one sample before it that is the window's baseline, so the
# growth is computable at ANY poll cadence rather than only at the 5s
# ``_OUTPUT_PROGRESS_INTERVAL_S``. This count is then only the memory guard for a
# caller driving the judge far faster than the progress interval -- a 5s cadence
# needs 13 samples for a 60s window, and 128 covers it down to a 0.47s cadence.
_U59_LAG_SAMPLES = 128


def _resolve_first_output_timeout_s(explicit: float | None) -> float:
    """Resolve the time-to-FIRST-output bound: an explicit constructor value
    wins, else the ``CIVICCAST_GST_FIRST_OUTPUT_TIMEOUT_S`` env var, else the
    45s default -- always clamped to ``[10, 120]``s. Mirrors
    ``_resolve_preroll_timeout_s`` exactly, including the ``math.isfinite``
    guard against a NaN/inf value silently defeating the clamp (Python's
    ``min``/``max`` do not clamp NaN -- see that function's docstring for the
    full reasoning, which applies unchanged here)."""
    if explicit is not None:
        if not math.isfinite(explicit):
            print(
                f"CTRL first-output: ignoring non-finite first_output_timeout_s="
                f"{explicit!r}; using default {_DEFAULT_FIRST_OUTPUT_TIMEOUT_S}s",
                file=sys.stderr,
                flush=True,
            )
            return _DEFAULT_FIRST_OUTPUT_TIMEOUT_S
        return min(max(explicit, _MIN_FIRST_OUTPUT_TIMEOUT_S), _MAX_FIRST_OUTPUT_TIMEOUT_S)
    raw = os.environ.get(_FIRST_OUTPUT_TIMEOUT_ENV_VAR)
    if raw:
        try:
            parsed = float(raw)
        except ValueError:
            parsed = math.nan  # falls through to the non-finite branch below
        if not math.isfinite(parsed):
            print(
                f"CTRL first-output: ignoring non-finite {_FIRST_OUTPUT_TIMEOUT_ENV_VAR}="
                f"{raw!r}; using default {_DEFAULT_FIRST_OUTPUT_TIMEOUT_S}s",
                file=sys.stderr,
                flush=True,
            )
            return _DEFAULT_FIRST_OUTPUT_TIMEOUT_S
        return min(max(parsed, _MIN_FIRST_OUTPUT_TIMEOUT_S), _MAX_FIRST_OUTPUT_TIMEOUT_S)
    return _DEFAULT_FIRST_OUTPUT_TIMEOUT_S


def _resolve_preroll_timeout_s(explicit: float | None) -> float:
    """Resolve the PLAYING-preroll bound: an explicit constructor value wins,
    else the ``CIVICCAST_GST_PREROLL_TIMEOUT_S`` env var, else the 30s
    default — always clamped to ``[5, 45]``s. The floor guards against
    false-positiving on ordinary CPU-load preroll jitter (the exact item 82
    failure mode); the ceiling keeps the bound safely under the daemon's 60s
    healthy-uptime streak-reset threshold (see the module-level comment above
    ``_DEFAULT_PREROLL_TIMEOUT_S`` for why an unclamped value there silently
    defeats the crash-loop escalation to fallback slate).

    Round-2 review item 4 (Opus, PR #183): ``min(max(x, lo), hi)`` does NOT
    clamp a NaN -- Python's ``max``/``min`` keep their FIRST argument on any
    comparison against NaN (every comparison with NaN is False), so
    ``min(max(nan, 5.0), 45.0)`` evaluates to ``nan``, not ``5.0``. A NaN
    bound then reaches ``_await_playing``'s ``deadline = time.monotonic() +
    self.preroll_timeout_s`` as NaN, every ``remaining`` comparison against it
    is False, and the loop falls through to the generic pipeline-construction
    ``ValueError`` path instead of ever raising the distinct
    ``PrerollTimeoutError`` -- silently defeating the whole point of this
    fix (a slow start dies as an ORDINARY crash again, indistinguishable from
    a real pipeline failure). ``CIVICCAST_GST_PREROLL_TIMEOUT_S=nan`` parses
    cleanly as a float (Python's ``float("nan")`` succeeds), so it was never
    caught by the existing ``except ValueError`` either. Guarded with
    ``math.isfinite`` before the clamp on both the explicit-arg and env-var
    paths: a non-finite value (NaN or +/-inf) falls back to the 30s default
    with a warning, exactly like any other unusable input."""
    if explicit is not None:
        if not math.isfinite(explicit):
            print(
                f"CTRL preroll: ignoring non-finite preroll_timeout_s={explicit!r}; "
                f"using default {_DEFAULT_PREROLL_TIMEOUT_S}s",
                file=sys.stderr,
                flush=True,
            )
            return _DEFAULT_PREROLL_TIMEOUT_S
        return min(max(explicit, _MIN_PREROLL_TIMEOUT_S), _MAX_PREROLL_TIMEOUT_S)
    raw = os.environ.get(_PREROLL_TIMEOUT_ENV_VAR)
    if raw:
        try:
            parsed = float(raw)
        except ValueError:
            parsed = math.nan  # falls through to the non-finite branch below
        if not math.isfinite(parsed):
            print(
                f"CTRL preroll: ignoring non-finite {_PREROLL_TIMEOUT_ENV_VAR}={raw!r}; "
                f"using default {_DEFAULT_PREROLL_TIMEOUT_S}s",
                file=sys.stderr,
                flush=True,
            )
            return _DEFAULT_PREROLL_TIMEOUT_S
        return min(max(parsed, _MIN_PREROLL_TIMEOUT_S), _MAX_PREROLL_TIMEOUT_S)
    return _DEFAULT_PREROLL_TIMEOUT_S


# Item 85: bounds for the reload-commit watchdog (_arm_commit_watchdog). 3s
# floor -- below that the watchdog would fire on ordinary GStreamer scheduling
# jitter, not a real wedge; 120s ceiling -- an operator-configured value this
# item's own worker.py env override (CIVICCAST_RELOAD_COMMIT_TIMEOUT_S) could
# otherwise set arbitrarily high must still bound how long a genuinely wedged
# worker can sit unresponsive before the daemon ever finds out.
_DEFAULT_COMMIT_TIMEOUT_S = 15.0
_MIN_COMMIT_TIMEOUT_S = 3.0
_MAX_COMMIT_TIMEOUT_S = 120.0

# U37: the two bounds on a deferred switch's rebase guard (see
# ``_arm_rebase_segment_observers``). ``_REBASE_DRAIN_DEADLINE_S`` is how long the
# commit may WAIT, off the main loop's critical path, for the affected mux sink
# pads to give up the outgoing leg's last queued buffer before it switches. It is
# the load-bearing half of the guard, and it is sized from measurement rather than
# from the intuition that "the mux is greedy, so the queue empties in
# milliseconds": on the real LPM media the affected pad holds a steady 5-6 buffers
# -- ordinary backlog -- for as long as the outgoing tail's last ~1s is still
# arriving through the chain, and only then falls 6->4->2->1->0. Measured over
# this slice's 15 final-candidate switches: 1.125-2.141s, eleven of them inside
# 1.125-1.203 and the two worst at 2.109 and 2.141s. An earlier draft's 1.0s
# bound sat below that, and its own artefact records the false alarm: ``WARN:
# rebase drain did not empty within 1.0s for reload 1 (sink_66=5 still queued at
# the mux); switching anyway`` -- on a switch whose recorded output is
# indistinguishable from the runs that drained inside the bound.
# ``_REBASE_OBSERVER_DEADLINE_S`` is how long after that the arrival observers may
# keep counting -- measured from the switch itself, so the two windows do not
# overlap. It bounds only the observers' own bookkeeping: they no longer change
# what airs, so an expired one costs nothing. Raising the drain bound reduces the
# margin the observer deadline has over the drain poller it removes at expiry, so
# the two must stay summed above ``_REBASE_DRAIN_DEADLINE_S``.
_REBASE_DRAIN_DEADLINE_S = 3.0
# U56 round 5: how long the mux tail fence may stay on after the first selector
# mutation, waiting for the retiring leg's last buffer to reach the mux pad.
_MUX_TAIL_DEADLINE_S = 0.3
# ...and how close an arriving end must come to the retiring leg's own measured
# end to count as "the tail is here". Strictly under one buffer interval: one AAC
# frame is 21.3ms, and 50ms of slack measured the SECOND-TO-LAST audio buffer
# (end 3.584 against a 3.605 target) as "the tail is here" and released the fence
# on the arrival it was armed to drop.
_MUX_TAIL_ARRIVED_EPSILON_NS = 5_000_000
# ...and how long a fenced pad must have been silent before the fence may come
# off. MEASURED: the retiring audio's last two buffers reach the mux pad in the
# SAME tick as the drain's "drained" -- the tick the fence used to come off -- so
# a condition on the seen end alone releases on the very arrival it exists to
# hold for. One poll interval of silence separates "the tail has arrived" from
# "the tail is arriving".
_MUX_TAIL_QUIET_S = 0.020
# U56 round 5: the one stream the mux fence is installed on. The video is
# excluded by measurement, not by preference -- see ``_arm_mux_tail_cutoff``.
_MUX_TAIL_FENCE_STREAM = "audio"
_REBASE_OBSERVER_DEADLINE_S = 1.0
# 20ms: fine enough that a drain is seen within a frame of the pad emptying, and
# that the wait added to a real switch is its own duration and no more; coarse
# enough not to spin.
_REBASE_DRAIN_POLL_MS = 20

# The switch point for a natural finite A/V ending is the later measured end.
# The shorter stream is bridged with its final picture or silence after its EOS,
# so choosing the common maximum preserves both tails instead of either trimming
# the longer stream or leaving a hole on the shorter one. The previous 2s trim
# bound described that old trade and is retained only for diagnostic compatibility.
_SWITCH_SHORTER_LEG_MAX_TRIM_S = 2.0

# U44: how far past its OWN previous arrival a video buffer may land at a mux sink
# pad and still be honoured, and how far ahead of the pipeline's own running time
# it may be. An arrival that clears BOTH is one the mux could only air by WAITING
# for it, which is the freeze ``_make_mux_pad_arrival_guard`` exists to stop.
#
# 2.5s: the largest legitimate forward step measured on this pad is the switch's
# own drain-then-rebase step, 1.753s (dbg1/dbg2, 2026-09-26), and the drain wait
# that bounds it is ``_REBASE_DRAIN_DEADLINE_S`` = 3.0s -- so this sits just under
# the deadline that produces legitimate steps and far above the 33ms cadence of the
# stream itself. The defect's own steps measured 10.033s (an intra-leg subchain
# boundary) and 25.033s (the reload that took government off air).
#
# 1.0s: the mux airs at the pipeline clock and sleeps in
# ``gst_aggregator_wait_and_check`` until the clock reaches its chosen head's
# running time, so an arrival further ahead than this is a freeze of at least that
# long; below it the mux is merely buffering ahead, which is ordinary. This arm is
# also what keeps the guard off a legitimate arrival: a pre-rebase arrival is
# clipped under the OLD segment (behind the clock, no wait) exactly as U37
# measured, and a stream that simply arrived early is caught by the cadence arm
# instead, because early arrivals are still in cadence.
_MUX_ARRIVAL_CADENCE_BOUND_NS = 2_500_000_000
_MUX_ARRIVAL_AHEAD_BOUND_NS = 1_000_000_000

# U37: how far behind another stream's AIRING running time one required stream may
# fall before the per-stream judge calls it stalled (``_check_stream_stalls``).
# Chosen from measurement rather than taste, and reproducible from a run's own
# recorded output: for every PES the mux emitted, take the largest airing running
# time across the streams minus the emitting stream's own (the parser is a
# throwaway -- any TS/PES reader gives this). Across the 74 healthy stream-rows
# (video and audio counted separately) of the 35 healthy recordings this slice
# produced -- the real LPM media through the live playout shape (1280x720@30,
# openh264enc, the caption-embed leg), every one on the same instrument -- the
# largest lag anywhere was 0.726678s, in the same range as the media's own A/V
# end offset at the boundary (0.014-0.748s across those runs; the outgoing
# video's last frame ends ~0.7s before its audio). That is the shape this slice
# exists to tolerate, not a defect of its own, and the same offset produced both
# that 0.726678s and a full collapse -- which is why the judge is a bound and not
# a prediction. Not one healthy row emitted a PES more than 1.0s behind. The four
# collapsed captures ran their video 9.408000-9.429333s behind, each with 253 of
# ~1720 emitted PES more than 1.0s behind and 133 more than 5.0s. 2.0s sits 2.75x
# above every healthy value measured and 4.70x below the collapse.
#
# Two properties do the real work here, and both are worth keeping in mind before
# this number is retuned. It is only reachable by a stream that is STILL
# advancing -- a stream whose running time has stopped is judged by the flat rule,
# which is the tighter bound and the correct attribution for a frozen frontier --
# and the lag has to PERSIST for a whole ``stall_timeout_s`` before anything is
# judged. That persistence is what makes the bound safe against an A/V end offset
# at the switch boundary: an offset O lags the ending stream for about O of wall
# clock, so it can only reach this judge when O alone exceeds ``stall_timeout_s``
# and this bound together.
_STREAM_LAG_BOUND_S = 2.0

# U41: the always-hot SLATE leg's selector index. It is the second leg the graph
# builds (``bridge.py`` pads the program leg at 0 and the live/infinite slate at
# 1), and it is the only thing in the pipeline that can be put on air when the
# programme's own plan has ended and its replacement is not ready yet -- which is
# what makes ``_hold_slate_for_plan_eos`` a hold rather than a stop.
_SLATE_LEG_INDEX = 1


def _resolve_commit_timeout_s(explicit: float) -> float:
    """Validate/clamp the reload-commit watchdog bound to
    ``[_MIN_COMMIT_TIMEOUT_S, _MAX_COMMIT_TIMEOUT_S]``, WARNING on stderr
    whenever the clamp actually changes the configured value -- unlike an
    earlier draft's ``max(1.0, self.commit_timeout_s)`` inline in
    ``_arm_commit_watchdog``, which silently floored an unreasonably low value
    with no operator-visible signal at all. Mirrors
    ``_resolve_preroll_timeout_s``'s non-finite guard (``math.isfinite``):
    Python's ``min``/``max`` do not clamp NaN (every comparison against NaN is
    False, so both keep their first argument), so a NaN here would otherwise
    reach ``threading.Timer`` as its ``interval`` and either raise or silently
    never fire, defeating the watchdog entirely."""
    if not math.isfinite(explicit):
        print(
            f"CTRL reload: ignoring non-finite commit_timeout_s={explicit!r}; "
            f"using default {_DEFAULT_COMMIT_TIMEOUT_S}s",
            file=sys.stderr,
            flush=True,
        )
        return _DEFAULT_COMMIT_TIMEOUT_S
    clamped = min(max(explicit, _MIN_COMMIT_TIMEOUT_S), _MAX_COMMIT_TIMEOUT_S)
    if clamped != explicit:
        print(
            f"CTRL reload: commit_timeout_s={explicit!r} out of bounds "
            f"[{_MIN_COMMIT_TIMEOUT_S}, {_MAX_COMMIT_TIMEOUT_S}]; clamped to {clamped}s",
            file=sys.stderr,
            flush=True,
        )
    return clamped


class SwapController:
    """Pluggable hot-swap mechanism. Lets GstInterpipe drop in later (S15 §9)."""

    name = "abstract"

    def bind(self, engine: GstPlayoutEngine) -> None:
        raise NotImplementedError

    def swap_to(self, index: int) -> None:
        raise NotImplementedError


class InputSelectorSwap(SwapController):
    """Stage-0-validated swap: set the input-selector's ``active-pad``."""

    name = "input-selector"

    def __init__(self) -> None:
        self._selector: Gst.Element | None = None
        self._engine: GstPlayoutEngine | None = None
        self._pads: list[Gst.Pad] = []
        self._audio_selector: Gst.Element | None = None
        self._audio_pads: list[Gst.Pad] = []

    def bind(self, engine: GstPlayoutEngine) -> None:
        self._engine = engine
        self._selector = engine.selector
        self._pads = engine.selector_sink_pads
        self._audio_selector = engine.audio_selector
        self._audio_pads = engine.audio_sink_pads

    def swap_to(self, index: int) -> None:
        if self._selector is None:
            raise RuntimeError("swap controller not bound to an engine")
        if not 0 <= index < len(self._pads):
            # Clear error instead of a bare IndexError (e.g. swap to a 'live' leg that
            # the 2-leg program+slate graph doesn't have — ENG-004 / TEST-005).
            raise IndexError(f"source index {index} out of range ({len(self._pads)} legs built)")
        self._selector.set_property("active-pad", self._pads[index])
        if self._audio_selector is not None and index < len(self._audio_pads):
            # swap audio atomically with video (seconds-granularity, same thread)
            self._audio_selector.set_property("active-pad", self._audio_pads[index])
        if self._engine is not None:
            self._engine._set_board_output_gain(
                self._engine._program_output_gain_db if index == 0 else 0.0
            )
            self._engine._board_live_program_selected = index == 0
            self._engine._set_board_live_visibility()


class GstPlayoutEngine:
    """One persistent playout pipeline built from a ``PlayoutGraph``."""

    # U41: whether the end of the programme's PLAN is a reason to stop feeding the
    # channel. Class-level defaults, so a partially-built engine (and every test
    # that assembles one with ``object.__new__``) inherits the finite-run
    # behaviour -- ``run()`` still ends at EOS. Only ``run_forever``, the
    # production worker, turns the hold on; only a commit turns ``_plan_eos_held``
    # back off. See ``_on_program_pad_plan_eos``.
    _hold_slate_at_plan_eos: bool = False
    _plan_eos_held: bool = False

    def __init__(
        self,
        graph: PlayoutGraph,
        *,
        swap: SwapController | None = None,
        teardown_timeout_s: float = 5.0,
        reload_timeout_s: float = 10.0,
        stall_timeout_s: float = 10.0,
        defer_switch_timeout_s: float = 900.0,
        preroll_timeout_s: float | None = None,
        first_output_timeout_s: float | None = None,
        commit_timeout_s: float = 15.0,
    ) -> None:
        prefer_cpu_decoders_by_default()
        Gst.init([])
        # Belt-and-suspenders with the env-var rank list above: demote by klass, from
        # the registry that really exists here, so a bundled hardware decoder nobody
        # added to the name list cannot win autoplug (Gate A T4 root cause).
        demoted = demote_hardware_decoders(Gst.Registry.get().get_feature_list(Gst.ElementFactory))
        if demoted:
            print(
                f"CTRL decode: demoted hardware decoders to CPU decode: {','.join(demoted)}",
                file=sys.stderr,
                flush=True,
            )
        self.graph = graph
        self._program_output_gain_db = (
            graph.sources[0].output_gain_db
            if graph.sources and isinstance(graph.sources[0], PlaylistLeg)
            else 0.0
        )
        self._board_output_gain_elements: list[Gst.Element] = []
        # Per-branch processing hold is separate from mux-drain observations.
        # Reload/EOS handoff uses this bounded graph-declared delay to avoid
        # committing while audio remains inside a per-output normalizer.
        self.output_audio_latency_ns = graph.max_output_audio_latency_ns
        self.swap = swap or InputSelectorSwap()
        self.teardown_timeout_s = teardown_timeout_s
        self.reload_timeout_s = reload_timeout_s
        # B3 fix: bounds how long a switch_at_end_of_current=True reload waits for
        # the OUTGOING leg's own EOS once the new leg is already ready, before
        # forcing the switch anyway (never leak two legs held open forever if a
        # schedule item's actual duration runs long or its EOS never arrives).
        # Deliberately much longer than reload_timeout_s -- that timer bounds "is
        # the new leg ready at all", this one bounds "how long is it acceptable to
        # sit on a ready leg waiting for a natural handoff point".
        self.defer_switch_timeout_s = defer_switch_timeout_s
        # S9-5: if output (TS buffers past the mux) does not advance for this long while
        # on-air, the pipeline has silently stalled — quit so the daemon restarts the
        # worker to a known state (a live source that freezes without posting an error).
        self.stall_timeout_s = stall_timeout_s
        # Item 84: bounded wait for the FIRST output buffer past the mux,
        # counted separately from ``stall_timeout_s`` above (see
        # ``_arm_stall_watchdog``/``_check_stall`` and the module-level
        # comment above ``_DEFAULT_FIRST_OUTPUT_TIMEOUT_S`` for the measured
        # failure this closes -- PLAYING is not evidence of output, and
        # applying the 10s post-first-buffer stall bound from PLAYING instead
        # of from the first real buffer killed healthy workers under
        # start-up load).
        self.first_output_timeout_s = _resolve_first_output_timeout_s(first_output_timeout_s)
        # Item 84: latched True the first time ``_check_stall`` observes
        # ``_output_buffers`` advance -- selects which of the two budgets
        # above ``_check_stall`` measures against.
        self._first_output_seen = False
        # Item 84 Round-2 review BLOCKER: the moment ``_await_playing`` last
        # observed a real PLAYING transition (``time.monotonic()``, set only
        # on the success path) -- the reference point the NEW
        # ``CTRL first-output: first buffer after Ns pid=N`` marker's ``Ns``
        # is measured from. ``None`` until PLAYING is actually reached.
        self._playing_reached_at: float | None = None
        # Item 84 Round-2 review BLOCKER: latched True the FIRST time the
        # ``CTRL first-output: ...`` marker is printed (see
        # ``_maybe_print_first_output_marker``) so it never prints twice for
        # one run, independent of ``_first_output_seen``'s own re-arm-per-call
        # semantics (see ``_arm_stall_watchdog``).
        self._first_output_marker_printed = False
        # Item 85: bounds ``_commit_reload`` itself -- see ``_arm_commit_watchdog``
        # for why a plain ``GLib.timeout_add`` cannot do this job alone (the
        # measured wedge blocks the very thread that would run it). Validated/
        # clamped (with a stderr warning on an out-of-bounds or non-finite
        # value) by ``_resolve_commit_timeout_s`` -- never silently floored.
        self.commit_timeout_s = _resolve_commit_timeout_s(commit_timeout_s)
        # Item 82: bounded PLAYING-preroll wait (see ``_await_playing`` / the
        # module-level ``_resolve_preroll_timeout_s`` for the constructor-arg /
        # env-var / default resolution and clamp).
        self.preroll_timeout_s = _resolve_preroll_timeout_s(preroll_timeout_s)
        self.pipeline = Gst.Pipeline.new("civiccast-playout")
        self.mux: Gst.Element | None = None
        self.selector: Gst.Element | None = None
        # S11a: the live caption appsrc (set when the graph has a live caption embed
        # leg) the daemon pushes timed-text cues into via the ``caption`` control command.
        self.caption_appsrc: Gst.Element | None = None
        self._caption_stream_position_ms = 0
        self._caption_gap_gate = CaptionGapGate()
        self._caption_gap_probe: tuple[Any, int] | None = None
        self._caption_gap_heartbeat_id: int | None = None
        self.selector_sink_pads: list[Gst.Pad] = []
        self.audio_selector: Gst.Element | None = None
        self.audio_sink_pads: list[Gst.Pad] = []
        self.audio_tap_appsink: Gst.Element | None = None
        self.audio_tap_writer: RollingWavSegmentWriter | None = None
        self._error: object | None = None
        self._loop: GLib.MainLoop | None = None
        # S9-5 stall watchdog state (output-buffer progress past the mux).
        self._output_buffers = 0
        self._stall_last_count = 0
        self._stall_last_advance_t = 0.0
        # Item 84c (measured in sandbox run 17, soak-a6d7871-20260906-213332Z):
        # the count of output buffers already observed AT ARM TIME -- see
        # ``_arm_stall_watchdog``/``_check_stall`` for why "first output" must
        # be measured against this snapshot, not against zero.
        self._output_buffers_at_arm = 0
        # Item 84c: last time ``_check_stall`` printed the
        # ``CTRL output: ...`` progress line (see ``_maybe_print_output_progress``).
        # 0.0 (never printed) rather than ``None`` so the first tick's
        # ``now - self._last_output_progress_print_t`` comparison is a plain
        # float subtraction with no None-guard needed.
        self._last_output_progress_print_t = 0.0
        # U59 round 4: per-worker state for the changeover leg-rate diagnostic
        # (see the block comment above ``_U59_RATE_ENV_VAR``). Allocated here
        # rather than left to the class-level defaults so two engines in one
        # process can never share an episode latch; the defaults stay immutable
        # for the ``object.__new__`` shapes (the U16 real-runtime emitter).
        self._u59_init_diagnostics()
        # U30: one BUFFER counter per mux SINK pad -- the always-on answer to
        # "which stream is still feeding the mux?". The counter above reads the
        # mux SRC, so it keeps advancing (at the audio-only rate, measured 234
        # buffers/5s against 332-496 for healthy A/V) while video is starving;
        # both live 2026-09-25 freezes read as healthy output in that line. The
        # U16 first-buffer reporters cannot answer it either -- they REMOVE
        # themselves after one buffer, and the freeze happened long after that.
        #
        # Keyed by pad NAME, never by resolved stream label: a label resolved
        # before caps negotiation would key on something the next print cannot
        # find and render as ``video=+0`` -- a lie in the exact direction this
        # diagnostic exists to detect. Single writer per key (one probe per pad,
        # one streaming thread each), read only on the GLib loop -- the same
        # lock-free contract as ``_output_buffers`` above.
        self._mux_input_pads: dict[str, Any] = {}
        self._mux_input_buffers: dict[str, int] = {}
        self._mux_input_snapshot: dict[str, int] = {}
        self._mux_input_snapshot_t = 0.0
        # U37: per-pad AIRING running time -- the running time at which the mux
        # last EMITTED a buffer it took off this pad (the aggregator's
        # ``buffer-consumed`` signal, armed in ``_install_mux_input_counters``).
        # This is the per-stream watchdog's reference, in place of the arrival
        # counter above: arrivals say what REACHED the mux, which is blind by
        # construction to the shape U37 is about. After a deferred switch a
        # stale outgoing-tail buffer could be re-dated ~9.4 s into the future and
        # pin the video pad's head while the mux drained audio alone -- with
        # video buffers still arriving at their normal rate the whole time, so
        # ``video=+122`` read as healthy while the channel aired no video at all.
        # Only consumption says what the channel was AIRING. Keyed by pad NAME
        # for the same reason the counters are, single writer per pad (the mux's
        # streaming thread), read only on the GLib loop.
        self._mux_pad_airing_rt: dict[str, int] = {}
        # U44: the arrival guard's own state -- the running time of the last video
        # buffer ACCEPTED at each mux sink pad, and how many arrivals the guard has
        # dropped there. The reference is the guard's whole memory: advancing it on
        # an accepted arrival and NOT on a dropped one is what lets two consecutive
        # future-dated arrivals both be dropped while the next real frame re-seeds
        # it. Keyed by pad NAME for the same reason the counters above are, single
        # writer per pad (that pad's own streaming thread), and read only to render
        # the diagnostic's count.
        self._mux_pad_arrival_rt: dict[str, int] = {}
        self._mux_pad_arrival_drops: dict[str, int] = {}
        # U34: per-stream stall state -- the watchdog above measures ONE
        # aggregate count (the mux SRC pad), so a channel whose video branch
        # stopped feeding the mux while audio kept flowing keeps "advancing"
        # and is never judged stalled. That is exactly the shape of all four
        # 2026-09-25 freezes (U30 measured 390 buffers/5s healthy against 244
        # buffers/5s with video gone -- the audio-only rate), and the worker
        # never exited. These three follow the pads above, keyed by pad NAME for
        # the same reason those are: a pad whose caps are not yet negotiated has
        # no honest label, and a wrong label would accuse the wrong stream.
        # ``_stream_last_rt``/``_stream_last_advance_t`` are re-baselined
        # wherever the aggregate reference is (arm time, commit settlement).
        # ``_stream_ever_seen`` is never cleared: a stream that has fed the mux
        # once is a stream whose silence is news, while a pad that has never
        # produced at all is the first-output budget's business.
        self._stream_last_rt: dict[str, int] = {}
        self._stream_last_advance_t: dict[str, float] = {}
        # U37: when each pad was FIRST seen lagging the leading stream's airing
        # running time by more than ``_STREAM_LAG_BOUND_S``. The second half of
        # the running-time judge: a stream that keeps advancing but sits seconds
        # behind the others is not airing in step either, and the mux cannot emit
        # a stream whose head the other has already passed. Re-baselined with
        # ``_stream_last_advance_t`` (arm time, commit settlement) and cleared the
        # moment the pad is back within the bound.
        self._stream_lag_since: dict[str, float] = {}
        self._stream_ever_seen: set[str] = set()
        # U34: the reload context the per-stream stall line reports -- the last
        # content reload's id and where it got to ("prepared"/"committing"/
        # "committed"/"cleanup-failed"). None until the first content reload;
        # the live reload-7 case renders ``last reload id=7 stage=committed``,
        # matching the ``stage=committed elements=52`` line the coordinator
        # read off the station.
        self._reload_context: tuple[int, str] | None = None
        # U30 EOS-origin diagnostic: WHERE the EOS that ends this worker's output
        # first arrived. Both 2026-09-25 incidents and both off-live campaigns end
        # with a bus EOS that quits the run loop -- a CLEAN teardown the daemon
        # cannot tell from an operator's ``stop`` -- and no line said where the EOS
        # entered the persistent output half. "The outgoing leg's own end escaped a
        # guard that was not watching" and "something inside the output half
        # emitted EOS on its own" are the same sight in the log and opposite
        # diagnoses. One report-only observer per already-counted pad appends its
        # label here in arrival order (which is data order, so the list reads like
        # the pipeline), and ``_announce_pipeline_eos`` renders it. Bounded: the
        # list is capped and a worker prints at most one EOS line per lifetime.
        self._eos_arrivals: list[str] = []
        self._eos_arrivals_overflow = 0
        # Per-leg element lists (index-aligned with ``selector_sink_pads``) so a
        # content-reload can dispose the leg it replaces. ``_collecting`` captures the
        # elements built for the current leg; ``_pending_reload`` holds the in-flight
        # reload (None = none settling) so it can be committed, aborted, or superseded.
        self._source_leg_elements: list[list[Gst.Element]] = []
        self._collecting: list[Gst.Element] | None = None
        # H1 fix (measured 2026-09-06 hardware soak): monotonic sequence number so
        # a ``PlaylistLeg``'s ``concat`` aggregators (``vconcat_<label>``/
        # ``aconcat_<label>``) get a fresh, unique element name on EVERY build --
        # the initial ``_build()`` call AND every later ``reload_program`` rebuild
        # of that same-labeled leg (typically "program"). Before this fix every
        # build reused the bare ``f"vconcat_{leg.label}"`` name, so a reload's
        # rebuilt aggregator collided with the still-live outgoing leg's aggregator
        # of the same name -- see ``_make``'s fail-loud ``pipeline.add`` check
        # above, and ``_build_playlist``'s naming below. Mirrors
        # ``_overlay_layer_seq`` immediately below, same rationale.
        self._source_leg_seq = 0
        self._reload_txn_counter = count(1)
        self._abort_retire_threads: list[threading.Thread] = []
        # Item 3 (education exit 1, 2026-09-25 14:51:22): an aborted reload leg is
        # NOT gone when ``_abort_pending_reload`` returns -- its disposal runs on a
        # worker thread -- so its elements are still live and still post to the bus
        # for the whole of that window. ``_abort_retire_threads`` above is only the
        # join list for ``stop()``; this is the attribution record
        # ``_belongs_to_aborted_leg`` reads, one entry per aborted leg whose
        # elements have not been proven gone:
        # ``{"thread": Thread, "elements": list, "retired": bool}``.
        self._abort_retire_legs: list[dict[str, Any]] = []
        self._pending_reload: dict[str, Any] | None = None
        # A commit keeps its transaction in ``_pending_reload`` until old-leg
        # retirement and main-loop finalization both complete. Only the potentially
        # blocking retirement runs on this one background thread; all transaction
        # ownership stays on the GLib loop. An overlapping request is rejected so
        # the worker/strategy/daemon can restart from the complete newest graph.
        # The engine must not privately retain only the program half of a worker
        # graph after that worker has already advanced through its overlay call.
        self._reload_commit_thread: threading.Thread | None = None
        self._stopping = False
        # S15 graphics-overlay reload state (BLOCKER fix, 2026-08-30 audit): the
        # compositor + per-layer-name pad/elements built by ``_build_graphics_overlay``
        # (None/empty when the graph has no overlay leg), plus any swap that is
        # currently settling toward a first-buffer commit -- mirrors
        # ``_source_leg_elements``/``_pending_reload`` above, one entry per layer NAME
        # instead of one leg per role index (see ``reload_graphics_overlay``).
        self._overlay_compositor: Gst.Element | None = None
        self._overlay_layer_pads: dict[str, Gst.Pad] = {}
        self._overlay_layer_elements: dict[str, list[Gst.Element]] = {}
        # R3: the image_path each currently-live layer's chain reads from, so a
        # swap/removal can delete the file it is REPLACING once (and only once)
        # that old chain is fully disposed -- never the file the still-live or
        # about-to-commit chain has open. Index-aligned with
        # ``_overlay_layer_pads``/``_overlay_layer_elements`` by layer name.
        self._overlay_layer_image_paths: dict[str, str] = {}
        self._pending_overlay_swaps: dict[str, dict[str, Any]] = {}
        self._overlay_layer_seq = 0
        self._overlay_gpu = True
        self._program_video_concat: Gst.Element | None = None
        self._board_live_concat_kinds: dict[str, dict[str, str]] = {}
        self._board_live_program_selected = True
        first_source = graph.sources[0] if graph.sources else None
        self._board_live_segment = bool(
            isinstance(first_source, PlaylistLeg)
            and first_source.subchain_kinds
            and first_source.subchain_kinds[0] == "cg"
        )
        self._emergency_payload: dict[str, Any] | None = None
        self._emergency_scroll_source: int | None = None
        # S11 gap 9: language tag events for secondary audio must be pushed AFTER the
        # pipeline reaches PLAYING (push_event at NULL state doesn't flow into mpegtsmux).
        # Stored here during _build(); flushed by _flush_lang_tags() post-_await_playing().
        self._pending_lang_tags: list[tuple[Gst.Element, str]] = []
        self._build()
        self.swap.bind(self)

    # -- construction (element factories + set_property; no parse_launch) --------

    def _make(self, spec: ElementSpec) -> Gst.Element:
        element = Gst.ElementFactory.make(spec.factory, spec.name)
        if element is None:
            raise RuntimeError(f"GStreamer element factory missing: {spec.factory!r}")
        for key, value in spec.props.items():
            element.set_property(
                key,
                coerce_serialized_property(
                    key=key,
                    value=value,
                    caps_from_string=Gst.Caps.from_string,
                ),
            )
        for key, handle in spec.secret_props.items():
            # WP-07: the graph file on disk carries only an opaque handle; the
            # real secret is fetched from the station's OS credential store
            # here, at element-construction time, and set straight onto the
            # element. It is never logged and never written back anywhere.
            # Fail closed -- a live SRT source whose passphrase cannot be read
            # must not silently start unauthenticated.
            from civiccast.live.secrets import load_live_source_secret

            secret = load_live_source_secret(handle)
            if not secret:
                raise RuntimeError(
                    f"credential handle {handle!r} for {spec.factory} property {key!r} is "
                    "not present in this station's credential store"
                )
            element.set_property(key, secret)
        # H1 fix (measured 2026-09-06 hardware soak, three-channel seamless-rollover
        # stall): ``Gst.Bin.add()`` returns a bool and silently REFUSES a duplicate
        # element name in the same bin ("Name '<name>' is not unique in bin ... not
        # adding") instead of raising -- the discarded return value here used to let
        # a reload's rebuilt concat aggregator (see ``_build_playlist``'s
        # ``_source_leg_seq`` naming below) dangle unlinked in the pipeline: its
        # elements existed but were never actually part of the bin, so the new
        # leg's probes never fired and every reload timed out and was silently
        # retried by automation forever (worker.py's stall watchdog then bounced
        # the channel every ~30s). Fail loud instead: the caller
        # (``reload_program``'s try/except ENG-008 path) aborts the in-flight
        # reload cleanly and the current program keeps playing.
        #
        # Candidate-3 smoke regression (2026-09-06, real GStreamer 1.28.5 on the
        # installed product closure): this check's own contract assumption was
        # wrong for gst-python's ``overrides/Gst.py``, which wraps the raw C
        # ``gst_bin_add()`` -- ``Gst.Bin.add()`` there does NOT return the raw
        # bool at all: it returns ``None`` on SUCCESS and RAISES ``Gst.AddError``
        # on failure (never returns a plain ``False``). ``if not self.pipeline.
        # add(element):`` treated that ``None`` success return as falsy, so the
        # very FIRST element ever added to a freshly built pipeline (the video
        # selector, name "sel") always raised this RuntimeError even though
        # GStreamer's own C code had just logged "added element sel" --
        # confirmed directly: ``pipeline.add(el)`` returned ``None`` on a real
        # add, ``pipeline.iterate_elements()`` showed the element really was a
        # child, and a genuine duplicate raised ``Gst.AddError`` rather than
        # returning ``False``. This broke every initial build against a real,
        # override-wrapped gst-python -- unit tests never caught it because the
        # fake pipeline fixture modeled the assumed (raw-bool) contract, not
        # this one. ``_bin_add`` below normalizes both contracts to a plain
        # bool so this check works against either.
        if not self._bin_add(element):
            raise RuntimeError(
                f"GStreamer refused to add element {spec.factory!r} "
                f"(name={element.get_name()!r}) to the pipeline -- a duplicate "
                "element name already exists in this bin"
            )
        if self._collecting is not None:
            # Building a source leg — record the element so the leg can be torn down
            # as a unit on a later content-reload.
            self._collecting.append(element)
        return element

    def _bin_add(self, element: Gst.Element) -> bool:
        """``self.pipeline.add(element)``, normalized to a plain bool regardless
        of which of TWO real contracts this GStreamer's Python binding follows:

        * the raw C ``gst_bin_add()`` contract (a gboolean return, no
          exception) -- what the fake pipeline fixture in
          ``tests/egress/test_gst_engine_reload_concat_naming.py`` models, and
          what an older/unwrapped binding may still do; or
        * gst-python's ``overrides/Gst.py`` ``Bin.add()`` override -- confirmed
          directly against GStreamer 1.28.5 (the candidate-3 installed
          product's own runtime): returns ``None`` on success and RAISES
          ``Gst.AddError`` on failure, never a bare ``False``.

        ``getattr(Gst, "AddError", ())`` degrades to an empty tuple (an
        ``except ()`` that matches nothing) when this binding has no
        ``AddError`` at all -- e.g. the fake ``Gst`` module the unit tests
        install -- so this helper needs no test-fixture changes to keep
        working against either contract."""
        try:
            added = self.pipeline.add(element)
        except getattr(Gst, "AddError", ()):
            return False
        return added is not False

    @staticmethod
    def _link(upstream: Gst.Element, downstream: Gst.Element) -> None:
        if not upstream.link(downstream):
            raise RuntimeError(f"failed to link {upstream.get_name()} -> {downstream.get_name()}")

    _DECODERS = ("decodebin", "uridecodebin", "decodebin3")

    def _link_dynamic_video(self, decoder: Gst.Element, downstream: Gst.Element) -> None:
        """Link a decoder's video src pad to ``downstream`` once it appears.

        decodebin exposes pads dynamically (FINDING-203); the audio handler is
        registered separately. A failed link is surfaced (audit M3) rather than
        silently dropped, so a black channel leaves a diagnostic."""
        sink = downstream.get_static_pad("sink")

        def _on_pad_added(_decoder: Gst.Element, pad: Gst.Pad) -> None:
            if sink.is_linked():
                return
            caps = pad.get_current_caps() or pad.query_caps(None)
            if (
                caps is not None
                and caps.to_string().startswith("video/")
                and pad.link(sink) != Gst.PadLinkReturn.OK
            ):
                print(
                    f"WARN: failed to link decoded video pad into {downstream.get_name()}",
                    flush=True,
                )

        decoder.connect("pad-added", _on_pad_added)

    def _link_dynamic_audio(self, decoder: Gst.Element, downstream: Gst.Element) -> None:
        """Link a decoder's audio src pad to ``downstream`` once it appears.

        Registered alongside the video handler on the same decodebin; each handler
        links only its own media type."""
        sink = downstream.get_static_pad("sink")

        def _on_pad_added(_decoder: Gst.Element, pad: Gst.Pad) -> None:
            if sink.is_linked():
                return
            caps = pad.get_current_caps() or pad.query_caps(None)
            if (
                caps is not None
                and caps.to_string().startswith("audio/")
                and pad.link(sink) != Gst.PadLinkReturn.OK
            ):
                print(
                    f"WARN: failed to link decoded audio pad into {downstream.get_name()}",
                    flush=True,
                )

        decoder.connect("pad-added", _on_pad_added)

    def _link_rtsp_video(self, source: Gst.Element, decoder: Gst.Element) -> None:
        """Attach only the RTSP video RTP pad to a zone's decoder."""
        sink = decoder.get_static_pad("sink")

        def _on_pad_added(_source: Gst.Element, pad: Gst.Pad) -> None:
            if sink.is_linked():
                return
            caps = pad.get_current_caps() or pad.query_caps(None)
            if caps is None or caps.get_size() == 0:
                return
            structure = caps.get_structure(0)
            if (
                structure.get_name() != "application/x-rtp"
                or structure.get_string("media") != "video"
            ):
                return
            if pad.link(sink) != Gst.PadLinkReturn.OK:
                print("WARN: failed to link RTSP video into board decoder", flush=True)

        source.connect("pad-added", _on_pad_added)

    def _build_chain(
        self, specs: tuple[ElementSpec, ...], *, collected: list[Gst.Element] | None = None
    ) -> tuple[Gst.Element, Gst.Element]:
        """Build a linear chain (decodebin links dynamically). Returns (first, last)."""
        elements = [self._make(spec) for spec in specs]
        if collected is not None:
            collected.extend(elements)
        for upstream, downstream in pairwise(elements):
            if upstream.get_factory().get_name() in self._DECODERS:
                self._link_dynamic_video(upstream, downstream)
            elif upstream.get_factory().get_name() == "rtspsrc":
                self._link_rtsp_video(upstream, downstream)
            else:
                self._link(upstream, downstream)
        return elements[0], elements[-1]

    def _build_playlist(self, leg: PlaylistLeg) -> tuple[Gst.Element, Gst.Element | None]:
        """Gapless playlist leg: a video ``concat`` (and, when ``audio_tail`` is set,
        a parallel audio ``concat`` fed by each clip's decodebin audio pad) sequence
        the sub-chains. Returns ``(video_concat, audio_concat | None)``.

        H1 fix: the aggregators' element names carry ``self._source_leg_seq`` (bumped
        once per call) so a content-reload's rebuilt aggregator for the SAME leg label
        (``reload_program`` always reloads the "program" role) never collides with the
        still-live outgoing leg's aggregator of the same name -- see ``_make``'s
        fail-loud ``pipeline.add`` check and this class's ``_source_leg_seq`` docstring
        for the measured defect this closes."""
        self._source_leg_seq += 1
        seq = self._source_leg_seq
        vconcat = self._make(ElementSpec("concat", f"vconcat_{leg.label}_{seq}"))
        aconcat = (
            self._make(ElementSpec("concat", f"aconcat_{leg.label}_{seq}"))
            if leg.audio_tail
            else None
        )
        pad_kinds: dict[str, str] = {}
        for index, subchain in enumerate(leg.subchains):
            elements = [self._make(spec) for spec in subchain]
            decoder: Gst.Element | None = None
            for upstream, downstream in pairwise(elements):
                if upstream.get_factory().get_name() in self._DECODERS:
                    decoder = upstream
                    self._link_dynamic_video(upstream, downstream)
                else:
                    self._link(upstream, downstream)
            vsink = vconcat.request_pad_simple("sink_%u")
            if (
                vsink is None
                or elements[-1].get_static_pad("src").link(vsink) != Gst.PadLinkReturn.OK
            ):
                raise RuntimeError(f"failed to link video sub-chain into {vconcat.get_name()}")
            pad_kinds[vsink.get_name()] = (
                leg.subchain_kinds[index] if leg.subchain_kinds else "program"
            )
            if aconcat is not None and decoder is not None:
                atail = [self._make(spec) for spec in leg.audio_tail]
                for upstream, downstream in pairwise(atail):
                    self._link(upstream, downstream)
                self._link_dynamic_audio(decoder, atail[0])
                asink = aconcat.request_pad_simple("sink_%u")
                if (
                    asink is None
                    or atail[-1].get_static_pad("src").link(asink) != Gst.PadLinkReturn.OK
                ):
                    raise RuntimeError(f"failed to link audio sub-chain into {aconcat.get_name()}")
        if leg.label == "program":
            self._board_live_concat_kinds[vconcat.get_name()] = pad_kinds
            if hasattr(vconcat, "connect"):
                vconcat.connect("notify::active-pad", self._on_board_concat_active_notify)
            if self._program_video_concat is None:
                self._program_video_concat = vconcat
        return vconcat, aconcat

    def _on_board_concat_active_notify(self, concat: Gst.Element, _pspec: object) -> None:
        GLib.idle_add(self._apply_board_concat_active, concat)

    def _apply_board_concat_active(self, concat: Gst.Element) -> bool:
        if concat != self._program_video_concat:
            return False
        pad = concat.get_property("active-pad")
        kind = self._board_live_concat_kinds.get(concat.get_name(), {}).get(
            pad.get_name() if pad is not None else "", "program"
        )
        self._board_live_segment = kind == "cg"
        self._set_board_live_visibility()
        return False

    def _set_board_live_visibility(self) -> None:
        alpha = 1.0 if self._board_live_program_selected and self._board_live_segment else 0.0
        for name, pad in self._overlay_layer_pads.items():
            if name.startswith("board_live_"):
                pad.set_property("alpha", alpha)

    def _build_caption_embed(self, leg: CaptionEmbedLeg, video_prev: Gst.Element) -> Gst.Element:
        """S11a: insert the CEA-708 caption embed leg on the output half.

        ``video_prev`` is the encoder chain's tail (h264parse, ALREADY-ENCODED H.264).
        Per the documented gst-plugins-bad pipeline, the encoded video feeds
        ``cccombiner``'s always 'sink' (video) pad while the caption source chain
        (timed text → tttocea608 → ccconverter → cc_data) feeds cccombiner's REQUEST
        'caption' pad; cccombiner attaches a caption meta and ``h264ccinserter``
        serializes it as A/53 SEI. Returns the inserter chain's tail (its src feeds the
        mux). A live ``appsrc`` source is captured into ``self.caption_appsrc`` so the
        daemon can push cues. The live SEI presence is POSIX/LPM-validated."""
        combiner_spec = leg.combiner
        if leg.caption_source[0].factory == "appsrc":
            # Live cc_data is already timestamped and padded by ccconverter.
            # cccombiner's optional scheduler retains its private FIFO when a
            # caption request pad is replaced, delaying the program-boundary
            # erase behind outgoing padding. Keep timestamp-based aggregation
            # without that additional FIFO for the live source.
            combiner_spec = replace(combiner_spec, props={**combiner_spec.props, "schedule": False})
        combiner = self._make(combiner_spec)
        self._link(video_prev, combiner)  # encoded H.264 → cccombiner 'sink' (video) pad

        # Caption text chain → cccombiner's request 'caption' pad.
        self._caption_combiner = combiner
        self._caption_source_leg = leg
        self._build_caption_source(leg, combiner)

        # cccombiner → h264ccinserter (→ h264parse) → [mux, linked by the caller].
        prev = combiner
        for spec in leg.inserter_chain:
            element = self._make(spec)
            self._link(prev, element)
            prev = element
        return prev

    def _build_caption_source(
        self, leg: CaptionEmbedLeg, combiner: Gst.Element, *, clear_at_ns: int | None = None
    ) -> None:
        caption_specs = leg.caption_source
        if caption_specs[0].factory == "appsrc":
            # Separate serialized GAP forwarding from appsrc's source task.
            # Queue limits count buffers, NOT serialized events; the admission
            # gate below separately bounds heartbeat GAPs to current + one queued.
            caption_specs = (
                caption_specs[0],
                ElementSpec(
                    "queue",
                    "caption_flow_queue",
                    {
                        "max-size-buffers": 200,
                        "max-size-bytes": 10_485_760,
                        "max-size-time": 1_000_000_000,
                        "leaky": 0,
                    },
                ),
                *caption_specs[1:],
            )
        token = getattr(self, "_caption_program_token", "")
        if token:
            caption_specs = tuple(
                replace(spec, name=f"{spec.name}_{token}" if spec.name else None)
                for spec in caption_specs
            )
        collected: list[Gst.Element] = []
        cap_first, cap_last = self._build_chain(caption_specs, collected=collected)
        self._caption_source_elements = collected
        if cap_first.get_factory().get_name() == "appsrc":
            self.caption_appsrc = cap_first  # daemon pushes cues here (push_caption_cue)
            queue = self.pipeline.get_by_name(
                f"caption_flow_queue_{token}" if token else "caption_flow_queue"
            )
            if queue is None:
                raise RuntimeError("live caption forwarding queue was not built")
            pad = queue.get_static_pad("src")
            gate = self._caption_gap_gate

            def _entered_caption_downstream(_pad: Any, info: Any) -> Any:
                event = info.get_event()
                if event.type == Gst.EventType.GAP:
                    # This means entered the push, NOT consumed downstream. If
                    # that push blocks, only one additional GAP can wait behind it.
                    gate.entered_downstream(int(event.get_seqnum()))
                return Gst.PadProbeReturn.OK

            probe_id = pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, _entered_caption_downstream)
            self._caption_gap_probe = (pad, probe_id)
        if clear_at_ns is not None:
            # A blank timed-text page needs several CEA control frames before
            # it displays. Put EDM directly on the existing cc_data path so an
            # outgoing caption is erased on the first incoming video frame.
            erase = self._make(
                ElementSpec(
                    "appsrc",
                    f"caption_erase_{token}",
                    {
                        "is-live": True,
                        "format": 3,
                        "caps": "closedcaption/x-cea-708,format=(string)cc_data,framerate=(fraction)30/1",
                    },
                )
            )
            concat = self._make(
                ElementSpec("concat", f"caption_reset_{token}", {"adjust-base": False})
            )
            self._link(erase, concat)
            self._link(cap_last, concat)
            collected.extend((erase, concat))
            cap_last = concat
            clear = Gst.Buffer.new_allocate(None, 3, None)
            clear.fill(0, b"\xfc\x94\x2c")  # valid field-1 CEA-608 EDM, with parity
            clear.pts = clear_at_ns
            clear.duration = Gst.SECOND // 30
            erase.emit("push-buffer", clear)
            erase.emit("end-of-stream")
        caption_pad = combiner.request_pad_simple("caption")
        if (
            caption_pad is None
            or cap_last.get_static_pad("src").link(caption_pad) != Gst.PadLinkReturn.OK
        ):
            raise RuntimeError("failed to link caption source into cccombiner 'caption' pad")

        self._caption_request_pad = caption_pad

    def _begin_caption_program(self, cutoff_ns: int | None = None) -> None:
        """Replace only the sparse caption source at the actual selector switch.

        The live combiner has no additional scheduling FIFO. The
        encoder, mux, selected video/audio and graphics stay in place. A new
        source also discards appsrc/queue/converter state; late commands must
        carry the new token and cannot refill it with the outgoing program.
        """
        writer = getattr(self, "audio_tap_writer", None)
        if getattr(self, "caption_appsrc", None) is None and writer is None:
            return
        token = uuid.uuid4().hex
        self._caption_program_token = token
        if cutoff_ns is None:
            cutoff_ns = self._pipeline_running_time_ms() * Gst.MSECOND
        self._caption_audio_cutoff_ns = cutoff_ns
        if writer is not None:
            writer.begin_program(token)
        if getattr(self, "caption_appsrc", None) is None:
            return
        old_elements = self._caption_source_elements
        combiner = self._caption_combiner
        pad = self._caption_request_pad
        old_src = old_elements[-1].get_static_pad("src")
        old_src.add_probe(
            Gst.PadProbeType.BUFFER
            | Gst.PadProbeType.BUFFER_LIST
            | Gst.PadProbeType.EVENT_DOWNSTREAM,
            _drop_everything_probe,
        )
        old_src.unlink(pad)
        combiner.release_request_pad(pad)
        probe = self._caption_gap_probe
        if probe is not None:
            probe[0].remove_probe(probe[1])
        self._caption_gap_gate.close()
        self._caption_gap_gate = CaptionGapGate()
        self.caption_appsrc = None
        now_ms = cutoff_ns // Gst.MSECOND
        self._build_caption_source(
            self._caption_source_leg, combiner, clear_at_ns=now_ms * Gst.MSECOND
        )
        self._caption_stream_position_ms = now_ms
        for element in reversed(self._caption_source_elements):
            element.sync_state_with_parent()
        self._send_live_caption_gap(now_ms, LIVE_CAPTION_LEAD_MS)
        thread = threading.Thread(
            target=self._dispose_elements_best_effort,
            args=(old_elements,),
            name="cc-caption-retire",
            daemon=True,
        )
        self._abort_retire_threads = [
            item for item in self._abort_retire_threads if item.is_alive()
        ]
        self._abort_retire_threads.append(thread)
        thread.start()
        print(f"CTRL caption program changed: {token}", flush=True)

    def push_caption_cue(
        self,
        *,
        text: str,
        pts_seconds: float,
        duration_seconds: float,
        program_token: str | None = None,
    ) -> bool:
        """Push one timed-text caption cue into the live caption appsrc (S11a).

        Returns False if no live caption source is built (no-op). The daemon calls this
        via the ``caption`` control command to feed continuous captions from the channel
        caption pipeline; the buffer carries PTS+duration so cccombiner schedules the
        cue against the video. Live behavior is POSIX/LPM-validated."""
        if (program_token or "") != getattr(self, "_caption_program_token", ""):
            return False
        appsrc = self.caption_appsrc
        if appsrc is None:
            return False
        data = text.encode("utf-8")
        buf = Gst.Buffer.new_allocate(None, len(data), None)
        buf.fill(0, data)
        running_time_ms = self._pipeline_running_time_ms()
        aligned_pts_ms = align_live_caption_pts_ms(
            requested_pts_ms=max(0, round(pts_seconds * 1000)),
            running_time_ms=running_time_ms,
            stream_position_ms=self._caption_stream_position_ms,
        )
        duration_ms = max(0, round(duration_seconds * 1000))
        buf.pts = aligned_pts_ms * Gst.MSECOND
        buf.duration = duration_ms * Gst.MSECOND
        pushed = bool(appsrc.emit("push-buffer", buf) == Gst.FlowReturn.OK)
        if pushed:
            self._caption_stream_position_ms = max(
                self._caption_stream_position_ms,
                aligned_pts_ms + duration_ms,
            )
            delivery_clock = getattr(self, "caption_delivery_clock", None)
            if delivery_clock is not None:
                try:
                    delivery_clock.record(text, aligned_pts_ms / 1000.0, duration_ms / 1000.0)
                except OSError as exc:
                    # Metadata failure must not re-send an already accepted page.
                    print(
                        f"WARN: caption delivery clock unavailable: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
        return pushed

    def _pipeline_running_time_ns(self) -> int | None:
        """The pipeline's own running time in nanoseconds, or ``None`` if unreadable.

        ``None`` is deliberately distinct from a measured ``0``: a pipeline that
        has not yet reached its base time really is at zero, while one whose clock
        cannot be read at all is not -- and the U44 guard, whose whole job is to
        decide whether an arrival is LATE, must never act on a number it never
        measured. ``_pipeline_running_time_ms`` renders both of those as 0 (its
        own contract, unchanged), which is the right answer for a printed
        diagnostic and the wrong one for a data-path decision.
        """
        clock = self.pipeline.get_clock()
        if clock is None:
            return None
        base_time = int(self.pipeline.get_base_time())
        clock_time = int(clock.get_time())
        if clock_time < base_time:
            return None
        return clock_time - base_time

    def _pipeline_running_time_ms(self) -> int:
        running_time_ns = self._pipeline_running_time_ns()
        if running_time_ns is None:
            return 0
        return max(0, round(running_time_ns / int(Gst.MSECOND)))

    def _prime_live_caption_stream(self) -> None:
        """Prime the sparse caption pad with a GAP so PLAYING cannot deadlock."""

        appsrc = self.caption_appsrc
        if appsrc is None:
            return
        if self._send_live_caption_gap(0, LIVE_CAPTION_LEAD_MS) is not True:
            raise RuntimeError("failed to prime the live caption stream")
        self._caption_stream_position_ms = LIVE_CAPTION_LEAD_MS

    def _send_live_caption_gap(self, start_ms: int, duration_ms: int) -> bool | None:
        """Reserve before enqueue; None means a heartbeat is already waiting.

        appsrc.send_event accepts serialized GAPs into a private queue; True is
        not proof of delivery. Use Gst's assigned seqnum so a fast queue probe
        or a late send failure cannot clear a different heartbeat reservation.
        Cue buffers retain their existing path and are not counted by this gate.
        """
        appsrc = self.caption_appsrc
        if appsrc is None:
            return False
        event = Gst.Event.new_gap(start_ms * Gst.MSECOND, duration_ms * Gst.MSECOND)
        seqnum = int(event.get_seqnum())
        if not self._caption_gap_gate.reserve(seqnum):
            return None
        try:
            accepted = bool(appsrc.send_event(event))
        except Exception:
            self._caption_gap_gate.cancel(seqnum)
            raise
        if not accepted:
            self._caption_gap_gate.cancel(seqnum)
        return accepted

    def _advance_live_caption_gap(self) -> bool:
        """Keep sparse caption time moving when no caption buffer is present."""

        appsrc = self.caption_appsrc
        if appsrc is None or self._stopping:
            return False
        if self._caption_gap_gate.pending is not None:
            return True
        window = caption_gap_window_ms(
            stream_position_ms=self._caption_stream_position_ms,
            running_time_ms=self._pipeline_running_time_ms(),
        )
        if window is None:
            return True
        start_ms, duration_ms = window
        try:
            accepted = self._send_live_caption_gap(start_ms, duration_ms)
        except Exception as exc:
            accepted = False
            print(f"WARN: live caption GAP enqueue failed: {exc!r}", file=sys.stderr, flush=True)
        if accepted is None:
            return True
        if not accepted:
            self._error = ("caption-gap", "failed to advance live caption stream")
            if self._loop is not None:
                self._loop.quit()
            return False
        self._caption_stream_position_ms = start_ms + duration_ms
        return True

    def _arm_live_caption_gap_heartbeat(self) -> None:
        if self.caption_appsrc is not None and self._caption_gap_heartbeat_id is None:
            self._caption_gap_heartbeat_id = GLib.timeout_add(100, self._advance_live_caption_gap)

    def _build_graphics_overlay(
        self, leg: GraphicsOverlayLeg, video_prev: Gst.Element
    ) -> Gst.Element:
        """S15 graphics-overlay leg: composite the station bug/logo (and any other
        image layer, e.g. a pre-rendered lower-third text banner) over the program
        video on the output half, between the selector and the encoder chain.

        ``video_prev`` is the selector (or whatever upstream element the caller has
        built so far). The base program video and every overlay layer are uploaded to
        D3D11 GPU memory when that compositor is selected, then downloaded back
        before the encoder. The software ``compositor`` uses system-memory
        converters instead. Both paths retain the channel's fixed output canvas.
        Returns the tail element (``videoconvert`` after the download)
        the caller links into its encoder chain.

        The compositor and each layer's pad/elements are retained on
        ``self._overlay_compositor``/``self._overlay_layer_pads``/
        ``self._overlay_layer_elements`` so a later content-reload
        (``reload_graphics_overlay``) can add/swap/remove layers by name on the
        already-PLAYING pipeline instead of silently ignoring the reload's overlay
        leg (BLOCKER fix, 2026-08-30 audit)."""
        compositor = self._make(leg.compositor)
        self._overlay_compositor = compositor
        self._overlay_gpu = leg.compositor.factory.startswith("d3d11")

        base_upload = self._make(
            ElementSpec(
                "d3d11upload" if self._overlay_gpu else "videoconvert",
                name="graphics_overlay_base_upload",
            )
        )
        self._link(video_prev, base_upload)
        base_pad = compositor.request_pad_simple("sink_%u")
        if (
            base_pad is None
            or base_upload.get_static_pad("src").link(base_pad) != Gst.PadLinkReturn.OK
        ):
            raise RuntimeError("failed to link program video into the graphics-overlay compositor")

        for layer in leg.layers:
            layer_pad, elements = self._instantiate_overlay_layer(layer, compositor)
            self._overlay_layer_pads[layer.name] = layer_pad
            self._overlay_layer_elements[layer.name] = elements
            self._overlay_layer_image_paths[layer.name] = layer.image_path

        download = self._make(
            ElementSpec(
                "d3d11download" if self._overlay_gpu else "videoconvert",
                name="graphics_overlay_download",
            )
        )
        self._link(compositor, download)
        post_convert = self._make(ElementSpec("videoconvert", name="graphics_overlay_post_convert"))
        self._link(download, post_convert)
        # A scrolling strip can be wider than the picture. Keep the compositor's
        # canvas at the channel profile instead of expanding and shrinking video.
        for spec in self.graph.encoder:
            caps = spec.props.get("caps")
            if spec.factory == "capsfilter" and isinstance(caps, str) and "video/x-raw" in caps:
                canvas = self._make(ElementSpec("capsfilter", props={"caps": caps}))
                self._link(post_convert, canvas)
                return canvas
        return post_convert

    def _instantiate_overlay_layer(
        self, layer: GraphicsOverlayLayer, compositor: Gst.Element
    ) -> tuple[Gst.Pad, list[Gst.Element]]:
        """Build one graphics-overlay image layer's still-image chain
        (``filesrc ! decodebin ! videoconvert ! d3d11upload``) and link it into a
        NEW compositor request pad, applying the layer's position/size/alpha/
        repeat-after-eos properties. Shared by the initial ``_build_graphics_overlay``
        (pipeline not yet PLAYING — no explicit state sync needed, the top-level
        ``set_state(PLAYING)`` cascades to every element already added) and by
        ``_swap_overlay_layer`` (the content-reload re-apply path on an
        already-PLAYING pipeline, which arms a first-buffer probe THEN calls
        ``sync_state_with_parent()`` itself — mirrors ``reload_program``'s ENG-002
        ordering, so the caller controls when/whether to sync).

        Element names are suffixed with a monotonic sequence number
        (``self._overlay_layer_seq``) so a reload's rebuilt chain for an
        already-built layer name never collides with the still-live old chain it
        is about to replace. Returns ``(compositor_sink_pad, elements)`` — the
        caller collects/disposes ``elements`` as a unit, exactly like a source leg."""
        self._overlay_layer_seq += 1
        collected: list[Gst.Element] = []
        self._collecting = collected
        try:
            chain = (
                layer.source_chain
                if layer.source_chain
                else (
                    ElementSpec("filesrc", props={"location": layer.image_path}),
                    ElementSpec("decodebin"),
                )
            ) + (
                ElementSpec("videoconvert"),
                ElementSpec(
                    "d3d11upload" if self._overlay_gpu else "videoconvert",
                    name=f"graphics_overlay_upload_{layer.name}_{self._overlay_layer_seq}",
                ),
            )
            _first, layer_tail = self._build_chain(chain)
        finally:
            self._collecting = None
        layer_pad = compositor.request_pad_simple("sink_%u")
        if (
            layer_pad is None
            or layer_tail.get_static_pad("src").link(layer_pad) != Gst.PadLinkReturn.OK
        ):
            raise RuntimeError(
                f"failed to link graphics-overlay layer {layer.name!r} into the compositor"
            )
        layer_pad.set_property("xpos", layer.xpos)
        layer_pad.set_property("ypos", layer.ypos)
        if layer.width:
            layer_pad.set_property("width", layer.width)
        if layer.height:
            layer_pad.set_property("height", layer.height)
        layer_pad.set_property(
            "alpha",
            layer.alpha
            if not layer.name.startswith("board_live_")
            or (self._board_live_program_selected and self._board_live_segment)
            else 0.0,
        )
        if layer.name == "emergency":
            layer_pad.set_property("zorder", 10000)
        # A still-image filesrc/decodebin chain EOSes its compositor pad after its
        # single buffer (the bundled runtime ships no `imagefreeze`); repeat-after-eos
        # holds that last buffer on screen instead of dropping the pad — proven live
        # (see the S15 graphics-overlay pipeline proof test).
        layer_pad.set_property("repeat-after-eos", layer.repeat_after_eos)
        still_path = layer.image_path
        if layer.source_chain and layer.source_chain[0].factory == "filesrc":
            still_path = str(layer.source_chain[0].props.get("location", ""))
        if layer.repeat_after_eos and still_path and Path(still_path).suffix.lower() == ".png":
            # PNG decoders can give their sole still a 40ms duration. The live
            # compositor may already be past that window when decoding finishes;
            # GstVideoAggregator skips that buffer before repeat-after-eos can
            # retain it. A static image has no expiry. Leave live layer timing alone.
            def retain_still(_pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
                with info.writable_object() as buffer:
                    if buffer is not None:
                        buffer.duration = Gst.CLOCK_TIME_NONE
                return Gst.PadProbeReturn.OK

            layer_tail.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, retain_still)
        return layer_pad, collected

    def _build_secondary_audio(self, leg: SecondaryAudioLeg, mux: Gst.Element) -> None:
        """S11 gap 9: build one secondary audio program and mux it as an extra audio PID.

        ``leg.source`` produces raw audio (its tail is typically a ``decodebin`` whose
        audio pad appears dynamically); ``leg.encoder`` is the AAC chain. The encoder
        tail links to the mux, which assigns a new audio PID, and the stream is tagged
        with ``leg.language`` for the PID's ISO-639 language descriptor. Live PID
        assignment + descriptor are POSIX/LPM-validated."""
        src_elements = [self._make(spec) for spec in leg.source]
        for upstream, downstream in pairwise(src_elements):
            if upstream.get_factory().get_name() not in self._DECODERS:
                self._link(upstream, downstream)
        enc_elements = [self._make(spec) for spec in leg.encoder]
        for upstream, downstream in pairwise(enc_elements):
            self._link(upstream, downstream)
        # source tail → encoder head: dynamic when the tail is a decodebin (audio pad
        # arrives later), else a static link.
        src_tail = src_elements[-1]
        if src_tail.get_factory().get_name() in self._DECODERS:
            self._link_dynamic_audio(src_tail, enc_elements[0])
        else:
            self._link(src_tail, enc_elements[0])
        self._link(enc_elements[-1], mux)  # encoder tail → mux (requests a new audio PID)
        self._pending_lang_tags.append((enc_elements[-1], leg.language))

    def _build_audio_tap(self, source: Gst.Element, leg: AudioTapLeg) -> None:
        """Fork selected raw program audio into the atomic rolling WAV writer."""

        writer = RollingWavSegmentWriter(
            leg.tap_dir,
            segment_seconds=leg.segment_seconds,
        )
        specs = _audio_tap_element_specs()
        elements = [self._make(spec) for spec in specs]
        self._link(source, elements[0])
        for upstream, downstream in pairwise(elements):
            self._link(upstream, downstream)
        appsink = elements[-1]

        def _on_new_sample(sink: Gst.Element) -> Gst.FlowReturn:
            sample = sink.emit("pull-sample")
            if sample is None:
                return Gst.FlowReturn.ERROR
            buffer = sample.get_buffer()
            if buffer is None:
                return Gst.FlowReturn.ERROR
            mapped, map_info = buffer.map(Gst.MapFlags.READ)
            if not mapped:
                return Gst.FlowReturn.ERROR
            try:
                token = getattr(self, "_caption_program_token", None)
                cutoff_ns = getattr(self, "_caption_audio_cutoff_ns", None)
                if cutoff_ns is not None:
                    segment = sample.get_segment()
                    running_ns = segment.to_running_time(Gst.Format.TIME, buffer.pts)
                    if running_ns == Gst.CLOCK_TIME_NONE or running_ns < cutoff_ns:
                        return Gst.FlowReturn.OK
                writer.write_pcm_s16le(map_info.data, program_token=token)
            except Exception as exc:
                self._error = f"caption audio tap failed: {exc}"
                print(f"ERROR: {self._error}", flush=True)
                if self._loop is not None:
                    GLib.idle_add(self._loop.quit)
                return Gst.FlowReturn.ERROR
            finally:
                buffer.unmap(map_info)
            return Gst.FlowReturn.OK

        appsink.connect("new-sample", _on_new_sample)
        self.audio_tap_appsink = appsink
        self.audio_tap_writer = writer
        # A restarted worker must not reuse a prior worker's program fence.
        if (writer.tap_dir / "program-boundary.json").exists():
            token = uuid.uuid4().hex
            self._caption_program_token = token
            writer.begin_program(token)
            print(f"CTRL caption program changed: {token}", flush=True)

    @staticmethod
    def _tag_audio_language(element: Gst.Element, language: str) -> None:
        """Best-effort: stamp the audio stream's ISO-639 language so mpegtsmux writes a
        language descriptor on the PID. Best-effort by design — a tagging hiccup must
        never wedge playout; the live descriptor is POSIX/LPM-validated."""
        try:
            src = element.get_static_pad("src")
            if src is None:
                return
            tags = Gst.TagList.new_empty()
            tags.add_value(Gst.TagMergeMode.REPLACE, "language-code", language)
            src.push_event(Gst.Event.new_tag(tags))  # TAG is a downstream event; push, not send
        except Exception as exc:  # a tagging failure must not kill the channel
            print(f"WARN: secondary audio language tag failed ({language!r}): {exc!r}", flush=True)

    def _flush_lang_tags(self) -> None:
        """Push deferred ISO-639 language TAG events after the pipeline reaches PLAYING.
        Must be called post-_await_playing() so push_event flows into mpegtsmux."""
        for element, language in self._pending_lang_tags:
            self._tag_audio_language(element, language)
        self._pending_lang_tags.clear()

    #: ``input-selector`` properties this engine sets DELIBERATELY on every
    #: selector it builds, rather than inheriting whatever the linked GStreamer
    #: build happens to default to. Each is load-bearing for the boundary-aligned
    #: deferred switch (``reload_program(switch_at_end_of_current=True)``):
    #:
    #: * ``sync-streams=True`` — an inactive pad's buffers are held/dropped
    #:   against the ACTIVE stream's running time instead of racing ahead. The
    #:   deferred switch never relies on this (its new leg is held upstream of
    #:   the selector by a blocking pad probe and pushes nothing at all while
    #:   inactive), but leaving it on means a leg that somehow DOES push while
    #:   inactive — a live source on the slate role, say — cannot flood the
    #:   selector.
    #: * ``sync-mode=active-segment`` — sync inactive pads against the active
    #:   pad's SEGMENT running time, not the pipeline clock. The program legs are
    #:   non-live (filesrc→decodebin): their running time is segment-derived and
    #:   does not track the clock when the sinks do not pace the pipeline, so
    #:   ``clock`` mode would compare two unrelated timebases.
    #: * ``cache-buffers=False`` — an inactive pad must NEVER accumulate buffers.
    #:   Caching them is exactly the unbounded-RSS failure the hold-probe design
    #:   exists to avoid on a 24/7 channel: a leg prepared minutes ahead of its
    #:   boundary would otherwise queue every one of those minutes in memory.
    #: * ``drop-backwards=True`` — retain GStreamer's secondary discontinuity guard
    #:   for pads that have already been skipped. Fresh held pads do not yet carry
    #:   that internal marker, so the commit path also closes its outgoing timestamp
    #:   boundary with nonblocking selector-sink DROP probes before sampling it.
    _SELECTOR_PROPS: ClassVar[dict[str, object]] = {
        "sync-streams": True,
        "sync-mode": 0,
        "cache-buffers": False,
        "drop-backwards": True,
    }

    def _make_selector(self, name: str) -> Gst.Element:
        selector = self._make(ElementSpec("input-selector", name))
        for key, value in self._SELECTOR_PROPS.items():
            selector.set_property(key, value)
        return selector

    _SELECTOR_ISOLATION_QUEUE_PROPS: ClassVar[dict[str, object]] = {
        # Explicit GStreamer 1.28.5 queue defaults. A queue starts forwarding as
        # soon as data arrives; these are capacity bounds, not one second of added
        # latency. Non-leaky is load-bearing: station media must never be dropped.
        "max-size-buffers": 200,
        "max-size-bytes": 10_485_760,
        "max-size-time": 1_000_000_000,
        "leaky": 0,
    }

    def _make_selector_isolation_queue(self, name: str) -> Gst.Element:
        queue = self._make(ElementSpec("queue", name))
        for key, value in self._SELECTOR_ISOLATION_QUEUE_PROPS.items():
            queue.set_property(key, value)
        return queue

    def _build(self) -> None:
        # Output half (stays PLAYING): selector → encode chain → mux → sink(s).
        self.selector = self._make_selector("sel")
        video_isolation_queue = self._make_selector_isolation_queue(
            "program_video_selector_isolation"
        )
        self._link(self.selector, video_isolation_queue)
        prev = video_isolation_queue
        encoder_specs = self.graph.encoder
        if encoder_specs and encoder_specs[0].name == "cg_board_overlay":
            # The board raster is opaque. Place it below the compositor so a
            # configured live-video zone remains visible in the final output.
            board_raster = self._make(encoder_specs[0])
            self._link(prev, board_raster)
            prev = board_raster
            encoder_specs = encoder_specs[1:]
        if self.graph.graphics_overlay is not None:
            # S15 graphics-overlay leg: station bug/logo (+ lower-third banner) burned
            # in on the output half, before encode, so it survives every source
            # swap/reload untouched — same insertion point as the S15 §5 CG-lite
            # full-frame board raster in bridge.graph_from_config.
            prev = self._build_graphics_overlay(self.graph.graphics_overlay, prev)
        for spec in encoder_specs:
            element = self._make(spec)
            self._link(prev, element)
            prev = element
        if self.graph.captions is not None:
            # S11a: insert the CEA-708 embed leg between the encoder and output fan-out.
            prev = self._build_caption_embed(self.graph.captions, prev)
        if self.graph.output_branches:
            self._build_output_branches(prev)
        else:
            mux = self._make(self.graph.mux)
            self.mux = mux  # S9-5: the stall watchdog counts buffers on the mux src pad
            self._link(prev, mux)

            if self.graph.audio_encoder:
                self.audio_selector = self._make_selector("asel")
                audio_isolation_queue = self._make_selector_isolation_queue(
                    "program_audio_selector_isolation"
                )
                self._link(self.audio_selector, audio_isolation_queue)
                audio_prev = audio_isolation_queue
                if self.graph.audio_tap is not None:
                    audio_tee = self._make(ElementSpec("tee", "caption_audio_tap_tee"))
                    self._link(audio_prev, audio_tee)
                    self._build_audio_tap(audio_tee, self.graph.audio_tap)
                    audio_prev = audio_tee
                for spec in self.graph.audio_encoder:
                    element = self._make(spec)
                    self._link(audio_prev, element)
                    audio_prev = element
                self._link(audio_prev, mux)  # audio parser → mux (requests an audio sink pad)

            # S11 gap 9: each secondary audio program (SAP / descriptive) is its own
            # continuous source → AAC → an ADDITIONAL mux audio PID (the TV SAP button).
            for secondary in self.graph.secondary_audio:
                self._build_secondary_audio(secondary, mux)

            if len(self.graph.sinks) == 1:
                tail = mux
                for spec in self.graph.sinks[0]:
                    element = self._make(spec)
                    self._link(tail, element)
                    tail = element
            else:
                tee = self._make(ElementSpec("tee", "t"))
                self._link(mux, tee)
                for branch in self.graph.sinks:
                    tail = tee  # tee src pads are request pads; link() requests one
                    for spec in branch:
                        element = self._make(spec)
                        self._link(tail, element)
                        tail = element

        # Source halves (hot-swappable): each leg → a video (and optional audio) pad.
        for leg in self.graph.sources:
            out_pad, audio_out_pad, elements = self._instantiate_source_leg(leg)
            self._source_leg_elements.append(elements)
            video_sink_pad, audio_sink_pad = self._link_leg_to_selectors(
                leg.label, out_pad, audio_out_pad
            )
            self.selector_sink_pads.append(video_sink_pad)
            if audio_sink_pad is not None:
                self.audio_sink_pads.append(audio_sink_pad)

        if self.selector_sink_pads:
            self.selector.set_property("active-pad", self.selector_sink_pads[0])

    def _build_output_branches(self, encoded_video: Gst.Element) -> None:
        """Fan shared encoded video and selected raw audio into independent muxes."""
        branches = self.graph.output_branches
        video_source = encoded_video
        if len(branches) > 1:
            video_source = self._make(ElementSpec("tee", "output_video_tee"))
            self._link(encoded_video, video_source)

        has_audio = any(branch.audio_chain for branch in branches)
        audio_source: Gst.Element | None = None
        if has_audio:
            self.audio_selector = self._make_selector("asel")
            audio_isolation_queue = self._make_selector_isolation_queue(
                "program_audio_selector_isolation"
            )
            self._link(self.audio_selector, audio_isolation_queue)
            audio_source = audio_isolation_queue
            if self.graph.audio_tap is not None:
                audio_tap_tee = self._make(ElementSpec("tee", "caption_audio_tap_tee"))
                self._link(audio_source, audio_tap_tee)
                self._build_audio_tap(audio_tap_tee, self.graph.audio_tap)
                audio_source = audio_tap_tee
            if sum(bool(branch.audio_chain) for branch in branches) > 1:
                output_audio_tee = self._make(ElementSpec("tee", "output_audio_tee"))
                self._link(audio_source, output_audio_tee)
                audio_source = output_audio_tee

        muxes: list[tuple[ElementSpec, Gst.Element]] = []
        for branch in branches:
            mux = self._make(branch.mux)
            muxes.append((branch.mux, mux))

            video_tail = video_source
            for spec in branch.video_chain:
                element = self._make(spec)
                self._link(video_tail, element)
                video_tail = element
            self._link(video_tail, mux)

            if branch.audio_chain:
                if audio_source is None:
                    raise RuntimeError(
                        f"output branch {branch.label!r} has audio but the graph has no audio selector"
                    )
                audio_tail = audio_source
                for spec in branch.audio_chain:
                    element = self._make(spec)
                    if spec.factory == "volume" and any(
                        item.factory == "audioloudnorm" for item in branch.audio_chain
                    ):
                        element.set_property("volume", 10 ** (self._program_output_gain_db / 20))
                        self._board_output_gain_elements.append(element)
                    self._link(audio_tail, element)
                    audio_tail = element
                self._link(audio_tail, mux)

            # Secondary SAP/descriptive programs are MPEG-TS audio PIDs. They do
            # not belong in FLV/RTMP output branches.
            if branch.mux.factory == "mpegtsmux":
                for secondary in self.graph.secondary_audio:
                    self._build_secondary_audio(secondary, mux)

            sink_tail = mux
            for spec in branch.sinks:
                element = self._make(spec)
                self._link(sink_tail, element)
                sink_tail = element

        # The watchdog observes one actual mux output. Prefer TS when the graph
        # has one; it carries the existing buffer-list counter contract.
        self.mux = next(
            (mux for spec, mux in muxes if spec.factory == "mpegtsmux"),
            muxes[0][1] if muxes else None,
        )

    def _set_board_output_gain(self, gain_db: float) -> None:
        """Preserve board-bed level after per-output loudness normalization."""
        for element in getattr(self, "_board_output_gain_elements", ()):
            element.set_property("volume", 10 ** (gain_db / 20))

    def _instantiate_source_leg(
        self, leg: SourceLeg | PlaylistLeg
    ) -> tuple[Gst.Pad | None, Gst.Pad | None, list[Gst.Element]]:
        """Build one source leg's elements (collected so the leg can be disposed as a
        unit on a content-reload). Returns ``(video_src_pad, audio_src_pad, elements)``.

        F2 fix (hostile-review follow-up, 2026-09-06): a build failure partway
        through (e.g. ``_make``'s fail-loud ``pipeline.add`` check, item 1) used
        to leave whatever elements it HAD already added to the pipeline before
        the failure permanently leaked -- this method's own caller
        (``reload_program``, for a content-reload) has no ``pending`` entry to
        route the cleanup through at this point (that entry is only created
        AFTER this call succeeds), so the disposal has to happen right here,
        at the only place still holding a reference to ``collected``."""
        collected: list[Gst.Element] = []
        self._collecting = collected
        try:
            audio_out_pad = None
            if isinstance(leg, PlaylistLeg):
                video_concat, audio_concat = self._build_playlist(leg)
                out_pad = video_concat.get_static_pad("src")
                if audio_concat is not None:
                    audio_out_pad = audio_concat.get_static_pad("src")
            else:
                _first, video_out = self._build_chain(leg.elements)
                out_pad = video_out.get_static_pad("src")
                if leg.audio:
                    _audio_first, audio_out = self._build_chain(leg.audio)
                    audio_out_pad = audio_out.get_static_pad("src")
        except Exception:
            self._dispose_elements_best_effort(collected)
            raise
        finally:
            self._collecting = None
        return out_pad, audio_out_pad, collected

    def _dispose_elements_best_effort(self, elements: list[Gst.Element]) -> None:
        """NULL + remove a list of elements from the pipeline, swallowing every
        error -- used from an already-failing build/link path (F2 fix) where
        raising a SECOND exception would replace the caller's real one. Mirrors
        ``_dispose_source_leg``'s element half but skips the pad/selector half
        (a caller here never got as far as linking anything to a selector)."""
        for element in elements:
            with contextlib.suppress(Exception):
                element.set_state(Gst.State.NULL)
        for element in elements:
            with contextlib.suppress(Exception):
                self.pipeline.remove(element)

    def _link_leg_to_selectors(
        self, label: str, out_pad: Gst.Pad | None, audio_out_pad: Gst.Pad | None
    ) -> tuple[Gst.Pad, Gst.Pad | None]:
        """Request selector sink pad(s) and link this leg's src pad(s) into them.
        Returns ``(video_sink_pad, audio_sink_pad | None)``; raises on a link failure.

        F2 fix (hostile-review follow-up, 2026-09-06): every raise path below now
        releases whatever selector request pad(s) IT ITSELF already requested
        before failing -- a partial failure (audio raises after video already
        linked, say) used to leave an orphaned, still-requested selector sink
        pad behind forever (a request pad is never automatically released; only
        ``release_request_pad`` frees it). The caller (``reload_program``) still
        disposes the LEG'S ELEMENTS on this raise (see its own try/except); this
        method is only responsible for pads IT requested."""
        selector = self.selector
        if selector is None:
            raise RuntimeError("video selector was not built")
        sink_pad = selector.request_pad_simple("sink_%u")
        try:
            video_linked = (
                out_pad is not None
                and sink_pad is not None
                and out_pad.link(sink_pad) == Gst.PadLinkReturn.OK
            )
        except Exception as exc:  # gi may raise on a caps mismatch
            self._release_selector_pad_best_effort(selector, sink_pad)
            raise RuntimeError(
                f"failed to link source {label!r} into selector (caps mismatch?): {exc}"
            ) from exc
        if not video_linked:
            self._release_selector_pad_best_effort(selector, sink_pad)
            raise RuntimeError(f"failed to link source {label!r} into selector (caps mismatch?)")

        audio_sink_pad = None
        audio_selector = self.audio_selector
        if audio_selector is not None:
            # A/V index alignment (audit CRITICAL): when audio is enabled EVERY
            # leg must carry audio so video pad N and audio pad N swap together —
            # a mixed graph would desync the selectors (wrong audio over wrong
            # video, the issue-#56 class).
            if audio_out_pad is None:
                self._release_selector_pad_best_effort(selector, sink_pad, linked_pad=out_pad)
                raise RuntimeError(f"audio is enabled but source {label!r} has no audio leg")
            audio_sink_pad = audio_selector.request_pad_simple("sink_%u")
            if audio_sink_pad is None or audio_out_pad.link(audio_sink_pad) != Gst.PadLinkReturn.OK:
                self._release_selector_pad_best_effort(selector, sink_pad, linked_pad=out_pad)
                self._release_selector_pad_best_effort(audio_selector, audio_sink_pad)
                raise RuntimeError(f"failed to link audio for source {label!r}")
        return sink_pad, audio_sink_pad

    @staticmethod
    def _release_selector_pad_best_effort(
        selector: Gst.Element, sink_pad: Gst.Pad | None, *, linked_pad: Gst.Pad | None = None
    ) -> None:
        """Unlink (if ``linked_pad`` proves it was actually linked) and release
        one selector request pad, swallowing any error -- called only from an
        already-failing path, so this must never raise a SECOND exception that
        would replace the caller's real one."""
        if sink_pad is None:
            return
        with contextlib.suppress(Exception):
            if linked_pad is not None:
                linked_pad.unlink(sink_pad)
            selector.release_request_pad(sink_pad)

    # -- runtime -----------------------------------------------------------------

    def _on_bus(self, _bus: Gst.Bus, message: Gst.Message) -> bool:
        if message.type == Gst.MessageType.ERROR:
            # A finite outgoing playlist can report GST_FLOW_ERROR while its
            # already-off-air demux/decode chain is being detached and driven to
            # NULL.  Once selector handoff has happened this error belongs to
            # retirement; the selected replacement and persistent output path are
            # still healthy.  Contain only errors proven to come from that old leg.
            pending = self._pending_reload
            if (
                pending is not None
                and pending.get("commit_in_progress", False)
                and pending.get("selector_handoff_confirmed", False)
                and self._belongs_to_retiring_reload(message.src)
            ):
                err, _debug = message.parse_error()
                source_name = "unknown"
                with contextlib.suppress(Exception):
                    source_name = message.src.get_name()
                print(
                    "CTRL reload: contained retiring old-leg error after confirmed selector handoff "
                    f"(source={source_name}): {err}",
                    flush=True,
                )
                return True
            # ENG-009: an async error on the not-yet-committed reload leg (e.g. a live
            # source whose connection is refused) must NOT take the channel off air.
            # Abort the reload and keep the current program playing.
            if (
                self._pending_reload is not None
                and not self._pending_reload.get("commit_in_progress", False)
                and self._belongs_to_pending_reload(message.src)
            ):
                err, _debug = message.parse_error()
                # Round-2: name the element that actually failed. The round-1
                # trace recorded only the error text, which left the reviewer --
                # and this round -- unable to tell an errored NEW leg apart from a
                # shared element that ``_belongs_to_pending_reload`` merely
                # attributed to it. The source name makes the next occurrence
                # diagnosable instead of a guess.
                source_name = "unknown"
                with contextlib.suppress(Exception):
                    source_name = message.src.get_name()
                print(
                    "CTRL reload aborted: new program errored before commit "
                    f"(source={source_name}): {err}; debug="
                    + re.sub(
                        r"[A-Za-z][A-Za-z0-9+.-]*://\S+", "[source URI redacted]", str(_debug)
                    ),
                    flush=True,
                )
                self._abort_pending_reload("error")
                return True
            # Once commit begins, the replacement is already selected and is no
            # longer safe to abort as an uncommitted leg. Its error follows the
            # ordinary fatal channel path below so supervisor recovery is truthful.
            # Mirrors the ENG-009 containment above, but for a still-settling
            # graphics-overlay layer swap (e.g. a reloaded lower-third banner PNG
            # that fails to decode): an async error on the NOT-YET-COMMITTED new
            # layer chain must abort only that swap, never take the channel off air.
            overlay_layer_name = self._belongs_to_pending_overlay_swap(message.src)
            if overlay_layer_name is not None:
                err, _debug = message.parse_error()
                print(
                    f"CTRL graphics-overlay reload for layer {overlay_layer_name!r} aborted: "
                    f"new layer errored before commit: {err}",
                    flush=True,
                )
                self._abort_pending_overlay_swap(overlay_layer_name, reason="error")
                return True
            for layer_name, elements in tuple(getattr(self, "_overlay_layer_elements", {}).items()):
                if not layer_name.startswith("board_live_"):
                    continue
                node = message.src
                while node is not None and node not in elements:
                    node = node.get_parent() if hasattr(node, "get_parent") else None
                if node is not None:
                    err, _debug = message.parse_error()
                    print(
                        f"CTRL board live-video layer {layer_name!r} failed; "
                        f"keeping the ordinary board on air: {err}",
                        flush=True,
                    )
                    self._remove_overlay_layer(layer_name)
                    return True
            # Item 3: last attribution before the fatal tail. A leg the engine has
            # ALREADY aborted is not gone until its background retirement finishes,
            # and in that window neither ``_belongs_to_pending_reload`` (slot
            # cleared) nor ``_belongs_to_retiring_reload`` (an abort never had a
            # commit) can attribute it -- so its teardown used to decide the
            # worker's fate. See ``_belongs_to_aborted_leg``.
            if self._belongs_to_aborted_leg(message.src):
                err, _debug = message.parse_error()
                source_name = "unknown"
                with contextlib.suppress(Exception):
                    source_name = message.src.get_name()
                print(
                    "CTRL reload: contained error from an already-aborted reload leg "
                    f"(source={source_name}): {err}",
                    flush=True,
                )
                return True
            self._error = message.parse_error()
            if self._loop is not None:
                self._loop.quit()
        elif message.type == Gst.MessageType.EOS:
            self._announce_pipeline_eos(message.src)
            if self._loop is not None:
                self._loop.quit()
        else:
            # U59 round 4: observe-only. QoS is the one message class this engine
            # has never handled, and it is the decoder's own account of exactly
            # the shape this round is chasing -- a video branch being told to
            # slow down while audio is untouched. Read via getattr because the
            # fake-Gst harnesses in this repo's tests build a MessageType without
            # a QOS member; a diagnostic must not be the reason one fails.
            qos_type = getattr(Gst.MessageType, "QOS", None)
            if qos_type is not None and message.type == qos_type:
                self._u59_record_qos(message)
        return True

    def _announce_pipeline_eos(self, src: Any) -> None:
        """Name the pipeline-level EOS that is about to quit this worker.

        A bus EOS quits the run loop, so the worker exits and the daemon sees a
        CLEAN teardown -- and until U30 no line anywhere said the channel's
        output had ended. An off-live U30 run reproduced exactly that shape: an
        immediate reload committed, the mux then reached the OUTGOING leg's own
        end 2.2 s later, the file stopped growing at that PTS, and the worker
        left with ``{'error': None, 'teardown_clean': True}``. Without this line
        that is indistinguishable in the log from an operator's ``stop``.

        Cheap and always-on: at most one line per worker lifetime, because it
        quits the loop immediately after. The ``[mux-in ...]`` suffix is the
        same interval counter the progress line carries, so the line also says
        what was still feeding the mux when the output ended -- which is what
        separates "video went silent first" from "everything stopped at once".
        The U30 ``[eos-arrivals: ...]`` clause says WHERE that EOS entered the
        output half -- see ``_record_eos_arrival``."""
        name = "unknown"
        with contextlib.suppress(Exception):
            if src is not None:
                name = src.get_name()
        suffix = ""
        with contextlib.suppress(Exception):
            suffix = self._mux_input_delta_suffix(time.monotonic() - self._mux_input_snapshot_t)
        print(
            f"CTRL output: pipeline EOS from {name} -- quitting the worker"
            f"{suffix}{self._eos_arrival_suffix()}",
            file=sys.stderr,
            flush=True,
        )

    # -- S9-5 pipeline supervision: stall watchdog --------------------------------

    def _install_output_counter(self) -> None:
        """Count TS output leaving the mux — the stall watchdog's progress signal.
        A swap/reload keeps the persistent output half PLAYING, so this advances
        through them; it only flatlines on a genuine output stall.

        CRITICAL: past mpegtsmux the data is pushed as ``GstBufferList`` (188-byte TS
        packets batched), NOT individual ``GstBuffer`` — a plain ``BUFFER`` probe never
        fires there and the watchdog would false-fire on perfectly healthy output. The
        mask MUST include ``BUFFER_LIST`` (proven: BUFFER-only counts 0 while the sink
        file grows; BUFFER|BUFFER_LIST counts 99/189/279 over the same window)."""
        if self.mux is None:
            return
        src = self.mux.get_static_pad("src")
        if src is None:
            return

        def _count(_pad: Gst.Pad, _info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
            # One increment per buffer-or-list is enough: the watchdog only needs to
            # see the count ADVANCE, not the exact packet tally.
            #
            # Threading contract: the mux src pad is fed by a single GStreamer streaming
            # thread, so this probe is the ONLY writer of _output_buffers; _check_stall
            # (the GLib main-loop thread) is a reader only. A single-writer int with a
            # plain read needs no lock — the reader may observe a value one behind, which
            # only ever delays a stall verdict by a tick, never causes a false stall.
            self._output_buffers += 1
            return Gst.PadProbeReturn.OK

        src.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST, _count)
        # U30: armed AFTER the counter, so ``probes[0]`` on this pad stays the
        # progress signal the stall watchdog reads (see the test fixtures).
        self._install_eos_observer(src, self._make_mux_src_eos_label())

    # -- U16 start-side diagnostic: what the OUTPUT side first saw ---------------
    #
    # The mirror of the reload-side diagnostic: at worker start, one line per
    # stream naming the running time of the first buffer that reached that
    # stream's mux sink pad. U16's live one-sided +108.95s step was invisible
    # because nothing recorded where each stream's output actually began.
    #
    # BUFFER only, NOT BUFFER_LIST: the buffer-list batching called out in
    # ``_install_output_counter`` happens AFTER ``mpegtsmux`` -- on its SINK pads
    # each stream is still one buffer at a time, and a BUFFER|BUFFER_LIST mask
    # would make the callback name a type this probe can never receive.

    def _make_mux_first_buffer_reporter(self, pad_name: str) -> Any:
        """A one-shot probe callback for one mux sink pad.

        A factory rather than a def-in-loop so the callback closes over ITS OWN
        pad's name, and so the callback's ``__name__`` is stable for logs."""

        def _report_mux_first_buffer(pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
            # Guarded end to end: this runs on a streaming thread, and a
            # diagnostic must never be able to take a channel off air.
            with contextlib.suppress(Exception):
                label = self._mux_pad_stream_label(pad)
                print(
                    self._mux_first_buffer_diagnostic(
                        label=label,
                        pad_name=pad_name,
                        measured=self._measure_first_buffer(pad, info),
                    ),
                    file=sys.stderr,
                    flush=True,
                )
            return Gst.PadProbeReturn.REMOVE

        return _report_mux_first_buffer

    def _install_mux_input_diagnostics(self) -> None:
        """Arm one self-removing first-buffer probe per mux sink pad.

        Called once at worker start, beside ``_install_output_counter``. Silent
        and safe when there is no mux, when the mux exposes no sink pads, or when
        it cannot be iterated at all."""
        mux = getattr(self, "mux", None)
        if mux is None:
            return
        try:
            iterator = mux.iterate_sink_pads()
        except Exception:
            return
        if iterator is None:
            return
        while True:
            try:
                result, pad = iterator.next()
            except Exception:
                return
            if result != Gst.IteratorResult.OK or pad is None:
                return
            try:
                pad_name = pad.get_name()
            except Exception:
                pad_name = "unknown"
            with contextlib.suppress(Exception):
                pad.add_probe(
                    Gst.PadProbeType.BUFFER, self._make_mux_first_buffer_reporter(pad_name)
                )

    # -- U30 input-side diagnostic: which stream is still feeding the mux --------
    #
    # The mux SRC counter says "output is advancing"; it cannot say WHAT is
    # advancing. Both 2026-09-25 incidents froze with the worker alive and the
    # output total still climbing at the audio-only rate, and the reload commit
    # path gave no line that distinguished "the outgoing leg never ended" from
    # "it ended and the settlement was declined". These two additions close
    # exactly those two gaps, and both are always-on and cheap: one int
    # increment per buffer per pad (no allocation, no locking, no logging) and
    # one already-existing log line that gains a suffix.

    def _install_mux_input_counters(self) -> None:
        """Arm one BUFFER counter per mux sink pad, and record the pad set.

        Called once at worker start, beside ``_install_mux_input_diagnostics``,
        and deliberately NOT self-removing (unlike the U16 first-buffer
        reporters): the failure both incidents showed came long after the first
        buffer, so a one-shot probe has nothing left to say about it.

        A pad whose probe cannot be installed is NOT registered. A registered
        pad that can never be probed would read ``+0`` forever and accuse a
        healthy stream of having stopped -- the honest rendering of "we could
        not count this stream" is absence from the line.

        The mux is part of the persistent output half (it never restarts across
        swaps), so its sink pads are the same objects for the whole worker
        lifetime; counting them once covers every later reload.

        Silent and safe with no mux, no sink pads, or an un-iterable pad set --
        same contract as ``_install_mux_input_diagnostics``."""
        # Fresh dicts, not ``clear()``: this method's contract is that it is
        # silent and safe whatever it finds, and a re-arm must not accumulate
        # the previous pad set.
        self._mux_input_pads = {}
        self._mux_input_buffers = {}
        # U44: the arrival guard's reference and tally, created on the same terms
        # -- an empty reference is the SAFE direction (the next arrival is adopted,
        # never dropped), so a re-arm can only ever make the guard more permissive.
        self._mux_pad_arrival_rt = {}
        self._mux_pad_arrival_drops = {}
        mux = getattr(self, "mux", None)
        if mux is None:
            return
        try:
            iterator = mux.iterate_sink_pads()
        except Exception:
            return
        if iterator is None:
            return
        while True:
            try:
                result, pad = iterator.next()
            except Exception:
                return
            if result != Gst.IteratorResult.OK or pad is None:
                return
            try:
                pad_name = pad.get_name()
            except Exception:
                pad_name = "unknown"
            self._mux_input_pads[pad_name] = pad
            self._mux_input_buffers[pad_name] = 0
            try:
                pad.add_probe(Gst.PadProbeType.BUFFER, self._make_mux_input_counter(pad_name))
            except Exception:
                # Could not be counted -> must not be listed (see docstring).
                self._mux_input_pads.pop(pad_name, None)
                self._mux_input_buffers.pop(pad_name, None)
            else:
                # U37: the per-stream watchdog's AIRING frontier, for the
                # registered pads only -- a pad that is not listed is not judged,
                # so a recorder for it would be dead weight. Outside the try
                # above for the same reason ``_install_eos_observer`` is: a
                # refused recorder must not un-register a counter that IS
                # installed (the flow ladder must keep counting this pad).
                self._install_mux_pad_airing_frontier(pad, pad_name)
                # U44: and the data-path guard for these same pads. Inside this
                # ``else`` on purpose: a pad whose BUFFER probe was refused is not
                # registered, so it is neither counted, nor judged, nor guarded --
                # and it is named by the WARN that already fired above.
                self._install_mux_pad_arrival_guard(pad, pad_name)
            # U30: armed after the counter (never inside its try -- a refused
            # observer must not un-register a counter that IS installed), and
            # labelled lazily: this runs before caps are negotiated.
            self._install_eos_observer(pad, self._make_mux_sink_eos_label(pad))

    def _install_mux_pad_airing_frontier(self, pad: Any, pad_name: str) -> None:
        """U37: record when the mux last AIRED a buffer off ``pad``, and at what running time.

        ``emit-signals`` makes the aggregator emit ``buffer-consumed`` for every
        buffer it takes off this pad to build an output buffer, and the signal
        carries the CLIPPED buffer -- whose PTS is this stream's running time at
        the mux. Measured on the installed 1.28 runtime: with ``set_offset(x)``
        on a branch pad, ``consumed.pts - arrival.pts == x`` exactly for every
        buffer, i.e. the recorded value really is the mux's own running time for
        this stream and not the leg's raw timeline.

        Best-effort, but not silent-ignoring: a pad whose signal cannot be armed
        is named in a WARN, because a required stream this watchdog cannot see is
        exactly the blindness U37 exists to close. The pad stays registered
        (``_mux_input_pads``, so the ``mux-in`` flow ladder keeps reporting it)
        and the per-stream judge simply skips it while it has no frontier --
        which is the same "has never produced" exclusion these tests already
        carry, now asked of airing rather than of arrival."""
        try:
            pad.set_property("emit-signals", True)
            pad.connect("buffer-consumed", self._make_mux_pad_consumed_recorder(pad_name))
        except Exception as exc:
            print(
                f"WARN: mux pad {pad_name} cannot report consumed buffers "
                f"({exc!r}); its stream is not judged by running time",
                flush=True,
            )

    def _make_mux_pad_consumed_recorder(self, pad_name: str) -> Any:
        """A per-pad ``buffer-consumed`` recorder closing over ITS OWN pad name.

        A factory rather than a def-in-loop for the same two reasons as
        ``_make_mux_input_counter``: the closure must key on its own pad, and
        ``__name__`` stays stable for logs and tests."""

        def _record_mux_pad_consumed(_pad: Gst.Pad, buffer: Any) -> None:
            # Guarded end to end -- this runs on the mux's streaming thread, and
            # a watchdog input must never be able to take a channel off air.
            # ``Gst.CLOCK_TIME_IS_VALID`` is a C macro and is NOT exposed by
            # PyGObject (measured: AttributeError inside this very handler), so
            # the validity test is spelled out instead.
            with contextlib.suppress(Exception):
                pts = buffer.pts
                if isinstance(pts, int) and 0 <= pts < Gst.CLOCK_TIME_NONE:
                    self._mux_pad_airing_rt[pad_name] = pts

        return _record_mux_pad_consumed

    def _make_mux_input_counter(self, pad_name: str) -> Any:
        """A per-pad BUFFER counter closing over ITS OWN pad name.

        A factory rather than a def-in-loop, for the same two reasons as
        ``_make_mux_first_buffer_reporter``: the closure must key on its own
        pad, and ``__name__`` stays stable for logs and tests."""

        def _count_mux_input(_pad: Gst.Pad, _info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
            # Guarded end to end -- this runs on a streaming thread, and a
            # diagnostic must never be able to take a channel off air. OK, never
            # DROP: this probe observes, it does not police the data path.
            with contextlib.suppress(Exception):
                self._mux_input_buffers[pad_name] = self._mux_input_buffers.get(pad_name, 0) + 1
            return Gst.PadProbeReturn.OK

        return _count_mux_input

    def _install_mux_pad_arrival_guard(self, pad: Any, pad_name: str) -> None:
        """U44: arm the drop guard for a future-dated VIDEO arrival on ``pad``.

        See ``_make_mux_pad_arrival_guard`` for the mechanism and the measured
        numbers. A pad whose probe cannot be installed is NAMED in a WARN: this
        guard's absence is not a diagnostic gap but a live defect left in place, so
        it must not be silent. The pad stays registered either way -- the WARN is
        the only honest rendering of "this stream is not protected"."""
        try:
            pad.add_probe(Gst.PadProbeType.BUFFER, self._make_mux_pad_arrival_guard(pad_name))
        except Exception as exc:
            print(
                f"WARN: mux pad {pad_name} cannot take the arrival guard ({exc!r}); a "
                "future-dated buffer on this stream will NOT be dropped and can stall "
                "the mux for its own full future offset",
                file=sys.stderr,
                flush=True,
            )

    def _make_mux_pad_arrival_guard(self, pad_name: str) -> Any:
        """U44: the DROP probe that keeps a future-dated video arrival off the mux.

        A factory rather than a def-in-loop for the same two reasons as
        ``_make_mux_input_counter``: the closure must key on its own pad, and
        ``__name__`` stays stable for logs and tests.

        WHAT IT PROTECTS. ``gst_base_ts_mux_find_best_pad`` picks the pad to pop
        from on the queued heads' timestamps and ``gst_aggregator_wait_and_check``
        then sleeps until the clock reaches the chosen head's running time, so ONE
        video buffer whose running time lies D seconds in the future starves the
        muxed VIDEO for ~D seconds while audio plays on -- a freeze the flow
        ladders cannot see, because video buffers keep ARRIVING at their normal
        rate the whole time. The live 2026-09-26 government freeze (reload id=4,
        running time 6900.968s, off air 13s later) was 25s of it.

        The producer of that buffer is measured, and it is not the reload: each
        video subchain ends in a ``videorate``, which is forced to push one
        closing-duplicate frame at its subchain boundary (gstvideorate.c:1063/1077,
        on the ``count < 1`` branch no property can switch off), and the leg's
        ``concat`` updates its base for the NEXT subchain (gstconcat.c:589) before
        forwarding that duplicate -- so the duplicate is clipped under the new
        segment and its running time lands one subchain-duration in the future.
        Measured: 10.033s at a 10s subchain, 25.033s at the 25s one that took the
        channel off air. The freeze lasts exactly as long as the subchain, which is
        why a 10s subchain only grazes the 10s per-stream judge.

        WHEN IT DROPS. Only on a pad whose stream label, resolved LAZILY inside the
        probe (caps are not negotiated at install time), is ``video`` -- production
        video subchains end in ``videorate``, the source of the duplicate, while the
        audio tail has no ``audiorate``, and dropping legitimately-clipped AUDIO
        arrivals on this same path was measured at a 1.024s hole in the aired audio.
        Only when BOTH arms fire, so an ordinary forward step cannot be touched:

        * the arrival's running time is more than
          ``_MUX_ARRIVAL_CADENCE_BOUND_NS`` past this pad's previous ACCEPTED
          arrival's -- far above the 33ms video cadence and above the 1.753s
          legitimate switch step; and
        * it is more than ``_MUX_ARRIVAL_AHEAD_BOUND_NS`` ahead of the pipeline's
          own running time -- i.e. the mux would have to WAIT for it, which is the
          freeze itself.

        The second arm is what keeps this off legitimate media: a pre-rebase arrival
        is clipped under the OLD segment and so sits BEHIND the clock (no wait, no
        drop), and an arrival far past its predecessor but already behind the clock
        -- a genuine hole the mux airs immediately -- is likewise left alone.

        The reference advances only on ACCEPTED arrivals: a second future-dated
        arrival behind the first is dropped too, and the first real frame after the
        drop (which lands behind the dropped one) re-seeds it. A BUFFER probe sees
        no segment events, so nothing here can be re-seeded by a segment crossing --
        structurally, not by a check. Cost of a false positive: one frame, against
        which the alternative is D seconds of frozen video.

        Guarded end to end: this runs on a streaming thread and must never be able
        to take a channel off air by raising."""

        def _guard_mux_pad_arrival(pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
            with contextlib.suppress(Exception):
                if self._mux_pad_stream_label(pad) != "video":
                    return Gst.PadProbeReturn.OK
                _pts, running_time, _base = self._measure_first_buffer(pad, info)
                if running_time is None:
                    return Gst.PadProbeReturn.OK
                pipeline_running_time = self._pipeline_running_time_ns()
                previous = self._mux_pad_arrival_rt.get(pad_name)
                if previous is None or pipeline_running_time is None:
                    # First arrival on this pad, or no clock to compare it with:
                    # adopt it as the reference and let it through. Never guess.
                    self._mux_pad_arrival_rt[pad_name] = running_time
                    return Gst.PadProbeReturn.OK
                if (
                    running_time - previous <= _MUX_ARRIVAL_CADENCE_BOUND_NS
                    or running_time - pipeline_running_time <= _MUX_ARRIVAL_AHEAD_BOUND_NS
                ):
                    self._mux_pad_arrival_rt[pad_name] = running_time
                    return Gst.PadProbeReturn.OK
                # Both arms fire: honouring this arrival means sleeping until the
                # clock reaches it, which is the whole defect. Drop it, count it,
                # and do NOT move the reference.
                drops = self._mux_pad_arrival_drops
                drops[pad_name] = drops.get(pad_name, 0) + 1
                print(
                    f"CTRL mux diagnostic: dropped future-dated video arrival "
                    f"pad={pad_name} "
                    f"running_time={_seconds_or_none(running_time)} "
                    f"last_arrival={_seconds_or_none(previous)} "
                    f"pipeline_running_time={_seconds_or_none(pipeline_running_time)} "
                    f"ahead={_seconds_or_none(running_time - pipeline_running_time)} "
                    f"step={_seconds_or_none(running_time - previous)} "
                    f"drops={drops[pad_name]}",
                    file=sys.stderr,
                    flush=True,
                )
                return Gst.PadProbeReturn.DROP
            return Gst.PadProbeReturn.OK

        return _guard_mux_pad_arrival

    def _snapshot_mux_input(self, now: float) -> None:
        """Move the mux-input baseline to ``now``.

        Called at arm time and after every progress print, so each line reports
        the interval since the previous one rather than a cumulative total --
        ``video=+0`` on one interval is the signal, and a cumulative total would
        hide a stream that stopped ten minutes ago behind everything it sent
        before that."""
        with contextlib.suppress(Exception):
            self._mux_input_snapshot = dict(self._mux_input_buffers)
            self._mux_input_snapshot_t = now

    def _mux_input_delta_suffix(self, elapsed: float) -> str:
        """The whole flow ladder as one suffix: the mux clause, then the chain
        clause. ``""`` when nothing was counted anywhere.

        The mux clause is ``" [mux-in <elapsed>s: video=+N audio=+M ...]"`` -- a
        pure read of the counters against the last snapshot, and byte-identical
        to what it rendered before the chain clause existed whenever no chain
        counter was armed. Video first, then audio, then anything else by pad
        name: those two are the streams whose silence changes what the channel
        is airing. See ``_chain_input_delta_suffix`` for the rest of the ladder.

        Guarded throughout, because it runs inside the progress path and must
        never be able to raise into it."""
        pads = getattr(self, "_mux_input_pads", None) or {}
        counters = getattr(self, "_mux_input_buffers", None) or {}
        snapshot = getattr(self, "_mux_input_snapshot", None) or {}
        mux_clause = ""
        if pads and counters:
            try:
                parts: list[tuple[int, str, int]] = []
                for pad_name, pad in pads.items():
                    current = counters.get(pad_name, 0)
                    # A pad missing from the snapshot has no interval baseline yet
                    # (it was registered after the last print): report ``+0`` rather
                    # than invent a delta from a baseline we never took.
                    previous = snapshot.get(pad_name, current)
                    label = self._mux_pad_stream_label(pad)
                    rank = 0 if label == "video" else 1 if label == "audio" else 2
                    parts.append((rank, str(label), current - previous))
                parts.sort(key=lambda item: (item[0], item[1]))
                rendered = " ".join(f"{label}=+{delta}" for _rank, label, delta in parts)
            except Exception:
                return self._chain_input_delta_suffix(elapsed)
            mux_clause = f" [mux-in {elapsed:.1f}s: {rendered}]"
        return mux_clause + self._chain_input_delta_suffix(elapsed)

    # -- U30 flow ladder: WHERE between the selector and the mux data stops -----
    #
    # ``[mux-in ...]`` says whether the output half is still being fed; it cannot
    # say WHERE inside it a stream stopped. The dead immediate-switch runs of the
    # 2026-09-25 off-live campaign show the switched-in leg's buffer arriving on
    # the selector's own input pad, the selector's active-pad readback confirming
    # the switch, and the mux input counters never moving for that leg -- so the
    # loss is in the stretch those counters cannot see:
    # ``selector -> isolation queue -> encoder -> mux``. One counter at each end
    # of it bisects that stretch on the next occurrence, at the same cost as the
    # mux counters (one int increment per buffer, no allocation, no locking, no
    # logging, and no extra line).
    #
    # Rung ``in`` closes the remaining ambiguity: ``sel`` at ``+0`` after a switch
    # says the new leg's data stopped at or before the selector's src pad, which
    # is either "the leg's producer stopped pushing" or "the selector swallowed
    # it". ``in`` counts the leg's buffers arriving at the selector's OWN request
    # sink pad, so ``in +N, sel +0`` names the selector and ``in +0, sel +0``
    # names the producer. Armed per reload (the pad is created by the reload), so
    # it is absent until the first switch and re-baselined at each one.

    _CHAIN_INPUT_RUNGS: ClassVar[tuple[str, ...]] = ("in", "sel", "queue")
    _CHAIN_INPUT_STREAMS: ClassVar[tuple[str, ...]] = ("video", "audio")
    _CHAIN_INPUT_ISOLATION_QUEUES: ClassVar[dict[str, str]] = {
        "video": "program_video_selector_isolation",
        "audio": "program_audio_selector_isolation",
    }

    def _install_chain_input_counters(self) -> None:
        """Arm one BUFFER counter at each end of the selector->queue stretch.

        Rung ``sel`` is the selector's src pad -- what the selector actually
        forwarded -- and rung ``queue`` is the isolation queue's src pad, the
        encode chain's own input. Two rungs, one per stream each: the selectors
        and the queues are part of the persistent output half and never restart
        across a swap, so their src pads are the same objects for the whole
        worker lifetime. Rung ``in`` (see ``_install_new_leg_inbound_counters``)
        is the third and cannot be armed here: its pad only exists once a reload
        has built a replacement leg.

        Keyed by ``(rung, stream)`` rather than by pad name: an
        ``input-selector``'s src pad can be armed before caps are negotiated, and
        a label read from unnegotiated caps falls back to the pad name, which the
        next print could not match (reading ``+0`` forever).

        Same registration contract as ``_install_mux_input_counters``: an element
        that cannot be found, a missing src pad, or a refused probe leaves that
        rung/stream OUT of the map. A registered-but-uncountable rung would read
        ``+0`` forever and accuse a healthy stream of having stopped; absence is
        the honest rendering of "not counted". Silent and safe with no selector,
        no queue, or no pipeline."""
        self._chain_input_pads: dict[tuple[str, str], Gst.Pad] = {}
        self._chain_input_buffers: dict[tuple[str, str], int] = {}
        pipeline = getattr(self, "pipeline", None)
        lookup = getattr(pipeline, "get_by_name", None)

        def _src_pad(element: Any) -> Any:
            """The element's static ``src`` pad, or ``None`` if it has none."""
            get_static_pad = getattr(element, "get_static_pad", None)
            if not callable(get_static_pad):
                return None
            with contextlib.suppress(Exception):
                return get_static_pad("src")
            return None

        def _isolation_queue(stream: str) -> Any:
            """The named isolation queue for ``stream``, or ``None``."""
            if not callable(lookup):
                return None
            with contextlib.suppress(Exception):
                return lookup(self._CHAIN_INPUT_ISOLATION_QUEUES[stream])
            return None

        video_selector = getattr(self, "selector", None)
        audio_selector = getattr(self, "audio_selector", None)
        candidates: list[tuple[str, str, Any, Callable[[], str]]] = [
            # U30: the ``sel`` label carries the selector's active sink pad as it
            # stood when the EOS arrived -- see ``_make_selector_src_eos_label``.
            (
                "sel",
                "video",
                _src_pad(video_selector),
                self._make_selector_src_eos_label(video_selector, "video"),
            ),
            (
                "sel",
                "audio",
                _src_pad(audio_selector),
                self._make_selector_src_eos_label(audio_selector, "audio"),
            ),
        ]
        candidates += [
            (
                "queue",
                stream,
                _src_pad(_isolation_queue(stream)),
                self._make_chain_eos_label("queue", stream),
            )
            for stream in self._CHAIN_INPUT_ISOLATION_QUEUES
        ]
        for rung, stream, pad, label in candidates:
            if pad is None:
                continue
            key = (rung, stream)
            self._chain_input_pads[key] = pad
            self._chain_input_buffers[key] = 0
            try:
                pad.add_probe(Gst.PadProbeType.BUFFER, self._make_chain_input_counter(rung, stream))
            except Exception:
                # Could not be counted -> must not be listed (see docstring).
                self._chain_input_pads.pop(key, None)
                self._chain_input_buffers.pop(key, None)
            # U30: armed after the counter, so ``probes[0]`` stays the counter.
            # A rung label is a constant (or one property read) -- no caps needed.
            self._install_eos_observer(pad, label)

    def _install_new_leg_inbound_counters(self, pending: dict[str, Any]) -> None:
        """Arm rung ``in``: the new leg's buffers arriving at its selector sink pad.

        Called from ``_arm_new_leg_selector_diagnostics`` -- after the rebase
        offsets are set and while the holds still block the first buffer, so no
        buffer can cross before the counter exists. Overwrites the previous
        reload's pad reference for the key, so ``in`` always names the leg that
        is selected NOW. Best-effort per pad, exactly like every other rung: a
        pad that cannot be counted is left out, never registered as ``+0``.

        Rung ``in`` JOINS the ladder; it must never require one to exist.
        ``_install_chain_input_counters`` arms these two maps once per worker and
        this method only adds a key to them, so an object that never armed the
        ladder has no rung ``in`` to add -- and no ladder to render either, since
        ``_chain_input_delta_suffix`` already renders a missing rung as absent
        rather than ``+0``. The shape is real, not hypothetical: the U16
        real-runtime emitter arms the selector-side diagnostics on a bare
        ``object.__new__(GstPlayoutEngine)`` (no ``__init__``, no pipeline), and
        an unguarded write here raised ``AttributeError`` and took that whole
        emit down with it."""
        pads = getattr(self, "_chain_input_pads", None)
        counters = getattr(self, "_chain_input_buffers", None)
        if pads is None or counters is None:
            return
        for label, pad_key in (("video", "new_video_pad"), ("audio", "new_audio_pad")):
            pad = pending.get(pad_key)
            if pad is None or not hasattr(pad, "add_probe"):
                continue
            key = ("in", label)
            pads[key] = pad
            counters[key] = 0
            try:
                pad.add_probe(Gst.PadProbeType.BUFFER, self._make_chain_input_counter("in", label))
            except Exception:
                # Could not be counted -> must not be listed (see docstring).
                pads.pop(key, None)
                counters.pop(key, None)
            # U30: armed after the counter, so ``probes[0]`` stays the counter.
            self._install_eos_observer(pad, self._make_chain_eos_label("in", label))

    def _make_chain_eos_label(self, rung: str, stream: str) -> Callable[[], str]:
        """The ``U30`` EOS-observer label for a rung/stream pair.

        Deliberately the SAME text the flow ladder uses (``"sel:video"``), so one
        rendered arrival reads against the counter clause of the same line. Rung
        ``out`` is not a ladder rung -- it labels the OUTGOING leg's own selector
        sink pads, armed per reload by ``reload_program``."""

        def _label() -> str:
            return f"{rung}:{stream}"

        return _label

    def _make_chain_input_counter(self, rung: str, stream: str) -> Any:
        """A per-rung BUFFER counter closing over ITS OWN rung and stream.

        A factory rather than a def-in-loop, for the same two reasons as
        ``_make_mux_input_counter``: the closure must key on its own rung, and
        ``__name__`` stays stable for logs and tests."""

        def _count_chain_input(_pad: Gst.Pad, _info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
            # Guarded end to end -- this runs on a streaming thread, and a
            # diagnostic must never be able to take a channel off air. OK, never
            # DROP: this probe observes, it does not police the data path.
            with contextlib.suppress(Exception):
                key = (rung, stream)
                self._chain_input_buffers[key] = self._chain_input_buffers.get(key, 0) + 1
            return Gst.PadProbeReturn.OK

        return _count_chain_input

    def _snapshot_chain_input(self) -> None:
        """Move the flow ladder's baseline to right now.

        Called at arm time and after every progress print, for the same reason as
        ``_snapshot_mux_input``: each line reports its own interval, and a
        cumulative total would hide a stream that stopped ten minutes ago behind
        everything it sent before that."""
        with contextlib.suppress(Exception):
            self._chain_input_snapshot = dict(self._chain_input_buffers)

    def _chain_input_delta_suffix(self, elapsed: float) -> str:
        """``" [chain-in <elapsed>s: sel video=+N audio=+M | queue video=+N audio=+M]"``.

        Rungs upstream to downstream, then video before audio, so the clause
        reads in data order and matches the mux clause's stream order. A rung or
        stream that was never counted is absent, not ``+0``. Returns ``""`` when
        nothing was counted, so a graph without these elements renders exactly the
        line it rendered before -- and is guarded throughout, because it runs
        inside the same progress/EOS path as the mux clause."""
        pads = getattr(self, "_chain_input_pads", None) or {}
        counters = getattr(self, "_chain_input_buffers", None) or {}
        snapshot = getattr(self, "_chain_input_snapshot", None) or {}
        if not pads or not counters:
            return ""
        try:
            rung_rank = {rung: index for index, rung in enumerate(self._CHAIN_INPUT_RUNGS)}
            stream_rank = {stream: index for index, stream in enumerate(self._CHAIN_INPUT_STREAMS)}
            ranked = sorted(
                pads,
                key=lambda key: (
                    rung_rank.get(key[0], len(rung_rank)),
                    stream_rank.get(key[1], len(stream_rank)),
                    key,
                ),
            )
            grouped: dict[str, list[str]] = {}
            for key in ranked:
                current = counters.get(key, 0)
                # A rung missing from the snapshot has no interval baseline yet
                # (registered after the last print): report ``+0`` rather than
                # invent a delta from a baseline we never took.
                previous = snapshot.get(key, current)
                grouped.setdefault(key[0], []).append(f"{key[1]}=+{current - previous}")
            rendered = " | ".join(f"{rung} {' '.join(items)}" for rung, items in grouped.items())
        except Exception:
            return ""
        return f" [chain-in {elapsed:.1f}s: {rendered}]"

    # -- U30 EOS-origin diagnostic: where the EOS that ends output arrived ------
    #
    # ``CTRL output: pipeline EOS from civiccast-playout`` names the end of the
    # channel's output but not its origin, and both off-live campaigns produce
    # that line in a shape no other line explains: the outgoing leg streamed its
    # whole 4.05s through ``sel``/``queue`` into the mux, the worker then quit
    # cleanly, and NOT ONE of the reload's own guards printed -- no
    # ``outgoing-EOS-dropped``, no ``firing``, no commit. Either the EOS crossed
    # a selector sink pad the boundary probe was not on, or it came into being
    # downstream of the probe entirely. Those are opposite fixes, and the log
    # could not tell them apart.
    #
    # One report-only observer on each pad that already carries a flow-ladder
    # counter answers it on the NEXT occurrence: the labels land in
    # ``_eos_arrivals`` in arrival order (which is data order), so
    # ``[eos-arrivals: out:video, sel:video, queue:video, mux-sink:video, mux-src]``
    # says the outgoing leg's own EOS ran all the way through, while
    # ``[eos-arrivals: none]`` is itself the finding: nothing in the observed
    # output half ever saw an EOS, so it was generated below every one of them.
    #
    # The first campaign to run with those labels back was NOT that clean, and
    # the reason was the labels, not the pipeline: ten of its eleven dead runs
    # read ``[eos-arrivals: sel:video, sel:audio, queue:video, mux-sink:video,
    # queue:audio, mux-sink:audio, mux-src]`` -- an EOS on the selector's own src
    # pad, with NO ``out:*`` arrival before it, and no boundary-probe guard line
    # either. An ``input-selector`` cannot put an EOS on its src pad unless one
    # arrived on a sink pad, so that EOS crossed a selector sink pad that carries
    # no label -- the one stretch the labelled set did not cover. Two additions
    # close it, both still one observer per pad and one int per reload:
    #
    # * ``sink-pad:<padname>:<stream>`` -- one observer on EVERY sink pad of both
    #   selectors, including the pads of the leg being switched in
    #   (``_install_selector_sink_eos_observers``, called by ``reload_program``).
    #   The next occurrence names the pad instead of leaving "somewhere".
    # * ``sel:<stream>[active=<padname>]`` -- the selector's ``active-pad`` read
    #   at EOS time (``_make_selector_src_eos_label``), which says whether the
    #   selector was still pointed at the leg that had been airing when it
    #   emitted that EOS.
    #
    # Report-only, always: never DROP, never REMOVE, never a line of its own --
    # the EOS it watches for is the one about to quit the worker anyway, so this
    # cannot change what the channel airs. The observers on the OUTGOING selector
    # sink pads are armed before the reload's boundary probes are (same-priority
    # pads probes run in installation order), so an arrival is recorded even when
    # the boundary probe installed after it returns DROP.
    #
    # ONLY ``Gst.EventType.EOS`` is recorded. An EVENT_DOWNSTREAM probe fires for
    # every downstream event on its pad -- stream-start, caps, segment, tag -- and
    # an earlier U30 build of this diagnostic recorded all of them: the campaign
    # line came back ``[eos-arrivals: sel:audio, queue:audio, ... +186 more]`` on
    # runs whose entire log was 4 s long, which is a per-buffer event stream, not
    # 198 ends of output. A label list that admits non-EOS events does not answer
    # "where did the EOS enter?" -- it answers "what happened on these pads", and
    # it answers it in a clause named ``eos-arrivals``. Filtering on the event type
    # is what makes a rendered label a claim about an EOS.

    _EOS_ARRIVAL_MAX: ClassVar[int] = 12

    def _record_eos_arrival(self, label: str) -> None:
        """Append one arrival label, keeping the rendered line bounded.

        Guarded end to end -- this runs on a streaming thread inside a probe. A
        label identical to the previous one is folded: two observers on one pad
        (a superseded reload re-arms the same outgoing pad) observe one EOS, and
        reporting it twice would read as two arrivals."""
        with contextlib.suppress(Exception):
            arrivals = getattr(self, "_eos_arrivals", None)
            if arrivals is None:
                self._eos_arrivals = arrivals = []
            if arrivals and arrivals[-1] == label:
                return
            if len(arrivals) < self._EOS_ARRIVAL_MAX:
                arrivals.append(label)
            else:
                self._eos_arrivals_overflow = getattr(self, "_eos_arrivals_overflow", 0) + 1

    def _make_eos_observer(self, label: Callable[[], str]) -> Any:
        """A report-only EOS observer closing over a LAZY label.

        Records an arrival for ``Gst.EventType.EOS`` and nothing else, so the
        rendered list is a list of ends of output rather than of every caps,
        segment and tag event that happened to cross the same pad.

        A factory rather than a def-in-loop for the same reason as
        ``_make_mux_input_counter``: ``__name__`` stays stable for logs and tests.
        Lazy rather than a plain string because a mux sink pad's stream label is
        only readable once caps are negotiated, and these observers are armed
        before PLAYING."""

        def _observe_eos(_pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
            # Guarded end to end -- streaming thread. OK, never DROP: this probe
            # observes the data path, it does not police it.
            with contextlib.suppress(Exception):
                event = info.get_event() if info is not None else None
                # An unreadable event is NOT an arrival: silence here stays
                # silence, and the line keeps saying ``none`` rather than
                # inventing an EOS this observer never saw.
                if event is not None and event.type == Gst.EventType.EOS:
                    self._record_eos_arrival(label())
            return Gst.PadProbeReturn.OK

        return _observe_eos

    def _make_mux_src_eos_label(self) -> Callable[[], str]:
        """The mux's own src pad -- the last pad inside the worker.

        A constant label, but a callable like every other one: an EOS arriving
        HERE means the mux itself emitted it, so nothing upstream of the mux
        produced the end and this pad is the boundary the diagnosis turns on.
        """

        def _label() -> str:
            return "mux-src"

        return _label

    def _make_mux_sink_eos_label(self, pad: Any) -> Callable[[], str]:
        """Lazy label for one mux sink pad: the stream that stopped feeding it.

        Lazy because these observers are armed before PLAYING, when the pad's
        caps are not negotiated yet and the stream label is not yet readable."""

        def _label() -> str:
            return f"mux-sink:{self._mux_pad_stream_label(pad)}"

        return _label

    def _make_selector_src_eos_label(self, selector: Any, stream: str) -> Callable[[], str]:
        """``"sel:video[active=sink_0]"`` -- the selector's own EOS, plus where
        the selector was pointed when it emitted it.

        ``sel:video`` alone says the selector's src pad emitted an EOS, not which
        input was feeding it when it did. The deferred deaths record that arrival
        while the pad the outgoing leg delivers on records nothing, and the
        reading turns on exactly this: the selector was still pointed at the
        outgoing leg (so the EOS came out of the leg that had been airing) or it
        had already been pointed elsewhere. One property read at EOS time buys
        that separation; no new probe, no per-buffer cost.

        Read lazily and guarded: a selector whose ``active-pad`` cannot be read
        renders the bare ``sel:video`` the line rendered before -- silence about
        the pad, never a guess at it."""

        def _label() -> str:
            with contextlib.suppress(Exception):
                active = selector.get_property("active-pad") if selector is not None else None
                if active is not None:
                    return f"sel:{stream}[active={active.get_name()}]"
            return f"sel:{stream}"

        return _label

    def _make_selector_sink_eos_label(self, pad: Any, stream: str) -> Callable[[], str]:
        """Lazy label for one selector SINK pad: the pad's own name.

        ``out:video``/``out:audio`` can only name the pad the outgoing leg is
        EXPECTED to deliver on. An ``input-selector`` puts an EOS on its src pad
        only after one reached a sink pad, so a ``sel:video`` arrival with no
        ``out:video`` arrival means the EOS crossed a sink pad that carries no
        label -- and this label names it, whatever it turns out to be
        (``sink-pad:sink_1:video``). Lazy because the name is read at EOS time,
        after a commit may already have replaced the leg on that pad."""

        def _label() -> str:
            with contextlib.suppress(Exception):
                return f"sink-pad:{pad.get_name()}:{stream}"
            return f"sink-pad:?:{stream}"

        return _label

    def _selector_sink_pad_pairs(self) -> list[tuple[str, Any]]:
        """``(stream, pad)`` for every sink pad of both program selectors.

        The engine's own link-order record of which legs feed which selector:
        index 0 is the leg that has been airing, the rest are the legs a reload
        left attached. Read at arm time, so a pad added to the selector by a
        LATER reload is not covered -- ``reload_program`` passes those in
        explicitly."""
        pairs: list[tuple[str, Any]] = []
        for stream, pads in (
            ("video", getattr(self, "selector_sink_pads", [])),
            ("audio", getattr(self, "audio_sink_pads", [])),
        ):
            for pad in list(pads):
                if pad is not None:
                    pairs.append((stream, pad))
        return pairs

    def _install_selector_sink_eos_observers(
        self,
        extra: Sequence[tuple[str, Any]] = (),
        skip: Sequence[Any] = (),
    ) -> None:
        """Name EVERY selector sink pad, so an EOS crossing one is named.

        ``out:video``/``out:audio`` are armed on the pads the outgoing leg is
        expected to deliver on; ten of the eleven deferred deaths of the
        2026-09-25 off-live campaign recorded an EOS at the selector's own src
        pad with no arrival on either of them, and the EOS must have reached a
        sink pad to get there. These observers close that hole with the pad's own
        name, and ``extra`` covers the pads of the leg being switched IN -- which
        is held at its first buffer and carries no observer at all until the
        commit arms the ``in`` rung, so an arrival from the NEW leg is otherwise
        unobservable no matter what it does.

        ``skip`` is the pads that already carry ``out:*``: one EOS must not
        render under two labels. Report-only and best-effort per pad, exactly
        like ``_install_eos_observer`` -- a pad that refuses a probe stays
        unobserved rather than being reported as silent."""
        for stream, pad in list(self._selector_sink_pad_pairs()) + list(extra):
            if any(pad is skipped for skipped in skip):
                continue
            self._install_eos_observer(pad, self._make_selector_sink_eos_label(pad, stream))

    def _install_eos_observer(self, pad: Any, label: Callable[[], str]) -> None:
        """Arm one observer if the pad can take it; silent and safe otherwise.

        Same registration contract as every flow-ladder rung: a pad that is
        missing or refuses a probe is left unobserved, never reported as having
        seen nothing, because ``[eos-arrivals: none]`` is a claim about the
        pipeline and must not be made by a pad we never watched."""
        if pad is None or not hasattr(pad, "add_probe"):
            return
        with contextlib.suppress(Exception):
            pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._make_eos_observer(label))

    def _eos_arrival_suffix(self) -> str:
        """``" [eos-arrivals: out:video, sel:video, mux-src]"`` -- or
        ``" [eos-arrivals: none]"`` when no observed pad saw one.

        Guarded: it runs inside the bus EOS path, and returns ``""`` (rendering
        the line exactly as it was before U30) if the arrival state cannot be
        read at all."""
        with contextlib.suppress(Exception):
            arrivals = getattr(self, "_eos_arrivals", None) or []
            if not arrivals:
                return " [eos-arrivals: none]"
            rendered = ", ".join(arrivals)
            overflow = getattr(self, "_eos_arrivals_overflow", 0)
            if overflow:
                rendered += f", +{overflow} more"
            return f" [eos-arrivals: {rendered}]"
        return ""

    def _arm_stall_watchdog(self) -> None:
        # Item 84: arm unconditionally as long as EITHER budget is active --
        # before this fix a ``stall_timeout_s <= 0`` (an operator disabling the
        # post-first-buffer stall check entirely) also silently disabled the
        # first-output bound, which is a genuinely different, always-useful
        # check (a pipeline that never produces a single buffer at all).
        if self.stall_timeout_s <= 0 and self.first_output_timeout_s <= 0:
            return
        self._stall_last_count = self._output_buffers
        self._stall_last_advance_t = time.monotonic()
        # Item 84c (measured in sandbox run 17): a buffer WILL already have
        # crossed the mux by arm time on essentially every worker -- the
        # persistent output half's async sink chain means the pipeline
        # cannot even reach PLAYING before at least one buffer (PAT/PMT/SDT
        # tables + preroll) has prerolled through it. Treating "buffers > 0
        # at arm" as first-output evidence (the pre-84c behavior) was
        # therefore a TAUTOLOGY, not a real signal -- see the module-level
        # comment above ``_FIRST_OUTPUT_MIN_BUFFERS_AFTER_ARM``. Snapshot the
        # count instead of latching evidence from it: real first output is
        # measured strictly AFTER this point, in ``_check_stall``.
        self._output_buffers_at_arm = self._output_buffers
        self._first_output_seen = False
        self._last_output_progress_print_t = self._stall_last_advance_t
        # U59 round 4: the rate diagnostic's first interval starts at arm time --
        # the same moment the ladder below takes its baseline -- and its episode
        # latch starts clean (a worker that just armed has no episode in flight).
        # The last-fire stamp is deliberately NOT reset: the bound-exceeded
        # WARN's 60s "delivery fell short" window must survive an arm.
        self._u59_rate_interval_t = self._stall_last_advance_t
        self._u59_rate_streak = 0
        self._u59_rate_in_episode = False
        # U30: the per-stream counters' baseline must start here too -- the
        # progress line's ``[mux-in ...]`` / ``[chain-in ...]`` deltas are
        # measured from arm time (and from each later print), never from a
        # cumulative total.
        self._snapshot_mux_input(self._stall_last_advance_t)
        self._snapshot_chain_input()
        # U34: the per-stream references start at the same moment and against
        # the same baseline as the aggregate one -- see ``_check_stream_stalls``.
        self._arm_stream_stall_reference(self._stall_last_advance_t)
        GLib.timeout_add_seconds(1, self._check_stall)

    def _maybe_print_first_output_marker(self) -> None:
        """Item 84 Round-2 review BLOCKER: print the pid-tagged
        ``CTRL first-output: first buffer after Ns pid=N`` marker exactly
        once, the moment the FIRST TS buffer/buffer-list is observed crossing
        the mux -- distinct from, and more meaningful than, the ``CTRL
        preroll: reached PLAYING`` marker.

        Measured escalation-cliff BLOCKER this closes: PLAYING (even
        NO_PREROLL) is not evidence output ever flowed, but
        ``EgressDaemon._observed_on_air_evidence`` used to credit the
        PLAYING marker alone as on-air evidence for the GStreamer strategy --
        a worker that reaches PLAYING on every single relaunch but never
        crosses ``first_output_timeout_s`` (at ANY configured value, 65s
        through the 120s clamp ceiling) got its crash-loop streak reset on
        every cycle by the ALIVE-poll path, and never escalated to fallback
        slate (streak pinned at 1) even though it never once produced real
        output. ``civiccast.egress.health.worker_produced_output`` greps for
        this exact marker, anchored to the same spawn offset and pid check as
        ``worker_reached_playing``, and the daemon's alive-poll evidence
        check now requires THIS marker (not PLAYING) for a GStreamer-strategy
        channel -- see ``EgressDaemon._observed_on_air_evidence``. No budget
        value can defeat escalation now: PLAYING alone is never sufficient."""
        if self._first_output_marker_printed:
            return
        self._first_output_marker_printed = True
        elapsed = (
            time.monotonic() - self._playing_reached_at
            if self._playing_reached_at is not None
            else 0.0
        )
        print(
            # ASCII only, stable prefix -- a parsed contract like the PLAYING
            # marker above, not just a human-readable log line (see
            # ``civiccast.egress.health.worker_produced_output``).
            f"CTRL first-output: first buffer after {elapsed:.1f}s pid={os.getpid()}",
            file=sys.stderr,
            flush=True,
        )

    def _maybe_print_output_progress(self, now: float) -> None:
        """Item 84c addendum: a bounded, at-most-one-line-per-5s progress
        breadcrumb -- the total output-buffer count and the delta observed
        since arm (PLAYING) time -- so the NEXT soak shows exactly when real
        output stops, instead of only the eventual stall-kill line 10s (or
        45s, before first output) later. Sandbox run 17 had no such signal:
        the only evidence of the item 88 stall was TSDuck's after-the-fact
        silence plus the watchdog kill."""
        if now - self._last_output_progress_print_t < _OUTPUT_PROGRESS_INTERVAL_S:
            return
        self._last_output_progress_print_t = now
        delta = self._output_buffers - self._output_buffers_at_arm
        # U30: the total above cannot say WHICH stream is advancing -- it kept
        # climbing at the audio-only rate through both 2026-09-25 freezes. The
        # suffix names each counted mux sink pad's delta over THIS interval, and
        # is "" (line byte-identical) whenever nothing was counted.
        mux_in = self._mux_input_delta_suffix(now - self._mux_input_snapshot_t)
        print(
            f"CTRL output: {self._output_buffers} buffers (+{delta}) since PLAYING{mux_in}",
            file=sys.stderr,
            flush=True,
        )
        # U59 round 4: judged BEFORE this poll's snapshot, so the deltas it reads
        # are this interval's -- the ladder's baseline is still the previous
        # print's (the same baseline the line above just rendered).
        self._u59_judge_leg_rate(now)
        # U59 round 4b: the same poll feeds the accumulated-delivery judge, and on
        # the same terms -- before the snapshot, so it reads this interval's
        # deltas. It is a second instrument, not a replacement: the rate floor
        # catches a leg that is COLLAPSING, this catches one that is quietly
        # running short (0.967x nominal on 2026-09-28 06:24) and accumulating the
        # seconds that become the changeover's hole.
        self._u59_judge_leg_lag(now)
        self._snapshot_mux_input(now)
        self._snapshot_chain_input()

    # -- U59 round 4: the changeover leg-rate diagnostic ------------------------
    #
    # Judged on rung ``sel`` -- the selector's src pad, i.e. the stream the
    # channel is actually AIRING -- which is armed for both streams for the whole
    # worker lifetime. Rung ``in`` is the wrong witness: it names the leg selected
    # NOW, which during a deferral is the INCOMING leg (held, so it reads ~0 on
    # both streams and the audio guard would swallow the fire), while the U59
    # evidence puts the collapse on the RETIRING leg's last ~21s -- exactly the
    # leg rung ``in`` stops naming once the switch commits.
    #
    # Disclosed limits. (a) It judges the leg on air at each poll, so a collapse
    # that never reaches ``sel`` is invisible. (b) It cannot see inside
    # ``decodebin``: a decodebin's internal queues are reachable only by iterating
    # a live bin, which a diagnostic must never do to a channel that is on air, so
    # the queue column can read ``queues=none`` on a leg whose stall is
    # decoder-internal -- an honest gap, not a measurement of zero. (c) Per-piece
    # position is read from the video concat's sink-pad PTS, which is the piece's
    # own media timeline: exact within a piece, and "the leg's running time" is
    # the sum of the completed pieces plus the position in the active one.

    # Class-level immutable defaults keep diagnostics safe for the narrow test
    # and fallback paths that construct this engine with object.__new__(). These
    # values are per-engine state once an instance is initialized.
    _u59_legs: tuple[dict[str, Any], ...] = ()
    _u59_qos: tuple[dict[str, Any], ...] = ()
    _u59_rate_streak: int = 0
    _u59_rate_in_episode: bool = False
    _u59_fired_t: float | None = None
    _u59_rate_interval_t: float = 0.0
    _u59_lag_samples: tuple[tuple[float, float], ...] = ()
    _u59_lag_total: float = 0.0
    _u59_lag_txn: Any = None
    _u59_lag_streak: int = 0
    _u59_lag_in_episode: bool = False
    _u59_lag_fired_t: float | None = None
    _u59_lag_interval_t: float = 0.0

    def _u59_init_diagnostics(self) -> None:
        """This worker's own diagnostic state.

        The class-level defaults are immutable tuples for the shapes that never
        run ``__init__`` (``object.__new__(GstPlayoutEngine)`` -- the U16
        real-runtime emitter and several tests): they read empty containers
        instead of mutating a class attribute shared with every other instance.
        """
        self._u59_legs = ()
        self._u59_qos = ()
        self._u59_rate_streak = 0
        self._u59_rate_in_episode = False
        self._u59_fired_t = None
        self._u59_rate_interval_t = 0.0
        self._u59_lag_samples = ()
        self._u59_lag_total = 0.0
        self._u59_lag_txn = None
        self._u59_lag_streak = 0
        self._u59_lag_in_episode = False
        self._u59_lag_fired_t = None
        self._u59_lag_interval_t = 0.0

    @staticmethod
    def _u59_enabled() -> bool:
        """The one kill switch: ``CIVICAST_GST_LEG_RATE_DIAG=0`` (or ``off`` /
        ``false`` / ``no``) silences the line. Unset and empty mean ON -- this
        round exists to catch an occurrence nothing else in the log explains, so
        it is armed by default and only an explicit refusal turns it off."""
        raw = os.environ.get(_U59_RATE_ENV_VAR)
        if raw is None or not raw.strip():
            return True
        return raw.strip().lower() not in ("0", "off", "false", "no")

    @staticmethod
    def _u59_factory_name(element: Any) -> str:
        """The element's factory name, or ``""`` -- never raises."""
        with contextlib.suppress(Exception):
            return str(element.get_factory().get_name())
        return ""

    @staticmethod
    def _u59_element_name(element: Any) -> str:
        with contextlib.suppress(Exception):
            return str(element.get_name() or "")
        return ""

    @staticmethod
    def _u59_pad_index(pad: Any) -> int:
        """A concat request pad's ``sink_<n>`` index, for ordering pieces.

        ``sinkpads`` is returned in an unspecified order; the request order is the
        build order, which is the playlist order, so sorting by the name's numeric
        suffix reconstructs it. A pad whose name carries no index sorts last."""
        name = ""
        with contextlib.suppress(Exception):
            name = str(pad.get_name() or "")
        match = re.search(r"_(\d+)$", name)
        return int(match.group(1)) if match else 1 << 30

    def _u59_record_qos(self, message: Any) -> None:
        """Remember one QOS message for the diagnostic's 30s window.

        Bounded and append-only on the GLib loop (``_on_bus`` is a bus-sync
        handler), read on the same loop, so no lock is needed or wanted."""
        with contextlib.suppress(Exception):
            entry: dict[str, Any] = {"t": time.monotonic(), "src": "unknown"}
            with contextlib.suppress(Exception):
                entry["src"] = str(message.src.get_name())
            with contextlib.suppress(Exception):
                structure = message.get_structure()
                if structure is not None:
                    for key in ("jitter", "proportion", "quality"):
                        if structure.has_field(key):
                            with contextlib.suppress(Exception):
                                entry[key] = float(structure.get_value(key))
                    for key in ("dropped", "processed"):
                        if structure.has_field(key):
                            with contextlib.suppress(Exception):
                                entry[key] = int(structure.get_value(key))
            history = [*self._u59_qos, entry]
            self._u59_qos = tuple(history[-_U59_QOS_HISTORY:])

    def _u59_record_leg(self, pending: dict[str, Any]) -> None:
        """Remember the leg just built, for the rate diagnostic.

        Called from ``_install_new_leg_inbound_counters`` -- the same moment rung
        ``in`` is re-armed, i.e. "this leg is the selected one now". Guarded end to
        end: this runs inside the reload path, and a diagnostic must never be the
        reason a changeover fails."""
        if not self._u59_enabled():
            return
        try:
            record = self._u59_leg_record(pending)
        except Exception:
            return
        if record is None:
            return
        legs = [leg for leg in self._u59_legs if leg.get("txn_id") != record["txn_id"]]
        legs.append(record)
        self._u59_legs = tuple(legs[-_U59_RATE_KEEP_LEGS:])

    def _u59_leg_record(self, pending: dict[str, Any]) -> dict[str, Any] | None:
        """The diagnostic's view of one leg, from the elements the build returned.

        ``pending['new_elements']`` is the build's collection list in ``_make``
        order, so a ``filesrc`` starts a new piece and the pieces come out in
        playlist order -- which is also the video concat's request-pad order and
        therefore the index a ``sink_<n>`` name carries. No new key is added to
        ``pending``: that dict is the reload's own contract, and a diagnostic has
        no business extending it."""
        elements = list(pending.get("new_elements") or ())
        if not elements:
            return None
        label = _DIAGNOSTIC_NONE
        concat: Any = None
        pieces: list[dict[str, Any]] = []
        queues: list[Any] = []
        decoders: list[Any] = []
        capsfilters: list[Any] = []
        for element in elements:
            factory = self._u59_factory_name(element)
            name = self._u59_element_name(element)
            if factory == "filesrc":
                path: Any = None
                with contextlib.suppress(Exception):
                    path = element.get_property("location")
                pieces.append(
                    {
                        "filesrc": element,
                        "path": path,
                        "count": 0,
                        "first_pts": None,
                        "last_pts": None,
                        "last_t": 0.0,
                    }
                )
            elif factory == "concat" and name.startswith("vconcat_") and concat is None:
                concat = element
                match = re.match(r"^vconcat_(.+)_\d+$", name)
                if match:
                    label = match.group(1)
            elif factory in ("queue", "multiqueue"):
                queues.append(element)
            elif factory in getattr(self, "_DECODERS", ()):
                decoders.append(element)
            elif factory == "capsfilter":
                capsfilters.append(element)
        concat_pads: list[Any] = []
        if concat is not None:
            with contextlib.suppress(Exception):
                concat_pads = sorted(
                    getattr(concat, "sinkpads", None) or [], key=self._u59_pad_index
                )
        for index, piece in enumerate(pieces):
            if index >= len(concat_pads):
                break
            pad = concat_pads[index]
            piece["pad"] = pad
            if not hasattr(pad, "add_probe"):
                continue
            with contextlib.suppress(Exception):
                pad.add_probe(Gst.PadProbeType.BUFFER, self._u59_make_piece_probe(piece))
        return {
            "txn_id": pending.get("txn_id"),
            "label": label,
            "video_pad": pending.get("new_video_pad"),
            "pieces": pieces,
            "queues": queues,
            "decoders": decoders,
            "capsfilters": capsfilters,
            "recorded_t": time.monotonic(),
        }

    @staticmethod
    def _u59_make_piece_probe(entry: dict[str, Any]) -> Callable[..., Any]:
        """An observe-only BUFFER probe on one piece's video concat sink pad.

        Two readings, no mutation: the pad's buffer count and its PTS window.
        ``concat`` does not rewrite PTS on its SINK pads, so ``last_pts -
        first_pts`` on a piece's pad is that piece's own media span -- which is
        what "position in its piece" means here. Single writer (that pad's
        streaming thread), read on the GLib loop: the same lock-free contract as
        every other counter in this module."""

        def _u59_piece_probe(_pad: Any, info: Any) -> Any:
            with contextlib.suppress(Exception):
                entry["count"] = int(entry.get("count") or 0) + 1
                entry["last_t"] = time.monotonic()
                get_buffer = getattr(info, "get_buffer", None)
                buffer = get_buffer() if callable(get_buffer) else None
                if buffer is not None:
                    pts = int(getattr(buffer, "pts", -1))
                    # CLOCK_TIME_NONE (and a negative) is "no timestamp", not a
                    # position: recording it would render a leg 584 years long.
                    if 0 <= pts < (1 << 62):
                        if entry.get("first_pts") is None:
                            entry["first_pts"] = pts
                        entry["last_pts"] = pts
            return Gst.PadProbeReturn.OK

        return _u59_piece_probe

    def _u59_airing_leg(self) -> dict[str, Any] | None:
        """Which recorded leg is on air, by pad IDENTITY first.

        The selector's ``active-pad`` compared against each record's
        ``new_video_pad`` is exact -- the same identity comparison
        ``_confirm_reload_selector_handoff`` reads back. Only when that read is
        unavailable (no selector, or a pad we never recorded) does it fall back to
        the leg with the most recent per-piece activity, then to the newest
        record: during a deferral the incoming leg is HELD and silent, so activity
        picks the retiring leg that is still airing. A per-piece TIE (no leg has
        strictly newer activity) deliberately keeps the FIRST active leg -- the
        older, retiring one -- which is the same answer that read would give."""
        legs = self._u59_legs
        if not legs:
            return None
        active: Any = None
        with contextlib.suppress(Exception):
            selector = getattr(self, "selector", None)
            if selector is not None:
                active = selector.get_property("active-pad")
        if active is not None:
            for leg in reversed(legs):
                if leg.get("video_pad") is active:
                    return leg
        best: dict[str, Any] | None = None
        best_t = 0.0
        for leg in legs:
            for piece in leg.get("pieces") or ():
                last_t = float(piece.get("last_t") or 0.0)
                if last_t > best_t:
                    best_t = last_t
                    best = leg
        return best if best is not None else legs[-1]

    @staticmethod
    def _u59_piece_span(piece: dict[str, Any] | None) -> float | None:
        """A piece's media span in seconds, or ``None`` when it never reported a
        timestamp. ``None`` is not ``0.0``: an un-timestamped piece is unknown,
        and rendering it as zero would claim the leg had not advanced."""
        if not piece:
            return None
        first = piece.get("first_pts")
        last = piece.get("last_pts")
        if first is None or last is None:
            return None
        return float(int(last) - int(first)) / float(Gst.SECOND)

    @staticmethod
    def _u59_caps_text(element: Any) -> str:
        with contextlib.suppress(Exception):
            pad = element.get_static_pad("src")
            if pad is not None:
                caps = pad.get_current_caps()
                if caps is not None:
                    return str(caps.to_string())
        return ""

    def _u59_nominal_rates(self, leg: dict[str, Any] | None) -> tuple[float, float]:
        """The leg's OWN nominal rates, from its capsfilters' negotiated caps.

        The floor is only meaningful against the rate this leg was actually built
        for, so the framerate and sample rate are read rather than assumed. The
        module constants stand in when the caps are not readable -- the comparison
        must never run against an invented zero."""
        video = _U59_RATE_NOMINAL_VIDEO_PER_S
        audio = _U59_RATE_NOMINAL_AUDIO_PER_S
        if not leg:
            return video, audio
        for element in leg.get("capsfilters") or ():
            text = self._u59_caps_text(element)
            if text.startswith("video/"):
                match = re.search(r"framerate=\(fraction\)(\d+)/(\d+)", text)
                if match and int(match.group(2)):
                    video = int(match.group(1)) / int(match.group(2))
            elif text.startswith("audio/"):
                match = re.search(r"rate=\(int\)(\d+)", text)
                if match and int(match.group(1)):
                    audio = int(match.group(1)) / 1024.0
        return video, audio

    def _u59_rate_rung(self) -> str | None:
        """The rung both streams are read from -- see the block comment for why
        ``sel`` leads. Both streams must be present: a video reading compared
        against an absent audio one is not a comparison."""
        pads = getattr(self, "_chain_input_pads", None) or {}
        for rung in _U59_RATE_RUNG_PREFERENCE:
            if all((rung, stream) in pads for stream in self._CHAIN_INPUT_STREAMS):
                return rung
        return None

    def _u59_rates(self, rung: str, interval: float) -> tuple[float, float] | None:
        """``(video, audio)`` buffers per second over the poll's interval.

        The baseline is ``_chain_input_snapshot`` -- the value the last progress
        print left behind, since the judge runs BEFORE this poll's snapshot. A
        rung/stream registered after that snapshot has no baseline yet and is
        treated exactly as ``_chain_input_delta_suffix`` treats it (delta 0 from
        the current count), so the first interval after a switch cannot report a
        delta we never measured."""
        pads = getattr(self, "_chain_input_pads", None) or {}
        counters = getattr(self, "_chain_input_buffers", None) or {}
        snapshot = getattr(self, "_chain_input_snapshot", None) or {}
        if interval <= 0.0:
            return None
        deltas: list[float] = []
        for stream in self._CHAIN_INPUT_STREAMS:
            key = (rung, stream)
            if key not in pads or key not in counters:
                return None
            current = counters[key]
            deltas.append((current - snapshot.get(key, current)) / interval)
        return deltas[0], deltas[1]

    def _u59_judge_leg_rate(self, now: float) -> None:
        """Fire the diagnostic's one WARN line for a video-starved leg.

        Runs on the CTRL output poll, before that poll's snapshot, so the delta it
        reads is this interval's. An episode is the run of consecutive polls with
        video below the floor WHILE audio stays above its own: the audio guard is
        what separates this shape from the box-wide contention dips that take both
        streams down together (measured ~0.92 nominal each on 2026-09-27), which
        are not this defect and must not be logged as one. One line per episode,
        and at most one line per ``_U59_RATE_WARN_INTERVAL_S``; an episode
        suppressed by the interval is not latched, so it fires as soon as the
        interval allows while it is still running."""
        try:
            interval = now - float(self._u59_rate_interval_t or 0.0)
            self._u59_rate_interval_t = now
            if not self._u59_enabled():
                return
            if interval <= 0.0:
                return
            rung = self._u59_rate_rung()
            if rung is None:
                return
            rates = self._u59_rates(rung, interval)
            if rates is None:
                return
            video_rate, audio_rate = rates
            leg = self._u59_airing_leg()
            video_nominal, audio_nominal = self._u59_nominal_rates(leg)
            starving = video_rate < _U59_RATE_VIDEO_FLOOR * video_nominal
            audio_healthy = audio_rate >= _U59_RATE_AUDIO_FLOOR * audio_nominal
            if not (starving and audio_healthy):
                self._u59_rate_streak = 0
                self._u59_rate_in_episode = False
                return
            self._u59_rate_streak = int(self._u59_rate_streak or 0) + 1
            if self._u59_rate_in_episode:
                return
            if self._u59_rate_streak < _U59_RATE_STREAK_POLLS:
                return
            fired_t = self._u59_fired_t
            if fired_t is not None and now - float(fired_t) < _U59_RATE_WARN_INTERVAL_S:
                return
            self._u59_rate_in_episode = True
            self._u59_fired_t = now
            print(
                self._u59_warn_line(
                    now=now,
                    leg=leg,
                    rung=rung,
                    video_rate=video_rate,
                    audio_rate=audio_rate,
                    video_nominal=video_nominal,
                    audio_nominal=audio_nominal,
                    interval=interval,
                ),
                file=sys.stderr,
                flush=True,
            )
        except Exception:
            # A diagnostic is never allowed to take a channel off air.
            return

    def _u59_warn_line(
        self,
        *,
        now: float,
        leg: dict[str, Any] | None,
        rung: str,
        video_rate: float,
        audio_rate: float,
        video_nominal: float,
        audio_nominal: float,
        interval: float,
    ) -> str:
        """The one line an episode prints: which leg, how far into it, which piece
        of its playlist and how far into that, its source path, the queue levels
        and decoders on its branch, and any QOS from the last 30s."""
        txn_id = (leg or {}).get("txn_id")
        return (
            f"WARN: leg chain-in video below floor "
            f"reload_id={_DIAGNOSTIC_NONE if txn_id is None else txn_id} "
            f"rung={rung} "
            f"video={video_rate:.1f}/s ({video_rate / video_nominal:.2f}x nominal {video_nominal:.1f}) "
            f"audio={audio_rate:.1f}/s ({audio_rate / audio_nominal:.2f}x nominal {audio_nominal:.1f}) "
            f"window={interval:.1f}s polls={_U59_RATE_STREAK_POLLS} "
            f"{self._u59_leg_suffix(now, leg)}"
        )

    def _u59_leg_suffix(self, now: float, leg: dict[str, Any] | None) -> str:
        """The per-leg half of a U59 WARN line, shared by both instruments.

        Factored out in round 4b so the delivery-lag line names the same leg with
        the same fields as the rate line -- one description of the leg, two
        measurements of it. The text is byte-identical to what the rate line
        built inline before this round."""
        pieces = list((leg or {}).get("pieces") or [])
        active_index: int | None = None
        best_t = 0.0
        for index, piece in enumerate(pieces):
            last_t = float(piece.get("last_t") or 0.0)
            # ``last_t`` is a monotonic stamp, so a piece that never streamed has
            # none and must not be selected -- its position is unknown, which is
            # exactly what ``pos=none`` says. Two pieces CAN carry the same stamp
            # (the clock is coarser than a 30fps probe), and on that tie the LATER
            # piece wins: pieces stream in playlist order, so the later index is
            # the one still streaming when the stamps agree.
            if last_t <= 0.0:
                continue
            if last_t >= best_t:
                best_t = last_t
                active_index = index
        if active_index is None:
            piece_field = f"piece={_DIAGNOSTIC_NONE}"
            pos_field = f"pos={_DIAGNOSTIC_NONE}"
            leg_rt_field = f"leg_rt={_DIAGNOSTIC_NONE}"
            path_field = _DIAGNOSTIC_NONE
        else:
            active = pieces[active_index]
            span = self._u59_piece_span(active)
            completed = 0.0
            for piece in pieces[:active_index]:
                piece_span = self._u59_piece_span(piece)
                if piece_span is not None:
                    completed += piece_span
            piece_field = f"piece={active_index + 1}/{len(pieces)}"
            pos_field = f"pos={_DIAGNOSTIC_NONE}" if span is None else f"pos={span:.3f}s"
            leg_rt_field = (
                f"leg_rt={_DIAGNOSTIC_NONE}"
                if span is None and completed <= 0.0
                else f"leg_rt={completed + (span or 0.0):.3f}s"
            )
            path_field = str(active.get("path") or _DIAGNOSTIC_NONE)
        return (
            f"leg={(leg or {}).get('label') or _DIAGNOSTIC_NONE} "
            f"{piece_field} {pos_field} {leg_rt_field} path={path_field} "
            f"queues={self._u59_queue_field(leg)} "
            f"decoders={self._u59_decoder_field(leg)} {self._u59_qos_field(now)}"
        )

    def _u59_judge_leg_lag(self, now: float) -> None:
        """Round 4b: fire one WARN line when the on-air leg stops DELIVERING video
        as fast as it delivers audio, measured as accumulated lag.

        The quantity is ``audio_seconds - video_seconds`` delivered by this leg,
        each counted in its own negotiated nominal, and the test is its GROWTH
        across a fully covered ``_U59_LAG_WINDOW_S`` window -- not its absolute
        value, because an absolute bound would accumulate a small nominal
        mismatch over a 2000s leg. The window's own baseline is a sample taken at
        least a window ago, so the growth over it is drift-immune.

        Why a second instrument: the round-4 rate floor must stay quiet on
        healthy legs, and a shortfall mild enough to stay above a quiet floor
        (0.967x nominal, live 2026-09-28 06:24) still accumulates seconds -- it
        became a 2.000s picture hole. This measures the accumulation itself, and
        the number it prints is the number the switch then measures as the
        ``video_end``/``audio_end`` spread.

        Disclosed limits. (a) It is armed only once the window is fully covered,
        i.e. after the first ``_U59_LAG_WINDOW_S`` of a leg, because a growth over
        a partly covered window is not the bound it is compared against. (b) The
        accumulator resets when the leg on air changes identity, so a deficit is
        never inherited across a changeover. (c) Like the rate judge it watches
        rung ``sel``, so a collapse that never reaches the selector is invisible
        to it. (d) It is one-sided: a leg CATCHING UP (a post-switch burst, video
        above nominal) grows the lag negatively and is not this defect.
        """
        try:
            interval = now - float(self._u59_lag_interval_t or 0.0)
            self._u59_lag_interval_t = now
            if not self._u59_enabled():
                return
            if interval <= 0.0:
                return
            rung = self._u59_rate_rung()
            if rung is None:
                return
            rates = self._u59_rates(rung, interval)
            if rates is None:
                return
            video_rate, audio_rate = rates
            leg = self._u59_airing_leg()
            video_nominal, audio_nominal = self._u59_nominal_rates(leg)
            self._u59_accumulate_leg_lag(
                now, leg, video_rate, audio_rate, video_nominal, audio_nominal, interval
            )
            growth = self._u59_lag_growth(now)
            if growth is None or growth < _U59_LAG_BOUND_S:
                self._u59_lag_streak = 0
                self._u59_lag_in_episode = False
                return
            self._u59_lag_streak = int(self._u59_lag_streak or 0) + 1
            if self._u59_lag_in_episode:
                return
            if self._u59_lag_streak < _U59_LAG_STREAK_POLLS:
                return
            fired_t = self._u59_lag_fired_t
            if fired_t is not None and now - float(fired_t) < _U59_RATE_WARN_INTERVAL_S:
                return
            self._u59_lag_in_episode = True
            self._u59_lag_fired_t = now
            print(
                self._u59_lag_line(
                    now=now,
                    leg=leg,
                    rung=rung,
                    growth=growth,
                    video_rate=video_rate,
                    audio_rate=audio_rate,
                    interval=interval,
                ),
                file=sys.stderr,
                flush=True,
            )
        except Exception:
            # A diagnostic is never allowed to take a channel off air.
            return

    def _u59_accumulate_leg_lag(
        self,
        now: float,
        leg: dict[str, Any] | None,
        video_rate: float,
        audio_rate: float,
        video_nominal: float,
        audio_nominal: float,
        interval: float,
    ) -> None:
        """Add this poll's delivery to the on-air leg's lag, resetting on a change
        of leg. The sample list is pruned by time and capped, so the memory cost
        is a constant however long a leg runs."""
        txn_id = (leg or {}).get("txn_id")
        if not self._u59_lag_samples or txn_id != self._u59_lag_txn:
            self._u59_lag_samples = ()
            self._u59_lag_total = 0.0
            self._u59_lag_txn = txn_id
            self._u59_lag_streak = 0
            self._u59_lag_in_episode = False
        video_seconds = video_rate * interval / video_nominal
        audio_seconds = audio_rate * interval / audio_nominal
        self._u59_lag_total = float(self._u59_lag_total or 0.0) + (audio_seconds - video_seconds)
        samples = [*self._u59_lag_samples, (now, self._u59_lag_total)]
        self._u59_lag_samples = tuple(self._u59_prune_lag_samples(samples, now))

    @staticmethod
    def _u59_prune_lag_samples(
        samples: list[tuple[float, float]], now: float
    ) -> list[tuple[float, float]]:
        """Keep every sample inside the window, plus the newest one BEFORE it.

        That older sample is the window's baseline: without it the growth is
        uncomputable, so it is dropped last -- the count cap trims the oldest
        IN-window sample, never the baseline. Pruning by time rather than by
        count is what keeps a full window available whatever the poll cadence."""
        cutoff = now - _U59_LAG_WINDOW_S
        inside = [sample for sample in samples if sample[0] > cutoff]
        before = [sample for sample in samples if sample[0] <= cutoff]
        return ([before[-1]] if before else []) + inside[-(_U59_LAG_SAMPLES - 1) :]

    def _u59_lag_growth(self, now: float) -> float | None:
        """The lag's growth across the window ending now, or ``None`` while the
        window is not yet fully covered -- an uncovered window has no baseline and
        must not be judged as if it were zero."""
        cutoff = now - _U59_LAG_WINDOW_S
        baseline: float | None = None
        for sample_t, sample_total in self._u59_lag_samples:
            if sample_t > cutoff:
                break
            baseline = sample_total
        if baseline is None:
            return None
        return float(self._u59_lag_total or 0.0) - baseline

    def _u59_lag_line(
        self,
        *,
        now: float,
        leg: dict[str, Any] | None,
        rung: str,
        growth: float,
        video_rate: float,
        audio_rate: float,
        interval: float,
    ) -> str:
        """The lag episode's one line. It names the accumulated deficit in
        seconds and the window it grew across, which is the quantity the
        changeover will measure as its ``video_end``/``audio_end`` spread -- then
        the same leg description the rate line carries."""
        txn_id = (leg or {}).get("txn_id")
        return (
            f"WARN: leg delivery lag "
            f"reload_id={_DIAGNOSTIC_NONE if txn_id is None else txn_id} "
            f"rung={rung} "
            f"lag=+{growth:.3f}s over {_U59_LAG_WINDOW_S:.1f}s "
            f"(bound {_U59_LAG_BOUND_S:.1f}s, polls={_U59_LAG_STREAK_POLLS}) "
            f"video={video_rate:.1f}/s audio={audio_rate:.1f}/s window={interval:.1f}s "
            f"{self._u59_leg_suffix(now, leg)}"
        )

    def _u59_leg_delivery_clause(self, moment: float) -> str:
        """The bound-exceeded line's delivery clause, naming whichever instrument
        measured the shortfall that became this spread.

        Prefers the LAG witness: it measures the deficit in seconds -- the same
        quantity the spread is -- whereas the rate witness only reports that the
        instantaneous rate was low at some point. Round 4 could only see the rate
        witness; live 2026-09-28 06:24 is the case where the lag witness fired and
        the rate one did not."""
        lag_t = getattr(self, "_u59_lag_fired_t", None)
        if lag_t is not None and 0.0 <= moment - float(lag_t) <= _U59_RATE_RECENT_FIRE_S:
            return (
                "video delivery fell short at the end of the leg "
                f"(leg delivery lag fired {moment - float(lag_t):.1f}s ago)"
            )
        rate_t = getattr(self, "_u59_fired_t", None)
        if rate_t is not None and 0.0 <= moment - float(rate_t) <= _U59_RATE_RECENT_FIRE_S:
            return (
                "video delivery fell short at the end of the leg "
                f"(leg chain-in video below floor {moment - float(rate_t):.1f}s ago)"
            )
        return ""

    @staticmethod
    def _u59_queue_field(leg: dict[str, Any] | None) -> str:
        """``current-level-buffers``/``current-level-time`` for every queue-like
        element in the leg. ``none`` when the leg has no top-level queue -- true of
        a playlist leg, whose sub-chains carry no queue at all -- and that is an
        honest "not addressable here", never a measured zero (see the block
        comment's limit (b))."""
        rendered: list[str] = []
        for element in (leg or {}).get("queues") or ():
            name = _DIAGNOSTIC_NONE
            with contextlib.suppress(Exception):
                name = str(element.get_name() or _DIAGNOSTIC_NONE)
            buffers = _DIAGNOSTIC_NONE
            with contextlib.suppress(Exception):
                current = element.get_property("current-level-buffers")
                if current is not None:
                    buffers = str(int(current))
            seconds = _DIAGNOSTIC_NONE
            with contextlib.suppress(Exception):
                current_time = element.get_property("current-level-time")
                if current_time is not None:
                    seconds = f"{int(current_time) / Gst.SECOND:.3f}s"
            rendered.append(f"{name}={buffers}buf/{seconds}")
        return ",".join(rendered) if rendered else _DIAGNOSTIC_NONE

    @staticmethod
    def _u59_decoder_field(leg: dict[str, Any] | None) -> str:
        names: list[str] = []
        for element in (leg or {}).get("decoders") or ():
            with contextlib.suppress(Exception):
                name = str(element.get_name() or "")
                if name:
                    names.append(name)
        return ",".join(names) if names else _DIAGNOSTIC_NONE

    def _u59_qos_field(self, now: float) -> str:
        """QOS messages seen in the last ``_U59_QOS_WINDOW_S``, newest three
        rendered. ``qos=0 in 30s`` when the window is empty, so the line always
        carries the answer rather than omitting the question."""
        cutoff = now - _U59_QOS_WINDOW_S
        recent = [entry for entry in self._u59_qos if float(entry.get("t") or 0.0) >= cutoff]
        if not recent:
            return f"qos=0 in {int(_U59_QOS_WINDOW_S)}s"
        rendered: list[str] = []
        for entry in recent[-3:]:
            fields = [str(entry.get("src") or "unknown")]
            for key, label in (
                ("jitter", "jit"),
                ("proportion", "prop"),
                ("quality", "qual"),
                ("dropped", "drop"),
                ("processed", "proc"),
            ):
                if key in entry:
                    value = entry[key]
                    fields.append(
                        f"{label}={value:.4f}" if isinstance(value, float) else f"{label}={value}"
                    )
            rendered.append(",".join(fields))
        return f"qos={len(recent)} in {int(_U59_QOS_WINDOW_S)}s [{'; '.join(rendered)}]"

    def _bound_exceeded_warn_line(
        self,
        pending: dict[str, Any],
        measured_ends: Sequence[tuple[str, int | None]],
        spread_ns: int,
        now: float | None = None,
    ) -> str:
        """U59 round 4 item 2: the bound-exceeded line states what was MEASURED.

        The pre-round-4 text ended "... keeping the longer-leg end (no trim) -- a
        truly broken asset must stay visible", which asserts a cause -- an asset
        whose streams merely differ in length. Live 2026-09-27 showed the other
        cause: an asset that is not broken at all, whose video DELIVERY fell short
        at the end of the leg. The line therefore names the two measured ends, and
        says delivery fell short when either U59 instrument fired inside the last
        ``_U59_RATE_RECENT_FIRE_S``. The WARN prefix and the first clause are
        unchanged, so an existing grep for
        ``reload switch-at-shorter-leg bound exceeded`` still matches.

        Round 4b: the clause is now chosen by ``_u59_leg_delivery_clause``, which
        prefers the LAG witness (it measures the deficit in seconds -- the same
        quantity as the spread) and falls back to the round-4 rate witness. When
        neither fired the line is byte-identical to round 4's."""
        ends = dict(measured_ends)
        parts = [
            "WARN: reload switch-at-shorter-leg bound exceeded",
            f"reload_id={pending.get('txn_id')}",
            f"spread={spread_ns / Gst.SECOND:.3f}s",
            f"> {_SWITCH_SHORTER_LEG_MAX_TRIM_S:.3f}s;",
            "keeping the longer-leg end (no trim)",
            f"video_end={_seconds_or_none(ends.get('video'))}",
            f"audio_end={_seconds_or_none(ends.get('audio'))}",
        ]
        moment = time.monotonic() if now is None else now
        clause = self._u59_leg_delivery_clause(moment)
        if clause:
            parts.append(clause)
        return " ".join(parts)

    def _check_stall(self) -> bool:
        """Quit the run loop on either of two DISTINCT budgets, measured from
        PLAYING (see ``_arm_stall_watchdog``, called immediately after
        ``_await_playing``):

        * Item 84: while no output buffer has crossed the mux yet, bound the
          wait against ``first_output_timeout_s`` (45s default, much more
          generous -- see the module-level comment above
          ``_DEFAULT_FIRST_OUTPUT_TIMEOUT_S``). PLAYING (even NO_PREROLL) is
          NOT evidence a buffer actually crossed the mux, so this is a
          separate, later piece of evidence than ``_await_playing`` ever had.
        * Once the first REAL buffer IS observed, fall back to the original
          S9-5 behavior unchanged: bound against ``stall_timeout_s`` (10s
          default) from the last observed advance, for a pipeline that aired
          fine and then silently stopped.

        Item 84c: "the first buffer IS observed" is no longer "any buffer
        past the arm-time snapshot" -- see ``_FIRST_OUTPUT_MIN_BUFFERS_AFTER_
        ARM`` and ``_arm_stall_watchdog`` for why a raw ``> 0`` check there
        was a tautology (the async output sink chain always prerolls at
        least one buffer before PLAYING is even reached). Real first output
        now requires the count to exceed the arm-time snapshot by at least
        ``_FIRST_OUTPUT_MIN_BUFFERS_AFTER_ARM`` buffers, all observed AFTER
        arming.

        A no-op while output is flowing either way (it resets the timer on
        every advance). The worker exits non-zero on either budget and the
        daemon restarts it to a known state."""
        now = time.monotonic()
        if self._output_buffers != self._stall_last_count:
            self._stall_last_count = self._output_buffers
            self._stall_last_advance_t = now
            if (
                not self._first_output_seen
                and self._output_buffers - self._output_buffers_at_arm
                >= _FIRST_OUTPUT_MIN_BUFFERS_AFTER_ARM
            ):
                self._first_output_seen = True
                self._maybe_print_first_output_marker()
            self._maybe_print_output_progress(now)
            # U34: the aggregate advanced -- but it advanced on SOME stream.
            # In both shapes of a real freeze one branch keeps feeding the mux
            # (audio), so this is the only branch where the aggregate counter
            # is blind by construction; a per-stream judgement belongs here and
            # nowhere else. When the aggregate itself is flat the code below
            # keeps precedence unchanged.
            # False here means a per-stream stall was judged and the loop was
            # quit -- the watchdog is done, exactly like the paths below.
            return self._check_stream_stalls(now)
        self._maybe_print_output_progress(now)
        elapsed = now - self._stall_last_advance_t
        if not self._first_output_seen:
            if self.first_output_timeout_s <= 0 or elapsed < self.first_output_timeout_s:
                return True
            # STDERR, not stdout (Gate A T4 visibility fix): the daemon reads the
            # worker's stderr tail into ``last_error`` when the child exits non-zero,
            # so the reason a channel bounced is on the operator's state row instead
            # of only in an uncollected stdout log.
            print(
                # ASCII only -- same reasoning as the stall message below (the
                # daemon folds this into the state row's last_error, written
                # to Postgres via a client_encoding that may not be UTF-8).
                f"CTRL first-output: no output within {int(self.first_output_timeout_s)}s "
                "of PLAYING - quitting for daemon restart",
                file=sys.stderr,
                flush=True,
            )
            # Distinct reason from ("stall", ...) below -- worker.py maps this
            # to its own exit code (GST_FIRST_OUTPUT_TIMEOUT_EXIT_CODE) so the
            # daemon's relaunch path can rate-limit it the same way it already
            # does GST_PREROLL_TIMEOUT_EXIT_CODE, instead of counting it as an
            # ordinary crash toward the fallback-slate streak.
            self._error = ("first-output-timeout", "no output buffers observed within bound")
            if self._loop is not None:
                self._loop.quit()
            return False  # one-shot: stop the watchdog
        if self.stall_timeout_s <= 0:
            # Post-first-buffer stall check disabled (operator opt-out) --
            # unchanged from the pre-item-84 semantics of ``stall_timeout_s <=
            # 0`` skipping the watchdog entirely, now scoped to just this
            # budget since the first-output budget above may still be active.
            return True
        if elapsed >= self.stall_timeout_s:
            pending = self._pending_reload
            # A fully prerolled replacement is held safely off the selector,
            # but a broken outgoing leg can stop producing without ever
            # delivering EOS. Once output has flatlined for the ordinary
            # stall budget, force this transaction through the existing
            # forced-boundary path instead of killing the worker and losing
            # the ready replacement. The helper marks this transaction's
            # boundary satisfied and reuses the normal lock-safe
            # commit/retirement path. U34: the per-stream path asks the same
            # question of the same preconditions, so both call this one
            # predicate rather than restating it.
            if self._force_ready_deferred_switch(
                reason="output stalled after replacement preroll; forcing switch"
            ):
                return True
            if pending is not None and pending.get("commit_in_progress", False):
                # Round-2 finding 2: the two watchdogs were racing and the WRONG
                # one always won. A commit holds the replacement leg held (not
                # producing) while the old leg is retired, and the isolation
                # queues downstream drain in well under a second, so output
                # legitimately stops advancing for the length of the retirement.
                # With stall_timeout_s=10 and commit_timeout_s=15 the stall
                # watchdog therefore always fired FIRST -- killing the worker five
                # seconds before the commit watchdog could dump the faulthandler
                # stacks that exist precisely to localise a wedged retirement. In
                # production that dump was consequently never emitted.
                #
                # While a commit is in flight the commit watchdog owns the window:
                # it has the tighter diagnosis and the same fail-safe exit. Push
                # the stall reference forward so the check is genuinely suspended
                # rather than merely skipped once. ``_reset_stall_reference`` is
                # then called when the commit finishes, so the post-commit channel
                # is measured against a FULL fresh budget instead of the backlog
                # accumulated while suspended. Outside a commit this watchdog is
                # unchanged.
                self._stall_last_advance_t = now
                if not pending.get("stall_suspended_logged", False):
                    pending["stall_suspended_logged"] = True
                    print(
                        "CTRL stall: suspended while a reload commit is in progress; "
                        f"the {int(self.commit_timeout_s)}s commit watchdog owns this window",
                        file=sys.stderr,
                        flush=True,
                    )
                return True
            # STDERR, not stdout (Gate A T4 visibility fix): the daemon reads the
            # worker's stderr tail into ``last_error`` when the child exits non-zero,
            # so the reason a channel bounced is on the operator's state row instead
            # of only in an uncollected stdout log.
            print(
                # ASCII only: the daemon folds this line into the state row's
                # last_error, which is written to Postgres. A non-ASCII byte here
                # is re-read as U+FFFD (_child_stderr_tail reads errors="replace")
                # and a non-UTF8 client_encoding then fails the whole state write
                # -- see _child_stderr_tail's sanitiser and the T6 soak evidence
                # (soak-120-e502074-20260905).
                f"CTRL stall: no output for {int(self.stall_timeout_s)}s - quitting for daemon restart",
                file=sys.stderr,
                flush=True,
            )
            self._error = ("stall", "output stalled")  # → worker exits non-zero → restart
            if self._loop is not None:
                self._loop.quit()
            return False  # one-shot: stop the watchdog
        return True

    def _reset_stall_reference(self) -> None:
        """Round-2 finding 2: start the post-first-output stall budget over.

        Called when a reload commit settles. ``_check_stall`` suspends itself for
        the duration of a commit (the commit watchdog owns that window), and this
        is what makes the hand-back clean: the channel that comes out of a commit
        gets a full ``stall_timeout_s`` to resume output, rather than inheriting
        however much of the budget had already elapsed when the commit began.

        U30: the flow ladder's interval restarts here too. A committed reload is
        exactly the moment the question "is the SWITCHED-IN leg feeding the mux
        yet?" starts to have an answer, and without this baseline the first
        progress/EOS line after a commit would report a delta that straddles the
        commit and the healthy outgoing flow before it -- which is how the
        2026-09-25 freezes read as ``video=+121`` while the mux was receiving
        nothing but the retiring tail. Only the diagnostic interval moves; the
        stall watchdog reads ``_output_buffers`` and is unaffected."""
        now = time.monotonic()
        self._stall_last_advance_t = now
        self._snapshot_mux_input(now)
        self._snapshot_chain_input()
        # U34: the per-stream references restart here too, for the same reason
        # and with the same clean hand-back: the leg that was switched IN has
        # only just begun to have a chance to feed the mux, and it must get the
        # full per-stream budget rather than inherit the commit's silence.
        self._arm_stream_stall_reference(now)

    # -- U34 per-stream stall judgement ---------------------------------------

    def _required_stream_labels(self) -> set[str]:
        """The stream labels whose silence means the channel stopped airing.

        ``video`` always; ``audio`` only when the persistent output half
        actually carries an audio branch (``self.audio_selector`` is built iff
        the graph declares an audio encoder -- see the pipeline build), because
        a graph with no audio branch has no audio mux pad to judge and an
        audio-less channel must not be judged silent for having no audio.

        Caption/subtitle pads are deliberately excluded from this set: a cue
        track is legitimately sparse (minutes can pass between cues), so its
        silence is not an outage. They stay in the logged flow ladder
        (``_mux_input_delta_suffix``) where an operator can see them."""
        labels = {"video"}
        if getattr(self, "audio_selector", None) is not None:
            labels.add("audio")
        return labels

    def _arm_stream_stall_reference(self, now: float) -> None:
        """Baseline every counted mux SINK pad's stall reference at ``now``.

        Called at arm time and from ``_reset_stall_reference`` (commit
        settlement), exactly where the aggregate reference is re-baselined, so
        the two watchdogs always agree about when the current window started.

        U37: the reference is the pad's AIRING running time
        (``_mux_pad_airing_rt``), not its arrival count, so what this watchdog
        measures is whether the mux is still EMITTING this stream -- see the
        attribute's own comment for why arrivals cannot answer that.

        Pads whose frontier is already set at this moment are credited as having
        produced -- ``_stream_ever_seen`` is additive and never cleared, so a
        stream that fed the mux before a commit is still a stream whose later
        silence is an outage."""
        with contextlib.suppress(Exception):
            pads = getattr(self, "_mux_input_pads", None) or {}
            airing = getattr(self, "_mux_pad_airing_rt", None) or {}
            ever = getattr(self, "_stream_ever_seen", None)
            if ever is None:
                ever = set()
                self._stream_ever_seen = ever
            self._stream_last_rt = dict(airing)
            self._stream_last_advance_t = dict.fromkeys(pads, now)
            # U37: the lag clock starts over with the advance clock -- a new
            # window must not inherit a lag measured before it began.
            self._stream_lag_since = {}
            for pad_name in pads:
                if airing.get(pad_name) is not None:
                    ever.add(pad_name)

    def _check_stream_stalls(self, now: float) -> bool:
        """U34: judge each REQUIRED stream's own mux SINK pad.

        Called only from ``_check_stall``'s output-ADVANCING branch, because
        that is the branch where the aggregate counter is blind by
        construction: the four 2026-09-25 freezes each kept the mux SRC counter
        climbing at the audio-only rate (244 buffers/5s against 390 healthy)
        while the video branch fed the mux nothing at all. A required stream
        (``video``, plus ``audio`` when the graph has an audio branch) flat for
        ``stall_timeout_s`` is judged stalled even though the aggregate moved.

        U37: "flat" is the stream's AIRING running time -- the running time of
        the last buffer the mux CONSUMED off that pad (``buffer-consumed``) --
        and no longer its arrival count. The two differ exactly in the shape that
        motivated this slice: a stream keeps arriving at its normal rate while
        the mux emits none of it (U37 measured a 9.4 s audio-only run with the
        video pad's head pinned ~9.4 s into the future and ``video`` still
        arriving), and an arrival count reads that as healthy.

        Two predicates, one action (U37). A required stream is judged stalled if
        its airing running time does not advance with wall clock for
        ``stall_timeout_s`` (the flat rule, which owns the attribution), OR if it
        keeps advancing but sits more than ``_STREAM_LAG_BOUND_S`` behind the
        leading required stream's airing running time for that same budget (the
        lag rule -- a stream the other has already passed cannot air in step).
        The flat rule is consulted first and the two are never combined: a frozen
        stream's lag grows without bound, so a lag-only judge would name the audio
        it left behind rather than the video that froze. Both take
        ``_act_on_stream_stall``, so a lagged stream gets U34's rescue-or-exit
        route, not a new one.

        Returns True to keep watching, False when this watchdog has stopped it
        (the loop was quit), mirroring ``_check_stall``.

        Deliberately NOT judged:

        * a pad outside ``_required_stream_labels`` (caption/subtitle -- sparse
          by nature -- or an unnegotiated pad with no honest label);
        * a pad with no airing frontier at all -- a stream this mux has never
          emitted, which is the first-output budget's question, not this one;
        * any stream while a reload commit is in flight -- the commit watchdog
          owns that window (same rationale as the aggregate suspension), and
          the reference is pushed forward silently so the per-stream check
          cannot print inside a window the healthy-run test asserts is free of
          ``CTRL stall`` lines.

        Known bounded limitation (U37): the aggregator emits ``buffer-consumed``
        for a buffer its clip vfunc DROPPED too, and that one carries the leg's
        UNCLIPPED pts, which is not this mux pad's running time. Such a value can
        move the frontier spuriously and delay (never cause) a judgement by at
        most one ``stall_timeout_s``. A BACKWARDS step is therefore not judged:
        with that value arriving in the same channel, a raw leg-domain pts after
        a rebase is indistinguishable from a real pts regression, and a watchdog
        that restarts a healthy channel whenever it switches is worse than one
        that waits a budget longer.

        Lock-free on purpose: the frontier is written by one streaming thread per
        pad and read here on the GLib main loop, the same single-writer contract
        as ``_output_buffers``. Nothing is re-baselined outside this method
        except at arm/reset time from the same main loop."""
        if self.stall_timeout_s <= 0:
            # Same operator opt-out as the aggregate check: ``stall_timeout_s
            # <= 0`` means "no post-first-buffer stall bound at all".
            return True
        pads = getattr(self, "_mux_input_pads", None) or {}
        airing = getattr(self, "_mux_pad_airing_rt", None) or {}
        if not pads:
            return True
        last_rt = getattr(self, "_stream_last_rt", None)
        if last_rt is None:
            last_rt = {}
            self._stream_last_rt = last_rt
        last_advance = getattr(self, "_stream_last_advance_t", None)
        if last_advance is None:
            last_advance = {}
            self._stream_last_advance_t = last_advance
        ever = getattr(self, "_stream_ever_seen", None)
        if ever is None:
            ever = set()
            self._stream_ever_seen = ever
        lag_since = getattr(self, "_stream_lag_since", None)
        if lag_since is None:
            lag_since = {}
            self._stream_lag_since = lag_since
        required = self._required_stream_labels()
        pending = getattr(self, "_pending_reload", None)
        suspended = pending is not None and pending.get("commit_in_progress", False)
        judged: list[tuple[str, float]] = []
        for pad_name in sorted(pads):
            current = airing.get(pad_name)
            if current is None:
                # The mux has emitted nothing off this pad yet: a stream that has
                # not started, which is the first-output budget's question.
                continue
            previous = last_rt.get(pad_name)
            if previous is None or current != previous:
                # The mux emitted a different buffer off this pad since the
                # previous tick (or emitted its first): this stream is airing.
                last_rt[pad_name] = current
                last_advance[pad_name] = now
                ever.add(pad_name)
                continue
            if pad_name not in ever or self._mux_pad_stream_label(pads[pad_name]) not in required:
                continue
            elapsed = now - last_advance.get(pad_name, now)
            if elapsed < self.stall_timeout_s:
                continue
            if suspended:
                last_advance[pad_name] = now
                continue
            judged.append((pad_name, elapsed))
        # U37: the leading AIRING running time among the streams this judge may
        # name, and the pad carrying it. The lag half measures every other
        # required stream against this. It is the max of the same frontiers the
        # flat half reads, so the two halves can never disagree about which
        # stream is ahead.
        leader_pad: str | None = None
        leader_rt = 0
        for candidate in sorted(pads):
            candidate_rt = airing.get(candidate)
            if candidate_rt is None:
                continue
            if self._mux_pad_stream_label(pads[candidate]) not in required:
                continue
            if leader_pad is None or candidate_rt > leader_rt:
                leader_pad = candidate
                leader_rt = candidate_rt
        for candidate in sorted(pads):
            candidate_rt = airing.get(candidate)
            if candidate_rt is None or leader_pad is None:
                continue
            if self._mux_pad_stream_label(pads[candidate]) not in required:
                continue
            behind_s = (leader_rt - candidate_rt) / 1_000_000_000.0
            if behind_s > _STREAM_LAG_BOUND_S:
                lag_since.setdefault(candidate, now)
            else:
                # Back within the bound: whatever this pad was behind, it is not
                # behind it any more, and a lag has to be continuous to be judged.
                lag_since.pop(candidate, None)
        if suspended:
            # A commit in flight owns its window (the flat rule above pushes the
            # advance clock forward inside it). Drop the lag clocks with it rather
            # than let one grow across a switch whose own boundary is expected to
            # move the streams apart for a moment.
            lag_since.clear()
            return True
        if judged:
            # Judge the stream silent longest: one line, one exit, and the most
            # informative of the offenders.
            pad_name, elapsed = max(judged, key=lambda item: item[1])
            label = self._mux_pad_stream_label(pads[pad_name])
            return self._act_on_stream_stall(
                label=label,
                force_reason=f"{label} stream stalled after replacement preroll; forcing switch",
                headline=f"no {label} buffers for {int(elapsed)}s",
                error_reason=f"{label} stream stalled",
            )
        # Nothing has stopped airing -- but a stream can still be running seconds
        # behind the others, which is the other half of the running-time judge and
        # the `or` the slice asked for. Reached only when the flat half is empty,
        # deliberately: a frozen stream's lag against the leader also grows without
        # bound, so a lag-only judge would name the stream the frozen one left
        # behind instead of the frozen one. The flat half is the tighter bound and
        # the correct attribution, and it goes first.
        lagged = [
            (candidate, now - lag_since[candidate])
            for candidate in sorted(lag_since)
            if now - lag_since[candidate] >= self.stall_timeout_s
        ]
        if not lagged or leader_pad is None:
            return True
        pad_name, elapsed = max(lagged, key=lambda item: item[1])
        label = self._mux_pad_stream_label(pads[pad_name])
        leader_label = self._mux_pad_stream_label(pads[leader_pad])
        behind_s = (leader_rt - airing.get(pad_name, leader_rt)) / 1_000_000_000.0
        return self._act_on_stream_stall(
            label=label,
            force_reason=(
                f"{label} stream running {behind_s:.1f}s behind {leader_label} after "
                "replacement preroll; forcing switch"
            ),
            headline=(
                f"{label} running time {behind_s:.1f}s behind {leader_label} for {int(elapsed)}s"
            ),
            error_reason=f"{label} stream lagging {leader_label}",
        )

    def _act_on_stream_stall(
        self, *, label: str, force_reason: str, headline: str, error_reason: str
    ) -> bool:
        """The ACTION half of the per-stream judge -- U34's action, unchanged.

        Factored out (U37) so the running-time judge's two predicates -- a stream
        that stopped airing and a stream that is airing seconds behind the others
        -- take exactly the same route off air: force a ready replacement through
        if one is held, else exit non-zero naming the stream. Returns True to keep
        watching; False when this watchdog stopped the loop."""
        # Same rescue as the aggregate path, same preconditions: a fully
        # prerolled replacement held off the selector can take over a wedged
        # channel without any restart at all.
        if self._force_ready_deferred_switch(reason=force_reason):
            return True
        ctx = getattr(self, "_reload_context", None)
        ctx_text = (
            f"last reload id={ctx[0]} stage={ctx[1]}" if ctx is not None else "no content reload"
        )
        print(
            # STDERR, not stdout, and ASCII only: the daemon reads the worker's
            # stderr tail into ``last_error`` for the operator's state row (see
            # the aggregate stall message). Deliberately does NOT reuse the
            # aggregate wording "no output for" -- the aggregate watchdog did
            # NOT judge this stall, and a log reader (or a test) must be able to
            # tell which watchdog named it.
            f"CTRL stall: {headline} ({ctx_text}) - quitting for daemon restart",
            file=sys.stderr,
            flush=True,
        )
        # ("stall", ...) -- the ordinary exit-1 case the daemon already
        # relaunches, with a reason that names the stream that stopped.
        self._error = ("stall", error_reason)
        if self._loop is not None:
            self._loop.quit()
        return False

    def _force_ready_deferred_switch(self, *, reason: str) -> bool:
        """Force a ready-but-waiting deferred switch through the existing
        forced-boundary path; True when the force was accepted.

        A fully prerolled replacement is held safely off the selector, but a
        broken outgoing leg can stop producing without ever delivering the EOS
        or boundary this switch is waiting for -- the live reload-7 case. Both
        stall paths (the aggregate one and U34's per-stream one) reach their
        bound in exactly that state and need exactly this rescue, so the
        predicate lives here once instead of being restated (and drifting)."""
        pending = getattr(self, "_pending_reload", None)
        if (
            pending is not None
            and pending.get("switch_at_end_of_current", False)
            and pending.get("new_leg_ready", False)
            and not pending.get("old_leg_eos", False)
            and not pending.get("commit_in_progress", False)
        ):
            return bool(self._force_deferred_boundary(pending["txn_id"], reason=reason))
        return False

    def _await_playing(self) -> None:
        """Bounded wait for the PLAYING transition so a wedged preroll can't hang the
        run loop before the time-bounded teardown could ever run (audit M1).

        Item 82 (sandbox run 13 evidence): a fresh worker under CPU load took
        longer than the old hard-coded 5.0s bound (``teardown_timeout_s``, which
        was never meant to double as a preroll bound) to reach PLAYING, and the
        daemon treated the resulting crash as an ordinary one — a relaunch storm
        against a source that was never actually broken. This now:

        * waits up to ``self.preroll_timeout_s`` (30s default, configurable —
          see ``_resolve_preroll_timeout_s``), polling in
          ``_PREROLL_POLL_INTERVAL_S``-second slices rather than blocking on one
          ``get_state`` call for the whole bound, so a slow-but-progressing
          preroll is VISIBLE on stderr instead of silent until it either
          finishes or the bound is hit;
        * raises the distinct ``PrerollTimeoutError`` (not a bare
          ``RuntimeError``) only once the bound is actually exceeded, so
          ``worker.py`` can exit with a distinct code and the daemon's relaunch
          path can tell a slow start apart from a genuine crash.
        """
        started = time.monotonic()
        deadline = started + self.preroll_timeout_s
        while True:
            remaining = deadline - time.monotonic()
            slice_s = (
                remaining if remaining < _PREROLL_POLL_INTERVAL_S else _PREROLL_POLL_INTERVAL_S
            )
            result, current, pending = self.pipeline.get_state(int(max(slice_s, 0.0) * Gst.SECOND))
            if result in (Gst.StateChangeReturn.SUCCESS, Gst.StateChangeReturn.NO_PREROLL):
                # Round-2 review BLOCKER (Opus, PR #183, item 1): this line is the
                # ONLY genuine evidence that the pipeline actually reached PLAYING
                # (real output, not merely "the process hasn't exited yet"). The
                # daemon's alive-poll path (``EgressDaemon._poll_process``) used to
                # reset the crash-loop streak on wall-clock seconds since SPAWN,
                # which also counts interpreter start + ``import gi``/``Gst.init``
                # + graph build + this very preroll wait -- none of which is air.
                # Under load that non-air overhead alone can exceed the daemon's
                # 60s healthy-uptime reset threshold, so a worker that has NEVER
                # once reached PLAYING could still get its crash streak reset
                # every cycle (measured: streak stuck at 1, never escalates to
                # fallback slate in 40 cycles). The daemon now greps this exact
                # marker out of the worker's stderr log
                # (``civiccast.egress.health.worker_reached_playing``) and only
                # starts the 60s healthy-uptime clock from the moment it is
                # first observed -- see ``EgressDaemon._poll_process``. ASCII
                # only + a stable prefix: this text is a parsed contract, not
                # just a log line -- changing it silently breaks the daemon's
                # match.
                # Round-4 (PR #183 review, BLOCKER reproduced): the marker
                # now also carries THIS worker's own pid. The daemon's
                # per-channel stderr log is opened in APPEND mode and never
                # truncated per spawn (see ``_default_worker_launcher`` /
                # ``start_ffmpeg``), so a fixed marker string alone let a
                # PREVIOUS worker's own "reached PLAYING" line satisfy a
                # brand-new worker's on-air-evidence check the moment it was
                # spawned. The daemon's primary defense is now anchoring the
                # scan to bytes at or after ITS OWN spawn point
                # (``EgressDaemon._stderr_spawn_offset``); this pid is a
                # second, independent check
                # (``civiccast.egress.health.worker_reached_playing``'s
                # ``expected_pid``) that holds even if the byte offset is
                # ever wrong.
                print(
                    f"CTRL preroll: reached PLAYING after {time.monotonic() - started:.1f}s "
                    f"pid={os.getpid()}",
                    file=sys.stderr,
                    flush=True,
                )
                # Item 84 Round-2 review BLOCKER: the reference point the
                # NEW ``CTRL first-output: ...`` marker's ``Ns`` measures
                # from -- see ``_maybe_print_first_output_marker``. PLAYING
                # itself is deliberately NOT treated as on-air evidence
                # anywhere downstream; this timestamp only feeds that
                # marker's elapsed-time text.
                self._playing_reached_at = time.monotonic()
                return
            if result == Gst.StateChangeReturn.FAILURE:
                # Not a slow preroll -- the pipeline itself failed. Distinct from
                # the timeout case: this is a real construction/link problem, not
                # something a slow-start retry would ever recover from.
                raise RuntimeError(
                    f"pipeline failed while waiting for PLAYING (get_state={result.value_nick})"
                )
            now = time.monotonic()
            if now >= deadline:
                raise PrerollTimeoutError(
                    f"pipeline did not reach PLAYING within {self.preroll_timeout_s}s "
                    f"(get_state={result.value_nick})"
                )
            # Round-2 review, item 3: log the CURRENT state too, not just the
            # get_state result and the pending state -- ``current`` is what
            # actually tells an operator whether the pipeline is stuck in
            # NULL/READY (never really started) vs PAUSED (genuinely
            # prerolling, just slowly) while it waits.
            print(
                f"CTRL preroll: still waiting for PLAYING after "
                f"{self.preroll_timeout_s - (deadline - now):.1f}s of "
                f"{self.preroll_timeout_s:.1f}s (get_state={result.value_nick}, "
                f"current={current.value_nick}, pending={pending.value_nick})",
                file=sys.stderr,
                flush=True,
            )

    def run(self, *, swaps: int, interval_s: int) -> dict[str, Any]:
        """Start PLAYING, swap the active source ``swaps`` times every ``interval_s``
        seconds, then stop. Returns a result dict; never blocks indefinitely."""
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)
        loop = GLib.MainLoop()  # before PLAYING so a startup bus ERROR isn't swallowed
        self._loop = loop
        self._prime_live_caption_stream()
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("pipeline failed to reach PLAYING")
        self._await_playing()
        self._arm_live_caption_gap_heartbeat()
        self._flush_lang_tags()  # push deferred secondary-audio ISO-639 descriptors
        state = {"n": 0, "cur": 0}
        nsrc = len(self.selector_sink_pads)

        def _tick() -> bool:
            if state["n"] >= swaps:
                loop.quit()
                return False
            state["cur"] = (state["cur"] + 1) % nsrc
            self.swap.swap_to(state["cur"])
            state["n"] += 1
            return True

        GLib.timeout_add_seconds(interval_s, _tick)
        GLib.timeout_add_seconds(interval_s * (swaps + 4), lambda: (loop.quit(), False)[1])
        loop.run()
        clean = self.stop()
        return {"swaps": state["n"], "error": self._error, "teardown_clean": clean}

    def run_forever(
        self, *, control_fifo: str | None = None, hold_slate_at_plan_eos: bool = False
    ) -> dict[str, Any]:
        """Run the channel until EOS, a pipeline error, SIGINT/SIGTERM, or a control
        ``stop``. Production mode for the per-channel worker. If ``control_fifo`` is
        given, newline commands (``swap <index>``, ``reload <graph.json>``, ``stop``)
        drive seamless role swaps and program content-reloads (D-S1-6: change the
        active source in place, never a restart). SIGTERM — what the daemon's
        ``terminate()`` sends — also quits and tears down gracefully (time-bounded
        ``→NULL`` with force-exit so the worker can never hang).

        ``hold_slate_at_plan_eos`` (U41) is the one part of that contract that is
        NOT universal, so it is a parameter and defaults OFF. Set it only for a
        PERSISTENT live channel -- the daemon's own launches mark those with
        ``reload_policy.WORKER_PERSISTENT_ENV=1`` in the child's environment and
        ``worker.main()`` forwards it here. The engine then holds the always-hot
        slate for the current programme's EOS (see ``_on_program_pad_plan_eos``)
        instead of letting EOS end the run. A FINITE run -- smoke ``SWAPS`` mode,
        a harness, the beta.5 baseline pin, whose only shutdown IS the plan ending
        -- must keep the old behaviour, so anything unmarked passes False."""
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)
        self._install_output_counter()  # S9-5: count TS buffers past the mux
        # U16: name what the OUTPUT side first saw, per stream. Armed here rather
        # than per-reload because the start of a worker's timeline is the other
        # end of the same question the reload diagnostic asks.
        self._install_mux_input_diagnostics()
        # U30: the always-on per-sink-pad counters behind the progress line's
        # ``[mux-in ...]`` suffix -- armed here (the mux is part of the
        # persistent output half, so its sink pads outlive every reload) so the
        # next freeze says WHICH stream stopped, in the same line the live
        # incidents already had and misread as healthy.
        self._install_mux_input_counters()
        # U30: the second half of that ladder -- counters at the two ends of the
        # ``selector -> isolation queue`` stretch the mux counters cannot see, so
        # the next occurrence of the 2026-09-25 stop says not just THAT the
        # switched-in leg's data stopped, but WHERE. Same persistent-output-half
        # lifetime, same one-int-per-buffer cost, no extra line.
        self._install_chain_input_counters()
        loop = GLib.MainLoop()  # before PLAYING so a startup bus ERROR isn't swallowed
        self._loop = loop
        self._prime_live_caption_stream()
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("pipeline failed to reach PLAYING")
        self._await_playing()
        self._arm_live_caption_gap_heartbeat()
        self._flush_lang_tags()  # push deferred secondary-audio ISO-639 descriptors
        self._arm_stall_watchdog()  # S9-5: quit (→ daemon restart) on a silent output stall
        # U41: the plan ending is not the channel ending. A worker that reaches the
        # end of its programme with no replacement ready holds the always-hot slate
        # instead of quitting, so the daemon never has to relaunch it (2026-09-25
        # education channel: worker exit_code=0 at 23:54:41 with state=ON_AIR, ~3
        # minutes dark, while the next source was still being prepared). Armed here
        # -- the production entry point -- and ONLY for a run the daemon marked
        # persistent, so a finite run keeps its EOS-ends-the-run contract (the
        # beta.5 baseline pin depends on it; ``run()`` never arms it either).
        self._hold_slate_at_plan_eos = hold_slate_at_plan_eos
        self._plan_eos_held = False
        if hold_slate_at_plan_eos:
            self._arm_plan_eos_hold_probes()

        keepalive_fd = self._watch_control_fifo(control_fifo) if control_fifo else None

        install_unix_signal_handlers(
            GLib,
            signal_numbers=(signal.SIGINT, signal.SIGTERM),
            quit_loop=loop.quit,
        )
        loop.run()
        if keepalive_fd is not None:
            with contextlib.suppress(OSError):
                os.close(keepalive_fd)
        clean = self.stop(force_exit_on_hang=True)
        return {"error": self._error, "teardown_clean": clean}

    def _watch_control_fifo(self, path: str) -> int:
        """Watch a control FIFO for swap/stop commands. Returns a keepalive write fd
        (held open so the read end never EOFs when external writers come and go)."""
        open_flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        read_fd = os.open(path, open_flags)
        keepalive_fd = os.open(path, os.O_WRONLY | getattr(os, "O_NONBLOCK", 0))
        channel = GLib.IOChannel.unix_new(read_fd)
        channel.set_encoding(None)
        channel.set_buffered(False)

        def _on_ctrl(_channel: GLib.IOChannel, condition: GLib.IOCondition) -> bool:
            if condition & GLib.IOCondition.IN:
                try:
                    data = os.read(read_fd, 4096)
                except BlockingIOError:
                    return True
                for line in data.decode("utf-8", "replace").splitlines():
                    self._dispatch_control(line)
            return True  # keep watching

        GLib.io_add_watch(
            channel,
            GLib.PRIORITY_DEFAULT,
            GLib.IOCondition.IN | GLib.IOCondition.HUP,
            _on_ctrl,
        )
        return keepalive_fd

    @staticmethod
    def _write_reload_status(channel_dir: Path, *, reload_id: str, result: str) -> None:
        """POSIX FIFO counterpart of ``worker.py``'s ``_write_reload_status``
        (the Windows D2 pipe dispatch's identical helper) -- writes the SAME
        ``<channel_dir>/reload-status.json`` file ``EgressDaemon._poll_reload_
        settlement`` polls, so a reload's eventual settle outcome is reported
        the same way regardless of which control-channel transport dispatched
        it. Atomic write (tmp + replace); best-effort (a write hiccup must
        never crash the channel -- the daemon's own deadline is the backstop
        if a status update never arrives at all)."""
        status_path = channel_dir / "reload-status.json"
        tmp_path = status_path.with_name(status_path.name + ".tmp")
        payload = json.dumps({"id": reload_id, "result": result, "ts": time.time()})
        try:
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(status_path)
        except OSError as exc:
            print(f"WARN: failed to write reload-status.json: {exc!r}", flush=True)

    def _dispatch_control(self, line: str) -> None:
        command = parse_control_line(line)
        if command is None:
            return
        if command[0] == "swap":
            try:
                self.swap.swap_to(command[1])
                print(f"CTRL swap {command[1]} applied", flush=True)
            except Exception as exc:
                print(f"CTRL swap {command[1]} failed: {exc!r}", flush=True)
        elif command[0] == "reload":
            try:
                reload_path = Path(command[1])
                channel_dir = reload_path.parent
                reload_id = reload_id_from_sidecar_path(command[1])
                with reload_path.open(encoding="utf-8") as handle:
                    new_graph = graph_from_json(handle.read())
                with contextlib.suppress(OSError):
                    reload_path.unlink()  # one-shot graph file: consume it after read

                # Hostile-review follow-up (2026-09-06): the POSIX FIFO path used
                # to call reload_program with no ``on_settled`` at all, so a
                # reload dispatched here NEVER reported its eventual commit/abort
                # -- the daemon's ``_poll_reload_settlement`` would wait out the
                # full 960s deadline and fall back to restart even for a reload
                # that landed perfectly. Write the SAME ``reload-status.json``
                # file the Windows D2 pipe seam's worker.py writes (using the
                # reload id embedded in this sidecar's own filename -- the FIFO
                # has no separate envelope/ack id field to carry the daemon's
                # own id, see ``reload_policy.reload_id_from_sidecar_path``),
                # so the daemon can observe settlement on this platform too.
                def _on_settled(
                    committed: bool,
                    reason: str | None,
                    _channel_dir: Path = channel_dir,
                    _reload_id: str = reload_id,
                ) -> None:
                    result = "applied" if committed else f"aborted:{reason or 'unknown'}"
                    self._write_reload_status(_channel_dir, reload_id=_reload_id, result=result)

                self.reload_program(
                    new_graph.sources[0],
                    switch_at_end_of_current=reload_switch_is_deferred(command[1]),
                    on_settled=_on_settled,
                )
                # BLOCKER fix: a content-reload must also re-apply the graphics-overlay
                # leg (station bug / lower-third) from the SAME reloaded graph — reload
                # used to rebuild only the program leg and silently drop
                # new_graph.graphics_overlay (see reload_graphics_overlay's docstring).
                self.reload_graphics_overlay(new_graph.graphics_overlay)
                print(f"CTRL reload armed ({command[1]})", flush=True)
            except Exception as exc:  # a bad reload must not kill the channel
                print(f"CTRL reload failed: {exc!r}", flush=True)
        elif command[0] == "emergency":
            try:
                self.set_emergency_overlay(json.loads(base64.b64decode(command[1]).decode("utf-8")))
            except Exception as exc:
                print(f"CTRL emergency failed: {exc!r}", flush=True)
        elif command[0] == "caption":
            # ("caption", pts_ms, dur_ms, b64text) — push one cue into the live appsrc.
            try:
                text = base64.b64decode(command[3]).decode("utf-8", "replace")
                pushed = self.push_caption_cue(
                    text=text,
                    pts_seconds=command[1] / 1000.0,
                    duration_seconds=command[2] / 1000.0,
                    **({"program_token": command[4]} if len(command) == 5 else {}),
                )
                if not pushed:
                    print("CTRL caption dropped: no live caption source", flush=True)
            except Exception as exc:  # a bad caption must never kill the channel
                print(f"CTRL caption failed: {exc!r}", flush=True)
        elif command[0] == "stop":
            print("CTRL stop", flush=True)
            if self._loop is not None:
                self._loop.quit()

    # -- content-reload (D-S1-6): rebuild the program leg while output stays PLAYING --

    @staticmethod
    def _notify_reload_settled(
        callback: Callable[[bool, str | None], None] | None,
        committed: bool,
        reason: str | None,
    ) -> None:
        """Invoke one reload settlement callback without letting receipt I/O fail playout."""
        if callback is not None:
            with contextlib.suppress(Exception):
                callback(committed, reason)

    def reload_program(
        self,
        new_leg: SourceLeg | PlaylistLeg,
        *,
        switch_at_end_of_current: bool = False,
        on_settled: Callable[[bool, str | None], None] | None = None,
    ) -> None:
        """Replace the program leg (source index 0) with ``new_leg`` seamlessly.

        Builds the new leg on the live PLAYING pipeline and prerolls it. The switch
        point depends on ``switch_at_end_of_current`` (B3 fix, kit 4b30c99 soak
        evidence):

        * ``False`` — an IMMEDIATE switch. A finite/segment-timed replacement is
          still held after both audio and video preroll and rebased onto the running
          output timeline before it is selected. A clock-timed live replacement is
          observed without blocking and switches once all of its streams produce a
          first buffer. This is used when a due programme must interrupt a
          FALLBACK_SLATE and for an operator-initiated live takeover / forced slate.
        * ``True`` — a BOUNDARY-ALIGNED switch. The new leg is built and prerolled
          exactly far enough to prove it decodes (its first buffer), then HELD
          there by a blocking pad probe on its own tail src pad; the switch happens
          at the outgoing leg's own end, so the currently-airing item plays out to
          its natural end with no re-decode and no jump. Used for automation's
          seamless plan-rollover reload
          (``ChannelAutomationService._check_plan_rollover``), which is triggered
          well before the live plan's projected end specifically so the new leg has
          time to be ready and WAIT rather than cut in early and truncate the
          still-airing item.

          Three mechanisms make that switch seamless; all three are load-bearing
          and all three were verified against the bundled GStreamer 1.28 runtime
          (``tests/egress/test_gst_engine_wsl.py::
          test_deferred_rollover_switches_at_the_boundary_without_eos``):

          1. **The outgoing leg's EOS is DROPPED, not observed.** ``input-selector``
             forwards EOS from its ACTIVE sink pad straight downstream, so an
             observe-only probe (``PAD_PROBE_REMOVE``/``PASS``) lets EOS reach the
             encoder → ``mpegtsmux`` → the bus, and the run loop quits *before* any
             ``GLib.idle_add`` commit can run — the channel goes STOPPED at every
             single boundary, deterministically. ``_on_outgoing_pad_data`` returns
             ``Gst.PadProbeReturn.DROP`` instead, so the selector never sees the
             EOS at all (it is therefore also never latched as that pad's EOS
             state) and no pipeline-level EOS is ever produced.
          2. **BOTH outgoing pads are probed.** The audio selector's active sink
             pad EOSes independently of video's; an unhandled audio EOS latches the
             mux's audio pad and every post-switch audio buffer is refused. Video
             and audio each get the same drop-probe, and the commit fires on
             whichever end arrives first.
          3. **A finite new leg's running time is rebased onto the old leg's end.** A
             freshly built leg starts at running time ~0 while the pipeline is
             hours in; without a rebase the output timeline jumps backwards at the
             switch (PCR/PTS discontinuity). ``_commit_reload`` reads the exact end
             running time of the outgoing pad's LAST buffer (``pts + duration`` in
             that pad's own segment — clock-independent, so it is right whether or
             not the sinks pace the pipeline) and applies it with
             ``Gst.Pad.set_offset`` on the new leg's tail src pad. That marks the
             leg's sticky ``SEGMENT`` for re-send, so when the hold probe is
             released the selector receives (and forwards) a segment whose ``base``
             is the old leg's end and output running time continues monotonically
             across the boundary. Setting the offset on the SELECTOR's sink pad
             instead does NOT work — the segment has already crossed that pad by
             then and the stored copy is not rewritten.

          The same finite-leg hold/rebase transaction is used for immediate and
          boundary-aligned switches. The hold probe also bounds the cost of
          preparing early. A leg
          held for the whole (up to ``defer_switch_timeout_s``) wait decodes one
          buffer plus whatever ``decodebin``'s internal multiqueue admits, then
          blocks its own streaming thread. Without it the leg keeps decoding for
          the entire wait, and the selector throws every frame away. Measured on
          HALO over a 40 s wait, a real 360p H.264 rollover payload behind a
          640x360 channel (three runs each, worker process tree):

              channel alone            5.09 / 5.53 / 6.45 s CPU, RSS +0.3 MiB
              new leg HELD             5.75 / 5.88 / 6.02 s CPU, RSS +52 MiB
              new leg free-running     7.47 / 8.19 / 8.73 s CPU, RSS +57 MiB

          i.e. the held leg's CPU cost sits inside the channel's own run-to-run
          spread while the unheld one adds ~40%, on content chosen to be cheap;
          the saving scales with the payload's decode cost, so a 1080p rollover
          is where it stops being a rounding error. Note it is a CPU saving, not
          a memory one -- the leg's elements and decoder are allocated either
          way. Note also what the unheld case is NOT: ``sync-streams`` (see
          ``_SELECTOR_PROPS``) already paces an inactive pad against the active
          stream, so the old behaviour was a second real-time decode leg, not an
          unbounded burn through the file.

        The reload can never wedge the channel (the engine's "playout can never wedge"
        invariant). Escape hatches cover every way a switch might not happen: (a) a
        bounded watchdog aborts the reload if the new leg never delivers a first
        buffer (a live source that connects but never rolls); (b) a synchronous
        build/preroll failure aborts cleanly and re-raises (the current program keeps
        playing); (c) an async bus error on the uncommitted new leg aborts via
        ``_on_bus`` rather than taking output down; (d) for a deferred switch, a
        second, much longer watchdog (``defer_switch_timeout_s``) forces the switch
        anyway if the outgoing leg's EOS never arrives (a schedule item that runs
        long, or a leg that never naturally EOSes) — the reload always eventually
        commits, it never holds two legs open forever. A newer reload arriving while
        the prior leg is still uncommitted SUPERSEDES it. Once commit has selected a
        replacement, an overlap is explicitly rejected so the daemon can restart
        from the complete newest worker graph rather than queue only its program
        half.

        ``on_settled`` (item 4, honest ack): an optional ``(committed: bool,
        reason: str | None) -> None`` callback invoked EXACTLY ONCE for this
        specific call's reload -- with ``(True, None)`` from ``_commit_reload``, or
        ``(False, <reason>)`` from abort, failed commit cleanup, or stop. The reason
        is a stable diagnostic token such as ``timeout`` or ``cleanup-failed``. This is
        the whole point of the fix: this method itself only ARMS the reload (builds
        + prerolls the new leg and returns) -- the actual commit/abort happens later,
        asynchronously, on the main loop. The D2 Windows worker-pipe dispatch
        (``worker.py``'s ``_dispatch_control_with_ack``) used to ack a ``reload``
        command "applied" the instant this method RETURNED, which is not the same
        thing as the reload having landed -- a channel could be told "applied" for a
        reload that then silently timed out or errored. Passing ``on_settled`` lets
        that dispatch path defer its ack until the reload genuinely lands one way or
        the other. A caller that does not pass it (the POSIX FIFO dispatch path,
        ``_dispatch_control``, which was always fire-and-forget) is unaffected."""
        if self.selector is None or not self.selector_sink_pads:
            raise RuntimeError("engine not built; cannot reload")
        if getattr(self, "_stopping", False):
            self._notify_reload_settled(on_settled, False, "stopped")
            return
        if self._pending_reload is not None:
            if self._pending_reload.get("commit_in_progress", False):
                # The worker deserializes one graph, then calls reload_program()
                # before reload_graphics_overlay(). Privately saving only this
                # program leg would return control so that worker could advance the
                # corresponding overlay and ACK "armed" with no receipt binding the
                # delayed program request. Reject before building or mutating
                # anything. The worker reports an explicit error, strategy returns
                # False, and the daemon's existing fallback restarts from the
                # complete newest graph.
                raise RuntimeError("reload commit already in progress")
            # Supersede the still-settling reload with this newer one (never drop a
            # program change). The superseded leg is disposed before we build the new.
            print("CTRL reload superseding a still-settling reload", flush=True)
            self._abort_pending_reload("superseded")

        old_video_pad = self.selector_sink_pads[0]
        old_audio_pad = self.audio_sink_pads[0] if self.audio_sink_pads else None
        old_elements = self._source_leg_elements[0]

        # U30 EOS-origin diagnostic: watch the OUTGOING leg's own selector sink
        # pads, armed HERE -- before ``_arm_new_leg_selector_diagnostics``
        # installs this reload's boundary probes. Same-priority pad probes run in
        # installation order, so the observer sees an EOS even when the boundary
        # probe installed after it returns DROP (and when the boundary probe is
        # never reached at all, which is the deferred dead runs' shape). These
        # are the pads the outgoing leg actually delivers on: an ``out:video``
        # arrival with no ``mux-sink:video`` after it localises the loss to the
        # stretch between them, and an ``out:video`` with no drop line at all
        # says the reload never got the chance to decline it.
        self._install_eos_observer(old_video_pad, self._make_chain_eos_label("out", "video"))
        self._install_eos_observer(old_audio_pad, self._make_chain_eos_label("out", "audio"))

        # U30 Work 2 -- TRANSACTION OWNERSHIP BEGINS BEFORE THE BUILD.
        #
        # OBSERVED (2026-09-25 off-live reproduction, GST_DEBUG=input-selector:7,
        # concat:7): the deferred boundary EOS crosses the selector's ACTIVE sink
        # pad WHILE the new leg is still being built. On the reproducer the EOS
        # crossed at 0:00:02.195 and this reload's first arming line printed at
        # 0:00:02.441 -- 0.25s later -- because ``self._pending_reload`` and its
        # boundary DROP probes used to be installed only after
        # ``_instantiate_source_leg``/``_link_leg_to_selectors`` returned. An EOS
        # arriving in that window was forwarded by the selector, reached
        # ``mpegtsmux``, and quit the worker: ``teardown_clean=True``, no commit
        # stage, ``reload-status.json`` ``aborted:stopped``, output stopped for
        # good. The same graph ending at the same instant COMMITS when the probes
        # are armed in time -- one race, two outcomes.
        #
        # So: transaction identity, the pending slot and the DROP probes are
        # established first; only the fields that describe the NEW leg are filled
        # in when the build returns. Nothing on the EOS path reads a new-leg field
        # -- ``_on_old_leg_eos`` merely records the boundary, and
        # ``_on_new_leg_ready`` commits at once when the boundary was already seen
        # -- so an EOS caught during the build becomes the ordinary healthy
        # deferred switch instead of a dead channel.
        #
        # ``rebase_new_leg`` comes from the leg object alone, so the arming
        # condition is exactly the one this method always used
        # (``switch_at_end_of_current or rebase_new_leg``) with the widest
        # possible window. It also cannot leak a built leg any more: an exception
        # here now precedes the build rather than following it.
        rebase_new_leg = not source_leg_is_clock_timed(new_leg)

        pending: dict[str, Any] = {
            "new_output_gain_db": new_leg.output_gain_db
            if isinstance(new_leg, PlaylistLeg)
            else 0.0,
            # Round-2 finding 5: identifies THIS reload transaction. The boundary
            # probes live on the OUTGOING pads, which are the same pad objects
            # across successive transactions (``selector_sink_pads[0]`` until a
            # commit swaps it), so "is this pad one I am watching?" cannot tell a
            # superseded transaction's queued EOS from the current one's. Each
            # boundary probe therefore carries the id of the transaction that
            # installed it and ``_on_old_leg_eos`` compares it before settling.
            "txn_id": next(self._reload_txn_counter),
            "old_video_pad": old_video_pad,
            "old_audio_pad": old_audio_pad,
            "old_elements": old_elements,
            # The NEW-leg fields, filled in below once the build returns. They are
            # present from the start because this dict is reachable before then --
            # the boundary probes below run on streaming threads while the build
            # is still in progress.
            "new_video_pad": None,
            "new_audio_pad": None,
            "new_elements": [],
            "probe_id": None,
            "readiness_probes": [],
            "ready_pads": set(),
            "timeout_id": None,
            # Item 4 (honest ack): the caller's completion callback, if any --
            # invoked exactly once by ``_commit_reload`` or ``_abort_pending_reload``.
            "on_settled": on_settled,
            "commit_in_progress": False,
            "commit_watchdog": None,
            "commit_completed": None,
            "retirement_result": None,
            # Nonblocking DROP fences first close the outgoing selector-sink
            # timestamp boundary, then contain the source peers while their request
            # pads and elements are retired off the GLib/streaming threads.
            "old_tail_drop_probes": [],
            "handoff_started": False,
            "selector_handoff_started": False,
            # Every switch waits for this transaction's first buffer. Only the
            # outgoing-EOS condition starts satisfied for an immediate switch.
            "switch_at_end_of_current": switch_at_end_of_current,
            "new_leg_ready": False,
            # U41: a boundary-aligned switch normally waits for the outgoing leg's
            # own EOS. After ``_hold_slate_for_plan_eos`` there is no such EOS to
            # wait for -- the leg already ended and its EOS was dropped -- so the
            # transaction starts with the boundary recorded, which is the state
            # ``_on_new_leg_ready`` commits on at once. Without this, a reload
            # dispatched after a held plan end would sit on a ready leg until the
            # 900 s ``defer_switch_timeout_s`` fired, with the slate on air the
            # whole time.
            "old_leg_eos": (not switch_at_end_of_current) or self._plan_eos_held,
            "boundary_forced": False,
            "defer_timeout_id": None,
            # A natural A/V end waits for BOTH EOS events. Once the first stream
            # ends, a small producer keeps that selector's output continuous with
            # a repeated final video frame or format-correct audio silence until
            # the other stream's measured end. It is always off the streaming and
            # GLib threads; the events are only wake/stop signals, never locks held
            # across pad.push().
            "tail_padding_done": True,
            "tail_padding_started": False,
            "tail_padding_pad": None,
            "tail_padding_target_ns": None,
            "tail_padding_target_final": False,
            "tail_padding_wake": threading.Event(),
            "tail_padding_stop": threading.Event(),
            "tail_padding_thread": None,
            "tail_padding_start_id": None,
            # Finite replacement timing state (immediate or deferred):
            #   new_src_pads   -- the new leg's own tail src pad(s) (video, audio).
            #                     These carry the hold probes AND the running-time
            #                     rebase (Gst.Pad.set_offset), NOT the selector's
            #                     sink pads -- see the docstring, mechanism 3.
            #   hold_probes    -- (pad, probe_id) blocking probes holding the new
            #                     leg at its first buffer.
            #   holds_awaited  -- how many hold probes have not yet fired; the leg
            #                     is "ready" only when EVERY stream has decoded.
            #   boundary_probes-- (pad, probe_id) observers/drop-probes on the
            #                     OUTGOING pads. Deferred switches wait for their
            #                     EOS; immediate finite switches use their most
            #                     recent buffer end as the rebase point.
            #   outgoing_end   -- per outgoing pad: running time of the end of its
            #                     last buffer, and the pad's cached segment.
            #   outgoing_eos_pads -- outgoing pads whose EOS callback has reached
            #                     the main loop (deduplicates queued callbacks).
            "new_src_pads": [],
            "hold_probes": [],
            "holds_awaited": 0,
            "held_pads": set(),
            "boundary_probes": [],
            "outgoing_end": {},
            "outgoing_eos_pads": set(),
            "selector_notify_handlers": [],
            # A finite/segment-timed leg starts near running time zero even during
            # an immediate slate-to-programme switch. Hold and rebase it before
            # selection. A clock-timed live leg is already on the pipeline
            # running-time base and cannot be paused without going stale, so it
            # remains unheld/unrebased.
            "rebase_new_leg": rebase_new_leg,
        }
        self._pending_reload = pending
        if switch_at_end_of_current or rebase_new_leg:
            self._arm_boundary_probes(pending)

        # Build + link the new leg. A failure here has committed no state, so the
        # current program keeps playing — just propagate (the caller logs it).
        # F2 fix: ``_instantiate_source_leg`` already disposes ITS OWN partial
        # build on a raise (its own try/except); this second layer covers the
        # remaining leak window -- ``_link_leg_to_selectors`` raising AFTER
        # instantiate already succeeded, which would otherwise leave a fully
        # built, still-in-the-pipeline (but never-linked-anywhere) leg behind
        # with nothing left holding a reference to it.
        #
        # U30 Work 2: the probes armed above sit on the LIVE outgoing pads and
        # DROP that leg's EOS, so this unwind must take them off again. A
        # survivor would swallow the channel's next real boundary EOS forever --
        # worse than the race it closes.
        new_elements: list[Gst.Element] = []
        try:
            out_pad, audio_out_pad, new_elements = self._instantiate_source_leg(new_leg)
            new_video_pad, new_audio_pad = self._link_leg_to_selectors(
                "program(reload)", out_pad, audio_out_pad
            )
        except Exception:
            self._remove_boundary_probes(pending)
            if self._pending_reload is pending:
                self._pending_reload = None
            self._dispose_elements_best_effort(new_elements)
            raise
        # The build is done; the transaction can now describe what it built. The
        # boundary probes have been watching throughout, so any EOS they dropped
        # is already recorded in ``pending["old_leg_eos"]`` (and any buffer ends
        # in ``pending["outgoing_end"]``).
        pending["new_video_pad"] = new_video_pad
        pending["new_audio_pad"] = new_audio_pad
        pending["new_elements"] = new_elements
        pending["new_src_pads"] = [pad for pad in (out_pad, audio_out_pad) if pad is not None]
        # U30 EOS-origin diagnostic, second half: name EVERY selector sink pad,
        # including the one this reload just linked the new leg to. The ``out:*``
        # observers above can only name the pad the outgoing leg is expected to
        # deliver on; ten of the eleven deferred deaths recorded an EOS at the
        # selector's own src pad with no arrival on either of them, and an
        # ``input-selector`` only forwards an EOS that reached a sink pad. The
        # new leg's pads are named here because NOTHING else watches them before
        # the commit -- the ``in`` rung and its observer are armed at commit time
        # -- so an EOS arriving from the leg being switched in is otherwise
        # unobservable, which is the one hypothesis this reload can act on.
        self._install_selector_sink_eos_observers(
            extra=(("video", new_video_pad), ("audio", new_audio_pad)),
            skip=(old_video_pad, old_audio_pad),
        )
        try:
            if pending["rebase_new_leg"]:
                # ENG-002: hold a finite new leg AT its first buffer.
                # BLOCK|BUFFER lets the sticky STREAM_START/CAPS/SEGMENT through
                # (so the leg is fully linked and negotiated) but blocks the very
                # first buffer, which is both the readiness proof AND the point
                # decoding stops. Armed BEFORE PLAYING so no buffer can slip past.
                for pad in pending["new_src_pads"]:
                    probe_id = pad.add_probe(
                        Gst.PadProbeType.BLOCK | Gst.PadProbeType.BUFFER,
                        self._on_new_leg_hold,
                        pending["txn_id"],
                    )
                    pending["hold_probes"].append((pad, probe_id))
                    pending["holds_awaited"] += 1
            else:
                # Live/clock-timed streams must keep flowing, but video alone is
                # not proof that the replacement audio decoded. Observe each
                # stream without blocking or rebasing it. For deferred switches,
                # readiness still waits for the outgoing boundary.
                for pad in pending["new_src_pads"]:
                    probe_id = pad.add_probe(
                        Gst.PadProbeType.BUFFER, self._on_reload_first_buffer, pending["txn_id"]
                    )
                    pending["readiness_probes"].append((pad, probe_id))
            for element in new_elements:
                element.sync_state_with_parent()  # preroll the new leg
        except Exception:  # ENG-008: a preroll/arm failure must not wedge
            self._abort_pending_reload("build-error")
            raise
        # ENG-001: bound the wait for the new leg's first buffer. If it never arrives,
        # abort rather than pin _pending_reload forever (the old program keeps playing).
        pending["timeout_id"] = GLib.timeout_add_seconds(
            max(1, int(self.reload_timeout_s)), self._on_reload_timeout, pending["txn_id"]
        )

    def _on_reload_first_buffer(
        self, pad: Gst.Pad, _info: Gst.PadProbeInfo, txn_id: int
    ) -> Gst.PadProbeReturn:
        # Streaming thread: hand the readiness update to the main loop (no state
        # changes here).
        GLib.idle_add(self._on_unheld_probe_engaged, pad, txn_id)
        return Gst.PadProbeReturn.REMOVE

    def _on_unheld_probe_engaged(self, pad: Gst.Pad, txn_id: int) -> bool:
        """Main-loop: require a first buffer from every unheld replacement stream."""
        pending = self._pending_reload
        if pending is None or pending["txn_id"] != txn_id:
            return False
        expected = {source_pad for source_pad, _probe_id in pending["readiness_probes"]}
        if pad not in expected or pad in pending["ready_pads"]:
            return False
        pending["ready_pads"].add(pad)
        if expected <= pending["ready_pads"]:
            self._on_new_leg_ready(txn_id)
        return False

    def _on_new_leg_ready(self, txn_id: int) -> bool:
        """Main-loop: the new leg's first buffer landed. Cancels the
        new-leg-readiness watchdog (it has done its job); for a deferred switch,
        arms the longer ``defer_switch_timeout_s`` safety watchdog instead of
        committing immediately, and waits for the outgoing leg's EOS."""
        pending = self._pending_reload
        if (
            pending is None
            or pending["txn_id"] != txn_id
            or pending["new_leg_ready"]
            or pending["holds_awaited"] != 0
        ):
            return False  # aborted or superseded before the first buffer landed
        if pending["timeout_id"] is not None:
            with contextlib.suppress(Exception):
                GLib.source_remove(pending["timeout_id"])
            pending["timeout_id"] = None
        pending["new_leg_ready"] = True
        timing = "finite" if pending["rebase_new_leg"] else "clock"
        switch_mode = "deferred" if pending["switch_at_end_of_current"] else "immediate"
        print(
            f"CTRL reload: new leg preroll verified (reload_id={txn_id}) "
            f"held_streams={len(pending['hold_probes'])} timing={timing} mode={switch_mode}",
            flush=True,
        )
        # U34: the context a later per-stream stall line reports.
        self._reload_context = (txn_id, "prepared")
        if pending["switch_at_end_of_current"] and not pending["old_leg_eos"]:
            pending["defer_timeout_id"] = GLib.timeout_add_seconds(
                max(1, int(self.defer_switch_timeout_s)), self._on_defer_switch_timeout, txn_id
            )
            return False
        if pending.get("tail_padding_done", True):
            self._commit_reload()
        return False

    def _on_new_leg_hold(
        self, pad: Gst.Pad, _info: Gst.PadProbeInfo, txn_id: int
    ) -> Gst.PadProbeReturn:
        """Streaming thread: a finite new leg produced its first buffer
        on ``pad``. Returning from a BLOCK probe leaves the pad BLOCKED (and the
        callback is not re-entered until the probe is removed), which is exactly
        what is wanted: the buffer proves the leg really decodes, and the block
        stops it decoding any further until ``_commit_reload`` releases it. The
        readiness bookkeeping is handed to the main loop -- this thread is now
        parked inside GStreamer and must touch no engine state."""
        # Capture identity when the probe is installed, not when its delayed
        # callback runs. Superseding a reload must invalidate all its callbacks.
        GLib.idle_add(self._on_hold_probe_engaged, pad, txn_id)
        return Gst.PadProbeReturn.OK

    def _on_hold_probe_engaged(self, pad: Gst.Pad, txn_id: int) -> bool:
        """Main-loop: one of the deferred reload's streams reached (and is now
        holding at) its first buffer. The leg counts as ready only once EVERY
        stream has -- a video-only readiness signal would let the commit fire
        while the audio leg has not decoded a single frame."""
        pending = self._pending_reload
        if pending is None or pending["txn_id"] != txn_id:
            return False
        expected = {held_pad for held_pad, _probe_id in pending["hold_probes"]}
        if pad not in expected or pad in pending["held_pads"]:
            return False
        pending["held_pads"].add(pad)
        pending["holds_awaited"] = len(expected - pending["held_pads"])
        print(
            "CTRL reload: new leg stream held at its first buffer "
            f"({pending['holds_awaited']} stream(s) still to preroll) (reload_id={txn_id})",
            flush=True,
        )
        if pending["holds_awaited"] == 0 and not pending["new_leg_ready"]:
            self._on_new_leg_ready(txn_id)
        return False

    @staticmethod
    def _buffer_end_running_time(
        pad: Gst.Pad, buffer: Gst.Buffer, segment: Any
    ) -> tuple[int, Any] | None:
        """``(running time just past this buffer's end, segment used)``, or None if
        it cannot be computed. ``segment`` is the caller's cached segment for the
        pad; the pad's sticky SEGMENT is consulted when that cache is cold, because
        a probe armed on an ALREADY-RUNNING pad missed the event itself (the
        outgoing leg has been airing for a while by the time a reload arms this)."""
        if segment is None:
            sticky = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
            if sticky is None:
                return None
            segment = sticky.parse_segment()
        if buffer.pts == Gst.CLOCK_TIME_NONE:
            return None
        running = segment.to_running_time(Gst.Format.TIME, buffer.pts)
        if running == Gst.CLOCK_TIME_NONE or running < 0:
            return None
        duration = buffer.duration if buffer.duration != Gst.CLOCK_TIME_NONE else 0
        return int(running) + int(duration), segment

    def _on_outgoing_pad_data(
        self, pad: Gst.Pad, info: Gst.PadProbeInfo, txn_id: int
    ) -> Gst.PadProbeReturn:
        """Streaming thread: data crossing an outgoing pad while a switch is pending.

        * BUFFER -- record the running time of this buffer's END. That value (not
          the pipeline clock) is the rebase reference the new leg's running time
          continues from, so the seam is correct whether or not the sinks pace the
          pipeline.
        * EOS -- the boundary. DROP it: ``input-selector`` forwards an ACTIVE pad's
          EOS straight downstream to the encoder/mux/bus, which quits the run loop
          before any commit scheduled on the main loop could run (this is the exact
          defect this method exists to close). Dropping also keeps the selector from
          latching the pad as EOS'd. The probe deliberately STAYS installed (DROP,
          not REMOVE) so the OTHER stream's later EOS is dropped too, and the
          drop is UNCONDITIONAL -- it does not consult ``_pending_reload``. This
          probe only ever lives on a pad whose leg is being retired, and the
          window that matters is precisely the one where the reload has just
          committed (``_pending_reload`` back to None) but the old leg has not
          finished being disposed: an audio EOS arriving in THAT window is the
          one that would otherwise still take the channel off air.
        * anything else -- untouched.
        """
        pending = self._pending_reload
        if info.type & Gst.PadProbeType.BUFFER:
            if pending is None:
                return Gst.PadProbeReturn.OK
            state = pending["outgoing_end"].setdefault(pad, {"end": None, "segment": None})
            buffer = info.get_buffer()
            if buffer is not None:
                computed = self._buffer_end_running_time(pad, buffer, state["segment"])
                if computed is not None:
                    state["end"], state["segment"] = computed
                    # Keep only a ref to the latest buffer. The producer makes a
                    # deep copy once on EOS; copying every raw video frame here
                    # would turn a boundary observer into a frame-sized memcpy.
                    with contextlib.suppress(Exception):
                        state["last_buffer"] = buffer.copy()
                    fill_pad = pending.get("tail_padding_pad")
                    if fill_pad is not None and pad is not fill_pad:
                        # Live frontier: let the filler follow the stream that is
                        # still running without getting ahead of its latest end.
                        pending["tail_padding_target_ns"] = int(computed[0])
                        pending["tail_padding_wake"].set()
            return Gst.PadProbeReturn.OK
        event = info.get_event()
        if event is None:
            return Gst.PadProbeReturn.OK
        if event.type == Gst.EventType.EOS:
            # U30: name the drop before it happens. Until now the ONLY trace of
            # this path was the settlement line the queued callback prints
            # later, so an EOS dropped and then declined by one of
            # ``_on_old_leg_eos``'s guards left no line at all -- and a leg that
            # never delivered EOS to a probed pad left no line either. Those two
            # are the same sight (a frozen channel, a live worker) but opposite
            # diagnoses, and the 2026-09-25 logs could not tell them apart:
            # a drop line followed by no settle line is the first; no line at
            # all is the second.
            # The pad name is resolved with a fallback rather than inside the
            # blanket guard: this line's ABSENCE is itself evidence (see above), so
            # nothing but the write may be allowed to eat the whole line.
            try:
                pad_name = pad.get_name()
            except Exception:
                pad_name = "<unknown>"
            # Round 6: both outgoing leg pads are ``sink_0`` on two different
            # selectors, so the pad NAME cannot say which stream's EOS this is --
            # and EOS ORDER is what separates the clean reloads from the
            # reddening ones (live tally: video-first 5/5 clean, audio-first
            # 0/3). Resolve it by pad identity, the test ``_on_old_leg_eos`` uses.
            # The mux fence's own pad is not a leg pad and reports as such.
            try:
                if pending is not None and pad is pending.get("old_video_pad"):
                    stream_name = "video"
                elif pending is not None and pad is pending.get("old_audio_pad"):
                    stream_name = "audio"
                else:
                    stream_name = "<not-a-leg-pad>"
            except Exception:
                stream_name = "<unknown>"
            with contextlib.suppress(Exception):
                print(
                    f"CTRL reload diagnostic: outgoing-EOS-dropped pad={pad_name} "
                    f"stream={stream_name} "
                    f"pending_txn={pending['txn_id'] if pending is not None else 'none'}",
                    file=sys.stderr,
                    flush=True,
                )
            GLib.idle_add(self._on_old_leg_eos, pad, txn_id)
            return Gst.PadProbeReturn.DROP
        if event.type == Gst.EventType.SEGMENT and pending is not None:
            state = pending["outgoing_end"].setdefault(pad, {"end": None, "segment": None})
            state["segment"] = event.parse_segment()
        return Gst.PadProbeReturn.OK

    # -- U16 reload-side diagnostics: make the next displacement decomposable ----
    #
    # U16 (2026-09-24) could not explain a live, one-sided, constant +108.95s
    # audio displacement after a leg switch. The engine applies ONE offset (the
    # max of the outgoing ends) to BOTH streams, so the shared value cannot by
    # itself produce a one-sided step -- the trigger had to be in the two
    # per-pad ends, which were computed and then thrown away unprinted, and in
    # what the new leg's own first buffer became once the offset was applied,
    # which nothing recorded at all. These lines close both gaps.
    #
    # Always on, no env switch: the point is that the station's NEXT occurrence
    # is decomposable from its own logs. Nothing here may change behaviour --
    # every body is exception-guarded, every probe one-shot and read-only.

    def _reload_outgoing_ends_in_order(
        self, pending: dict[str, Any]
    ) -> list[tuple[str, int | None]]:
        """Each outgoing stream's observed ``state["end"]``, video first.

        Labelled by PAD IDENTITY, not by iteration order: ``outgoing_end`` is
        keyed by pad object and is populated in first-BUFFER order, which is not
        the order a leg declares its streams, so iterating the dict could
        silently swap the two labels. This is the same idiom ``_on_old_leg_eos``
        uses to name the stream it is retiring, and the same
        ``(("video", ...), ("audio", ...))`` tuple ``_arm_old_selector_cutoff``
        walks. A stream with no recorded state is reported as ``none`` rather
        than dropped -- "this pad observed nothing" is itself the finding.
        """
        ends: list[tuple[str, int | None]] = []
        for label, pad_key in (("video", "old_video_pad"), ("audio", "old_audio_pad")):
            pad = pending.get(pad_key)
            if pad is None:
                continue
            state = pending["outgoing_end"].get(pad)
            ends.append((label, None if state is None else state.get("end")))
        return ends

    def _rebase_reference_diagnostic(
        self,
        pending: dict[str, Any],
        *,
        switch_running_time: int,
        rebase_fallback: bool,
        pipeline_running_time_ms: int | None,
    ) -> str:
        """One line naming every input to the rebase reference.

        Printed to stderr beside the existing ``finite switch rebased to running
        time ...`` stdout line. ``ends=[video=..,audio=..]`` are the two numbers
        the switch point is chosen from (round 4: the SHORTER of them; the
        ``switch-at-shorter-leg`` line names which one won and what was trimmed)
        -- the per-pad values that U16 could not recover from the live logs --
        and ``fallback`` says whether the pipeline's own running time stood in
        for them. Both are needed to decompose a step: two ends ~109s apart
        indict the ends themselves, while two agreeing ends whose shared value
        still lands far from the output's own position indict the reference
        basis instead.
        """
        ends = ",".join(
            f"{label}={_seconds_or_none(end)}"
            for label, end in self._reload_outgoing_ends_in_order(pending)
        )
        mode = "deferred" if pending["switch_at_end_of_current"] else "immediate"
        pipeline = (
            _DIAGNOSTIC_NONE
            if pipeline_running_time_ms is None
            else f"{pipeline_running_time_ms / 1000:.3f}"
        )
        return (
            f"CTRL reload diagnostic: rebase-reference reload_id={pending['txn_id']} "
            f"mode={mode} streams={len(pending['new_src_pads'])} "
            f"fallback={'yes' if rebase_fallback else 'no'} ends=[{ends}] "
            f"pipeline_running_time={pipeline} "
            f"switch_running_time={_seconds_or_none(switch_running_time)}"
        )

    @staticmethod
    def _select_switch_running_time(
        measured_ends: list[tuple[str, int | None]],
        *,
        max_trim_ns: int,
    ) -> tuple[int, tuple[str, int] | None, int, bool]:
        """The shared switch point is the later observed outgoing end.

        Returns ``(switch_running_time, trim, spread_ns, bound_exceeded)``:

        * ``switch_running_time`` -- the value BOTH new-leg pads are offset to and
          the value ``old_tail_cutoff_ns`` publishes to the fence. One number, two
          readers, and it has to stay that way: the fence exists to cut the
          retiring leg exactly where the offset stops accounting for it.
        * ``trim`` -- always ``None``: the longer tail is retained, and the shorter
          stream is bridged by ``_run_tail_padding`` through this common end.
        * ``spread_ns`` -- the difference between the two ends, ``0`` when fewer
          than two were measured. Reported even when nothing was trimmed, so the
          bound's own decision is readable from the log line.
        * ``bound_exceeded`` -- always false. No tail is trimmed by this choice.

        ``measured_ends`` is ``(label, end)`` in leg order, video first, from
        ``_reload_outgoing_ends_in_order`` -- labelled by PAD IDENTITY, so the
        shorter/longer naming cannot be swapped by dict iteration order.
        """
        ends = [(label, end) for label, end in measured_ends if end is not None]
        if not ends:
            raise ValueError("no outgoing end measured")
        longest = max(ends, key=lambda item: item[1])
        spread_ns = longest[1] - min(item[1] for item in ends)
        if len(ends) == 1:
            return longest[1], None, 0, False
        return longest[1], None, spread_ns, False

    @staticmethod
    def _switch_shorter_leg_diagnostic(
        pending: dict[str, Any],
        *,
        measured_ends: list[tuple[str, int | None]],
        switch_running_time: int,
        trim: tuple[str, int] | None,
        spread_ns: int,
        bound_exceeded: bool,
    ) -> str:
        """One line naming which leg the switch point came from (U56 round 4).

        ``trimmed`` names the LONGER stream and how much of its delivered tail the
        fence cut; ``none`` when the two legs ended together, only one was
        measured, or the spread was over the bound -- ``bound_exceeded`` tells the
        last of those apart from the first two. ``spread`` is reported alongside
        so a boundary at 0.9s and one at 3.0s differ in more than their verdict.
        """
        ends = dict(measured_ends)
        trimmed = _DIAGNOSTIC_NONE if trim is None else f"{trim[0]}:{trim[1] / Gst.SECOND:.3f}"
        return (
            f"CTRL reload diagnostic: switch-at-common-end reload_id={pending['txn_id']} "
            f"video_end={_seconds_or_none(ends.get('video'))} "
            f"audio_end={_seconds_or_none(ends.get('audio'))} "
            f"trimmed={trimmed} spread={_seconds_or_none(spread_ns)} "
            f"bound={_SWITCH_SHORTER_LEG_MAX_TRIM_S:.3f} "
            f"bound_exceeded={'yes' if bound_exceeded else 'no'} "
            f"switch_running_time={_seconds_or_none(switch_running_time)}"
        )

    def _schedule_tail_padding(self, pending: dict[str, Any]) -> None:
        """Start the selected-output filler after the EOS probe has returned.

        The probe queues a GLib callback while GStreamer still owns the selector's
        streaming lock. A short main-loop timer moves the actual pad push out of
        that critical section; the producer itself is also a worker thread.
        """
        if pending.get("tail_padding_start_id") is not None:
            return
        try:
            pending["tail_padding_start_id"] = GLib.timeout_add(
                1, self._start_tail_padding, pending
            )
        except Exception as exc:
            pending["tail_padding_done"] = True
            pending["tail_padding_error"] = f"could not schedule filler: {exc!r}"
            print(
                f"WARN: reload tail filler could not be scheduled for "
                f"reload {pending['txn_id']}: {exc!r}",
                file=sys.stderr,
                flush=True,
            )

    def _start_tail_padding(self, pending: dict[str, Any]) -> bool:
        """Main-loop: start a bounded worker that bridges the first-ended stream."""
        pending["tail_padding_start_id"] = None
        if (
            self._pending_reload is not pending
            or self._stopping
            or pending["tail_padding_stop"].is_set()
        ):
            return False
        short_pad = pending.get("tail_padding_pad")
        state = pending.get("outgoing_end", {}).get(short_pad) if short_pad is not None else None
        template = None if state is None else state.get("last_buffer")
        segment = None if state is None else state.get("segment")
        if template is None or segment is None:
            pending["tail_padding_done"] = True
            pending["tail_padding_error"] = "the ended stream has no copyable final buffer/segment"
            self._tail_padding_finished(pending["txn_id"], pending["tail_padding_error"])
            return False
        stream = "video" if short_pad is pending.get("old_video_pad") else "audio"
        selector = self.selector if stream == "video" else self.audio_selector
        src_pad = None if selector is None else selector.get_static_pad("src")
        if src_pad is None:
            pending["tail_padding_done"] = True
            pending["tail_padding_error"] = f"{stream} selector has no src pad"
            self._tail_padding_finished(pending["txn_id"], pending["tail_padding_error"])
            return False
        pending["tail_padding_done"] = False
        pending["tail_padding_stream"] = stream
        pending["tail_padding_thread"] = threading.Thread(
            target=self._run_tail_padding,
            args=(pending, short_pad, src_pad, template, segment, stream),
            name=f"civiccast-reload-{stream}-tail-fill",
            daemon=True,
        )
        try:
            pending["tail_padding_thread"].start()
        except Exception as exc:
            pending["tail_padding_thread"] = None
            self._tail_padding_finished(pending["txn_id"], repr(exc))
        return False

    @staticmethod
    def _tail_padding_audio_shape(pad: Gst.Pad, template: Gst.Buffer) -> tuple[int, int, int]:
        """Return (rate, bytes/sample, channels) for raw PCM silence generation."""
        caps = pad.get_current_caps()
        if caps is None or caps.get_size() < 1:
            raise RuntimeError("audio selector input has no current raw caps")
        structure = caps.get_structure(0)
        if not structure.get_name().startswith("audio/x-raw"):
            raise RuntimeError(f"audio filler requires raw PCM, got {structure.get_name()}")
        fmt = structure.get_string("format") or "S16LE"
        widths = {
            "U8": 1,
            "S8": 1,
            "S16LE": 2,
            "S16BE": 2,
            "U16LE": 2,
            "U16BE": 2,
            "S24LE": 3,
            "S24BE": 3,
            "U24LE": 3,
            "U24BE": 3,
            "S24_32LE": 4,
            "S24_32BE": 4,
            "U24_32LE": 4,
            "U24_32BE": 4,
            "S32LE": 4,
            "S32BE": 4,
            "U32LE": 4,
            "U32BE": 4,
            "F32LE": 4,
            "F32BE": 4,
            "F64LE": 8,
            "F64BE": 8,
        }
        bytes_per_sample = widths.get(fmt)
        rate = int(structure.get_value("rate"))
        channels = int(structure.get_value("channels"))
        if bytes_per_sample is None or rate <= 0 or channels <= 0:
            raise RuntimeError(f"unsupported raw audio caps for silence: {caps.to_string()}")
        if template.get_size() % (bytes_per_sample * channels):
            raise RuntimeError("final PCM buffer is not sample-frame aligned")
        return rate, bytes_per_sample, channels

    def _run_tail_padding(
        self,
        pending: dict[str, Any],
        short_pad: Gst.Pad,
        selector_src: Gst.Pad,
        template_ref: Gst.Buffer,
        segment: Gst.Segment,
        stream: str,
    ) -> None:
        """Push repeated video or PCM silence up to the other stream's end.

        This runs with no engine lock held and never on a pad-probe callback. The
        other stream's buffer probe advances ``tail_padding_target_ns``; its EOS
        makes that target final. The output clock paces synthetic buffers, and the
        existing mux/rebase drain remains the final switch confirmation.
        """
        pending_id = pending["txn_id"]
        error: str | None = None
        try:
            template = template_ref.copy_deep()
            if template is None or template.pts == Gst.CLOCK_TIME_NONE:
                raise RuntimeError("final buffer has no usable PTS")
            duration = int(template.duration)
            audio_shape: tuple[int, int, int] | None = None
            audio_frames = 0
            audio_silence = b"\x00"
            if stream == "audio":
                rate, bytes_per_sample, channels = self._tail_padding_audio_shape(
                    short_pad, template
                )
                frame_bytes = bytes_per_sample * channels
                audio_frames = template.get_size() // frame_bytes
                if audio_frames <= 0:
                    raise RuntimeError("final PCM buffer is empty")
                duration = max(1, round(audio_frames * Gst.SECOND / rate))
                audio_shape = (rate, bytes_per_sample, channels)
                structure_format = (
                    short_pad.get_current_caps().get_structure(0).get_string("format")
                )
                if structure_format and structure_format.startswith("U8"):
                    audio_silence = b"\x80"
            if duration <= 0 or duration == Gst.CLOCK_TIME_NONE:
                raise RuntimeError("final buffer has no usable duration")
            pts = int(template.pts) + duration
            dts = (
                int(template.dts) + duration
                if template.dts != Gst.CLOCK_TIME_NONE
                else Gst.CLOCK_TIME_NONE
            )
            run_now = self._pipeline_running_time_ms() * int(Gst.MSECOND)
            mono_now = time.monotonic()
            wake: threading.Event = pending["tail_padding_wake"]
            stop: threading.Event = pending["tail_padding_stop"]
            while not stop.is_set():
                wake.clear()
                target = pending.get("tail_padding_target_ns")
                final = bool(pending.get("tail_padding_target_final"))
                current_run = segment.to_running_time(Gst.Format.TIME, pts)
                if current_run == Gst.CLOCK_TIME_NONE or current_run < 0:
                    raise RuntimeError("cannot map synthetic PTS to running time")
                current_run = int(current_run)
                if target is None or current_run >= int(target):
                    if final:
                        break
                    wake.wait(0.1)
                    continue
                remaining = int(target) - current_run
                chunk_duration = duration
                chunk_frames = audio_frames
                if stream == "audio" and audio_shape is not None:
                    rate, bytes_per_sample, channels = audio_shape
                    chunk_frames = min(audio_frames, (remaining * rate) // int(Gst.SECOND))
                    if chunk_frames <= 0:
                        if final:
                            break
                        wake.wait(0.05)
                        continue
                    chunk_duration = max(1, round(chunk_frames * Gst.SECOND / rate))
                else:
                    chunk_duration = min(duration, remaining)
                desired = mono_now + max(0.0, (current_run - run_now) / Gst.SECOND)
                delay = desired - time.monotonic()
                if delay > 0:
                    wake.wait(min(delay, 0.1))
                    continue
                if stop.is_set():
                    break
                if stream == "video":
                    filler = template.copy_deep()
                    filler.pts = pts
                    filler.dts = dts
                    filler.duration = chunk_duration
                else:
                    assert audio_shape is not None
                    _rate, bytes_per_sample, channels = audio_shape
                    byte_count = chunk_frames * bytes_per_sample * channels
                    filler = Gst.Buffer.new_allocate(None, byte_count, None)
                    filler.fill(0, audio_silence * byte_count)
                    filler.pts = pts
                    filler.dts = dts
                    filler.duration = chunk_duration
                    if template.offset != Gst.BUFFER_OFFSET_NONE:
                        filler.offset = int(template.offset) + audio_frames
                    if template.offset_end != Gst.BUFFER_OFFSET_NONE:
                        filler.offset_end = int(template.offset_end) + audio_frames
                flow = selector_src.push(filler)
                if flow != Gst.FlowReturn.OK:
                    raise RuntimeError(f"{stream} selector filler push returned {flow.value_nick}")
                pts += chunk_duration
                if dts != Gst.CLOCK_TIME_NONE:
                    dts += chunk_duration
                if stream == "audio":
                    audio_frames = chunk_frames
        except Exception as exc:
            error = repr(exc)
        GLib.idle_add(self._tail_padding_finished, pending_id, error)

    def _tail_padding_finished(self, txn_id: int, error: str | None) -> bool:
        """Main-loop settlement after the filler producer stops pushing."""
        pending = self._pending_reload
        if pending is None or pending.get("txn_id") != txn_id:
            return False
        pending["tail_padding_thread"] = None
        pending["tail_padding_done"] = True
        if error:
            pending["tail_padding_error"] = error
            print(
                f"WARN: reload tail filler stopped before its common end "
                f"(reload_id={txn_id}): {error}",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                f"CTRL reload diagnostic: tail-padding-complete "
                f"stream={pending.get('tail_padding_stream', 'none')} "
                f"target={_seconds_or_none(pending.get('tail_padding_target_ns'))} "
                f"audio_latency={getattr(self, 'output_audio_latency_ns', 0)}ns "
                f"reload_id={txn_id}",
                file=sys.stderr,
                flush=True,
            )
        expected = {pad for pad, _probe_id in pending.get("boundary_probes", ())}
        if expected and expected <= pending.get("outgoing_eos_pads", set()):
            pending["old_leg_eos"] = True
        if pending.get("new_leg_ready") and (
            pending.get("old_leg_eos") or pending.get("boundary_forced")
        ):
            self._commit_reload()
        return False

    @staticmethod
    def _measure_first_buffer(pad: Any, info: Any) -> tuple[int | None, int | None, int | None]:
        """``(pts, running time, segment base)`` of this buffer, in ns.

        Each field is measured independently and can be absent independently: a
        buffer whose PTS is readable but whose segment is not still says
        something useful, and a probe must never raise on a streaming thread.
        ``None`` means "could not measure", which is deliberately distinct from a
        measured ``0`` -- at a rebase boundary those mean opposite things.
        """
        pts: int | None = None
        running_time: int | None = None
        segment_base: int | None = None
        with contextlib.suppress(Exception):
            buffer = None if info is None else info.get_buffer()
            if buffer is not None and buffer.pts != Gst.CLOCK_TIME_NONE:
                pts = int(buffer.pts)
        with contextlib.suppress(Exception):
            sticky = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
            segment = None if sticky is None else sticky.parse_segment()
            if segment is not None:
                segment_base = int(segment.base)
                if pts is not None:
                    converted = segment.to_running_time(Gst.Format.TIME, pts)
                    if converted != Gst.CLOCK_TIME_NONE and converted >= 0:
                        running_time = int(converted)
        return pts, running_time, segment_base

    @staticmethod
    def _new_leg_first_buffer_diagnostic(
        *,
        label: str,
        txn_id: Any,
        applied_offset: int,
        measured: tuple[int | None, int | None, int | None] | None,
    ) -> str:
        """What the new leg's first buffer became AFTER the rebase offset.

        ``applied_offset`` is printed beside the measurement so the two can be
        compared directly in the log."""
        pts, running_time, segment_base = measured if measured is not None else (None, None, None)
        return (
            f"CTRL reload diagnostic: new-leg-first-buffer stream={label} "
            f"reload_id={txn_id} applied_offset={_seconds_or_none(applied_offset)} "
            f"pts={_seconds_or_none(pts)} running_time={_seconds_or_none(running_time)} "
            f"segment_base={_seconds_or_none(segment_base)}"
        )

    def _make_new_leg_first_buffer_reporter(
        self, *, label: str, txn_id: Any, applied_offset: int
    ) -> Any:
        """A one-shot probe callback for one new-leg tail src pad (see
        ``_make_mux_first_buffer_reporter`` for why this is a factory)."""

        def _report_new_leg_first_buffer(
            pad: Gst.Pad, info: Gst.PadProbeInfo
        ) -> Gst.PadProbeReturn:
            with contextlib.suppress(Exception):
                print(
                    self._new_leg_first_buffer_diagnostic(
                        label=label,
                        txn_id=txn_id,
                        applied_offset=applied_offset,
                        measured=self._measure_first_buffer(pad, info),
                    ),
                    file=sys.stderr,
                    flush=True,
                )
            return Gst.PadProbeReturn.REMOVE

        return _report_new_leg_first_buffer

    def _arm_new_leg_rebase_diagnostics(self, pending: dict[str, Any], applied_offset: int) -> None:
        """One self-removing first-buffer probe per new-leg tail src pad.

        Called from ``_begin_reload_commit`` AFTER the offsets are set and BEFORE
        the hold probes are released, and that window is what makes the
        measurement race-free: the leg's first buffer cannot flow until the holds
        lift, so every buffer this probe can see already carries the offset.

        Labelled by the pad's INDEX in ``new_src_pads``, which the leg builds as
        ``(video, audio)`` -- the same ordering convention the selector's sink
        pads use. A padded-out index is named as ``streamN`` rather than
        mislabelled as one of the two real streams.
        """
        txn_id = pending["txn_id"]
        for index, pad in enumerate(pending["new_src_pads"]):
            label = (
                _NEW_LEG_STREAM_LABELS[index]
                if index < len(_NEW_LEG_STREAM_LABELS)
                else f"stream{index}"
            )
            with contextlib.suppress(Exception):
                pad.add_probe(
                    Gst.PadProbeType.BUFFER,
                    self._make_new_leg_first_buffer_reporter(
                        label=label, txn_id=txn_id, applied_offset=applied_offset
                    ),
                )

    @staticmethod
    def _new_leg_selector_first_buffer_diagnostic(
        *,
        label: str,
        pad_name: str,
        txn_id: Any,
        applied_offset: int,
        measured: tuple[int | None, int | None, int | None] | None,
    ) -> str:
        """That same first buffer, read where the rebase offset is visible.

        ``Gst.Pad.set_offset`` takes effect at the CROSSING to the pad's peer, not
        on the pad it is called on: a probe on the offset pad keeps reading that
        leg's own pre-offset timeline. Measured on the packaged 1.28.5 runtime --
        the offset pad reported ``pts=0.300 segment_base=0.000 running=0.300`` for
        a buffer its peer reported as ``pts=0.300 segment_base=5.000
        running=5.300``, and every later buffer likewise (5.333, 5.367, ...). So
        the tail-src-pad line above cannot show the post-offset running time; this
        one is measured on the receiving side (the new leg's selector sink pad),
        and it is the number that has to line up with ``switch_running_time`` for a
        seam with no step. ``segment_base`` here should read as the applied offset.
        """
        pts, running_time, segment_base = measured if measured is not None else (None, None, None)
        return (
            f"CTRL reload diagnostic: new-leg-selector-first-buffer stream={label} "
            f"pad={pad_name} reload_id={txn_id} applied_offset={_seconds_or_none(applied_offset)} "
            f"pts={_seconds_or_none(pts)} running_time={_seconds_or_none(running_time)} "
            f"segment_base={_seconds_or_none(segment_base)}"
        )

    def _make_new_leg_selector_first_buffer_reporter(
        self, *, label: str, txn_id: Any, applied_offset: int
    ) -> Any:
        """A one-shot probe callback for one new-leg selector SINK pad."""

        def _report_new_leg_selector_first_buffer(
            pad: Gst.Pad, info: Gst.PadProbeInfo
        ) -> Gst.PadProbeReturn:
            with contextlib.suppress(Exception):
                pad_name = "unknown"
                with contextlib.suppress(Exception):
                    pad_name = str(pad.get_name())
                print(
                    self._new_leg_selector_first_buffer_diagnostic(
                        label=label,
                        pad_name=pad_name,
                        txn_id=txn_id,
                        applied_offset=applied_offset,
                        measured=self._measure_first_buffer(pad, info),
                    ),
                    file=sys.stderr,
                    flush=True,
                )
            return Gst.PadProbeReturn.REMOVE

        return _report_new_leg_selector_first_buffer

    def _arm_new_leg_selector_diagnostics(
        self, pending: dict[str, Any], applied_offset: int
    ) -> None:
        """One self-removing first-buffer probe per new-leg selector SINK pad.

        Same window as ``_arm_new_leg_rebase_diagnostics`` -- after the offsets are
        set, before the holds lift -- and race-free for the same reason: the leg's
        first buffer cannot flow until the holds lift. Labelled by pad IDENTITY
        (``new_video_pad``/``new_audio_pad`` are the selector request pads, built
        video-first), which is exact here, unlike the index fallback the tail-src
        pads need.
        """
        txn_id = pending["txn_id"]
        for label, pad_key in (("video", "new_video_pad"), ("audio", "new_audio_pad")):
            pad = pending.get(pad_key)
            if pad is None or not hasattr(pad, "add_probe"):
                continue
            with contextlib.suppress(Exception):
                pad.add_probe(
                    Gst.PadProbeType.BUFFER,
                    self._make_new_leg_selector_first_buffer_reporter(
                        label=label, txn_id=txn_id, applied_offset=applied_offset
                    ),
                )
        # U30: the same pad, counted rather than reported once -- see the flow
        # ladder's ``in`` rung for why the first buffer alone cannot say whether
        # the producer KEPT pushing after the switch.
        self._install_new_leg_inbound_counters(pending)
        # U59 round 4: the same moment is "this leg is the selected one now", so
        # it is also where the rate diagnostic learns which leg to attribute a
        # video starvation to. Guarded inside -- see ``_u59_record_leg``.
        self._u59_record_leg(pending)

    @staticmethod
    def _mux_pad_stream_label(pad: Any) -> str:
        """``video``/``audio`` from the pad's negotiated caps, else its name.

        The caps are the negotiated truth about which stream this pad carries;
        the pad's name is only a fallback for an unnegotiated pad."""
        with contextlib.suppress(Exception):
            caps = pad.get_current_caps()
            if caps is not None:
                text = caps.to_string()
                if text.startswith("video/"):
                    return "video"
                if text.startswith("audio/"):
                    return "audio"
        with contextlib.suppress(Exception):
            name = pad.get_name()
            if name:
                return str(name)
        return "unknown"

    @staticmethod
    def _mux_first_buffer_diagnostic(
        *,
        label: str,
        pad_name: str,
        measured: tuple[int | None, int | None, int | None] | None,
    ) -> str:
        """The first buffer to reach one mux SINK pad, at worker start."""
        pts, running_time, segment_base = measured if measured is not None else (None, None, None)
        return (
            f"CTRL start diagnostic: mux-first-buffer stream={label} pad={pad_name} "
            f"pts={_seconds_or_none(pts)} running_time={_seconds_or_none(running_time)} "
            f"segment_base={_seconds_or_none(segment_base)}"
        )

    def _on_old_leg_eos(self, pad: Gst.Pad, txn_id: int) -> bool:
        """Main-loop: record outgoing EOS and bridge the first-ended A/V stream.

        The first EOS starts paced picture/silence padding on that selector's src
        while the other stream continues. Only after both natural EOS events and
        padding through the later measured end does the reload commit. This keeps
        the mux fed while waiting, and preserves the complete longer tail.

        Known residual, disclosed rather than papered over: in THAT ordering the
        outgoing leg has ended and its EOS has been dropped, so nothing is feeding
        the selector until the new leg becomes ready. Output freezes for the gap.
        If readiness never comes at all, ``_on_reload_timeout`` disposes the new leg
        and the frozen old one stays active -- at which point the S9-5 stall
        watchdog (``stall_timeout_s``, 10s) sees output stop advancing and quits so
        the daemon restarts the channel to a known state. A bounded freeze then a
        restart is the fail-safe end of this path; the alternative (forwarding the
        EOS) is an immediate, unconditional restart at EVERY boundary, which is the
        defect.

        U41 closed the same gap one case earlier -- a plan that ends with NO
        transaction in flight at all, which is what the 2026-09-25 education-channel
        incident was: ``_on_program_pad_plan_eos`` holds the slate for that interval.
        This path still freezes, deliberately: a transaction IS in flight, so its
        commit (or its abort, which hands the frozen leg back to the stall watchdog)
        is the settlement this boundary belongs to. The interval is bounded by
        ``reload_timeout_s`` rather than by a live slate, and the watchdog owns the
        escalation if it expires."""
        pending = self._pending_reload
        if pending is None:
            return False  # aborted or superseded before this fired
        # ``idle_add`` outlives the streaming-thread callback that queued it. A
        # superseding reload can replace ``_pending_reload`` before this runs; an
        # EOS from that older leg must never settle the new transaction. An
        # immediate finite reload now has outgoing observers too, so transaction
        # identity remains load-bearing for both switch modes.
        #
        # Round-2 finding 5: the pad identity alone cannot make that distinction.
        # The boundary probes sit on the OUTGOING pads, and those are the SAME pad
        # objects from one transaction to the next (``selector_sink_pads[0]`` is
        # only replaced when a commit swaps it), so a stale queued EOS from a
        # superseded transaction passed the old ``pad in expected`` test and
        # settled the transaction that replaced it. Compare the transaction id the
        # probe captured when it was installed instead; the pad-membership test
        # stays as the second half of the same guard.
        if txn_id != pending["txn_id"]:
            return False
        expected = {outgoing_pad for outgoing_pad, _probe_id in pending["boundary_probes"]}
        if pad not in expected:
            return False
        seen = pending["outgoing_eos_pads"]
        if pad in seen:
            return False
        seen.add(pad)
        stream = "video" if pad is pending["old_video_pad"] else "audio"
        print(
            "CTRL reload: outgoing EOS observed "
            f"stream={stream} ({len(seen & expected)}/{len(expected)} stream(s))",
            file=sys.stderr,
            flush=True,
        )
        if len(expected) <= 1:
            pending["old_leg_eos"] = True
            pending["tail_padding_done"] = True
        else:
            pending["tail_padding_done"] = False
            ends_by_pad = {
                outgoing_pad: pending.get("outgoing_end", {}).get(outgoing_pad, {}).get("end")
                for outgoing_pad in expected
            }
            if len(seen & expected) == 1:
                # The first-ended stream needs a live filler while its A/V partner
                # finishes. The partner's buffer probe advances this target, so
                # the filler never gets ahead of media still available on that leg.
                pending["tail_padding_pad"] = pad
                partner_end = max(
                    (
                        value
                        for candidate, value in ends_by_pad.items()
                        if candidate is not pad and value is not None
                    ),
                    default=None,
                )
                own_end = ends_by_pad.get(pad)
                candidates = [end for end in (partner_end, own_end) if end is not None]
                pending["tail_padding_target_ns"] = max(candidates) if candidates else None
                pending["tail_padding_target_final"] = False
                pending["tail_padding_started"] = True
                pending["tail_padding_wake"].set()
                self._schedule_tail_padding(pending)
            else:
                ends = self._reload_outgoing_ends_in_order(pending)
                measured = [(label, end) for label, end in ends if end is not None]
                if measured:
                    short_label, _short_end = min(measured, key=lambda item: item[1])
                    pending["tail_padding_pad"] = (
                        pending["old_video_pad"]
                        if short_label == "video"
                        else pending["old_audio_pad"]
                    )
                    pending["tail_padding_target_ns"] = max(end for _label, end in measured)
                pending["tail_padding_target_final"] = True
                pending["tail_padding_wake"].set()
                if not pending.get("tail_padding_started"):
                    pending["tail_padding_started"] = True
                    self._schedule_tail_padding(pending)
                elif (
                    pending.get("tail_padding_thread") is None
                    and pending.get("tail_padding_start_id") is None
                ):
                    self._schedule_tail_padding(pending)
        if (
            pending.get("new_leg_ready")
            and pending.get("old_leg_eos")
            and pending.get("tail_padding_done", True)
        ):
            self._commit_reload()
        return False

    def _force_deferred_boundary(self, txn_id: int, *, reason: str) -> bool:
        pending = self._pending_reload
        if (
            pending is None
            or pending["txn_id"] != txn_id
            or pending.get("commit_in_progress", False)
            or not pending["switch_at_end_of_current"]
            or not pending["new_leg_ready"]
            or pending.get("holds_awaited", 0) != 0
            or pending.get("old_leg_eos", False)
            or pending.get("boundary_forced", False)
        ):
            return False
        pending["boundary_forced"] = True
        print(f"CTRL reload: {reason} (reload_id={txn_id})", flush=True)
        if pending.get("tail_padding_started") and not pending.get("tail_padding_done", True):
            observed = [
                end
                for _label, end in self._reload_outgoing_ends_in_order(pending)
                if end is not None
            ]
            if observed:
                pending["tail_padding_target_ns"] = max(observed)
            pending["tail_padding_target_final"] = True
            pending["tail_padding_wake"].set()
            if pending.get("tail_padding_thread") is None:
                self._schedule_tail_padding(pending)
            return True
        self._commit_reload()
        return True

    def _on_defer_switch_timeout(self, txn_id: int) -> bool:
        """Safety watchdog (B3 fix): the outgoing leg's EOS never arrived within
        ``defer_switch_timeout_s`` of the new leg becoming ready. Force the switch
        rather than hold two legs open indefinitely -- the reload always eventually
        commits."""
        pending = self._pending_reload
        if pending is None or pending.get("txn_id") != txn_id:
            return False
        pending["defer_timeout_id"] = None
        self._force_deferred_boundary(
            txn_id,
            reason=(
                "outgoing leg produced no EOS within "
                f"{max(1, int(self.defer_switch_timeout_s))}s of replacement readiness; "
                "forcing switch"
            ),
        )
        return False

    def _arm_commit_watchdog(self) -> tuple[threading.Timer, threading.Event]:
        """Item 85 (sandbox runs 12/14/15): ``_commit_reload`` must never be able to
        hang the worker forever. Seven soaks recorded the SAME wedge: the last line
        either worker ever printed was "boundary switch rebased...", then nothing --
        the process stayed alive but the pipe control reader stopped answering.
        Later physical traces localized it to synchronous old-leg disposal; see
        ``_commit_reload`` for the selector-lock/caption-GAP cycle and its repair.
        This watchdog remains the independent last-resort bound and live-stack
        evidence if any commit path wedges again -- see ``faulthandler`` below.

        A ``GLib.timeout_add_seconds`` source is USELESS as the sole guard here: if
        the wedge is (as measured) the GLib main-loop thread itself blocked inside a
        synchronous GStreamer call, the main loop never gets a turn to run ANY of
        its own timeout sources -- the same thread is stuck. Only a real OS-level
        thread, independent of the GLib loop, is guaranteed to fire regardless of
        what either retirement or main-loop finalization is doing. It remains armed
        until ``_finish_reload_commit`` completes cleanup, hold release, logging, and
        settlement; a commit that finishes in time never prints from this thread or
        calls ``os._exit``.

        Returns ``(timer, completed)``: ``completed`` is a ``threading.Event`` the
        caller sets (in its ``finally``, BEFORE calling ``timer.cancel()``) the
        moment the commit genuinely finishes. ``Timer.cancel()`` alone cannot
        close the race where the timer's own thread has ALREADY started running
        ``_on_commit_wedged`` (past the cancel-event check inside ``Timer.run``)
        at the exact moment the commit finishes -- cancelling at that point is a
        no-op, and without this second flag the watchdog would still dump a stack
        and force-exit a worker that had, in fact, just committed cleanly.
        ``_on_commit_wedged`` re-checks ``completed`` as its very first action and
        returns immediately, exiting nothing, if it is set.

        Deliberately does NOT attempt ANY pipeline teardown before exiting (no
        ``set_state(Gst.State.NULL)``, unlike a graceful ``stop()``): a downward
        state transition takes GStreamer's internal per-element ``STREAM_LOCK``,
        which is exactly the lock a genuinely wedged streaming thread already
        holds -- attempting it here would either do nothing (if the lock is free,
        in which case it wasn't a real wedge) or itself block this watchdog
        thread indefinitely, defeating the one guarantee this method exists to
        provide. ``os._exit`` is called unconditionally (in a ``finally``)
        immediately after the diagnostic dump, with no attempt at a clean exit in
        between -- the exit must fire even if the dump or the print themselves
        raise (e.g. a closed/unusable stderr).

        No ``WORKER_RESULT`` receipt is ever emitted on this path, BY DESIGN: the
        whole reason this watchdog exists is that the main thread that would
        normally build and print that receipt is presumed stuck forever, and
        ``os._exit`` bypasses every remaining line of Python on this process,
        including the one that would print it. A caller reading this worker's
        exit (e.g. ``civiccast.native.installed_gstreamer_smoke.
        require_clean_worker_result``) must treat
        ``GST_RELOAD_COMMIT_TIMEOUT_EXIT_CODE`` as its own distinct, receipt-less
        signal, not as a missing-receipt failure of some other kind."""
        completed = threading.Event()

        def _on_commit_wedged() -> None:
            if completed.is_set():
                return  # the commit finished; cancel() lost the race, this didn't
            try:
                # FIRST: dump every thread's live Python stack to stderr -- this
                # is the actual deliverable that localizes the wedge on the next
                # soak that reproduces it (round 1 of this item shipped a
                # hypothesis about where it was without this proof; review
                # rejected that hypothesis and asked for the instrument instead
                # of another guess).
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
                print(
                    "CTRL reload: commit did not finish within "
                    f"{self.commit_timeout_s:.0f}s - quitting for daemon restart",
                    file=sys.stderr,
                    flush=True,
                )
            finally:
                # Unconditional: the exit must happen even if stderr itself is
                # unusable (a closed fd, a broken pipe) and the dump/print above
                # raised -- this watchdog's one job is to guarantee the process
                # goes away, not to guarantee a clean diagnostic first.
                os._exit(int(GST_RELOAD_COMMIT_TIMEOUT_EXIT_CODE))

        # self.commit_timeout_s is already validated/clamped by
        # _resolve_commit_timeout_s in __init__ -- no inline floor needed here.
        timer = threading.Timer(self.commit_timeout_s, _on_commit_wedged)
        timer.daemon = True
        timer.start()
        return timer, completed

    def _commit_reload(self) -> bool:
        """Quiesce the old leg, hand off on the main loop, then retire it off-loop.

        A no-op if the reload was aborted/superseded before this fired. ``return
        False`` keeps the GLib timeout/idle source one-shot. Transaction state,
        selector switching, hold release, final logs, and settlement callbacks stay
        on the main loop. Only ``_dispose_source_leg`` runs on the retirement thread.

        The 93f9168 Sandbox candidate's failure stack stopped inside
        ``peer.push_event(FLUSH_START)`` while the selector switch was pending and
        the other outgoing stream was still live. This path follows GStreamer's
        dynamic-unlink protocol without a blocking old-tail barrier: request both
        selector pads while the old tails still flow, release both first-buffer
        holds so GStreamer 1.28 can apply its pending switches, require exact
        active-pad notification/readback for both streams, then DROP-fence and
        release the inactive old request pads before NULLing the isolated old
        elements.

        Five staged stderr prints ("switching selector" / "holds released" /
        "selector handoff confirmed" / "old leg disposed" / "committed
        (elements=N)") show exactly where any future regression stalls, plus a
        real-OS-thread commit watchdog
        (``_arm_commit_watchdog``) that dumps every thread's live Python stack
        (``faulthandler.dump_traceback``) the moment a commit exceeds
        ``commit_timeout_s`` and then force-exits -- the actual localization
        tool for whichever GStreamer call is holding the GIL/thread when a
        future soak reproduces this."""
        pending = self._pending_reload
        if pending is None or pending.get("commit_in_progress", False):
            return False  # aborted or superseded before the first buffer landed
        # F-1: no caller (including an expired timer) may retire the old leg
        # without readiness for every required stream of THIS transaction.
        if not pending["new_leg_ready"] or pending["holds_awaited"] != 0:
            return False
        if (
            pending["switch_at_end_of_current"]
            and not pending["old_leg_eos"]
            and not pending.get("boundary_forced", False)
        ):
            return False
        print(f"CTRL reload: firing (reload_id={pending['txn_id']})", flush=True)
        pending["commit_in_progress"] = True
        # U34: set synchronously with the commit flag, so a per-stream stall
        # judged in this window (it cannot be -- commits suspend that check --
        # but a crash dump can) still names where the channel was.
        self._reload_context = (pending["txn_id"], "committing")
        pending["retirement_cancelled"] = False
        try:
            watchdog, completed = self._arm_commit_watchdog()
        except Exception as exc:
            pending["commit_in_progress"] = False
            print(f"ERROR: reload commit watchdog did not start: {exc!r}", flush=True)
            self._abort_pending_reload("commit-watchdog-start")
            return False
        pending["commit_watchdog"] = watchdog
        pending["commit_completed"] = completed
        try:
            self._prepare_reload_handoff(pending)
            self._start_reload_commit(pending)
        except Exception as exc:
            pending["commit_in_progress"] = False
            self._release_selector_notify_handlers(pending)
            self._release_old_tail_drop_fences(pending)
            self._finish_commit_watchdog(pending)
            print(f"ERROR: reload selector handoff setup failed: {exc!r}", flush=True)
            self._abort_pending_reload("selector-handoff-setup")
            return False
        return False

    def _prepare_reload_handoff(self, pending: dict[str, Any]) -> None:
        """Record old tails and subscribe to the selector's actual handoff.

        GStreamer 1.28's ``input-selector`` setter is two-phase: setting
        ``active-pad`` can leave a pending switch that is applied only when the
        selector sees the next buffer or serialized event. Both replacement pads
        are still held when this method runs, so setter return is not handoff
        proof. ``notify::active-pad`` plus property readback supplies that proof
        after both first-buffer holds are released.
        """
        pending["old_tail_drop_probes"] = []
        pending["handoff_started"] = False
        pending["selector_handoff_started"] = False
        pending["selector_handoff_confirmed"] = False
        pending["replacement_released"] = False
        pending["selector_notify_handlers"] = []
        if self.selector is None:
            raise RuntimeError("video selector disappeared before reload handoff")
        selectors = [self.selector]
        if pending["new_audio_pad"] is not None:
            if self.audio_selector is None:
                raise RuntimeError("audio selector disappeared before A/V reload handoff")
            selectors.append(self.audio_selector)
        for selector in selectors:
            if selector is None:
                continue
            handler_id = selector.connect(
                "notify::active-pad",
                self._on_selector_active_pad_notify,
                pending["txn_id"],
            )
            pending["selector_notify_handlers"].append((selector, handler_id))

    def _on_selector_active_pad_notify(
        self, _selector: Gst.Element, _pspec: object, txn_id: int
    ) -> None:
        """Queue selector property confirmation onto the GLib main loop."""
        GLib.idle_add(self._confirm_reload_selector_handoff, txn_id)

    def _confirm_reload_selector_handoff(self, txn_id: int) -> bool:
        """Start retirement only after both selectors report the new active pad."""
        pending = self._pending_reload
        if (
            pending is None
            or pending.get("txn_id") != txn_id
            or not pending.get("commit_in_progress", False)
            or pending.get("selector_handoff_confirmed", False)
            or not pending.get("replacement_released", False)
        ):
            return False
        expected = [(self.selector, pending["new_video_pad"])]
        if pending["new_audio_pad"] is not None and self.audio_selector is not None:
            expected.append((self.audio_selector, pending["new_audio_pad"]))
        try:
            if any(
                selector is None or selector.get_property("active-pad") != new_pad
                for selector, new_pad in expected
            ):
                return False
        except Exception as exc:
            print(f"WARN: reload selector handoff readback failed: {exc!r}", flush=True)
            return False
        # Publish the new role only after both two-phase selector transitions are
        # real. Setter return alone can still leave the old pad active in 1.28.
        self.selector_sink_pads[0] = pending["new_video_pad"]
        if self.audio_sink_pads and pending["new_audio_pad"] is not None:
            self.audio_sink_pads[0] = pending["new_audio_pad"]
        self._source_leg_elements[0] = pending["new_elements"]
        if hasattr(self, "_board_live_concat_kinds"):
            old_program_concat = self._program_video_concat
            self._program_video_concat = next(
                (
                    element
                    for element in pending["new_elements"]
                    if element.get_name().startswith("vconcat_program_")
                ),
                None,
            )
            if self._program_video_concat is not None:
                self._apply_board_concat_active(self._program_video_concat)
            if old_program_concat is not None:
                self._board_live_concat_kinds.pop(old_program_concat.get_name(), None)
            for name in pending.get("board_live_remove", ()):
                self._remove_overlay_layer(name)
        self._program_output_gain_db = pending.get("new_output_gain_db", 0.0)
        self._set_board_output_gain(self._program_output_gain_db)
        # U41: the program is back, so any plan-EOS hold is over -- and the hold has
        # to be re-armed on the pad just published, because the probe from before
        # this commit sits on the pad the line above displaced. Both statements
        # belong to this moment and nowhere else: earlier and the probe would guard
        # a pad that is not on air yet, later and a plan end in between would still
        # quit the worker.
        self._plan_eos_held = False
        self._arm_plan_eos_hold_probes()
        pending["selector_handoff_confirmed"] = True
        print(
            "CTRL reload diagnostic: stage=selector-handoff-confirmed",
            file=sys.stderr,
            flush=True,
        )
        self._release_selector_notify_handlers(pending)
        start_retirement = pending.get("retirement_start_event")
        if start_retirement is not None:
            start_retirement.set()
        return False

    @staticmethod
    def _release_selector_notify_handlers(pending: dict[str, Any]) -> None:
        """Disconnect active-pad observers; idempotent and best-effort."""
        for selector, handler_id in pending.get("selector_notify_handlers", ()):
            with contextlib.suppress(Exception):
                selector.disconnect(handler_id)
        pending["selector_notify_handlers"] = []

    def _start_reload_commit(self, pending: dict[str, Any]) -> None:
        """Request both selector switches, release holds, then await proof."""
        if (
            self._pending_reload is not pending
            or self._stopping
            or not pending.get("commit_in_progress", False)
            or pending.get("handoff_started", False)
        ):
            return
        pending["handoff_started"] = True

        start_retirement = threading.Event()
        pending["retirement_start_event"] = start_retirement
        try:
            thread = threading.Thread(
                target=self._retire_reload_old_leg,
                args=(pending, start_retirement),
                name="civiccast-reload-retirement",
                daemon=True,
            )
            thread.start()
        except Exception as exc:
            pending["handoff_started"] = False
            pending["commit_in_progress"] = False
            self._release_old_tail_drop_fences(pending)
            self._finish_commit_watchdog(pending)
            print(f"ERROR: reload retirement thread did not start: {exc!r}", flush=True)
            self._abort_pending_reload("commit-thread-start")
            return
        self._reload_commit_thread = thread
        try:
            self._begin_reload_commit(pending)
        except Exception as exc:
            self._handle_commit_failure(pending, exc)
            return
        # ``_begin_reload_commit`` releases the holds and then confirms immediate
        # selector readback. A real pending 1.28 switch starts this thread later
        # from ``notify::active-pad``; setter return alone never starts teardown.

    def _handle_commit_failure(self, pending: dict[str, Any], exc: BaseException) -> None:
        """Roll back a commit attempt that raised -- sync, or on a U37 drain resume.

        Factored out of ``_start_reload_commit``'s ``except`` (U37) unchanged, so
        the deferred rebase path rolls back through exactly the same code as the
        synchronous one. ``selector_handoff_started`` is what decides the two
        outcomes: before the first selector mutation the attempt is fully
        rollbackable; after it, there is no atomic rollback for a split A/V
        handoff and the commit watchdog is deliberately left armed to force worker
        recovery."""
        pending["retirement_cancelled"] = True
        start_retirement = pending.get("retirement_start_event")
        if start_retirement is not None:
            start_retirement.set()
        thread = self._reload_commit_thread
        if thread is not None:
            thread.join(timeout=min(1.0, self.teardown_timeout_s))
        self._reload_commit_thread = None
        self._release_selector_notify_handlers(pending)
        self._release_rebase_observers(pending)
        if not pending.get("selector_handoff_started", False):
            # Nothing reached either selector. Restore the outgoing leg exactly
            # as it was before the attempt, then retire the unused replacement.
            pending["handoff_started"] = False
            pending["commit_in_progress"] = False
            self._release_old_tail_drop_fences(pending)
            self._finish_commit_watchdog(pending)
            print(f"ERROR: reload commit setup failed before handoff: {exc!r}", flush=True)
            self._abort_pending_reload("commit-setup")
        else:
            # Once the first selector mutation starts there is no atomic rollback
            # for a possible split A/V handoff. Keep the watchdog armed so this
            # worker exits nonzero and the daemon rebuilds a coherent pipeline.
            print(
                f"ERROR: reload selector handoff failed: {exc!r}; "
                "commit watchdog will force worker recovery",
                file=sys.stderr,
                flush=True,
            )

    def _begin_reload_commit(self, pending: dict[str, Any]) -> None:
        """Main-loop half: request the rebased replacement and release its holds."""
        for timeout_key in ("timeout_id", "defer_timeout_id"):
            if pending[timeout_key] is not None:
                with contextlib.suppress(Exception):
                    GLib.source_remove(pending[timeout_key])
        # Still fail here, before the rebase block below, rather than only in
        # ``_continue_reload_commit``: a missing selector must abort this attempt
        # while nothing has been mutated, not after a rebase has been applied.
        selector = self.selector
        if selector is None:
            raise RuntimeError("video selector disappeared during reload commit")
        if pending["rebase_new_leg"]:
            # Close the outgoing timestamp boundary before reading it. These are
            # DATA probes on the old selector sink pads, not BLOCK/IDLE probes: an
            # in-flight buffer already beyond this point has already crossed the
            # timestamp observer; every later buffer is dropped before it can enter
            # input-selector. This prevents the old leg advancing after the rebase
            # snapshot without recreating the coupled A/V quiescence deadlock.
            #
            # U56: the fence is BOUND to the snapshot read below, not left
            # unbounded. Until ``old_tail_cutoff_ns`` is written (a few statements
            # away) it drops everything; from then on it drops only what the
            # retiring leg produces PAST the switch point, so the outgoing tail
            # the offset already accounts for is aired instead of discarded.
            self._arm_old_selector_cutoff(pending)
            # Rebase the held leg onto the outgoing leg's end BEFORE anything of it
            # crosses the selector (mechanism 3 in reload_program's docstring).
            # ONE offset for both streams, so neither video nor audio can start
            # EARLIER than where its own stream stopped: a backwards step is what
            # breaks PCR/CC.
            #
            # U56: the forward step that same choice creates is NOT sub-frame, and
            # until round 4 the choice was the MAX of the two ends -- which leaves
            # a real gap on the SHORTER stream's own timeline, and is exactly why
            # the fence above has to be value-bounded rather than instantaneous.
            # The gap the emitted output sees is the part no outgoing buffer
            # covers.
            #
            # U56 round 4: that one offset is the SHORTER of the two outgoing ends,
            # not the max. The max cannot start either stream early, but it starts
            # the shorter one LATE by the whole A/V end offset -- and the emitted
            # hole is exactly that offset, because the shorter stream has nothing
            # left to cover it with (measured live 2026-09-27: hole 0.631s =
            # audio end 8999.467 - video end 8998.836, picture only). The shorter
            # end costs the longer leg its tail past that point, which the fence
            # above has already been dropping since U56 -- so the value the fence
            # cuts at and the value the offsets are set to are still ONE number,
            # and that is the invariant to keep when changing either. See
            # ``_SWITCH_SHORTER_LEG_MAX_TRIM_S`` for the bound on the trim and why
            # a spread over it keeps the max instead.
            #
            # The ends read here are the FINAL ones: ``outgoing_end`` is updated by
            # the outgoing pad probe as the leg delivers and stops being updated
            # once it EOSes, and this snapshot is taken after the deferral has
            # waited for the end of the current programme -- so a leg that EOSed
            # is read at its true end whether it EOSed before or after the other
            # one (live, 2026-09-27: video first at the round-3 education seam,
            # audio first at the reload_id=6 one).
            measured_ends = self._reload_outgoing_ends_in_order(pending)
            observed = [end for _label, end in measured_ends if end is not None]
            # U16: the pipeline-time read happens ONCE, in the branch that uses
            # it, and both the applied value and the report come from that one
            # read -- a second read a tick later would print a reference that is
            # not the one the offset was actually set to.
            if observed:
                (
                    switch_running_time,
                    switch_trim,
                    switch_spread_ns,
                    switch_trim_bound_exceeded,
                ) = self._select_switch_running_time(
                    measured_ends,
                    max_trim_ns=int(_SWITCH_SHORTER_LEG_MAX_TRIM_S * Gst.SECOND),
                )
                pipeline_running_time_ms: int | None = None
            else:
                # No buffer was ever observed on the outgoing pads (a leg that
                # EOS'd immediately, or a forced switch before any buffer): fall
                # back to the pipeline's own running time. Never 0 -- that would
                # rewind the output timeline by the whole uptime. With no
                # per-stream bound to read, the fence stays unbounded.
                pipeline_running_time_ms = self._pipeline_running_time_ms()
                switch_running_time = pipeline_running_time_ms * int(Gst.MSECOND)
                # Nothing was measured, so there are no two ends to compare and
                # nothing to trim: the reference is the pipeline's own position,
                # which is not a per-stream end at all.
                switch_trim = None
                switch_spread_ns = 0
                switch_trim_bound_exceeded = False
                # U56 round 8: name the fallback, and the value it took, on the
                # pending record. The offset below is frozen here -- BEFORE the
                # rebase drain -- and on this branch the drain airs retiring
                # audio, so the release half re-measures and clamps forward.
                # See ``_clamp_fallback_switch_point``.
                pending["fallback_switch_running_time_ns"] = switch_running_time
            # U56 round 4: over the bound the max stands and the spread is named,
            # so a boundary that was NOT trimmed never looks like one that was.
            # U59 round 4 item 2: the line names the two MEASURED ends and no
            # longer asserts a broken asset -- see ``_bound_exceeded_warn_line``.
            if switch_trim_bound_exceeded:
                print(
                    self._bound_exceeded_warn_line(pending, measured_ends, switch_spread_ns),
                    file=sys.stderr,
                    flush=True,
                )
            # U56: Publish the bound the fence reads. This is the SAME value the
            # offsets below are set to, from the SAME snapshot -- one number, so
            # the retiring leg can never air content the new leg's offset does
            # not account for, and never loses content it does.
            # R8: the fallback publishes the SAME number the offsets above are set
            # to. ``observed`` empty means the outgoing pads were quiet at this read,
            # not that the retiring leg has no tail: on the round-7 red the tail was
            # already past the selector and inside the mux's isolation queue
            # (stage=rebase-drain-wait pads=sink_65=3,sink_66=4, drained after
            # 2.141s). Publishing None disarmed the mux fence entirely
            # (_arm_mux_tail_cutoff returns [] and _mux_tail_drop_decision is
            # fail-OPEN), so that tail aired past the switch: emitted
            # EMIT audio back=1 minstep=-2.134 -> relay 'Invalid timestamps'.
            # One number, two readers -- unchanged, just no longer unbounded.
            pending["old_tail_cutoff_ns"] = switch_running_time
            for pad in pending["new_src_pads"]:
                # NB: the leg's OWN tail src pad, not the selector's sink pad --
                # set_offset there marks the leg's sticky SEGMENT for re-send, so
                # the selector receives a segment whose base is the old leg's end.
                # On the selector's sink pad the segment has already crossed and
                # the stored copy is not rewritten (measured, GStreamer 1.28.5).
                with contextlib.suppress(Exception):
                    pad.set_offset(switch_running_time)
            # U56 round 5: the same bound, one hop downstream, on the audio pad.
            # The selector-side fence above reads the same cutoff, but the
            # retiring audio's tail has already passed it into the isolation
            # queue; this is the last pad before the mux, downstream of that
            # queue. See ``_arm_mux_tail_cutoff``.
            self._arm_mux_tail_cutoff(pending)
            switch_mode = "deferred" if pending["switch_at_end_of_current"] else "immediate"
            print(
                "CTRL reload: finite switch rebased to running time "
                f"{switch_running_time / Gst.SECOND:.3f}s mode={switch_mode} "
                f"streams={len(pending['new_src_pads'])} reload_id={pending['txn_id']}",
                flush=True,
            )
            # U16: the same switch, decomposed -- the per-pad ends the shared
            # reference was taken over, whether the pipeline-time fallback stood in
            # for them, and the value actually applied. Guarded: a diagnostic must
            # never be able to abort a commit.
            with contextlib.suppress(Exception):
                print(
                    self._rebase_reference_diagnostic(
                        pending,
                        switch_running_time=switch_running_time,
                        rebase_fallback=pipeline_running_time_ms is not None,
                        pipeline_running_time_ms=pipeline_running_time_ms,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
            # U56 round 4: which leg the reference came from, and what that cost
            # the other one. Printed at EVERY boundary, not only a trimmed one:
            # which branch this took is the fact that makes the next live change
            # decomposable, and a boundary that trimmed 0.6s and one that trimmed
            # nothing are otherwise indistinguishable in the emitted output.
            with contextlib.suppress(Exception):
                print(
                    self._switch_shorter_leg_diagnostic(
                        pending,
                        measured_ends=measured_ends,
                        switch_running_time=switch_running_time,
                        trim=switch_trim,
                        spread_ns=switch_spread_ns,
                        bound_exceeded=switch_trim_bound_exceeded,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
            # U16: observe each new stream's FIRST post-offset buffer. Armed here,
            # between the offsets above and the hold release below, so no buffer
            # can reach a probe without already carrying the rebase offset.
            with contextlib.suppress(Exception):
                self._arm_new_leg_rebase_diagnostics(pending, switch_running_time)
            # U16: the same first buffer, read where the offset is actually visible
            # (set_offset materialises at the crossing to the peer, so the tail src
            # pads above report the leg's own pre-offset timeline).
            with contextlib.suppress(Exception):
                self._arm_new_leg_selector_diagnostics(pending, switch_running_time)
            # U37: the outgoing leg's last queued buffer is still sitting on the
            # mux's sink pad for as long as it takes that pad to pop it, and the
            # segment this rebase sends downstream re-dates exactly that buffer --
            # see ``_arm_rebase_segment_observers``. Observe this switch's
            # arrivals, then refuse to switch at all until the pad has given the
            # old buffer up; the wait is bounded and off the main loop's critical
            # path.
            if self._arm_rebase_segment_observers(pending):
                held = self._rebase_drain_held(pending)
                if held and self._defer_rebase_drain(pending, held):
                    return
        self._continue_reload_commit(pending)

    def _clamp_fallback_switch_point(self, pending: dict[str, Any]) -> None:
        """U56 round 8: re-measure the FALLBACK's switch point where it is final.

        The fallback branch of ``_begin_reload_commit`` has no per-stream
        outgoing end to read, so it takes the pipeline's own running time as the
        switch point. That read happens BEFORE the bounded rebase drain, and on
        the fallback the drain is not a formality: the mux sink pad is still
        holding the retiring leg's already-consumed audio, and giving it up airs
        it. By the time the drain reports empty the audio mux pad's own aired
        frontier has moved past the frozen switch point, and the incoming audio's
        head lands exactly ON the offset (its own running time is still 0.000 at
        the crossing), so it lands behind the retiring audio that just aired.
        That is the emitted backward step.

        So the switch point is re-read here, on the release half, from the mux's
        OWN aired frontier: ``_mux_pad_airing_rt`` is written by
        ``_install_mux_pad_airing_frontier``'s ``buffer-consumed`` handler, and
        with ``set_offset`` on a branch pad ``consumed.pts - arrival.pts ==
        offset`` for every buffer -- the recorded value really is the mux's own
        running time for this stream, which is the value the emitted PES PTS
        carries.

        U62: EVERY affected pad and FORWARD only, deliberately -- the frontier that
        matters is the furthest one the mux has already aired, because that is the
        mux's own position in this switch and nothing may be placed behind it. The
        r8 form of this read is AUDIO only, and the audio pad is the one
        ``_arm_mux_tail_cutoff`` fences: a fenced buffer is DROPPED, a dropped
        buffer is never consumed, so the audio pad's frontier moves past the
        arm-time bound only once that fence has RELEASED. On C12 it never did --
        no ``stage=mux-tail-released*`` line in the whole run, the drain burned its
        full 3.0s deadline with the video pad still holding 2 buffers, and the
        reclamp stayed silent while the UNFENCED video pad's frontier ran on to
        188.769s against a frozen 185.808s. That is the failing half: silent on the
        very shape it was written for.

        It is not unsatisfiable in general, and the other half is why the audio
        column is not enough. At U56g's r8 probe run the fence carried a marker
        target (``target={'sink_66': 9215988888}``), the drain drained (1.421s), the
        audio frontier ran on, and the audio-only clamp DID fire -- and, moving the
        audio offset alone, left the two streams 1.055s apart where they entered the
        selector (``new-leg-selector-first-buffer``: video ``running_time=7.094``,
        audio ``running_time=8.149``) while video kept the stale arm-time offset.
        So an audio-only read fails one way, silent when the fence holds the pad,
        and an audio-only write fails the other, desynchronising when it releases.
        The max over the affected pads, written to EVERY new leg pad, is what makes
        both cases land on one number.

        The maximum across the affected pads is applied to ALL of the new leg's
        source pads, which keeps the one-offset-for-both-streams invariant and can
        never place a stream behind what the mux already aired; and a frontier at
        or behind the frozen offset is not a clamp -- the offset stands.
        ``old_tail_cutoff_ns`` is still left alone, and deliberately: it is the
        bound the retiring leg is CUT at, and moving it forward here would let the
        already-dropped retiring buffers back through the fence to be re-dated a
        second time. The offsets move; the cut stays where the retiring leg ended.
        """
        switch_ns = pending.get("fallback_switch_running_time_ns")
        if switch_ns is None or self._stopping:
            return
        airing = getattr(self, "_mux_pad_airing_rt", None) or {}
        if not airing:
            return
        affected = self._rebase_affected_mux_pads(pending)
        frontier = max(
            (int(airing[pad_name]) for _s, _p, pad_name in affected if pad_name in airing),
            default=None,
        )
        if frontier is None or frontier <= int(switch_ns):
            return
        offsets: list[str] = []
        for index, pad in enumerate(pending.get("new_src_pads") or ()):
            label = _NEW_LEG_STREAM_LABELS[index] if index < len(_NEW_LEG_STREAM_LABELS) else None
            if label is None or not hasattr(pad, "set_offset"):
                continue
            with contextlib.suppress(Exception):
                pad.set_offset(int(frontier))
                offsets.append(label)
        if not offsets:
            return
        pending["fallback_switch_reclamped_ns"] = int(frontier)
        print(
            "CTRL reload diagnostic: "
            f"stage=fallback-switch-point-reclamped streams={','.join(offsets)} "
            f"was={int(switch_ns)} now={int(frontier)} "
            f"pads={','.join(pad_name for _s, _p, pad_name in affected if pad_name in airing)} "
            f"reload_id={pending.get('txn_id')}",
            file=sys.stderr,
            flush=True,
        )

    def _continue_reload_commit(self, pending: dict[str, Any]) -> None:
        """Second half of the commit: switch both selectors, release, confirm.

        Split out of ``_begin_reload_commit`` (U37) so the rebased path can wait,
        bounded and asynchronously, for the affected mux sink pads to drain before
        anything is switched -- see ``_arm_rebase_segment_observers``. Everything
        below runs in the same order, on the same main loop, as it did before that
        split; that order is what the U30/U34 handover tests pin, and
        ``pending['selector_handoff_started']`` keeps its meaning exactly: the
        first selector mutation has completed."""
        # U56 round 8 / U62: the fallback's switch point is re-read HERE, after the
        # bounded drain has given the mux pad its queued buffers up, because that
        # is the first moment the mux's aired frontier is final. Every affected
        # pad, forward only. See ``_clamp_fallback_switch_point``.
        self._clamp_fallback_switch_point(pending)
        new_video_pad = pending["new_video_pad"]
        new_audio_pad = pending["new_audio_pad"]
        selector = self.selector
        if selector is None:
            raise RuntimeError("video selector disappeared during reload commit")
        print("CTRL reload: switching selector", flush=True)
        print("CTRL reload diagnostic: stage=switching-selector", file=sys.stderr, flush=True)
        # U56 round 5: the retiring audio's bound stays on past the first
        # mutation until the retiring tail has actually ARRIVED at the mux --
        # the drain's "pad is empty" is not that, once a fence eats the arrivals.
        # See ``_defer_mux_tail_release``.
        self._defer_mux_tail_release(pending)
        # Explicitly preserve successful upstream flow while each request is
        # pending and after it becomes inactive. InputSelectorPad currently
        # defaults this to true, but the reload contract must not depend on an
        # implicit plugin default.
        for old_selector_pad in (pending["old_video_pad"], pending["old_audio_pad"]):
            if old_selector_pad is None or not hasattr(old_selector_pad, "find_property"):
                continue
            if old_selector_pad.find_property("always-ok") is not None:
                old_selector_pad.set_property("always-ok", True)
        selector.set_property("active-pad", new_video_pad)
        # This flag means the first selector mutation completed, not merely that
        # it was attempted. A failure before this line can restore the old leg;
        # a later A/V-partial failure cannot be rolled back atomically.
        pending["selector_handoff_started"] = True
        audio_selector = self.audio_selector
        if new_audio_pad is not None and audio_selector is not None:
            audio_selector.set_property("active-pad", new_audio_pad)
        # Both pending setters have returned. Release BOTH first-buffer holds so
        # input-selector can observe the buffer/serialized event that applies its
        # pending active-pad transition. Teardown is still gated by notify plus
        # exact property readback in ``_confirm_reload_selector_handoff``.
        self._begin_caption_program(pending.get("old_tail_cutoff_ns"))
        self._release_hold_probes(pending)
        pending["replacement_released"] = True
        print("CTRL reload: holds released", flush=True)
        print("CTRL reload diagnostic: stage=holds-released", file=sys.stderr, flush=True)
        self._confirm_reload_selector_handoff(pending["txn_id"])

    def _retire_reload_old_leg(
        self, pending: dict[str, Any], start_retirement: threading.Event
    ) -> None:
        """Background half: retire the old leg, then request main-loop finalization."""
        start_retirement.wait()
        if pending.get("retirement_cancelled", False):
            return
        try:
            result = self._dispose_confirmed_old_leg(pending)
        except Exception as exc:  # defensive: disposal normally returns a failure result
            result = (False, f"unexpected retirement error: {exc!r}")
        pending["retirement_result"] = result
        GLib.idle_add(self._finish_reload_commit, pending)

    @staticmethod
    def _finish_commit_watchdog(pending: dict[str, Any]) -> None:
        completed = pending.get("commit_completed")
        watchdog = pending.get("commit_watchdog")
        if completed is not None:
            # Set BEFORE cancel(): closes Timer.cancel()'s already-running race.
            completed.set()
        if watchdog is not None:
            watchdog.cancel()

    def _finish_reload_commit(self, pending: dict[str, Any]) -> bool:
        """Main-loop finalizer: publish success only after complete old-leg cleanup."""
        if self._pending_reload is not pending or not pending.get("commit_in_progress", False):
            return False
        retirement_result = pending.get("retirement_result")
        if retirement_result is None:
            return False
        cleanup_ok, cleanup_reason = retirement_result
        self._reload_commit_thread = None
        self._release_selector_notify_handlers(pending)
        if not cleanup_ok or self._stopping:
            reason = "stopped" if self._stopping else "cleanup-failed"
            # Round-2 finding 3: a retirement that did not fully clean up is
            # reported honestly (the receipt says NOT applied) but no longer kills
            # the channel. The replacement leg is already selected and the mux is
            # still being fed; quitting the loop here took a PRODUCING channel off
            # air over a leftover element, and did so on returns -- ASYNC on a
            # bin's downward transition -- that were not even failures. If the
            # incomplete retirement really does stop output, the stall watchdog
            # takes the channel down a few seconds later with better evidence.
            print(
                f"ERROR: reload commit did not complete old-leg cleanup: {cleanup_reason or reason}"
                "; keeping the channel on air (stall watchdog owns a real outage)",
                file=sys.stderr,
                flush=True,
            )
            self._reload_context = (pending["txn_id"], "cleanup-failed")
            # U37: an observer left installed would keep counting for the rest of
            # the worker's life -- remove it on every exit from the commit, not
            # only on the clean one.
            self._release_rebase_observers(pending)
            for pad, probe_id in pending["boundary_probes"]:
                with contextlib.suppress(Exception):
                    pad.remove_probe(probe_id)
            self._release_hold_probes(pending)
            self._pending_reload = None
            self._notify_reload_settled(pending["on_settled"], False, reason)
            self._finish_commit_watchdog(pending)
            self._reset_stall_reference()
            return False

        print("CTRL reload: old leg disposed", flush=True)
        print("CTRL reload diagnostic: stage=old-leg-disposed", file=sys.stderr, flush=True)
        # U37: normally already removed by the rebased segment crossing the pad;
        # idempotent, and guarantees no observer outlives the transaction it
        # describes.
        self._release_rebase_observers(pending)
        # Only now may the outgoing pads' EOS-drop probes go: the retiring leg can
        # still emit an EOS (the audio stream typically ends a beat after video)
        # right up until it is unlinked and NULLed.
        for pad, probe_id in pending["boundary_probes"]:
            with contextlib.suppress(Exception):  # probe may already have auto-removed
                pad.remove_probe(probe_id)
        # Element count proves disposal reclaimed (the POSIX leak test asserts it is flat
        # across many reloads — a dispose leak would grow it).
        element_count = self._element_count()
        print(f"CTRL reload committed (elements={element_count})", flush=True)
        print(
            f"CTRL reload diagnostic: stage=committed elements={element_count}",
            file=sys.stderr,
            flush=True,
        )
        # U34: the state a per-stream stall in the live reload-7 shape reports
        # ("committed") -- the freeze happened after the commit, and this is
        # the line the coordinator's station evidence carried.
        self._reload_context = (pending["txn_id"], "committed")
        # Item 4 (honest ack): tell the caller the reload actually landed. Fired
        # last, after every other commit side-effect, and guarded so a callback
        # failure (e.g. the worker's pipe write) can never re-wedge a reload that
        # has, in every other respect, already committed cleanly.
        self._pending_reload = None
        self._notify_reload_settled(pending["on_settled"], True, None)
        self._finish_commit_watchdog(pending)
        self._reset_stall_reference()
        return False

    def _on_reload_timeout(self, txn_id: int) -> bool:
        """Watchdog: the new reload leg never produced a first buffer in time. Abort so
        the channel isn't wedged on a never-committing reload; the old program keeps
        playing and the next due program can retry."""
        if (
            self._pending_reload is None
            or self._pending_reload["txn_id"] != txn_id
            or self._pending_reload.get("commit_in_progress", False)
            or self._pending_reload["new_leg_ready"]
        ):
            return False  # already committed/aborted
        print(
            f"CTRL reload aborted: new program produced no buffer within {max(1, int(self.reload_timeout_s))}s; "
            "keeping current program",
            flush=True,
        )
        self._abort_pending_reload("timeout")
        return False  # one-shot

    def _is_current_program_pad(self, pad: Any) -> bool:
        """True only for the pad the channel is airing RIGHT NOW.

        Identity, never name or value. ``selector_sink_pads[0]`` is replaced by
        every commit (see ``_confirm_reload_selector_handoff``), so the pad a
        superseded transaction was watching is a DIFFERENT object from the one on
        air, and that is exactly the distinction the hold needs: an EOS from the
        displaced leg is the boundary machinery's business, not the hold's."""
        for slots in (self.selector_sink_pads, self.audio_sink_pads):
            for current in slots[:1]:
                if current is not None and current is pad:
                    return True
        return False

    def _arm_plan_eos_hold_probes(self) -> None:
        """Install the U41 plan-EOS hold on the CURRENT program leg's selector sink
        pads -- video and audio, one probe each.

        Best-effort by construction: a pad that refuses the probe is left
        unguarded, which is precisely the pre-U41 behaviour (the leg's EOS crosses
        and the worker exits cleanly), never a new failure. Both call sites matter:
        ``run_forever`` installs it for the first plan, and every commit installs it
        again, because a commit REPLACES the pad object in
        ``selector_sink_pads[0]``/``audio_sink_pads[0]`` -- a probe left behind on
        the displaced pad would guard a leg the channel no longer airs, and the next
        plan end would quit the worker exactly as it used to.

        Nothing is installed when the hold is disarmed (``run()``'s finite
        contract), and nothing is installed on the slate pads -- the slate leg is
        live and infinite (``bridge.py``), so an EOS from it is real information
        that must keep its existing behaviour."""
        if not self._hold_slate_at_plan_eos:
            return
        for pad in self.selector_sink_pads[:1] + self.audio_sink_pads[:1]:
            if pad is None:
                continue
            try:
                pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_program_pad_plan_eos)
            except Exception as exc:  # pragma: no cover - a pad that refuses a probe
                print(
                    f"WARN: could not arm the plan-EOS hold on {pad!r}: {exc!r}; "
                    "this leg's EOS will keep its existing behaviour",
                    file=sys.stderr,
                    flush=True,
                )

    def _on_program_pad_plan_eos(self, pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        """Streaming thread: the CURRENT program leg reached its natural end and
        nothing is being prepared to replace it. DROP the EOS and put the slate on
        air, so the channel never goes dark waiting.

        Why a probe and not the bus: once an EOS crosses an ACTIVE selector pad it
        is forwarded to ``mpegtsmux`` and to the bus, and ``_on_bus`` answers a
        pipeline EOS by quitting the loop (engine.py:1841-1844). The output side is
        ended by then -- re-selecting a pad cannot revive it. The only place an EOS
        can be declined is before it crosses, which is the same mechanism every
        healthy rollover already uses (``_arm_boundary_probes``).

        Why it declines so much: this probe sits on the same pad as a transaction's
        boundary probe and is installed FIRST (arming order is
        ``run_forever``/commit, then the transaction). Same-priority pad probes run
        in installation order, so an unconditional DROP here would starve
        ``_on_outgoing_pad_data`` and no reload could ever commit. Hence:

        * a reload transaction in flight -> OK: its boundary probe owns this
          boundary, and the commit it drives is the correct outcome;
        * fewer than two legs -> OK: there is no slate to hold, and swallowing the
          EOS would turn a clean, classifiable exit into an unclassifiable freeze;
        * not the current program pad -> OK: the slate leg and every displaced pad
          keep their own behaviour;
        * not an EOS (the mask is EVENT_DOWNSTREAM, which also carries segments,
          caps and tags) or an unreadable event -> OK. Dropping a SEGMENT here
          would corrupt the very timeline the reload machinery rebases onto.

        The decision is main-loop state (``_pending_reload``) read from a streaming
        thread, which is safe in one direction only: a transaction that appears
        between this read and the queued ``_hold_slate_for_plan_eos`` makes that
        callback re-check and decline, whereas a transaction that was already in
        flight is seen here. Losing that race is the pre-U41 behaviour, not a new
        failure."""
        if not self._hold_slate_at_plan_eos:
            return Gst.PadProbeReturn.OK
        if self._pending_reload is not None:
            return Gst.PadProbeReturn.OK
        if len(self.selector_sink_pads) < 2:
            return Gst.PadProbeReturn.OK
        if not self._is_current_program_pad(pad):
            return Gst.PadProbeReturn.OK
        try:
            event = info.get_event()
        except Exception:
            return Gst.PadProbeReturn.OK  # an unanswerable probe info is not an EOS
        if event is None or getattr(event, "type", None) != Gst.EventType.EOS:
            return Gst.PadProbeReturn.OK
        print(
            "CTRL plan EOS: program leg ended with no reload in flight; holding the slate",
            file=sys.stderr,
            flush=True,
        )
        GLib.idle_add(self._hold_slate_for_plan_eos)
        return Gst.PadProbeReturn.DROP

    def _hold_slate_for_plan_eos(self) -> bool:
        """Main-loop: the programme's plan ended with nothing replacing it, so put
        the always-hot slate leg on air and stay there until a reload commits.

        The slate leg is ``videotestsrc is-live=True`` + ``audiotestsrc
        is-live=True`` -- live and infinite, so it never EOSes and the output keeps
        advancing: the S9-5 stall watchdog is not tripped, the bus never sees an
        EOS, and the worker stays alive under the daemon's state row, which is
        already ON_AIR. That is also why the plan's end no longer needs to be
        treated as the channel's end: nothing about the off-air decision belonged
        to the plan in the first place.

        ``_plan_eos_held`` is what the next ``reload_program`` reads: its
        ``old_leg_eos`` flag starts True, so ``_on_new_leg_ready`` commits the
        instant the replacement's first buffer lands instead of arming the 900 s
        ``defer_switch_timeout_s`` for a boundary that has already passed.

        If the selector cannot be switched after the EOS has been dropped, the
        output really would freeze with no signal, so the worker is failed
        explicitly (``_error`` + quit) -- the daemon's restart is the correct and
        only remaining recovery, and it is the same escalation the stall watchdog
        uses."""
        if not self._hold_slate_at_plan_eos or self._plan_eos_held:
            return False
        if self._pending_reload is not None:
            return False  # a transaction appeared while this callback was queued
        if len(self.selector_sink_pads) < 2:
            return False
        try:
            self.swap.swap_to(_SLATE_LEG_INDEX)
        except Exception as exc:
            print(
                f"CTRL plan EOS: could not select the slate leg ({exc!r}); "
                "failing the worker so the daemon restarts the channel",
                file=sys.stderr,
                flush=True,
            )
            self._error = f"plan EOS: slate hold failed: {exc!r}"
            if self._loop is not None:
                with contextlib.suppress(Exception):
                    self._loop.quit()
            return False
        self._plan_eos_held = True
        print(
            "CTRL plan EOS: slate on air; the channel stays up until the replacement is ready",
            file=sys.stderr,
            flush=True,
        )
        return False

    def _arm_boundary_probes(self, pending: dict[str, Any]) -> None:
        """Watch BOTH outgoing pads (video AND audio -- an unhandled audio EOS
        latches the mux's audio pad just as fatally as a video one takes the whole
        pipeline down): track each pad's last-buffer end running time (the rebase
        reference) and DROP its EOS, so no pipeline-level EOS is ever produced at
        the boundary. Armed for EVERY deferred switch, plus an immediate finite
        switch so its replacement can be rebased to the actual outgoing A/V edge.

        U30 Work 2: armed BEFORE the new leg is built, because the boundary EOS
        arrives while that build is still running (see ``reload_program``). An EOS
        that crosses the selector in that window is forwarded, reaches
        ``mpegtsmux``, and quits the worker -- output stops with no commit. The
        probe is the only thing that can decline it, so it has to exist first."""
        for pad in (pending["old_video_pad"], pending["old_audio_pad"]):
            if pad is None:
                continue
            probe_id = pad.add_probe(
                Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM,
                self._on_outgoing_pad_data,
                pending["txn_id"],
            )
            pending["boundary_probes"].append((pad, probe_id))

    @staticmethod
    def _remove_boundary_probes(pending: dict[str, Any]) -> None:
        """Take this transaction's outgoing-side probes off the live pads.

        Every exit from an uncommitted transaction must run this. These probes
        DROP the outgoing leg's EOS and are installed on pads that outlive the
        transaction (``selector_sink_pads[0]`` is only replaced by a commit), so a
        survivor would swallow the channel's real boundary EOS forever."""
        for pad, probe_id in pending.get("boundary_probes", ()):
            with contextlib.suppress(Exception):
                pad.remove_probe(probe_id)

    def _abort_pending_reload(self, reason: str) -> None:
        """Tear down the in-flight (uncommitted) reload leg and clear the pending slot.
        The currently-active program is untouched. Used by supersede, build-error,
        watchdog timeout, and async-error containment."""
        pending = self._pending_reload
        if pending is None:
            return
        if pending.get("commit_in_progress", False):
            # The replacement is already selected; aborting it would tear down
            # active media. Commit failure/stop owns recovery from this point.
            return
        self._pending_reload = None
        pending["tail_padding_stop"].set()
        pending["tail_padding_wake"].set()
        if pending.get("tail_padding_start_id") is not None:
            with contextlib.suppress(Exception):
                GLib.source_remove(pending["tail_padding_start_id"])
            pending["tail_padding_start_id"] = None
        self._release_selector_notify_handlers(pending)
        if pending["probe_id"] is not None:
            with contextlib.suppress(Exception):  # probe may already have auto-removed
                pending["new_video_pad"].remove_probe(pending["probe_id"])
        for pad, probe_id in pending.get("readiness_probes", ()):
            with contextlib.suppress(Exception):  # first-buffer probes remove themselves
                pad.remove_probe(probe_id)
        self._remove_boundary_probes(pending)
        # MUST run before _dispose_source_leg below: a held pad has a GStreamer
        # streaming thread parked inside the blocking probe, and tearing the leg
        # down around a still-installed block is how a "disposal" turns into a
        # wedge. Releasing first lets those threads unwind normally.
        # Round-2 finding 1, half one: DETACH BEFORE RELEASE. Lifting the holds
        # lets this leg's streaming threads run again, and until now they ran
        # straight into the input-selector on a pad that is still INACTIVE. That
        # is a state the selector was never configured for: the class docstring on
        # ``_SELECTOR_PROPS`` says in as many words that the deferred switch "never
        # relies on" ``sync-streams`` because a held leg "pushes nothing at all
        # while inactive" -- which stops being true the moment an abort releases
        # it. On the commit path the same release happens only AFTER the pad has
        # been made active, so the two paths were releasing into two very
        # different selector states. Dropping this leg's data at its own src pads
        # keeps the abort path's promise structural rather than incidental: the
        # retiring leg cannot perturb the selector because nothing of it ever
        # reaches the selector.
        self._detach_leg_from_selectors(pending)
        # GstBin children can lose their parent link during asynchronous NULL
        # teardown before the main-loop bus callback handles an already-posted
        # ERROR. Snapshot those objects while the leg is intact so error
        # attribution does not depend on a parent chain that teardown can sever.
        attribution_elements = self._snapshot_leg_attribution_elements(pending["new_elements"])
        self._release_hold_probes(pending)
        if pending["timeout_id"] is not None and reason != "timeout":
            # 'timeout' means the watchdog source is firing now (auto-removed on return).
            with contextlib.suppress(Exception):
                GLib.source_remove(pending["timeout_id"])
        if pending["defer_timeout_id"] is not None:
            with contextlib.suppress(Exception):
                GLib.source_remove(pending["defer_timeout_id"])
        # Round-2 finding 1, half two: RETIRE OFF THE LOOP, exactly as the commit
        # path already does. ``_abort_pending_reload`` runs on the GLib main loop
        # (bus handler, watchdog, or supersede), and ``_dispose_source_leg`` calls
        # ``set_state(NULL)``, which BLOCKS until the leg's streaming threads are
        # joined. On a healthy leg that is fast -- measured at 0.078s on this
        # repository's own supersede reproducer -- but the leg being aborted on the
        # "error" path is by definition not healthy, and a NULL that blocks there
        # would take the main loop with it: no stall watchdog, no commit watchdog,
        # no control-plane reader, on a channel that is otherwise still on air.
        # Retiring on a worker thread makes that whole class impossible. Item 3's
        # result tuple is consumed and reported there rather than discarded.
        self._start_aborted_leg_retirement(
            reason,
            pending["new_video_pad"],
            pending["new_audio_pad"],
            pending["new_elements"],
            attribution_elements=attribution_elements,
        )
        # Item 4 (honest ack): tell the caller this reload did NOT land, and why --
        # ``reason`` is one of "error"/"timeout"/"superseded"/"build-error"/
        # "selector-missing" (the strings each call site above passes). Fired
        # without waiting on retirement: the reload's OUTCOME is already decided,
        # and blocking an honest ack behind cleanup is what item 4 fixed.
        self._notify_reload_settled(pending["on_settled"], False, reason)

    @staticmethod
    def _detach_leg_from_selectors(pending: dict[str, Any]) -> None:
        """Drop everything an aborted leg produces, at the leg's OWN src pads.

        Must run BEFORE ``_release_hold_probes``: once the holds are gone the
        leg's streaming threads are free, and this probe is what keeps their
        output out of the selector. Best-effort per pad -- a leg must never stay
        wedged because one probe could not be installed."""
        # ``.get`` deliberately: an abort is a SAFETY path reached from the bus
        # handler and the watchdogs, so it must degrade rather than raise if a
        # transaction was recorded without its src pads.
        for pad in pending.get("new_src_pads") or ():
            with contextlib.suppress(Exception):
                pad.add_probe(
                    Gst.PadProbeType.BUFFER
                    | Gst.PadProbeType.BUFFER_LIST
                    | Gst.PadProbeType.EVENT_DOWNSTREAM,
                    _drop_everything_probe,
                )

    def _start_aborted_leg_retirement(
        self,
        reason: str,
        video_pad: Gst.Pad,
        audio_pad: Gst.Pad | None,
        elements: list[Gst.Element],
        *,
        attribution_elements: list[Gst.Element] | None = None,
    ) -> None:
        """Retire an aborted leg on a worker thread; see ``_abort_pending_reload``."""
        # Item 3: reap a previous aborted leg only once it is provably gone -- its
        # retirement thread finished AND that retirement reported complete. Until
        # both hold, ``_belongs_to_aborted_leg`` keeps attributing its elements, so
        # the teardown of a leg this engine already rejected cannot take a still-
        # producing channel off air. A leg whose disposal FAILED is deliberately
        # kept: it is still in the pipeline, and the WARN below is the evidence.
        self._abort_retire_legs = [
            record
            for record in self._abort_retire_legs
            if record["thread"].is_alive() or not record["retired"]
        ]
        record: dict[str, Any] = {
            "thread": None,
            "elements": list(
                attribution_elements if attribution_elements is not None else elements
            ),
            "retired": False,
        }

        def _retire() -> None:
            try:
                retired, detail = self._dispose_source_leg(video_pad, audio_pad, elements)
            except Exception as exc:  # defensive: disposal returns a failure result
                retired, detail = False, f"unexpected abort retirement error: {exc!r}"
            record["retired"] = retired
            if not retired:
                print(
                    f"WARN: aborted reload leg ({reason}) did not retire cleanly: {detail}",
                    file=sys.stderr,
                    flush=True,
                )

        self._abort_retire_threads = [t for t in self._abort_retire_threads if t.is_alive()]
        thread = threading.Thread(target=_retire, name="cc-abort-retire", daemon=True)
        record["thread"] = thread
        self._abort_retire_threads.append(thread)
        self._abort_retire_legs.append(record)
        try:
            thread.start()
        except Exception as exc:
            # A thread that will not start must not leak the leg: fall back to the
            # in-line retirement this method exists to move OFF the loop. The
            # blocking risk is the lesser problem versus never cleaning up at all.
            self._abort_retire_threads.remove(thread)
            self._abort_retire_legs.remove(record)
            print(
                f"WARN: aborted reload leg ({reason}) retiring inline; "
                f"worker thread did not start: {exc!r}",
                file=sys.stderr,
                flush=True,
            )
            _retire()

    @staticmethod
    def _release_hold_probes(pending: dict[str, Any]) -> None:
        """Remove the blocking probes holding a deferred reload's new leg, letting
        its streaming thread(s) run again. Idempotent (the list is emptied), and
        best-effort per pad -- a leg must never stay wedged because one probe id
        was already gone."""
        for pad, probe_id in pending["hold_probes"]:
            with contextlib.suppress(Exception):
                pad.remove_probe(probe_id)
        pending["hold_probes"] = []

    @staticmethod
    def _release_old_tail_drop_fences(pending: dict[str, Any]) -> None:
        """Remove selector-sink/source-peer DROP fences after retirement or failure."""
        for pad, probe_id in pending.get("old_tail_drop_probes", ()):
            with contextlib.suppress(Exception):
                pad.remove_probe(probe_id)
        pending["old_tail_drop_probes"] = []

    @staticmethod
    def _arm_old_selector_cutoff(pending: dict[str, Any]) -> None:
        """Freeze the observed outgoing timestamp without blocking either stream.

        GStreamer pad DATA probes run before the selector chain function. Installing
        both probes before sampling ``outgoing_end`` means a buffer is either already
        represented in that snapshot or is dropped before reaching the selector.
        A partial installation raises; the caller removes every recorded fence while
        the original programme is still selected.

        U56: the fence drops by VALUE (``_drop_past_switch_point_probe``), not by
        the instant it was armed. The bound it enforces is
        ``pending["old_tail_cutoff_ns"]``, written by ``_begin_reload_commit``
        immediately after it reads the very snapshot this ordering protects -- so
        the fence and the new leg's rebase offset are provably one number. Until
        that key is written the probe drops everything, which is the pre-U56
        behaviour for the sub-millisecond window between these two statements.
        """
        for stream, selector_pad in (
            ("video", pending["old_video_pad"]),
            ("audio", pending["old_audio_pad"]),
        ):
            if selector_pad is None:
                continue
            try:
                probe_id = selector_pad.add_probe(
                    Gst.PadProbeType.BUFFER
                    | Gst.PadProbeType.BUFFER_LIST
                    | Gst.PadProbeType.EVENT_DOWNSTREAM,
                    _drop_past_switch_point_probe,
                    pending,
                )
                pending["old_tail_drop_probes"].append((selector_pad, probe_id))
            except Exception as exc:
                raise RuntimeError(f"old selector cutoff failed for {stream}: {exc!r}") from exc

    def _arm_mux_tail_cutoff(self, pending: dict[str, Any]) -> list[str]:
        """U56 round 5: put the switch-point bound on the AUDIO mux sink pad.

        The selector-side fence cannot reach a leg that has already raced ahead
        of the commit -- see ``_mux_tail_cutoff_probe``. This is the same bound,
        one hop downstream, on the pad ``_rebase_affected_mux_pads`` already
        resolves for the U37 drain.

        AUDIO only, and that is a measurement, not a preference. On the video
        path this fence is harmful. Round 5 attributed that to the encoder
        chain's ``videorate`` (which it no longer carries -- U56 round 7 removed
        it, see ``graph.encode_chain_specs``), and the round-5 measurement
        stands on its own terms: the trees that fenced both pads
        (``tree_fix4``/``tree_fix5``) each emitted a 0.067 s video hole, while
        the same code with only diagnostics differing (``tree_fix6``) did not.
        The reason survives the removal: a video buffer arriving at the mux past
        the bound is the INCOMING leg's first picture, whose own end sits ON the
        bound by construction (each stream's first buffer is mapped to the switch
        running time), so a video fence at this hop drops legitimate incoming
        picture rather than outgoing tail. The retiring VIDEO's tail is already
        dropped at the selector-side fence (``u56z15-fix7``: 18 buffers, every
        one ``dec=DROP``), so the video needs nothing added to it.

        The probe's own decision reads ``pending["outgoing_end"][pad]`` for the
        segment to measure a running time in. Nothing has recorded state for a
        MUX pad -- it is not a leg pad -- so it is seeded here, from the matching
        outgoing leg pad's state when that exists. The seed is the RETIRING
        leg's segment and it stays that segment for the fence's whole life, which
        is what makes the bound mean "past the switch point in the retiring leg's
        own timeline" rather than "later than the wall clock".

        Round 5 assumed that seeding alone kept the incoming leg's buffers: they
        would carry the same pts the retiring leg's did and compute as running
        ~0.000, inside the bound. MEASURED, that holds only when the incoming
        file's own head starts where the retiring one did. An incoming leg whose
        timestamps restart lower (or higher) is either unplaceable in this
        segment or placeable past the bound, and round 5's probe answered DROP to
        both. That is why the decision is now ``_mux_tail_drop_decision`` and why
        the fence also releases at the changeover segment
        (``_mux_tail_segment_probe``).

        A pad that cannot take the probe is a WARN, not a raise: the extra fence
        is an addition to the switch, and a commit that cannot arm it is the
        pre-fix behaviour, not a broken channel.

        The probes join ``old_tail_drop_probes`` so that every existing abort and
        retirement path removes them; the release only DISARMS them
        (``_disarm_mux_tail_cutoff``), so none is ever removed twice.
        """
        if pending.get("old_tail_cutoff_ns") is None:
            return []
        outgoing = pending.get("outgoing_end", {})
        armed: list[str] = []
        for stream, pad, pad_name in self._rebase_affected_mux_pads(pending):
            if stream != _MUX_TAIL_FENCE_STREAM:
                continue
            old_pad = pending.get(f"old_{stream}_pad")
            seed = outgoing.get(old_pad) if old_pad is not None else None
            state = pending["outgoing_end"].setdefault(pad, {"end": None, "segment": None})
            if state.get("segment") is None and seed is not None:
                state["segment"] = seed.get("segment")
            if seed is not None and seed.get("end") is not None:
                # The retiring leg's OWN measured end for this stream (live:
                # audio 3.605 against a switch point of 2.533). The fence may not
                # come off until an arrival reaches it.
                pending.setdefault("mux_tail_target", {})[pad_name] = int(seed["end"])
            elif seed is None or seed.get("end") is None:
                # A finite source leg can supply its measured end above. A
                # fallback slate has no EOS/end measurement, so keep the mux
                # fence bounded by the same switch point used by the selector
                # fence instead of treating the missing target as already
                # drained.
                pending.setdefault("mux_tail_target", {})[pad_name] = int(
                    pending["old_tail_cutoff_ns"]
                )
            try:
                probe_id = pad.add_probe(Gst.PadProbeType.BUFFER, _mux_tail_cutoff_probe, pending)
            except Exception as exc:
                print(
                    f"WARN: mux tail cutoff not installed on {pad_name} "
                    f"(stream={stream}) for reload {pending.get('txn_id')}: {exc!r}; "
                    "the outgoing audio may still air past the switch point",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            pending["old_tail_drop_probes"].append((pad, probe_id))
            pending.setdefault("mux_tail_cutoff_probes", []).append((pad, probe_id))
            # The origin boundary, on the same pad. Same ownership: both probe
            # ids live in ``old_tail_drop_probes``, so every abort and retirement
            # path removes each exactly once, and the release only disarms.
            try:
                seg_probe_id = pad.add_probe(
                    Gst.PadProbeType.EVENT_DOWNSTREAM, _mux_tail_segment_probe, pending
                )
            except Exception as exc:
                print(
                    f"WARN: mux tail origin release not installed on {pad_name} "
                    f"(stream={stream}) for reload {pending.get('txn_id')}: {exc!r}; "
                    "the fence still releases on the retiring tail's own arrival "
                    "or its deadline",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                pending["old_tail_drop_probes"].append((pad, seg_probe_id))
                pending.setdefault("mux_tail_cutoff_probes", []).append((pad, seg_probe_id))
            armed.append(pad_name)
        if armed:
            print(
                f"CTRL reload diagnostic: stage=mux-tail-armed "
                f"pads={','.join(armed)} "
                f"target={pending.get('mux_tail_target')} "
                f"cutoff={pending.get('old_tail_cutoff_ns')} "
                f"reload_id={pending.get('txn_id')}",
                file=sys.stderr,
                flush=True,
            )
        return armed

    @staticmethod
    def _disarm_mux_tail_cutoff(pending: dict[str, Any]) -> None:
        """U56 round 5: turn the mux fence into a pass-through, once, at release.

        DISARMED, not removed. The probes live in ``old_tail_drop_probes``, which
        every abort and retirement path already drains, so leaving the removal to
        that owner removes each probe exactly once. Removing them here as well is
        what produced the round-5 ``pad has no probe with id`` warning: the
        release filtered ``old_tail_drop_probes`` by value, and two wrappers for
        the same GStreamer pad are not equal. A fence that is disarmed cannot be
        removed twice, and a probe that is never removed cannot outlive its
        transaction.

        The bound is only meaningful while the RETIRING leg feeds the mux: from
        the switch onward the mux sees the INCOMING leg, and the bound reads the
        retiring leg's own end. Nothing that arrived before the release is
        re-decided by disarming -- the decision was taken as the buffer crossed.

        Round 6: this is now the SECOND of two release paths. The first is
        ``_mux_tail_segment_probe``, which fires the instant the changeover
        segment crosses the pad -- the boundary between the two legs, measured,
        not inferred. This one remains for the order where the retiring tail is
        still in flight and reaches its own measured end first (the clean case),
        and as the deadline behind both.
        """
        if not (pending.get("mux_tail_cutoff_probes") or ()):
            return
        pending["mux_tail_released"] = True

    @staticmethod
    def _mux_tail_arrived(pending: dict[str, Any]) -> bool:
        """U56 round 5: has the RETIRING audio's tail finished arriving at the mux?

        The drain wait (``_defer_rebase_drain``) polls the mux sink pad's
        ``current-level-buffers``, and a fence makes that level fall to zero
        EARLY: the fence eats the arrivals, so an empty pad stops meaning "the
        retiring leg has been consumed" and starts meaning "the fence is keeping
        up". MEASURED on the fix bytes: level hit zero 0.156 s after the arm with
        two of the retiring leg's buffers still inside the 1.0 s non-leaky
        isolation queue; they reached the pad as the fence came off, aired, and
        stepped the emitted audio backwards by 1.072 s.

        The honest condition is this one, in two halves. Every fenced pad has
        SEEN an arrival whose computed end reaches the retiring leg's own
        measured end (so nothing of the retiring leg is still on its way), AND
        every fenced pad has been silent for a drain-poll interval (so the
        arrival that satisfies the first half is not still crossing the pad when
        the fence comes off). The first half alone released on that arrival --
        measured, both escapes carried the release instant's own timestamp.
        """
        targets = pending.get("mux_tail_target") or {}
        if not targets:
            return True
        seen = pending.get("mux_tail_seen") or {}
        reach = _MUX_TAIL_ARRIVED_EPSILON_NS
        if not all(
            seen.get(pad_name, -1) >= int(target) - reach for pad_name, target in targets.items()
        ):
            return False
        last = pending.get("mux_last_arrival_t")
        return not (last is not None and time.monotonic() - last < _MUX_TAIL_QUIET_S)

    def _defer_mux_tail_release(self, pending: dict[str, Any]) -> None:
        """U56 round 5: hold the mux fence past the first mutation, bounded.

        Called where the release used to happen -- immediately after the first
        selector mutation. Holding past the mutation is safe by measurement: the
        incoming leg's buffers carry the SAME pts the outgoing leg's did, and the
        fence reads them under the RETIRING leg's segment, so it computes their
        running time as ~0.000 and keeps them. It is only the retiring leg's
        queued tail -- running times past the bound -- that it drops.

        Bounded by ``_MUX_TAIL_DEADLINE_S`` so a tail that never completes cannot
        hold the fence open; the deadline is far shorter than the time it would
        take the incoming leg's own timeline to reach the bound.
        """
        if not (pending.get("mux_tail_cutoff_probes") or ()):
            return
        pending["mux_tail_release_t"] = time.monotonic()
        if self._mux_tail_arrived(pending):
            self._disarm_mux_tail_cutoff(pending)
            return
        print(
            f"CTRL reload diagnostic: stage=mux-tail-wait "
            f"seen={pending.get('mux_tail_seen')} "
            f"target={pending.get('mux_tail_target')} "
            f"reload_id={pending.get('txn_id')}",
            file=sys.stderr,
            flush=True,
        )
        GLib.timeout_add(_REBASE_DRAIN_POLL_MS, self._poll_mux_tail_release, pending)

    def _poll_mux_tail_release(self, pending: dict[str, Any]) -> bool:
        """U56 round 5: the bounded wait for the retiring audio tail to arrive."""
        waited = time.monotonic() - pending.get("mux_tail_release_t", time.monotonic())
        arrived = self._mux_tail_arrived(pending)
        if arrived or waited >= _MUX_TAIL_DEADLINE_S:
            print(
                f"CTRL reload diagnostic: stage=mux-tail-released waited={waited:.3f}s "
                f"arrived={arrived} seen={pending.get('mux_tail_seen')} "
                f"reload_id={pending.get('txn_id')}",
                file=sys.stderr,
                flush=True,
            )
            self._disarm_mux_tail_cutoff(pending)
            return False
        return True

    # -- U37: drain the affected mux sink pads, and observe their arrivals -------
    #
    # A deferred program->program switch rebases the incoming leg by giving its
    # sticky SEGMENT a new base (``set_offset`` on the leg's own tail src pads).
    # A mux sink pad receives that segment as an ordinary event and
    # ``gst_aggregator_default_sink_event`` copies it into ``agg_pad->segment``
    # the moment it arrives -- but the buffers already queued on that pad are
    # clipped later, at POP time, under whatever segment is current THEN. So ONE
    # outgoing-leg buffer left in the video pad's queue when the rebased segment
    # lands is re-dated into the new base's timeline: its running time becomes
    # its OWN running time plus this transaction's rebase offset.
    #
    # Measured off-live on the real LPM media, in the recorded mux output
    # (2026-09-25): the stale frame's running time 9.411322222s AIRED at
    # 19.992322222s -- 9.411322222 + the 10.581s switch running time this
    # transaction reports as ``rebase-reference`` -- and the next video frame
    # stepped BACKWARDS to 10.581322222s. That 9.411s PTS regression, at emission
    # #297, is the src-rate collapse this slice was opened for. The same
    # capture's audio was continuous throughout.
    #
    # The buffer that must not be re-dated is in exactly one place that matters:
    # QUEUED on the pad when the switch begins. No probe can see it (probes fire
    # on ARRIVAL), so the commit does not switch anything until the pad has GIVEN
    # IT UP (``_rebase_drain_held``). Lossless: those buffers are ordinary
    # outgoing-leg media and they leave the pad by being CONSUMED, not dropped.
    #
    # What ARRIVES during that wait needs no guard at all, which is the one thing
    # this fix's first draft got wrong. Serialized on this pad, an arrival that
    # precedes the rebased SEGMENT is still clipped under the OLD segment and
    # therefore airs with its own correct running time -- it is the outgoing
    # leg's legitimate tail. Dropping it is pure loss, measured (2026-09-25) by
    # running the same real reload with a DROP-until-SEGMENT fence instead:
    # ``stage=rebase-fence-disarmed pad=sink_66 stream=audio dropped=48`` (that
    # build's wording) plus a
    # 1.024s hole in the aired audio (1013 frames against the 1061 every
    # non-dropping run airs), with that run's video set a strict subset of the
    # non-dropping one's (653 frames against 659) and nothing anywhere that the
    # non-dropping run did not also air. The probe is therefore an OBSERVER: it
    # counts what a stream put on the pad before its rebased segment crossed,
    # removes itself on that segment, and changes nothing.
    #
    # Both bounds (``_REBASE_DRAIN_DEADLINE_S`` / ``_REBASE_OBSERVER_DEADLINE_S``)
    # report themselves when reached, because a guard whose premise has failed
    # must say so rather than hold a stream off air. Neither touches the handover
    # order the U30/U34 tests pin: they run strictly BEFORE the first selector
    # mutation.

    def _rebase_affected_mux_pads(self, pending: dict[str, Any]) -> list[tuple[str, Any, str]]:
        """``(stream, pad, pad_name)`` for each mux SINK pad this switch replaces.

        The stream list comes from the transaction's OWN pads -- ``new_video_pad``
        / ``new_audio_pad`` -- not from ``_required_stream_labels()``: a stream the
        new leg does not carry is not switched, and waiting on its mux pad would
        hold a stream off air for a rebase it is not part of.

        Silent when no mux pad is registered (unit fakes; a graph with no mux):
        there is nothing to guard. A switched stream with NO pad resolving to its
        label is a WARN -- the guard cannot see a stream it is meant to protect.
        """
        streams: list[str] = []
        if pending.get("new_video_pad") is not None:
            streams.append("video")
        if pending.get("new_audio_pad") is not None:
            streams.append("audio")
        pads = getattr(self, "_mux_input_pads", None) or {}
        if not streams or not pads:
            return []
        affected: list[tuple[str, Any, str]] = []
        for stream in streams:
            match = next(
                (
                    (pad_name, pad)
                    for pad_name, pad in sorted(pads.items())
                    if self._mux_pad_stream_label(pad) == stream
                ),
                None,
            )
            if match is None:
                print(
                    f"WARN: no mux sink pad resolves to stream {stream} for reload "
                    f"{pending.get('txn_id')}; the rebase drain cannot guard a pad it "
                    "cannot find",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            affected.append((stream, match[1], match[0]))
        return affected

    def _rebase_drain_deadline_s(self) -> float:
        """Keep the mux-pad confirmation bound beyond declared output audio hold.

        Per-output normalization buffers audio before the mux. Its graph metadata
        is a real processing latency, while queue capacity is not; extend the
        existing measured mux-drain window by that latency and still require the
        mux-pad drain observation before selector handoff.
        """
        latency_ns = max(0, int(getattr(self, "output_audio_latency_ns", 0) or 0))
        return float(_REBASE_DRAIN_DEADLINE_S + latency_ns / Gst.SECOND)

    def _arm_rebase_segment_observers(self, pending: dict[str, Any]) -> bool:
        """Watch this switch's pre-rebase arrivals at the affected mux sink pads.

        See the U37 block comment above for the mechanism. These probes COUNT what
        a pad put on the mux before its rebased segment crossed and then remove
        themselves on that segment -- they change nothing about what airs. That is
        deliberate: an arrival that precedes the rebased segment is still clipped
        under the OLD segment and airs with its own correct running time, so
        dropping it would discard legitimate outgoing tail (measured: a 1.024 s
        audio hole). The load-bearing guard is the drain wait in
        ``_defer_rebase_drain``; these probes are the evidence that says whether
        the segment crossed inside its bound.

        The deadline is armed FIRST, and the probes only if it could be armed: an
        observer no timer can remove would outlive the transaction it describes,
        so an unavailable timer degrades to today's un-observed behaviour with a
        WARN instead.

        Returns True when this switch has pads worth waiting for. Note that a pad
        whose probe could not be installed still joins the drain set: waiting for
        its queue to empty is the guard that actually protects it.
        """
        affected = self._rebase_affected_mux_pads(pending)
        if not affected:
            return False
        drain_deadline_s = self._rebase_drain_deadline_s()
        bound_ms = round((drain_deadline_s + _REBASE_OBSERVER_DEADLINE_S) * 1000)
        try:
            deadline_id = GLib.timeout_add(bound_ms, self._on_rebase_observer_deadline, pending)
        except Exception as exc:
            print(
                f"WARN: rebase arrival observer unavailable for reload {pending['txn_id']}: "
                f"no observer deadline timer ({exc!r}); the drain wait still guards the "
                "pads, but no arrival count will be reported",
                file=sys.stderr,
                flush=True,
            )
            return False
        pending["rebase_observer_deadline_id"] = deadline_id
        pending["rebase_observer_pads"] = list(affected)
        pending["rebase_observer_arrived"] = {}
        armed: list[tuple[Any, Any, str]] = []
        armed_names: list[str] = []
        for stream, pad, pad_name in affected:
            try:
                probe_id = pad.add_probe(
                    Gst.PadProbeType.BUFFER
                    | Gst.PadProbeType.BUFFER_LIST
                    | Gst.PadProbeType.EVENT_DOWNSTREAM,
                    self._make_rebase_segment_observer(pending, pad_name, stream),
                )
            except Exception as exc:
                print(
                    f"WARN: rebase arrival observer not installed on mux pad {pad_name} "
                    f"(stream={stream}) for reload {pending['txn_id']}: {exc!r}; the drain "
                    f"wait still guards this pad, but its arrivals go uncounted",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            armed.append((pad, probe_id, pad_name))
            armed_names.append(pad_name)
        pending["rebase_observer_probes"] = armed
        print(
            f"CTRL reload diagnostic: stage=rebase-observer-armed "
            f"pads={','.join(pad_name for _s, _p, pad_name in affected)} "
            f"observed={','.join(armed_names) if armed_names else 'none'} "
            f"reload_id={pending['txn_id']}",
            file=sys.stderr,
            flush=True,
        )
        return True

    def _make_rebase_segment_observer(
        self, pending: dict[str, Any], pad_name: str, stream: str
    ) -> Any:
        """A per-pad observer of the rebase arrival window (see the armer).

        Keyed by PAD NAME in the arrival tally for the same reason the counters
        are: one recorder per pad, written from that pad's own streaming thread."""

        def _observe_rebase_arrivals(_pad: Any, info: Any) -> Any:
            try:
                if info.type & (Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST):
                    # U37 H1: counted, NOT dropped. An arrival that precedes the
                    # rebased segment on this pad is still clipped under the OLD
                    # segment and therefore airs with its own correct running
                    # time; dropping it discards legitimate outgoing tail.
                    arrived = pending.setdefault("rebase_observer_arrived", {})
                    arrived[pad_name] = arrived.get(pad_name, 0) + 1
                    return Gst.PadProbeReturn.OK
                event = info.get_event()
                if event is not None and event.type == Gst.EventType.SEGMENT:
                    # The rebased leg's own segment: every buffer after it is
                    # dated by the new base, so the window this observer watches
                    # has closed. REMOVE (not DROP) -- the segment itself must
                    # reach the mux.
                    pending.setdefault("rebase_observer_removed", set()).add(pad_name)
                    with contextlib.suppress(Exception):
                        print(
                            f"CTRL reload diagnostic: stage=rebase-observer-disarmed "
                            f"pad={pad_name} stream={stream} "
                            f"arrived={(pending.get('rebase_observer_arrived') or {}).get(pad_name, 0)} "
                            f"reload_id={pending.get('txn_id')}",
                            file=sys.stderr,
                            flush=True,
                        )
                    return Gst.PadProbeReturn.REMOVE
            except Exception:
                # A probe callback that raises is a per-buffer stderr flood on the
                # streaming thread. This observer's failure mode must be "stops
                # counting", never "takes the mux's streaming thread with it".
                return Gst.PadProbeReturn.OK
            return Gst.PadProbeReturn.OK

        return _observe_rebase_arrivals

    def _rebase_drain_held(self, pending: dict[str, Any]) -> list[str]:
        """``pad=level`` for each affected mux sink pad STILL holding a buffer.

        ``current-level-buffers`` is the aggregator pad's own queue depth -- a
        readable ``GParamUInt64`` on the installed runtime, measured on a live pad
        -- and it is the right predicate rather than a proxy: the aggregator
        decrements it as it POPS a buffer, clipped buffer included, so zero means
        this pad has handed over everything it had taken before the rebase.

        A level that cannot be read is reported as ``unknown`` and is therefore
        NOT proven empty; the caller waits and its deadline decides. Treating an
        unreadable property as empty would silently reinstate the very re-dating
        this guard exists to remove."""
        held: list[str] = []
        for _stream, pad, pad_name in pending.get("rebase_observer_pads") or ():
            level: int | None = None
            with contextlib.suppress(Exception):
                level = int(pad.get_property("current-level-buffers"))
            if level is None:
                held.append(f"{pad_name}=unknown")
            elif level > 0:
                held.append(f"{pad_name}={level}")
        return held

    def _defer_rebase_drain(self, pending: dict[str, Any], held: list[str]) -> bool:
        """Hand the commit to a main-loop poller until the pads empty (bounded).

        Returns True when the poller took over -- the caller must return WITHOUT
        switching -- and False when no poller could be scheduled, in which case
        the caller proceeds now (today's behaviour) rather than waiting forever.

        Asynchronous on purpose: a blocking wait here would take the main loop
        with it -- no stall watchdog, no commit watchdog, no control-plane reader,
        on a channel that is otherwise still on air."""
        pending["rebase_drain_started_t"] = time.monotonic()
        try:
            timeout_id = GLib.timeout_add(_REBASE_DRAIN_POLL_MS, self._resume_rebase_drain, pending)
        except Exception as exc:
            print(
                f"WARN: rebase drain could not be deferred for reload {pending['txn_id']} "
                f"({exc!r}); switching with {', '.join(held)} still queued at the mux",
                file=sys.stderr,
                flush=True,
            )
            return False
        pending["rebase_drain_timeout_id"] = timeout_id
        print(
            f"CTRL reload diagnostic: stage=rebase-drain-wait pads={','.join(held)} "
            f"reload_id={pending['txn_id']}",
            file=sys.stderr,
            flush=True,
        )
        return True

    def _resume_rebase_drain(self, pending: dict[str, Any]) -> bool:
        """Main-loop tick: switch once the pads have drained, or at the deadline."""
        pending["rebase_drain_timeout_id"] = None
        if (
            self._pending_reload is not pending
            or self._stopping
            or not pending.get("commit_in_progress", False)
            or pending.get("selector_handoff_started", False)
        ):
            # The transaction died, or the channel is stopping, between the last
            # tick and this one: release and let whoever ended it own the rest.
            self._release_rebase_observers(pending)
            return False
        held = self._rebase_drain_held(pending)
        started = pending.get("rebase_drain_started_t")
        waited = 0.0 if started is None else time.monotonic() - started
        deadline_s = self._rebase_drain_deadline_s()
        if held and waited < deadline_s:
            return True  # keep polling
        if held:
            print(
                f"WARN: rebase drain did not empty within {deadline_s:.1f}s "
                f"for reload {pending['txn_id']} ({', '.join(held)} still queued at the mux); "
                "switching anyway -- a stale buffer may still be re-dated by the segment",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                f"CTRL reload diagnostic: stage=rebase-drain-drained waited={waited:.3f}s "
                f"reload_id={pending['txn_id']}",
                file=sys.stderr,
                flush=True,
            )
        try:
            self._continue_reload_commit(pending)
        except Exception as exc:
            self._handle_commit_failure(pending, exc)
        return False

    def _on_rebase_observer_deadline(self, pending: dict[str, Any]) -> bool:
        """Main-loop tick: the observer window closed without its segment -- report it."""
        pending["rebase_observer_deadline_id"] = None
        if not pending.get("rebase_observer_probes"):
            return False
        arrived = pending.get("rebase_observer_arrived") or {}
        pairs = ", ".join(
            f"{stream}:{pad_name}={arrived.get(pad_name, 0)}"
            for stream, _pad, pad_name in pending.get("rebase_observer_pads") or ()
        )
        print(
            f"WARN: rebase arrival observer window closed after "
            f"{self._rebase_drain_deadline_s() + _REBASE_OBSERVER_DEADLINE_S:.1f}s for reload "
            f"{pending.get('txn_id')}: no rebased segment crossed the mux sink pads "
            f"({pairs or 'no pads'}); removing the observers -- nothing is held off air",
            file=sys.stderr,
            flush=True,
        )
        self._release_rebase_observers(pending)
        return False

    def _release_rebase_observers(self, pending: dict[str, Any]) -> None:
        """Remove the U37 observers and their timers; idempotent, best-effort."""
        deadline_id = pending.get("rebase_observer_deadline_id")
        if deadline_id is not None:
            pending["rebase_observer_deadline_id"] = None
            with contextlib.suppress(Exception):
                GLib.source_remove(deadline_id)
        drain_id = pending.get("rebase_drain_timeout_id")
        if drain_id is not None:
            pending["rebase_drain_timeout_id"] = None
            with contextlib.suppress(Exception):
                GLib.source_remove(drain_id)
        # A probe that already saw the rebased segment removed ITSELF (REMOVE);
        # removing that id again is a GStreamer-WARNING on the stderr of every
        # clean switch. The pad name it reported is the record of that.
        removed = pending.get("rebase_observer_removed") or set()
        for pad, probe_id, pad_name in pending.get("rebase_observer_probes") or ():
            if pad_name in removed:
                continue
            with contextlib.suppress(Exception):
                pad.remove_probe(probe_id)
        pending["rebase_observer_probes"] = []

    def _dispose_confirmed_old_leg(self, pending: dict[str, Any]) -> tuple[bool, str | None]:
        """Fence, detach, and retire old tails after confirmed selector handoff.

        No blocking probe is used. Both selectors have reported the replacement
        pad active and both replacement holds are already released, so the old
        tails are off the shared output path. Nonblocking DROP fences contain any
        late old-leg buffer/event before each request pad is released. GStreamer
        1.28's request-pad release owns deactivation and unlinking, matching the
        selector's ownership contract. No synchronous flush event enters the
        selector and no coupled A/V IDLE barrier can form.
        """
        warnings: list[str] = []
        failures: list[str] = []
        tails: list[tuple[str, Gst.Element, Gst.Pad]] = []
        for stream, selector, selector_pad in (
            ("video", self.selector, pending["old_video_pad"]),
            ("audio", self.audio_selector, pending["old_audio_pad"]),
        ):
            if selector is None or selector_pad is None:
                continue
            peer = selector_pad.get_peer()
            if peer is None:
                warnings.append(f"selector-peer-missing:{stream}")
                tails.append((stream, selector, selector_pad))
                continue
            try:
                drop_probe_id = peer.add_probe(
                    Gst.PadProbeType.BUFFER
                    | Gst.PadProbeType.BUFFER_LIST
                    | Gst.PadProbeType.EVENT_DOWNSTREAM,
                    _drop_everything_probe,
                )
                pending["old_tail_drop_probes"].append((peer, drop_probe_id))
                tails.append((stream, selector, selector_pad))
            except Exception as exc:
                failures.append(f"old-tail-fence-error:{stream}:{exc!r}")
        if failures:
            reason = "; ".join(failures)
            print(f"WARN: old-tail containment did not arm: {reason}", flush=True)
            return False, reason
        # The 18:48:11 wedge dump put the retirement thread inside
        # ``release_request_pad``, but the last stage line on stderr was
        # ``stage=selector-handoff-confirmed`` -- one step before this loop, so
        # the log could not say whether the loop was even entered. Entry names
        # the streams about to be released; each stream then prints *after* its
        # own release call returns, so a stall leaves that stream's line missing
        # and identifies the stalled stream by elimination.
        print(
            "CTRL reload diagnostic: stage=retiring-tail-release-entered streams="
            f"{','.join(stream for stream, _selector, _pad in tails) or '-'}",
            file=sys.stderr,
            flush=True,
        )
        for stream, selector, selector_pad in tails:
            try:
                selector.release_request_pad(selector_pad)
            except Exception as exc:
                failures.append(f"selector-retirement-error:{stream}:{exc!r}")
                continue
            print(
                f"CTRL reload diagnostic: stage=retiring-tail-released stream={stream}",
                file=sys.stderr,
                flush=True,
            )
        print("CTRL reload diagnostic: stage=old-tail-detached", file=sys.stderr, flush=True)
        for index, element in enumerate(pending["old_elements"]):
            self._null_retiring_element(element, index + 1, failures)
        for index, element in enumerate(pending["old_elements"]):
            try:
                if self.pipeline.remove(element) is False:
                    warnings.append(f"element-remove-failed:{index + 1}")
            except Exception as exc:
                warnings.append(f"element-remove-error:{index + 1}:{exc!r}")
        if not failures:
            # The old producers are now NULL, so their DROP fences have finished
            # their job and can release their pad references. Keep them installed
            # only when a producer failed to stop and could still emit late data.
            self._release_old_tail_drop_fences(pending)
        if warnings:
            print(f"WARN: leg disposal incomplete: {'; '.join(warnings)}", flush=True)
        if failures:
            reason = "; ".join(failures)
            print(f"WARN: leg disposal did not reach NULL: {reason}", flush=True)
            return False, reason
        return True, None

    def _belongs_to_pending_reload(self, src: object) -> bool:
        """True if ``src`` (a bus-message source) is one of the pending reload leg's
        elements or nested under one (e.g. a decodebin-internal decoder)."""
        pending = self._pending_reload
        if pending is None or src is None:
            return False
        new_elements = pending["new_elements"]
        node = src
        while node is not None:
            if node in new_elements:
                return True
            node = node.get_parent() if hasattr(node, "get_parent") else None
        return False

    def _belongs_to_retiring_reload(self, src: object) -> bool:
        """True when ``src`` belongs to the old leg currently being retired."""
        pending = self._pending_reload
        if pending is None or src is None:
            return False
        old_elements = pending.get("old_elements", ())
        node = src
        while node is not None:
            if node in old_elements:
                return True
            node = node.get_parent() if hasattr(node, "get_parent") else None
        return False

    def _belongs_to_aborted_leg(self, src: object) -> bool:
        """True when ``src`` is an element of a leg this engine has ALREADY aborted.

        Item 3 (education, 2026-09-25 14:51:22; ``gst-worker.stdout.log`` 2649-2650):
        ``_abort_pending_reload`` clears ``_pending_reload`` the moment it rejects
        the new leg, but the leg is not gone -- its disposal runs on a worker thread
        (``_start_aborted_leg_retirement``). Between those two instants the leg's
        elements are still live and still post to the bus, and BOTH predicates above
        return False for an element that provably IS the aborted leg's:
        ``_belongs_to_pending_reload`` early-outs on the cleared slot, and
        ``_belongs_to_retiring_reload`` needs a commit that an abort never had. So
        the second error from the aborted leg's own decodebin fell through every
        containment branch to the fatal tail, and the worker exited
        (``WORKER_RESULT {'error': (gerror=... decodebin14 ... all streams without
        buffers)}``, exit_code=1) on a channel that was still producing output.

        Containing it is what the leg's own state already promises: before the
        abort's holds are lifted it is DROP-fenced at its own src pads
        (``_detach_leg_from_selectors``), so nothing it emits can reach the
        selector, the mux, or the on-air output -- its errors are bookkeeping, not
        airtime. Attributing first by retained element identity covers children
        detached during teardown; the parent walk also covers descendants that
        remain nested under a recorded element.
        """
        if src is None:
            return False
        for record in self._abort_retire_legs:
            elements = record["elements"]
            node = src
            while node is not None:
                if node in elements:
                    return True
                node = node.get_parent() if hasattr(node, "get_parent") else None
        return False

    @staticmethod
    def _snapshot_leg_attribution_elements(elements: list[Gst.Element]) -> list[Gst.Element]:
        """Retain nested bin children so queued bus errors survive teardown detachment.

        GStreamer may deliver an ERROR after a child has been removed from its
        decodebin, at which point walking ``get_parent()`` no longer reaches the
        top-level leg element. Snapshot child identities before releasing an
        aborted leg's holds. Enumeration is best-effort because this runs on the
        abort safety path; top-level attribution remains available if a bin's
        iterator cannot be completed.
        """
        snapshot = list(elements)
        identities = {id(element) for element in snapshot}
        resync_result = getattr(Gst.IteratorResult, "RESYNC", None)
        for element in elements:
            iterate_recurse = getattr(element, "iterate_recurse", None)
            if not callable(iterate_recurse):
                continue
            try:
                iterator = iterate_recurse()
                resyncs = 0
                while True:
                    result, child = iterator.next()
                    if result == Gst.IteratorResult.OK:
                        if child is not None and id(child) not in identities:
                            identities.add(id(child))
                            snapshot.append(child)
                        continue
                    if resync_result is not None and result == resync_result and resyncs < 3:
                        resyncs += 1
                        iterator.resync()
                        continue
                    break
            except Exception as exc:
                print(
                    "WARN: could not snapshot nested elements for aborted reload "
                    f"error attribution: {exc!r}",
                    file=sys.stderr,
                    flush=True,
                )
        return snapshot

    def _element_count(self) -> int:
        """Count elements currently in the pipeline (for the reload-leak guard)."""
        iterator = self.pipeline.iterate_elements()
        count = 0
        while True:
            result, _element = iterator.next()
            if result != Gst.IteratorResult.OK:
                break
            count += 1
        return count

    def _dispose_source_leg(
        self,
        video_pad: Gst.Pad,
        audio_pad: Gst.Pad | None,
        elements: list[Gst.Element],
    ) -> tuple[bool, str | None]:
        """Retire an aborted, never-selected leg using the same isolation policy.

        The abort path fences its tails before releasing the first-buffer holds.
        Release the inactive selector requests before potentially blocking NULL,
        as on confirmed retirement. A connected-tail FLUSH_START and NULL-first
        teardown can interact with shared selector streaming locks while the next
        replacement is prerolling. No flush or state change enters the selected
        program; disposal still runs off the main loop and reports failure.
        """
        return self._dispose_confirmed_old_leg(
            {
                "old_video_pad": video_pad,
                "old_audio_pad": audio_pad,
                "old_elements": elements,
                "old_tail_drop_probes": [],
            }
        )

    def _null_retiring_element(self, element: Gst.Element, index: int, failures: list[str]) -> bool:
        """Bring ONE element of a retiring leg to NULL under the finding-3 policy.

        Returns True when the element genuinely reached NULL. Appends to
        ``failures`` (and returns False) only when it did not, after a bounded
        wait and one retry.

        ASYNC is the case the previous code got wrong. A ``set_state(NULL)`` is
        synchronous for a plain element, but these legs are BINS
        (``decodebin``/``uridecodebin`` and friends), and a bin whose children are
        still winding down legitimately returns ASYNC on a downward transition.
        Treating that as "incomplete cleanup" made an ordinary, correct retirement
        report failure -- which the commit path then escalated to a channel kill.
        Wait for it instead, bounded by ``_LEG_NULL_ASYNC_WAIT_S``."""
        for attempt in (1, 2):
            try:
                state_result = element.set_state(Gst.State.NULL)
            except Exception as exc:
                failures.append(f"element-null-error:{index}:{exc!r}")
                return False
            if state_result == Gst.StateChangeReturn.SUCCESS:
                return True
            if state_result == Gst.StateChangeReturn.ASYNC:
                try:
                    ret, state, _pending = element.get_state(
                        int(_LEG_NULL_ASYNC_WAIT_S * Gst.SECOND)
                    )
                except Exception as exc:
                    failures.append(f"element-null-getstate-error:{index}:{exc!r}")
                    return False
                if (
                    ret
                    in (
                        Gst.StateChangeReturn.SUCCESS,
                        Gst.StateChangeReturn.NO_PREROLL,
                    )
                    and state == Gst.State.NULL
                ):
                    return True
                detail = f"async-unsettled:{getattr(ret, 'value_nick', ret)}"
            else:
                detail = getattr(state_result, "value_nick", str(state_result))
            if attempt == 1:
                print(
                    f"WARN: retiring element {index} did not reach NULL ({detail}); retrying once",
                    flush=True,
                )
                continue
            failures.append(f"element-null-incomplete:{index}:{detail}")
        return False

    # -- content-reload (S15 BLOCKER fix): re-apply the graphics-overlay leg too --

    def _belongs_to_pending_overlay_swap(self, src: object) -> str | None:
        """The layer NAME whose still-settling swap ``src`` (a bus-message source)
        belongs to, or None. Mirrors ``_belongs_to_pending_reload``, one pending
        swap per layer name instead of a single pending reload."""
        if src is None:
            return None
        for layer_name, entry in self._pending_overlay_swaps.items():
            node = src
            while node is not None:
                if node in entry["new_elements"]:
                    return layer_name
                node = node.get_parent() if hasattr(node, "get_parent") else None
        return None

    def set_emergency_overlay(self, payload: dict[str, Any] | None) -> None:
        """Change only the reserved emergency layer; other station graphics remain."""
        if self._overlay_compositor is None:
            raise RuntimeError("Emergency presentation was not enabled when this channel started.")
        if self._emergency_scroll_source is not None:
            GLib.source_remove(self._emergency_scroll_source)
            self._emergency_scroll_source = None
        self._emergency_payload = payload
        if payload is None:
            self._remove_overlay_layer("emergency")
            return
        if payload["mode"] not in {"crawl", "overlay", "forced_slate"}:
            raise ValueError("Invalid emergency display mode")
        layer = GraphicsOverlayLayer(
            name="emergency",
            image_path=payload["image_path"],
            xpos=int(payload["canvas_width"]) if payload["mode"] == "crawl" else 0,
            ypos=int(payload["ypos"]),
            width=int(payload["width"]),
            height=int(payload["height"]),
        )
        self._swap_overlay_layer(layer)

    def _start_emergency_crawl(self) -> None:
        payload = self._emergency_payload
        if payload is None or payload["mode"] != "crawl":
            return
        started = time.monotonic()
        width = int(payload["width"])
        canvas_width = int(payload["canvas_width"])

        def step() -> bool:
            pad = self._overlay_layer_pads.get("emergency")
            if pad is None or self._emergency_payload is not payload:
                return False
            offset = int((time.monotonic() - started) * 90) % (canvas_width + width)
            pad.set_property("xpos", canvas_width - offset)
            return True

        self._emergency_scroll_source = GLib.timeout_add(40, step)

    def reload_graphics_overlay(self, new_leg: GraphicsOverlayLeg | None) -> None:
        """Re-apply the S15 graphics-overlay leg on a content-reload.

        BLOCKER fix (2026-08-30 audit): a content-reload used to rebuild ONLY the
        program source leg (``reload_program``) and silently drop
        ``new_graph.graphics_overlay`` — an operator's mid-broadcast lower-third
        text update never took effect until a full restart, even though the API +
        UI advertise a content-reload as the way to apply it (see
        ``civiccast.egress.router.update_graphics_overlay`` and
        ``bridge.graphics_overlay_leg_from_config``, which re-renders a fresh
        banner PNG on every call specifically so a reload can pick it up).

        For every layer NAME present in ``new_leg`` this builds a fresh image
        chain and swaps it in for that name's currently-live layer (or adds it, if
        the name is new) via ``_swap_overlay_layer`` — the exact first-buffer-probe
        pattern ``reload_program`` uses for the video source leg, so there is never
        a frame where the OLD and NEW image for the SAME layer are both composited
        (a visible double-exposure), and a build/preroll failure never disturbs the
        already-on-air overlay. A layer name no longer present in ``new_leg`` (the
        operator removed a layer, or turned the whole overlay off) is dropped via
        ``_remove_overlay_layer``. The lower-third's operative case — the SAME
        layer name (``"lower_third"``) with a freshly-rendered PNG at a new path —
        is exactly a swap-by-name.

        If the engine was built WITHOUT a graphics-overlay leg at all (no
        compositor exists in this pipeline's topology —
        ``self._overlay_compositor is None``), a reload cannot safely splice a
        D3D11 compositor into an already-running pipeline; this is logged and
        skipped, matching ``PlayoutGraph.graphics_overlay``'s documented
        "None preserves today's behavior" contract — a channel that wants to ADD an
        overlay where none existed needs a fresh start, not a reload."""
        if self._overlay_compositor is None:
            if new_leg is not None:
                print(
                    "WARN: content-reload carried a graphics-overlay update but this "
                    "channel's pipeline was built without an overlay compositor -- a "
                    "fresh start is required to add one; skipping.",
                    flush=True,
                )
            return
        new_layers_by_name = {
            layer.name: layer for layer in (new_leg.layers if new_leg is not None else ())
        }
        for name in list(self._overlay_layer_pads):
            if name == "emergency":
                continue
            if name not in new_layers_by_name:
                if name.startswith("board_live_") and self._pending_reload is not None:
                    self._pending_reload.setdefault("board_live_remove", []).append(name)
                    continue
                self._remove_overlay_layer(name)
        # A layer name can be PENDING (a not-yet-committed ADD, i.e. one never in
        # ``_overlay_layer_pads`` because this is its first-ever reload) without the
        # loop above ever seeing it -- close that gap here so a layer removed before
        # its own add commits doesn't orphan a settling swap forever.
        for name in list(self._pending_overlay_swaps):
            if name == "emergency":
                continue
            if name not in new_layers_by_name and name not in self._overlay_layer_pads:
                self._abort_pending_overlay_swap(name, reason="removed")
        for layer in new_layers_by_name.values():
            self._swap_overlay_layer(layer)

    def _swap_overlay_layer(self, layer: GraphicsOverlayLayer) -> None:
        """Build a fresh image chain for ``layer`` onto a new compositor pad, and
        commit it in place of that layer name's current pad on the new chain's
        first buffer. Mirrors ``reload_program``'s ENG-001/ENG-002/ENG-008 handling
        (bounded watchdog, probe-armed-before-preroll, build-error containment) —
        see that method's docstring for the rationale of each."""
        compositor = self._overlay_compositor
        if compositor is None:
            return  # defensive; reload_graphics_overlay already gates this
        if layer.name in self._pending_overlay_swaps:
            # Supersede a still-settling swap for this same layer name (never drop
            # a due overlay change) — the superseded chain is disposed first.
            print(
                f"CTRL graphics-overlay reload superseding a still-settling swap "
                f"for layer {layer.name!r}",
                flush=True,
            )
            self._abort_pending_overlay_swap(layer.name, reason="superseded")
        new_pad, new_elements = self._instantiate_overlay_layer(layer, compositor)
        if layer.name == "emergency":
            # A decoded still starts at PTS zero. Align a newly admitted alert
            # with this running channel, otherwise the compositor drops it as late.
            clock = self.pipeline.get_clock()
            if clock is not None:
                new_pad.set_offset(max(0, clock.get_time() - self.pipeline.get_base_time()))
        entry: dict[str, Any] = {
            "layer": layer,
            "new_pad": new_pad,
            "new_elements": new_elements,
            "probe_id": None,
            "timeout_id": None,
        }
        self._pending_overlay_swaps[layer.name] = entry
        try:
            # ENG-002 (mirrored): arm the first-buffer probe BEFORE PLAYING so the
            # genuine first buffer can never slip past an unarmed probe.
            entry["probe_id"] = new_pad.add_probe(
                Gst.PadProbeType.BUFFER,
                lambda _pad, _info, name=layer.name: self._on_overlay_first_buffer(name),
            )
            for element in new_elements:
                element.sync_state_with_parent()  # preroll the new layer chain
        except Exception:  # ENG-008 (mirrored): a preroll/arm failure must not wedge
            self._abort_pending_overlay_swap(layer.name, reason="build-error")
            raise
        # ENG-001 (mirrored): bound the wait for the new layer's first buffer. If it
        # never arrives, abort rather than pin the pending swap forever (the current
        # overlay for this layer name keeps showing).
        entry["timeout_id"] = GLib.timeout_add_seconds(
            max(1, int(self.reload_timeout_s)),
            lambda name=layer.name: self._on_overlay_swap_timeout(name),
        )

    def _on_overlay_first_buffer(self, layer_name: str) -> Gst.PadProbeReturn:
        # Streaming thread: hand the commit to the main loop (no state changes here).
        GLib.idle_add(self._commit_overlay_swap, layer_name)
        return Gst.PadProbeReturn.REMOVE

    def _commit_overlay_swap(self, layer_name: str) -> bool:
        """Main-loop commit of one layer's swap: repoint ``self._overlay_layer_pads``/
        ``self._overlay_layer_elements`` at the prerolled new chain, then dispose the
        old one. A no-op if the swap was aborted/superseded before this fired.
        ``return False`` so the GLib idle source runs once."""
        entry = self._pending_overlay_swaps.get(layer_name)
        if entry is None:
            return False  # aborted or superseded before the first buffer landed
        if entry["timeout_id"] is not None:
            GLib.source_remove(entry["timeout_id"])  # committing — cancel the watchdog
        old_pad = self._overlay_layer_pads.get(layer_name)
        old_elements = self._overlay_layer_elements.get(layer_name, [])
        old_image_path = self._overlay_layer_image_paths.get(layer_name)
        self._overlay_layer_pads[layer_name] = entry["new_pad"]
        self._overlay_layer_elements[layer_name] = entry["new_elements"]
        self._overlay_layer_image_paths[layer_name] = entry["layer"].image_path
        del self._pending_overlay_swaps[layer_name]
        if layer_name == "emergency":
            self._start_emergency_crawl()
        if old_pad is not None:
            # R3: the swap point -- the NEW layer's first buffer has just landed,
            # so the OLD chain (about to be disposed below) is provably off-air.
            # Delete its banner PNG only once ``_dispose_overlay_layer_pad`` has
            # unlinked/NULL'd/removed every element reading it (best-effort: never
            # raises, matches that method's disposal-hiccup contract).
            self._dispose_overlay_layer_pad(old_pad, old_elements)
            self._delete_stale_overlay_png(old_image_path)
        print(
            f"CTRL graphics-overlay layer {layer_name!r} reload committed "
            f"(elements={self._element_count()})",
            flush=True,
        )
        return False

    def _on_overlay_swap_timeout(self, layer_name: str) -> bool:
        """Watchdog: the new layer chain never produced a first buffer in time.
        Abort so the channel isn't wedged on a never-committing overlay swap; the
        current overlay for this layer name keeps showing and the next due change
        can retry."""
        if layer_name not in self._pending_overlay_swaps:
            return False  # already committed/aborted
        print(
            f"CTRL graphics-overlay reload for layer {layer_name!r} aborted: new layer "
            f"produced no buffer within {max(1, int(self.reload_timeout_s))}s; "
            "keeping current overlay",
            flush=True,
        )
        self._abort_pending_overlay_swap(layer_name, reason="timeout")
        return False  # one-shot

    def _abort_pending_overlay_swap(self, layer_name: str, *, reason: str) -> None:
        """Tear down the in-flight (uncommitted) swap for ``layer_name`` and clear
        its pending slot. The currently-shown layer for that name is untouched.
        Used by supersede, build-error, watchdog timeout, and async-error
        containment."""
        entry = self._pending_overlay_swaps.pop(layer_name, None)
        if entry is None:
            return
        if entry["probe_id"] is not None:
            with contextlib.suppress(Exception):  # probe may already have auto-removed
                entry["new_pad"].remove_probe(entry["probe_id"])
        if entry["timeout_id"] is not None and reason != "timeout":
            # 'timeout' means the watchdog source is firing now (auto-removed on return).
            with contextlib.suppress(Exception):
                GLib.source_remove(entry["timeout_id"])
        self._dispose_overlay_layer_pad(entry["new_pad"], entry["new_elements"])
        # R3: this chain never went on-air (superseded/build-error/timeout/removed
        # before its first buffer committed) -- its banner PNG is an orphan the
        # moment its elements are disposed above; delete it now rather than
        # leaking it until the next start()'s sweep.
        self._delete_stale_overlay_png(entry["layer"].image_path)

    def _remove_overlay_layer(self, layer_name: str) -> None:
        """Drop a layer no longer present in a reloaded graphics-overlay leg (the
        operator removed a layer, or turned the overlay off) — disposes its
        compositor pad + elements the same way ``_dispose_source_leg`` retires a
        source leg. A layer with a swap still settling is left alone (its own
        commit/abort path disposes whichever chain loses)."""
        if layer_name in self._pending_overlay_swaps:
            self._abort_pending_overlay_swap(layer_name, reason="removed")
        pad = self._overlay_layer_pads.pop(layer_name, None)
        elements = self._overlay_layer_elements.pop(layer_name, [])
        image_path = self._overlay_layer_image_paths.pop(layer_name, None)
        if pad is not None:
            self._dispose_overlay_layer_pad(pad, elements)
            self._delete_stale_overlay_png(image_path)

    def _dispose_overlay_layer_pad(self, pad: Gst.Pad, elements: list[Gst.Element]) -> None:
        """Tear down one now-inactive overlay layer chain: unlink from the
        compositor, release its request pad, then NULL + remove its elements.
        Best-effort — mirrors ``_dispose_source_leg``: a disposal hiccup is logged,
        never raised, so it can never take a live channel off air."""
        try:
            for element in elements:
                element.set_state(Gst.State.NULL)
            compositor = self._overlay_compositor
            if compositor is not None:
                peer = pad.get_peer()
                if peer is not None:
                    peer.unlink(pad)
                compositor.release_request_pad(pad)
            for element in elements:
                self.pipeline.remove(element)
        except Exception as exc:
            print(f"WARN: graphics-overlay layer disposal incomplete: {exc!r}", flush=True)

    def _delete_stale_overlay_png(self, image_path: str | None) -> None:
        """Delete an overlay layer's rendered banner PNG once its GStreamer chain
        is fully disposed (called only after ``_dispose_overlay_layer_pad`` has
        unlinked/NULL'd/removed every element that had it open).

        Restricted to filenames matching ``_STALE_BANNER_PNG_RE`` -- the exact
        per-call unique pattern ``bridge.graphics_overlay_leg_from_config``
        renders -- so this can never delete an operator-configured, persistent
        image (e.g. a future station-bug/logo layer's ``image_path``), only a
        file this module's own reload/swap path generated. Best-effort: a file
        still locked by a lingering process (Windows) fails to unlink and is
        logged, never raised -- disposal must never take a live channel off air
        (R3, 2026-08-31: round-1's per-uuid banner filename fix left nothing to
        delete the old ones, so a 24/7 station accumulated one PNG per
        start()/content-reload forever on the same volume as recordings/HLS/DB)."""
        if not image_path:
            return
        path = Path(image_path)
        if not _STALE_BANNER_PNG_RE.match(path.name):
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            print(
                f"WARN: failed to delete stale graphics-overlay banner PNG {path}: {exc!r}",
                flush=True,
            )

    def stop(self, *, force_exit_on_hang: bool = False) -> bool:
        """Time-bounded teardown, including any reload-retirement thread.

        Returns True iff that thread ended and the pipeline reached NULL within one
        shared ``teardown_timeout_s`` budget. Both ``set_state(NULL)`` and its
        confirming ``get_state`` run on a daemon teardown thread: native evidence
        proves the state-change call itself can block in a streaming task, so merely
        putting a timeout on the later ``get_state`` cannot bound this method.
        With ``force_exit_on_hang``
        (worker-process model), an incomplete transition triggers ``os._exit(70)``
        (nonzero = forced kill, so the supervisor doesn't read it as a clean exit)
        so the process can never hang on stuck live-source streaming threads.

        Item 88 Round-2 review BLOCKER: ``RollingWavSegmentWriter.close()`` is
        itself bounded now (a wedged disk cannot make IT hang forever either --
        see ``audio_tap.py``'s own docstring), so calling it here, BEFORE the
        ``force_exit_on_hang`` escape below, can no longer defeat this method's
        own bounded-teardown contract the way an earlier, unconditionally-
        blocking ``close()`` could have."""
        self._stopping = True
        scroll_source = getattr(self, "_emergency_scroll_source", None)
        if scroll_source is not None:
            with contextlib.suppress(Exception):
                GLib.source_remove(scroll_source)
            self._emergency_scroll_source = None
        deadline = time.monotonic() + self.teardown_timeout_s

        # Stop admissions before removing the downstream probe: a late probe
        # cannot reopen the terminal gate. The callback itself holds only gate,
        # not engine state or a lock across any GStreamer call.
        gate = getattr(self, "_caption_gap_gate", None)
        if gate is not None:
            gate.close()
        heartbeat_id = getattr(self, "_caption_gap_heartbeat_id", None)
        if heartbeat_id is not None:
            with contextlib.suppress(Exception):
                GLib.source_remove(heartbeat_id)
            self._caption_gap_heartbeat_id = None
        probe = getattr(self, "_caption_gap_probe", None)
        if probe is not None:
            with contextlib.suppress(Exception):
                probe[0].remove_probe(probe[1])
            self._caption_gap_probe = None

        pending = self._pending_reload
        if pending is not None:
            self._release_selector_notify_handlers(pending)
        if pending is not None and (
            not pending.get("commit_in_progress", False)
            or not pending.get("selector_handoff_confirmed", False)
        ):
            # A first-ended stream may have a producer pacing tail buffers while
            # the other stream is still active. Stop and wake it before clearing
            # the transaction so it cannot remain in its frontier wait after the
            # pipeline has gone NULL.
            pending.get("tail_padding_stop", threading.Event()).set()
            pending.get("tail_padding_wake", threading.Event()).set()
            tail_start_id = pending.get("tail_padding_start_id")
            if tail_start_id is not None:
                with contextlib.suppress(Exception):
                    GLib.source_remove(tail_start_id)
                pending["tail_padding_start_id"] = None
            # Do not synchronously dispose a still-prerolling leg here. Release its
            # probes and let the whole-pipeline NULL below own teardown under the
            # same bound.
            if pending.get("commit_in_progress", False):
                # Selector notification may still be queued. Make the transaction
                # terminal before pipeline teardown so that callback cannot start
                # old-leg retirement during stop.
                pending["retirement_cancelled"] = True
                start_event = pending.get("retirement_start_event")
                if start_event is not None:
                    start_event.set()
                pending["commit_in_progress"] = False
                pending["handoff_started"] = False
                self._release_old_tail_drop_fences(pending)
                self._finish_commit_watchdog(pending)
            self._pending_reload = None
            if pending["probe_id"] is not None:
                with contextlib.suppress(Exception):
                    pending["new_video_pad"].remove_probe(pending["probe_id"])
            for pad, probe_id in pending.get("readiness_probes", ()):
                with contextlib.suppress(Exception):
                    pad.remove_probe(probe_id)
            for pad, probe_id in pending["boundary_probes"]:
                with contextlib.suppress(Exception):
                    pad.remove_probe(probe_id)
            self._release_hold_probes(pending)
            # U37: an observer and its deadline timer must never outlive the
            # transaction they belong to -- the timer holds a reference to
            # ``pending`` and would fire against a torn-down pipeline.
            self._release_rebase_observers(pending)
            for timeout_key in ("timeout_id", "defer_timeout_id"):
                if pending[timeout_key] is not None:
                    with contextlib.suppress(Exception):
                        GLib.source_remove(pending[timeout_key])
            self._notify_reload_settled(pending["on_settled"], False, "stopped")
        elif pending is not None:
            # A selected replacement cannot be aborted in place, but the caller
            # must receive a terminal result even if retirement stays blocked until
            # forced process recovery. Clear the stored callback to make a later
            # finalizer idempotent.
            callback = pending["on_settled"]
            pending["on_settled"] = None
            self._notify_reload_settled(callback, False, "stopped")

        retirement_thread = self._reload_commit_thread
        pending = self._pending_reload
        if (
            retirement_thread is not None
            and not retirement_thread.is_alive()
            and pending is not None
            and pending.get("retirement_result") is not None
        ):
            # The GLib loop may already have quit, so its queued finalizer cannot
            # run. Finalize synchronously before the pipeline transition.
            self._finish_reload_commit(pending)

        transition: dict[str, object] = {"result": None, "error": None}

        def _set_pipeline_null() -> None:
            try:
                state_result = self.pipeline.set_state(Gst.State.NULL)
                if state_result == Gst.StateChangeReturn.FAILURE:
                    transition["result"] = state_result
                    return
                remaining = max(0.0, deadline - time.monotonic())
                result, _current, _pending = self.pipeline.get_state(int(remaining * Gst.SECOND))
                transition["result"] = result
            except Exception as exc:
                transition["error"] = repr(exc)

        transition_thread: threading.Thread | None = None
        try:
            transition_thread = threading.Thread(
                target=_set_pipeline_null,
                name="civiccast-pipeline-null",
                daemon=True,
            )
            transition_thread.start()
            transition_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        except Exception as exc:
            transition["error"] = repr(exc)
        transition_clean = bool(
            transition_thread is not None
            and not transition_thread.is_alive()
            and transition["result"] == Gst.StateChangeReturn.SUCCESS
        )
        if not transition_clean:
            detail = transition["error"] or "transition exceeded teardown budget"
            print(
                f"WARN: pipeline did not reach NULL within teardown bound: {detail}",
                file=sys.stderr,
                flush=True,
            )
        retirement_thread = self._reload_commit_thread
        if retirement_thread is not None and retirement_thread.is_alive():
            retirement_thread.join(timeout=max(0.0, deadline - time.monotonic()))
            pending = self._pending_reload
            if (
                not retirement_thread.is_alive()
                and pending is not None
                and pending.get("retirement_result") is not None
            ):
                self._finish_reload_commit(pending)
        # Round-2 finding 1: aborted legs now retire on their own worker threads
        # (``_start_aborted_leg_retirement``). Give them the same bounded chance to
        # finish before the process goes away, so an abort taken moments before
        # stop still releases its selector pads instead of being reported as an
        # unclean teardown. Bounded by the same deadline as everything else here.
        for abort_thread in self._abort_retire_threads:
            if abort_thread.is_alive():
                abort_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        aborts_clean = not any(thread.is_alive() for thread in self._abort_retire_threads)
        if not aborts_clean:
            print(
                "WARN: an aborted reload leg was still retiring at stop",
                file=sys.stderr,
                flush=True,
            )
        retirement_clean = retirement_thread is None or not retirement_thread.is_alive()
        clean = bool(transition_clean and retirement_clean and aborts_clean)
        if self.audio_tap_writer is not None:
            # Round-2 finding 4: this used to be handed the LEFTOVER of the shared
            # teardown deadline. By this point the pipeline-NULL transition and the
            # retirement join above have usually consumed all of it, so the tap
            # writer was routinely closed with timeout=0.0 -- reported "abandoned",
            # and the final caption WAV chunk (the one covering the last seconds of
            # the meeting, which is exactly the material an operator is most likely
            # to need) was lost on an otherwise clean stop. Flushing a WAV chunk is
            # a short, local, disk-bound operation that has nothing to do with how
            # long the GStreamer teardown took, so give it its own small bounded
            # budget instead of the remainder of somebody else's.
            tap_deadline = min(_AUDIO_TAP_CLOSE_TIMEOUT_S, self.teardown_timeout_s)
            tap_status = self.audio_tap_writer.close(timeout=max(0.0, tap_deadline))
            if tap_status == "abandoned":
                print(
                    "WARN: caption audio tap writer did not drain before teardown "
                    f"({self.teardown_timeout_s}s bound); abandoning it in the "
                    "background -- a trailing caption segment may be lost",
                    file=sys.stderr,
                    flush=True,
                )
            self.audio_tap_writer = None
        pending = self._pending_reload
        if pending is not None and pending.get("commit_in_progress", False):
            # ``force_exit_on_hang=False`` is used by non-worker callers/tests. Do
            # not leave the process-wide commit watchdog armed after bounded stop
            # has already reported failure; the worker path exits immediately below.
            self._finish_commit_watchdog(pending)
        if not clean and force_exit_on_hang:
            os._exit(70)  # nonzero: a forced kill, not a clean exit (audit MINOR)
        return clean
