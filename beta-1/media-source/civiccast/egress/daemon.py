# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Egress daemon loop foundation."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Generator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from threading import Event, Lock, RLock
from typing import Any, ClassVar, NamedTuple, Protocol, cast

from civiccast.captions.tap import build_audio_tap_plan
from civiccast.egress._text import db_safe_text, db_safe_text_or_none
from civiccast.egress.asrun import (
    AsRunCaptureSchemaError,
    AsRunRecorder,
    asset_id_for_segment,
    map_source_kind,
)
from civiccast.egress.branding import EgressBrandingPlan
from civiccast.egress.caption_embed import EgressCaptionEmbeddingPlan
from civiccast.egress.cg_bridge import (
    CG_EGRESS_PROOF_BOUNDARY,
    EgressCgOverlayClearProof,
    EgressCgOverlayProof,
    build_cg_overlay_clear_egress_proof,
)
from civiccast.egress.encoder_strategy import (
    ConcatEncoderStrategy,
    EncoderStartRequest,
    EncoderStrategy,
)
from civiccast.egress.errors import (
    ConfigInvalidError,
    EgressError,
    EncoderUnavailableError,
    SecretUnresolvedError,
    SourcePrepareError,
)
from civiccast.egress.gst.exit_codes import (
    GST_FIRST_OUTPUT_TIMEOUT_EXIT_CODE,
    GST_PREROLL_TIMEOUT_EXIT_CODE,
    GST_RELOAD_COMMIT_TIMEOUT_EXIT_CODE,
)
from civiccast.egress.gst.graph import GraphicsOverlayLayer
from civiccast.egress.gst.reload_policy import should_defer_switch
from civiccast.egress.health import (
    EgressEncoderMetrics,
    build_default_sink_health,
    encoder_has_progress,
    read_ffmpeg_encoder_metrics_since,
    read_latest_ffmpeg_encoder_metrics,
    worker_produced_output,
)

# BETA.10 U16 item B: the output A/V guard reads the channel's own HLS window
# with the SAME manifest semantics (and the same ffprobe resolution/timeout/
# tri-state fail-safe) U12 gave the relay's progress and stream-kind checks: an
# independent reader of the same window would eventually disagree with those
# about what "the newest complete segment" means.
from civiccast.egress.hls_relay import (
    _last_complete_segment,
    _playlist_path_for,
    _segment_first_packet_pts,
)
from civiccast.egress.models import (
    MAX_PLAYLIST_SUBCHAINS,
    RESTART_RECOVERY_COMMAND_PREFIX,
    SLATE_RESTART_COMMAND_PREFIX,
    CaptionStatus,
    EgressCommand,
    EgressConfig,
    EgressHealthSample,
    EgressProofEvent,
    EgressSourcePlan,
    EgressSourceSegment,
    EgressState,
    EgressStateRow,
    redact_source_uri,
    redact_uris_in_text,
    restart_recovery_previous_state,
    slate_restart_guard_state,
)
from civiccast.egress.pacing import UniformPacingLatch
from civiccast.egress.preparer import SourcePreparationReport
from civiccast.egress.process_identity import verify_and_kill_process
from civiccast.egress.runtime import (
    FfmpegStarter,
)
from civiccast.egress.schema_currency import current_schema_version
from civiccast.egress.sinks import SecretResolver
from civiccast.egress.store import EgressStore
from civiccast.stream._ffmpeg import FfmpegNotFoundError

SourcePlanProvider = Callable[[str], EgressSourcePlan | None]
BoundarySourcePlanProvider = Callable[[str, datetime], EgressSourcePlan | None]
#: U53 item 1: ``(channel_id, after)`` -> the start instant of the next
#: playable scheduled item strictly after ``after``, or None when nothing
#: further is scheduled. Supplied by ``ScheduleSourcePlanProvider
#: .next_item_start_at`` in production; a filler rollover uses it to trim the
#: filler to the gap and chain the programme due at its end into the same plan.
NextProgramStartProvider = Callable[[str, datetime], datetime | None]
FallbackSourceProvider = Callable[[EgressConfig], EgressSourcePlan]
SourcePreparerFunc = Callable[[EgressSourcePlan, EgressConfig], SourcePreparationReport]
BrandingPlanProvider = Callable[[str], EgressBrandingPlan | None]
CaptionPlanProvider = Callable[[str], EgressCaptionEmbeddingPlan | None]
CaptionStatusProvider = Callable[[str], CaptionStatus]
CaptionReadinessProvider = Callable[[str], Any]
CgOverlayProofProvider = Callable[[str], EgressCgOverlayProof | None]
#: S15 §5 CG-lite: (channel_id, config) → board raster for the engine overlay.
#: Takes the config because the raster must match ``canonical_profile`` geometry.
BoardOverlayProvider = Callable[[str, EgressConfig], Path | None]
BoardLiveVideoProvider = Callable[[str, EgressConfig], tuple[GraphicsOverlayLayer, ...]]
SinkHealthProvider = Callable[[str, EgressConfig, EgressEncoderMetrics], dict[str, bool]]
AlertEvaluatorHook = Callable[[str, EgressState, "float | None", "float | None"], None]
"""(channel_id, state, encoder_fps, encoder_bitrate_kbps) → None. S8 alert hook."""
IndependentSlateStrategyFactory = Callable[[], "EncoderStrategy | None"]
"""() → an encoder strategy that does not depend on the `ffmpeg` binary being on
PATH, or None if no such strategy is available in this deployment. See
``_default_independent_slate_strategy`` (K2-1 follow-up, P1)."""

_LOG = logging.getLogger(__name__)


def _is_ready_generated_slate(plan: EgressSourcePlan) -> bool:
    """The built-in plain slate is canonical and extended by stream copy."""
    return bool(plan.segments) and all(
        segment.kind == "slate"
        and segment.source_ref == "civiccast-slate"
        and segment.inpoint_seconds is None
        and segment.outpoint_seconds is None
        for segment in plan.segments
    )


def _default_independent_slate_strategy() -> EncoderStrategy | None:
    """Best-effort construction of an ffmpeg-PATH-independent encoder, used only
    as the last-resort slate retry after a ``FfmpegNotFoundError`` (K2-1 follow-up,
    P1 audit finding).

    ``civiccast.stream._ffmpeg._ffmpeg_path()`` is a pure ``shutil.which("ffmpeg")``
    PATH lookup: it is completely content-independent, so it returns the identical
    result on every call within one process regardless of which source plan (the
    program, or the fallback slate) is being encoded. Retrying the SAME
    ``ConcatEncoderStrategy`` instance against the slate plan is therefore
    guaranteed to raise the identical ``FfmpegNotFoundError`` again -- the
    "advance the ladder to slate" claim for this specific exception was never
    reachable through the real strategy, only through a fake test double that
    could not exist in production (audit finding: the regression test asserted a
    state the real strategy cannot enter).

    ``GstPlayoutStrategy`` is a genuinely separate encoder: it launches a
    per-channel GStreamer worker subprocess and never calls ``_ffmpeg_path()`` or
    shells out to the ``ffmpeg`` binary at all, so it is a real independent
    fallback rather than a second attempt at the one already known to be
    unusable. Returns None (falls through to the existing zero-ffmpeg-floor ERROR
    handling, unchanged from before this fix) when the GStreamer engine's package
    is not importable in this deployment, or its construction fails for any
    reason -- this must never raise past the caller.
    """
    try:
        from civiccast.egress.gst.strategy import GstPlayoutStrategy
    except ImportError:
        return None
    try:
        return GstPlayoutStrategy()
    except Exception:
        return None


# S9-5 crash-relaunch back-off. The first crash relaunches immediately (fast
# recovery for a one-off); a crash that recurs within the cooldown is a churn
# signal (typically a worker that dies at startup — a bad graph, a missing
# element) and is paced instead of hot-looped. A worker that stays up at least
# _RESTART_STREAK_RESET_UPTIME_S resets the streak (the failure was transient,
# not a loop). NOTE: the restart cooldown (15s) and the engine's stall timeout
# (CIVICCAST_STALL_TIMEOUT_S, default 10s) are independent constants. A worker
# that keeps stalling restarts on roughly the cooldown cadence — its ~stall-
# timeout-long no-output cycle is under the 15s cooldown, so every relaunch after
# the first is paced rather than passed straight through. That pacing IS the
# intended anti-churn behavior (a persistently-dead source must not hot-loop).
_RESTART_COOLDOWN_SECONDS = 15.0
_RESTART_STREAK_RESET_UPTIME_S = 60.0
# Clean-machine walkthrough of beta.5 (2026-09-09 MDT): a channel airing the
# FALLBACK SLATE for a program committed at 22:00 showed STOPPED at 22:00:23
# and stayed dark until an operator pressed Start. The slate plan is finite to
# the next due item (source_plan.py); ChannelAutomationService's slate replan
# enqueued the reload at the boundary, but the reload's prepare (the first-ever
# conform of the clip) ran synchronously on the automation thread, the slate
# worker reached EOS meanwhile and exited 0, and _poll_process's clean-exit
# branch -- which had no pending reload to bind to -- wrote STOPPED. Automation
# bails on STOPPED and only auto_start (UI default off) restarts a dark
# channel. That branch now relaunches onto the due program itself (see
# _relaunch_slate_eos_onto_due_program) -- at most this many CONSECUTIVE
# automatic relaunches per channel. A COUNT, deliberately not a time window:
# round-2 hostile review measured that the fallback plan from
# bulletin_filler.build_filler_source_provider is at most
# min(MAX_PLAYLIST_SUBCHAINS // len(segments), ceil(3600 / cycle)) cycles of
# _SLIDE_SECONDS (10s) -- a hard ceiling of 120s per slate plan -- so a 30s
# window could never refuse anything: a persistently unplayable program would
# have flapped slate -> relaunch -> slate every ~2 minutes forever (two plan
# resolves plus one synchronous cold prepare on the automation thread per
# cycle). The counter clears only when a REAL program airing (ON_AIR, not the
# slate) holds observed on-air evidence for _RESTART_STREAK_RESET_UPTIME_S,
# and on an operator stop or start (see _reset_restart_tracking and
# _process_command). Once the cap is hit the next clean slate exit goes STOPPED
# with a last_error naming the program's media as the thing to check.
_SLATE_EOS_RELAUNCH_MAX_CONSECUTIVE = 1
# Emit an escalation proof event at this restart streak and every multiple after
# (the S8 alerting hook — the actual alert dispatch is wired when S8 lands, build
# step 4; until then the escalation is durably recorded as a proof event).
_RESTART_ESCALATION_STREAK = 5

# BLOCKER B1 (hostile audit): a live SRT/UDP/RTSP source that is unreachable or
# drops makes the GStreamer worker crash immediately after each relaunch — the
# encoder process itself starts fine (so _start's own EncoderUnavailableError /
# FfmpegNotFoundError fallback-to-slate seam never fires), then dies inside the
# pipeline once it can't connect to / keep reading the source. Left alone that
# is an infinite crash-loop against the SAME dead source: _relaunch_after_crash
# paces the *rate* of relaunch (the cooldown above) but never changes *what* it
# relaunches, so the channel never reaches a stable on-air state — "dead air is
# NEVER acceptable" (see the encoder-unavailable comment below) was violated for
# exactly this case. At this many consecutive crash-relaunches that never once
# reached a healthy uptime (the streak only advances on a crash before the
# reset-uptime threshold; see _RESTART_STREAK_RESET_UPTIME_S), the daemon stops
# trusting the configured source and forces the same fallback-slate path used
# for EncoderUnavailableError/FfmpegNotFoundError in _start, instead of
# relaunching against the same source again. This IS the terminal state that
# replaces dead air: the channel lands on FALLBACK_SLATE, healthy uptime there
# resets the streak (see _poll_process), and civiccast.egress.automation's
# existing _check_slate_replan already retries the real source on its own
# 30s-paced cooldown — so a source that recovers is picked back up automatically,
# and a source that stays dead keeps the station on slate instead of crash-looping
# silently forever.
_LIVE_SOURCE_FAILURE_FALLBACK_STREAK = _RESTART_ESCALATION_STREAK
# Bound on the child-stderr tail folded into ``last_error`` -- the state row is an
# operator-facing string, not a log sink.
_STDERR_TAIL_MAX_CHARS = 600
_RELOAD_COMMIT_TIMEOUT_MARKER = "CTRL reload: commit did not finish"
_RELOAD_FRAME_RE = re.compile(
    r'^\s*File ".*[\\/]engine\.py", line (?P<line>\d+) in '
    r"(?P<function>_dispose_confirmed_old_leg|_confirm_reload_selector_handoff|"
    r"_dispose_source_leg|_retire_reload_old_leg|"
    r"_finish_reload_commit|_begin_reload_commit|_commit_reload|_commit_reload_body)\s*$"
)
# U14 F1: ordered INNERMOST blocking frame first, because ``_child_stderr_tail``
# returns the first name in this tuple that appears anywhere in the log -- so a
# caller listed ahead of its callee wins and sends the reader one frame off.
#
# Measured, 2026-09-24 18:48 (U13): the dump puts the retirement thread in
# ``_dispose_confirmed_old_leg`` (engine.py:3468) with its three-line caller
# ``_retire_reload_old_leg`` (engine.py:3151) directly beneath it; the tuple held
# only the caller, so the operator's state row and control-plane-app.log:22310
# named the waiter -- "blocked at engine.py:3151 in _retire_reload_old_leg" -- and
# never the synchronous ``release_request_pad`` that actually held the lock.
# ``_confirm_reload_selector_handoff`` is the twin case: five further watchdog
# dumps in the same log block the main loop at its ``get_property("active-pad")``
# readback (engine.py:2964) reached through ``_begin_reload_commit`` (engine.py:3141),
# and before U14 no name in this tuple matched it at all.
#
# The two commit-path frames therefore rank above ``_dispose_source_leg`` (the
# ABORT path's disposer, kept because beta.5-era evidence used it): a dump can
# contain both threads' frames, and the marker that gates this whole branch
# (``_RELOAD_COMMIT_TIMEOUT_MARKER``) is printed by the COMMIT watchdog, so the
# commit-path frame is the one that answers the question the operator asked.
_RELOAD_FRAME_PRIORITY = (
    "_dispose_confirmed_old_leg",
    "_confirm_reload_selector_handoff",
    "_dispose_source_leg",
    "_retire_reload_old_leg",
    "_finish_reload_commit",
    "_begin_reload_commit",
    "_commit_reload",
    # Historical beta.5 evidence used the pre-repair synchronous helper name.
    "_commit_reload_body",
)

# Item 82: a GST_PREROLL_TIMEOUT_EXIT_CODE exit (a slow-but-progressing preroll
# under CPU load) still relaunches through _relaunch_after_crash's normal
# back-off path, but must not advance _restart_streak (the crash-loop counter
# that eventually forces fallback slate, _LIVE_SOURCE_FAILURE_FALLBACK_STREAK
# above) more than once per this window -- see _relaunch_after_crash's own
# docstring for why an uncapped counter would misfire on a healthy source.
_PREROLL_TIMEOUT_STREAK_COOLDOWN_S = 60.0

# Item 84: a GST_FIRST_OUTPUT_TIMEOUT_EXIT_CODE exit (PLAYING was reached, but
# no output buffer crossed the mux within the engine's own, separate bound --
# see engine.py's ``_check_stall``) is the SAME kind of thing as a
# preroll-timeout exit for every purpose _relaunch_after_crash cares about: a
# slow-but-progressing start under load, not a crash, that must relaunch
# through the normal back-off path while being rate-limited out of the
# crash-loop streak the same way, using the SAME per-channel cooldown window
# and the same ``_preroll_timeout_streak_incr_at`` bookkeeping (one shared
# "was there a recent slow-start streak increment for this channel" clock is
# correct here -- a channel legitimately alternating between the two exit
# reasons is still one and the same "slow start" situation, not two
# independent ones needing two separate rate limits).
_SLOW_START_EXIT_CODES = frozenset(
    {GST_PREROLL_TIMEOUT_EXIT_CODE, GST_FIRST_OUTPUT_TIMEOUT_EXIT_CODE}
)

# F1 redesign (coordinator hostile review, 2026-09-06): absolute backstop for a
# pending reload settlement that never arrives at all (e.g. the worker crashed
# between arming the reload and writing reload-status.json, or the status file
# itself never made it to disk). Longer than the engine's own
# ``defer_switch_timeout_s`` (900s default, GstPlayoutEngine.__init__) plus a
# generous margin for the pipe round trip and a couple of missed poll ticks --
# a legitimate deferred switch always settles well within this window; past it,
# treat the reload as lost and fall back to restart rather than wait forever.
_PENDING_RELOAD_SETTLE_DEADLINE_S = 960.0
_ROLLOVER_EXTENSION_TOLERANCE_S = 0.25

# U26 defect 2 (live, government, 2026-09-25 02:58 MDT): a reload that resolves
# the schedule at wall-clock now can land on the CLOSING SECONDS of the item
# that is due. With the GStreamer engine selected the production provider is
# built with ``max_segments=1`` (automation.py:2634), so the plan for that slot
# is one segment covering only its remainder -- legal, prepared, and aired to
# EOS, after which the channel needs a SECOND worker start to reach the program
# that was due the whole time. The log pins the shape: the engine reached EOS at
# 02:58:19,385 for a plan whose own recorded end was 02:58:30.392844Z (~11s of
# slot left), the restart onto that stub went ON_AIR at 02:58:26,207, the stub
# itself ran to EOS at 02:58:34,137, and the next worker only reached ON_AIR at
# 02:58:36,574.
#
# Below this floor a tail is not worth the restart that installs it. Landing a
# restart costs ~10s end to end on this box (prepare ~2.2s + [terminate + exit
# detection] ~2.2s + respawn ~2.4s + worker start to first output ~2s + settle),
# so 30s is ~3x the cost of the switch itself -- while still an order of
# magnitude below the shortest plausible scheduled item, and half the
# automation's own 60s rollover lead margin so it can never fight the rollover
# machinery. It is also far below the slate fill horizon (3600s, one continuous
# pre-conformed fill file since U27), which is what makes replacing the tail
# safe rather than a second way to sit on slate.
_SCHEDULE_TAIL_FLOOR_SECONDS = 30.0
# The boundary is taken just PAST the tail's own end, where the next item
# becomes due: the resolver's item test is the half-open ``starts_at <= t <
# ends_at`` (_current_item_index), and two adjacent slots need not meet exactly
# (a gap the fill policy owns, a schedule authored to the second, a plan whose
# durations were floored), so the margin keeps the resolution inside the next
# item rather than on a boundary instant.
_SCHEDULE_TAIL_BOUNDARY_MARGIN_S = 1.0
# U67: the deployed rollover lead, restated (automation.py derives it as
# ``2 * 300s preparation timeout + 30s + 60s = 690s``; see
# ``ChannelAutomationService._rollover_lead_seconds``). Restated rather than
# imported on purpose: the daemon must not take a live dependency on the
# automation module's internals, and this watchdog has to hold on a daemon that
# is draining already-queued commands with no automation in the process at all.
_TRANSITIONING_WATCHDOG_LEAD_SECONDS = 690.0
# U67: how long a ``_pending_reloads`` pin may stand before the daemon stops
# trusting it. The pin is armed only by a reload that DECLINED or by a wedge
# (see ``_pending_reloads``), and every one of its arms hands the channel's
# recovery to a worker EXIT -- so its honest lifetime is "the rest of the plan
# on air, then a restart", and a bound derived from plan length + lead is the
# right shape. Flat part: one lead (the automation never dispatches a rollover
# with more than a lead of plan left, and inside the lead the channel is
# *meant* to be waited on) plus ``_PENDING_RELOAD_SETTLE_DEADLINE_S`` for any
# accepted-but-unsettled reload still outstanding -- 1650s, 2.4x the worst
# legitimate pin. ``_reload_stall_bound_seconds`` extends it from the
# rollover's own recorded horizon when that is further out, so a declined
# reload against a long plan is never judged early.
_TRANSITIONING_WATCHDOG_SECONDS = (
    _TRANSITIONING_WATCHDOG_LEAD_SECONDS + _PENDING_RELOAD_SETTLE_DEADLINE_S
)
# U67: after the watchdog re-issues the rollover once, how long the pin may
# survive before the channel is restarted instead. One preparation timeout --
# the longest a re-issue can take to arm something -- so a pin still standing
# after it has declined a second time, with nothing in flight, is a dead end.
_TRANSITIONING_WATCHDOG_REISSUE_GRACE_SECONDS = 300.0
# U36 item 7: how many times a boundary reload the worker ABORTED pre-commit is
# re-resolved and re-armed before the channel gives up on the seamless path and
# arms the fallback slate at the boundary instead. Two, because the abort's
# observed causes are transient (a decodebin stream error on one prepared
# segment, a source that was not ready) and every retry costs only a re-resolve
# plus a prepare while the outgoing leg keeps airing -- whereas giving up
# leaves the boundary unprepared, which is what cost the public channel ~80s of
# dead air on 2026-09-25 (10s aggregate stall watchdog -> exit 1 -> relaunch).
_RELOAD_RETRY_LIMIT = 2
# BETA.10 live finding: after a relay child is (re)spawned it needs a moment to
# receive the UDP feed and cut its first segment before its window is judged.
# Self-heal is suppressed inside this window so a fresh child is never killed.
_HLS_RELAY_STARTUP_GRACE_S = 20.0
_START_EXPIRED_FALLBACK_REASON = (
    "Scheduled source plan expired during preparation; aired fallback slate while "
    "automation resolves the current schedule."
)

# BETA.10 U47: the reason recorded on an ACTIVE channel's start/relaunch while its
# program is being prepared. The live 2026-09-26 finding (evidence/public-exit1-1119
# and evidence/public-exit0-0159): a worker exit was followed by a relaunch that
# resolved and conformed the scheduled program for 20-133 s with NO encoder running
# at all -- the channel was black for the whole preparation and the relay reported
# "live window has not advanced" (public dark ~11 min at 01:59). The rule: an ACTIVE
# channel is never dark while its program is being prepared, so the relaunch airs
# the fallback slate at once and hands the prepared program in afterwards.
_SLATE_FIRST_HANDOFF_REASON = (
    "Preparing the scheduled program; airing the fallback slate so the channel "
    "stays up while it conforms."
)

#: The states an ACTIVE channel can be in when a start/relaunch reaches
#: ``_start_steps``. ON_AIR and TRANSITIONING are the crash-relaunch route
#: (``_begin_relaunch`` writes STARTING first, but the row a relaunch reads back
#: can still be the pre-exit one); FALLBACK_SLATE is a channel already holding the
#: slate that is being restarted for some other reason. A STOPPED/ERROR channel --
#: an operator start of a dark channel -- is deliberately NOT here: there is no
#: slate to keep, so the first thing that channel should air is the program.
#:
#: U53 item 4: STARTING is here too. ``_start_steps`` publishes STARTING before
#: its worker exists, so a predecessor that died mid-start leaves that row
#: behind, and the restart-recovery sweep -- whose own reconcile set
#: (``_STALE_RECONCILE_STATES``, mirrored by ``_STALE_CLAIM_STATES`` in
#: models.py) is ``{"ON_AIR", "STARTING", "TRANSITIONING"}`` -- reads it to
#: DECIDE to recover and carries it onto the queued command. With STARTING out
#: of this set that recovery start passed ``slate_first=True`` and then failed
#: this gate, so it conformed its program with nothing on air: the same dead
#: air the ON_AIR shape had, reached through the other recovered state.
_SLATE_FIRST_ACTIVE_STATES = frozenset({"ON_AIR", "STARTING", "FALLBACK_SLATE", "TRANSITIONING"})

# BETA.10 U16 item B: the OUTPUT A/V sync guard.
#
# The U16 finding (2026-09-24) was a one-sided rebase step -- one leg's
# timestamps moved ~109s and the other's did not -- and NOTHING in the egress
# path looked at the A/V relationship of the program actually leaving the
# encoder, so the only place that step was ever visible was a human watching the
# channel. This guard is the cheap output-side self-check that closes that gap:
# per channel, on a settled ON_AIR channel only, measure the first packet PTS of
# video and of audio in the newest COMPLETE segment of the channel's HLS window
# (``_segment_first_packet_pts``, U12's own ffprobe resolution/timeout/fail-safe)
# and, when the two stay apart across consecutive probes, restart that channel's
# worker the same way a dead encoder is restarted.
#
# The thresholds are deliberately loose. A SINGLE probe being off is normal --
# segment cuts land on video keyframes, so a cut segment's audio can legitimately
# lead or lag its video by a fraction of a frame, and a window roll can show one
# more of those -- which is why the verdict needs THREE consecutive over-limit
# probes (~90s of a channel that is visibly broken) and why the limit itself is
# a whole second rather than a frame. This guard is a backstop for a fault the
# system cannot yet prevent (see the U16 reports): it must be far slower to
# act than the rebase path it watches.
_OUTPUT_AV_GUARD_PROBE_INTERVAL_S = 30.0
#: |audio first PTS - video first PTS| above this (seconds) is over limit.
_OUTPUT_AV_GUARD_MAX_OFFSET_S = 1.0
#: Consecutive over-limit probes before the guard acts.
_OUTPUT_AV_GUARD_CONSECUTIVE_PROBES = 3
#: Restarts the guard may spend per channel inside the rolling window below.
#: Once spent, the guard requests the fallback slate and holds it until the
#: latest restart ages out; then it makes one program retry.
_OUTPUT_AV_GUARD_RESTART_BUDGET = 3
_OUTPUT_AV_GUARD_BUDGET_WINDOW_S = 3600.0
#: Cadence of the budget-exhausted ERROR line (it repeats, but not per probe).
_OUTPUT_AV_GUARD_EXHAUSTED_LOG_INTERVAL_S = 600.0

# BETA.10 U30: the relay-self-heal FAILURE escalation.
#
# The U30 finding (2026-09-25, observed live twice: education 06:27, government
# 01:03) is that the relay self-heal is a one-step recovery with no step after
# it. A deferred program->program reload commits at a segment boundary
# (``CTRL reload committed (elements=52)``), the HLS window stops advancing, the
# relay supervisor correctly detects the frozen window, correctly respawns its
# ffmpeg child once -- and that child then writes nothing either. The one-shot
# ``heal_attempted`` latch (hls_relay audit finding 4) deliberately prevents a
# second heal in the same episode, so the relay path is out of moves and the
# channel sits frozen until the operator restarts it. Both live incidents ended
# exactly that way: ``STALE 36s -> 144s -> full channel restart``.
#
# This guard is the missing second step, modeled on the U16 output A/V guard
# above (same shape: probe on the existing poll tick, act through the existing
# bounded worker termination, bounded restarts per rolling hour, then fallback
# slate and one program retry after the latest restart ages out).
#
# SIGNAL DECISION: the HLS live window is the signal, and it is the only one.
# It is what residents actually see, the daemon already reads it every tick for
# the relay poll, and the relay supervisor already stamps an exact clock for it
# (``heal_frozen_seconds`` -- time since the heal). The worker's ``CTRL output: N
# buffers`` progress line is deliberately NOT used even though it is the more
# direct "is the engine producing" question: the daemon has no cheap, reliable
# seam to it. Those lines are written to the worker child's stderr and the
# daemon holds only a ``Popen`` handle and a probe counter, never the child's
# text stream; reading it would mean capturing and parsing a child's stderr on
# the hot path, and (see the U30 report) the stderr of the live incident shows
# the counter ADVANCING all the way through the freeze, so it would not even
# have answered the question. The window is ground truth; production is a
# hypothesis about the window.
#
# The bound is the relay's own bound plus one segment cadence of slack. The
# relay already required ``_DEFAULT_STALL_BOUND_S`` (30s) of a motionless
# window before it healed at all, and a healthy replacement ffmpeg child writes
# its first segment within its normal segment cadence (seconds). Waiting
# another 30s after the heal is an order of magnitude past that, and it keeps
# the two bounds identical and equally explainable to an operator instead of
# introducing a second magic number.
_FREEZE_ESCALATION_AFTER_HEAL_S = 30.0
#: Worker restarts this escalation may spend per channel inside the window
#: below, mirroring the U16 budget. Once spent, the channel stays on its
#: fallback slate until the latest restart ages out, then gets one program retry.
_FREEZE_ESCALATION_RESTART_BUDGET = 3
_FREEZE_ESCALATION_BUDGET_WINDOW_S = 3600.0
#: Cadence of the budget-exhausted CRITICAL line (it repeats, but not per tick).
_FREEZE_ESCALATION_EXHAUSTED_LOG_INTERVAL_S = 600.0

# A quality-guard budget exhaustion uses the same rolling-hour horizon as the
# restart budget. The channel stays on its fallback slate through that window;
# only then does the supervisor make one program retry.
_QUALITY_RECOVERY_COOLDOWN_S = 3600.0
_CHILD_STDERR_TAIL_BYTES = 256 * 1024


class _PendingReloadSettlement(NamedTuple):
    """F1 redesign: everything ``_poll_reload_settlement`` needs to either
    finish the target-state bookkeeping (on "applied") or fall back to restart (on
    "aborted:<reason>" or a deadline lapse) for one armed-but-not-yet-settled
    content-reload. Constructed by ``_try_content_reload``, consumed and
    cleared by ``_poll_reload_settlement``."""

    reload_id: str
    since: float  # daemon._monotonic() at arm time -- the deadline clock
    process: object
    config: EgressConfig
    source_plan: EgressSourcePlan
    switch_at_end_of_current: bool
    previous_state: str | None
    previous_source_label: str | None
    target_state: EgressState
    plan_dir: Path | None
    #: U36 item 7: the rollover horizon this reload was armed for, or ``None``
    #: for a reload with no recorded horizon. Carried into the settlement so
    #: ``_poll_reload_settlement`` can re-arm an aborted boundary reload against
    #: the SAME boundary (``_retry_aborted_boundary_reload``) and key its bounded
    #: retry budget on it.
    rollover_plan_end_at: datetime | None = None
    #: U53 item 1: the projected end of the next scheduled programme when this
    #: reload's plan is a filler with that programme chained behind it, else
    #: ``None``. Carried into ``_commit_reload_settlement``'s
    #: ``_record_dispatched_plan`` call, which is what makes
    #: ``chained_slate_program_end`` -- and so channel automation's
    #: slate-replan suppression -- describe THIS plan and no other.
    chained_program_end_at: datetime | None = None


class _TailFloorOutcome(NamedTuple):
    """U36 decision 2: what ``_resolve_schedule_tail`` decided. ``plan`` is the
    plan the caller should air -- the original tail, the item due where that
    tail ends, or (``slate`` True) the fallback slate. ``slate`` tells the
    caller to publish ``FALLBACK_SLATE`` for it rather than ``ON_AIR``."""

    plan: EgressSourcePlan
    slate: bool


def _ascii_safe(text: str) -> str:
    """Fold arbitrary child output so it can reach the database, whatever its
    server encoding.

    T6 soak evidence (Desktop/CIVICCAST-EVIDENCE/soak-120-e502074-20260905, kit
    e502074): the GStreamer worker's stall message contained one non-ASCII
    character. ``_child_stderr_tail`` reads the child log with
    ``errors="replace"``, so it arrived as U+FFFD; ``_child_exit_error`` folded it
    into ``last_error``; ``_write_state`` wrote that to Postgres, and psycopg
    raised ``UnicodeEncodeError: 'charmap' codec can't encode character '\\ufffd'``
    while converting the statement for a non-UTF8 client encoding.

    That exception escaped ``_begin_relaunch`` -> ``_relaunch_after_crash`` ->
    ``_poll_process`` -> ``process_once``, i.e. out of
    ``ChannelAutomationService._run_channel_pass`` BEFORE it reaches
    ``_check_slate_replan`` / ``_check_plan_rollover`` (automation.py) -- so every
    crash-relaunch tick silently skipped the seamless-rollover machinery #162
    added. 23 aborted passes in that 2h soak, zero rollovers dispatched.

    Child stderr is untrusted, arbitrarily encoded text; nothing about an
    operator-facing error string needs to carry it verbatim. Fold it here, at the
    single boundary where child bytes become a persisted value -- delegates to
    :func:`civiccast.egress._text.db_safe_text`, the same helper every OTHER
    persisted free-text path in this module now goes through (current_source_label,
    proof-event label/machine_summary, last_error), so every one of those paths
    degrades identically instead of each choosing its own fold.
    """

    return db_safe_text(text)


# RAT-004: poll cadence for stop_all_channels' observed-exit wait loop.
_DRAIN_POLL_INTERVAL_SECONDS = 0.05


class ChannelDrainOutcome(NamedTuple):
    """One channel's result from :meth:`EgressDaemon.stop_all_channels`."""

    channel_id: str
    outcome: str  # "drained" | "killed_after_deadline" | "already_gone"


class DrainResult(NamedTuple):
    """Per-channel outcomes of a :meth:`EgressDaemon.stop_all_channels` call."""

    outcomes: tuple[ChannelDrainOutcome, ...]


class OrphanInfo(NamedTuple):
    """Identity of a running process a reaper is considering (audit ENG-001).

    ``created_at`` is the process start time (epoch seconds); reapers only
    touch processes created BEFORE this server booted, and terminators
    re-verify it so a recycled pid is never killed (pid-reuse TOCTOU).
    """

    name: str
    created_at: float


class _PreparationState(Enum):
    PENDING = "preparing"
    START_EXPIRED = "start-expired"
    #: U47: a start/relaunch of a channel that was ALREADY ON AIR has launched
    #: its worker on the fallback slate, so the channel is no longer dark -- but
    #: the scheduled program it is actually there for has not been prepared yet.
    #: The caller hands that program in through the ordinary reload path.
    #:
    #: A separate value rather than a bool because the two outcomes mean
    #: different things to ``_poll_preparation``: START_EXPIRED re-enters
    #: ``_start`` with the slate forced, whereas this one must NOT re-enter
    #: ``_start`` at all (that would relaunch the worker it just launched).
    SLATE_FIRST = "slate-first"


class _PreparationRequest(NamedTuple):
    plan: EgressSourcePlan
    config: EgressConfig


class _ReusePreparedPlan(NamedTuple):
    """F3(b): the outcome a reload's steps return when it must NOT take the
    in-place selector swap because the channel is on FALLBACK_SLATE and the
    policy declined to defer, carrying the preparation report that reload had
    ALREADY produced.

    A non-deferred reload out of FALLBACK_SLATE tears down the live slate leg
    under the input-selector, which wedged for 270 s on 2026-09-24 (U13), so
    that combination is routed to the terminate+restart path instead. Carrying
    the report is what keeps that restart from conforming the same plan a
    second time (measured 133.6 s for the 18:48 government program).

    Never constructed without a report: when this daemon has no
    ``source_preparer`` there is nothing to reuse, so the steps return plain
    ``False`` and the ordinary restart re-resolves the plan.

    ``target_state`` is the running state the SEAMLESS path would have
    published for this same report (``_reload_steps``' own ``target_state``,
    ``FALLBACK_SLATE`` only for a force-fallback filler rollover). The restart
    reproduces it rather than re-deriving it, so a reused slate plan cannot be
    published as ``ON_AIR`` and vice versa."""

    report: SourcePreparationReport
    target_state: EgressState


# What a preparation's steps can hand back when they finish: ``True``/``False``
# for a reload's arm outcome, ``None`` for a start that ran (or deliberately
# declined), a ``_PreparationState`` for the one state a start can report, and
# ``_ReusePreparedPlan`` for an F3(b) reload that declined the in-place swap
# while holding a plan the restart must air instead of re-preparing.
_PreparationOutcome = bool | _PreparationState | _ReusePreparedPlan | None

_PreparationSteps = Generator[
    _PreparationRequest,
    SourcePreparationReport,
    _PreparationOutcome,
]
AsyncSourcePreparerFunc = Callable[
    [EgressSourcePlan, EgressConfig, Event, frozenset[Path]], SourcePreparationReport
]
#: U60: the off-air whole-asset pre-conform the automation's rollover
#: look-ahead asks for -- ``SourcePreparer.warm_plan``. Returns nothing and
#: must be total (it queues work; it never prepares anything itself), so the
#: automation's dispatch tick can call it without a latency or failure
#: contract on the caller.
SourcePlanWarmerFunc = Callable[[EgressConfig, EgressSourcePlan], None]


@dataclass
class _PendingPreparation:
    steps: _PreparationSteps
    future: Future[SourcePreparationReport]
    cancel: Event
    kind: str
    process: object | None
    config: EgressConfig | None
    #: U47: this preparation is the hand-off half of a slate-first start, i.e.
    #: the channel is ON AIR on the fallback slate right now and this reload is
    #: trying to replace it with the scheduled program. Carried here because the
    #: failure of exactly this preparation must NOT take the ordinary
    #: "fall back to restart" route -- see ``_poll_preparation``.
    slate_first: bool = False


@dataclass
class _OutputAvGuardState:
    """Per-channel bookkeeping for the output A/V sync guard (U16 item B).

    ``pid`` is the worker these measurements describe: the streak is reset
    whenever it changes, because a replacement worker's first segment says
    nothing about its predecessor's last one.

    ``offsets`` holds the last ``_OUTPUT_AV_GUARD_CONSECUTIVE_PROBES``
    ``(offset_seconds, segment_name)`` pairs -- the evidence the ERROR line
    reports, so an operator can see the three measurements rather than only
    being told the channel was restarted.

    ``restarted_at`` is this channel's restart timestamps inside
    ``_OUTPUT_AV_GUARD_BUDGET_WINDOW_S`` (pruned on each use), and
    ``last_exhausted_log_at`` throttles the exhausted-budget line.
    """

    pid: int | None = None
    streak: int = 0
    offsets: list[tuple[float, str]] = field(default_factory=list)
    last_probe_at: float | None = None
    restarted_at: list[float] = field(default_factory=list)
    last_exhausted_log_at: float | None = None


@dataclass
class _FreezeEscalationState:
    """Per-channel bookkeeping for the relay-self-heal failure escalation (U30).

    Two different kinds of state live here, and they are deliberately NOT
    cleared together:

    * ``pid``/``escalated_for_pid`` are the CURRENT EPISODE -- which worker the
      frozen window is evidence about, and whether this escalation has already
      acted on that worker. A pid change voids the episode: whatever the
      replacement does, its predecessor's frozen window says nothing about it.
      ``escalated_for_pid`` is what makes "exactly one ERROR line and one
      restart per escalation" true even in the ticks before a killed worker's
      exit is reaped and its replacement started.
    * ``restarted_at``/``last_exhausted_log_at`` are the CHANNEL's budget, and
      are deliberately NOT voided by a pid change: a restart obviously changes
      the pid, so clearing the budget with it would make the budget meaningless
      and the escalation an unbounded restart loop.
    """

    pid: int | None = None
    escalated_for_pid: int | None = None
    restarted_at: list[float] = field(default_factory=list)
    last_exhausted_log_at: float | None = None


@dataclass
class _QualityRecoveryState:
    """One bounded fallback/cooldown cycle after a quality guard exhausts its budget."""

    retry_at: float
    reason: str


class EgressDaemon:
    """Consume egress commands and run the configured output encoder."""

    def __init__(
        self,
        store: EgressStore,
        *,
        work_dir: Path,
        source_plan_provider: SourcePlanProvider,
        boundary_source_plan_provider: BoundarySourcePlanProvider | None = None,
        # U53 item 1: used only when a rollover's target is filler -- it answers
        # "when is the next scheduled programme due?" so the filler can be
        # trimmed to exactly that gap and the programme chained behind it in the
        # same plan (one plan per channel, kept). None (every caller that does
        # not wire it, plus the ffmpeg-concat engine) means the filler airs
        # whole, exactly as before.
        next_program_start_provider: NextProgramStartProvider | None = None,
        fallback_source_provider: FallbackSourceProvider | None = None,
        source_preparer: SourcePreparerFunc | None = None,
        async_source_preparer: AsyncSourcePreparerFunc | None = None,
        branding_plan_provider: BrandingPlanProvider | None = None,
        cg_overlay_provider: BoardOverlayProvider | None = None,
        board_live_video_provider: BoardLiveVideoProvider | None = None,
        caption_plan_provider: CaptionPlanProvider | None = None,
        caption_status_provider: CaptionStatusProvider | None = None,
        caption_readiness_provider: CaptionReadinessProvider | None = None,
        cg_overlay_proof_provider: CgOverlayProofProvider | None = None,
        sink_health_provider: SinkHealthProvider | None = None,
        alert_evaluator_hook: AlertEvaluatorHook | None = None,
        encoder_strategy: EncoderStrategy | None = None,
        independent_slate_strategy_factory: IndependentSlateStrategyFactory | None = None,
        resolve_secret: SecretResolver | None = None,
        ffmpeg_starter: FfmpegStarter | None = None,
        orphan_probe: Callable[[int], OrphanInfo | None] | None = None,
        pid_is_dead: Callable[[int], bool | None] | None = None,
        orphan_terminator: Callable[[int, float], None] | None = None,
        as_run_recorder: AsRunRecorder | None = None,
        restart_cooldown_seconds: float = _RESTART_COOLDOWN_SECONDS,
        monotonic: Callable[[], float] | None = None,
        ts_relay_supervisor: Any | None = None,
        # DEFECT A: the GStreamer engine's hls sinks are delivered by a
        # supervised ffmpeg relay child (real segments + a real manifest),
        # not a native GStreamer element — see civiccast.egress.hls_relay.
        # None (the ffmpeg-concat engine's default) means hls sinks pass
        # through unchanged; that engine's own EgressSink/HlsSink already
        # writes real HLS directly.
        hls_relay_supervisor: Any | None = None,
        # DEFECT D: called (channel_id, command, exception) whenever one
        # queued command raises during process_once — see process_once's
        # per-command isolation. Wired to the operator alert surface in
        # civiccast.egress.automation.build_channel_automation; None is a
        # silent no-op (tests / the CLI daemon loop that don't wire alerting).
        command_failure_hook: Callable[[str, EgressCommand, BaseException], None] | None = None,
        sleep: Callable[[float], None] | None = None,
        # F3 fix (hostile-review follow-up, 2026-09-06): optional hook to
        # immediately reclaim one specific SourcePreparer per-plan directory
        # (``SourcePreparer.release``) once THIS daemon independently knows a
        # plan is retired -- see ``_poll_reload_settlement`` (a just-settled
        # reload's predecessor) and ``_stop`` (the channel's active plan on an
        # operator stop). None (the default, and every caller that doesn't
        # construct a real ``SourcePreparer``) means GC alone reclaims stale
        # directories -- never a correctness issue, just slower cleanup.
        prepared_plan_release: Callable[[Path | None], None] | None = None,
        channel_start_hook: Callable[[str], None] | None = None,
        # U60: ``SourcePreparer.warm_plan``, called by the automation's
        # rollover look-ahead to pre-conform an upcoming boundary's assets
        # while the plan on air is still shorter than the rollover lead.
        # None (the default, and every caller that doesn't construct a real
        # ``SourcePreparer``) makes ``warm_source_plan`` a no-op -- the
        # automation probes for that method and skips the walk, so an old or
        # bare daemon behaves exactly as it did before U60.
        source_plan_warmer: SourcePlanWarmerFunc | None = None,
    ) -> None:
        self._store = store
        # Single injectable monotonic clock for all crash-relaunch timing (the
        # restart latch + worker uptime), so tests drive the back-off deterministically
        # through the constructor instead of reaching into private state.
        self._monotonic = monotonic or time.monotonic
        # #151: persistent per-channel TS relay so encoder relaunches never
        # reset the mux session at a udp-ts headend. None = pass-through.
        self._ts_relay = ts_relay_supervisor
        self._hls_relay = hls_relay_supervisor
        self._command_failure_hook = command_failure_hook
        self._channel_start_hook = channel_start_hook
        self._source_plan_warmer = source_plan_warmer
        # Cleared only by a successful real launch reset, never by an in-place
        # content reload or duplicate Start against the current worker.
        self._caption_reset_failed: set[str] = set()
        self._work_dir = work_dir
        self._source_plan_provider = source_plan_provider
        self._boundary_source_plan_provider = boundary_source_plan_provider
        self._next_program_start_provider = next_program_start_provider
        self._fallback_source_provider = fallback_source_provider
        self._source_preparer = source_preparer
        self._async_source_preparer = async_source_preparer
        self._preparation_executor: ThreadPoolExecutor | None = None
        self._preparations: dict[str, _PendingPreparation] = {}
        self._preparation_guard = RLock()
        self._preparation_channel_locks: dict[str, Lock] = {}
        self._preparation_closed = False
        self._branding_plan_provider = branding_plan_provider
        # S15 §5 CG-lite: per-channel board raster for the engine overlay leg.
        self._cg_overlay_provider = cg_overlay_provider
        self._board_live_video_provider = board_live_video_provider
        self._caption_plan_provider = caption_plan_provider
        self._caption_status_provider = caption_status_provider
        self._caption_readiness_provider = caption_readiness_provider
        self._cg_overlay_proof_provider = cg_overlay_proof_provider
        self._sink_health_provider = sink_health_provider
        self._alert_evaluator_hook = alert_evaluator_hook
        self._encoder_strategy = encoder_strategy or ConcatEncoderStrategy()
        # K2-1 follow-up (P1): factory, not an instance, so a real GStreamer worker
        # is never constructed unless a FfmpegNotFoundError actually needs it. Tests
        # inject a fake factory instead of a fake encoder_strategy so the injected
        # double represents a genuinely SEPARATE encoder, matching production.
        self._independent_slate_strategy_factory = (
            independent_slate_strategy_factory or _default_independent_slate_strategy
        )
        self._resolve_secret = resolve_secret
        self._ffmpeg_starter = ffmpeg_starter
        self._processes: dict[str, object] = {}
        self._started_at: dict[str, float] = {}
        # Round-3 fix (PR #183 review, BLOCKER item 1): _monotonic() of the
        # FIRST poll tick that observed real evidence the current worker
        # reached PLAYING (GStreamer: the ``CTRL preroll: reached PLAYING``
        # stderr marker) or is actively encoding (FFmpeg: fps/bitrate
        # progress) -- see ``_observed_on_air_evidence``. Absent until that
        # evidence is seen; popped on every exit/spawn alongside
        # ``_started_at`` so a fresh worker never inherits a stale timestamp.
        # ``_poll_process``'s healthy-uptime reset (the ALIVE-poll twin of
        # ``_relaunch_after_crash``'s own reset) is gated on THIS, never on
        # wall-clock seconds since spawn -- spawn-to-PLAYING time includes
        # interpreter start + ``import gi``/``Gst.init`` + graph build + the
        # preroll wait itself, none of which is air, and none of which is
        # bounded by the worker's own preroll timeout.
        self._on_air_confirmed_at: dict[str, float] = {}
        # Round-4 fix (PR #183 review, BLOCKER reproduced): the byte SIZE of
        # the channel's stderr log at the moment the CURRENTLY-tracked worker
        # was spawned (0 if the log did not exist yet). ``strategy.py`` /
        # ``_ffmpeg.py`` open this fixed per-channel log in APPEND mode and
        # never truncate it per spawn -- without this anchor, one worker
        # EVER reaching PLAYING (or producing FFmpeg progress) left that
        # evidence sitting in the log forever, and every later worker on the
        # same channel read as "confirmed on air" on its very first poll
        # tick even if it never produced any output itself (measured: 40
        # relaunches, streak pinned at 1 -- the round-3 fix's own healthy-
        # uptime clock never actually started from real evidence, because it
        # was reading round-1's worker's marker). ``_observed_on_air_evidence``
        # scans only bytes AT OR AFTER this offset (see
        # ``civiccast.egress.health.worker_reached_playing`` /
        # ``read_ffmpeg_encoder_metrics_since``). Popped on every exit/spawn
        # alongside ``_started_at`` so a fresh worker never inherits a stale
        # offset from the worker it replaced.
        self._stderr_spawn_offset: dict[str, int] = {}
        # Identity of the active append/rotating log generation when each
        # worker was spawned. A rotating writer can replace the base path while
        # keeping the same name; offsets from the previous inode then point into
        # the wrong generation.
        self._stderr_spawn_identity: dict[str, tuple[int, int] | None] = {}
        # Per channel: the horizon of the source plan actually DISPATCHED to the
        # encoder (see dispatched_plan_horizon / _record_dispatched_plan).
        self._dispatched_plan_horizon: dict[str, tuple[str | None, tuple[float, ...], bool]] = {}
        # Per channel: the first program occurrence, asset, and join-in-progress
        # offset in the plan actually on air, keyed to its proof event. A later
        # lookup of that same occurrence at an advanced offset is its remainder;
        # another scheduled use of the same asset is a fresh short programme.
        self._dispatched_program_source: dict[str, tuple[str, str, str, float]] = {}
        # Per channel (U53 item 1): when the dispatched plan is a filler whose tail
        # is the next scheduled programme chained behind it, the projected end of
        # that chained programme -- ``(proof_event_id, end_at)``. Set and cleared
        # only by ``_record_dispatched_plan`` (the single dispatch choke point),
        # keyed by the same proof event as the horizon record, so a stale chain can
        # never outlive the plan it described. Read by
        # ``chained_slate_program_end`` -> channel automation's slate-replan
        # suppression. Empty means "the dispatched plan is not a chained filler".
        self._chained_slate_program: dict[str, tuple[str | None, datetime]] = {}
        # Item 78 fix 3: the automation-known projected end of the LIVE plan a
        # rollover reload is extending, recorded by ChannelAutomationService
        # (record_rollover_plan_end) immediately before it enqueues the reload
        # command. The durable EgressCommand queue is a fixed webapp<->daemon
        # schema shared by every issuer (operator actions included), not the
        # place to carry ephemeral per-dispatch automation bookkeeping, so this
        # rides an in-memory side channel instead.
        #
        # Round 3 (coordinator review): consumed (popped) at the TOP of
        # _request_reload -- the one method every "reload" command reaches --
        # not inside _try_content_reload. Round 2 popped it inside
        # _try_content_reload, which closed every early return INSIDE that
        # method but missed _request_reload's own three earlier exits that
        # never call it at all (worker missing/dead -> _start; no state row;
        # strategy lacks supports_content_reload), each of which measurably
        # left a stale value sitting here forever.
        #
        # Round 4 (coordinator review): a round-3 revision ALSO popped this in
        # _start, on the theory that _start's other callers (initial start,
        # auto_start, crash-relaunch) never go through _request_reload either
        # and could otherwise leave a stale value sitting here. That was
        # itself a regression, MEASURED: item 78's own diagnosed scenario is
        # a worker crash whose relaunch (_poll_process -> _relaunch_after_
        # crash -> _begin_relaunch -> _start) runs BEFORE that same tick's
        # queued "reload" command is drained -- _start's pop ran first and
        # ate the value _request_reload's own pop needed moments later,
        # silently turning a should-cut-immediately rollover reload back into
        # a deferred one (a 900s held leg). _start must never pop this; only
        # _request_reload (every reader) and _stop (the channel going dark
        # makes any in-flight rollover moot) do.
        #
        # Round 5 (coordinator review): round 4's docstring here claimed the
        # entries left uncleared by _drain and stop_all_channels' "process
        # already gone" branch were benign because "every route back to a
        # seamless content reload for this channel goes through
        # _request_reload's own unconditional pop first, so a value sitting
        # here from before the channel went dark can never reach
        # should_defer_switch stale." That was FALSE: _request_reload's pop
        # does not discard the value, it PASSES it down (as
        # rollover_plan_end_at=) through _try_content_reload into
        # should_defer_switch -- popping a stale entry feeds it to
        # should_defer_switch exactly as readily as a fresh one would.
        # MEASURED: record_rollover_plan_end("gov", <2020-01-01>); let the
        # worker exit cleanly (rc=0, STOPPED) -- none of _stop, _drain, nor
        # stop_all_channels ran, so the value survived; restart the channel
        # (ON_AIR); issue a plain operator reload with no rollover behind it
        # at all -- switch_at_end_of_current came back False (cut
        # immediately) instead of True (should have deferred, the ordinary
        # ON_AIR/no-override shape). Every off-air route that bypasses
        # _stop -- _poll_process's clean-exit and terminal-ERROR worker-exit
        # branches, _drain's process-is-None branch, and stop_all_channels'
        # "already gone" branch -- now pops this itself, the same as _stop
        # does.
        #
        # Round 5 claimed this made it impossible for anything recorded
        # before a channel goes fully dark to survive to a later, unrelated
        # reload. That claim was ALSO false, MEASURED two more ways (round 6,
        # coordinator review):
        #
        # - Crash 1, then crash 2 landing inside the back-off cooldown, took
        #   the DEFERRED branch of _relaunch_after_crash: the channel sits in
        #   STARTING with no process running for the whole cooldown, and this
        #   value survived untouched (that branch never popped). When
        #   _service_backoff_relaunch later fired the deferred relaunch it
        #   re-resolved a fresh plan same as any other relaunch, so the stale
        #   value never fed that relaunch -- but it was still there to wrongly
        #   bind a DIFFERENT, later, plain operator reload landing during the
        #   cooldown. The same leak reached the same place when an operator
        #   explicit start command superseded the deferred relaunch instead
        #   (_process_command pops ``_backoff_relaunch`` there but was not
        #   popping this dict).
        # - _start's own two terminal-ERROR ``except`` clauses (ConfigInvalid/
        #   SecretUnresolved/FfmpegNotFound and the general EgressError
        #   fallback) never popped either. ERROR is not on air by any
        #   definition this dict cares about, so a value recorded going into
        #   a start attempt that lands in ERROR survived across it.
        #
        # Both routes now pop, closing those two gaps. Exactly two routes
        # deliberately still do NOT pop, and both are call sites of _start
        # that keep the channel effectively ON AIR through the transition
        # (per round 4's _start-must-never-pop fix above), relying on
        # _request_reload's own pop instead: the pending-reload restart
        # inside _poll_process, and the IMMEDIATE crash-relaunch path
        # (_relaunch_after_crash -> _begin_relaunch -> _start, taken when the
        # back-off latch permits running now rather than deferring).
        #
        # Round 7 (coordinator review): the IMMEDIATE crash-relaunch path
        # above still leaked, MEASURED: record_rollover_plan_end for a
        # rollover reload about to be enqueued -> a crash lands before that
        # command drains -> the immediate relaunch (_begin_relaunch -> _start,
        # which per round 4 must not pop) re-resolves a fresh plan and puts
        # the channel back ON_AIR -> the value sits here, popped by nothing,
        # until whatever "reload" command drains next -- which is not
        # necessarily the rollover's own command. A later, wholly unrelated
        # operator reload draining before (or instead of) the rollover's own
        # queued command inherited the stale, already-past plan_end_at and
        # was wrongly cut immediately (switch_at_end_of_current=False)
        # instead of deferring (True, the ordinary ON_AIR/no-override shape
        # an unrecorded reload takes). Every route above closes "does the
        # channel go off-air" leaks; none of them closes "does the reload
        # that drains next actually belong to the automation that recorded
        # this."
        #
        # Fixed by scoping the recorded value to the specific reload command
        # it was recorded for: this dict now stores ``(command_id,
        # plan_end_at)`` pairs. ``record_rollover_plan_end`` takes the
        # ``command_id`` of the reload command automation is about to
        # enqueue (see ``ChannelAutomationService._enqueue``, which now
        # returns the id it generated so the caller can record against the
        # exact same id).
        #
        # Round 8 (coordinator review): round 7's ``_request_reload`` popped
        # this dict UNCONDITIONALLY -- on every drain, matched or not -- and
        # only gated whether the popped value was USED on the id match. That
        # was still wrong, MEASURED: the common trigger is
        # ``ChannelAutomationService``'s own 45s issued-timeout retry
        # (``_check_plan_rollover``'s ``retrying_undelivered`` branch) --
        # dispatch A records ``command_id=A`` and enqueues reload A; the
        # daemon stalls past the retry timeout; the retry re-records
        # (OVERWRITING this dict's entry) ``command_id=B`` and enqueues
        # reload B. If A then drains first, round 7's unconditional pop threw
        # B's freshly-recorded entry away right there, on A's mismatch --
        # so when B itself drained moments later there was nothing left at
        # all, and ``should_defer_switch`` deferred against no recorded
        # horizon even though B's own horizon (the one just discarded) had
        # already passed. Round 6 measured the pre-scoping shape as
        # ``[False, True]`` (A wrongly cut); round 7's scoping flipped it to
        # ``[True, True]`` (both wrongly defer -- dead air on a horizon
        # already past, never cut at all).
        #
        # Fixed by popping ONLY when ``_request_reload``'s own ``command_id``
        # actually matches the recorded one -- a mismatch now LEAVES the
        # entry in place, untouched, for whichever command it was actually
        # recorded against to consume when THAT one drains, rather than
        # discarding it on the first unrelated command that happens to drain
        # first. This is safe against every off-air/relaunch leak inventoried
        # above: none of those routes go through ``_request_reload`` at all
        # -- each pops this dict itself, unconditionally, at its own off-air
        # transition (see every "Round N" entry above) -- so an entry left
        # here by a mismatch cannot outlive the channel going off-air either.
        # The one route this changes behavior for is exactly the one it
        # exists to fix: a later command matching the recorded id can still
        # find it and consume it, no matter how many *other* mismatched
        # reloads drained first.
        #
        # ``record_rollover_plan_end``'s ``command_id`` is now a required
        # keyword-only parameter -- the ``None`` "wildcard matches whatever
        # drains next" default is gone; a caller that does not (or cannot)
        # scope its recording must say so explicitly (``command_id=None``),
        # and an explicit ``None`` only matches a drain whose own
        # ``command_id`` is also ``None`` (the direct-call routes in
        # ``supervisor.py`` that bypass the command queue entirely) -- it is
        # ordinary equality now, not a special-cased wildcard. See
        # ``record_rollover_plan_end`` and ``_request_reload``'s docstrings
        # for the exact matching rule.
        # U41: the fourth element is the rollover's own horizon
        # (``min_plan_seconds``) when the dispatcher measured one, else None.
        self._rollover_plan_end_at: dict[str, tuple[str | None, datetime, bool, float | None]] = {}
        # S9-5 crash-relaunch back-off: a latch paces rapid repeat relaunches, a
        # per-channel streak counts consecutive rapid crashes (for escalation +
        # reset on healthy uptime), and _backoff_relaunch holds a deferred relaunch
        # (committed (prev_state, prev_source_label)) until the latch permits it.
        # RAT-004: injectable sleep for the drain-all deadline-wait loop (mirrors
        # the ``monotonic`` seam above) so tests drive it deterministically
        # without a real wall-clock wait.
        self._sleep = sleep or time.sleep
        self._restart_latch = UniformPacingLatch(
            default_cooldown_seconds=restart_cooldown_seconds, clock=self._monotonic
        )
        self._restart_streak: dict[str, int] = {}
        # Consecutive automatic relaunches of a finite fallback-slate plan that
        # reached EOS onto the due program (_poll_process's clean-exit branch
        # -> _relaunch_slate_eos_onto_due_program), per channel. Capped by
        # _SLATE_EOS_RELAUNCH_MAX_CONSECUTIVE; cleared by a real ON_AIR
        # airing that holds healthy uptime, an operator stop, or an operator
        # start (see _reset_restart_tracking / _process_command).
        self._slate_eos_relaunches: dict[str, int] = {}
        # Item 82 (extended by item 84): last _monotonic() the crash-loop
        # streak was actually incremented for a "slow start" exit --
        # GST_PREROLL_TIMEOUT_EXIT_CODE or GST_FIRST_OUTPUT_TIMEOUT_EXIT_CODE
        # (see _SLOW_START_EXIT_CODES) -- rate-limits how often either of
        # those exit reasons can advance the streak (see
        # _relaunch_after_crash). Ordinary crashes are unaffected and always
        # increment the streak on every exit.
        self._preroll_timeout_streak_incr_at: dict[str, float] = {}
        self._backoff_relaunch: dict[str, tuple[str, str | None]] = {}
        self._draining_channels: set[str] = set()
        self._pending_reloads: dict[str, tuple[str | None, str | None]] = {}
        # Issue #157: channels whose encoder WE terminated for a filler
        # reload - their non-zero exit still honors the pending reload.
        self._reload_kills: set[str] = set()
        # U52 item 2: WHY each of those kills happened, carried to the
        # ``worker exited`` line. A deliberate kill exits non-zero on real
        # ffmpeg, so without this the log line for a 3m48s-slate-recovery
        # restart is byte-identical in shape to an encoder crash.
        self._reload_kill_reasons: dict[str, str] = {}
        # Issue #161: a server restart leaves the previous server's encoder
        # children streaming to the sink ports; before starting fresh, the
        # daemon reaps any still-running ffmpeg pid from the durable state
        # row. Seams default to psutil implementations.
        self._orphan_probe = orphan_probe or _default_orphan_probe
        # Restart reconciliation uses a SEPARATE, stricter liveness predicate
        # (tri-state; fails CLOSED on uncertainty) so it never treats
        # AccessDenied/unknown as 'dead' the way the orphan probe's None does.
        self._pid_is_dead = pid_is_dead or _default_pid_is_dead
        self._orphan_terminator = orphan_terminator or _default_orphan_terminator
        # S23 as-run capture: optional append-only side-write at each ACTUAL
        # source transition (None on the test/CLI in-memory path). Capture is
        # fail-safe — _record_as_run_transition guards every call so a recorder
        # error never breaks the playout path.
        self._as_run_recorder = as_run_recorder
        # S23 §6.1 schema-drift flag (E-1): set when the recorder raises
        # AsRunCaptureSchemaError so the daemon can mark a degraded-mode marker
        # without crashing the playout path. Toggling this is loud (an ERROR
        # log + a distinct message) so silent loss of the as-aired ledger is
        # impossible — the very failure mode S23 §6.1 exists to prevent.
        self._as_run_schema_drift = False
        # Reap guard (audit ENG-001): only processes created before this
        # daemon existed can be a predecessor's orphans.
        self._boot_epoch = time.time()
        self._stderr_logs: dict[str, Path] = {}
        # MAJOR M1: last-observed liveness of each channel's supervised HLS
        # relay child (True = confirmed dead since last poll; absent = alive
        # or not applicable). Polled every process_once tick (see
        # _poll_hls_relay) so a relay death (disk full / ffmpeg missing / OOM)
        # is visible even while the main encoder keeps sending fine.
        self._hls_relay_dead: dict[str, bool] = {}
        # BETA.10 U16 item B: the output A/V sync guard's measurement seam and
        # per-channel bookkeeping. The probe is a plain attribute (not a
        # constructor parameter) for the same reason ``_monotonic`` is one: it
        # is not a production wiring choice -- the shipped measurement is
        # ``_segment_first_packet_pts`` and always will be -- but tests must be
        # able to script measurements without an ffprobe and without real
        # segments on disk.
        self._output_av_probe: Callable[[Path], tuple[float, float] | None] = (
            _segment_first_packet_pts
        )
        self._output_av_guard: dict[str, _OutputAvGuardState] = {}
        # BETA.10 U30: the relay-self-heal failure escalation's per-channel
        # bookkeeping. No seam is needed here -- its input signal is the relay
        # supervisor's ``heal_frozen_seconds``, which the daemon already holds.
        self._freeze_escalation: dict[str, _FreezeEscalationState] = {}
        # Shared, session-local fallback cooldown for both quality guards.
        self._quality_recovery: dict[str, _QualityRecoveryState] = {}
        # An operator stop is stronger than configured auto_start for this
        # daemon session. Only an explicit operator start clears this latch.
        self._operator_stopped_channels: set[str] = set()
        # The STORED config each live pipeline was built from (review round 3
        # delta, MAJOR 2). Health samples key ``sink_connected`` by sink label
        # from this, not from whatever the config row says NOW: a sink saved
        # after the build is not on air, and reporting it as connected is the
        # "working-but-invisible" lie. The headend-preset route reads the
        # latest sample's labels to decide whether an identical re-apply is
        # truly ``unchanged`` on air (``router._running_pipeline_delivers``).
        # The keying happens inside ``_sink_connected`` so that EVERY health
        # appender -- including a content reload's settlement, which carries
        # the config row it read when it armed -- reports the built set
        # (review round 4 delta, MAJOR 1).
        self._built_configs: dict[str, EgressConfig] = {}
        self._last_loudness_lufs: dict[str, float] = {}
        self._active_cg_overlay_ids: dict[str, str] = {}
        self._last_cg_overlay_event_keys: dict[str, tuple[str, str, str | None]] = {}
        # F1 redesign (2026-09-06): a content-reload's ack now means only
        # "armed" -- the actual commit/abort is tracked here until
        # ``_poll_reload_settlement`` observes it (or its deadline lapses).
        # See ``_try_content_reload``'s docstring for the full design.
        self._pending_reload_settle: dict[str, _PendingReloadSettlement] = {}
        # U67: the reload-stall watchdog's own bookkeeping for the
        # ``_pending_reloads`` pin -- when that pin was armed, the bound it is
        # judged against (extended by the rollover horizon it was armed with),
        # and which recovery rung it has already been given. All three are
        # cleared the moment the pin clears; see ``_arm_pending_reload`` and
        # ``_poll_reload_stall_watchdog``.
        self._reload_stall_since: dict[str, float] = {}
        self._reload_stall_bound_s: dict[str, float] = {}
        self._reload_stall_rungs: dict[str, int] = {}
        # F3 fix: the per-plan prepared/ directory (SourcePreparationReport.
        # plan_dir) backing the CURRENTLY on-air plan for each channel, if the
        # configured source_preparer reports one. Updated when a reload
        # settles applied; the PREVIOUS value is released at that point (see
        # ``_poll_reload_settlement``) rather than left for GC alone.
        self._active_prepared_plan_dir: dict[str, Path] = {}
        # F3(b): the plan a non-deferred FALLBACK_SLATE reload already
        # prepared (carried in a ``_ReusePreparedPlan``), held between "the
        # restart was decided" and "the restart's ``_start`` consumes it". Its
        # ``report.plan_dir`` is part of ``live_prepared_plan_dirs`` while it
        # is held, and EVERY path that does not consume it must release it
        # (see ``_discard_prepared_restart_plan``) -- an entry here is a live
        # directory the preparer's GC may not evict.
        self._prepared_restart_plans: dict[str, _ReusePreparedPlan] = {}
        self._prepared_plan_release = prepared_plan_release
        # Hostile-review follow-up (2026-09-06), item 1: the reload_id a
        # discarded (worker-exited/superseded/restarted) pending settlement
        # last carried, kept ONLY so a late-arriving status write for that
        # dead attempt can be recognized and logged as ignored instead of
        # silently doing nothing -- see ``_discard_pending_reload_settlement``
        # and ``_poll_reload_settlement``. Keyed by channel_id, so this is
        # already bounded by the number of channels this daemon instance
        # tracks (at most one entry per channel, always overwritten by that
        # channel's next discard) -- NOT by time. (Hostile-review follow-up,
        # third pass, P2: a second-pass revision briefly added a bounded-age
        # expiry here on the theory that this dict could otherwise grow
        # unboundedly; it could not (the per-channel key already caps it),
        # and the added expiry could evict an id whose real settlement lands
        # legitimately right at the edge of the same
        # ``_PENDING_RELOAD_SETTLE_DEADLINE_S`` budget the pending entry
        # itself was allowed to take -- and it was never evicted for a
        # channel that gets stopped, so it did not even close the gap it was
        # written for. Reverted to a plain reload_id with no expiry.)
        self._discarded_reload_ids: dict[str, str] = {}

        # U36 item 7: bounded retry budget for a boundary reload the worker
        # aborted pre-commit, keyed by channel. The value is
        # ``(rollover_plan_end_at, attempts_used)``; the horizon is part of the
        # key so a NEW boundary's budget starts at zero without anything having
        # to clear the entry (see ``_retry_aborted_boundary_reload``).
        self._reload_retries: dict[str, tuple[datetime | None, int]] = {}

    def _board_live_video_layers(
        self, channel_id: str, config: EgressConfig
    ) -> tuple[GraphicsOverlayLayer, ...]:
        if self._board_live_video_provider is None:
            return ()
        try:
            return self._board_live_video_provider(channel_id, config)
        except Exception:
            _LOG.exception(
                "channel %s: board live-video settings could not be read; keeping the ordinary board",
                channel_id,
            )
            return ()

    def enable_async_preparation(self) -> None:
        """Keep media conformance off the shared automation thread."""
        with self._preparation_guard:
            if self._preparation_executor is None:
                self._preparation_closed = False
                self._preparation_executor = ThreadPoolExecutor(
                    max_workers=8, thread_name_prefix="egress-prepare"
                )

    def shutdown_preparation(self) -> None:
        with self._preparation_guard:
            self._preparation_closed = True
            for channel_id in tuple(self._preparations):
                self._cancel_preparation(channel_id)
            executor = self._preparation_executor
            self._preparation_executor = None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    def _cancel_preparation(self, channel_id: str) -> None:
        with self._preparation_guard:
            pending = self._preparations.pop(channel_id, None)
            if pending is None:
                return
            pending.cancel.set()
            pending.steps.close()
            pending.future.cancel()

            # A late result may own files, but may never launch an encoder or
            # write channel state. The callback only releases that unused plan.
            def release_unused(future: Future[SourcePreparationReport]) -> None:
                if not future.cancelled() and future.exception() is None:
                    self._release_prepared_plan_dir(future.result().plan_dir)

            pending.future.add_done_callback(release_unused)

    def _drive_preparation(
        self,
        channel_id: str,
        steps: _PreparationSteps,
        *,
        kind: str,
        slate_first: bool = False,
    ) -> _PreparationOutcome:
        with self._preparation_guard:
            if self._preparation_closed:
                steps.close()
                return None
            try:
                request = next(steps)
            except StopIteration as done:
                # ``Generator``'s StopIteration value is typed ``Any``.
                return cast(_PreparationOutcome, done.value)
            preparer = self._source_preparer
            assert preparer is not None
            executor = self._preparation_executor
            if executor is None:
                try:
                    try:
                        report = preparer(request.plan, request.config)
                    except Exception as exc:
                        steps.throw(exc)
                    else:
                        steps.send(report)
                except StopIteration as done:
                    return cast(_PreparationOutcome, done.value)
                raise RuntimeError("Unexpected second media preparation in one operation")

            cancel = Event()
            protected = self.live_prepared_plan_dirs(channel_id)
            channel_lock = self._preparation_channel_locks.setdefault(channel_id, Lock())
            async_preparer = self._async_source_preparer

            def prepare() -> SourcePreparationReport:
                # Superseded preparations for one channel must not race its GC.
                with channel_lock:
                    if cancel.is_set():
                        raise SourcePrepareError("Preparation cancelled")
                    if async_preparer is not None:
                        return async_preparer(request.plan, request.config, cancel, protected)
                    return preparer(request.plan, request.config)

            self._preparations[channel_id] = _PendingPreparation(
                steps=steps,
                future=executor.submit(prepare),
                cancel=cancel,
                kind=kind,
                process=self._processes.get(channel_id),
                config=self._store.get_config(channel_id),
                slate_first=slate_first,
            )
            _LOG.info("channel %s: %s source preparation queued", channel_id, kind)
            return _PreparationState.PENDING

    def _poll_preparation(self, channel_id: str) -> None:
        with self._preparation_guard:
            pending = self._preparations.get(channel_id)
            if pending is None or not pending.future.done():
                return
            if pending.process is not self._processes.get(
                channel_id
            ) or pending.config != self._store.get_config(channel_id):
                self._cancel_preparation(channel_id)
                self._request_reload(channel_id)
                return
            self._preparations.pop(channel_id)
            completed = False
            outcome: _PreparationOutcome = None
            try:
                try:
                    report = pending.future.result()
                except Exception as exc:
                    pending.steps.throw(exc)
                else:
                    pending.steps.send(report)
            except StopIteration as done:
                completed = True
                outcome = cast(_PreparationOutcome, done.value)
            finally:
                pending.steps.close()
            if completed:
                if isinstance(outcome, _ReusePreparedPlan):
                    # F3(b): the reload declined the in-place selector swap
                    # (FALLBACK_SLATE + non-deferred) and handed back the plan
                    # it had already prepared. Terminate+restart, carrying that
                    # report so the restart does not conform it again.
                    self._fall_back_to_restart_for_reused_plan(channel_id, outcome)
                elif pending.kind == "reload" and outcome is False:
                    if pending.slate_first:
                        # U47: the hand-off's program preparation failed while the
                        # channel is ON AIR on the slate. The ordinary fallback
                        # (``_fall_back_to_restart_reload``) adds this channel to
                        # ``_reload_kills`` and terminates the live slate worker
                        # (issue #157), taking an airing channel dark -- exactly
                        # what U47 exists to prevent, and measurably what happened
                        # before this branch existed: the slate worker was gone
                        # and the channel had no encoder until the next restart.
                        # Same handling as the synchronous path's tail in
                        # ``_request_reload``.
                        self._keep_slate_after_failed_hand_off(channel_id)
                    else:
                        self._fall_back_to_restart_reload(channel_id)
                elif pending.kind == "start" and outcome is _PreparationState.START_EXPIRED:
                    self._start(
                        channel_id,
                        force_fallback_slate=True,
                        force_fallback_reason=_START_EXPIRED_FALLBACK_REASON,
                    )
                elif pending.kind == "start" and outcome is _PreparationState.SLATE_FIRST:
                    # U47: this is where the production (async) path reaches the
                    # hand-off. ``_start`` already returned at the slate's own
                    # preparation being QUEUED, so -- unlike the synchronous path
                    # -- the slate worker is only up now, as this tick's
                    # preparation settles. Handing the program in from here is
                    # what keeps an ON_AIR channel airing the slate for the whole
                    # conform instead of being dark for it.
                    self._hand_off_slate_first_program(channel_id)
                return
            raise RuntimeError("Unexpected second media preparation in one operation")

    def process_once(self, channel_id: str) -> int:
        """Process all currently queued commands for one channel.

        DEFECT D (found live: after a crash, later queued commands —
        "takeover", then a "stop" — sat unprocessed with zero log activity
        for minutes). Root cause: ``pop_pending_commands`` marks the ENTIRE
        currently-pending batch consumed in one durable update before any of
        it runs (see ``EgressStore.pop_pending_commands``); an unguarded
        ``for`` loop here meant one command raising (e.g. the DEFECT A hls
        crash, or a hls-config'd channel's ``_start`` re-raising on every
        subsequent takeover/reload attempt because the broken sink was still
        configured) aborted the loop, and every command AFTER it in that same
        batch was already marked consumed — durably lost, never retried,
        with no per-command trace of what happened. Isolating each command's
        processing means one bad command can no longer take the rest of the
        batch down with it; the crashed command itself is still consumed
        (at-most-once delivery is unchanged — reissue it), but everything
        queued alongside or after it still runs.

        Encoding-defect follow-up (reviewer finding on the state-write-encoding
        branch): ``_poll_process``/``_service_backoff_relaunch`` write
        persisted state on a crash-relaunch (``_write_state``,
        ``append_proof_event``), and ``_poll_hls_relay`` can too. Before this
        fix, an exception escaping ANY of the three (e.g. the exact
        ``UnicodeEncodeError`` the encoding fix closes, or any other write
        failure) propagated out of ``process_once`` and aborted the WHOLE
        pass for that channel — including ``pop_pending_commands`` below,
        so every queued command (takeover, stop, ...) sat unprocessed too,
        not just the poll that failed. Guarding each call the same way the
        command loop below already guards each command means one poll's
        write failure can never again take the rest of the pass down with
        it — the failing poll is simply retried next tick (each is
        idempotent against durable state), exactly like a failed command is
        simply reissued.
        """

        # Poll the HLS relay child BEFORE the main worker so a relay death is
        # already reflected in _hls_relay_dead by the time _poll_process's own
        # health append (below) calls _sink_connected this same tick (MAJOR M1).
        # Order is preserved across the guard: each call still runs only after
        # the previous one completed (successfully or not), never in parallel.
        # F1 redesign: _poll_reload_settlement checks reload-status.json for a
        # channel with an armed-but-not-yet-settled content-reload -- a no-op
        # for every other channel (it returns immediately when there is no
        # pending entry).
        # BETA.10 U16 item B: the output A/V guard runs LAST, deliberately. It
        # judges the state row, so it must see the row the polls above just
        # wrote -- in particular ``_poll_process`` rewrites it every tick and
        # publishes ON_AIR / FALLBACK_SLATE / TRANSITIONING / DRAINING from the
        # channel's own live condition. Running last also means a worker the
        # guard kills at the end of one tick is already replaced (or is being
        # replaced) by the time the next tick's guard pass looks at it.
        for poll in (
            self._poll_hls_relay,
            self._poll_process,
            self._service_backoff_relaunch,
            self._poll_reload_settlement,
            self._poll_output_av_guard,
            # BETA.10 U30: the relay-self-heal failure escalation goes after the
            # A/V guard, for the same reason the A/V guard goes last -- it
            # judges a settled channel state, so it must see the row the polls
            # above just wrote. It is also strictly downstream of
            # ``_poll_hls_relay`` in the same tick: that is the poll that calls
            # the self-heal whose failure this one escalates, so ordering them
            # this way means a heal performed this tick is observable by the
            # escalation immediately rather than a tick late.
            self._poll_freeze_escalation,
            # A budget-exhausted quality recovery keeps a channel on its
            # fallback slate for one rolling-hour cooldown, then issues one
            # program retry. It runs after both detectors so either guard can
            # create the latch in this same tick.
            self._poll_quality_recovery,
            # BETA.10 U67: the reload-stall watchdog goes after everything else,
            # for the same reason the A/V guard and the freeze escalation do --
            # it judges the settled state row (it needs ``_poll_process``'s
            # TRANSITIONING write from this same tick to have already happened)
            # and nothing downstream may be left looking at a channel it just
            # decided to restart. It is a no-op unless the channel is carrying a
            # ``_pending_reloads`` pin -- i.e. unless a seamless hand-off has
            # already failed -- so a normal 690s-lead rollover never reaches it.
            self._poll_reload_stall_watchdog,
        ):
            try:
                poll(channel_id)
            except Exception:
                _LOG.exception(
                    "channel %s: %s failed this process_once tick; the pass continues "
                    "(command draining below still runs, and every other poll for this "
                    "channel still runs). Will be retried next tick.",
                    channel_id,
                    poll.__name__,
                )
        commands_processed = self._process_queued_commands(channel_id)
        # Consume Stop/new intent before committing any completed preparation.
        try:
            self._poll_preparation(channel_id)
        except Exception:
            _LOG.exception("channel %s: prepared operation failed", channel_id)
        return commands_processed

    def process_commands_only(self, channel_id: str) -> int:
        """Drain one queued command batch without polling or recovering workers.

        Channel automation uses this for explicit terminal intent on disabled
        channels, which are deliberately excluded from its normal poll pass.
        """
        return self._process_queued_commands(channel_id)

    def _process_queued_commands(self, channel_id: str) -> int:
        commands = self._store.pop_pending_commands(channel_id)
        for command in commands:
            try:
                self._process_command(command)
            except Exception as exc:
                _LOG.exception(
                    "Egress command %s (%s) failed for channel %s; it will NOT be "
                    "retried (already marked consumed by pop_pending_commands) but "
                    "every other queued command for this channel this pass still runs. "
                    "Reissue the failed command if it was still needed.",
                    command.command_id,
                    command.action,
                    channel_id,
                )
                if self._command_failure_hook is not None:
                    try:
                        self._command_failure_hook(channel_id, command, exc)
                    except Exception:  # the hook must never break command draining
                        _LOG.exception(
                            "command_failure_hook itself raised for channel %s; continuing.",
                            channel_id,
                        )
        return len(commands)

    def has_live_process(self, channel_id: str) -> bool:
        """True when THIS daemon instance tracks a live encoder process.

        CA-2 channel automation uses this for the auto-start pass: a flagged
        channel with no live process (fresh app start, or a stop the operator
        issued) is a candidate for a re-issued start command.
        """

        process = self._processes.get(channel_id)
        return process is not None and _process_poll(process) is None

    #: Persisted states that claim a live encoder may be sitting on air. Only
    #: Persisted states that claim a live encoder AND legitimately mean "this
    #: channel should be on air", so a dead PID is a recoverable fault.
    #: DRAINING is deliberately EXCLUDED: ``_drain`` sets it for explicit
    #: operator/supervisor OFF-AIR intent (see ``_drain``/``_poll_process``),
    #: so a restart that finds a dead pid mid-drain must stay off air -- never
    #: auto-start a channel the operator meant to stop. STOPPED/FALLBACK_SLATE
    #: are likewise terminal/deliberate and never reconciled.
    _STALE_RECONCILE_STATES: ClassVar[frozenset[str]] = frozenset(
        {"ON_AIR", "STARTING", "TRANSITIONING"}
    )

    def reconcile_stale_state(self) -> list[str]:
        """STARTUP sweep: clear persisted on-air claims with a DEAD encoder.

        Live defect (2026-09-24): after a supervisor restart, ``egress_states``
        kept reading ON_AIR for three channels whose encoder PIDs no longer
        existed, and the HLS playlists stayed frozen. The daemon's
        ``_processes`` map is in-memory, so a fresh instance tracks no encoder;
        recovery is normally driven by ``ChannelAutomationService`` re-issuing
        ``start`` for a channel with no live process -- but only for
        ``auto_start`` channels, so a persisted-ON_AIR channel that is NOT
        auto_start was never reconciled and its stale row was trusted forever.

        For each enabled channel whose persisted row claims an on-air state AND
        whose encoder is *definitively gone* (see ``_encoder_is_gone``), it
        atomically:

        * clears the stale claim by writing STOPPED, and
        * enqueues a ``start`` command issued by ``restart-recovery``,

        via ONE store transaction (``store.recover_stale_state(...)``) so there
        is no crash window in which STOPPED exists with no start queued -- the
        hazard that would otherwise strand the channel dark because STOPPED is
        not itself reconciled.

        This is the STARTUP arm, so it also clears a claim that carries NO pid
        at all: a crash before the pid was recorded leaves STARTING/ON_AIR with
        ``pid=None``, and a just-started daemon provably owns no encoder for any
        channel. The per-pass arm (:meth:`reconcile_dead_encoders`) deliberately
        does not, because on a running daemon that shape can be its own work.

        Safety properties:

        * a LIVE pid is never touched, and neither is a pid whose identity
          cannot be read -- fail closed, leave the row;
        * a channel this daemon tracks is never touched, even if its worker has
          already exited (``_poll_process`` owns that transition);
        * STOPPED / FALLBACK_SLATE / DRAINING rows are never touched;
        * the recovery command id is UNIQUE per attempt (and <=120 chars), so a
          LATER restart queues a fresh start (a constant id would be skipped
          forever by the SQL store's duplicate-id guard);
        * idempotent within one attempt: after recovery the row is STOPPED, so
          a re-run reports nothing and queues nothing.
        """
        return self._reconcile_stale_claims(reconcile_pidless_claims=True)

    def reconcile_dead_encoders(self) -> list[str]:
        """PER-PASS sweep: clear persisted on-air claims whose encoder is GONE.

        The startup arm above runs once. A row whose encoder was still alive at
        that instant and died afterwards is invisible to it: ``_poll_process``
        returns immediately for a channel with no *tracked* process, and a
        channel that is not ``auto_start`` has no other supervisor -- so the
        frozen-HLS-with-a-stale-ON_AIR-row state returns with no restart at all.
        This arm runs on every automation pass and reconciles those rows with
        the same atomic store operation.

        It reconciles only pid-BEARING claims: a pid-less claim on a running
        daemon can be this daemon's own in-flight work (see
        ``_reconcile_stale_claims``). Returns the channel ids recovered.
        """
        return self._reconcile_stale_claims(reconcile_pidless_claims=False)

    def _reconcile_stale_claims(self, *, reconcile_pidless_claims: bool) -> list[str]:
        """Shared body of the two sweeps; see their docstrings for the contract."""

        recovered: list[str] = []
        for config in self._store.list_configs():
            if not config.enabled:
                continue
            channel_id = config.channel_id
            if self.has_operator_stop_intent(channel_id) or self._has_pending_terminal_command(
                channel_id
            ):
                continue
            if channel_id in self._processes:
                # This daemon OWNS a worker for the channel -- and a worker that
                # has ALREADY exited stays owned: ``_poll_process`` is what sees
                # that exit and owns the crash relaunch, its back-off latch and
                # its fallback escalation, all of which key off the persisted row
                # still reading ON_AIR. Reconciling the row here would race that
                # path and replace its pacing with a bare start on the same tick.
                # (This subsumes the ``has_live_process`` short-circuit the first
                # cut used, which let a just-exited worker through: it reported
                # no live process, so the sweep cleared a row it did not own.)
                continue
            state = self._store.read_state(channel_id)
            if state is None or state.state not in self._STALE_RECONCILE_STATES:
                continue
            if state.pid is None:
                if not reconcile_pidless_claims:
                    # On a RUNNING daemon a pid-less claim can be this daemon's
                    # own in-flight work: ``_relaunch_after_crash``'s deferred
                    # (back-off) branch persists exactly this shape -- STARTING,
                    # no pid, no tracked process -- for the whole crash-loop
                    # cooldown, and ``_service_backoff_relaunch`` is what services
                    # it. Only the startup sweep, where a fresh instance provably
                    # owns no encoder, may clear it.
                    continue
                detail = "carried no encoder pid"
            else:
                if not self._encoder_is_gone(state):
                    continue
                detail = f"referenced dead encoder pid {state.pid}"
            command = EgressCommand(
                channel_id=channel_id,
                action="start",
                issued_at=datetime.now(UTC),
                issued_by="restart-recovery",
                # Unique per attempt, bounded: the SQL store skips a repeated
                # command_id forever, so a constant id would make a second
                # restart queue nothing (permanently dark). uuid4().hex is 32
                # chars; the prefix keeps it well under the 120-char cap.
                #
                # U53: the id also CARRIES the state this sweep is clearing.
                # The row read above is the only place that state exists -- the
                # transaction below writes STOPPED over it, and the start is
                # drained a poll later -- but the dispatch needs it, because a
                # channel that WAS on air must air the slate before it conforms
                # a program (see ``restart_recovery_previous_state``). Read back
                # by ``_process_command``.
                command_id=f"{RESTART_RECOVERY_COMMAND_PREFIX}{state.state}-{uuid.uuid4().hex}",
            )
            self._store.recover_stale_state(
                EgressStateRow(
                    channel_id=channel_id,
                    state="STOPPED",
                    current_source_label=state.current_source_label,
                    updated_at=datetime.now(UTC),
                    pid=None,
                    last_error=(
                        f"restart recovery: persisted {state.state} claim {detail}; "
                        "cleared stale claim and queued a start"
                    ),
                ),
                command,
            )
            _LOG.warning(
                "channel %s: cleared stale persisted %s claim (%s) and "
                "atomically queued restart-recovery start %s.",
                channel_id,
                state.state,
                detail,
                command.command_id,
            )
            recovered.append(channel_id)
        return recovered

    def _encoder_is_gone(self, state: EgressStateRow) -> bool:
        """True when the encoder the persisted row describes is definitively GONE.

        ``_pid_is_dead`` answers only "does SOME process hold this pid?". After a
        reboot Windows hands a dead encoder's pid to an unrelated process, so the
        stale row's pid reads as ALIVE and the channel is never recovered -- the
        exact 2026-09-24 shape. The question is "is this OUR encoder?", and the
        identity evidence available is AGE: ``_write_state`` stamps
        ``updated_at`` while ``_poll_process`` is polling a LIVE encoder, so a
        process holding the pid that was CREATED AFTER that instant cannot be the
        encoder the row describes.

        The image name is NOT usable evidence here: the encoder worker runs as
        ``<interpreter> <worker-script> <graph> <control-channel>``
        (``gst/strategy.py``), so its image name is shared with the control plane
        and with every other job (unlike the ffmpeg-only orphan probe in
        ``_reap_orphan``, which can and does check the name).

        Fails CLOSED on every uncertainty:

        * liveness ``None`` (AccessDenied, psutil error) -> not gone;
        * the pid is held by a process whose identity cannot be read (it exited
          between the two probes, or access is denied) -> not gone;
        * a process created BEFORE the row's own last write -> that is ours, and
          is never touched.
        """

        pid = state.pid
        if pid is None:  # pragma: no cover - callers gate on this
            return False
        dead = self._pid_is_dead(pid)
        if dead is None:
            # Unknown (AccessDenied / psutil error): fail closed.
            return False
        if dead:
            return True
        info = self._orphan_probe(pid)
        if info is None:
            # The pid exists but who holds it is unknowable: fail closed.
            return False
        return info.created_at > _epoch_seconds(state.updated_at)

    def has_manual_override(self, channel_id: str) -> bool:
        """True while an operator override (live takeover / forced fallback slate)
        is active for this channel. The base daemon has no notion of either — only
        ``PlayoutSupervisor`` (civiccast/egress/supervisor.py) does — so this
        default is always False, and ``PlayoutSupervisor`` overrides it. Consulted
        by channel-automation's plan-rollover pass (B1 fix: neither an operator
        force-slate nor a live takeover writes a state-row transition the rollover
        check could otherwise key off) and by ``_try_content_reload`` below (B3
        fix: whether a reload may defer its selector switch to the outgoing leg's
        EOS — never while an override is active, see ``reload_policy.
        should_defer_switch``)."""

        return False

    def has_operator_stop_intent(self, channel_id: str) -> bool:
        """Whether a human-issued stop/drain remains authoritative this session."""

        return channel_id in self._operator_stopped_channels

    @staticmethod
    def _is_operator_command(command: EgressCommand) -> bool:
        """Separate direct staff actions from supervisor-generated commands."""

        return not (
            command.issued_by in {"channel-automation", "restart-recovery"}
            or command.command_id.startswith(
                (SLATE_RESTART_COMMAND_PREFIX, RESTART_RECOVERY_COMMAND_PREFIX, "commit-start-")
            )
        )

    def _has_pending_terminal_command(self, channel_id: str) -> bool:
        return any(
            command.action in {"stop", "drain"}
            for command in self._store.peek_pending_commands(channel_id)
        )

    def _quality_guard_deferred(self, channel_id: str) -> bool:
        return (
            channel_id in self._quality_recovery
            or self.has_operator_stop_intent(channel_id)
            or self._has_pending_terminal_command(channel_id)
        )

    def _begin_quality_recovery(
        self,
        channel_id: str,
        *,
        restarted_at: list[float],
        now: float,
        detail: str,
    ) -> None:
        """Use the existing slate for one cooldown, then allow a bounded retry."""

        if self._quality_guard_deferred(channel_id):
            return
        spent_restarts = [
            at
            for timestamps in (
                restarted_at,
                self._output_av_guard[channel_id].restarted_at
                if channel_id in self._output_av_guard
                else [],
                self._freeze_escalation[channel_id].restarted_at
                if channel_id in self._freeze_escalation
                else [],
            )
            for at in timestamps
            if at > now - _QUALITY_RECOVERY_COOLDOWN_S
        ]
        retry_at = max(spent_restarts, default=now) + _QUALITY_RECOVERY_COOLDOWN_S
        reason = f"automatic quality recovery cooldown: {detail}"
        recovery = _QualityRecoveryState(retry_at=retry_at, reason=reason)
        self._quality_recovery[channel_id] = recovery
        if self.has_manual_override(channel_id):
            return
        request = getattr(self, "request_fallback_slate", None)
        if not callable(request):
            _LOG.error(
                "channel %s: quality recovery budget exhausted; no fallback-slate capability "
                "is available, so retry is deferred until the restart cooldown expires.",
                channel_id,
            )
            return
        try:
            request(channel_id=channel_id, reason=reason)
        except Exception:
            # The cooldown itself is the bound. Do not call a failing slate
            # transition again on every process tick.
            _LOG.exception(
                "channel %s: fallback-slate request failed; quality recovery remains in its "
                "cooldown until %.1f.",
                channel_id,
                retry_at,
            )

    def _clear_quality_recovery(self, channel_id: str, *, clear_slate: bool) -> None:
        recovery = self._quality_recovery.pop(channel_id, None)
        if recovery is None or not clear_slate:
            return
        clear = getattr(self, "clear_fallback_slate", None)
        if not callable(clear):
            return
        try:
            clear(channel_id=channel_id, reason=recovery.reason)
        except Exception:
            _LOG.exception(
                "channel %s: could not clear the quality-recovery slate before terminal intent.",
                channel_id,
            )

    def _poll_quality_recovery(self, channel_id: str) -> None:
        recovery = self._quality_recovery.get(channel_id)
        if recovery is None:
            return
        if self.has_operator_stop_intent(channel_id) or self._has_pending_terminal_command(
            channel_id
        ):
            self._clear_quality_recovery(channel_id, clear_slate=True)
            return
        now = self._monotonic()
        if now < recovery.retry_at:
            return

        # Remove the latch before external calls: an exception must not turn
        # the next two-second poll into another automatic program retry.
        self._quality_recovery.pop(channel_id, None)
        is_ours = getattr(self, "has_fallback_slate_reason", None)
        exit_slate = getattr(self, "request_slate_exit", None)
        try:
            if (
                callable(is_ours)
                and is_ours(channel_id=channel_id, reason=recovery.reason)
                and callable(exit_slate)
            ):
                exit_slate(channel_id=channel_id)
        except Exception:
            _LOG.exception(
                "channel %s: program retry after the quality-recovery cooldown failed; "
                "automatic recovery will not repeat until a new failure episode.",
                channel_id,
            )

        guard = self._output_av_guard.get(channel_id)
        if guard is not None:
            guard.streak = 0
            guard.offsets.clear()
            guard.last_probe_at = now
        freeze = self._freeze_escalation.get(channel_id)
        if freeze is not None:
            freeze.escalated_for_pid = None

    def dispatched_plan_horizon(
        self, channel_id: str
    ) -> tuple[str | None, tuple[float, ...], bool] | None:
        """``(proof_event_id, segment durations, switch_was_deferred)`` for the
        source plan this daemon most recently DISPATCHED to the encoder for
        ``channel_id`` -- or None if it has dispatched none.

        Exists because channel-automation's rollover pass has to know when the
        airing plan runs out, and the only honest source for that is the plan that
        was actually sent. It used to re-derive the horizon by calling the source
        plan provider AGAIN and summing whatever came back
        (``_check_plan_rollover``); that is a DIFFERENT plan -- the provider
        re-windows from the schedule item live at the moment of the call, capped at
        ``max_segments``, so the re-query's segment list is neither the one on air
        nor aligned to it, and the derived horizon can land the rollover trigger on
        (or past) the real end. Recorded at the two sites that actually dispatch:
        ``_start`` and ``_try_content_reload``.

        ``switch_was_deferred`` is what makes the horizon correct for a
        boundary-aligned rollover: that plan does NOT begin when it is dispatched:
        the engine holds it until the outgoing leg's own end
        (``reload_policy.should_defer_switch`` /
        ``GstPlayoutEngine.reload_program``), so its projected end runs from the
        OUTGOING plan's end, not from the dispatch instant."""

        return self._dispatched_plan_horizon.get(channel_id)

    def warm_source_plan(self, channel_id: str, plan: EgressSourcePlan) -> None:
        """Pre-conform every asset ``plan`` will air, off the air path (U60).

        Called by channel-automation's rollover look-ahead while the plan on
        air is still SHORTER than the rollover lead -- the one regime where the
        incoming boundary's synchronous preparation has less runway than its
        own whole-asset conform needs. Queues background work on the source
        preparer's warm worker and returns immediately; nothing here blocks a
        dispatch tick and nothing here can fail it.

        The config is read from the STORE, not from any cached/built config:
        the conform cache key is config-dependent (canonical profile, target
        and tolerance LUFS), so warming against a config the air path will not
        use would populate an entry nobody reads. A channel with no config, or
        a disabled one, is skipped -- there is no air path to warm for.
        """

        warmer = self._source_plan_warmer
        if warmer is None:
            return
        try:
            config = self._store.get_config(channel_id)
        except Exception:
            _LOG.exception(
                "U60 plan look-ahead warm could not read the config for %s; "
                "skipping the warm (the air path is unaffected).",
                channel_id,
            )
            return
        if config is None or not config.enabled:
            return
        try:
            warmer(config, plan)
        except Exception:
            # ``warm_plan`` is meant to be total; this is belt-and-braces so a
            # future warmer implementation cannot surface a fault on the
            # automation thread that dispatched the rollover.
            _LOG.exception(
                "U60 plan look-ahead warm failed for %s; the air path is "
                "unaffected and will prepare this plan synchronously.",
                channel_id,
            )

    def chained_slate_program_end(self, channel_id: str) -> datetime | None:
        """The projected end of the programme chained behind the filler in the plan
        this daemon most recently dispatched for ``channel_id``, or None.

        U53 item 1: a filler rollover plan may carry ``[filler..., programme...]``
        in one plan, in which case the state row legitimately stays
        ``FALLBACK_SLATE`` for the whole of that programme. Channel automation's
        slate-replan pass reads this so it does not cut the already-airing chained
        programme back to its own start.

        Returns None once a DIFFERENT plan has been dispatched (the chain is
        cleared at the same choke point that writes the horizon, and the record is
        validated against the horizon's proof event before it is believed), so a
        leg that failed and was relaunched can never leave a stale suppression
        behind."""

        record = self._chained_slate_program.get(channel_id)
        if record is None:
            return None
        proof_event_id, end_at = record
        horizon = self._dispatched_plan_horizon.get(channel_id)
        if horizon is None or horizon[0] != proof_event_id:
            # The horizon moved on (or was never written): the chain is stale.
            del self._chained_slate_program[channel_id]
            return None
        return end_at

    def _record_dispatched_plan(
        self,
        channel_id: str,
        *,
        proof_event_id: str | None,
        source_plan: EgressSourcePlan,
        switch_deferred: bool,
        chained_program_end_at: datetime | None = None,
    ) -> None:
        """Remember the plan just dispatched, keyed by the proof event written with
        it (so a consumer can tell "this is the plan now on air" from "this is a
        stale record"). See ``dispatched_plan_horizon``.

        ``chained_program_end_at`` (U53 item 1) is the projected end of the next
        scheduled programme when ``source_plan`` is a filler with that programme
        chained behind it; it is recorded -- or cleared -- here, together with the
        horizon, so the two can never disagree about which plan is on air."""

        self._dispatched_plan_horizon[channel_id] = (
            proof_event_id,
            tuple(float(segment.duration_seconds) for segment in source_plan.segments),
            switch_deferred,
        )
        first_segment = source_plan.segments[0]
        if (
            proof_event_id is not None
            and first_segment.kind == "program"
            and first_segment.source_ref is not None
            and first_segment.schedule_occurrence_id is not None
        ):
            self._dispatched_program_source[channel_id] = (
                proof_event_id,
                first_segment.schedule_occurrence_id,
                first_segment.source_ref,
                float(first_segment.inpoint_seconds or 0.0),
            )
        else:
            self._dispatched_program_source.pop(channel_id, None)
        if chained_program_end_at is None:
            self._chained_slate_program.pop(channel_id, None)
        else:
            self._chained_slate_program[channel_id] = (proof_event_id, chained_program_end_at)

    def emergency_presentation_ready(self, channel_id: str) -> bool:
        """Read-only admission check for operator emergency display requests."""
        ready = getattr(self._encoder_strategy, "emergency_presentation_ready", None)
        process = self._processes.get(channel_id)
        state = self._store.read_state(channel_id)
        return bool(
            callable(ready)
            and ready(channel_id)
            and process is not None
            and _process_poll(process) is None
            and state is not None
            and state.state == "ON_AIR"
        )

    def send_caption_cue(
        self,
        channel_id: str,
        work_dir: Path,
        *,
        text: str,
        pts_seconds: float,
        duration_seconds: float,
        delivery_id: str,
        program_token: str | None = None,
    ) -> bool:
        """Send a cue through the encoder strategy that owns the live worker.

        Native Windows keeps each worker's duplex named-pipe server on the
        strategy instance that started that worker.  A newly constructed
        strategy cannot reach those pipes, so the caption feed must delegate
        through this daemon's existing strategy instance.
        """

        if channel_id in self._caption_reset_failed:
            return False
        sender = getattr(self._encoder_strategy, "send_caption_cue", None)
        if not callable(sender):
            return False
        return bool(
            sender(
                channel_id,
                work_dir,
                text=text,
                pts_seconds=pts_seconds,
                duration_seconds=duration_seconds,
                delivery_id=delivery_id,
                **({"program_token": program_token} if program_token is not None else {}),
            )
        )

    def _process_command(self, command: EgressCommand) -> None:
        # An explicit operator/automation command supersedes any deferred crash
        # back-off relaunch still waiting on the latch: clear it so a start that
        # then ERRORs (e.g. an unresolved secret) can't be silently resurrected
        # later by the automatic relaunch the operator was trying to replace.
        self._backoff_relaunch.pop(command.channel_id, None)
        required_state = slate_restart_guard_state(command)
        if required_state is not None:
            # The headend-preset route's slate restart: run this half only
            # while the channel is still in the state the half assumes, so a
            # program that committed to air between the route's state read
            # and this drain is never cut (see SLATE_RESTART_COMMAND_PREFIX).
            row = self._store.read_state(command.channel_id)
            actual_state = row.state if row is not None else None
            if actual_state != required_state:
                _LOG.warning(
                    "channel %s: skipped slate-restart %s (%s): the channel is %s, not %s; "
                    "the preset stays saved and lands at the channel's next start.",
                    command.channel_id,
                    command.action,
                    command.command_id,
                    actual_state or "not running",
                    required_state,
                )
                return
        operator_command = self._is_operator_command(command)
        if command.action in {"stop", "drain"} and operator_command:
            self._operator_stopped_channels.add(command.channel_id)
        if command.action in {"stop", "drain"}:
            self._clear_quality_recovery(command.channel_id, clear_slate=True)
        if command.action == "start":
            config = self._store.get_config(command.channel_id)
            if config is not None and not config.enabled:
                _LOG.info(
                    "channel %s: ignoring queued start because the channel is disabled",
                    command.channel_id,
                )
                return
            if operator_command:
                self._operator_stopped_channels.discard(command.channel_id)
            elif self.has_operator_stop_intent(command.channel_id):
                _LOG.info(
                    "channel %s: ignoring automatic start %s while an operator stop is "
                    "active; an explicit start is required.",
                    command.channel_id,
                    command.command_id,
                )
                return
            # NOTE on ordering: the caption session-start hook is NOT called here.
            # It is invoked by ``_start_steps`` only on the branch where a NEW
            # session actually begins.  Calling it here ran the hook for a
            # DUPLICATE start on an already-live channel -- which is a no-op that
            # keeps the current writer running -- so its session-scoped cleanup
            # would have discarded the LIVE session's own audio (reproduced via
            # the daemon command path: the hook fired twice for start -> stop?
            # no: for an operator start followed by a duplicate start on a live
            # channel).  Firing it at the real transition is both correct and
            # sufficient, because at that point no writer for the new session
            # exists yet.
            # An operator start is a fresh intent: the slate-EOS relaunch cap
            # (see _SLATE_EOS_RELAUNCH_MAX_CONSECUTIVE) starts over for it.
            self._slate_eos_relaunches.pop(command.channel_id, None)
            # U53: a recovery start relaunches a channel the sweeps found
            # claiming to be ON AIR, and the command id carries the state the
            # sweep cleared (``restart_recovery_previous_state``). Handing it
            # to ``_start`` is what lets U47's slate-first branch fire here --
            # that branch is gated on ``previous_state`` being an active state,
            # so a plain ``_start(channel_id)`` could never take it, and the
            # channel stayed dark for the whole conform (6 m 55 s, measured).
            # A non-recovery start parses to ``None`` and is unchanged.
            recovered_state = restart_recovery_previous_state(command)
            self._start(
                command.channel_id,
                previous_state=recovered_state,
                slate_first=recovered_state is not None,
            )
            return
        if command.action == "stop":
            self._stop(command.channel_id, draining=False)
            if self._ts_relay is not None:
                # Operator stop = the channel is leaving air; the relay's
                # session ends WITH the channel (relaunches never come here).
                self._ts_relay.stop_channel(command.channel_id)
            if self._hls_relay is not None:
                self._hls_relay.stop_channel(command.channel_id)
            self._discard_stale_hls_playlists(command.channel_id)
            self._write_state(command.channel_id, "STOPPED")
            return
        if command.action == "drain":
            self._drain(command.channel_id)
            return
        if command.action == "reload":
            config = self._store.get_config(command.channel_id)
            if config is not None and not config.enabled:
                recorded_rollover = self._rollover_plan_end_at.get(command.channel_id)
                if recorded_rollover is not None and recorded_rollover[0] == command.command_id:
                    # Preserve _request_reload's matching-command cleanup even
                    # though a disabled channel must not enter its restart path.
                    self._rollover_plan_end_at.pop(command.channel_id, None)
                _LOG.info(
                    "channel %s: ignoring queued reload because the channel is disabled",
                    command.channel_id,
                )
                return
            self._request_reload(command.channel_id, command_id=command.command_id)
            return
        raise ConfigInvalidError(f"unsupported egress command action: {command.action}")

    def _discard_stale_hls_playlists(
        self, channel_id: str, *, config: EgressConfig | None = None
    ) -> None:
        """Remove ``playlist.m3u8`` from each of the channel's ``hls`` sink folders.

        The live-HLS writers (``hls_relay`` / ``sinks.HlsSink``) rewrite the
        playlist in place and never remove it, so once a channel has aired
        once the file exists forever -- and ``/api/public/live/current``
        advertises the manifest URL on the strength of that file existing
        (``live.router._local_live_manifest_url``). Without this, every start
        after the first advertised the PREVIOUS broadcast's playlist at t=0
        (review round 3 delta, MINOR 3). Called on an operator ``stop`` (the
        writer is gone with the channel) and on a start that finds no live
        relay. Segments are left for the next writer's own rolling window.
        Folders that fail containment are skipped: the API never advertises
        those anyway. This is the one consumer of the resolver that WRITES,
        so it bypasses the resolver's memo: a folder swapped for a junction
        inside the TTL is refused here as it would be uncached, never
        followed to an ``unlink`` outside the root (review round 4 delta,
        MINOR 5). It runs on a stop or a start, never on a hot path.
        """
        from civiccast.egress.headend import resolve_local_hls_directory

        if config is None:
            config = self._store.get_config(channel_id)
        if config is None:
            return
        for sink in config.sinks:
            if sink.kind != "hls":
                continue
            try:
                playlist = resolve_local_hls_directory(sink.uri, use_cache=False) / "playlist.m3u8"
            except ValueError:
                continue
            try:
                playlist.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                _LOG.warning(
                    "channel %s: could not remove the previous broadcast's %s (%s); residents "
                    "may be offered it until the new pipeline overwrites it.",
                    channel_id,
                    playlist,
                    exc,
                )
                continue
            _LOG.info(
                "channel %s: removed the previous broadcast's %s so the web preview is not "
                "advertised until the new pipeline writes it.",
                channel_id,
                playlist,
            )

    def _start(
        self,
        channel_id: str,
        *,
        previous_state: str | None = None,
        previous_source_label: str | None = None,
        force_fallback_slate: bool = False,
        force_fallback_reason: str | None = None,
        resolved_plan: EgressSourcePlan | None = None,
        plan_resolution_started_at: float | None = None,
        prepared_reload: _ReusePreparedPlan | None = None,
        slate_first: bool = False,
    ) -> None:
        with self._preparation_guard:
            if channel_id in self._preparations:
                # F3(b): a newer operation already owns this channel's
                # preparation slot, so this restart never runs and the plan it
                # was handed will not be aired. Release it here rather than let
                # a caller-held plan leak past its restart (the caller has
                # already dropped its own reference).
                self._release_prepared_restart_plan(
                    channel_id, prepared_reload, reason="a newer preparation owns this channel"
                )
                return
            try:
                result = self._drive_preparation(
                    channel_id,
                    self._start_steps(
                        channel_id,
                        previous_state=previous_state,
                        previous_source_label=previous_source_label,
                        force_fallback_slate=force_fallback_slate,
                        force_fallback_reason=force_fallback_reason,
                        resolved_plan=resolved_plan,
                        prepared_reload=prepared_reload,
                        slate_first=slate_first,
                        plan_resolution_started_at=(
                            self._monotonic()
                            if plan_resolution_started_at is None
                            else plan_resolution_started_at
                        ),
                    ),
                    kind="start",
                )
            except BaseException:
                # F3(b): a failure BEFORE the reused plan is bound (no config,
                # plan resolution, an encoder start that raises) would drop a
                # caller-supplied report's directory with no owner at all --
                # the steps' own release paths only cover what they have
                # already bound. Release, then propagate unchanged.
                self._release_prepared_restart_plan(
                    channel_id,
                    prepared_reload,
                    reason="start failed before the reused plan was bound",
                )
                raise
            if result is _PreparationState.START_EXPIRED:
                self._start(
                    channel_id,
                    force_fallback_slate=True,
                    force_fallback_reason=_START_EXPIRED_FALLBACK_REASON,
                )
            elif result is _PreparationState.SLATE_FIRST:
                # U47: the channel is already up on the slate and only the
                # program is outstanding. Hand it in through the ordinary reload
                # path -- NOT through ``_start``, which would tear down the very
                # worker this start just launched. The hand-off deliberately does
                # not run here: ``_drive_preparation`` in the ASYNC path returns
                # at the QUEUE, so the program's preparation belongs to a later
                # tick and ``_poll_preparation`` dispatches it (see there).
                self._hand_off_slate_first_program(channel_id)

    def _start_steps(
        self,
        channel_id: str,
        *,
        previous_state: str | None = None,
        previous_source_label: str | None = None,
        force_fallback_slate: bool = False,
        force_fallback_reason: str | None = None,
        resolved_plan: EgressSourcePlan | None = None,
        prepared_reload: _ReusePreparedPlan | None = None,
        slate_first: bool = False,
        plan_resolution_started_at: float,
    ) -> _PreparationSteps:
        # ``resolved_plan``: a program plan the caller ALREADY resolved from
        # ``source_plan_provider`` this same tick (the slate-EOS relaunch
        # probes for a due program before deciding to relaunch). Threaded in
        # so the provider is not asked twice -- under
        # PlayoutSupervisor._next_source_plan a second call pops (and
        # refills) _source_lookahead, running the DB lookahead twice for one
        # start. Ignored when ``force_fallback_slate`` is set (the caller has
        # already decided the program is not to be trusted).
        if prepared_reload is not None:
            # F3(b): the plan this start must air was ALREADY resolved and
            # conformed by the reload that just declined the in-place swap.
            # Presenting it as the resolved plan is what keeps the provider
            # from being consulted again (and its lookahead popped) for a plan
            # this start will not use -- and what makes the "no valid source
            # plan available" aborts below unreachable while a perfectly good
            # prepared plan is in hand. The reuse branch further down binds it
            # for real (with the reload's own target state).
            resolved_plan = prepared_reload.report.source_plan
        try:
            config = self._store.get_config(channel_id)
            if config is None:
                raise ConfigInvalidError("No egress configuration exists for this channel.")
            if not config.enabled:
                raise ConfigInvalidError("Egress is disabled for this channel.")
            if self._ts_relay is not None:
                # #151: route udp-ts sinks through the channel-lifetime relay so
                # this (re)launch splices into ONE continuous mux session.
                config = self._ts_relay.apply(config)
            # Was anything writing this channel's HLS window when this start
            # began? Read BEFORE the U21 new-session reset below tears the
            # previous relay child down. It answers a question the rebind does
            # not: "should whatever playlist.m3u8 is on disk be deleted, or
            # carried forward?" A relay that WAS alive leaves a window its
            # replacement continues (``delete_segments+append_list``), so
            # residents keep a manifest across an encoder crash-relaunch; a
            # playlist whose writer is already gone belongs to a previous
            # broadcast and must not be advertised.
            hls_relay_was_alive = (
                self._hls_relay is not None and self._hls_relay.is_alive(channel_id) is True
            )
            # Is a worker still alive and owning this channel right now? Decided
            # BEFORE ``apply`` so the relay reset and the short-circuit below
            # agree on the same fact: a start that is a NO-OP must not disturb a
            # live worker's session -- and must not restart that session's relay
            # out from under it.
            existing_process = self._processes.get(channel_id)
            worker_already_live = (
                existing_process is not None and _process_poll(existing_process) is None
            )
            stored_config = config
            if self._hls_relay is not None:
                # DEFECT A: route hls sinks through the supervised ffmpeg relay
                # that actually writes segments + a manifest for this engine.
                # BETA.10 U21: every GENUINE worker (re)start -- first start,
                # crash relaunch, output-desync guard restart, slate->program
                # restart -- rebinds this channel's relay to the NEW session. A
                # relay child carries its own corrected timeline, so an inherited
                # child strands a PTS jump in the new worker's output as a
                # constant output A/V offset for the rest of that child's life.
                # U24: the relay child's stderr is captured next to the
                # channel's other egress logs (``<work_dir>/<channel>/logs``),
                # so a live incident leaves child-side evidence behind.
                config = self._hls_relay.apply(
                    config,
                    new_session=not worker_already_live,
                    log_root=self._work_dir,
                )
            if worker_already_live:
                # A duplicate START leaves the current plan and its observation
                # unchanged. Promoting FALLBACK_SLATE to ON_AIR here strands an
                # unbounded slate: automation no longer requests the due program.
                # Clearing its proof ID also loses the actual dispatched horizon.
                # The regular worker poll already refreshes state and health.
                # F3(b): this start is a no-op (a live worker already owns the
                # channel), so the reuse branch below is never reached and a
                # carried plan would be neither aired nor released -- the
                # caller dropped its own reference when it handed it over.
                self._release_prepared_restart_plan(
                    channel_id, prepared_reload, reason="a live worker already owns this channel"
                )
                return None
            # This is a genuine fresh start: the worker that owned any prior
            # reload pin and deliberate-kill marker is no longer live. Clear
            # that worker's restart bookkeeping now, after the live-worker
            # no-op above, so it cannot mark this new session TRANSITIONING.
            self._pending_reloads.pop(channel_id, None)
            self._reload_kills.discard(channel_id)
            self._reload_kill_reasons.pop(channel_id, None)
            self._reload_stall_since.pop(channel_id, None)
            self._reload_stall_bound_s.pop(channel_id, None)
            self._reload_stall_rungs.pop(channel_id, None)
            held_prepared_reload = self._prepared_restart_plans.pop(channel_id, None)
            if held_prepared_reload is not None and held_prepared_reload is not prepared_reload:
                # A different caller-held plan superseded this abandoned one.
                # Keep ``prepared_reload`` alive: this fresh start owns it.
                self._release_prepared_restart_plan(
                    channel_id,
                    held_prepared_reload,
                    reason="a fresh start superseded the held restart plan",
                )
            # Hostile-review follow-up, items 1 & 4: reaching here means either
            # no process was tracked at all, or the tracked one has ALREADY
            # exited (the guard above only returns early while it is still
            # alive) -- some callers reach _start directly on a dead process
            # without going through _poll_process (e.g. _request_reload's own
            # poll check), so this is the other place that exit must be
            # recognized. Any armed-but-unsettled reload for the OLD process
            # is moot, and the OLD active plan is no longer being read by
            # anything -- reclaim both before starting fresh.
            self._discard_pending_reload_settlement(channel_id, reason="channel restarting")
            self._discard_active_prepared_plan_dir(channel_id)
            self._reap_orphan(channel_id)
            # A GENUINE new session begins here: this is past the "existing
            # process still alive" short-circuit (which returned above) and past
            # the dead-process reap, and before any new pipeline/writer is built.
            # The caption session-start hook therefore runs exactly once per real
            # transition -- so its session-scoped cleanup can never discard the
            # audio of a LIVE session, and every chunk present at this instant
            # belongs to a previous session rather than the one about to start.
            # Best-effort: a caption-sidecar failure must never block broadcast.
            if self._channel_start_hook is not None:
                try:
                    self._channel_start_hook(channel_id)
                except Exception:
                    self._caption_reset_failed.add(channel_id)
                    _LOG.exception(
                        "channel %s: caption session-start hook failed; captions disabled "
                        "until a successful new session reset; continuing broadcast",
                        channel_id,
                    )
                else:
                    self._caption_reset_failed.discard(channel_id)
            # From here on a pipeline is being BUILT from ``stored_config``.
            self._built_configs[channel_id] = stored_config
            if not hls_relay_was_alive:
                # No relay was writing this channel's HLS window when this
                # start began (a fresh daemon, an unclean death, or a native
                # HlsSink worker that is gone): whatever playlist.m3u8 is on
                # disk belongs to a previous broadcast. Remove it before the
                # new writer produces anything so /api/public/live/current does
                # not advertise it. A relay that WAS already alive leaves a
                # window its replacement continues across the U21 rebind
                # (``apply(..., new_session=True)`` above), so that window is
                # carried forward rather than deleted -- the manifest survives
                # an encoder crash-relaunch. Uses the STORED config: ``apply``
                # above rewrote hls sinks to their relay's local-ts uri.
                self._discard_stale_hls_playlists(channel_id, config=stored_config)
            using_fallback_slate = False
            fallback_reason: str | None = None
            # U47: set only by the slate-first branch below, and read once at the
            # very end of the encoder-start block (after health is appended, so
            # the channel's own row/health record this start the same way any
            # other start does). It marks that the worker launched here is on the
            # SLATE and that a program preparation is still owed.
            slate_first_handoff = False
            if force_fallback_slate and self._fallback_source_provider is not None:
                # BLOCKER B1: the caller (a crash-relaunch that has never once
                # reached a healthy uptime — see _LIVE_SOURCE_FAILURE_FALLBACK_STREAK)
                # has already decided the configured source is unusable right now.
                # Go straight to the fallback-slate plan rather than re-resolving
                # (and re-trusting) the same source_plan_provider that keeps
                # producing a source the encoder can't stay attached to.
                fallback_reason = force_fallback_reason or (
                    "Live source failed repeatedly; aired fallback slate instead of "
                    "an infinite crash-loop."
                )
                self._write_state(channel_id, "FALLBACK_SLATE", last_error=fallback_reason)
                # Annotated because this is the FIRST binding of source_plan on this
                # branch path — see the matching note at the other first-binding
                # branch below for why the annotation matters to mypy.
                source_plan: EgressSourcePlan | None = self._fallback_source_provider(config)
                using_fallback_slate = True
                readiness = None
            elif (
                readiness := (
                    self._caption_readiness_provider(channel_id)
                    if self._caption_readiness_provider is not None
                    else None
                )
            ) is not None and not getattr(readiness, "ready", True):
                reason = getattr(readiness, "refusal_reason", None) or "caption-readiness-refused"
                fallback_reason = f"caption storage refused: {reason}"
                if not getattr(readiness, "requires_fallback_slate", False):
                    raise ConfigInvalidError(fallback_reason)
                if self._fallback_source_provider is None:
                    self._write_state(channel_id, "FALLBACK_SLATE", last_error=fallback_reason)
                    self._append_health(channel_id, "FALLBACK_SLATE", sink_connected={})
                    # F3(b): aborts before the reuse branch -- see the matching
                    # release at this method's live-worker early return.
                    self._release_prepared_restart_plan(
                        channel_id,
                        prepared_reload,
                        reason="caption storage refused and no fallback slate is configured",
                    )
                    return None
                self._write_state(channel_id, "FALLBACK_SLATE", last_error=fallback_reason)
                source_plan = self._fallback_source_provider(config)
                using_fallback_slate = True
            elif resolved_plan is not None:
                source_plan = resolved_plan
            elif (
                slate_first
                and previous_state in _SLATE_FIRST_ACTIVE_STATES
                and prepared_reload is None
                and self._fallback_source_provider is not None
                and self._preparation_executor is not None
            ):
                # U47: this channel was ALREADY ON AIR and its worker is gone or
                # being replaced. Whatever the schedule says, the channel must
                # not be dark for the 20-133 s a cold conform takes -- so air the
                # slate NOW and let the program follow through the reload path
                # (``_hand_off_slate_first_program``). The program's own plan is
                # deliberately NOT resolved here: the hand-off resolves it at
                # hand-off time, which is also the freshest read of the schedule
                # (a plan resolved before the slate aired could already be stale
                # by the time the slate is up).
                #
                # ``prepared_reload is None`` keeps F3(b)'s reuse restart on its
                # own path: that one already HAS the program conformed and must
                # not sit on the slate first.
                #
                # ``_preparation_executor is not None`` scopes this to the
                # asynchronous preparer -- ``enable_async_preparation``, which
                # the station's own automation turns on (automation.py), and the
                # path the live finding was measured on: there the start's
                # ``_PreparationRequest`` goes to the executor and ``_start``
                # returns with NO worker on air at all, so the channel is dark
                # for the whole conform.
                #
                # SCOPE, stated exactly, because it is NOT an implementation
                # limit: this branch would work without an executor too (the
                # slate worker is a separate PROCESS -- airing it before the
                # program's conform does not need a free daemon thread, and the
                # conform that follows blocks only this thread while the slate is
                # already on air). What it collides with there is a different,
                # deliberately pinned contract: the crash-loop escalation ladder
                # below. Airing the slate on the first crash-relaunch writes
                # FALLBACK_SLATE immediately and stops the program being retried,
                # which is 22 pinned tests -- `_LIVE_SOURCE_FAILURE_FALLBACK_STREAK`
                # (a source that never comes up is retried until streak >= 5 and
                # reaches the slate at a measured 405.0 s,
                # test_daemon_first_output_timeout_relaunch.py) and the never-
                # healthy latch it exists for ("dead air forever" -- see
                # ``_begin_relaunch``'s B1 note). Two committed rules, pointing
                # opposite ways for the same event.
                #
                # This unit therefore implements the rule where the finding was
                # measured and where the station runs (the executor path), keeps
                # the ladder intact everywhere else, and files the collision
                # itself as a product question (oversight questions/U47.md)
                # rather than picking a winner silently.
                #
                # B1 (the never-healthy crash-loop) deliberately wins over this
                # branch -- it is checked first above, and it also sets
                # ``using_fallback_slate``; a B1 relaunch is a channel that keeps
                # failing on its program, so it airs the slate and stops there
                # rather than immediately re-attempting the thing that is
                # crashing it.
                fallback_reason = _SLATE_FIRST_HANDOFF_REASON
                self._write_state(channel_id, "FALLBACK_SLATE", last_error=fallback_reason)
                # NOT annotated, unlike the B1 binding above: ``source_plan`` is one
                # variable for the whole method, so a second annotation here is a
                # mypy ``no-redef`` error rather than a second binding.
                source_plan = self._fallback_source_provider(config)
                using_fallback_slate = True
                slate_first_handoff = True
            else:
                try:
                    source_plan = self._source_plan_provider(channel_id)
                except SourcePrepareError as exc:
                    if self._fallback_source_provider is None:
                        raise
                    fallback_reason = str(exc)
                    self._write_state(channel_id, "FALLBACK_SLATE", last_error=fallback_reason)
                    source_plan = self._fallback_source_provider(config)
                    using_fallback_slate = True
            if source_plan is None:
                if self._fallback_source_provider is None:
                    self._write_state(
                        channel_id,
                        "FALLBACK_SLATE",
                        last_error=(
                            "No valid source plan is available. Slate generation is required "
                            "before this channel can go on air."
                        ),
                    )
                    self._append_health(channel_id, "FALLBACK_SLATE", sink_connected={})
                    return None
                fallback_reason = "No valid source plan is available; generated fallback slate."
                self._write_state(channel_id, "FALLBACK_SLATE", last_error=fallback_reason)
                source_plan = self._fallback_source_provider(config)
                using_fallback_slate = True
            if source_plan.channel_id != channel_id:
                raise ConfigInvalidError(
                    f"Source plan channel {source_plan.channel_id!r} does not match "
                    f"requested channel {channel_id!r}."
                )
            first_program_remaining_seconds = (
                source_plan.segments[0].duration_seconds
                if (
                    not using_fallback_slate
                    and self._boundary_source_plan_provider is not None
                    and not self.has_manual_override(channel_id)
                    and source_plan.segments[0].kind == "program"
                )
                else None
            )
            if first_program_remaining_seconds is not None and prepared_reload is None:
                # ``prepared_reload`` is excluded because that plan was already
                # resolved through ``_reload_steps`` -- which applies this same
                # floor -- and the reuse branch below re-binds the report's own
                # plan, so consulting the boundary here would be a wasted call
                # whose answer is discarded.
                #
                # U36 decision 2(ii): the relaunch path is never handed a
                # sub-floor schedule tail either. It was the same defect as the
                # rollover: on 2026-09-25 the public channel's 14:17:52 crash
                # relaunch re-resolved the July 23 program with the drifted
                # schedule clock and armed its single segment as the last few
                # seconds of media that had already fully aired -- the leg EOSed
                # immediately and the worker exited. The precondition above is
                # exactly the set of cases where a program plan was resolved
                # against a schedule boundary provider (not a slate, not an
                # operator override), which is where "the schedule is nearly
                # over" is a meaningful question at all -- and where the plan
                # about to be PREPARED is still swappable. ``_resolve_schedule_tail``
                # returns the plan unchanged, by identity, at or above the floor,
                # so an ordinary start never consults the boundary a second time.
                tail_outcome = self._resolve_schedule_tail(
                    channel_id, source_plan, config, slate_when_no_next=True
                )
                if tail_outcome.plan is not source_plan:
                    source_plan = tail_outcome.plan
                    using_fallback_slate = tail_outcome.slate
                    first_program_remaining_seconds = (
                        None if tail_outcome.slate else source_plan.segments[0].duration_seconds
                    )
            if not using_fallback_slate:
                # Clean-machine walkthrough of beta.5 (2026-09-09 MDT): an
                # operator Start at 22:00:39 still read Stopped at 22:00:41
                # and 22:00:47 and only flipped to ON AIR at 22:01:03 -- the
                # preparer below (a first-ever conform of the clip) ran for
                # ~20s with NO state written yet, so the row kept whatever it
                # said before (STOPPED). Publish STARTING with the target
                # source label BEFORE preparation so the screen never says
                # Stopped during a start. The early slate flips above already
                # wrote FALLBACK_SLATE with their own last_error -- leave those
                # in place while its worker is started.
                self._write_state(
                    channel_id,
                    "STARTING",
                    current_source_label=source_plan.segments[0].label,
                    pid=None,
                )
            # Hostile-review follow-up, item 4: None unless the preparer
            # actually reports a discrete per-plan directory for the plan
            # this worker ends up airing -- tracked into
            # _active_prepared_plan_dir below once the encoder actually
            # starts, so the path taken when a channel is opted out via
            # CIVICCAST_EGRESS_SEAMLESS_RELOAD=0 releases it too, instead of
            # relying solely on _try_content_reload's tracking (which never
            # runs at all while supports_content_reload is False).
            prepared_plan_dir: Path | None = None
            if prepared_reload is not None and using_fallback_slate:
                # F3(b): the resolution above REFUSED the prepared plan --
                # caption storage refused and demanded the fallback slate, so
                # this start must air what it resolved, not the plan it was
                # handed. Release the held plan (the caller dropped its own
                # reference) and fall through to the resolved fallback below.
                # ``resolved_plan`` above makes every OTHER
                # resolution failure mode unreachable here.
                self._release_prepared_restart_plan(
                    channel_id,
                    prepared_reload,
                    reason="this start must air its own resolved fallback slate",
                )
            if prepared_reload is not None and not using_fallback_slate:
                # F3(b): this start IS the restart of an interrupted
                # FALLBACK_SLATE reload, and the plan it was interrupted on
                # has already been conformed. Bind exactly what the yield
                # below would have produced, without yielding -- re-running
                # the preparer is the whole cost this path exists to avoid
                # (measured 133.6 s on 2026-09-24). The plan aired is the
                # report's own, per the owner-authorized option (a): if the
                # schedule horizon moved while the restart was in flight, the
                # next ordinary rollover corrects it.
                reused_report = prepared_reload.report
                source_plan = reused_report.source_plan
                prepared_plan_dir = reused_report.plan_dir
                # The state the SEAMLESS path would have published for this
                # same plan, carried rather than re-derived: a force-fallback
                # filler rollover (the one route to a non-deferred reload whose
                # prepared plan is the SLATE) must come back up as
                # FALLBACK_SLATE, not as ON_AIR over a slate plan.
                using_fallback_slate = prepared_reload.target_state == "FALLBACK_SLATE"
                self._record_prepared_loudness(channel_id, reused_report)
                _LOG.info(
                    "Restarting %s onto the plan its interrupted reload already prepared: "
                    "label=%r plan_dir=%s state=%s.",
                    channel_id,
                    source_plan.segments[0].label if source_plan.segments else "-",
                    prepared_plan_dir,
                    prepared_reload.target_state,
                )
            elif slate_first_handoff or (
                using_fallback_slate and _is_ready_generated_slate(source_plan)
            ):
                # The restart-recovery hand-off already airs its generated filler
                # directly. A cold start's built-in plain slate is equally ready:
                # SlateSourceGenerator renders the canonical profile, then extends
                # the horizon by stream copy. Re-conforming that hour-long silent
                # fill kept the worker absent until the encode finished or timed out.
                # Skip preparation only for this untrimmed generated slate; ordinary
                # programs, custom fallback providers and bulletin audio retain their
                # preparation/loudness handling. The slate-first program hand-off
                # still belongs to the async reload path after this worker starts.
                #
                # `prepared_plan_dir` stays None, so the reload path has no directory of
                # ours to release out from under the airing slate, and the loudness
                # cache is popped exactly as the `else` arm below pops it: nothing was
                # prepared here, so a stale pre-restart program loudness must not be
                # reported as this slate's.
                self._last_loudness_lufs.pop(channel_id, None)
            elif self._source_preparer is not None:
                try:
                    preparation_report = yield _PreparationRequest(source_plan, config)
                    source_plan = preparation_report.source_plan
                    prepared_plan_dir = preparation_report.plan_dir
                    self._record_prepared_loudness(channel_id, preparation_report)
                except SourcePrepareError as exc:
                    if self._fallback_source_provider is None:
                        raise
                    fallback_reason = str(exc)
                    self._write_state(channel_id, "FALLBACK_SLATE", last_error=fallback_reason)
                    source_plan = self._fallback_source_provider(config)
                    using_fallback_slate = True
                    self._last_loudness_lufs.pop(channel_id, None)
            else:
                self._last_loudness_lufs.pop(channel_id, None)

            # A cold conform can outlast a short scheduled item's remaining
            # window. Starting that categorically expired first program segment
            # guarantees stale-horizon recovery at startup. Do not compare plan
            # identity: adjacent schedule occurrences may use the same asset.
            # Release this unused prepared directory once and return a distinct
            # result; the caller starts one freshly prepared fallback operation.
            if (
                not using_fallback_slate
                and first_program_remaining_seconds is not None
                and (
                    self._monotonic() - plan_resolution_started_at
                    >= first_program_remaining_seconds
                )
            ):
                self._release_prepared_plan_dir(prepared_plan_dir)
                if self._fallback_source_provider is None:
                    raise SourcePrepareError(
                        "Scheduled source plan expired during preparation and no fallback "
                        "source is configured."
                    )
                _LOG.warning("channel %s: %s", channel_id, _START_EXPIRED_FALLBACK_REASON)
                return _PreparationState.START_EXPIRED

            # Hostile-review follow-up (third pass), P0: captured right here,
            # BEFORE the encoder-unavailable retry below gets a chance to flip
            # using_fallback_slate again -- this records whether the preparer
            # (just above, if required for this provider) ran against a plan
            # that was ALREADY the fallback slate (an early flip:
            # force_fallback_slate, caption-readiness
            # refusal, or source_plan is None, all above) versus the program
            # plan. The distinction matters because the tracking decision at
            # the end of this method used to release prepared_plan_dir on
            # ANY using_fallback_slate=True, including this early-flip case --
            # but there, prepared_plan_dir is the SLATE's own directory (the
            # preparer ran on source_plan after it was already reassigned to
            # the fallback plan), which the encoder is actively airing from.
            # Releasing it (rmtree) out from under a live airing was reviewer-
            # proven with a probe: prepared_for=['Fallback slate'], state
            # FALLBACK_SLATE pid=111, released=['plan-1'].
            prepared_for_fallback = using_fallback_slate

            # Hostile-review follow-up (second pass), items 1 & 2: everything
            # from here down to the tracking decision at the end of this
            # block can either raise (a provider hook, EncoderStartRequest
            # construction, the encoder itself) or can legitimately decide
            # NOT to use the prepared plan after all (the encoder-unavailable
            # retry falling back to slate, using_fallback_slate flips True
            # AFTER prepare() already succeeded) -- in EITHER case, if the
            # preparer minted a real prepared_plan_dir above, it must be
            # released here: nothing else ever looks at this local variable
            # again, so silently dropping the reference (the previous code)
            # or letting an exception skip past it were both permanent leaks,
            # recoverable only by age/budget GC, not this daemon's own faster
            # release.
            try:
                # Round-2 hostile review, item 5: _write_state REPLACES the
                # row, so this rewrite used to drop the label the pre-prepare
                # STARTING write above had just published; carry it (the
                # prepared plan's first segment -- the slate's own label when
                # the preparer fell back) through to the TRANSITIONING/ON_AIR
                # write below.
                self._write_state(
                    channel_id,
                    "STARTING",
                    current_source_label=source_plan.segments[0].label,
                    pid=None,
                )
                branding_plan = (
                    self._branding_plan_provider(channel_id)
                    if self._branding_plan_provider is not None
                    else None
                )
                caption_plan = (
                    self._caption_plan_provider(channel_id)
                    if self._caption_plan_provider is not None
                    and channel_id not in self._caption_reset_failed
                    else None
                )
                running_state: EgressState = "FALLBACK_SLATE" if using_fallback_slate else "ON_AIR"
                if previous_state in {"ON_AIR", "FALLBACK_SLATE"}:
                    transition_event = self._build_proof_event(
                        channel_id=channel_id,
                        state="TRANSITIONING",
                        source_plan=source_plan,
                        previous_state=previous_state,
                        previous_source_label=previous_source_label,
                    )
                    self._store.append_proof_event(transition_event)
                    self._write_state(
                        channel_id,
                        "TRANSITIONING",
                        current_source_label=source_plan.segments[0].label,
                        current_proof_event_id=transition_event.event_id,
                        pid=None,
                    )
                self._write_state(
                    channel_id,
                    running_state,
                    current_source_label=source_plan.segments[0].label,
                    pid=None,
                    last_error=fallback_reason if using_fallback_slate else None,
                )

                def _encoder_request(plan: EgressSourcePlan) -> EncoderStartRequest:
                    return EncoderStartRequest(
                        channel_id=channel_id,
                        source_plan=plan,
                        config=config,
                        work_dir=self._work_dir,
                        resolve_secret=self._resolve_secret,
                        branding_plan=branding_plan,
                        caption_plan=caption_plan,
                        captions_allowed=channel_id not in self._caption_reset_failed,
                        # Live caption tap (Beta B6, option A): env-configured
                        # audio fork rides the same encoder process.
                        audio_tap_plan=(
                            build_audio_tap_plan(channel_id)
                            if channel_id not in self._caption_reset_failed
                            else None
                        ),
                        ffmpeg_starter=self._ffmpeg_starter,
                        cg_overlay_image=(
                            self._cg_overlay_provider(channel_id, config)
                            if self._cg_overlay_provider is not None
                            else None
                        ),
                        board_live_video_layers=self._board_live_video_layers(channel_id, config),
                    )

                # Round-4 fix (PR #183 review, BLOCKER reproduced): snapshot
                # the channel's stderr log size BEFORE this spawn's
                # ``strategy.start()`` call, not after -- ``strategy.py`` /
                # ``_ffmpeg.py`` open a FIXED per-channel path in APPEND
                # mode, so this is exactly "how big was the log the moment
                # before this worker's own output could start landing in
                # it," which is what the offset anchor needs to mean.
                # Reading the size AFTER ``start()`` returns would already
                # include whatever this brand-new worker itself just wrote
                # (its subprocess's own real-world output has no such race
                # in production, but this ordering is correct either way and
                # removes even a theoretical one). Only reused if the new
                # spawn's stderr path is the SAME file as the previous
                # worker's (true in the overwhelmingly common case of an
                # unchanged encoder strategy) -- a mid-flight strategy
                # switch (the FfmpegNotFoundError independent-slate retry
                # below) gets a fresh path and starts its own offset at 0.
                previous_stderr_path = self._stderr_logs.get(channel_id)
                pre_spawn_stderr_size = (
                    _stderr_log_size(previous_stderr_path)
                    if previous_stderr_path is not None
                    else 0
                )
                pre_spawn_stderr_identity = (
                    _stderr_log_identity(previous_stderr_path)
                    if previous_stderr_path is not None
                    else None
                )
                try:
                    encoder_result = self._encoder_strategy.start(_encoder_request(source_plan))
                except (EncoderUnavailableError, FfmpegNotFoundError) as exc:
                    # Degraded-mode tier 4 (owner ruling: dead air is the cardinal
                    # sin, NEVER acceptable). The encoder itself could not start --
                    # e.g. GStreamer already fell back to FFmpeg and FFmpeg egress
                    # ALSO failed (EncoderUnavailableError), OR the FFmpeg tier
                    # itself has no ffmpeg binary on PATH (FfmpegNotFoundError --
                    # audit K2-1: this used to escape straight past this seam to
                    # the outer ERROR handler below, skipping the slate tier
                    # entirely). Both are tier failures of the SAME ladder, so
                    # both land here. Rather than dropping the channel to
                    # ERROR/black, put up the existing fallback SLATE ("technical
                    # difficulties") as the absolute last resort: rebuild the
                    # request against the slate source plan and try the encoder
                    # once more. Only ERROR (dead air) if there is no fallback
                    # provider, we were ALREADY airing the slate (so the slate
                    # itself is what failed), or the slate encode also fails --
                    # e.g. ffmpeg is genuinely absent from the machine, so no
                    # tier (program OR slate) can be encoded. That is the true
                    # zero-ffmpeg floor: the outer handler below still catches
                    # FfmpegNotFoundError and lands the channel on ERROR with
                    # last_error set and health appended -- a no-crash state with
                    # operator alerting, never a crash and never a silent hang.
                    if self._fallback_source_provider is None or using_fallback_slate:
                        raise
                    _LOG.error(
                        "channel %s: egress encoder unavailable (%s); falling back to the "
                        "technical-difficulties slate rather than dead air.",
                        channel_id,
                        exc,
                    )
                    fallback_reason = f"egress encoder unavailable; aired fallback slate: {exc}"
                    source_plan = self._fallback_source_provider(config)
                    using_fallback_slate = True
                    running_state = "FALLBACK_SLATE"
                    self._write_state(channel_id, running_state, last_error=fallback_reason)
                    # K2-1 follow-up (P1): FfmpegNotFoundError is a pure PATH lookup with
                    # no dependence on the source plan being encoded, so retrying THIS
                    # SAME strategy is guaranteed to fail identically again -- see
                    # _default_independent_slate_strategy's docstring. Route the slate
                    # retry through a genuinely separate, ffmpeg-independent encoder in
                    # that specific case. EncoderUnavailableError has no such guarantee
                    # (e.g. a hardware-encoder probe outcome can legitimately differ
                    # between the program and slate content), so it keeps retrying the
                    # original strategy unchanged.
                    retry_strategy = self._encoder_strategy
                    if isinstance(exc, FfmpegNotFoundError):
                        independent_strategy = self._independent_slate_strategy_factory()
                        if independent_strategy is not None:
                            retry_strategy = independent_strategy
                    encoder_result = retry_strategy.start(_encoder_request(source_plan))
                self._stderr_logs[channel_id] = encoder_result.stderr_path
                process = encoder_result.process
                self._processes[channel_id] = process
                self._started_at[channel_id] = self._monotonic()
                # Round-4 fix (PR #183 review, BLOCKER reproduced): anchor
                # this worker's on-air evidence scan to bytes at or after
                # THIS spawn -- see ``_stderr_spawn_offset``'s docstring for
                # why the append-mode log makes this necessary. Uses the
                # PRE-spawn snapshot taken above (not the log's current size
                # now, which may already include this worker's own output).
                self._stderr_spawn_offset[channel_id] = (
                    pre_spawn_stderr_size
                    if (
                        previous_stderr_path == encoder_result.stderr_path
                        and pre_spawn_stderr_identity is not None
                        and pre_spawn_stderr_identity
                        == _stderr_log_identity(encoder_result.stderr_path)
                        and _stderr_log_size(encoder_result.stderr_path) >= pre_spawn_stderr_size
                    )
                    else 0
                )
                self._stderr_spawn_identity[channel_id] = _stderr_log_identity(
                    encoder_result.stderr_path
                )
                # This IS a fresh spawn -- any on-air evidence latched for a
                # PREVIOUS worker no longer describes this one.
                self._on_air_confirmed_at.pop(channel_id, None)
                # Hostile-review follow-up, item 4: track the plan actually being
                # aired so it gets released on the next exit/stop even when the
                # channel is opted out via CIVICCAST_EGRESS_SEAMLESS_RELOAD=0
                # (supports_content_reload=False; the beta.5 default is ON) --
                # _try_content_reload/_commit_reload_settlement never run at
                # all on that path, so without this the only cleanup for a
                # _start()-launched plan was age/budget GC.
                if using_fallback_slate and not prepared_for_fallback:
                    # Item 1 fix, narrowed by the P0 fix above: only release
                    # when using_fallback_slate flipped True AFTER the
                    # preparer already ran (prepared_for_fallback is False) --
                    # the encoder-unavailable retry path just above, where
                    # prepared_plan_dir is the PROGRAM plan's directory, never
                    # actually dispatched to this encoder (a separately-built
                    # fallback slate plan aired instead). Release it.
                    self._release_prepared_plan_dir(prepared_plan_dir)
                elif prepared_plan_dir is not None:
                    # Covers both: a normal (non-fallback) start, AND an
                    # early-flip fallback slate (force_fallback_slate,
                    # caption-readiness refusal, or no source plan) where the
                    # preparer ran against the SLATE plan itself -- that
                    # directory is exactly what this encoder is airing from,
                    # so it must be tracked as active, not released.
                    self._active_prepared_plan_dir[channel_id] = prepared_plan_dir
            except Exception:
                # Item 2 fix: anything above that raises past this point --
                # a provider hook, EncoderStartRequest construction, an
                # encoder failure this method doesn't itself recover from --
                # must not leak prepared_plan_dir. Release, then propagate
                # unchanged (the existing outer except clauses below still
                # decide how the channel's STATE responds to this failure;
                # this inner handler only ever touches the plan directory).
                self._release_prepared_plan_dir(prepared_plan_dir)
                raise
            proof_event = self._build_proof_event(
                channel_id=channel_id,
                state=running_state,
                source_plan=source_plan,
                previous_state=previous_state,
                previous_source_label=previous_source_label,
            )
            self._store.append_proof_event(proof_event)
            self._record_as_run_transition(
                channel_id=channel_id,
                running_state=running_state,
                source_plan=source_plan,
                proof_event=proof_event,
            )
            self._sync_cg_overlay_proof(channel_id, running_state)
            self._write_state(
                channel_id,
                running_state,
                current_source_label=source_plan.segments[0].label,
                current_proof_event_id=proof_event.event_id,
                pid=_process_pid(process),
                last_error=fallback_reason if using_fallback_slate else None,
            )
            # A start puts its plan on air immediately (no deferred switch).
            self._record_dispatched_plan(
                channel_id,
                proof_event_id=proof_event.event_id,
                source_plan=source_plan,
                switch_deferred=False,
            )
            self._append_health(
                channel_id,
                running_state,
                sink_connected=self._sink_connected(channel_id, config, state=running_state),
                seconds_on_air=0,
            )
            if slate_first_handoff:
                # U47: the slate is airing and the channel is no longer dark, so
                # this operation is NOT complete -- it still owes the program its
                # caller asked for. Returned from inside the ``try`` so the two
                # ERROR handlers below keep their own ``return None``: a start
                # that FAILED to launch the slate has nothing to hand off, and
                # reporting SLATE_FIRST there would tell the caller a program is
                # on its way when no worker exists to put it on air.
                return _PreparationState.SLATE_FIRST
        except (ConfigInvalidError, SecretUnresolvedError, FfmpegNotFoundError) as exc:
            # FfmpegNotFoundError only reaches here when the ladder's own
            # tier-failure seam above already tried (or could not try) the
            # slate tier -- see the comment there. This is the documented
            # zero-ffmpeg floor: no crash, ERROR state with last_error set,
            # health appended, so the operator is alerted instead of the
            # channel silently hanging or the supervisor crashing.
            self._write_state(channel_id, "ERROR", last_error=str(exc))
            self._append_health(channel_id, "ERROR", sink_connected={})
            # Round 6 fix: ERROR is not on air -- a value recorded before this
            # start attempt (or one this attempt's own caller carried forward)
            # must not survive to feed a later, unrelated reload. See
            # ``_rollover_plan_end_at``'s docstring for the full route
            # inventory.
            self._rollover_plan_end_at.pop(channel_id, None)
        except EgressError as exc:
            self._write_state(channel_id, "ERROR", last_error=str(exc))
            self._append_health(channel_id, "ERROR", sink_connected={})
            self._rollover_plan_end_at.pop(channel_id, None)
        except Exception as exc:
            # A failed encoder launch must not leave the provisional slate
            # state looking like output. Keep the existing outer failure hook
            # and consumed-command policy by propagating after recording it.
            self._write_state(channel_id, "ERROR", last_error=f"Channel start failed: {exc}")
            self._append_health(channel_id, "ERROR", sink_connected={})
            self._rollover_plan_end_at.pop(channel_id, None)
            raise
        return None

    def _write_state(
        self,
        channel_id: str,
        state: EgressState,
        *,
        current_source_label: str | None = None,
        current_proof_event_id: str | None = None,
        last_error: str | None = None,
        pid: int | None = None,
    ) -> None:
        # Gate A T4 diagnosability fix (2026-09): this is the ONE choke point
        # every pipeline state transition passes through -- every
        # FALLBACK_SLATE / ERROR entry above sets ``last_error`` here, never
        # anywhere else. Logging it at INFO (rather than leaving it as a
        # store-only field) is what actually reaches an operator or a Gate A
        # probe reading ``control_plane-app.log``: before this fix the
        # control-plane child process had no configured handler for the
        # ``civiccast`` logger at any level, so this record was silently
        # dropped even though the STATE it describes was durably persisted.
        # ``current_source_label`` is never secret-bearing -- it is a
        # human-facing label (e.g. "Council meeting", "Fallback slate"), not
        # the underlying URI; the URI itself is redacted via
        # ``redact_source_uri`` before it ever reaches proof-event storage
        # (see ``_build_proof_event`` below) and is never passed to this
        # method at all.
        #
        # Both fields below are free text this daemon does not control the
        # content of: ``current_source_label`` traces back to an
        # operator-entered asset title (source_plan.py's ``label = asset.title
        # or item.asset_title or item.asset_id``), and ``last_error`` often
        # carries a raw ``str(exc)`` or a folded child-stderr tail. This is the
        # ONE choke point every state transition passes through (see the
        # comment above), so it is the one place to fold BOTH before they
        # reach ``EgressStore.write_state`` -- see
        # ``civiccast/egress/_text.py`` for why a non-UTF8 character here
        # aborts the whole automation pass if left unfolded.
        if channel_id in self._caption_reset_failed and state != "STOPPED":
            warning = "captions disabled: session reset failed; Stop and Start again to retry"
            if not last_error or warning not in last_error:
                last_error = f"{last_error}; {warning}" if last_error else warning
        current_source_label = db_safe_text_or_none(current_source_label)
        last_error = db_safe_text_or_none(last_error)
        _LOG.info(
            "channel %s: egress state -> %s (source=%s, pid=%s, last_error=%s)",
            channel_id,
            state,
            current_source_label or "-",
            pid if pid is not None else "-",
            last_error or "-",
        )
        self._store.write_state(
            EgressStateRow(
                channel_id=channel_id,
                state=state,
                current_source_label=current_source_label,
                current_proof_event_id=current_proof_event_id,
                updated_at=datetime.now(UTC),
                pid=pid,
                last_error=last_error,
            )
        )

    def _append_health(
        self,
        channel_id: str,
        state: EgressState,
        *,
        sink_connected: dict[str, bool],
        dropped_frames: int = 0,
        seconds_on_air: int = 0,
    ) -> None:
        metrics = self._health_metrics(channel_id, state=state)
        # S9: stamp the running schema version + the proof-event churn since the last
        # sample (operator skew + churn-loop visibility).
        last_health = self._store.recent_health(channel_id, 1)
        since = last_health[0].sampled_at if last_health else None
        self._store.append_health(
            EgressHealthSample(
                channel_id=channel_id,
                sampled_at=datetime.now(UTC),
                state=state,
                sink_connected=sink_connected,
                encoder_fps=metrics.encoder_fps,
                encoder_bitrate_kbps=metrics.encoder_bitrate_kbps,
                dropped_frames=(
                    metrics.dropped_frames if metrics.dropped_frames is not None else dropped_frames
                ),
                seconds_on_air=seconds_on_air,
                last_loudness_lufs=self._last_loudness_lufs.get(channel_id),
                caption_status=(
                    "not-verified"
                    if channel_id in self._caption_reset_failed
                    else (
                        self._caption_status_provider(channel_id)
                        if self._caption_status_provider is not None
                        else "not-verified"
                    )
                ),
                schema_version=current_schema_version(),
                proof_events_appended_since_last_sample=self._store.count_proof_events_since(
                    channel_id, since
                ),
            )
        )
        # S8-3: alert evaluator hook — derive conditions from the just-written sample.
        sync_emergency = getattr(self._encoder_strategy, "sync_emergency_overlay", None)
        if state == "ON_AIR" and callable(sync_emergency):
            config = self._store.get_config(channel_id)
            if config is not None and config.enabled:
                try:
                    if not sync_emergency(channel_id, config, self._work_dir):
                        _LOG.debug("channel %s emergency graphics update not accepted", channel_id)
                except Exception:
                    _LOG.exception("channel %s emergency graphics update failed", channel_id)
        if self._alert_evaluator_hook is not None:
            self._alert_evaluator_hook(
                channel_id, state, metrics.encoder_fps, metrics.encoder_bitrate_kbps
            )

    def _record_prepared_loudness(
        self,
        channel_id: str,
        preparation_report: SourcePreparationReport,
    ) -> None:
        for record in reversed(preparation_report.records):
            if record.measured_lufs is not None:
                self._last_loudness_lufs[channel_id] = record.measured_lufs
                return
        self._last_loudness_lufs.pop(channel_id, None)

    def _health_metrics(self, channel_id: str, *, state: str) -> EgressEncoderMetrics:
        if state not in {"ON_AIR", "TRANSITIONING", "FALLBACK_SLATE", "DRAINING"}:
            return EgressEncoderMetrics()
        log_path = self._stderr_logs.get(channel_id)
        if log_path is None:
            return EgressEncoderMetrics()
        return read_latest_ffmpeg_encoder_metrics(log_path)

    def _observed_on_air_evidence(self, channel_id: str) -> bool:
        """Round-3 fix (PR #183 review, BLOCKER item 1): real evidence the
        CURRENT worker has produced actual output, as opposed to merely
        having not exited yet.

        Checked (and, once True, latched into ``_on_air_confirmed_at``) on
        EVERY alive poll tick regardless of the channel's current state
        (ON_AIR / FALLBACK_SLATE / etc. all run a real encoder child that can
        supply this evidence) -- deliberately not gated the way
        ``_health_metrics`` is, since a channel airing the fallback slate
        through the SAME GStreamer engine is just as capable of proving it
        reached PLAYING.

        Round-4 (PR #183 review, BLOCKER reproduced): round-3's version read
        both evidence sources from the CHANNEL's log with no anchor to the
        CURRENT worker's own spawn point. ``strategy.py`` / ``_ffmpeg.py``
        both open this fixed per-channel log in APPEND mode and never
        truncate it per spawn, so one worker EVER reaching PLAYING (or ever
        showing FFmpeg progress) left that evidence sitting in the log
        forever -- every later worker on the same channel read as "confirmed
        on air" on its very first poll tick, even one that never produced
        any output itself (measured: 40 relaunches, streak pinned at 1; the
        round-3 healthy-uptime clock never actually started from THIS
        worker's evidence, because it kept reading a PREVIOUS worker's).
        Both evidence sources below are now anchored to
        ``self._stderr_spawn_offset[channel_id]`` -- the byte size of the log
        at the moment THIS worker was spawned (see that attribute's
        docstring) -- so a previous worker's evidence, which always sits
        before that offset, is never read at all, not merely filtered out
        after the fact.

        Two encoder families, two evidence sources, either is sufficient:

        * GStreamer (``civiccast.egress.gst.engine.GstPlayoutEngine``): the
          ``CTRL first-output: ...`` stderr marker
          ``_maybe_print_first_output_marker`` prints ONLY once a real TS
          buffer has crossed the mux (see
          ``civiccast.egress.health.worker_produced_output``).

          Item 84 Round-2 review BLOCKER, CORRECTING this function's own
          prior text directly above (which cited the ``CTRL preroll:
          reached PLAYING`` marker / ``worker_reached_playing`` as
          sufficient evidence here): PLAYING (even ``NO_PREROLL``) is NOT
          evidence a single buffer ever crossed the mux. The reviewer
          measured the exact consequence of treating it as such -- a worker
          that reaches PLAYING on every single relaunch but never produces
          real output (at ANY ``first_output_timeout_s`` from 65s through
          the 120s clamp ceiling) got its crash-loop streak reset by THIS
          alive-poll path on every cycle, and never escalated to fallback
          slate (streak pinned at 1) no matter how long the budget ran.
          ``worker_reached_playing``/the PLAYING marker are UNCHANGED and
          still printed (useful for operator logs and for
          ``EgressDaemon._poll_process``'s own on-air-evidence-latch
          docstring elsewhere), but this evidence gate now requires the
          LATER, stronger ``worker_produced_output`` instead -- same
          spawn-offset anchor and pid check as ``worker_reached_playing``
          (see that function's own docstring for the full round-4
          append-log rationale, which applies unchanged here), so even a
          marker that somehow lands at or after the offset cannot be
          credited to the wrong worker.
        * FFmpeg (the other encoder strategy this daemon can launch): no
          output marker exists, but ``read_ffmpeg_encoder_metrics_since``
          parses live fps=/bitrate= progress out of the same stderr log,
          anchored the same way -- reused here via ``encoder_has_progress``
          as the FFmpeg-side equivalent: real encoding progress is at least
          as strong a signal as a GStreamer worker's first output buffer,
          and without this branch an FFmpeg-encoded channel would NEVER get
          its healthy-uptime reset at all (the GStreamer markers only ever
          appear in a GStreamer worker's log).
        """
        log_path = self._stderr_logs.get(channel_id)
        if log_path is None:
            return False
        offset = self._current_stderr_spawn_offset(channel_id, log_path)
        expected_pid = _process_pid(self._processes.get(channel_id))
        if worker_produced_output(log_path, offset=offset, expected_pid=expected_pid):
            return True
        return encoder_has_progress(read_ffmpeg_encoder_metrics_since(log_path, offset=offset))

    def _engine_label(self) -> str:
        """Which encoder engine actually runs this channel's child process.

        Gate A T4 (2026-09): every non-zero child exit was reported as "FFmpeg child
        exited non-zero" even when the child was the GStreamer playout worker, which
        sent operators (and a Gate A triage) hunting an ffmpeg problem that did not
        exist. Name the engine from the strategy that launched it.
        """
        name = getattr(self._encoder_strategy, "name", "") or ""
        return "GStreamer playout worker" if "gstreamer" in name.lower() else "FFmpeg encoder"

    def _current_stderr_spawn_offset(self, channel_id: str, log_path: Path) -> int:
        """Offset for the current base-file generation, resetting after rotation."""

        identity = _stderr_log_identity(log_path)
        spawn_identity = self._stderr_spawn_identity.get(channel_id)
        offset = self._stderr_spawn_offset.get(channel_id, 0)
        if identity != spawn_identity or _stderr_log_size(log_path) < offset:
            self._stderr_spawn_identity[channel_id] = identity
            self._stderr_spawn_offset[channel_id] = 0
            return 0
        return offset

    def _child_stderr_tail(self, channel_id: str, *, max_lines: int = 8) -> str | None:
        """Last few non-blank stderr lines of the channel's just-exited child.

        The worker's traceback / stall message is the only thing that says WHY a
        channel is bouncing, and it lives in a per-channel log file nothing outside
        the work dir reads. Fold a bounded, redacted tail into ``last_error`` so the
        operator's state row and the control-plane log carry the reason.
        Redacted with ``redact_uris_in_text`` per line -- an ingest URI in a GStreamer
        error message can carry an SRT passphrase / RTMP key (ENG-003). NOT
        ``redact_source_uri``: that leaves a URI EMBEDDED in a longer line untouched, and
        ``ERROR failed to open srt://host?passphrase=...`` is exactly what a child writes.
        """
        log_path = self._stderr_logs.get(channel_id)
        if log_path is None:
            return None
        try:
            offset = self._current_stderr_spawn_offset(channel_id, Path(log_path))
            size = _stderr_log_size(Path(log_path))
            start = max(offset, size - _CHILD_STDERR_TAIL_BYTES)
            with Path(log_path).open("rb") as stream:
                stream.seek(start)
                text = stream.read(_CHILD_STDERR_TAIL_BYTES).decode("utf-8", errors="replace")
        except OSError:
            return None
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return None
        tail_lines = lines[-max_lines:]
        # A reload-commit watchdog dump puts the most useful current frame just
        # outside the ordinary eight-line tail: faulthandler prints that frame,
        # then its callers, then the watchdog marker.  Preserve a compact,
        # path-free frame when (and only when) this child's current tail contains
        # that marker.  Full logs remain authoritative; this is the bounded clue
        # operators need in the state row to distinguish selector switching from
        # old-leg disposal without exposing install paths or expanding the field.
        if any(_RELOAD_COMMIT_TIMEOUT_MARKER in line for line in tail_lines):
            frame: str | None = None
            for function in _RELOAD_FRAME_PRIORITY:
                for line in reversed(lines):
                    match = _RELOAD_FRAME_RE.match(line)
                    if match is not None and match.group("function") == function:
                        frame = (
                            "CTRL reload blocked at "
                            f"engine.py:{match.group('line')} in {match.group('function')}"
                        )
                        break
                if frame is not None:
                    break
            if frame is not None and frame not in tail_lines:
                tail_lines = [frame, *tail_lines]
        tail = " | ".join(redact_uris_in_text(line) for line in tail_lines)
        return _ascii_safe(tail)[:_STDERR_TAIL_MAX_CHARS]

    def _child_exit_error(self, channel_id: str, *, suffix: str) -> str:
        tail = self._child_stderr_tail(channel_id)
        message = f"{self._engine_label()} child exited non-zero; {suffix}"
        if tail:
            message = f"{message} Last child stderr: {tail}"
        return message

    def _build_proof_event(
        self,
        *,
        channel_id: str,
        state: str,
        source_plan: EgressSourcePlan,
        previous_state: str | None = None,
        previous_source_label: str | None = None,
    ) -> EgressProofEvent:
        segment = source_plan.segments[0]
        # ``segment.label`` is operator-entered free text (source_plan.py:
        # ``label = asset.title or item.asset_title or item.asset_id``), and
        # ``_proof_event_summary`` interpolates it (and the PREVIOUS label) into
        # ``machine_summary`` -- both are persisted via
        # ``EgressStore.append_proof_event``, a separate write path from
        # ``_write_state``, so it needs its own fold at this, its own choke
        # point. See ``civiccast/egress/_text.py``.
        source_label = db_safe_text(segment.label)
        return EgressProofEvent(
            event_id=f"egress-proof-{uuid.uuid4()}",
            observed_at=datetime.now(UTC),
            channel_id=channel_id,
            state=state,  # type: ignore[arg-type]
            source_label=source_label,
            # ENG-003: a live segment's path is an ingest URI that can carry an SRT
            # passphrase / RTMP key / RTSP credentials — redact before it lands in the
            # durable, operator-readable proof chain.
            source_path=redact_source_uri(segment.path),
            source_ref=segment.source_ref,
            proof_boundary="civiccast-egress-handoff-boundary",
            machine_summary=db_safe_text(
                _proof_event_summary(
                    state=state,
                    previous_state=previous_state,
                    previous_source_label=db_safe_text_or_none(previous_source_label),
                    source_kind=segment.kind,
                    source_label=source_label,
                    channel_id=channel_id,
                )
            ),
        )

    def _record_as_run_transition(
        self,
        *,
        channel_id: str,
        running_state: str,
        source_plan: EgressSourcePlan,
        proof_event: EgressProofEvent,
    ) -> None:
        """Append an as-run entry for an ACTUAL source transition (S23 §6.1).

        Called immediately after the engine emits an ON_AIR/FALLBACK_SLATE
        proof event (the on-air instant). Append-only side-effect — never raises
        into the playout path (the recorder also guards internally; this is the
        engine-side belt-and-suspenders, parallel to the proof-event auditing).
        """
        if self._as_run_recorder is None:
            return
        try:
            segment = source_plan.segments[0]
            source_kind = map_source_kind(segment_kind=segment.kind, running_state=running_state)
            self._as_run_recorder.record_transition(
                channel_id=channel_id,
                source_kind=source_kind,
                asset_id=asset_id_for_segment(source_plan=source_plan, source_kind=source_kind),
                source_label=segment.label,
                actual_start=proof_event.observed_at,
                proof_event_id=proof_event.event_id,
                schedule_item_id=(
                    segment.schedule_occurrence_id if source_kind == "program" else None
                ),
            )
        except AsRunCaptureSchemaError:
            # Schema drift on the engine→ledger seam (E-1). Loud, not silent:
            # mark degraded mode + log at ERROR so an operator sees the as-run
            # ledger is paused while playout continues.
            self._as_run_schema_drift = True
            _LOG.error(
                "as-run capture schema drift; playout safe; ledger paused (channel %s)",
                channel_id,
            )
        except Exception:  # auditing must never block the playout path
            _LOG.exception(
                "As-run capture failed for channel %s; playout is unaffected.",
                channel_id,
            )

    def _close_as_run(self, channel_id: str) -> None:
        """Close the channel's open as-run row at a terminal state (clean stop,
        error, drain) — the channel left air with no new source taking over.
        Fail-safe; a no-op when no recorder is wired or no row is open."""
        if self._as_run_recorder is None:
            return
        try:
            self._as_run_recorder.close_open(channel_id=channel_id, actual_end=datetime.now(UTC))
        except AsRunCaptureSchemaError:
            # Schema drift on close (E-1). Same loud-not-silent contract as the
            # record path.
            self._as_run_schema_drift = True
            _LOG.error(
                "as-run capture schema drift; playout safe; ledger paused (channel %s)",
                channel_id,
            )
        except Exception:  # auditing must never block the playout path
            _LOG.exception(
                "As-run close failed for channel %s; playout is unaffected.",
                channel_id,
            )

    def _restore_relay_streams(
        self,
        channel_id: str,
        *,
        now: float,
        producing: bool,
        startup_grace_s: float,
    ) -> None:
        """BETA.10 U12: give the relay supervisor one bounded chance to
        restore a relay that cannot carry every stream kind its sink requires.

        Called on both relay states -- a child that exited (the relay argv
        requires each kind the sink carries, so a probe missing one is a
        prompt, deliberate exit) and a live child whose served window is
        missing a kind (audio-only OR video-only) -- and also covers an
        ordinary relay death, which nothing respawned before this. The
        supervisor owns the gating (``producing``/startup grace for the live
        case, a backoff for both) and the attempt bound, so this is a thin,
        exception-safe hand-off; ``getattr``/``callable`` keeps it working with
        the older/simpler supervisor doubles the tests inject, exactly like the
        neighbouring ``note_progress``/``progress_stale`` calls.
        """
        if self._hls_relay is None:
            return
        restore = getattr(self._hls_relay, "maybe_restore_missing_streams", None)
        if not callable(restore):
            return
        try:
            restore(
                channel_id,
                now=now,
                producing=producing,
                startup_grace_s=startup_grace_s,
            )
        except Exception:
            _LOG.exception("channel %s: HLS relay stream-restore attempt failed.", channel_id)

    def _trim_relay_logs(self, channel_id: str) -> None:
        """BETA.10 U24: hard-cap this channel's relay stderr logs.

        A thin hand-off like the neighbouring relay-poll helpers: the supervisor
        owns the cap, the in-place rewrite, and the warning that names the file.
        ``getattr``/``callable`` keeps it working with the simpler supervisor
        doubles the tests inject. It never restarts a child -- the whole point of
        the in-place rewrite is that the relay serving residents keeps serving.
        """
        trim = getattr(self._hls_relay, "maybe_trim_logs", None)
        if not callable(trim):
            return
        try:
            trim(channel_id)
        except Exception:
            _LOG.exception("channel %s: HLS relay stderr log trim failed.", channel_id)

    def _poll_hls_relay(self, channel_id: str) -> None:
        """MAJOR M1: poll this channel's supervised HLS relay child liveness.

        Mirrors ``_poll_process`` polling the main worker, for the SEPARATE
        ffmpeg co-process ``HlsRelaySupervisor`` owns (see DEFECT A / M1 in
        ``civiccast.egress.hls_relay``). Before this, a relay death was never
        observed until the channel's next full encoder start/reload happened
        to call ``apply()`` again. Result is cached in ``_hls_relay_dead`` and
        consulted by ``_sink_connected`` so the ``hls`` sink's reported health
        reflects the relay's OWN liveness, not just the main encoder's UDP
        send progress.
        """
        if self._hls_relay is None:
            return
        # BETA.10 U24: the same tick that watches the relay also bounds its
        # stderr capture. It runs FIRST and regardless of liveness, because a
        # child that has already exited leaving an oversized log behind is
        # exactly the case the cap exists for.
        self._trim_relay_logs(channel_id)
        is_alive = getattr(self._hls_relay, "is_alive", None)
        if not callable(is_alive):
            return
        alive = is_alive(channel_id)
        if alive is None:
            # No relay currently tracked for this channel (no hls sink
            # configured, or none started yet) -- not a health signal.
            self._hls_relay_dead.pop(channel_id, None)
            return
        was_already_flagged_dead = self._hls_relay_dead.get(channel_id, False)
        self._hls_relay_dead[channel_id] = not alive
        now = self._monotonic()
        if not alive:
            if not was_already_flagged_dead:
                _LOG.error(
                    "HLS relay child for channel %s is no longer running (disk full, "
                    "ffmpeg missing, or OOM are the known causes; a relay child whose "
                    "probing found a required stream missing exits by design -- see "
                    "hls_relay's required stream mapping). The main encoder is "
                    "unaffected. The relay is recovered on a bounded, backed-off "
                    "cadence, so it may briefly have no live window while that cadence "
                    "runs.",
                    channel_id,
                )
            # BETA.10 U12: recover a dead relay child on THIS tick instead of
            # waiting for the channel's next encoder start/reload. That gap is
            # what left the live government channel serving audio-only HLS for
            # hours after its 18:46:32 mid-stream restart: the child had exited
            # (or was locked to a half-program window) and nothing respawned it.
            # ``maybe_restore_missing_streams`` is bounded and backed off, so a
            # relay that cannot be recovered cannot become a restart storm.
            self._restore_relay_streams(
                channel_id,
                now=now,
                producing=channel_id in self._on_air_confirmed_at,
                startup_grace_s=_HLS_RELAY_STARTUP_GRACE_S,
            )
            return
        # BETA.10 U12, checked BEFORE the served-window machinery below because
        # it is a different fault: an ALIVE relay can be advancing its window
        # perfectly while every segment in it is missing a required stream --
        # audio-only (the live government symptom at 18:46:32) or video-only
        # (U13's measurement at 18:47:44-18:47:56) -- so no amount of progress
        # monitoring sees it.
        self._restore_relay_streams(
            channel_id,
            now=now,
            producing=channel_id in self._on_air_confirmed_at,
            startup_grace_s=_HLS_RELAY_STARTUP_GRACE_S,
        )
        # BETA.10 live finding (2026-09-23): a STILL-RUNNING relay child can
        # stop advancing ``playlist.m3u8`` (the live government channel froze at
        # seg000001130.ts for minutes while the relay pid and the main encoder's
        # CTRL output counters both kept running). Process liveness alone
        # therefore reports that frozen window as healthy. Layer a served-window
        # progress check on top, using the SAME ``_hls_relay_dead`` seam so
        # ``_sink_connected`` tells the truth about a stalled-but-alive relay.
        note_progress = getattr(self._hls_relay, "note_progress", None)
        if not callable(note_progress):
            return
        try:
            note_progress(channel_id, now=now)
        except Exception:
            _LOG.exception("channel %s: HLS relay progress observation failed.", channel_id)
            return
        progress_stale = getattr(self._hls_relay, "progress_stale", None)
        stalled = progress_stale(channel_id, now=now) if callable(progress_stale) else None
        if stalled is None:
            # No readable window to judge. That is legitimate during
            # STARTING/no-source, so it is only a fault when the daemon has
            # ALREADY confirmed this channel is producing AND at least one live
            # relay is past its own startup grace without ever writing a window
            # (audit finding 3). Otherwise stay "unknown", never "stale".
            never_emitted = getattr(self._hls_relay, "never_emitted", None)
            if not (
                callable(never_emitted)
                and channel_id in self._on_air_confirmed_at
                and never_emitted(channel_id, now=now, startup_grace_s=_HLS_RELAY_STARTUP_GRACE_S)
            ):
                return
            if not was_already_flagged_dead:
                _LOG.error(
                    "HLS relay child for channel %s is running but has never written "
                    "a live window while the channel is producing; residents are "
                    "getting no HLS stream. Attempting a bounded relay self-heal.",
                    channel_id,
                )
            self._hls_relay_dead[channel_id] = True
        else:
            self._hls_relay_dead[channel_id] = bool(stalled)
            if not stalled:
                return
            if not was_already_flagged_dead:
                _LOG.error(
                    "HLS relay child for channel %s is still running but its live window "
                    "has not advanced; residents are getting a frozen stream. Attempting "
                    "a bounded relay self-heal.",
                    channel_id,
                )
        # Bounded self-heal: ONLY when the channel is genuinely producing (the
        # daemon's own on-air evidence latch, never mere process liveness), and
        # ONLY after the relay child has had its startup grace. A STARTING /
        # no-source channel can legitimately have a static or absent window and
        # must never be restarted from here. ``maybe_self_heal_stalled`` is
        # one-shot per stall episode, so this cannot become a restart storm.
        maybe_heal = getattr(self._hls_relay, "maybe_self_heal_stalled", None)
        if not callable(maybe_heal):
            return
        producing = channel_id in self._on_air_confirmed_at
        try:
            maybe_heal(
                channel_id,
                now=now,
                producing=producing,
                startup_grace_s=_HLS_RELAY_STARTUP_GRACE_S,
            )
        except Exception:
            _LOG.exception("channel %s: HLS relay self-heal attempt failed.", channel_id)

    def _poll_output_av_guard(self, channel_id: str) -> None:
        """U16 item B: measure the ON-AIR channel's output A/V relationship.

        The gate order here is the contract, and each clause exists because
        violating it would restart something that is not broken:

        * No live worker -> nothing to measure and nothing to restart.
        * Worker pid changed -> the previous worker's measurements are void
          (streak reset), but the probe CADENCE is left alone: a relaunch must
          not make the guard probe faster than once per interval.
        * Not ON_AIR/FALLBACK_SLATE -> not judged. STARTING has no output yet,
          DRAINING is deliberately leaving air, and TRANSITIONING is what
          ``_poll_process`` publishes for a channel with a content reload in
          flight -- exactly the moment the U16 rebase step happens, and a
          legitimate reason for A/V to move against each other for a moment.
          This guard judges the settled output, never the switch.
        * FALLBACK_SLATE IS judged (U21 item B2). The slate leg runs through the
          same mux, the same udpsink and the same relay child as a program leg,
          so it can carry - and strand - the same output A/V offset; the U21
          slate-entry incident was measured on a slate leg. Excluding it left
          the desync shape with no net at all, because B1's rebind cannot cure
          an offset born on a slate the worker never restarts out of. Judging a
          slate leg is safe for the same reason judging a program leg is: the
          guard reads the worker's own settled output, and a slate leg that
          cannot settle is exactly what a restart repairs.
        * Inside the probe interval -> not yet time (the tick rate is 2s).
        * Probe could not measure -> neither advances nor resets the streak, and
          can never restart anything by itself.

        The guard does NOT poll the worker itself. It runs LAST in the poll
        tuple, so ``_poll_process`` has already processed any exit this tick --
        it either published the state row for a live worker or moved the channel
        on (relaunch, slate, stop). A poll here would be a second, unordered
        reader of the same process's exit state: it changes when the exit is
        first observed by everything downstream of it, which is a real coupling
        (it silently shifted a reload-settlement tick's liveness re-check in
        ``test_worker_exit_between_poll_process_and_poll_reload_settlement_falls_back``)
        for no gain -- the state row this guard gates on is already the answer.
        """
        process = self._processes.get(channel_id)
        if process is None:
            return
        if self._quality_guard_deferred(channel_id):
            return

        pid = _process_pid(process)
        guard = self._output_av_guard.get(channel_id)
        if guard is None:
            guard = _OutputAvGuardState(pid=pid)
            self._output_av_guard[channel_id] = guard
        elif guard.pid != pid:
            guard.pid = pid
            guard.streak = 0
            del guard.offsets[:]

        state = self._store.read_state(channel_id)
        if (
            state is None
            or state.state not in {"ON_AIR", "FALLBACK_SLATE"}
            or channel_id in self._draining_channels
        ):
            return

        now = self._monotonic()
        if (
            guard.last_probe_at is not None
            and now - guard.last_probe_at < _OUTPUT_AV_GUARD_PROBE_INTERVAL_S
        ):
            return
        guard.last_probe_at = now

        measurement = self._measure_output_av_offset(channel_id)
        if measurement is None:
            return
        offset, segment = measurement
        if abs(offset) <= _OUTPUT_AV_GUARD_MAX_OFFSET_S:
            guard.streak = 0
            del guard.offsets[:]
            return

        guard.streak += 1
        guard.offsets.append((offset, segment))
        del guard.offsets[:-_OUTPUT_AV_GUARD_CONSECUTIVE_PROBES]
        if guard.streak < _OUTPUT_AV_GUARD_CONSECUTIVE_PROBES:
            return
        self._restart_desynced_output(channel_id, guard, process, now=now)

    def _measure_output_av_offset(self, channel_id: str) -> tuple[float, str] | None:
        """The newest COMPLETE segment's ``(audio - video)`` first-packet offset.

        ``None`` when there is nothing to measure: no config, no measurable
        ``hls`` sink, no complete segment yet, or a probe that could not answer
        (see ``_segment_first_packet_pts``). Sinks are tried in config order and
        the first measurable one wins -- two hls sinks of the same channel see
        the same program, so any of them answers the same question, and one
        unmeasurable sink (a directory not written yet) must not hide a sibling
        that can answer.

        Only ``hls`` sinks can be measured at all: an rtmp/rtsp sink's output is
        not on this machine to read, so a channel with no hls sink is simply
        never judged by this guard rather than judged on a guess.
        """
        config = self._built_configs.get(channel_id) or self._store.get_config(channel_id)
        if config is None:
            return None
        for sink in config.sinks:
            if sink.kind != "hls":
                continue
            playlist = _playlist_path_for(sink.uri)
            segment = _last_complete_segment(playlist)
            if segment is None:
                continue
            first_packets = self._output_av_probe(playlist.parent / segment)
            if first_packets is None:
                continue
            video_pts, audio_pts = first_packets
            return (audio_pts - video_pts, segment)
        return None

    def _restart_desynced_output(
        self,
        channel_id: str,
        guard: _OutputAvGuardState,
        process: object,
        *,
        now: float,
    ) -> None:
        """Report a confirmed output desync, restart, or enter cooldown recovery.

        The restart is the SAME route a dead encoder takes, and deliberately not
        a private one: this terminates the worker WITHOUT recording it in
        ``_reload_kills``, so the exit is an ordinary non-zero worker exit and
        ``_poll_process`` next tick finds the row still ON_AIR and calls
        ``_relaunch_after_crash`` -- the ordinary crash-relaunch path, with its
        own back-off, escalation accounting and proof event. Recording it as a
        deliberate kill would instead preserve a pending reload the guard never
        intended and bind the two mechanisms together.

        The kill is bounded (terminate -> short wait -> kill) so the worker is
        genuinely gone by the next tick rather than merely asked to leave: the
        ordinary path then sees a real non-zero exit, which is what it handles.
        """
        guard.restarted_at = [
            at for at in guard.restarted_at if at > now - _OUTPUT_AV_GUARD_BUDGET_WINDOW_S
        ]
        if self._quality_guard_deferred(channel_id):
            return
        measured = "; ".join(
            f"{segment} measured {offset:+.3f}s" for offset, segment in guard.offsets
        )
        detail = (
            f"channel {channel_id}: output A/V desync -- audio and video first packet "
            f"timestamps differ by more than {_OUTPUT_AV_GUARD_MAX_OFFSET_S:.1f}s on "
            f"{_OUTPUT_AV_GUARD_CONSECUTIVE_PROBES} consecutive probes "
            f"(audio minus video: {measured})"
        )
        if len(guard.restarted_at) >= _OUTPUT_AV_GUARD_RESTART_BUDGET:
            if (
                guard.last_exhausted_log_at is None
                or now - guard.last_exhausted_log_at >= _OUTPUT_AV_GUARD_EXHAUSTED_LOG_INTERVAL_S
            ):
                guard.last_exhausted_log_at = now
                _LOG.error(
                    "%s. Guard restart budget exhausted (%d restarts already spent in the "
                    "last hour): moving to the fallback slate for a bounded cooldown before "
                    "one program retry.",
                    detail,
                    len(guard.restarted_at),
                )
                self._begin_quality_recovery(
                    channel_id,
                    restarted_at=guard.restarted_at,
                    now=now,
                    detail="output A/V desync",
                )
            return
        guard.restarted_at.append(now)
        guard.streak = 0
        del guard.offsets[:]
        _LOG.error(
            "%s. Restarting this channel's worker through the ordinary crashed-encoder "
            "relaunch path.",
            detail,
        )
        _process_terminate_bounded(process)

    def _freeze_escalation_state(self, channel_id: str) -> _FreezeEscalationState:
        state = self._freeze_escalation.get(channel_id)
        if state is None:
            state = _FreezeEscalationState()
            self._freeze_escalation[channel_id] = state
        return state

    def _poll_freeze_escalation(self, channel_id: str) -> None:
        """U30: escalate when a relay self-heal has run and the window is STILL frozen.

        The relay supervisor's self-heal is one-shot per stall episode by design
        (hls_relay audit finding 4: a second heal in the same episode would be a
        restart storm), so a heal that does not restore the window leaves that
        path with no next move. This poll is the next move: it waits
        ``_FREEZE_ESCALATION_AFTER_HEAL_S`` past the heal and, if the window is
        still frozen, restarts the channel's worker through the ordinary
        crashed-encoder relaunch path.

        The gate order is the contract, and each clause exists so that nothing
        healthy is ever restarted:

        * No live worker entry -> nothing to restart, and the channel's
          bookkeeping is dropped (a stopped channel must not carry a stale
          budget into its next start).
        * Not ON_AIR/FALLBACK_SLATE, or draining -> not judged. These are the
          only two states in which the channel is supposed to be delivering a
          settled program. STARTING and TRANSITIONING are explicitly exempt:
          both are transient by construction (TRANSITIONING is what
          ``_poll_process`` publishes while a content reload is in flight --
          exactly when the U30 freeze has been seen) and neither has a window
          that is supposed to be advancing yet. DRAINING is leaving air on
          purpose.
        * Worker pid changed -> the episode is voided. A replacement worker's
          frozen predecessor-window is not evidence about it.
        * Already escalated for THIS pid -> nothing more to do (the killed
          worker is still the live entry until its exit is reaped).
        * ``heal_frozen_seconds`` is under the bound -> not a failed heal yet,
          or no relay in a failed-heal state at all. ``None`` (no relay
          tracked, or no live relay is mid-failed-heal) means "this question
          does not apply" and must never escalate: with no relay in play there
          is no heal to have failed.

        The signal is the HLS live window and only that -- see the constants
        block at the top of this module for why the worker's own output-progress
        line is deliberately not used.
        """
        process = self._processes.get(channel_id)
        if process is None:
            self._freeze_escalation.pop(channel_id, None)
            return
        if self._quality_guard_deferred(channel_id):
            return

        state = self._freeze_escalation_state(channel_id)
        pid = _process_pid(process)
        if state.pid != pid:
            state.pid = pid
            state.escalated_for_pid = None

        row = self._store.read_state(channel_id)
        if (
            row is None
            or row.state not in ("ON_AIR", "FALLBACK_SLATE")
            or channel_id in self._draining_channels
        ):
            return
        if state.escalated_for_pid == pid:
            return

        supervisor = self._hls_relay
        if supervisor is None:
            return
        # Defensive like the rest of this daemon's relay seams (see
        # ``_poll_hls_relay``): a supervisor double that predates this signal
        # must read as "not applicable", never as an exception on every tick.
        frozen_seconds = getattr(supervisor, "heal_frozen_seconds", None)
        if not callable(frozen_seconds):
            return
        now = self._monotonic()
        frozen = frozen_seconds(channel_id, now=now)
        if frozen is None or frozen < _FREEZE_ESCALATION_AFTER_HEAL_S:
            return
        self._restart_frozen_channel(channel_id, state, process, pid=pid, frozen=frozen, now=now)

    def _restart_frozen_channel(
        self,
        channel_id: str,
        state: _FreezeEscalationState,
        process: object,
        *,
        pid: int | None,
        frozen: float,
        now: float,
    ) -> None:
        """Report a confirmed post-heal freeze and restart the worker, or refuse.

        The restart is the SAME route the output A/V guard and a dead encoder
        take: terminate the worker WITHOUT recording it in ``_reload_kills``, so
        its exit is an ordinary non-zero exit that ``_poll_process`` next tick
        finds, with the row still ON_AIR, and hands to ``_relaunch_after_crash``
        -- the ordinary crash-relaunch path with its own back-off and
        accounting. The kill is bounded, so the worker is genuinely gone by the
        next tick rather than only asked to leave.

        This method does NOT touch the relay child, and that is the whole point of
        the split: the self-heal this escalation exists to escalate ALREADY
        replaced that child (its age is what ``heal_frozen_seconds`` reports), and
        in both live incidents the fresh child wrote nothing either -- a child
        that stops writing while its upstream has stopped feeding it is evidence
        about the upstream, not about the child, so replacing it a second time
        buys nothing. Nothing has replaced the WORKER, so the WORKER is what this
        restarts.

        Do NOT read that as "the relaunch leaves the relay alone end to end".
        On the merged beta10 line it does not: the relaunch reaches
        ``HlsRelaySupervisor.apply`` with the worker dead, so it takes U21's
        ``new_session=True`` path, which drops the channel's ``_Relay`` record
        and spawns a FRESH child -- a fresh one-shot ``heal_attempted`` latch
        (hls_relay audit finding 4) on the same deterministic port, so the
        still-frozen playlist re-arms exactly one further heal per relay
        incarnation. That is bounded, but the bound is the rolling-hour
        ``_FREEZE_ESCALATION_RESTART_BUDGET`` below (and the throttled CRITICAL
        once it is spent), NOT the heal latch. The merged-line sequence is pinned
        by
        ``test_hls_relay_progress.py::test_daemon_tick_sequence_with_escalation_is_bounded_not_a_storm``;
        the latch by itself is pinned by that file's
        ``..._no_restart_storm_with_frozen_playlist``, with this escalation
        switched off.

        Past ``_FREEZE_ESCALATION_RESTART_BUDGET`` restarts in the rolling hour
        the channel moves to its fallback slate through the cooldown, then gets
        one program retry after the latest restart ages out.
        """
        state.restarted_at = [
            at for at in state.restarted_at if at > now - _FREEZE_ESCALATION_BUDGET_WINDOW_S
        ]
        if self._quality_guard_deferred(channel_id):
            return
        detail = (
            f"channel {channel_id}: the HLS live window is still frozen {frozen:.1f}s after "
            f"the relay self-heal replaced its ffmpeg child, so the self-heal did not "
            f"restore it (residents are watching a stalled stream)"
        )
        if len(state.restarted_at) >= _FREEZE_ESCALATION_RESTART_BUDGET:
            if (
                state.last_exhausted_log_at is None
                or now - state.last_exhausted_log_at >= _FREEZE_ESCALATION_EXHAUSTED_LOG_INTERVAL_S
            ):
                state.last_exhausted_log_at = now
                _LOG.critical(
                    "%s. Freeze escalation budget exhausted (%d worker restarts already "
                    "spent in the last hour): moving to the fallback slate for a bounded "
                    "cooldown before one program retry.",
                    detail,
                    len(state.restarted_at),
                )
                self._begin_quality_recovery(
                    channel_id,
                    restarted_at=state.restarted_at,
                    now=now,
                    detail="frozen HLS output after relay self-heal",
                )
            return
        state.restarted_at.append(now)
        state.escalated_for_pid = pid
        _LOG.error(
            "%s. Restarting this channel's worker through the ordinary crashed-encoder "
            "relaunch path; restart %d of at most %d in the last hour (%d left in this "
            "hour).",
            detail,
            len(state.restarted_at),
            _FREEZE_ESCALATION_RESTART_BUDGET,
            _FREEZE_ESCALATION_RESTART_BUDGET - len(state.restarted_at),
        )
        _process_terminate_bounded(process)

    def _note_deliberate_kill(self, channel_id: str, reason: str) -> None:
        """Record and announce a deliberate worker kill (U52 item 2).

        Every site that terminates a worker on purpose records it in
        ``_reload_kills`` so ``_poll_process`` honors the pending reload
        instead of treating the non-zero exit as a crash. That bookkeeping
        was silent: the live 2026-09-26 education boundary left a bare
        ``worker exited (exit_code=1 ... pending_reload=True)`` in the log,
        which is the same shape an encoder crash produces. Say it out loud
        here, and carry ``reason`` to the exit line, so one line of log is
        enough to tell the two apart.
        """

        self._reload_kill_reasons[channel_id] = reason
        self._reload_kills.add(channel_id)
        _LOG.warning(
            "channel %s: terminating the worker deliberately for a reload (%s); its non-zero "
            "exit is this kill, not a crash.",
            channel_id,
            reason,
        )

    def _poll_process(self, channel_id: str) -> None:
        process = self._processes.get(channel_id)
        if process is None:
            return
        returncode = _process_poll(process)
        if returncode is None:
            state = self._store.read_state(channel_id)
            # _sink_connected keys health by the sinks the RUNNING pipeline was
            # built with (see _built_configs) for every appender; the config
            # row is only its fallback for a process without a recorded build.
            config = self._built_configs.get(channel_id) or self._store.get_config(channel_id)
            if channel_id in self._draining_channels:
                current_state: EgressState = "DRAINING"
            elif channel_id in self._pending_reloads:
                current_state = "TRANSITIONING"
            elif state and state.state == "FALLBACK_SLATE":
                current_state = "FALLBACK_SLATE"
            else:
                current_state = "ON_AIR"
            self._append_health(
                channel_id,
                current_state,
                sink_connected=(
                    self._sink_connected(channel_id, config, state=current_state) if config else {}
                ),
                seconds_on_air=self._seconds_on_air(channel_id),
            )
            self._write_state(
                channel_id,
                current_state,
                current_source_label=state.current_source_label if state else None,
                current_proof_event_id=state.current_proof_event_id if state else None,
                pid=_process_pid(process),
            )
            self._sync_cg_overlay_proof(channel_id, current_state)
            # Round-3 fix (PR #183 review, BLOCKER item 1): latch the FIRST
            # tick that observes real on-air evidence, never re-derive it from
            # wall-clock seconds since spawn (see ``_observed_on_air_evidence``
            # and ``_on_air_confirmed_at``'s own docstring for why spawn time
            # is the wrong clock -- it includes interpreter start, ``import
            # gi``/``Gst.init``, graph build, and the preroll wait itself).
            if channel_id not in self._on_air_confirmed_at and self._observed_on_air_evidence(
                channel_id
            ):
                self._on_air_confirmed_at[channel_id] = self._monotonic()
            confirmed_at = self._on_air_confirmed_at.get(channel_id)
            if (
                confirmed_at is not None
                and self._monotonic() - confirmed_at >= _RESTART_STREAK_RESET_UPTIME_S
                and (
                    channel_id in self._restart_streak
                    or channel_id in self._backoff_relaunch
                    or self._restart_latch.next_allowed_at(channel_id) != 0.0
                    or (current_state == "ON_AIR" and channel_id in self._slate_eos_relaunches)
                )
            ):
                # The worker has held real on-air evidence for a full healthy
                # window — any earlier crash streak was transient, not a loop;
                # clear it so the next crash relaunches at once. Guarded so it
                # fires once per healthy stretch (when there is state to
                # clear) rather than force-resetting the latch on every ~2s
                # poll. Before this evidence gate, this fired on wall-clock
                # uptime alone (measured: a worker that stayed "alive" per
                # poll for 60-62s of interpreter-start/import/graph-build
                # overhead, never once reaching PLAYING, still got its streak
                # reset here every tick -- streak stuck at 1, never escalating
                # to fallback slate in 40 cycles).
                #
                # The slate-EOS relaunch counter clears here ONLY for a real
                # program airing: a FALLBACK_SLATE worker holding healthy
                # uptime is exactly the state the cap exists to bound (the
                # slate plan itself runs up to 120s -- see
                # _SLATE_EOS_RELAUNCH_MAX_CONSECUTIVE), so letting it reset
                # the counter would re-arm the flap every cycle.
                self._reset_restart_tracking(
                    channel_id, slate_eos_relaunches=(current_state == "ON_AIR")
                )
            return
        started_at = self._started_at.get(channel_id)
        uptime = None if started_at is None else max(0.0, self._monotonic() - started_at)
        self._processes.pop(channel_id, None)
        self._started_at.pop(channel_id, None)
        self._on_air_confirmed_at.pop(channel_id, None)
        was_draining = channel_id in self._draining_channels
        self._draining_channels.discard(channel_id)
        # A deliberate filler kill (issue #157) exits non-zero on real
        # ffmpeg; it still flows into the pending reload, not crash relaunch.
        deliberate_kill = channel_id in self._reload_kills
        self._reload_kills.discard(channel_id)
        deliberate_kill_reason = self._reload_kill_reasons.pop(channel_id, None)
        queued_terminal_command = any(
            command.action in {"stop", "drain"}
            for command in self._store.peek_pending_commands(channel_id)
        )
        exited_state = self._store.read_state(channel_id)
        # U52 item 2: name the difference between a deliberate kill and a
        # crash ON this line. The live 2026-09-26 education boundary logged
        # ``exit_code=1 ... pending_reload=True`` and nothing else -- the
        # exact shape of an encoder crash -- while the daemon knew perfectly
        # well it had terminated the worker itself.
        _LOG.info(
            "channel %s: worker exited (exit_code=%s, state=%s, desired=%s, pending_reload=%s, "
            "deliberate_kill=%s)",
            channel_id,
            returncode,
            exited_state.state if exited_state is not None else "UNKNOWN",
            "STOPPED" if was_draining or queued_terminal_command else "ACTIVE",
            channel_id in self._pending_reloads,
            deliberate_kill,
        )
        if deliberate_kill:
            _LOG.info(
                "channel %s: that non-zero exit was a deliberate kill -- %s -- not an encoder "
                "crash; the pending reload is honored.",
                channel_id,
                deliberate_kill_reason or "no reason was recorded at the kill site",
            )
        pending_reload = self._pending_reloads.pop(channel_id, None)
        if (returncode != 0 and not deliberate_kill) or was_draining or queued_terminal_command:
            pending_reload = None
        _process_close(process)
        # Hostile-review follow-up, items 1 & 4: the worker that would have
        # settled any armed reload (and that was reading the currently-active
        # prepared plan) is now DEFINITELY gone -- reclaim both immediately
        # rather than let a spurious restart fire at the 960s deadline against
        # a channel that has already moved on (possibly restarted onto a
        # completely different plan by the branches below), or wait for GC to
        # notice the active plan is no longer referenced.
        self._discard_pending_reload_settlement(channel_id, reason="worker exited")
        self._discard_active_prepared_plan_dir(channel_id)
        if pending_reload is not None:
            previous_state, previous_source_label = pending_reload
            # Audit ENG-001: the state row still carries the just-exited
            # encoder's pid; clear it BEFORE the fresh start so the orphan
            # reap never probes a freed pid on this hot path.
            self._write_state(
                channel_id,
                "STARTING",
                current_source_label=previous_source_label,
                pid=None,
            )
            self._start(
                channel_id,
                previous_state=previous_state,
                previous_source_label=previous_source_label,
                # F3(b): this IS the restart a FALLBACK_SLATE reload's
                # terminate was for -- hand it the plan that reload already
                # prepared, so the restart does not conform it again. Popped
                # here (not left stashed) so the entry cannot outlive the
                # restart it was held for; ``_start`` releases it itself if
                # something prevents it from being aired.
                prepared_reload=self._prepared_restart_plans.pop(channel_id, None),
            )
            return
        # F3(b): nothing below takes the pending-reload restart this exit could
        # have consumed a held prepared-restart plan, so that plan will never
        # be aired by it -- release it now rather than leave a live directory
        # (or a stale plan a LATER, unrelated restart would wrongly air) behind.
        self._discard_prepared_restart_plan(
            channel_id, reason=f"worker exit took no pending reload (code={returncode})"
        )
        # A queued operator stop/drain is an explicit intent to leave air and
        # must win over recovery, even when the worker exits between command
        # enqueue and this poll.  The command is drained below against the
        # already-gone worker and records STOPPED.
        state = self._store.read_state(channel_id)
        active_intent = state is not None and state.state in {"ON_AIR", "TRANSITIONING"}
        if (
            returncode == 0
            and state is not None
            and active_intent
            and not was_draining
            and not queued_terminal_command
        ):
            self._relaunch_after_crash(channel_id, state, uptime, returncode)
            return
        if returncode == 0 or was_draining or queued_terminal_command:
            stop_error: str | None = None
            if not was_draining:
                relaunched, stop_error = self._relaunch_slate_eos_onto_due_program(channel_id)
                if relaunched:
                    return
            self._reset_restart_tracking(channel_id)  # clean exit — fresh slate
            self._clear_cg_overlay_proof(channel_id, "STOPPED")
            self._close_as_run(channel_id)  # the channel left air — close the open row
            if was_draining and self._ts_relay is not None:
                # Drain completed = the channel intentionally left air; a
                # natural plan-end (automation restarts next tick) keeps the
                # relay so the relaunch splices into the same mux session.
                self._ts_relay.stop_channel(channel_id)
            if was_draining and self._hls_relay is not None:
                self._hls_relay.stop_channel(channel_id)
            self._write_state(channel_id, "STOPPED", last_error=stop_error)
            self._append_health(channel_id, "STOPPED", sink_connected={})
            # Round 5 (coordinator review): a clean exit with no pending
            # reload is genuinely off-air -- unlike the pending_reload
            # restart above (which keeps the channel effectively on air and
            # must NOT pop this, per _start's own no-pop rule) and unlike
            # the IMMEDIATE crash-relaunch below (same reasoning: it
            # re-resolves and starts a fresh plan right away), nothing is
            # left here to defer a switch against. Bypasses _stop entirely
            # (this route is reached from a worker exit _poll_process
            # observes on its own, not from an operator/drain stop), so
            # _stop's own pop never ran -- MEASURED to otherwise leave a
            # stale entry sitting here across the channel going fully dark
            # and being freshly restarted, ready to wrongly bind to a later,
            # unrelated plain reload. See ``_rollover_plan_end_at``'s
            # docstring. (The DEFERRED crash-relaunch branch inside
            # ``_relaunch_after_crash`` is a separate case, fixed in round 6:
            # it leaves no process running for the whole cooldown, so it
            # pops there too -- see that branch's own comment.)
            self._rollover_plan_end_at.pop(channel_id, None)
            return
        self._pending_reloads.pop(channel_id, None)
        state = self._store.read_state(channel_id)
        if state is not None and state.state in {"ON_AIR", "FALLBACK_SLATE", "TRANSITIONING"}:
            self._relaunch_after_crash(channel_id, state, uptime, returncode)
            return
        self._reset_restart_tracking(channel_id)
        self._clear_cg_overlay_proof(channel_id, "ERROR")
        self._close_as_run(channel_id)  # terminal error — close the open row
        self._write_state(
            channel_id,
            "ERROR",
            last_error=self._child_exit_error(
                channel_id, suffix="inspect the channel's worker logs before retrying."
            ),
        )
        self._append_health(channel_id, "ERROR", sink_connected={}, dropped_frames=0)
        # Round 5 (coordinator review): same reasoning as the clean-exit
        # branch above -- a terminal ERROR is off-air and bypasses _stop,
        # so this route must clear the entry itself.
        self._rollover_plan_end_at.pop(channel_id, None)

    def _relaunch_slate_eos_onto_due_program(self, channel_id: str) -> tuple[bool, str | None]:
        """A finite FALLBACK_SLATE plan reached EOS (clean exit, no pending
        reload): relaunch onto the program that is now due instead of going
        dark. Returns ``(relaunched, stop_error)``: ``(True, None)`` when the
        relaunch was taken (``_start`` was invoked, or the relaunch was
        handed to the crash back-off path -- either way the caller returns
        and that path owns every state write from here), otherwise
        ``(False, reason)`` where ``reason`` is ``None`` for the ordinary
        STOPPED outcome (the channel was not on slate, no program is due, or
        an operator stop/drain is already queued) and a human-readable
        ``last_error`` when the consecutive-relaunch cap refused another one.

        See ``_SLATE_EOS_RELAUNCH_MAX_CONSECUTIVE`` for the walkthrough that
        found this. The automation's slate replan normally reloads BEFORE the
        slate ends; this is the belt for the case where that reload's own
        synchronous prepare (a cold conform) outlasts the slate's horizon and
        the worker exits underneath it.

        Round-2 hostile review, items 1-3, in order below:

        * a queued operator ``stop``/``drain`` wins (``process_once`` polls
          BEFORE it drains commands, so without this peek the relaunch spawned
          a worker the Stop then had to kill, and the Stop waited behind a
          cold prepare);
        * the consecutive cap is a COUNT, not a time window (the slate plan
          runs up to 120s, so a 30s window never fired);
        * the relaunch honours the crash back-off exactly like
          ``_relaunch_after_crash``: a streak at or past
          ``_LIVE_SOURCE_FAILURE_FALLBACK_STREAK`` renews the slate
          (``force_fallback_slate``) instead of retrying the dead source, and
          a still-cooling ``_restart_latch`` defers through
          ``_backoff_relaunch`` instead of bypassing it. A slate RENEWAL does
          not count toward the cap: it is the B1 terminal state that replaces
          dead air, and ``ChannelAutomationService._check_slate_replan``
          retries the real source on its own cooldown.
        """
        state = self._store.read_state(channel_id)
        if state is None or state.state != "FALLBACK_SLATE":
            return False, None
        queued = [
            command.action
            for command in self._store.peek_pending_commands(channel_id)
            if command.action in {"stop", "drain"}
        ]
        if queued:
            _LOG.info(
                "channel %s: fallback slate reached its end with an operator %s already "
                "queued; not relaunching (the command drains this same tick).",
                channel_id,
                queued[0],
            )
            return False, None
        relaunches = self._slate_eos_relaunches.get(channel_id, 0)
        if relaunches >= _SLATE_EOS_RELAUNCH_MAX_CONSECUTIVE:
            reason = (
                "Fallback slate reached its end again after "
                f"{relaunches} automatic relaunch(es) onto the due program; the program's "
                "own start keeps falling back to slate, so the channel was stopped instead "
                "of looping. Check the program's media, then start the channel."
            )
            _LOG.warning("channel %s: %s", channel_id, reason)
            return False, reason
        streak = self._restart_streak.get(channel_id, 0)
        force_fallback_slate = (
            streak >= _LIVE_SOURCE_FAILURE_FALLBACK_STREAK
            and self._fallback_source_provider is not None
        )
        force_fallback_reason: str | None = None
        source_plan: EgressSourcePlan | None = None
        source_plan_resolution_started_at: float | None = None
        if force_fallback_slate:
            force_fallback_reason = (
                f"Live source failed to stay on air after {streak} consecutive "
                "crash-relaunches; the fallback slate reached its end and was renewed "
                "rather than retrying the failed source."
            )
        else:
            try:
                source_plan_resolution_started_at = self._monotonic()
                source_plan = self._source_plan_provider(channel_id)
            except SourcePrepareError as exc:
                _LOG.info(
                    "channel %s: fallback slate ended but the due program is not playable "
                    "yet (%s); leaving the channel stopped.",
                    channel_id,
                    exc,
                )
                return False, None
            if source_plan is None or source_plan.channel_id != channel_id:
                return False, None
            self._slate_eos_relaunches[channel_id] = relaunches + 1
        if not self._restart_latch.should_run_now(channel_id):
            # A crash-relaunch landed inside the cooldown just before this
            # slate ended. Defer exactly as _relaunch_after_crash does; the
            # deferred _begin_relaunch re-resolves the plan itself (so the
            # probe above is not threaded through) and applies the streak
            # rule on its own.
            self._backoff_relaunch[channel_id] = (state.state, state.current_source_label)
            self._write_state(
                channel_id,
                "STARTING",
                current_source_label=state.current_source_label,
                pid=None,
                last_error=(
                    "Fallback slate reached its end inside the crash-relaunch cooldown; "
                    "backing off before relaunching onto the due program."
                ),
            )
            self._append_health(channel_id, "STARTING", sink_connected={}, dropped_frames=0)
            self._rollover_plan_end_at.pop(channel_id, None)
            _LOG.info(
                "channel %s: fallback slate reached its end inside the restart cooldown; "
                "relaunch deferred to the back-off path.",
                channel_id,
            )
            return True, None
        if force_fallback_slate:
            _LOG.info(
                "channel %s: fallback slate reached its end while the live source is still "
                "failing (streak %d); renewing the slate instead of stopping.",
                channel_id,
                streak,
            )
        else:
            _LOG.info(
                "channel %s: fallback slate reached its end with a scheduled program due; "
                "relaunching onto %r instead of stopping.",
                channel_id,
                source_plan.segments[0].label if source_plan and source_plan.segments else "-",
            )
        # Mirror the pending-reload restart above: the row still carries the
        # exited slate worker's pid -- clear it before the fresh start.
        self._write_state(
            channel_id,
            "STARTING",
            current_source_label=state.current_source_label,
            pid=None,
        )
        self._start(
            channel_id,
            previous_state=state.state,
            previous_source_label=state.current_source_label,
            force_fallback_slate=force_fallback_slate,
            force_fallback_reason=force_fallback_reason,
            resolved_plan=source_plan,
            plan_resolution_started_at=source_plan_resolution_started_at,
        )
        return True, None

    def _relaunch_after_crash(
        self,
        channel_id: str,
        state: EgressStateRow,
        uptime: float | None,
        returncode: int | None = None,
    ) -> None:
        """Crash-relaunch with back-off (S9-5). The first crash relaunches at once;
        a crash recurring within the cooldown is paced (a deferred relaunch the
        ``process_once`` tick services once the latch permits) so a worker that keeps
        dying at startup can't hot-loop. A worker that ran healthily resets the streak.

        Item 82 (extended by item 84): a GStreamer worker that exits with
        ``civiccast.egress.gst.exit_codes.GST_PREROLL_TIMEOUT_EXIT_CODE`` (a slow,
        CPU-load-bound preroll) OR ``GST_FIRST_OUTPUT_TIMEOUT_EXIT_CODE`` (PLAYING
        was reached, but no output buffer crossed the mux within the engine's own
        separate bound -- see ``_SLOW_START_EXIT_CODES``) -- neither a crash --
        still relaunches through the exact same path below -- the existing
        back-off/cooldown pacing applies unchanged -- but does NOT advance the
        crash-loop streak more than once per ``_PREROLL_TIMEOUT_STREAK_COOLDOWN_S``
        (60s), shared across both exit reasons. Left uncapped, a train
        of successive slow starts (each individually a legitimate retry) would
        trip ``_LIVE_SOURCE_FAILURE_FALLBACK_STREAK`` and force the channel onto
        fallback slate for a source that was never actually unreachable -- the
        exact failure this rate limit exists to prevent, without weakening the
        streak's real job of catching a genuinely dead/unreachable source
        (an ordinary non-zero exit still increments on every single crash).

        Round-2 review BLOCKER (Opus, PR #183): a preroll-timeout exit's
        ``uptime`` (how long the worker was alive before it gave up) is NOT
        evidence of a healthy run -- it is simply how long the doomed preroll
        attempt took, which can legitimately be close to (engine.py now clamps
        it below) the ``_RESTART_STREAK_RESET_UPTIME_S`` (60s) reset threshold
        below. Applying that reset to a preroll-timeout exit would silently
        defeat the crash-loop escalation to fallback slate for a source that
        NEVER comes up (measured: 40 consecutive relaunches, streak stuck at
        0, before this exemption existed) -- so this reset is skipped
        entirely for that exit reason; only a genuinely different exit
        (successful start reaching PLAYING, then crashing later, or an
        ordinary crash) can benefit from it."""
        streak = self._restart_streak.get(channel_id, 0)
        if (
            uptime is not None
            and uptime >= _RESTART_STREAK_RESET_UPTIME_S
            and returncode not in _SLOW_START_EXIT_CODES
        ):
            # Belt-and-suspenders with the healthy-poll reset in _poll_process: that
            # path clears the streak while the worker is RUNNING healthily; this one
            # covers a worker that ran healthily and then crashed in the SAME gap
            # between polls (so the poll-reset never saw it). Either way a fresh
            # failure after a healthy run is streak 1 → immediate relaunch.
            streak = 0
            # Round-2 review nit: a healthy run also makes any earlier
            # preroll-timeout rate-limit window stale -- clear it so a FUTURE
            # preroll-timeout exit (a fresh problem, unrelated to whatever
            # last incremented the streak before this healthy stretch) is
            # never rate-limited by a now-irrelevant past timestamp.
            self._preroll_timeout_streak_incr_at.pop(channel_id, None)
        if returncode in _SLOW_START_EXIT_CODES:
            last_incr_at = self._preroll_timeout_streak_incr_at.get(channel_id)
            if (
                last_incr_at is not None
                and self._monotonic() - last_incr_at < _PREROLL_TIMEOUT_STREAK_COOLDOWN_S
            ):
                # Rate-limited: still persist any healthy-uptime reset above,
                # but don't advance the streak again inside this cooldown window.
                self._restart_streak[channel_id] = streak
            else:
                streak += 1
                self._restart_streak[channel_id] = streak
                self._preroll_timeout_streak_incr_at[channel_id] = self._monotonic()
        else:
            streak += 1
            self._restart_streak[channel_id] = streak
        # F-7: recovery can replace the live state with STARTING/ON_AIR in
        # this same tick. Notify the existing evaluator of the actual off-air
        # fault before that recovery so the channel-named alert is not skipped.
        if self._alert_evaluator_hook is not None:
            try:
                self._alert_evaluator_hook(channel_id, "ERROR", None, None)
            except Exception:
                _LOG.exception(
                    "Off-air alert evaluation failed for %s; continuing recovery", channel_id
                )
        failure_event = self._append_encoder_child_failure_event(
            channel_id, state, clean_exit=(returncode == 0)
        )
        proof_event_id = failure_event.event_id
        if streak >= _RESTART_ESCALATION_STREAK and streak % _RESTART_ESCALATION_STREAK == 0:
            # S8 hook: durably record the escalation now; alert dispatch lands in S8.
            escalation = self._append_restart_escalation_event(channel_id, state, streak)
            proof_event_id = escalation.event_id
        if self._restart_latch.should_run_now(channel_id):
            self._begin_relaunch(
                channel_id,
                state.state,
                state.current_source_label,
                proof_event_id,
                returncode=returncode,
            )
        else:
            # Within the cooldown after a recent relaunch — defer instead of hot-looping.
            self._backoff_relaunch[channel_id] = (state.state, state.current_source_label)
            self._write_state(
                channel_id,
                "STARTING",
                current_source_label=state.current_source_label,
                current_proof_event_id=proof_event_id,
                last_error=(
                    f"Encoder exited {'cleanly' if returncode == 0 else 'non-zero'} repeatedly "
                    f"(restart #{streak}); backing off "
                    "before relaunch to avoid a crash loop."
                ),
            )
            self._append_health(channel_id, "STARTING", sink_connected={}, dropped_frames=0)
            # Round 6 fix: unlike the immediate relaunch below (_begin_relaunch,
            # which re-resolves a fresh plan through _start right away) and the
            # pending-reload restart in _poll_process, this deferred branch
            # leaves NO process running for the entire cooldown -- STARTING
            # with nothing actually airing. _service_backoff_relaunch's own
            # eventual _begin_relaunch call re-resolves a fresh plan same as
            # the immediate case, so a value sitting here does not feed that
            # relaunch; it only survives to wrongly bind to a DIFFERENT,
            # later, unrelated plain operator reload landing during the
            # cooldown (MEASURED). Pop it here, same as every other off-air
            # route. See ``_rollover_plan_end_at``'s docstring.
            self._rollover_plan_end_at.pop(channel_id, None)

    def _service_backoff_relaunch(self, channel_id: str) -> None:
        """Fire a deferred crash-relaunch once the back-off latch permits. Driven by
        every ``process_once`` tick (automation polls each enabled channel), so a paced
        relaunch lands without any dedicated timer."""
        pending = self._backoff_relaunch.get(channel_id)
        if pending is None:
            return
        # #212 review MINOR-1: a queued operator stop/drain wins, same as the
        # peek in _relaunch_slate_eos_onto_due_program. This runs BEFORE
        # process_once drains commands, so without the peek the tick that
        # opens the latch spawned a worker the Stop then had to kill. Leave
        # the deferred entry alone: _process_command pops it for every action
        # on this same tick.
        queued = [
            command.action
            for command in self._store.peek_pending_commands(channel_id)
            if command.action in {"stop", "drain"}
        ]
        if queued:
            _LOG.info(
                "channel %s: back-off relaunch due with an operator %s already queued; "
                "not relaunching (the command drains this same tick).",
                channel_id,
                queued[0],
            )
            return
        if channel_id in self._processes:  # something already brought it back
            self._backoff_relaunch.pop(channel_id, None)
            return
        if not self._restart_latch.should_run_now(channel_id):
            return  # still cooling down
        self._backoff_relaunch.pop(channel_id, None)
        previous_state, previous_source_label = pending
        state = self._store.read_state(channel_id)
        proof_event_id = state.current_proof_event_id if state else None
        self._begin_relaunch(channel_id, previous_state, previous_source_label, proof_event_id)

    def _begin_relaunch(
        self,
        channel_id: str,
        previous_state: str,
        previous_source_label: str | None,
        proof_event_id: str | None,
        *,
        returncode: int | None = None,
    ) -> None:
        # BLOCKER B1: a crash-relaunch streak that has never once reached a
        # healthy uptime is the "unreachable/dropping live source" signature —
        # relaunching against the SAME source again would just repeat the
        # crash. Once the streak crosses the threshold, force the same
        # fallback-slate path _start already uses for
        # EncoderUnavailableError/FfmpegNotFoundError, instead of trusting the
        # source_plan_provider again — deliberately WITHOUT excluding a
        # ``previous_state == "FALLBACK_SLATE"`` crash: the streak (once
        # crossed) only clears on a healthy uptime (see _poll_process /
        # _reset_restart_tracking) or an explicit operator/automation command
        # (see _process_command popping _backoff_relaunch), so leaving this
        # unguarded keeps the channel LATCHED onto slate across a slate-encoder
        # crash too, instead of one relaunch attempt bouncing back to
        # re-resolving the still-dead live source via source_plan_provider
        # (which has no reason to behave differently) before the very next
        # crash forces fallback again — an avoidable ON_AIR/FALLBACK_SLATE
        # flap that is not the stable terminal slate state this fix exists to
        # provide. A crash-looping slate encoder itself is the deeper
        # zero-ffmpeg-floor case _start's own exception handler still covers,
        # unchanged, if the slate encoder can't even START.
        streak = self._restart_streak.get(channel_id, 0)
        force_fallback_slate = (
            streak >= _LIVE_SOURCE_FAILURE_FALLBACK_STREAK
            and self._fallback_source_provider is not None
        )
        force_fallback_reason = (
            f"Live source failed to stay on air after {streak} consecutive "
            "crash-relaunches; aired fallback slate instead of an infinite "
            "crash-loop against the dead source."
            if force_fallback_slate
            else None
        )
        # Item 85: a reload-commit-timeout exit (the worker's own commit
        # watchdog force-exiting out of a detected wedge -- see engine.py's
        # _arm_commit_watchdog) is a genuine failure of an already-running
        # channel, not a slow-but-progressing start -- classify it distinctly
        # in the relaunch log line so an operator/on-call reading last_error
        # sees "reload commit wedged" rather than a generic non-zero exit.
        # Deliberately NOT exempted from the crash-loop streak anywhere in
        # this method or _relaunch_after_crash above (unlike
        # GST_PREROLL_TIMEOUT_EXIT_CODE): every occurrence counts as an
        # ordinary crash toward escalation to fallback slate.
        relaunch_suffix = (
            "the reload commit itself did not finish in time "
            "(reload-commit-timeout); relaunching encoder."
            if returncode == GST_RELOAD_COMMIT_TIMEOUT_EXIT_CODE
            else "relaunching encoder."
        )
        self._write_state(
            channel_id,
            "STARTING",
            current_source_label=previous_source_label,
            current_proof_event_id=proof_event_id,
            last_error=(
                force_fallback_reason
                or (
                    "Worker exited cleanly while the channel was expected to stay on air; "
                    "relaunching encoder."
                    if returncode == 0
                    else self._child_exit_error(channel_id, suffix=relaunch_suffix)
                )
            ),
        )
        self._append_health(channel_id, "STARTING", sink_connected={}, dropped_frames=0)
        self._start(
            channel_id,
            previous_state=previous_state,
            previous_source_label=previous_source_label,
            force_fallback_slate=force_fallback_slate,
            force_fallback_reason=force_fallback_reason,
            # U47: this is THE relaunch of a channel that was on air. It is where
            # the live finding was observed -- a worker exit followed by the
            # channel going dark for the whole cold conform of the next program
            # (public-exit1-1119: dark from 11:19:40, still dark at 11:20:29;
            # public-exit0-0159: ~11 min). Air the slate first; the program
            # follows through the reload path. Inert when B1 forced the slate
            # above (that branch is checked first in ``_start_steps`` and has no
            # program to hand off).
            slate_first=True,
        )
        # CC-WS5-006: a crash-relaunch brings up a FRESH worker on a fresh control
        # pipe. Replay the channel's desired state (reload/swap) over it so a swap
        # or content-reload that was live before the crash is restored rather than
        # silently lost. Optional strategy capability (only the GStreamer worker-pipe
        # strategy carries a control channel) — resolved via getattr like
        # send_command; a no-op on POSIX / for a strategy without the seam.
        self._reconnect_worker_channel(channel_id)

    def _reconnect_worker_channel(self, channel_id: str) -> None:
        reconnect = getattr(self._encoder_strategy, "reconnect_channel", None)
        if callable(reconnect):
            reconnect(channel_id)

    def _close_worker_channel(self, channel_id: str) -> None:
        """Release a channel's worker control pipe (CC-WS5-006). Optional strategy
        capability resolved via getattr; a no-op on POSIX / for a strategy without
        a worker-pipe seam / for an unknown channel."""
        close_channel = getattr(self._encoder_strategy, "close_channel", None)
        if callable(close_channel):
            close_channel(channel_id)

    def _reset_restart_tracking(
        self, channel_id: str, *, slate_eos_relaunches: bool = True
    ) -> None:
        """Clear crash-relaunch back-off state — the channel reached a good state
        (clean stop, error terminal, or a healthy run).

        ``slate_eos_relaunches``: also clear the slate-EOS automatic-relaunch
        counter (round-2 hostile review, item 4: ``_stop`` and every other
        off-air route reach this and must not leave a stale count behind).
        The healthy-uptime caller in ``_poll_process`` passes False while the
        channel is airing the fallback slate -- see the comment there.
        """
        self._restart_streak.pop(channel_id, None)
        self._preroll_timeout_streak_incr_at.pop(channel_id, None)
        self._backoff_relaunch.pop(channel_id, None)
        self._restart_latch.force_reset(channel_id)
        if slate_eos_relaunches:
            self._slate_eos_relaunches.pop(channel_id, None)

    def _append_encoder_child_failure_event(
        self,
        channel_id: str,
        state: EgressStateRow,
        *,
        clean_exit: bool = False,
    ) -> EgressProofEvent:
        exit_source = "ffmpeg-child:clean-exit" if clean_exit else "ffmpeg-child:nonzero-exit"
        event = EgressProofEvent(
            event_id=f"egress-encoder-child-relaunch-{uuid.uuid4()}",
            observed_at=datetime.now(UTC),
            channel_id=channel_id,
            state="STARTING",
            source_label=state.current_source_label or "Unknown egress source",
            source_path=exit_source,
            source_ref=state.current_proof_event_id,
            proof_boundary="civiccast-egress-handoff-boundary",
            machine_summary=(
                "CivicCast detected a worker that exited cleanly while the channel was "
                "expected to stay on air; the daemon kept running and started encoder relaunch."
                if clean_exit
                else "CivicCast detected a non-zero FFmpeg child exit while the channel was "
                "expected to stay on air; the daemon kept running and started encoder relaunch."
            ),
        )
        self._store.append_proof_event(event)
        return event

    def _append_restart_escalation_event(
        self,
        channel_id: str,
        state: EgressStateRow,
        streak: int,
    ) -> EgressProofEvent:
        """Durably record that a channel has crash-relaunched repeatedly in a short
        window. This is the S8 alerting hook seam: the proof event lands now; the
        operator-facing alert dispatch is wired when S8 alerting lands (build step 4)."""
        event = EgressProofEvent(
            event_id=f"egress-encoder-restart-escalation-{uuid.uuid4()}",
            observed_at=datetime.now(UTC),
            channel_id=channel_id,
            state="STARTING",
            source_label=state.current_source_label or "Unknown egress source",
            source_path="ffmpeg-child:restart-escalation",
            source_ref=state.current_proof_event_id,
            proof_boundary="civiccast-egress-handoff-boundary",
            machine_summary=(
                f"CivicCast encoder for this channel has crash-relaunched {streak} times in "
                "a short window; output is unstable. The daemon keeps retrying with back-off. "
                "Investigate the source/sink before air; an operator alert is dispatched here "
                "once S8 alerting is in service."
            ),
        )
        self._store.append_proof_event(event)
        return event

    def _reap_orphan(self, channel_id: str) -> None:
        """Terminate a predecessor server's still-running encoder (issue #161).

        Called on every fresh start (this daemon tracks no live process for
        the channel). The durable state row carries the last known encoder
        pid; if that pid is alive AND its process image is ffmpeg, it is an
        orphan from a previous server process still streaming to the sink
        port - two writers on one UDP destination corrupt the feed. A pid
        reused by an unrelated program is never touched.
        """

        state = self._store.read_state(channel_id)
        if state is None or state.pid is None:
            return
        # Audit ENG-001: a pid this daemon tracks belongs to it - never a
        # predecessor's orphan.
        tracked = {_process_pid(p) for p in self._processes.values()}
        if state.pid in tracked:
            return
        info = self._orphan_probe(state.pid)
        if info is None or "ffmpeg" not in info.name.lower():
            return
        if info.created_at >= self._boot_epoch:
            # Created after this server booted: a recycled pid on a FRESH
            # ffmpeg (another channel, a conform job, a relay). Not ours to
            # kill.
            return
        self._orphan_terminator(state.pid, info.created_at)
        _LOG.warning(
            "Reaped orphaned encoder pid %s for channel %s (left by a "
            "previous server process; it was still streaming to the sink).",
            state.pid,
            channel_id,
        )
        self._store.append_proof_event(
            EgressProofEvent(
                event_id=f"egress-orphan-reap-{uuid.uuid4()}",
                observed_at=datetime.now(UTC),
                channel_id=channel_id,
                state="STARTING",
                source_label=state.current_source_label or "unknown",
                source_path=f"orphan-pid-{state.pid}",
                source_ref=None,
                proof_boundary="civiccast-egress-handoff-boundary",
                machine_summary=(
                    f"CivicCast reaped an orphaned encoder (pid {state.pid}) for "
                    f"channel {channel_id!r} left by a previous server process "
                    "before starting a fresh encoder."
                ),
            )
        )

    def _release_prepared_plan_dir(self, plan_dir: Path | None) -> None:
        """Best-effort call into the configured ``prepared_plan_release`` hook
        (F3) -- a no-op if no hook is configured or ``plan_dir`` is None. A
        release hiccup must never break the caller's own state transition."""
        if self._prepared_plan_release is None or plan_dir is None:
            return
        with contextlib.suppress(Exception):
            self._prepared_plan_release(plan_dir)

    def _discard_pending_reload_settlement(self, channel_id: str, *, reason: str) -> None:
        """Hostile-review follow-up (2026-09-06), items 1 & 3: drop any
        armed-but-unsettled reload tracking for ``channel_id`` and release its
        plan directory immediately (never leave it for GC/the 960s deadline).

        Called from every path where the pending attempt is definitely moot:
        the worker that would have settled it exited (``_poll_process``), the
        channel is restarting fresh (``_start``), a newer reload superseded it
        before it settled (``_try_content_reload``, item 3 -- the previous
        code silently overwrote ``_pending_reload_settle`` here, leaking the
        replaced entry's plan_dir), and the explicit restart fallback
        (``_fall_back_to_restart_reload``) as a defensive backstop.

        Records the discarded reload_id (``_discarded_reload_ids``) so a
        LATE-arriving status write for that dead attempt is recognized and
        logged as ignored by ``_poll_reload_settlement``, instead of the
        previous silent no-op (pending already gone, nothing left to compare
        against) that gave no evidence the late write was even seen.

        Deliberately does NOT clear ``_discarded_reload_ids`` when there is no
        pending entry to discard: this method is called defensively from
        several places in a row for the SAME exit (e.g. ``_poll_process``'s
        crash branch calling ``_start``, which calls this again) -- an
        unconditional clear here would wipe out the very tracking the FIRST
        call just recorded before ``_poll_reload_settlement`` ever gets a
        chance to observe a late arrival against it."""
        pending = self._pending_reload_settle.pop(channel_id, None)
        if pending is None:
            return
        _LOG.info(
            "Content-reload for %s (reload_id=%s) discarded: %s.",
            channel_id,
            pending.reload_id,
            reason,
        )
        self._discarded_reload_ids[channel_id] = pending.reload_id
        self._release_prepared_plan_dir(pending.plan_dir)

    def _discard_active_prepared_plan_dir(self, channel_id: str) -> None:
        """Hostile-review follow-up, item 4: release the plan directory
        backing whatever ``channel_id`` was ACTUALLY airing (tracked from both
        ``_start`` and ``_commit_reload_settlement``), once the worker reading
        it is confirmed gone -- a real process exit (``_poll_process``), a
        fresh ``_start`` past its already-alive guard (which only reaches
        here when the previously-tracked process has already exited), a
        direct (non-draining) operator stop (``_stop(channel_id,
        draining=False)``, whose immediate ``_process_terminate`` call above
        it makes exit synchronous), or ``stop_all_channels``'s own
        observed-exit loop for the DRAINING case (hostile-review follow-up,
        third pass, P2: ``_stop(channel_id, draining=True)`` only sends the
        worker its graceful TERMINAL command and returns -- the worker is
        still airing, possibly for the entire ``deadline_seconds`` window, so
        ``_stop`` itself must NOT call this for a draining stop; the previous
        docstring here claiming the worker is "confirmed gone" on every
        ``_stop`` call was wrong for exactly that path). Deliberately NOT
        called from ``_fall_back_to_restart_reload``: that path's worker may
        still be alive and draining/airing the very plan this would
        release."""
        self._release_prepared_plan_dir(self._active_prepared_plan_dir.pop(channel_id, None))

    def _stash_prepared_restart_plan(self, channel_id: str, plan: _ReusePreparedPlan) -> None:
        """F3(b): hold the plan a FALLBACK_SLATE reload prepared, for the
        restart that will air it.

        Supersedes -- and releases -- any plan still held for this channel
        first, mirroring ``_try_content_reload``'s handling of a still-pending
        previous reload attempt: silently overwriting here would leak the
        replaced plan's directory. A held plan's ``plan_dir`` is part of
        ``live_prepared_plan_dirs`` for as long as it is held, so the
        preparer's own GC cannot evict a directory this daemon still intends
        to air."""
        self._discard_prepared_restart_plan(
            channel_id, reason="superseded by a newer prepared restart"
        )
        self._prepared_restart_plans[channel_id] = plan

    def _discard_prepared_restart_plan(self, channel_id: str, *, reason: str) -> None:
        """F3(b): release any prepared-restart plan held for ``channel_id``.

        Called from every path where the restart that would have consumed it
        can no longer happen -- operator stop, drain, a worker exit that did
        not take the pending-reload restart, shutdown, and a superseding
        attempt -- so a held directory is never left for GC alone."""
        self._release_prepared_restart_plan(
            channel_id, self._prepared_restart_plans.pop(channel_id, None), reason=reason
        )

    def _discard_all_prepared_restart_plans(self, *, reason: str) -> None:
        """F3(b): release every held prepared-restart plan this daemon owns.

        The service-shutdown sweep: no channel's restart can run after it, so
        every held directory must be released here rather than left to GC.
        Snapshot the keys first -- ``_discard_prepared_restart_plan`` mutates
        the dict being iterated."""
        for channel_id in tuple(self._prepared_restart_plans):
            self._discard_prepared_restart_plan(channel_id, reason=reason)

    def _release_prepared_restart_plan(
        self,
        channel_id: str,
        plan: _ReusePreparedPlan | None,
        *,
        reason: str,
    ) -> None:
        """Release one given prepared-restart plan (no-op when ``None``).

        Separate from ``_discard_prepared_restart_plan`` for the caller that
        already holds the plan itself rather than the stash entry: ``_start``
        takes it as an argument and must release it when its own
        already-preparing guard means this start will not run."""
        if plan is None:
            return
        report = plan.report
        _LOG.info(
            "Prepared restart plan for %s released unused (%s): label=%r plan_dir=%s.",
            channel_id,
            reason,
            report.source_plan.segments[0].label if report.source_plan.segments else "-",
            report.plan_dir,
        )
        self._release_prepared_plan_dir(report.plan_dir)

    def live_prepared_plan_dirs(self, channel_id: str) -> frozenset[Path]:
        """Hostile-review follow-up, item 5: every ``SourcePreparer`` per-plan
        directory this daemon currently considers LIVE for ``channel_id`` --
        the active on-air plan and any armed-but-not-yet-settled reload's
        plan. Wired into the configured ``SourcePreparer`` (via
        ``set_protected_plan_dirs_provider``, see cli.py/automation.py) as the
        ``keep=`` set its own GC pass must never evict, regardless of age,
        size, or keep-N recency -- closing the gap where nothing but this
        daemon actually knows which directories are still referenced."""
        pending = self._pending_reload_settle.get(channel_id)
        # F3(b): a report held for an in-flight FALLBACK_SLATE restart is just
        # as live as an armed reload's -- the restart is about to air it, so
        # the preparer's GC must keep its directory too.
        restart_plan = self._prepared_restart_plans.get(channel_id)
        return frozenset(
            plan_dir
            for plan_dir in (
                self._active_prepared_plan_dir.get(channel_id),
                pending.plan_dir if pending is not None else None,
                restart_plan.report.plan_dir if restart_plan is not None else None,
            )
            if plan_dir is not None
        )

    def record_rollover_plan_end(
        self,
        channel_id: str,
        plan_end_at: datetime,
        *,
        command_id: str | None,
        force_fallback: bool = False,
        min_plan_seconds: float | None = None,
    ) -> None:
        """Public capability ``ChannelAutomationService`` calls (mirrors the
        ``dispatched_plan_horizon``/``has_manual_override`` getattr-probed
        shape) immediately before it enqueues a plan-rollover reload command,
        so ``_try_content_reload`` can tell -- against wall-clock ``now`` at
        the moment it actually dispatches -- whether the switch may still
        defer to the outgoing leg's own EOS, or must cut immediately because
        the horizon it was computed against has already passed (item 78 fix
        3). See ``_rollover_plan_end_at``'s docstring for why this rides an
        in-memory side channel rather than the durable command queue.

        Round 7 (coordinator review): ``command_id`` should be the id of the
        SAME reload command the caller is about to enqueue (``_enqueue``
        returns the id it generated for exactly this purpose) -- it scopes
        the recorded value so ``_request_reload`` only hands it to
        ``should_defer_switch`` when the command actually draining is this
        one, never a different reload that happens to land first.

        Round 8 (coordinator review): ``command_id`` is now a REQUIRED
        keyword-only argument -- the round-7 ``None`` default doubled as a
        "wildcard, matches whatever drains next" behavior that zero
        production callers actually relied on (``ChannelAutomationService``
        is the only recorder, in `automation.py`, and it always supplies a
        real generated id). Removing the default forces every caller to say
        explicitly whether it is scoping a real id or deliberately recording
        unscoped (``command_id=None``) -- and ``None`` is no longer special:
        it matches a drain whose own ``command_id`` is also ``None`` by
        ordinary equality (see ``_request_reload``'s docstring), not by a
        wildcard carve-out.

        U41: ``min_plan_seconds`` is the horizon the dispatcher measured for
        this rollover (its own lead), carried alongside the boundary for the
        same reason and under the same command scoping. It is applied only to
        a DEFERRED switch: a deferred rollover's plan is built at dispatch but
        takes air at the outgoing item's own end, so a horizon measured from
        dispatch is partly spent by the time the plan is on air (live
        2026-09-25 education: 689 s planned at 23:41:56, 83 s left at the
        23:53:17 switch, EOS at 23:54:41). With the horizon measured from the
        switch instead, the plan still has a full lead of life when it takes
        air. ``None`` (every non-GStreamer deployment, and any recorder that
        does not measure a lead) is today's behavior exactly."""
        self._rollover_plan_end_at[channel_id] = (
            command_id,
            plan_end_at,
            force_fallback,
            min_plan_seconds,
        )

    def has_pending_reload_settlement(self, channel_id: str) -> bool:
        """Public capability ``ChannelAutomationService`` probes (via
        ``getattr``, like ``has_manual_override``/``dispatched_plan_horizon``)
        so its rollover cadence latch can tell "armed, still settling" (never
        retry -- wait for this daemon's own deadline) apart from "genuinely
        dropped" (retry after ``_ROLLOVER_ISSUED_TIMEOUT_SECONDS``)."""
        return channel_id in self._pending_reload_settle or channel_id in self._preparations

    def worker_initial_control_connection_observed(self, channel_id: str) -> bool:
        """Return whether the worker has made its first control connection.

        Strategies without in-place reloads take the existing planned-restart
        path and need no control-channel gate. Readiness is optional for other
        strategies so their established behavior remains unchanged.
        """

        if not getattr(self._encoder_strategy, "supports_content_reload", False):
            return True
        reader = getattr(self._encoder_strategy, "worker_initial_control_connection_observed", None)
        if not callable(reader):
            return True
        return bool(reader(channel_id))

    def has_pending_preparation(self, channel_id: str) -> bool:
        """Whether a media preparation for this channel is registered right now.

        U47: the reader ``ChannelAutomationService._check_slate_replan`` uses to
        tell "this daemon is already preparing the program this channel is
        waiting for" apart from "nobody is doing anything, so queue a reload".
        Without it the automation's slate-replan pass fires into the middle of a
        slate-first hand-off's OWN preparation: the queued ``reload`` command
        reaches ``_request_reload`` -> ``_cancel_preparation``, which abandons
        the in-flight hand-off and conforms the same program a second time
        (measured 20-133 s per conform in the field).

        Deliberately NARROWER than ``has_pending_reload_settlement`` above, which
        also reports an armed reload still settling. Reusing that one here would
        additionally hold the automation off for the whole settle window -- a
        behavior change outside U47's scope, on the one path (a rollover's
        settlement) that already has its own latch and cooldown.

        Read from the automation thread, so ``_preparation_guard`` is not taken:
        ``self._preparations`` is only ever mutated under that lock by the daemon
        thread, and the worst a torn read can do is make this answer stale by one
        tick -- far cheaper than blocking the automation loop on a conform."""
        return channel_id in self._preparations

    def _hand_off_slate_first_program(self, channel_id: str) -> None:
        """U47: put the program a slate-first start owes onto the air, using the
        reload machinery rather than another start.

        Reached from ``_start`` (the synchronous path, where the slate is already
        up and the steps returned ``SLATE_FIRST``) and from ``_poll_preparation``
        (the async path, where the same value arrives when the slate's own
        preparation settles).

        ``_request_reload(slate_first=True)`` is what makes this safe on failure:
        a hand-off whose program cannot be prepared keeps the live slate and
        returns, instead of running the ordinary restart fallback -- which, out
        of a live FALLBACK_SLATE, terminates the slate worker (issue #157) and
        so would take a channel that is ON AIR and make it dark for a second
        conform. That is exactly the outcome this unit exists to prevent.

        The live-process guard is not redundant. ``_start`` returns SLATE_FIRST
        after a successful launch, but the worker can be gone again by the time
        this runs (a fast crash), and ``_request_reload``'s no-live-process
        branch would then call ``_start`` -- which is correct, and is why this
        method does not attempt it here itself."""
        process = self._processes.get(channel_id)
        if process is None or _process_poll(process) is not None:
            return
        _LOG.info(
            "channel %s: fallback slate is on air; handing the prepared program in "
            "through the reload path",
            channel_id,
        )
        self._request_reload(channel_id, slate_first=True)

    def _keep_slate_after_failed_hand_off(self, channel_id: str) -> None:
        """U47: a slate-first hand-off whose program could not be prepared keeps
        the slate that is already on air.

        Called from both halves of the same failure. The synchronous one ends at
        the tail of ``_request_reload``; the asynchronous one settles in
        ``_poll_preparation``, where the failed reload is the ``outcome is
        False`` case. Both would otherwise run ``_fall_back_to_restart_reload``,
        which out of a live FALLBACK_SLATE terminates the slate worker (issue
        #157) and leaves the channel dark until the retry's conform finishes --
        the second darkness this unit exists to remove, arrived at from the other
        side.

        Deliberately does nothing but log: the slate worker is alive and airing,
        the channel's state is already FALLBACK_SLATE, and the ordinary
        slate-replan policy owns the retry. Restarting it, or writing the state
        again, would be the darkness with extra steps."""
        _LOG.warning(
            "channel %s: slate-first hand-off could not prepare the program; "
            "keeping the fallback slate on air and leaving the retry to the "
            "slate-replan policy.",
            channel_id,
        )

    def _fall_back_to_restart_for_reused_plan(
        self, channel_id: str, outcome: _ReusePreparedPlan
    ) -> bool:
        """F3(b): take the terminate+restart path for a reload that declined
        the in-place selector swap, carrying the plan it already prepared so
        the restart does not conform it a second time.

        Returns ``False`` -- "not armed" -- which is what both call sites
        mean by it: ``_poll_preparation`` ignores the value, and
        ``_try_content_reload`` passes it up so ``_request_reload`` runs its
        own (report-less, therefore harmless) fallback exactly as it does for
        a declined arm today."""
        self._fall_back_to_restart_reload(channel_id, prepared_reload=outcome)
        return False

    def _try_content_reload(
        self,
        channel_id: str,
        state: EgressStateRow,
        process: object,
        *,
        rollover_plan_end_at: datetime | None,
        rollover_plan_min_seconds: float | None = None,
        force_fallback: bool = False,
        slate_first: bool = False,
    ) -> bool | _PreparationState:
        self._cancel_preparation(channel_id)
        result = self._drive_preparation(
            channel_id,
            self._reload_steps(
                channel_id,
                state,
                process,
                rollover_plan_end_at=rollover_plan_end_at,
                rollover_plan_min_seconds=rollover_plan_min_seconds,
                force_fallback=force_fallback,
            ),
            kind="reload",
            slate_first=slate_first,
        )
        if isinstance(result, _ReusePreparedPlan):
            # F3(b): the SYNCHRONOUS preparation path (no
            # ``enable_async_preparation`` executor) completes the steps inline,
            # so the decision reaches here directly instead of via
            # ``_poll_preparation``. Same outcome, same handling -- without
            # this the carried plan would be dropped and the reload silently
            # never restarted.
            return self._fall_back_to_restart_for_reused_plan(channel_id, result)
        return result if result is not None else False

    def _resolve_schedule_tail(
        self,
        channel_id: str,
        source_plan: EgressSourcePlan,
        config: EgressConfig,
        *,
        resolved_at: datetime | None = None,
        slate_when_no_next: bool = False,
    ) -> _TailFloorOutcome:
        """Refuse to install a schedule tail shorter than the switch that does it.

        U26 defect 2, generalized by U36 decision 2. A plan that resolves the
        item due at some instant I -- wall-clock now (automation's slate gap
        replan in automation.py's ``_check_slate_replan``, an operator reload,
        the F3(b) reused-plan restart), a recorded rollover horizon, or a crash
        relaunch -- can land on an item whose slot is nearly over. The resolver
        then hands back a legal but degenerate plan (see
        ``_SCHEDULE_TAIL_FLOOR_SECONDS``) that the channel airs to its end and
        then EOSes on, costing a whole second restart to reach the program that
        was due all along. U36's incident is the same defect on the two paths
        the U26 guard skipped: the 14:06:50 horizon-bound rollover (whose new
        leg delivered 1.6s before EOS -- ``mux-in 1.6s: video=+24 audio=+37``
        -- on a horizon the plan recorded 3.65s past the outgoing leg's real
        end) and the 14:17:52 crash relaunch (a single sub-floor segment).

        So resolve the schedule where that tail ENDS instead -- the next item --
        and use it when it is a strict improvement. Everything else keeps the
        tail plan untouched, because in every one of those cases airing the tail
        is the honest behavior: a media-shorter-than-slot tail, a gap the fill
        policy owns, the end of the schedule, or a boundary that fails to
        prepare all resolve to ``None`` or to the same/short plan.

        A newly selected plan with known original playable media windows (after
        asset trims, before join, slot, or preparation-horizon clipping) whose
        combined duration is itself below the floor is an intentional short
        programme, not a near-EOS tail of a longer programme. Preserve it whole.
        If the same applied source is selected again at a later JIP inpoint, it
        is the earlier programme's remainder and gets the normal tail check.
        Plans without that provenance keep the existing conservative behavior.

        It cannot skip a program that genuinely has more than the floor left:
        the body returns ``source_plan`` unchanged (identity, not a copy) before
        any boundary call whenever the tail is at or above
        ``_SCHEDULE_TAIL_FLOOR_SECONDS``, and even below the floor the
        replacement is only taken when the plan due where the tail ends is
        STRICTLY longer than the tail it replaces. A tail of 30s or more is
        therefore never even measured against the schedule, and a shorter one is
        only ever exchanged for more media than it had.

        ``resolved_at`` is the instant the plan was resolved for. Callers that
        resolved at a known instant (a recorded rollover horizon) pass it, so
        the second look lands where THAT tail ends rather than where
        wall-clock now happens to be; callers that resolved at wall-clock now
        pass nothing.

        ``slate_when_no_next`` (U36 decision 2: "the leg starts the NEXT program
        instead (slate only if there is no next program)") arms the fallback
        slate for the two paths where a sub-floor tail is otherwise aired to an
        EOS that takes the worker down -- the horizon-bound rollover and the
        crash relaunch. The U26 no-horizon call site leaves it False and keeps
        its documented contract: ``None`` is never returned there, because a
        declined plan would send the caller to ``_request_reload``'s
        terminate+restart fallback, which is what the tail plan is already
        about to get anyway.
        """

        boundary_provider = self._boundary_source_plan_provider
        if boundary_provider is None or self.has_manual_override(channel_id):
            # An operator override owns plan resolution and resolves at
            # wall-clock now by its own contract (see the head of
            # ``_reload_steps``): never reach past it for a later boundary.
            return _TailFloorOutcome(source_plan, False)
        tail_seconds = sum(segment.duration_seconds for segment in source_plan.segments)
        playable_windows = [segment.playable_duration_seconds for segment in source_plan.segments]
        if (
            tail_seconds > 0.0
            and all(segment.kind == "program" for segment in source_plan.segments)
            and all(duration is not None for duration in playable_windows)
            and sum(float(duration) for duration in playable_windows if duration is not None)
            < _SCHEDULE_TAIL_FLOOR_SECONDS
            and not self._is_dispatched_program_remainder(channel_id, source_plan)
        ):
            return _TailFloorOutcome(source_plan, False)
        if tail_seconds <= 0.0 or tail_seconds >= _SCHEDULE_TAIL_FLOOR_SECONDS:
            return _TailFloorOutcome(source_plan, False)
        base_at = datetime.now(UTC) if resolved_at is None else resolved_at
        boundary_at = base_at + timedelta(seconds=tail_seconds + _SCHEDULE_TAIL_BOUNDARY_MARGIN_S)
        try:
            next_plan = boundary_provider(channel_id, boundary_at)
        except SourcePrepareError:
            next_plan = None
        if next_plan is None or next_plan.channel_id != channel_id:
            if slate_when_no_next and self._fallback_source_provider is not None:
                # Nothing is scheduled where this tail ends. Airing the tail
                # would EOS the leg and take the worker down; slate at the
                # boundary instead.
                try:
                    slate = self._fallback_source_provider(config)
                except SourcePrepareError:
                    return _TailFloorOutcome(source_plan, False)
                if slate.channel_id != channel_id:
                    return _TailFloorOutcome(source_plan, False)
                _LOG.info(
                    "Plan for %s resolved to a %.1fs tail of the closing scheduled item "
                    "and nothing is scheduled where that tail ends; airing the fallback "
                    "slate at the boundary instead.",
                    channel_id,
                    tail_seconds,
                )
                return _TailFloorOutcome(slate, True)
            return _TailFloorOutcome(source_plan, False)
        next_seconds = sum(segment.duration_seconds for segment in next_plan.segments)
        if next_seconds <= tail_seconds:
            return _TailFloorOutcome(source_plan, False)
        _LOG.info(
            "Plan for %s resolved to a %.1fs tail of the closing scheduled item; "
            "using the plan due where that tail ends instead (%.1fs).",
            channel_id,
            tail_seconds,
            next_seconds,
        )
        return _TailFloorOutcome(next_plan, False)

    def _is_dispatched_program_remainder(
        self,
        channel_id: str,
        source_plan: EgressSourcePlan,
    ) -> bool:
        """Whether this short plan resumes the same dispatched occurrence later.

        A complete short programme is allowed to air on its first selection.
        The schedule provider can later return that same item with a smaller
        JIP remainder while its slot is still open. Its occurrence ID separates
        it from a later scheduled use of the same asset, while the applied
        source/inpoint check prevents the short-media exception from rearming
        the EOS remainder indefinitely.
        """

        dispatched = self._dispatched_program_source.get(channel_id)
        state = self._store.read_state(channel_id)
        if (
            dispatched is None
            or state is None
            or state.current_proof_event_id != dispatched[0]
            or not source_plan.segments
        ):
            return False
        _, occurrence_id, source_ref, inpoint = dispatched
        segment = source_plan.segments[0]
        if (
            segment.kind != "program"
            or segment.schedule_occurrence_id != occurrence_id
            or segment.source_ref != source_ref
        ):
            return False
        resolved_inpoint = float(segment.inpoint_seconds or 0.0)
        return resolved_inpoint > inpoint

    def _chain_next_program(
        self,
        channel_id: str,
        filler_plan: EgressSourcePlan,
        *,
        boundary_at: datetime | None,
    ) -> tuple[EgressSourcePlan, datetime | None]:
        """U53 item 1: trim a rollover filler to the gap and chain the programme due
        at its end into the SAME plan.

        Returns ``(plan, chained_program_end_at)``. ``plan`` is ``filler_plan``
        unchanged -- and the end is ``None`` -- for every case this cannot do
        honestly: no ``next_program_start_provider`` wired, no boundary to measure
        the gap from, no next programme due, nothing but filler (no boundary plan
        provider), a programme that would arrive before the filler ends, or a chain
        that would exceed ``MAX_PLAYLIST_SUBCHAINS`` (the bridge raises
        ``PlaylistCapBypassedError`` for an oversize non-slate/cg plan, and failing
        that check closed would take a channel off air).

        Why this is a plan shape and not a second plan: the daemon and the engine
        are one-plan-per-channel (``_record_dispatched_plan`` /
        ``GstPlayoutEngine.reload_program`` replace the whole program leg), so the
        only way filler and programme can be dispatched together -- the programme
        prepared at the same moment, with the filler's own lead -- is for them to BE
        that one plan. The filler's declared durations are what the preparer writes
        to each per-plan file (``preparer.py``'s ``-t segment.duration_seconds`` on
        the bounded conform and on the GStreamer engine's cache-hit copy-out), and
        the bridge builds one decoder sub-chain per segment that runs to that file's
        EOS, so trimming the filler's LAST segment to the remaining gap is what
        makes the hand-off land on the programme's scheduled start.

        Measured on this station (U53 Part I): the 17:57-18:07 education rollover
        had a 554s plan on air, a filler target, and the next programme due 554s
        later; the filler aired whole and the programme followed it instead. The
        failure this avoids is the one item 3's adaptive lead only partly covers --
        the lead decides WHEN to start preparing, not what to air while the
        programme prepares."""

        if self._next_program_start_provider is None or boundary_at is None:
            return filler_plan, None
        if self._boundary_source_plan_provider is None:
            # Without a boundary provider there is no honest way to resolve the
            # programme due at the gap: the only other provider answers "what is
            # active NOW", which in this gap is the filler itself.
            return filler_plan, None
        try:
            next_start = self._next_program_start_provider(channel_id, boundary_at)
        except Exception:
            _LOG.warning(
                "Could not resolve the next scheduled programme after %s for %s; "
                "airing the filler whole.",
                boundary_at.isoformat(),
                channel_id,
                exc_info=True,
            )
            return filler_plan, None
        if next_start is None:
            return filler_plan, None
        gap_seconds = (next_start - boundary_at).total_seconds()
        if gap_seconds <= 0.0:
            # The programme is already due at the boundary: this rollover is not
            # filling a gap, so there is nothing to trim and nothing to chain.
            return filler_plan, None
        trimmed: list[EgressSourceSegment] = []
        remaining = gap_seconds
        for segment in filler_plan.segments:
            duration = float(segment.duration_seconds)
            if remaining > duration:
                trimmed.append(segment)
                remaining -= duration
                continue
            # The gap lands inside this segment: keep it, truncated, and drop the
            # rest of the filler -- the programme starts where the gap ends.
            #
            # ``model_validate`` rather than ``model_copy(update=...)``: a copy
            # skips validation, and ``duration_seconds`` is constrained ``gt=0``.
            # The arithmetic above guarantees ``remaining`` is positive here, and
            # validating proves it rather than assuming it.
            trimmed.append(
                EgressSourceSegment.model_validate(
                    {**segment.model_dump(), "duration_seconds": remaining}
                )
            )
            remaining = 0.0
            break
        if remaining > 0.0:
            # The filler's own declared run is SHORTER than the gap. Chaining the
            # programme now would put it on air before it is due -- strictly worse
            # than airing the filler whole, which the next rollover then handles.
            _LOG.info(
                "Rollover filler for %s declares %.1fs but the next programme is due in "
                "%.1fs; airing the filler whole (it will be re-evaluated at its own end).",
                channel_id,
                sum(float(segment.duration_seconds) for segment in filler_plan.segments),
                gap_seconds,
            )
            return filler_plan, None
        try:
            program_plan = self._boundary_source_plan_provider(channel_id, next_start)
        except Exception:
            _LOG.warning(
                "Could not resolve the programme due at %s for %s; airing the filler whole.",
                next_start.isoformat(),
                channel_id,
                exc_info=True,
            )
            return filler_plan, None
        if (
            program_plan is None
            or program_plan.channel_id != channel_id
            or not program_plan.segments
        ):
            return filler_plan, None
        if len(trimmed) + len(program_plan.segments) > MAX_PLAYLIST_SUBCHAINS:
            _LOG.warning(
                "Not chaining the programme due at %s behind the filler for %s: %d filler + "
                "%d programme segments exceeds the %d-subchain playlist cap; airing the "
                "filler whole.",
                next_start.isoformat(),
                channel_id,
                len(trimmed),
                len(program_plan.segments),
                MAX_PLAYLIST_SUBCHAINS,
            )
            return filler_plan, None
        program_seconds = sum(float(segment.duration_seconds) for segment in program_plan.segments)
        chained_end_at = next_start + timedelta(seconds=program_seconds)
        chained_plan = filler_plan.model_copy(
            update={"segments": [*trimmed, *program_plan.segments]}
        )
        _LOG.info(
            "Rollover filler for %s trimmed to the %.1fs gap and chained with the programme "
            "due at %s (%d filler + %d programme segment(s), %.1fs of programme).",
            channel_id,
            gap_seconds,
            next_start.isoformat(),
            len(trimmed),
            len(program_plan.segments),
            program_seconds,
        )
        return chained_plan, chained_end_at

    def _avoid_schedule_tail_plan(
        self, channel_id: str, source_plan: EgressSourcePlan
    ) -> EgressSourcePlan:
        """The U26 no-horizon call site's view of ``_resolve_schedule_tail``:
        the replacement plan, or the tail plan untouched. Kept as a named
        method because that contract -- a plan is ALWAYS returned, never
        ``None`` -- is what the U26 tests pin."""

        config = self._store.get_config(channel_id)
        if config is None:
            return source_plan
        return self._resolve_schedule_tail(channel_id, source_plan, config).plan

    def _reload_steps(
        self,
        channel_id: str,
        state: EgressStateRow,
        process: object,
        *,
        rollover_plan_end_at: datetime | None,
        rollover_plan_min_seconds: float | None = None,
        force_fallback: bool = False,
    ) -> _PreparationSteps:
        """Seamless program content-reload for a content-reload-capable strategy.

        Resolves the newly-due plan (same provider → preparer chain as ``_start``) and
        tells the running worker to rebuild its program leg in place. A scheduled
        rollover with no next plan uses the configured fallback slate when there is
        no operator override. Returns False — so the caller falls back to
        terminate+restart — when the seamless path still can't own the request: a
        missing or disabled config, no valid plan, a prepare failure, a strategy error,
        or a worker control channel that isn't ready yet.

        F1 redesign (coordinator hostile review, 2026-09-06): ``reload_content``
        returning True means the worker ACCEPTED the request for arm/preroll,
        NOT that it has committed -- committing (or
        aborting) can take up to the worker's own ``defer_switch_timeout_s`` (900s
        default) for a deferred/boundary-aligned switch (the automation-driven
        ON_AIR-extension case this method exists for -- see
        ``reload_policy.should_defer_switch``). Doing the ON_AIR proof-event/state
        bookkeeping immediately, as the pre-redesign code did, would therefore
        claim the channel is airing the NEW source before the switch has actually
        happened -- for a deferred switch, the OLD content is often still
        genuinely on air for minutes after this method returns.

        This method now only ARMS the reload and records a
        ``_PendingReloadSettlement`` in ``self._pending_reload_settle``; the
        state/proof-event bookkeeping is deferred to ``_commit_reload_settlement``,
        invoked by ``_poll_reload_settlement`` once ``reload-status.json`` actually
        reports ``"applied"`` (or the daemon falls back to restart on
        ``"aborted:<reason>"``/a deadline lapse). Returns True as soon as the
        reload is armed -- this is what tells the CALLER (``_request_reload``) not
        to fall back to terminate+restart; it does not mean the switch landed.

        Item 78 fix 3 (coordinator review, round 3): ``rollover_plan_end_at``
        is a required keyword-only parameter, not read from
        ``self._rollover_plan_end_at`` inside this method -- the CALLER
        (``_request_reload``) pops the dict and passes the value down
        explicitly. Round 2 popped it at the top of THIS method, which fixed
        the leak for every early return inside this method's own body, but
        ``_request_reload`` itself has three earlier exits that never call
        this method at all (the worker is missing/dead -> ``_start``; no
        state row; the strategy doesn't declare ``supports_content_reload``)
        -- each of those left the recorded value sitting in
        ``self._rollover_plan_end_at`` forever, to be read by whatever
        reload attempt for this channel came next. Popping in the one
        caller that is reached on every single "reload" command, before any
        of ITS branches run, is what actually closes all of them.

        U41: ``rollover_plan_min_seconds`` is the horizon the dispatcher
        measured for this rollover, threaded down from the same record as
        ``rollover_plan_end_at`` and applied to the boundary plan of a DEFERRED
        switch only (see the eligibility gate where the provider is called).
        It is the difference between a plan that has a full lead of life when
        it takes air and one that has already spent its lead waiting -- the
        live 2026-09-25 education incident, 689 s planned, 83 s left at the
        switch, EOS ~3 minutes later."""
        config = self._store.get_config(channel_id)
        if config is None or not config.enabled:
            return False
        if self._ts_relay is not None:
            # #151: keep the reload request's sink URIs consistent with the
            # relay-routed URIs the running encoder was started with.
            config = self._ts_relay.apply(config)
        if self._hls_relay is not None:
            config = self._hls_relay.apply(config, log_root=self._work_dir)
        source_plan = None
        target_state: EgressState = "ON_AIR"
        # Scheduled rollover prepares the item due at the outgoing boundary.
        # Start/recovery and operator overrides continue to resolve wall-clock now.
        boundary_provider = self._boundary_source_plan_provider
        boundary_at = (
            max(rollover_plan_end_at, datetime.now(UTC))
            if rollover_plan_end_at is not None
            else None
        )
        # U41: the recorded horizon (``min_plan_seconds``) is applied ONLY to a
        # switch that will defer, because only a deferred switch consumes its
        # runway while it waits for the outgoing item's own end -- an immediate
        # cut airs the plan the moment it is armed, so a horizon measured from
        # the switch and one measured from dispatch are the same thing there.
        #
        # The decision is taken here in its ELIGIBILITY form (``now=None``: "is
        # this reload one that defers at all"), not with the late ``now`` the
        # value handed to the encoder below uses. That late ``now`` is item 78
        # fix 3's stale-horizon cut and must stay late; making it early here
        # would reintroduce exactly the defect it fixes. The two can disagree in
        # the rare case where the boundary passes between this point and the
        # request build; the horizon then applies to a reload that ends up
        # cutting immediately, which costs a wider plan and nothing else.
        #
        # A force-fallback filler rollover is excluded: that branch is the slate
        # fill by design (see the deliberate filler semantics below), and
        # widening its late-plan lookup would change what that filler is allowed
        # to select.
        horizon_seconds: float | None = None
        if (
            not force_fallback
            and rollover_plan_min_seconds is not None
            and should_defer_switch(
                previous_state=state.state,
                manual_override_active=self.has_manual_override(channel_id),
                plan_end_at=rollover_plan_end_at,
                now=None,
            )
        ):
            horizon_seconds = rollover_plan_min_seconds
        horizon_kwargs: dict[str, float] = (
            {"min_plan_seconds": horizon_seconds} if horizon_seconds is not None else {}
        )
        if not force_fallback:
            try:
                if (
                    boundary_provider is not None
                    and boundary_at is not None
                    and not self.has_manual_override(channel_id)
                ):
                    source_plan = boundary_provider(channel_id, boundary_at, **horizon_kwargs)
                else:
                    source_plan = self._source_plan_provider(channel_id)
            except SourcePrepareError:
                return False  # let terminate+restart resolve the slate fallback
        else:
            # The automation decision is bound to this command so the terminal
            # item cannot be re-selected forever at EOS. Still honor a schedule
            # published while the durable command was waiting to drain when it
            # now reaches beyond the boundary this rollover protects.
            provider_sampled_at = datetime.now(UTC)
            try:
                if boundary_provider is not None and boundary_at is not None:
                    provider_sampled_at = boundary_at
                    late_plan = boundary_provider(channel_id, boundary_at)
                else:
                    late_plan = self._source_plan_provider(channel_id)
            except SourcePrepareError:
                late_plan = None
            late_plan_seconds = (
                sum(segment.duration_seconds for segment in late_plan.segments)
                if late_plan is not None and late_plan.channel_id == channel_id
                else 0.0
            )
            if (
                late_plan is not None
                and rollover_plan_end_at is not None
                and provider_sampled_at
                + timedelta(seconds=late_plan_seconds - _ROLLOVER_EXTENSION_TOLERANCE_S)
                > rollover_plan_end_at
            ):
                source_plan = late_plan
            elif self._fallback_source_provider is not None:
                try:
                    source_plan = self._fallback_source_provider(config)
                except SourcePrepareError as exc:
                    _LOG.warning(
                        "Could not prepare rollover filler for %s; keeping the current "
                        "output on air for the next automation retry: %s",
                        channel_id,
                        exc,
                    )
                    return True
                target_state = "FALLBACK_SLATE"
        # U67 (live, education, 2026-09-30 03:44:10 -- 34 hours of black and
        # silent air). A ``None`` from the boundary provider on the seamless arm
        # is the SAME degenerate answer the automation's own U63 defect-A rule
        # exists to repair (``_resolve_rollover_tail``): the schedule item test
        # is the half-open ``starts_at <= t < ends_at``, so the boundary this
        # reload was dispatched against -- the closing item's own DERIVED end,
        # never a measured instant -- can land exactly ON that item's slot end
        # and resolve to no plan at all. Automation re-asks one margin past it,
        # finds the next item genuinely due, and logs "resolved to a 0.0s tail
        # ... using the plan due where that tail ends instead (1800.0s)"; this
        # method asked the same instant, got the same ``None``, and declined --
        # silently. Where the two processes look at the same boundary they must
        # give the same answer, so take the same second look before treating the
        # rollover as unplannable.
        #
        # A RECOVERY, not a new selection rule: it can only turn a decline into
        # the plan the schedule already has due immediately after the boundary
        # -- which is the plan the automation recorded the horizon for. The tail
        # floor and the horizon downstream are unchanged.
        if (
            source_plan is None
            and not force_fallback
            and boundary_provider is not None
            and boundary_at is not None
            and not self.has_manual_override(channel_id)
        ):
            try:
                source_plan = boundary_provider(
                    channel_id,
                    boundary_at + timedelta(seconds=_SCHEDULE_TAIL_BOUNDARY_MARGIN_S),
                    **horizon_kwargs,
                )
            except SourcePrepareError:
                source_plan = None
            if source_plan is not None and source_plan.channel_id == channel_id:
                _LOG.warning(
                    "Content-reload for %s: the boundary at %s resolves to no plan, but the "
                    "instant one margin past it (%s) resolves to %s; using that plan rather "
                    "than declining into a restart.",
                    channel_id,
                    boundary_at.isoformat(),
                    (boundary_at + timedelta(seconds=_SCHEDULE_TAIL_BOUNDARY_MARGIN_S)).isoformat(),
                    source_plan.segments[0].label if source_plan.segments else "an empty plan",
                )
        # A scheduled rollover can legitimately reach the end of the schedule:
        # both the boundary and the one-second recovery lookup are empty. Keep the
        # persistent worker on a known source by arming its configured slate in
        # place. Manual overrides own their own resolver, and force-fallback
        # rollovers already select the slate above, so neither path comes through
        # here.
        if (
            source_plan is None
            and rollover_plan_end_at is not None
            and not force_fallback
            and not self.has_manual_override(channel_id)
            and self._fallback_source_provider is not None
        ):
            try:
                source_plan = self._fallback_source_provider(config)
            except SourcePrepareError as exc:
                _LOG.warning(
                    "Could not prepare rollover fallback slate for %s; keeping the current "
                    "output on air for the next automation retry: %s",
                    channel_id,
                    exc,
                )
                return True
            if source_plan is not None and source_plan.channel_id == channel_id:
                target_state = "FALLBACK_SLATE"
        # U53 item 1: this is a filler rollover -- if the schedule has a programme
        # due when the filler's gap closes, trim the filler to that gap and chain
        # the programme into the same plan, so the programme is prepared now (with
        # this reload's own lead) and airs at its scheduled instant instead of
        # after however long the filler ran. Computed BEFORE the preparation
        # request below, because the chained shape is what has to be prepared.
        # A no-op (identical plan, None) whenever the chain cannot be built; see
        # ``_chain_next_program``.
        #
        # Scoped to the force-fallback filler arm on purpose. The OTHER place
        # this method can arm a slate -- ``_resolve_schedule_tail``'s
        # ``slate_when_no_next`` outcome below -- already resolves the plan due
        # where the tail ends, and widening the chain to cover it would change
        # what that resolver is allowed to choose without any evidence for it;
        # that is a separate decision, not this one.
        chained_program_end_at: datetime | None = None
        if force_fallback and target_state == "FALLBACK_SLATE" and source_plan is not None:
            # (``source_plan is not None`` is a narrowing guard, not a behavior
            # change: a None plan still falls through to the ``return False``
            # immediately below, exactly as it did before this chain existed.)
            source_plan, chained_program_end_at = self._chain_next_program(
                channel_id,
                source_plan,
                boundary_at=rollover_plan_end_at,
            )
        if source_plan is None or source_plan.channel_id != channel_id:
            # U67: this used to be a bare ``return False``, and that silence is
            # what made the 2026-09-30 education incident take 34 hours to
            # explain: the reload was declined right here, the caller fell back
            # to terminate+restart, and under the U41 plan-EOS hold the worker
            # never exited, so the channel pinned TRANSITIONING with an empty
            # ``last_error`` and no line anywhere naming the cause.
            _LOG.warning(
                "Content-reload for %s resolved no plan (boundary=%s, force_fallback=%s, "
                "resolved_from=%s); declining into the terminate+restart fallback.",
                channel_id,
                boundary_at.isoformat() if boundary_at is not None else "-",
                force_fallback,
                "boundary" if boundary_provider is not None and boundary_at is not None else "now",
            )
            return False
        if not force_fallback and target_state != "FALLBACK_SLATE":
            # U26 defect 2 applied where U26 deliberately skipped it.
            #
            # U26 reasoned that a horizon-bound rollover resolves at the
            # OUTGOING plan's end, so the boundary IS the tail's own end and the
            # plan due there is the one the rollover already chose. U36 measured
            # that false on the live station: the recorded horizon comes from the
            # SCHEDULE clock while the outgoing leg started at the ON_AIR
            # instant, so the two disagree by seconds. On 2026-09-25 the 14:06:50
            # rollover resolved the July 23 program 664s into a 667s item, armed
            # its single segment as "the last ~3s", and the new leg delivered
            # 1.6s (`mux-in 1.6s: video=+24 audio=+37`) before EOS. On this
            # engine ``max_segments=1`` makes that leg the WHOLE pipeline, so the
            # EOS took the worker down. Decision 2(i) of the coordinator's
            # answer: the floor applies here too, measured from the instant the
            # plan was actually resolved for (`resolved_at=boundary_at`), and if
            # nothing is scheduled where the tail ends the slate is armed at the
            # boundary instead of airing the sliver.
            #
            # A selected fallback slate is already the intended fill, so both
            # force-fallback and terminal-schedule fallback skip this floor.
            tail_outcome = self._resolve_schedule_tail(
                channel_id,
                source_plan,
                config,
                resolved_at=boundary_at,
                slate_when_no_next=True,
            )
            source_plan = tail_outcome.plan
            if tail_outcome.slate:
                target_state = "FALLBACK_SLATE"
        # F3: None unless the preparer actually reports a discrete per-plan
        # directory (see SourcePreparationReport.plan_dir) -- tracked into the
        # pending settlement below so _commit_reload_settlement can release the
        # PREVIOUS plan once this one lands.
        prepared_plan_dir: Path | None = None
        preparation_report: SourcePreparationReport | None = None
        if target_state == "FALLBACK_SLATE" and _is_ready_generated_slate(source_plan):
            # As on startup, the generated plain slate is ready to air. Repeating
            # its hour-long conform here leaves an ended short programme holding
            # the engine's black safety leg until preparation finally completes.
            # Mixed/trimmed plans, custom slates and bulletin audio still prepare.
            self._last_loudness_lufs.pop(channel_id, None)
        elif self._source_preparer is not None:
            # Retain per-channel elapsed preparation evidence while the
            # production automation loop runs this work in the background.
            prepare_started = time.monotonic()
            try:
                preparation_report = yield _PreparationRequest(source_plan, config)
                source_plan = preparation_report.source_plan
                prepared_plan_dir = preparation_report.plan_dir
                self._record_prepared_loudness(channel_id, preparation_report)
            except SourcePrepareError:
                _LOG.warning(
                    "Content-reload source preparation FAILED for %s after %.1fs; "
                    "falling back to restart.",
                    channel_id,
                    time.monotonic() - prepare_started,
                )
                return False
            _LOG.info(
                "Content-reload source preparation for %s took %.1fs for %d segment(s) "
                "(media preparation completed).",
                channel_id,
                time.monotonic() - prepare_started,
                len(source_plan.segments),
            )
        request = EncoderStartRequest(
            channel_id=channel_id,
            source_plan=source_plan,
            config=config,
            work_dir=self._work_dir,
            resolve_secret=self._resolve_secret,
            branding_plan=(
                self._branding_plan_provider(channel_id)
                if self._branding_plan_provider is not None
                else None
            ),
            caption_plan=(
                self._caption_plan_provider(channel_id)
                if self._caption_plan_provider is not None
                and channel_id not in self._caption_reset_failed
                else None
            ),
            cg_overlay_image=(
                self._cg_overlay_provider(channel_id, config)
                if self._cg_overlay_provider is not None
                else None
            ),
            board_live_video_layers=self._board_live_video_layers(channel_id, config),
            captions_allowed=channel_id not in self._caption_reset_failed,
            audio_tap_plan=(
                build_audio_tap_plan(channel_id)
                if channel_id not in self._caption_reset_failed
                else None
            ),
            ffmpeg_starter=self._ffmpeg_starter,
            # Only a horizon-bound automation rollover of an ON_AIR or finite
            # FALLBACK_SLATE plan may defer to the outgoing leg's EOS. A no-horizon
            # slate gap-replan and every operator override still cut immediately.
            #
            # Item 78 fix 3: ``rollover_plan_end_at`` is this method's
            # keyword-only parameter, sourced by ``_request_reload`` popping
            # ``self._rollover_plan_end_at`` unconditionally at the top of
            # ITS body -- before any of its own branches run. Round 5
            # (coordinator review) found that pop alone does not scope the
            # value to the reload automation recorded it for -- it bound to
            # whichever "reload" command for this channel ``_request_reload``
            # drained NEXT, which could be a different, unrelated reload
            # (e.g. an operator-issued one queued ahead of automation's own,
            # or a plain reload issued after a stale entry survived the
            # channel going off-air and being restarted).
            #
            # Round 7 (coordinator review) closed that: ``_rollover_plan_
            # end_at`` now stores ``(command_id, plan_end_at)``, and
            # ``_request_reload`` only forwards ``plan_end_at`` down to this
            # method (as ``rollover_plan_end_at=``) when the command
            # actually draining matches the recorded ``command_id``. An explicit
            # ``None`` matches only a direct, likewise-unscoped reload; it is not
            # a wildcard. A value that reaches this parameter therefore belongs
            # to the reload command now being processed -- never handed down
            # from a mismatched command. See ``_rollover_plan_end_at`` and
            # ``_request_reload``'s docstrings for the exact matching rule.
            switch_at_end_of_current=should_defer_switch(
                previous_state=state.state if state else None,
                manual_override_active=self.has_manual_override(channel_id),
                plan_end_at=rollover_plan_end_at,
                now=datetime.now(UTC),
            ),
        )
        # F3(b), owner-authorized 2026-09-24: an IMMEDIATE (non-deferred) reload
        # out of FALLBACK_SLATE must not attempt the in-place selector swap.
        # That combination tears down the live slate leg under the selector and
        # wedged one commit for 270 s on 2026-09-24 (U13). The immediate cut
        # itself is correct policy (U13 Q3) and is unchanged -- only the
        # MECHANISM changes, to the terminate+restart the daemon already owns,
        # carrying the report this reload just produced so that restart does not
        # conform the same plan again. Scope is exactly this combination: a
        # deferred rollover (the common case) and every ON_AIR/override reload
        # keep the seamless path byte-for-byte.
        if (
            not request.switch_at_end_of_current
            and state is not None
            and state.state == "FALLBACK_SLATE"
        ):
            if preparation_report is None:
                # No preparer configured, so nothing was prepared to reuse: the
                # ordinary restart path re-resolves the plan, exactly as a
                # declined seamless arm does today.
                return False
            return _ReusePreparedPlan(preparation_report, target_state)
        # F1 redesign: a daemon-generated id (not the strategy's own internal
        # uuid, which this layer never sees) so _poll_reload_settlement can
        # correlate reload-status.json's "id" field back to THIS specific
        # attempt -- load-bearing across a supersede (a second reload armed
        # before the first settles gets its OWN id and its OWN pending entry;
        # see that method's docstring).
        reload_id = str(uuid.uuid4())
        # F5 note (coordinator hostile review; NOT fixed here, deliberately):
        # this call still runs synchronously on the automation thread, same as
        # the source-plan lookup and strategy dispatch below. Media preparation
        # has already completed off-thread. With the F2 change the
        # worker's ack is "accepted" -- fast, bounded by _reload_ack_timeout_s()
        # (the plain 5s default) -- so the WORST case this blocks the
        # automation thread for is that ~5s pipe round trip, not the up-to-
        # 900s settlement wait the pre-redesign code risked. That is still a
        # synchronous call on a thread automation's whole poll loop shares
        # across every channel; moving send_and_wait off the automation
        # thread entirely (e.g. a dedicated dispatch thread/executor per
        # channel) would remove even that bound but is a separate change --
        # left as a note, not attempted in this PR.
        try:
            armed = self._encoder_strategy.reload_content(
                channel_id, self._work_dir, request, command_id=reload_id
            )
        except Exception:
            _LOG.exception("Content-reload dispatch failed for %s; restarting encoder.", channel_id)
            return False
        if not armed:
            # Diagnosability fix (coordinator follow-up, 2026-09-06): this used to
            # return False with NO log line at all -- an operator/on-call reading
            # the control-plane log for "why did this channel restart instead of
            # reloading in place" found nothing. ``last_send_command_failure_reason``
            # is an OPTIONAL strategy capability (only GstPlayoutStrategy's D2
            # worker-pipe path tracks a reason; the ffmpeg concat strategy has none
            # to report), resolved via ``getattr`` -- mirroring the
            # ``supports_content_reload``/``send_command`` probes elsewhere in
            # this file.
            reason_fn = getattr(self._encoder_strategy, "last_send_command_failure_reason", None)
            reason = reason_fn(channel_id) if callable(reason_fn) else None
            _LOG.warning(
                "Seamless content-reload declined for %s (%s); falling back to restart.",
                channel_id,
                reason or "no reason reported by the encoder strategy",
            )
            # Item 85 daemon-side fix: an "ack timeout" here is the daemon-visible
            # symptom of the engine-side reload-commit wedge this item's engine.py
            # fix addresses (see GstPlayoutEngine._commit_reload / _dispose_source_
            # leg) -- the worker's GLib main-loop thread is blocked forever inside
            # a synchronous GStreamer call and will NEVER again answer a pipe
            # command (every command is marshalled onto that same loop via
            # GLib.idle_add; see worker.py's _windows_pipe_reader_loop). The
            # process itself is confirmed alive here (``_request_reload`` already
            # early-returned to ``_start`` above if it were not), so simply
            # falling back to restart -- which, for anything but FALLBACK_SLATE,
            # does not terminate the process at all -- leaves the wedged pid
            # running forever: ``_poll_process`` only ever turns a
            # ``_pending_reloads`` entry into a real restart once the worker's OWN
            # exit code is observed, so nothing but the process itself exiting
            # would ever clear it, and it never will on its own. Measured: the
            # daemon rewrote TRANSITIONING every ~2s for minutes against exactly
            # this condition (sandbox soaks 12/14/15). Terminate it now, bounded
            # (terminate -> wait -> kill), reusing the SAME deliberate-kill
            # bookkeeping (``_reload_kills``) the FALLBACK_SLATE branch below
            # already relies on, so ``_poll_process``'s existing worker-exit
            # handling picks this up as a clean deliberate kill (not a crash) and
            # actually restarts the channel next tick instead of pinning
            # TRANSITIONING against a pid that will never exit on its own.
            if (
                isinstance(reason, str)
                and "ack timeout" in reason
                and _process_poll(process) is None
            ):
                _LOG.warning(
                    "Worker for %s appears wedged (reload ack timed out on a live "
                    "pid, reason=%r); terminating it so the channel can restart.",
                    channel_id,
                    reason,
                )
                # Hostile-review follow-up: set the ``_pending_reloads`` fallback
                # entry BEFORE terminating, not after. ``_process_terminate_
                # bounded`` blocks (bounded) waiting for the process to actually
                # exit; if something else observed that exit (``_poll_process``)
                # before this entry existed, the exit would be misread as an
                # ordinary crash (no pending-reload entry to restore FROM) rather
                # than the pending-reload restart it actually is. Mirrors exactly
                # what ``_fall_back_to_restart_reload`` (called next, by
                # ``_request_reload``, once this method returns False) sets --
                # setting it here first closes the window; that later call
                # harmlessly re-sets the same value. U67: both arms now go
                # through ``_arm_pending_reload`` so the reload-stall watchdog
                # gets its clock and bound at the same instant.
                self._arm_pending_reload(channel_id, state, plan_end_at=rollover_plan_end_at)
                self._note_deliberate_kill(
                    channel_id,
                    "reload ack timed out on a live pid: the worker is wedged on a "
                    "synchronous GStreamer call and will never answer another command",
                )
                _process_terminate_bounded(process)
            return False
        # Item 3 fix: a still-pending PREVIOUS reload for this channel (this
        # attempt supersedes it -- e.g. automation issued another rollover
        # before the first settled) used to be silently overwritten below,
        # leaking its plan_dir forever. Discard it properly first.
        if channel_id in self._pending_reload_settle:
            self._discard_pending_reload_settlement(
                channel_id, reason="superseded by a newer reload attempt"
            )
        # U67: the seamless path has just TAKEN OVER this channel's recovery, so a
        # ``_pending_reloads`` pin left by an earlier declined attempt is obsolete
        # -- drop it. The pin means "no seamless hand-off happened; wait for the
        # worker to reach plan EOS and exit, then restart onto the prepared plan",
        # and this reload is now settling under ``_pending_reload_settle``
        # instead; its own abort/failure re-arms a pin through
        # ``_fall_back_to_restart_reload``. Left in place it was NOT harmless:
        # under the U41 plan-EOS hold the worker no longer exits at a boundary, so
        # nothing would ever pop it, and ``_poll_process`` would keep rewriting
        # TRANSITIONING over the channel for the whole settlement and then
        # permanently -- including after this reload lands its new program on air.
        # Measured on the candidate build: the reload-stall watchdog's own re-issue
        # succeeds exactly this way, and a pin that outlived its own recovery would
        # make the watchdog re-fire against a channel that is already on air.
        self._pending_reloads.pop(channel_id, None)
        # Armed, not yet settled: record it and return. _poll_reload_settlement
        # finishes the target-state bookkeeping once reload-status.json confirms
        # "applied" (or falls back to restart on "aborted:<reason>"/deadline).
        self._pending_reload_settle[channel_id] = _PendingReloadSettlement(
            reload_id=reload_id,
            since=self._monotonic(),
            process=process,
            config=config,
            source_plan=source_plan,
            switch_at_end_of_current=bool(request.switch_at_end_of_current),
            previous_state=state.state if state else None,
            previous_source_label=state.current_source_label if state else None,
            target_state=target_state,
            plan_dir=prepared_plan_dir,
            rollover_plan_end_at=rollover_plan_end_at,
            chained_program_end_at=chained_program_end_at,
        )
        _LOG.info(
            "Seamless content-reload accepted for %s (reload_id=%s, switch_at_end_of_current=%s); "
            "awaiting settlement.",
            channel_id,
            reload_id,
            request.switch_at_end_of_current,
        )
        return True

    def _commit_reload_settlement(self, channel_id: str, pending: _PendingReloadSettlement) -> None:
        """F1 redesign: the target proof-event/state bookkeeping ``_try_content_
        reload`` used to do immediately -- now run only once
        ``_poll_reload_settlement`` observes ``reload-status.json`` reporting
        ``"applied"`` for ``pending.reload_id``. Also releases the PREVIOUS
        active prepared-plan directory (F3) and records the new one."""
        config = pending.config
        source_plan = pending.source_plan
        previous_state = pending.previous_state
        previous_source_label = pending.previous_source_label
        # Record the source-to-source transition in the proof chain for parity with
        # the restart path — but DO NOT write a TRANSITIONING *state*: the seamless
        # swap never takes output down, so the channel stays in a running state.
        if previous_state in {"ON_AIR", "FALLBACK_SLATE"}:
            transition_event = self._build_proof_event(
                channel_id=channel_id,
                state="TRANSITIONING",
                source_plan=source_plan,
                previous_state=previous_state,
                previous_source_label=previous_source_label,
            )
            self._store.append_proof_event(transition_event)
        proof_event = self._build_proof_event(
            channel_id=channel_id,
            state=pending.target_state,
            source_plan=source_plan,
            previous_state=previous_state,
            previous_source_label=previous_source_label,
        )
        self._store.append_proof_event(proof_event)
        # The seamless twin of _start: output keeps running but the source
        # changed, so this is an as-run boundary for program or filler.
        self._record_as_run_transition(
            channel_id=channel_id,
            running_state=pending.target_state,
            source_plan=source_plan,
            proof_event=proof_event,
        )
        self._write_state(
            channel_id,
            pending.target_state,
            current_source_label=source_plan.segments[0].label,
            current_proof_event_id=proof_event.event_id,
            pid=_process_pid(pending.process),
        )
        self._record_dispatched_plan(
            channel_id,
            proof_event_id=proof_event.event_id,
            source_plan=source_plan,
            switch_deferred=pending.switch_at_end_of_current,
            chained_program_end_at=pending.chained_program_end_at,
        )
        self._append_health(
            channel_id,
            pending.target_state,
            sink_connected=self._sink_connected(channel_id, config, state=pending.target_state),
            seconds_on_air=self._seconds_on_air(channel_id),
        )
        # F3: the reload just settled -- the PREVIOUS plan is retired (the
        # engine has disposed its leg; see engine.reload_program's on_settled
        # contract). Release it immediately rather than waiting for GC, then
        # start tracking the new plan as active.
        self._release_prepared_plan_dir(self._active_prepared_plan_dir.pop(channel_id, None))
        if pending.plan_dir is not None:
            self._active_prepared_plan_dir[channel_id] = pending.plan_dir

    def _retry_aborted_boundary_reload(
        self, channel_id: str, pending: _PendingReloadSettlement, *, reason: str
    ) -> bool:
        """U36 item 7: re-resolve and re-arm a boundary reload the worker ABORTED
        pre-commit, instead of discarding it and leaving the boundary unprepared.

        Returns True when this method has already decided what happens next --
        either a retry is armed, or (retries exhausted) the fallback slate is
        armed AT the boundary. The caller must then do nothing further; the
        terminate+restart fallback it would otherwise run never happens, which is
        the point: the outgoing leg keeps airing while the retry is armed.
        False means "not an aborted BOUNDARY reload" -- carry on with the
        pre-U36 behavior (log, discard, restart fallback).

        Only a rollover-armed reload gets this treatment: the retry re-resolves
        through ``_request_reload``/``_reload_steps`` with the SAME horizon, so
        it goes through the U36 tail floor again (decision 2(i)). A reload with
        no horizon has no boundary to protect and no measured failure mode, so
        it keeps the plain restart fallback.

        Bounded by ``_RELOAD_RETRY_LIMIT``; the budget is keyed by horizon, so a
        NEW boundary starts at zero without anything having to clear the entry
        (see ``_reload_retries``). Once the slate arm itself has been tried and
        aborted too (``used > _RELOAD_RETRY_LIMIT``), this returns False and the
        channel takes the ordinary restart fallback rather than re-arming the
        slate forever.

        Evidence: on 2026-09-25 the public channel's 14:44 boundary reload was
        ``aborted:error``, discarded with no retry (``_fall_back_to_restart_
        reload`` does not terminate an ON_AIR worker, so nothing even restarted),
        and the next boundary then arrived with nothing prepared -- 10s aggregate
        stall watchdog, exit 1, ~80s of dead air.
        """
        horizon = pending.rollover_plan_end_at
        if horizon is None:
            return False
        recorded = self._reload_retries.get(channel_id)
        used = recorded[1] if recorded is not None and recorded[0] == horizon else 0
        if used > _RELOAD_RETRY_LIMIT:
            # The slate arm (below) has already been tried for THIS horizon and
            # the worker aborted that too. Stop re-arming; let the caller take
            # the restart fallback.
            return False
        # Discard BEFORE re-arming: _try_content_reload treats a still-pending
        # previous attempt as superseded, and this attempt is not a supersession
        # -- it is the successor to a failed one. This also releases the aborted
        # attempt's plan_dir now rather than at the 960s deadline.
        self._discard_pending_reload_settlement(channel_id, reason=f"worker reported {reason}")
        self._reload_retries[channel_id] = (horizon, used + 1)
        if used >= _RELOAD_RETRY_LIMIT:
            _LOG.warning(
                "Boundary reload for %s did not land (%s) and %d retries were spent; "
                "arming the fallback slate at the boundary so the outgoing leg is not "
                "left to run out unprepared.",
                channel_id,
                reason,
                _RELOAD_RETRY_LIMIT,
            )
            # ``force_fallback`` skips the resolver entirely and takes the slate
            # (``_fallback_source_provider``) -- the same arm automation uses for
            # a filler rollover -- carrying the horizon so the switch still lands
            # AT the boundary rather than immediately.
            self.record_rollover_plan_end(channel_id, horizon, command_id=None, force_fallback=True)
        else:
            _LOG.warning(
                "Boundary reload for %s did not land (%s); re-resolving and re-arming "
                "(retry %d of %d) while the outgoing leg keeps airing.",
                channel_id,
                reason,
                used + 1,
                _RELOAD_RETRY_LIMIT,
            )
            self.record_rollover_plan_end(channel_id, horizon, command_id=None)
        # _request_reload routes itself: seamless reload when the strategy
        # accepts it, terminate+restart otherwise -- so a retry that cannot even
        # be armed still ends somewhere sensible rather than nowhere.
        self._request_reload(channel_id)
        return True

    def _poll_reload_settlement(self, channel_id: str) -> None:
        """F1 redesign: poll ``reload-status.json`` for a channel with an
        armed-but-not-yet-settled content-reload (``_try_content_reload``).
        Runs once per ``process_once`` tick (added to its poll tuple), replacing
        the pre-redesign design's synchronous, potentially-900s-long block on
        the worker-pipe ack.

        * status ``"id"`` matches the pending reload and ``"result" ==
          "applied"`` -- the switch actually landed: finish the target-state
          bookkeeping (``_commit_reload_settlement``) and clear the pending
          entry.
        * status matches and ``"result"`` starts with ``"aborted:"`` -- the
          reload did not land (build error, timeout, supersession downstream of
          this specific attempt). U36 item 7: if this was a BOUNDARY reload
          (``pending.rollover_plan_end_at``), it is re-resolved and re-armed,
          bounded, before the horizon (``_retry_aborted_boundary_reload``) --
          discarding it outright left the boundary unprepared, which is what
          cost the public channel ~80s of dead air on 2026-09-25. Otherwise, log
          the reason and fall back to restart (``_fall_back_to_restart_reload``),
          the same path a synchronously-declined reload always took.
        * no status yet, or an id that does not match (a superseded attempt's
          own settlement arriving late) -- keep waiting, UNLESS
          ``_PENDING_RELOAD_SETTLE_DEADLINE_S`` has elapsed since this reload
          was armed, in which case treat it as lost and fall back to restart
          rather than wait forever for a status update that may never come
          (e.g. the worker crashed between arming and writing the file).
        * a status matching the pending id but carrying an UNRECOGNIZED
          result value (hostile-review follow-up: neither ``"applied"`` nor
          ``"aborted:..."`` -- a malformed write, a future/older worker
          version) is treated as aborted IMMEDIATELY, not silently waited out
          for the full deadline: whatever wrote it clearly ran, so waiting
          longer buys nothing, and a channel that is really fine should not
          sit unresolved for up to 960s over a status the daemon simply
          cannot interpret.

        If ``channel_id`` has no pending entry at all (already discarded --
        see ``_discard_pending_reload_settlement``), this also checks whether
        a LATE status write matches the most recently discarded reload_id and
        logs that it is being ignored, rather than the previous silent no-op
        that left no evidence the late write was ever observed."""
        pending = self._pending_reload_settle.get(channel_id)
        if pending is None:
            discarded_id = self._discarded_reload_ids.get(channel_id)
            if discarded_id is not None:
                status = self._read_reload_status(channel_id)
                if status is not None and status.get("id") == discarded_id:
                    _LOG.info(
                        "Content-reload settlement for %s (reload_id=%s) arrived after "
                        "that attempt was already discarded (worker exit/supersede/"
                        "restart); ignoring.",
                        channel_id,
                        discarded_id,
                    )
                    self._discarded_reload_ids.pop(channel_id, None)
            return
        status = self._read_reload_status(channel_id)
        if status is not None and status.get("id") == pending.reload_id:
            result = status.get("result")
            if result == "applied":
                # Follow-up (second hostile-review pass): liveness re-check --
                # the worker that armed this reload may have exited BETWEEN
                # _poll_process observing it (which would have discarded this
                # entry already) and this read, or in a process_once pass
                # where _poll_process ran before this worker's exit actually
                # happened. Stamping ON_AIR against a pid that is already dead
                # would be a lie the state row then carries until the next
                # tick catches it -- checked directly, not inferred.
                if _process_poll(pending.process) is not None:
                    _LOG.warning(
                        'Seamless content-reload for %s reported "applied" but its '
                        "worker had already exited (reload_id=%s); falling back to restart "
                        "instead of stamping ON_AIR against a dead process.",
                        channel_id,
                        pending.reload_id,
                    )
                    self._discard_pending_reload_settlement(
                        channel_id, reason="worker exited before settlement could be committed"
                    )
                    self._fall_back_to_restart_reload(
                        channel_id, plan_end_at=pending.rollover_plan_end_at
                    )
                    return
                # NOTE: a plain pop here, not _discard_pending_reload_settlement --
                # this attempt is COMMITTING, not being discarded; its plan_dir
                # becomes the new active one inside _commit_reload_settlement,
                # never released.
                self._pending_reload_settle.pop(channel_id, None)
                self._commit_reload_settlement(channel_id, pending)
                return
            if isinstance(result, str) and result.startswith("aborted:"):
                # U36 item 7: a boundary reload gets a bounded retry (and, when
                # that is exhausted, the slate AT the boundary) before this
                # channel is handed the restart fallback.
                if self._retry_aborted_boundary_reload(channel_id, pending, reason=result):
                    return
                _LOG.warning(
                    "Seamless content-reload for %s did not land (%s); falling back to restart.",
                    channel_id,
                    result,
                )
                self._discard_pending_reload_settlement(
                    channel_id, reason=f"worker reported {result}"
                )
                self._fall_back_to_restart_reload(
                    channel_id,
                    failure_reason=f"Seamless content reload failed: {result}",
                    plan_end_at=pending.rollover_plan_end_at,
                )
                return
            # Unrecognized result value -- see the docstring note above.
            _LOG.warning(
                "Seamless content-reload for %s reported an unrecognized settlement "
                "result %r (reload_id=%s); treating as aborted and falling back to restart.",
                channel_id,
                result,
                pending.reload_id,
            )
            self._discard_pending_reload_settlement(
                channel_id, reason=f"unrecognized settlement result {result!r}"
            )
            self._fall_back_to_restart_reload(channel_id, plan_end_at=pending.rollover_plan_end_at)
            return
        if self._monotonic() - pending.since >= _PENDING_RELOAD_SETTLE_DEADLINE_S:
            _LOG.warning(
                "Seamless content-reload for %s reported no settlement within %.0fs "
                "(reload_id=%s); falling back to restart.",
                channel_id,
                _PENDING_RELOAD_SETTLE_DEADLINE_S,
                pending.reload_id,
            )
            self._discard_pending_reload_settlement(
                channel_id, reason=f"no settlement within {_PENDING_RELOAD_SETTLE_DEADLINE_S:.0f}s"
            )
            self._fall_back_to_restart_reload(channel_id, plan_end_at=pending.rollover_plan_end_at)

    def _read_reload_status(self, channel_id: str) -> dict[str, Any] | None:
        """Best-effort read of ``<work>/<channel_id>/reload-status.json``
        (``worker.py``'s ``_write_reload_status``). Never raises: a torn read
        (mid-rewrite, though the worker writes atomically via tmp+replace so
        this should be rare) or a malformed body is just "no status yet",
        retried next tick."""
        status_path = self._work_dir / channel_id / "reload-status.json"
        try:
            data = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _arm_pending_reload(
        self,
        channel_id: str,
        state: EgressStateRow | None,
        *,
        plan_end_at: datetime | None = None,
    ) -> None:
        """U67: arm the ``_pending_reloads`` pin AND start the reload-stall
        watchdog's clock for it.

        Every writer of ``_pending_reloads`` goes through here --
        ``_fall_back_to_restart_reload`` (a declined or lost reload) and
        ``_reload_steps``' ack-timeout wedge branch -- so the bound is always
        recorded at the instant the pin appears, from whatever rollover horizon
        the caller still has in hand (``plan_end_at``; ``None`` when the reload
        carried no recorded horizon). See ``_poll_reload_stall_watchdog``."""
        self._pending_reloads[channel_id] = (
            state.state if state else None,
            state.current_source_label if state else None,
        )
        self._reload_stall_since[channel_id] = self._monotonic()
        self._reload_stall_bound_s[channel_id] = _reload_stall_bound_seconds(plan_end_at)
        self._reload_stall_rungs.pop(channel_id, None)

    def _poll_reload_stall_watchdog(self, channel_id: str) -> None:
        """U67: recover a channel whose ``_pending_reloads`` pin has outlived
        any honest reason to be there.

        The pin IS the daemon's "a reload declined; wait for the worker to reach
        plan EOS and exit, then restart onto the prepared plan" state, and both
        of its arms (``_fall_back_to_restart_reload``, and ``_reload_steps``'
        ack-timeout wedge branch) hand the channel's recovery to a worker EXIT.
        That was a safe assumption until U41's plan-EOS hold shipped: under it
        the engine drops the plan-EOS so the worker never exits at a plan
        boundary, and a pin whose only exit route is gone is permanent.
        Measured 2026-09-30 on the education channel: TRANSITIONING rewritten
        every ~2s with an unchanged pid and an empty ``last_error`` from
        03:44:13 until the log rolled -- 34 hours of black and silent air, with
        no line anywhere naming the cause.

        So: bound the pin (``_TRANSITIONING_WATCHDOG_SECONDS``, "plan length +
        lead", stretched by the rollover's own recorded horizon where that is
        further out). On expiry, log an ERROR and recover with no human, in two
        rungs: re-issue the reload once (the one that declined may well arm on a
        second look -- e.g. once the schedule's next item is genuinely due), and
        if the pin survives that by one more re-issue grace, restart the
        channel's egress by terminating the worker exactly as the Item 85 wedge
        branch does, so ``_poll_process``'s crash-relaunch re-resolves and
        starts a fresh plan.

        A NORMAL rollover can never reach this: nothing on the healthy path ever
        writes ``_pending_reloads``, so the clock only ever starts on a channel
        that has already failed a seamless hand-off. While a preparation or an
        accepted-but-unsettled reload is genuinely in flight the watchdog stands
        down entirely -- those carry their own deadlines (the preparation
        timeout / ``_PENDING_RELOAD_SETTLE_DEADLINE_S``) and are not this one's
        business."""
        if channel_id not in self._pending_reloads:
            # The pin cleared (the worker exited and the restart took it, a
            # fresh start, a drain, a stop): this supervision is over.
            self._reload_stall_since.pop(channel_id, None)
            self._reload_stall_bound_s.pop(channel_id, None)
            self._reload_stall_rungs.pop(channel_id, None)
            return
        now = self._monotonic()
        if self.has_pending_reload_settlement(channel_id):
            # Real work is outstanding: a preparation is running, or a reload
            # this daemon accepted is still settling. The pin is being driven,
            # not stuck. Restart the clock for whatever comes after it.
            self._reload_stall_since[channel_id] = now
            self._reload_stall_rungs.pop(channel_id, None)
            return
        since = self._reload_stall_since.get(channel_id)
        if since is None:
            # First sighting of a pin this watchdog did not arm itself (armed
            # before this process started, or between two ticks): measure from
            # here rather than never.
            self._reload_stall_since[channel_id] = now
            self._reload_stall_bound_s.setdefault(channel_id, _TRANSITIONING_WATCHDOG_SECONDS)
            return
        rung = self._reload_stall_rungs.get(channel_id, 0)
        bound = (
            _TRANSITIONING_WATCHDOG_REISSUE_GRACE_SECONDS
            if rung > 0
            else self._reload_stall_bound_s.get(channel_id, _TRANSITIONING_WATCHDOG_SECONDS)
        )
        elapsed = now - since
        if elapsed < bound:
            return
        self._reload_stall_since[channel_id] = now
        state = self._store.read_state(channel_id)
        process = self._processes.get(channel_id)
        if rung == 0:
            self._reload_stall_rungs[channel_id] = 1
            _LOG.error(
                "Channel %s: reload stall -- pinned TRANSITIONING for %.0fs with nothing in "
                "flight (state=%s, source=%s, pid=%s, last_error=%s). The reload was declined "
                "and its only recovery is a worker exit that has not come. Re-issuing the "
                "rollover now; a channel still pinned %.0fs from here is restarted.",
                channel_id,
                elapsed,
                state.state if state else "UNKNOWN",
                state.current_source_label if state else "-",
                _process_pid(process),
                (state.last_error if state and state.last_error else "-"),
                _TRANSITIONING_WATCHDOG_REISSUE_GRACE_SECONDS,
            )
            self._request_reload(channel_id)
            # A re-issue that declines again comes back through
            # ``_arm_pending_reload``, which resets this pin's clock and clears its
            # rung -- correct for a NEW stall, wrong here: this is still the SAME
            # stall, and without restoring the rung the grace below would never
            # apply and the channel would simply re-issue forever at the full
            # bound instead of ever reaching the restart. (A re-issue that
            # SUCCEEDS leaves no pin at all -- ``_reload_steps`` drops it when the
            # reload arms -- so the first branch above ends the episode.)
            if channel_id in self._pending_reloads:
                self._reload_stall_since[channel_id] = now
                self._reload_stall_rungs[channel_id] = 1
            return
        self._reload_stall_rungs[channel_id] = rung + 1
        _LOG.error(
            "Channel %s: reload stall not cleared by the re-issue (pinned TRANSITIONING a "
            "further %.0fs, state=%s, source=%s, pid=%s, last_error=%s). Restarting the "
            "channel's egress: terminating the worker so the crash-relaunch re-resolves and "
            "starts a fresh plan.",
            channel_id,
            elapsed,
            state.state if state else "UNKNOWN",
            state.current_source_label if state else "-",
            _process_pid(process),
            (state.last_error if state and state.last_error else "-"),
        )
        # Drop the pin BEFORE terminating: the pin is precisely what tells
        # ``_poll_process``'s exit branch that the exit it is about to observe
        # was "honor the pending reload" rather than a crash to relaunch from,
        # and this watchdog is terminating a channel whose pending reload it has
        # just judged untrustworthy -- the crash-relaunch is the recovery.
        self._pending_reloads.pop(channel_id, None)
        if process is None or _process_poll(process) is not None:
            # No live worker left to restart; the pin was simply stale. Dropping
            # it is the whole fix -- the next tick publishes the real state.
            return
        self._note_deliberate_kill(
            channel_id,
            "reload stall watchdog: the channel stayed TRANSITIONING past its bound with "
            "nothing in flight, and the re-issue did not clear it",
        )
        _process_terminate_bounded(process)

    def _fall_back_to_restart_reload(
        self,
        channel_id: str,
        *,
        failure_reason: str | None = None,
        prepared_reload: _ReusePreparedPlan | None = None,
        plan_end_at: datetime | None = None,
    ) -> None:
        """The terminate+restart reload path a declined/aborted/lost content-
        reload always falls through to -- factored out of ``_request_reload``
        so ``_poll_reload_settlement`` can take the exact same path for a
        reload that armed successfully but then failed to settle.

        Hostile-review follow-up, item 1: defensively discards any pending
        reload-settlement tracking for this channel too (a no-op if the
        caller already did -- every current call site does). Kept here as a
        backstop so a future call site reaching this method can never leave a
        stale pending entry (and its leaked plan_dir) behind.

        ``prepared_reload`` (F3(b), default ``None``): the plan a
        FALLBACK_SLATE reload prepared before declining the in-place selector
        swap. Held for the restart ``_poll_process`` performs when this
        method's worker exit arrives, so that restart reuses it instead of
        conforming it again. Every other caller passes nothing and behaves
        exactly as before.

        ``plan_end_at`` (U67, default ``None``): the rollover horizon this
        reload was dispatched against, when the caller still has it. Recorded on
        the ``_pending_reloads`` pin so the reload-stall watchdog's bound can be
        derived from the plan that is actually left rather than only from its
        flat floor -- see ``_arm_pending_reload``."""
        self._discard_pending_reload_settlement(channel_id, reason="falling back to restart")
        if prepared_reload is not None:
            self._stash_prepared_restart_plan(channel_id, prepared_reload)
        state = self._store.read_state(channel_id)
        process = self._processes.get(channel_id)
        self._arm_pending_reload(channel_id, state, plan_end_at=plan_end_at)
        proof_event_id = state.current_proof_event_id if state else None
        if failure_reason is not None:
            source_label = (
                state.current_source_label
                if state and state.current_source_label
                else "Unknown egress source"
            )
            event = EgressProofEvent(
                event_id=f"egress-content-reload-failure-{uuid.uuid4()}",
                observed_at=datetime.now(UTC),
                channel_id=channel_id,
                state="TRANSITIONING",
                source_label=source_label,
                source_path="content-reload:settlement-aborted",
                source_ref=proof_event_id,
                proof_boundary="civiccast-egress-handoff-boundary",
                machine_summary=f"{failure_reason}; restart requested.",
            )
            self._store.append_proof_event(event)
            proof_event_id = event.event_id
        self._write_state(
            channel_id,
            "TRANSITIONING",
            current_source_label=state.current_source_label if state else None,
            current_proof_event_id=proof_event_id,
            last_error=failure_reason,
            pid=_process_pid(process),
        )
        expired_boundary = plan_end_at is not None and plan_end_at <= datetime.now(UTC)
        if (
            state is not None
            and (state.state == "FALLBACK_SLATE" or expired_boundary)
            and process is not None
        ):
            # Issue #157 (CA-8 live finding): filler is interruptible by
            # design - a due program must not wait out the fill-target plan
            # (after #154 that wait is up to an hour of slate). An explicit
            # boundary that has passed also cuts now, including End on an open
            # live feed. Other programs retain their graceful drain.
            self._note_deliberate_kill(
                channel_id,
                "reload at an expired handoff boundary"
                if expired_boundary
                else "reload out of fallback slate: filler is interruptible by design "
                "(issue #157), so the due program is not made to wait out the "
                "fill-target plan; the restart carries the prepared plan",
            )
            _process_terminate(process)

    def _request_reload(
        self,
        channel_id: str,
        *,
        command_id: str | None = None,
        slate_first: bool = False,
        handoff_at: datetime | None = None,
    ) -> None:
        """Bring the channel's next program on air.

        ``slate_first`` (U47, default ``False`` = every pre-existing caller and
        every queued ``reload`` command): this reload is the HAND-OFF half of a
        slate-first start -- the slate is already on air and the channel must
        stay on it if this reload cannot prepare the program. It changes exactly
        two things, both about staying up rather than about the reload itself:
        the no-live-process branch's ``_start`` inherits the flag (the worker
        died between the slate's launch and this call, so that start is once
        again "rescue a live channel"), and a declined/failed preparation keeps
        the slate instead of running the restart fallback. See the tail below
        and ``_hand_off_slate_first_program``."""
        self._cancel_preparation(channel_id)
        # Item 78 fix 3 (coordinator review, round 3): pop the automation-
        # recorded rollover plan_end_at HERE, at the very top of the ONE
        # method every "reload" command reaches, before any of this
        # method's own branches run. Round 2 popped it inside
        # ``_try_content_reload`` instead, which fixed every early return
        # INSIDE that method but missed the three exits below that never
        # call it at all: the worker is missing/dead (falls to ``_start``),
        # there is no state row, or the strategy doesn't declare
        # ``supports_content_reload`` (both fall straight to
        # ``_fall_back_to_restart_reload``) -- each measurably left the
        # value sitting in ``self._rollover_plan_end_at`` forever, to be
        # read by whatever reload attempt for this channel came next.
        # Passed down explicitly to ``_try_content_reload`` rather than
        # having it read the (now already-emptied) dict itself.
        #
        # Round 7 (coordinator review): the entry is ``(recorded_command_id,
        # plan_end_at)`` and is only USED when ``recorded_command_id``
        # matches ``command_id`` -- the id of the command actually draining
        # right now (threaded in from ``_process_command``, or ``None`` for
        # a direct call that bypasses the command queue, e.g.
        # ``supervisor.py``'s live-takeover/handback/slate routes). Any
        # mismatch means a DIFFERENT reload command (not the one automation
        # recorded this value for) is the one draining -- the value must not
        # be used, and ``should_defer_switch`` falls back to its ordinary
        # ON_AIR/no-override behavior.
        #
        # Round 8 (coordinator review): round 7 still popped this entry
        # UNCONDITIONALLY here, on every drain regardless of match, and only
        # gated USE on the id comparison -- MEASURED wrong. The common
        # trigger is ``ChannelAutomationService``'s own 45s issued-timeout
        # retry: dispatch A records command_id=A and enqueues reload A; the
        # daemon stalls past the retry timeout; the retry re-records
        # (overwriting the dict entry) command_id=B and enqueues reload B.
        # If A drains first, round 7's unconditional pop discarded B's entry
        # right there on A's mismatch -- so when B itself drained moments
        # later there was nothing left, and this fell back to deferring
        # against no recorded horizon at all even though B's own horizon
        # (the one just thrown away) had already passed: measured
        # [True, True] (both defer -- dead air on a past horizon), where the
        # correct shape is [True, False] (A: no match, value left in place,
        # ordinary defer; B: match, cut on the past horizon).
        #
        # Fixed by popping ONLY on an actual match -- a mismatch leaves the
        # entry untouched in the dict for whichever command it really was
        # recorded for to consume when THAT one drains. This stays safe
        # against every off-air/relaunch leak ``_rollover_plan_end_at``'s
        # docstring inventories: none of those routes reach
        # ``_request_reload`` at all -- each pops the dict itself,
        # unconditionally, at its own off-air transition -- so a mismatched
        # entry left here still cannot outlive the channel going off-air.
        recorded = self._rollover_plan_end_at.get(channel_id)
        rollover_plan_end_at: datetime | None = None
        force_fallback = False
        # U41: the horizon the dispatcher measured for this rollover, applied
        # to the deferred switch only (see ``_try_content_reload``).
        rollover_plan_min_seconds: float | None = None
        if recorded is not None:
            (
                recorded_command_id,
                recorded_plan_end_at,
                recorded_force_fallback,
                recorded_min_plan_seconds,
            ) = recorded
            if recorded_command_id == command_id:
                rollover_plan_end_at = recorded_plan_end_at
                force_fallback = recorded_force_fallback
                rollover_plan_min_seconds = recorded_min_plan_seconds
                self._rollover_plan_end_at.pop(channel_id, None)
        # An explicit live End supplies its own boundary, rather than waiting
        # for EOS from an open feed. The same horizon reaches asynchronous
        # preparation and restart fallback, so those paths cannot lose it.
        if handoff_at is not None:
            rollover_plan_end_at = handoff_at
        state = self._store.read_state(channel_id)
        process = self._processes.get(channel_id)
        if process is None or _process_poll(process) is not None:
            self._start(
                channel_id,
                previous_state=state.state if state else None,
                previous_source_label=state.current_source_label if state else None,
                slate_first=slate_first,
            )
            return
        # S15 (D-S1-6): if the strategy can rebuild program content in place (the
        # GStreamer engine), apply the newly-due program seamlessly — no encoder
        # restart, so MPEG-TS continuity is unbroken at the program boundary (the
        # #151 fix applied to every reload). Any condition the seamless path can't
        # handle falls through to the terminate+restart reload below (which already
        # handles slate fallback, interruptible filler, and the graceful drain).
        if state is not None and getattr(self._encoder_strategy, "supports_content_reload", False):
            if self._try_content_reload(
                channel_id,
                state,
                process,
                rollover_plan_end_at=rollover_plan_end_at,
                rollover_plan_min_seconds=rollover_plan_min_seconds,
                force_fallback=force_fallback,
                slate_first=slate_first,
            ):
                # Preparing/accepted work is pending; it is not on-air proof.
                # _poll_reload_settlement (in process_once's poll tuple) finishes
                # the job (or falls back to restart via
                # _fall_back_to_restart_reload) once reload-status.json actually
                # reports an outcome.
                return
            if slate_first:
                if channel_id in self._prepared_restart_plans:
                    # Already routed: the F3(b) branch above stashed a plan and
                    # queued this channel's terminate+restart, so the exit that
                    # lands in ``_poll_process`` will air the program. Logging
                    # "keeping the fallback slate on air" here would be false --
                    # the slate worker has already been terminated.
                    return
                # U47: this reload was the hand-off half of a slate-first start
                # and it could not produce a program -- either the reload steps
                # declined (an unusable plan, an arming refusal) or the
                # preparation raised. The ordinary fallback below is WRONG here:
                # out of a live FALLBACK_SLATE it adds this channel to
                # ``_reload_kills`` and terminates the slate worker (issue #157)
                # and then conforms the slate again, i.e. it would take a channel
                # that is ON AIR and make it dark -- precisely the outcome U47
                # exists to prevent. Keep the slate; the program's retry belongs
                # to the automation's own slate-replan policy, which is already
                # watching a FALLBACK_SLATE channel for a due program.
                #
                # This tail is the SYNCHRONOUS path only. The same failure on the
                # asynchronous path never reaches here -- it settles in
                # ``_poll_preparation`` -- and is handled there by the same
                # helper, keyed off the pending preparation's ``slate_first``.
                self._keep_slate_after_failed_hand_off(channel_id)
                return
            # U67: the seamless path was available and DECLINED this reload.
            # This used to be completely silent, and that silence is what made
            # the 2026-09-30 education incident (34 hours of black and silent
            # air) take so long to explain: the only trace anywhere was the
            # TRANSITIONING row the fallback below writes -- with ``last_error``
            # empty, because this path passes no ``failure_reason``. Name it.
            _LOG.warning(
                "Seamless content-reload for %s did not arm (state=%s, source=%s, pid=%s); "
                "falling back to terminate+restart. The channel stays TRANSITIONING on a "
                "pending reload until the worker exits at the end of the plan it is airing.",
                channel_id,
                state.state if state else "UNKNOWN",
                state.current_source_label if state else "-",
                _process_pid(process),
            )
        self._fall_back_to_restart_reload(channel_id, plan_end_at=rollover_plan_end_at)

    def _drain(self, channel_id: str) -> None:
        self._cancel_preparation(channel_id)
        self._clear_quality_recovery(channel_id, clear_slate=True)
        process = self._processes.get(channel_id)
        if process is None:
            self._close_as_run(channel_id)  # nothing on air to drain — close any open row
            self._write_state(channel_id, "STOPPED")
            self._append_health(channel_id, "STOPPED", sink_connected={})
            # Round 5 (coordinator review): another off-air route that
            # bypasses _stop entirely (nothing to send a stop through) --
            # see the matching pop/comment in _poll_process's worker-exit
            # branches and ``_rollover_plan_end_at``'s docstring.
            self._rollover_plan_end_at.pop(channel_id, None)
            # F3(b): a drain is a terminal off-air intent -- any plan held for
            # a restart that will now never run must be released, not left for
            # GC. Safe for the LIVE plan too: this release only ever touches a
            # plan that has not been aired yet.
            self._discard_prepared_restart_plan(channel_id, reason="channel drained")
            return
        state = self._store.read_state(channel_id)
        config = self._store.get_config(channel_id)
        self._draining_channels.add(channel_id)
        self._pending_reloads.pop(channel_id, None)
        # F3(b): see the matching release in the no-process branch above -- a
        # drain never restarts onto a held plan, so it must release it.
        self._discard_prepared_restart_plan(channel_id, reason="channel draining to off air")
        self._reload_kills.discard(channel_id)  # drain cancels a pending kill
        self._reload_kill_reasons.pop(channel_id, None)
        self._write_state(
            channel_id,
            "DRAINING",
            current_source_label=state.current_source_label if state else None,
            current_proof_event_id=state.current_proof_event_id if state else None,
            pid=_process_pid(process),
        )
        self._append_health(
            channel_id,
            "DRAINING",
            sink_connected=(
                self._sink_connected(channel_id, config, state="DRAINING") if config else {}
            ),
            seconds_on_air=self._seconds_on_air(channel_id),
        )

    def _stop(self, channel_id: str, *, draining: bool) -> None:
        self._cancel_preparation(channel_id)
        self._clear_quality_recovery(channel_id, clear_slate=True)
        # Item 78 fix 3 (coordinator review, round 3): the channel is coming
        # off air (or draining toward it) -- any rollover plan_end automation
        # recorded for an in-flight content-reload attempt is moot now (there
        # is no "outgoing leg" left to defer a switch against), so discard it
        # rather than leaving it to leak into whatever reload happens after
        # this channel is eventually restarted.
        self._rollover_plan_end_at.pop(channel_id, None)
        process = self._processes.pop(channel_id, None)
        self._started_at.pop(channel_id, None)
        self._on_air_confirmed_at.pop(channel_id, None)
        self._stderr_spawn_offset.pop(channel_id, None)
        self._stderr_spawn_identity.pop(channel_id, None)
        # Operator stop -- clear crash back-off AND the slate-EOS relaunch
        # counter (round-2 hostile review, item 4).
        self._reset_restart_tracking(channel_id, slate_eos_relaunches=True)
        self._draining_channels.discard(channel_id)
        self._pending_reloads.pop(channel_id, None)
        # Audit ENG-005: a leaked reload-kill flag would later misclassify a
        # genuine crash as a clean reload handoff.
        self._reload_kills.discard(channel_id)
        self._reload_kill_reasons.pop(channel_id, None)
        # F1/F3 fix: an armed-but-unsettled reload is moot once the channel is
        # stopped (nothing will ever read reload-status.json for it again) --
        # release it immediately instead of waiting for GC. Hostile-review
        # follow-up (second pass), item 3: this used to plain-pop
        # _pending_reload_settle (never releasing pending.plan_dir, never
        # logging, never recording the discarded reload_id for late-arrival
        # detection) -- routed through the shared helper now.
        self._discard_pending_reload_settlement(channel_id, reason="channel stopped")
        # F3(b): same reasoning, and the same unconditional placement -- a held
        # prepared-restart plan has not been aired by anything, so it is safe
        # to release on BOTH stop flavors (the comment below is about the
        # ACTIVE plan's directory, which the draining worker may still be
        # reading; this one it is not).
        self._discard_prepared_restart_plan(channel_id, reason="channel stopped")
        # Hostile-review follow-up (third pass), P2: the ACTIVE prepared-plan
        # directory is only safe to release here when this is a direct
        # (non-draining) stop -- the _process_terminate call further down
        # this method makes the worker's exit synchronous with this call, so
        # by the time we'd release it is already confirmed gone. A DRAINING
        # stop only sends the worker its graceful TERMINAL command below and
        # returns; the worker may still be airing from this very directory
        # for up to stop_all_channels' whole deadline_seconds window, so that
        # path defers this release to stop_all_channels' own observed-exit
        # loop instead (see _discard_active_prepared_plan_dir's docstring).
        if not draining:
            self._discard_active_prepared_plan_dir(channel_id)
        self._stderr_logs.pop(channel_id, None)
        self._hls_relay_dead.pop(channel_id, None)
        self._built_configs.pop(channel_id, None)
        self._clear_cg_overlay_proof(channel_id, "DRAINING" if draining else "STOPPING")
        # Operator stop — the channel comes off air now; close the open as-run
        # row. The encoder is popped from _processes here, so _poll_process will
        # never see its exit; this is the terminal close for the stop path.
        self._close_as_run(channel_id)
        if process is None:
            return
        self._write_state(channel_id, "DRAINING" if draining else "STOPPING")
        if draining:
            # RAT-004 graceful drain (CC-WS5-004): send the worker its TERMINAL
            # protocol command through the D2 control channel and DO NOT
            # force-terminate here. ``stop_all_channels`` owns the deadline loop:
            # it observes the worker's OS process exit as ground truth and
            # escalates to ``_process_terminate`` ONLY after the deadline. Killing
            # the worker here (as the pre-fix code did) force-terminated it before
            # it ever received its graceful terminal action. ``send_command`` is an
            # OPTIONAL strategy capability (only the GStreamer engine carries a
            # worker control channel; the concat strategy has none), so it is
            # resolved via ``getattr`` — mirroring the ``supports_content_reload``
            # capability probe above. If it is absent or returns False (no live
            # control channel / lost ack), the deadline escalation still reaps a
            # worker that has not exited.
            send_command = getattr(self._encoder_strategy, "send_command", None)
            if callable(send_command):
                send_command(self._work_dir, channel_id, "stop")
            return
        # Direct operator stop (NOT the drain): no deadline/escalation loop exists
        # to reap a hung worker, so keep the immediate force-terminate rather than
        # risk hanging on a dead control channel.
        _process_terminate(process)
        # CC-WS5-006: the worker is gone — release its control pipe so the
        # named-pipe server is not leaked until Job Object teardown. The drain
        # path defers this to stop_all_channels, which closes once exit is observed.
        self._close_worker_channel(channel_id)

    def stop_all_channels(self, *, deadline_seconds: float) -> DrainResult:
        """RAT-004: the missing graceful drain-all owner for supervised shutdown.

        Snapshots every channel this daemon instance currently tracks a live
        process for, issues the terminal ``_stop(channel_id, draining=True)``
        to each (the existing graceful-stop path), then waits on OBSERVED
        process exit — ``poll()``, not any acknowledgement — as ground truth,
        up to ``deadline_seconds``. A channel still alive at the deadline is
        escalated with a second call to the existing ``_process_terminate``
        kill and reported ``killed_after_deadline``; the drain still returns
        rather than hanging. Idempotent: a channel with no live process at
        snapshot time (already stopped/crashed) is reported ``already_gone``
        without issuing a redundant stop. Zero tracked channels is a clean
        no-op.

        Hostile-review follow-up (third pass), P2: ``_stop(channel_id,
        draining=True)`` deliberately does NOT release the channel's active
        prepared-plan directory (see ``_discard_active_prepared_plan_dir``'s
        docstring) -- the worker is still airing from it until THIS method
        observes its exit. Every terminal branch below (``already_gone``,
        ``drained`` at either the polling loop or the deadline check, and
        ``killed_after_deadline``) therefore releases it here instead, once
        exit is actually confirmed.
        """

        # App shutdown drains before stopping the automation loop. Close new
        # preparation admission first, so a completed conform cannot launch a
        # fresh worker while this snapshot is being drained.
        with self._preparation_guard:
            self._preparation_closed = True
            for channel_id in tuple(self._preparations):
                self._cancel_preparation(channel_id)
            snapshot = list(self._processes.items())
        if not snapshot:
            self._discard_all_prepared_restart_plans(reason="service shutdown")
            return DrainResult(outcomes=())

        outcomes: dict[str, str] = {}
        pending: dict[str, object] = {}
        for channel_id, process in snapshot:
            if _process_poll(process) is not None:
                # No live process to drain — pop it so a stale handle can't
                # linger, and report the terminal state without a redundant
                # stop (RAT-004 idempotency).
                self._processes.pop(channel_id, None)
                outcomes[channel_id] = "already_gone"
                self._discard_active_prepared_plan_dir(channel_id)
                # Round 5 (coordinator review): this channel never goes
                # through _stop on this path either -- another off-air
                # route that must clear the entry itself. See
                # ``_rollover_plan_end_at``'s docstring.
                self._rollover_plan_end_at.pop(channel_id, None)
                continue
            self._stop(channel_id, draining=True)
            pending[channel_id] = process

        deadline_at = self._monotonic() + max(deadline_seconds, 0.0)
        while pending and self._monotonic() < deadline_at:
            exited = [
                channel_id
                for channel_id, process in pending.items()
                if _process_poll(process) is not None
            ]
            for channel_id in exited:
                outcomes[channel_id] = "drained"
                self._discard_active_prepared_plan_dir(channel_id)
                del pending[channel_id]
            if pending:
                self._sleep(_DRAIN_POLL_INTERVAL_SECONDS)

        for channel_id, process in pending.items():
            # Ground truth is re-checked one last time at the deadline boundary
            # before escalating, so a process that exits exactly on the last
            # tick is still reported ``drained`` rather than needlessly killed.
            if _process_poll(process) is not None:
                outcomes[channel_id] = "drained"
                self._discard_active_prepared_plan_dir(channel_id)
                continue
            _process_terminate(process)
            outcomes[channel_id] = "killed_after_deadline"
            self._discard_active_prepared_plan_dir(channel_id)

        # CC-WS5-006: shutdown/drain-all is a terminal off-air for each channel —
        # release its worker control pipe now that its process exit is resolved, so
        # no named-pipe server leaks past supervised shutdown.
        for channel_id, _ in snapshot:
            self._close_worker_channel(channel_id)
        # F3(b): service shutdown is a terminal off-air for every channel this
        # daemon owns -- no restart will ever consume a prepared-restart plan
        # still held here, so release them all rather than leak their
        # directories for GC. Swept once, after the drain, so a channel that
        # held a plan without appearing in the snapshot is covered too.
        self._discard_all_prepared_restart_plans(reason="service shutdown")

        return DrainResult(
            outcomes=tuple(
                ChannelDrainOutcome(channel_id, outcomes[channel_id]) for channel_id, _ in snapshot
            )
        )

    def _sync_cg_overlay_proof(self, channel_id: str, state: EgressState) -> None:
        if self._cg_overlay_proof_provider is None:
            return
        proof = self._cg_overlay_proof_provider(channel_id)
        if proof is None:
            self._last_cg_overlay_event_keys.pop(channel_id, None)
            self._clear_cg_overlay_proof(channel_id, state)
            return
        proof_key = (proof.status, proof.overlay_id, proof.blocker)
        if proof.status != "READY":
            if self._last_cg_overlay_event_keys.get(channel_id) == proof_key:
                return
            self._append_cg_overlay_event(proof, state=state)
            self._last_cg_overlay_event_keys[channel_id] = proof_key
            return
        if self._active_cg_overlay_ids.get(channel_id) == proof.overlay_id:
            return
        previous_overlay_id = self._active_cg_overlay_ids.get(channel_id)
        if previous_overlay_id is not None:
            clear_proof = build_cg_overlay_clear_egress_proof(
                channel_id=channel_id,
                overlay_id=previous_overlay_id,
            )
            self._append_cg_overlay_clear_event(clear_proof, state=state)
        self._append_cg_overlay_event(proof, state=state)
        self._active_cg_overlay_ids[channel_id] = proof.overlay_id
        self._last_cg_overlay_event_keys[channel_id] = proof_key

    def _clear_cg_overlay_proof(self, channel_id: str, state: EgressState) -> None:
        overlay_id = self._active_cg_overlay_ids.pop(channel_id, None)
        self._last_cg_overlay_event_keys.pop(channel_id, None)
        if overlay_id is None:
            return
        clear_proof = build_cg_overlay_clear_egress_proof(
            channel_id=channel_id,
            overlay_id=overlay_id,
        )
        self._append_cg_overlay_clear_event(clear_proof, state=state)

    def _append_cg_overlay_event(self, proof: EgressCgOverlayProof, *, state: EgressState) -> None:
        self._store.append_proof_event(
            EgressProofEvent(
                event_id=f"egress-cg-overlay-{uuid.uuid4()}",
                observed_at=datetime.now(UTC),
                channel_id=proof.channel_id,
                state=state,
                source_label=proof.operator_label,
                source_path=f"cg-overlay:{proof.overlay_id}",
                source_ref=proof.overlay_id,
                proof_boundary=CG_EGRESS_PROOF_BOUNDARY,
                machine_summary=_cg_overlay_event_summary(proof),
            )
        )

    def _append_cg_overlay_clear_event(
        self,
        proof: EgressCgOverlayClearProof,
        *,
        state: EgressState,
    ) -> None:
        self._store.append_proof_event(
            EgressProofEvent(
                event_id=f"egress-cg-overlay-clear-{uuid.uuid4()}",
                observed_at=datetime.now(UTC),
                channel_id=proof.channel_id,
                state=state,
                source_label=proof.operator_label,
                source_path=f"cg-overlay-clear:{proof.overlay_id}",
                source_ref=proof.overlay_id,
                proof_boundary=CG_EGRESS_PROOF_BOUNDARY,
                machine_summary=_cg_overlay_clear_event_summary(proof),
            )
        )

    def _seconds_on_air(self, channel_id: str) -> int:
        started_at = self._started_at.get(channel_id)
        if started_at is None:
            return 0
        return max(0, int(self._monotonic() - started_at))

    def _sink_connected(
        self,
        channel_id: str,
        config: EgressConfig,
        *,
        state: EgressState,
    ) -> dict[str, bool]:
        # Health is keyed by the sinks the RUNNING pipeline was built with,
        # decided HERE so every appender gets it -- the start, the poll tick,
        # the fallback-slate transition and the content-reload settlement
        # (review round 4 delta, MAJOR 1: the settlement passed the config
        # row as it stood NOW, so a program-boundary reload made a sink saved
        # after the build read as "delivering" for one automation tick). The
        # caller's config is only the fallback for a process this daemon has
        # no recorded build for.
        config = self._built_configs.get(channel_id) or config
        metrics = self._health_metrics(channel_id, state=state)
        if self._sink_health_provider is not None:
            health = self._sink_health_provider(channel_id, config, metrics)
        else:
            health = build_default_sink_health(config=config, metrics=metrics, state=state)
        # MAJOR M1: neither the injected provider nor the default health
        # builder above knows about the HLS relay CHILD PROCESS -- both only
        # see the main encoder's own send progress. A relay confirmed dead by
        # _poll_hls_relay overrides the hls sink(s) to unhealthy here, in one
        # place, regardless of which health path produced the dict, so
        # /api/staff/egress/channels/{id}/health stops reporting "connected"
        # for a dead relay.
        if self._hls_relay_dead.get(channel_id):
            hls_labels = [sink.label for sink in config.sinks if sink.kind == "hls"]
            if hls_labels:
                # Copy rather than mutate in place -- a provider (or the cached
                # default builder result) may hand back a dict it reuses.
                health = dict(health)
                for label in hls_labels:
                    health[label] = False
        return health


def _default_pid_is_dead(pid: int) -> bool | None:
    """Definitive tri-state liveness for restart reconciliation. NEVER guesses.

    Unlike ``_default_orphan_probe`` (whose ``None`` conflates
    ``psutil.NoSuchProcess`` with ``psutil.AccessDenied`` and is therefore only
    safe for the *never-kill* orphan-reaper decision), this answers a strict
    question -- "is this pid definitely gone?" -- and fails CLOSED on
    uncertainty:

    * ``True``  -- the pid does not exist (``psutil.pid_exists`` False): the
      persisted claim is definitively dead and may be reconciled.
    * ``False`` -- the pid exists (present in the process table): a live
      process holds it, so the claim must be left alone. Access-denied still
      returns False here: the pid EXISTS, it is merely unreadable, which is
      uncertainty about IDENTITY, not about liveness.
    * ``None``  -- we could not determine liveness (probe raised, no psutil):
      unknown => the caller MUST NOT reconcile.

    This deliberately does not modify ``_default_orphan_probe`` or the
    orphan-reaper's own semantics; it is a separate, stricter predicate used
    only by ``reconcile_stale_state``.
    """
    try:
        import psutil
    except Exception:  # psutil unavailable: unknown, never assume dead
        return None
    try:
        exists = psutil.pid_exists(pid)
    except Exception:  # any probe failure is uncertainty, not proof of death
        return None
    if exists:
        return False
    # pid_exists said the pid is gone. pid_exists can be fooled on some
    # platforms by access restrictions, so corroborate with a second,
    # independent signal: enumerating the process table. Only if BOTH agree the
    # pid is absent do we call it definitively dead.
    try:
        if pid in psutil.pids():
            return False
    except Exception:
        return None
    return True


def _epoch_seconds(when: datetime) -> float:
    """``when`` as an epoch float, treating a NAIVE datetime as UTC.

    ``EgressStateRow.updated_at`` is aware in production (the Postgres column is
    ``DateTime(timezone=True)``), but the SQLite backend hands it back naive, and
    the daemon writes UTC (``datetime.now(UTC)``) on every path. A naive value is
    therefore a UTC value whose offset was dropped, not a local-time one; calling
    ``.timestamp()`` on it directly would silently shift it by the host's offset
    and could make a live encoder look like a reincarnated pid.
    """

    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.timestamp()


def _default_orphan_probe(pid: int) -> OrphanInfo | None:
    """Return the running process identity for ``pid``, or None."""

    import psutil

    try:
        process = psutil.Process(pid)
        return OrphanInfo(name=process.name(), created_at=process.create_time())
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied:
        # Audit ENG-009: an unreapable orphan must at least be visible.
        _LOG.warning(
            "Orphan probe denied access to pid %s; if it is a predecessor's "
            "encoder it cannot be reaped from this process.",
            pid,
        )
        return None


def _default_orphan_terminator(pid: int, created_at: float) -> None:
    # S9 §6.3: the encoder-orphan terminator and the optional-co-process terminator
    # share one TOCTOU-safe kill primitive (re-verify create_time; never kill a
    # recycled pid; AccessDenied is logged, never raised into _start — audit ENG-001/009).
    verify_and_kill_process(pid, created_at)


def _process_pid(process: object) -> int | None:
    return getattr(process, "pid", None)


def _reload_stall_bound_seconds(plan_end_at: datetime | None) -> float:
    """U67: the reload-stall watchdog's bound for a pin armed against
    ``plan_end_at`` -- the rollover horizon automation recorded for this reload,
    or ``None`` when the reload carried none.

    ``_TRANSITIONING_WATCHDOG_SECONDS`` covers every pin with a lead or less of
    plan left, which is every pin automation itself can dispatch. When the
    recorded horizon is FURTHER out than that, the pin's honest lifetime really
    is longer -- the worker cannot reach plan EOS before then -- so the bound is
    stretched to "the plan that is left, plus the lead and settle window the
    restart behind it costs" rather than firing early against a channel that is
    still airing correctly. A horizon already in the past (the incident's own
    shape: the recorded boundary was 554s away when the reload was dispatched,
    and long past by the time anyone looked at it) leaves the flat bound
    standing."""
    if plan_end_at is None:
        return _TRANSITIONING_WATCHDOG_SECONDS
    remaining = (plan_end_at - datetime.now(UTC)).total_seconds()
    return max(
        _TRANSITIONING_WATCHDOG_SECONDS,
        remaining + _TRANSITIONING_WATCHDOG_LEAD_SECONDS + _PENDING_RELOAD_SETTLE_DEADLINE_S,
    )


def _stderr_log_size(path: Path) -> int:
    """Byte size of a channel's stderr log right now, 0 if it does not exist
    (or is otherwise unreadable) yet. Round-4 fix (PR #183 review, BLOCKER
    reproduced): used to snapshot ``EgressDaemon._stderr_spawn_offset`` at
    spawn time -- ``strategy.py`` / ``_ffmpeg.py`` open this fixed per-channel
    log in APPEND mode and never truncate it per spawn, so a fresh worker's
    "spawn point" in the log is this offset, not byte 0."""

    try:
        return path.stat().st_size
    except OSError:
        return 0


def _stderr_log_identity(path: Path) -> tuple[int, int] | None:
    """File identity for append-log generation tracking across rotation."""

    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino


class _PollableProcess(Protocol):
    def poll(self) -> int | None: ...


def _process_poll(process: object) -> int | None:
    return cast(_PollableProcess, process).poll()


def _process_terminate(process: object) -> None:
    process.terminate()  # type: ignore[attr-defined]


_WEDGED_WORKER_TERMINATE_WAIT_S = 3.0


def _process_terminate_bounded(
    process: object, *, wait_s: float = _WEDGED_WORKER_TERMINATE_WAIT_S
) -> None:
    """Item 85: terminate -> bounded wait -> kill, for a worker confirmed wedged
    (a reload ack timeout on a still-alive pid -- see ``_try_content_reload``'s
    "not armed" branch). A wedged worker's GLib main-loop thread is blocked
    inside a synchronous GStreamer call, not inside Python, so ``terminate()``
    (SIGTERM / a Windows console-control-style request) is not guaranteed to be
    observed promptly -- escalate to ``kill()`` if the process has not actually
    exited within ``wait_s``, so this call is itself bounded and can never hang
    the calling thread the way the wedge itself hangs the worker's own."""
    with contextlib.suppress(Exception):
        _process_terminate(process)
    poller = cast(_PollableProcess, process)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if poller.poll() is not None:
            return
        time.sleep(0.1)
    with contextlib.suppress(Exception):
        process.kill()  # type: ignore[attr-defined]


def _process_close(process: object) -> None:
    close = getattr(process, "close", None)
    if close is not None:
        close()


def _proof_event_summary(
    *,
    state: str,
    previous_state: str | None,
    previous_source_label: str | None,
    source_kind: str,
    source_label: str,
    channel_id: str,
) -> str:
    if state == "TRANSITIONING":
        prior = previous_source_label or previous_state or "previous source"
        if source_kind == "live":
            return (
                f"CivicCast began live takeover from {prior!r} to {source_label!r} "
                f"for channel {channel_id!r} at the configured handoff boundary."
            )
        return (
            f"CivicCast began an egress handoff from {prior!r} to {source_label!r} "
            f"for channel {channel_id!r} at the configured handoff boundary."
        )
    if state == "FALLBACK_SLATE":
        return (
            f"CivicCast entered fallback slate {source_label!r} for channel "
            f"{channel_id!r} at the configured handoff boundary."
        )
    if previous_state == "FALLBACK_SLATE":
        return (
            f"CivicCast exited fallback slate and started egress source {source_label!r} "
            f"for channel {channel_id!r} at the configured handoff boundary."
        )
    if source_kind == "live":
        return (
            f"CivicCast put live source {source_label!r} on air for channel "
            f"{channel_id!r} at the configured handoff boundary."
        )
    if previous_source_label and previous_source_label.lower().startswith("live:"):
        return (
            f"CivicCast released live source {previous_source_label!r} and returned to "
            f"scheduled source {source_label!r} for channel {channel_id!r} at the "
            "configured handoff boundary."
        )
    return (
        f"CivicCast started egress source {source_label!r} for channel "
        f"{channel_id!r} at the configured handoff boundary."
    )


def _cg_overlay_event_summary(proof: EgressCgOverlayProof) -> str:
    if proof.status == "BLOCKED":
        return (
            f"CivicCast blocked emergency banner {proof.overlay_id!r} for channel "
            f"{proof.channel_id!r}: {proof.blocker}. This is not an EAS claim."
        )
    return (
        f"CivicCast raised emergency banner {proof.overlay_id!r} "
        f"({proof.overlay_title!r}, severity {proof.severity!r}) for channel "
        f"{proof.channel_id!r}. This is not an EAS claim."
    )


def _cg_overlay_clear_event_summary(proof: EgressCgOverlayClearProof) -> str:
    return (
        f"CivicCast cleared emergency banner {proof.overlay_id!r} for channel "
        f"{proof.channel_id!r}. This is not an EAS claim."
    )
