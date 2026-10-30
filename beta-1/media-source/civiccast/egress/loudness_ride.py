# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""U25 -- the speech-leveling ride that runs INSTEAD OF loudnorm before playout.

Why the conform stopped using ``loudnorm``
------------------------------------------
``loudnorm`` normalizes a program to its target *on average*.  A public meeting
is not stationary: a chair at the dais, a speaker who steps back from the
lectern, and an un-mic'd speaker at the public-comment mic can sit 15-20 LU
apart inside one recording, so a single whole-program gain necessarily lands
every four-minute stretch somewhere else.  The acceptance bar for a prepared
egress asset is therefore per-stretch, not per-program: **every 240 s window of
the emitted program, in both window families, must land inside
-16 +/- 1.0 LUFS**, with the whole program inside +/- 0.5 LU.  No built-in
FFmpeg leveler reaches that band (U25 rounds 1-2 measured ``dynaudnorm``,
``speechnorm``, ``compand``, ``loudnorm`` one-pass and two-pass, and chains of
them); a slow, speech-following gain ride does.

Shape of the ride
-----------------
::

    source decode (f32le, conformed rate)   ->  per-sample gain curve (pure)
                                            ->  numpy multiply, one chunk at a time
                                            ->  volume=<trim>dB, 4x upsample,
                                                alimiter=<true-peak ceiling>,
                                                4x downsample
                                            ->  AAC / MPEG-TS  (the audio the
                                                video pass then stream-copies)
                                            ->  emitted-true-peak guard: measure
                                                the artifact, re-emit once lower
                                                if it is hotter than -1.0 dBTP

Everything above the process boundary is a pure function of numbers, so the
rules that decide the gain -- the two-stage gate, the window curve, the silence
hold, the clamp, the gap cap, the slew limit, the correction, the sum, the
converge loop -- are all unit-testable without touching FFmpeg.  The process
entry points (:func:`build_source_series_args`, :func:`build_measure_sink_args`,
:func:`build_encode_sink_args`, :func:`build_artifact_measure_args`,
:func:`build_peak_scan_args`, :func:`build_reencode_args`,
:func:`build_video_from_source_args`) are pure argument builders too, so the
exact FFmpeg grammar is asserted in tests rather than described in prose.

Two-stage gate
--------------
The ride's own loudness arithmetic (:func:`gated_loudness`) is the EBU R128
two-stage gate that the *acceptance* measurement uses, so the ride and the gate
agree by construction rather than by calibration.

The converge loop
-----------------
The gain curve alone gets the program close but not to the bar: the limiter that
protects the emitted true peak is not an identity function, so a ride that
targets -16.0 LUFS emits somewhere near -16.5.  The loop therefore measures the
rendered result and corrects it, three passes per iteration:

``A``  the ride alone, with no trim and no limiter -> ``I_A``, which sets
       ``trim0 = TARGET - I_A`` (the trim that *would* be right if the limiter
       were transparent);
``B``  the ride with ``trim0`` -> ``I_B``, whose residual ``TARGET - I_B`` is
       the limiter's shortfall;
``C``  the ride with ``trim1 = trim0 + (TARGET - I_B) / max(slope, 0.4)``, where
       ``slope`` is the limiter's *measured* dB-per-dB from the previous
       iteration (``(I_C - I_B) / (trim1 - trim0)``).  The floor of 0.4 keeps a
       nearly-flat measured slope from launching the trim to infinity.

The loop stops when the emitted whole-program error is within
:data:`RideParams.stop_lu`, or when an iteration fails to improve on the
previous one, or at :data:`RideParams.max_iters`.  Then the correction curve --
``target - L_W(t)`` of what iteration ``C`` actually measured -- is summed into
the applied curve for the next iteration, and the gap cap is re-applied.

Measured effect (U25 Q12, 4D36 and BIG, the two cells round 5 failed): the
slope-corrected loop moved 4D36 from -0.700 to -0.1 LU and BIG from -0.600 to
+0.1 LU of whole-program error, while the worst 240 s window *improved* on both
(0.899 -> 0.513 and -> 0.289).  Round 5's plain ``trim1 = trim0 + err`` step is
what pinned them 0.6 LU low.

The output stage bounds the true peak, and the emit proves it
------------------------------------------------------------
``alimiter`` is a *sample*-peak limiter: it acts on the samples it is handed, so
on its own it cannot bound the inter-sample peaks an encoder will later create.
U25 Q14 measured the cost of that gap directly -- with a -3.0 dBFS sample-peak
ceiling, BIG's ride output sat at exactly -3.000 dBFS (true peak -0.8 dBTP) and
the emitted AAC still carried four frames above 0 dBFS (peak +2.754 dBFS,
emitted true peak +2.8 dBTP), i.e. essentially all of the reported true-peak
growth was the encoder's, and no sample-peak ceiling can prevent it.  So the
limiter is handed a 4x-oversampled copy of the signal (48 kHz -> 192 kHz, soxr,
precision 28), limits at ``-1.5 dBTP`` -- the value the incumbent
``loudnorm ... TP=-1.5`` conform promised -- and the result is decimated back to
the conformed rate before the AAC encode.

Oversampling makes the *pre*-AAC true peak bounded; it does not by itself make
the *artifact* compliant, because the codec's overshoot is its own -- U25 round 7
measured the limiter holding to +0.0034 dB over its own ceiling while the AAC
round trip added up to +3.27 dB, which is why a lowered ceiling does not buy a
proportionally lower emitted peak.  U43 then showed the overshoot is codec-side
rather than spectral -- a lowpass at the encoder's own cutoff moved the peak by
+0.00 dB -- so the only lever that reaches it is headroom at the encoder.  The
artifact therefore gets the last word, through the pure decision functions that
are this module's whole guard API: after an attempt's emit, the emitted true
peak, the loudness and the decoded sample peak are measured seek-free,
:func:`guard_pad_db` returns how far the one pad round moves -- headroom equal to
the measured overshoot plus :data:`TP_GUARD_PAD_MARGIN_DB`, capped at
:data:`TP_GUARD_MAX_PAD_DB`, with the drive raised by the same amount so the
programme loudness holds -- and :func:`select_leveled_attempt` picks which
attempt ships -- the loudness gates first, then the lowest decoded sample peak,
then the lowest emitted true peak, keeping the nominal attempt when nothing
separates them.

That round is a *re-encode*, not a re-convergence.  The ride PCM is already
correct, so the guard re-runs only the limiter, the codec and the mux, over the
bytes the nominal emit consumed -- ``run_ride``'s ``pcm_path`` tee retains them
(:func:`build_reencode_args`, :func:`run_reencode`) -- so every round costs
seconds, not a second full ride.  The emitted true peak is best effort at
:data:`TP_GUARD_TARGET_DBTP`; the *guarantee* is
:data:`TP_GUARD_MAX_PEAK_DBFS` -- no decoded sample above +0.1 dBFS -- and it is
measured on the artifact rather than promised by a ceiling, which is why an
artifact over it is *reported* rather than re-emitted: the selector keeps the best
attempt it has, and an over-bound one ships with an error line, because a channel
that airs a slightly hot artifact beats one that airs nothing.  U25's panel is
what settled that: BIG's nominal emit, the hottest artifact the panel produced,
sits at +0.0036 dBFS from one sample in 9000 s, and every attempt that tried to
fix it landed worse.  The caller owns the ride, the encode and the measurement
*policy*: :func:`level_window` is the one entry point here that spawns the passes
(a source series, the converge probes, one emit, the artifact's measurement, at
most one pad round and then the encoder variants while the bound is still
missed), and it does that only because the caller asked for a leveled
window by calling it -- every piece it spawns is a module function the caller
could have run itself.  It never logs: it returns a :class:`LeveledSelection`
whose ``warning`` the caller logs, and a ``round_error`` the caller logs beside
it.  The trigger rate and the time it costs are counted in the unit report.

The acceptance gate, ported
---------------------------
The station scores a published artifact by its own geometry -- 240 s windows
tiled from 0 s and from 120 s, each window's gated loudness within 0.9 LU of
target, the whole program within 0.5 LU -- so :func:`level_window` judges its
attempts by that same arithmetic rather than by "close enough": see
:func:`window_levels`, :func:`worst_window_deviation_lu` and
:func:`whole_program_err_lu`, and the ``LOUDNESS_*`` constants they are built
from.  The port is deliberately *unrounded*, where the harness rounds each
window's level and start to 3 dp for display, and it skips a window it cannot
measure rather than scoring it -- an unmeasurable window is not evidence of a
deviation.  The printed summary ``I`` is **not** the gate: it is a separate
quantity FFmpeg computes, the panel proved the two disagree, so
:attr:`LeveledAttempt.emitted_lufs` records it for the report while
``loudness_ok`` reads the series.

The gap cap is not a leveler for room tone
------------------------------------------
:func:`gap_cap` exists so the ride cannot lift a quiet room to target -- that is
how a meeting recording ends up sounding like a compressor pumping between
speakers.  It is kept, and it is known not to be sufficient: U25 measured a
``+13.698 dB`` lift on 4D36's quietest gap.  Recorded as a known beta.10
limitation rather than hidden; see the unit's report.

Failure is a degrade, never a channel outage
--------------------------------------------
Every failure inside the ride -- numpy absent, an FFmpeg that will not start, a
pass that exits non-zero or times out, an ebur128 series that cannot be parsed
-- raises :class:`LoudnessRideError`, which the preparer catches and turns into
ONE warning plus the pre-U25 single-pass ``loudnorm`` conform.  The channel
airs; only the leveling is missing.  This mirrors ``_measure_loudnorm_metadata``'s
existing degrade contract (U20), and a degraded artifact is never promoted into
the whole-asset conform cache.  Cancellation is not a failure: it raises
:class:`LoudnessRideCancelledError` and is re-raised as a shutdown signal.
"""

from __future__ import annotations

import contextlib
import importlib
import math
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from civiccast.egress.models import CanonicalProfile, EgressSourceSegment

__all__ = [
    "DIGITAL_SILENCE",
    "LOUDNESS_WHOLE_TOL_LU",
    "LOUDNESS_WINDOW_MIN_TAIL_S",
    "LOUDNESS_WINDOW_OFFSET_S",
    "LOUDNESS_WINDOW_S",
    "LOUDNESS_WINDOW_TOL_LU",
    "TP_GUARD_MAX_PAD_DB",
    "TP_GUARD_MAX_PEAK_DBFS",
    "TP_GUARD_PAD_MARGIN_DB",
    "TP_GUARD_TARGET_DBTP",
    "LeveledAttempt",
    "LeveledSelection",
    "LeveledWindow",
    "LoudnessRideCancelledError",
    "LoudnessRideError",
    "LoudnessRideUnavailableError",
    "ReencodeRunner",
    "RideCurve",
    "RideParams",
    "RideRender",
    "RideRunner",
    "attempt_from_measurement",
    "build_artifact_measure_args",
    "build_encode_sink_args",
    "build_measure_sink_args",
    "build_peak_scan_args",
    "build_reencode_args",
    "build_source_series_args",
    "build_video_from_source_args",
    "canonical_video_filter",
    "combine",
    "converge",
    "correction_curve",
    "curve_stats",
    "gain_curve",
    "gap_cap",
    "gated_loudness",
    "guard_pad_db",
    "level_window",
    "limit_value",
    "limiter_filter",
    "measure_artifact",
    "parse_ebur128_series",
    "parse_integrated_lufs",
    "parse_true_peak_dbtp",
    "resample_filter",
    "run_capture",
    "run_reencode",
    "run_ride",
    "scan_peak_dbfs",
    "select_leveled_attempt",
    "series_duration_s",
    "slew_limit",
    "sliding_levels",
    "to_arrays",
    "whole_program_err_lu",
    "window_levels",
    "worst_window_deviation_lu",
]

#: ebur128 emits one momentary block per 100 ms.
BLOCK_STEP_S = 0.1

#: ebur128's absolute gate (EBU R128 / ITU-R BS.1770-4).
SILENCE_FLOOR_LUFS = -70.0

#: The relative gate, measured from the window's own ungated loudness.
RELATIVE_GATE_LU = -10.0

#: What a block that ebur128 reports as ``nan``/``-inf`` (digital silence)
#: becomes here: far below any gate, and finite so the arithmetic stays real.
DIGITAL_SILENCE = -1e9

#: 65536 frames of float32 stereo = 1.365 s = 512 KiB, the largest allocation
#: the ride makes for audio.  Peak memory is therefore a function of this
#: constant and the curve's length, never of how long the asset is -- that is
#: the bounded-memory requirement, and it is why a 2.5 h asset and an 8 s asset
#: ride at the same resident cost.
CHUNK_FRAMES = 65536

#: The acceptance gate's own geometry: a 240 s window, tiled from both the start
#: of the program and from a 120 s offset, every window's gated loudness within
#: 0.9 LU of target and the whole program's within 0.5 LU.  These are the
#: numbers the station's acceptance harness scores a published artifact by; they
#: are constants here rather than parameters because a ride that converged to a
#: different gate would be optimizing for a contract nobody published.
LOUDNESS_WINDOW_S = 240.0
LOUDNESS_WINDOW_OFFSET_S = 120.0
LOUDNESS_WINDOW_TOL_LU = 0.9
LOUDNESS_WHOLE_TOL_LU = 0.5

#: The harness tiles while ``start < duration - 1``: a tail shorter than this is
#: not a window, because a fraction of a window measures a fraction of the
#: program and would fail for arithmetic reasons rather than for level.
LOUDNESS_WINDOW_MIN_TAIL_S = 1.0

#: The children are dropped below normal priority so a ride (foreground,
#: background warm, or a second channel's) can never outrank an on-air encoder.
_BELOW_NORMAL_PRIORITY_CLASS = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_RE_I = re.compile(r"^\s*I:\s*(-?[\d.]+)\s*LUFS", re.M)
_RE_SERIES = re.compile(
    r"\]\s*t:\s*([\d.]+)\s+.*?M:\s*(-?[\d.]+|nan|-inf)\s+S:\s*(-?[\d.]+|nan|-inf)"
)
#: The true-peak SUMMARY line, and only that: the per-block lines carry ``TPK:``
#: columns, which a looser pattern would read as a peak measurement.  Matched
#: with ``.search`` -- the first ``Peak:`` line, exactly as the acceptance
#: harness reads it, so a chatty multi-pass stderr cannot shift the reading.
_RE_TP = re.compile(r"^\s*(?:True )?[Pp]eak:\s*(-?[\d.]+)\s*dBFS", re.M)


class LoudnessRideError(RuntimeError):
    """The ride could not produce a leveled program.  Callers degrade."""


class LoudnessRideCancelledError(LoudnessRideError):
    """The ride was cancelled -- a shutdown/pause signal, not a failure."""


class LoudnessRideUnavailableError(LoudnessRideError):
    """The ride cannot run in this installation (numpy is not importable)."""


@dataclass(frozen=True)
class RideParams:
    """Every number the ride obeys, in one reviewable place.

    The defaults are the U25 round-5/Q12 chain exactly; a caller that wants a
    different ride changes this object, not the code.
    """

    target_lufs: float
    sample_rate: int = 48_000
    channels: int = 2

    #: The ride's own follow window.  Short enough to track a speaker change,
    #: long enough that it does not chase syllables.
    window_s: float = 45.0
    #: The curve's grid: one gain point per second.
    step_s: float = 1.0
    #: How far the ride may pull a passage down, and how far up.  The ceiling is
    #: deliberately high (a distant public-comment mic can be 20 LU down); the
    #: gap cap is what keeps it from lifting room tone to that ceiling.
    g_min_db: float = -6.0
    g_max_db: float = 18.0
    #: The causal rate limit on the applied gain.
    slew_db_per_s: float = 0.5

    #: Gap cap: in a passage whose emitted momentary loudness is below the
    #: absolute gate, or more than ``gap_rel_lu`` under its own window's gated
    #: loudness, the gain is capped here.
    gap_cap_db: float = 8.0
    gap_rel_lu: float = 20.0
    gap_abs_lufs: float = SILENCE_FLOOR_LUFS

    #: How far one iteration's correction curve may move the gain.
    correction_bound_db: float = 30.0
    #: Loop bounds.  ``stop_lu`` is the whole-program error that ends the loop;
    #: ``slope_floor`` floors the measured limiter slope (round 5/6 measured
    #: 0.521-0.615 on the cells that needed correcting).
    max_iters: int = 4
    stop_lu: float = 0.1
    slope_floor: float = 0.4
    #: An iteration that improves the whole-program error by less than this is
    #: treated as converged-but-not-there and stops the loop.
    min_improvement_lu: float = 0.01

    #: The emitted TRUE-peak ceiling (dBTP) the output stage enforces, and the
    #: limiter's own parameters.  -1.5 dBTP is what the incumbent
    #: ``loudnorm ... TP=-1.5`` conform promised; this ride keeps that promise
    #: rather than the -3.0 dBFS *sample*-peak ceiling it shipped with before
    #: Q14.  ``level=0`` is load-bearing: without it ``alimiter`` normalizes the
    #: limited signal back up and undoes the trim.
    limit_dbtp: float = -1.5
    limiter_attack_ms: float = 5.0
    limiter_release_ms: float = 50.0

    #: The limiter sees a 4x-oversampled copy of the signal so that the ceiling
    #: above is a real true-peak ceiling rather than a sample-peak one -- see the
    #: module docstring.  ``resampler=None`` builds a plain ``aresample``, for a
    #: host whose FFmpeg has no soxr (the ride then fails and the caller degrades
    #: to the pre-U25 conform, which is the documented failure path).
    oversample_factor: int = 4
    resampler: str | None = "soxr"
    resampler_precision: int = 28

    #: Q10: re-apply the slew limit to the SUMMED (base + correction) curve.
    #: Both inputs already carry the limit, so without this the sum can move at
    #: up to twice the specified slew wherever base and correction move the same
    #: way.  ON by the coordinator's Q10 order.  The measured window effect is in
    #: the unit report: round 4 saw a second causal pass cost 2.6 dB of lag on
    #: EF4, and round-5's passing numbers were measured with this OFF, so the two
    #: settings are NOT interchangeable evidence.  One-line revert if the
    #: coordinator prefers the measured-behaviour default.
    slew_summed_curve: bool = True

    chunk_frames: int = CHUNK_FRAMES


# ---------------------------------------------------------------------------
# Pure functions: the gate, the curve, the cap, the slew, the sum.
# ---------------------------------------------------------------------------


def gated_loudness(levels: list[float]) -> float | None:
    """Integrated loudness of gating blocks, EBU R128 two-stage gate.

    Identical arithmetic to the acceptance measurement, so the ride and the gate
    agree by construction.  ``None`` means nothing survived the absolute gate.
    """
    kept_abs = [v for v in levels if v > SILENCE_FLOOR_LUFS]
    if not kept_abs:
        return None
    ungated = 10.0 * math.log10(sum(10.0 ** (v / 10.0) for v in kept_abs) / len(kept_abs))
    kept = [v for v in kept_abs if v > ungated + RELATIVE_GATE_LU]
    if not kept:
        return None
    return 10.0 * math.log10(sum(10.0 ** (v / 10.0) for v in kept) / len(kept))


def sliding_levels(
    blocks: list[tuple[float, float]],
    window_s: float,
    step_s: float,
) -> list[tuple[float, float | None]]:
    """Gated loudness of a ``window_s`` window centred on every ``step_s``."""
    if not blocks:
        return []
    n = len(blocks)
    half = max(1, round(window_s / 2.0 / BLOCK_STEP_S))
    span = max(1, round(step_s / BLOCK_STEP_S))
    out: list[tuple[float, float | None]] = []
    for k in range(0, n, span):
        i = min(k, n - 1)
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out.append((blocks[i][0], gated_loudness([v for _, v in blocks[lo:hi]])))
    return out


def slew_limit(
    curve: list[tuple[float, float]],
    slew_db_per_s: float,
    step_s: float,
) -> list[tuple[float, float]]:
    """Causal forward rate limit: the gain may move at most ``slew * step``.

    Causal on purpose: an anti-causal pass anticipates a step by dragging the new
    target backwards through the quiet run-up, attenuating material that has not
    changed yet.  A causal pass can only ever be late, never wrong in the run-up.
    """
    if not curve:
        return []
    limit = slew_db_per_s * step_s
    out = [list(p) for p in curve]
    for i in range(1, len(out)):
        d = out[i][1] - out[i - 1][1]
        if d > limit:
            out[i][1] = out[i - 1][1] + limit
        elif d < -limit:
            out[i][1] = out[i - 1][1] - limit
    return [(t, v) for t, v in out]


def gain_curve(
    levels: list[tuple[float, float | None]],
    *,
    target_lufs: float,
    g_min_db: float,
    g_max_db: float,
    slew_db_per_s: float,
    step_s: float,
) -> list[tuple[float, float]]:
    """``clamp(target - level)`` with the silence hold, then the slew limit.

    The silence hold matters: a window with nothing above the absolute gate has
    no measurable level, and dropping the gain to 0 dB there would punch a hole
    in the program at every pause.  The previous gain is carried instead.
    """
    raw: list[float] = []
    held: float | None = None
    for _, level in levels:
        if level is None:
            raw.append(0.0 if held is None else held)
            continue
        held = min(max(target_lufs - level, g_min_db), g_max_db)
        raw.append(held)
    return slew_limit(
        [(t, v) for (t, _level), v in zip(levels, raw, strict=True)], slew_db_per_s, step_s
    )


def _momentary_at(blocks: list[tuple[float, float]], t: float) -> float:
    """Momentary loudness of the block nearest ``t`` (blocks are 100 ms apart)."""
    if not blocks:
        return DIGITAL_SILENCE
    lo, hi = 0, len(blocks) - 1
    if t <= blocks[0][0]:
        return blocks[0][1]
    if t >= blocks[-1][0]:
        return blocks[-1][1]
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if blocks[mid][0] <= t:
            lo = mid
        else:
            hi = mid
    return blocks[lo][1] if (t - blocks[lo][0]) <= (blocks[hi][0] - t) else blocks[hi][1]


def gap_cap(
    curve: list[tuple[float, float]],
    blocks: list[tuple[float, float]],
    levels: list[tuple[float, float | None]],
    *,
    cap_db: float,
    rel_lu: float,
    abs_lufs: float,
    slew_db_per_s: float,
    step_s: float,
) -> tuple[list[tuple[float, float]], dict[str, Any]]:
    """Cap the applied gain in the quiet gaps, then rate-limit the capping.

    A block is a *gap* when its emitted momentary -- the source's own momentary
    loudness plus the gain about to be applied -- is below the EBU R128 absolute
    gate, or more than ``rel_lu`` under the gated loudness of its own 45 s
    neighbourhood.  That is the between-speakers case: room tone, a page turn,
    the pause before the chair recognises the next speaker.  In a gap the gain is
    capped at ``cap_db`` regardless of how far below target the passage measures,
    because lifting room tone to target is how a room gets loud.

    The cap is then passed through the same causal slew limit as everything else,
    so leaving a gap is a 0.5 dB/s ramp rather than a step.  It is deliberately
    *not* applied to non-gap points: a quiet passage that is genuinely being
    spoken is exactly what the ride is for.
    """
    if not curve:
        return [], {"n_points": 0}
    capped: list[float] = []
    n_gap = n_by_abs = n_by_rel = n_capped = 0
    depth = 0.0
    for (t, g), (_, lw) in zip(curve, levels, strict=False):
        m = _momentary_at(blocks, t)
        emitted = m + g
        is_abs = m <= abs_lufs or emitted <= abs_lufs
        is_rel = lw is not None and emitted < lw - rel_lu
        if is_abs or is_rel or lw is None:
            n_gap += 1
            n_by_abs += int(is_abs)
            n_by_rel += int(is_rel and not is_abs)
            if g > cap_db:
                n_capped += 1
                depth = max(depth, g - cap_db)
                capped.append(cap_db)
                continue
        capped.append(g)
    out = slew_limit(
        [(t, v) for (t, _g), v in zip(curve, capped, strict=True)], slew_db_per_s, step_s
    )
    steps = [abs(out[i][1] - out[i - 1][1]) for i in range(1, len(out))]
    return out, {
        "n_points": len(out),
        "n_gap_points": n_gap,
        "n_gap_by_abs_gate": n_by_abs,
        "n_gap_by_relative": n_by_rel,
        "n_gap_windows_silent": sum(1 for _, lw in levels if lw is None),
        "n_gap_points_capped": n_capped,
        "cap_depth_max_db": round(depth, 3),
        "cap_db": cap_db,
        "curve_min_db": round(min(v for _, v in out), 3),
        "curve_max_db": round(max(v for _, v in out), 3),
        "post_cap_max_step_db": round(max(steps, default=0.0), 4),
        "post_cap_max_rate_db_per_s": round(max(steps, default=0.0) / step_s, 4),
    }


def correction_curve(
    blocks: list[tuple[float, float]],
    *,
    target_lufs: float,
    bound_db: float,
    window_s: float,
    slew_db_per_s: float,
    step_s: float,
) -> tuple[list[tuple[float, float]], list[tuple[float, float | None]]]:
    """``c(t) = target - L_W(t)`` of a rendered output, same window/slew/hold."""
    levels = sliding_levels(blocks, window_s=window_s, step_s=step_s)
    return gain_curve(
        levels,
        target_lufs=target_lufs,
        g_min_db=-bound_db,
        g_max_db=bound_db,
        slew_db_per_s=slew_db_per_s,
        step_s=step_s,
    ), levels


def combine(
    base: list[tuple[float, float]],
    corr: list[tuple[float, float]],
    *,
    g_min_db: float,
    g_max_db: float,
    slew_db_per_s: float | None = None,
    step_s: float = 1.0,
) -> tuple[list[tuple[float, float]], dict[str, Any]]:
    """Sum two curves on the base grid, clamp the sum, optionally re-slew it.

    Both inputs already carry the slew limit, so the default recipe is
    add -> clamp.  ``slew_db_per_s`` re-applies the limit to the sum as well
    (U25 Q10); it is off by default because the shipped numbers were measured
    without it -- see :attr:`RideParams.slew_summed_curve`.
    """
    if not corr:
        return base, {"n_points": len(base), "n_corr_points": 0}
    corr_vals = [interp(corr, t) for t, _ in base]
    summed = [(t, g + c) for (t, g), c in zip(base, corr_vals, strict=True)]
    out = [(t, min(max(v, g_min_db), g_max_db)) for t, v in summed]
    if slew_db_per_s is not None:
        out = slew_limit(out, slew_db_per_s, step_s)
    steps = [abs(out[i][1] - out[i - 1][1]) for i in range(1, len(out))]
    return out, {
        "n_points": len(out),
        "n_corr_points": len(corr),
        "sum_min_db": round(min((v for _, v in summed), default=0.0), 3),
        "sum_max_db": round(max((v for _, v in summed), default=0.0), 3),
        "clamped_below_g_min": sum(1 for _, v in summed if v < g_min_db - 1e-9),
        "clamped_above_g_max": sum(1 for _, v in summed if v > g_max_db + 1e-9),
        "sum_max_step_db": round(max(steps, default=0.0), 4),
        "sum_max_rate_db_per_s": round(max(steps, default=0.0) / step_s, 4),
        "re_slewed": slew_db_per_s is not None,
        "corr_min_db": round(min((v for _, v in corr), default=0.0), 3),
        "corr_max_db": round(max((v for _, v in corr), default=0.0), 3),
    }


def interp(curve: list[tuple[float, float]], t: float) -> float:
    """Linear interpolation, flat outside the ends."""
    if not curve:
        return 0.0
    if t <= curve[0][0]:
        return curve[0][1]
    if t >= curve[-1][0]:
        return curve[-1][1]
    lo, hi = 0, len(curve) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if curve[mid][0] <= t:
            lo = mid
        else:
            hi = mid
    t0, v0 = curve[lo]
    t1, v1 = curve[hi]
    return v0 if t1 <= t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def curve_stats(
    curve: list[tuple[float, float]], tag: str, g_min_db: float, g_max_db: float
) -> dict[str, Any]:
    """Range/rate summary of one applied curve, for the diagnostics log."""
    gains = [g for _, g in curve]
    if not gains:
        return {"tag": tag, "n_points": 0}
    steps = [abs(gains[i] - gains[i - 1]) for i in range(1, len(gains))]
    return {
        "tag": tag,
        "n_points": len(gains),
        "curve_min_db": round(min(gains), 3),
        "curve_max_db": round(max(gains), 3),
        "curve_mean_db": round(sum(gains) / len(gains), 3),
        "hits_g_max": sum(1 for g in gains if g >= g_max_db - 1e-9),
        "hits_g_min": sum(1 for g in gains if g <= g_min_db + 1e-9),
        "max_step_db": round(max(steps, default=0.0), 4),
        "max_rate_db_per_s": round(max(steps, default=0.0), 4),
    }


def to_arrays(curve: list[tuple[float, float]]) -> tuple[list[float], list[float]]:
    """Split a curve into its time and gain sequences (the numpy multiply's input)."""
    return [t for t, _ in curve], [g for _, g in curve]


# ---------------------------------------------------------------------------
# FFmpeg grammar.  Pure builders -- no process, no filesystem.
# ---------------------------------------------------------------------------


def limit_value(limit_dbtp: float) -> float:
    """``alimiter``'s ``limit`` is linear amplitude, not dB."""
    return float(10.0 ** (limit_dbtp / 20.0))


def resample_filter(target_hz: int, params: RideParams) -> str:
    """One ``aresample`` leg of the oversampled output stage.

    soxr at ``precision=28`` is what the shipped build carries (the station's
    bundled FFmpeg is resolved by ``civiccast.stream._ffmpeg``); ``precision`` is
    a soxr option, so it is only emitted when a resampler is named.
    """
    if not params.resampler:
        return f"aresample={target_hz}"
    return (
        f"aresample={target_hz}:resampler={params.resampler}:precision={params.resampler_precision}"
    )


def limiter_filter(params: RideParams) -> str:
    """The true-peak-aware output stage: 4x up, limit, 4x down.

    ``alimiter`` is a sample-peak limiter -- it cannot see the inter-sample peaks
    a codec will later produce, which is why a sample-peak ceiling let BIG emit
    +2.754 dBFS samples (U25 Q14).  Oversampling puts those peaks *between* the
    samples the limiter sees, so the ceiling it enforces is a true-peak ceiling.

    ``asc=0`` keeps the lookahead off and ``level=0`` is what stops it
    normalizing the trimmed signal back up; both are load-bearing.
    """
    return ",".join(
        [
            resample_filter(params.sample_rate * params.oversample_factor, params),
            (
                f"alimiter=limit={limit_value(params.limit_dbtp):.6f}"
                f":attack={params.limiter_attack_ms:g}"
                f":release={params.limiter_release_ms:g}:asc=0:level=0"
            ),
            resample_filter(params.sample_rate, params),
        ]
    )


def build_source_series_args(
    *,
    source_path: Path,
    segment: EgressSourceSegment | None,
    params: RideParams,
    threads: int | None = None,
) -> list[str]:
    """One pass over the SOURCE window: per-100 ms momentary loudness, nothing else.

    This is where the base curve's levels and the gap cap's blocks come from.
    ``-ss``/``-t`` mirror ``build_conform_source_args``: a trimmed segment
    analyzes exactly its own window, ``segment=None`` analyzes the whole asset.
    """
    args = ["-hide_banner", "-loglevel", "info"]
    if threads is not None:
        args.extend(["-threads", str(threads)])
    if segment is not None and segment.inpoint_seconds is not None:
        args.extend(["-ss", f"{segment.inpoint_seconds:g}"])
    args.extend(["-i", str(source_path)])
    if segment is not None:
        args.extend(["-t", f"{segment.duration_seconds:g}"])
    args.extend(["-vn", "-af", "ebur128=peak=true", "-f", "null", "-"])
    return args


def build_decoder_args(
    *,
    source_path: Path,
    segment: EgressSourceSegment | None,
    params: RideParams,
    threads: int | None = None,
) -> list[str]:
    """Decode the segment's audio at the conformed rate, as raw float32.

    Output goes to this process's stdout; the ride feeds it, one chunk at a
    time, into the sink's stdin.  ``-vn`` is what keeps the pipe PCM-only -- the
    video is decoded once, later, by the video pass.
    """
    args = ["-hide_banner", "-loglevel", "error"]
    if threads is not None:
        args.extend(["-threads", str(threads)])
    if segment is not None and segment.inpoint_seconds is not None:
        args.extend(["-ss", f"{segment.inpoint_seconds:g}"])
    args.extend(["-i", str(source_path)])
    if segment is not None:
        args.extend(["-t", f"{segment.duration_seconds:g}"])
    args.extend(
        [
            "-vn",
            "-ar",
            str(params.sample_rate),
            "-ac",
            str(params.channels),
            "-f",
            "f32le",
            "-",
        ]
    )
    return args


def _pcm_input_args(*, params: RideParams, from_file: Path | None = None) -> list[str]:
    """The raw-float32 input leg, from ``-f f32le`` through the ``-i`` operand.

    ``from_file=None`` is the pipe a ride feeds (both live sinks); a path is bytes
    a previous pass already wrote -- the guard's re-encode, which must see the
    very samples the nominal emit consumed.  Every f32le reader in this module
    builds its leg here, so the three cannot drift in rate or channel count; a
    drift there would silently reshape the PCM rather than fail.
    """
    return [
        "-f",
        "f32le",
        "-ar",
        str(params.sample_rate),
        "-ac",
        str(params.channels),
        "-i",
        "pipe:0" if from_file is None else str(from_file),
    ]


def _encode_sink_tail(
    output_path: Path,
    trim_db: float,
    *,
    params: RideParams,
    profile: CanonicalProfile,
    variant: EncoderVariant | None = None,
) -> list[str]:
    """Everything the emitted-audio sink does after its input, in one place.

    Factored so the guard's re-encode round IS this encode, option for option: a
    change to the codec, bitrate, rate, channel count or container reaches both
    shapes or neither.  ``variant`` is the guard's *second* lever (U42): it may
    move the bitrate and append the encoder's own options, and nothing else --
    the filter chain, the sample rate, the channel count and the container are
    the profile's, not a variant's.
    """
    parts = [f"volume={trim_db:.3f}dB", limiter_filter(params)]
    bitrate_kbps = profile.audio_bitrate_kbps if variant is None else variant.bitrate_kbps
    encoder_args = [] if variant is None else list(variant.extra_args)
    return [
        "-af",
        ",".join(parts),
        "-c:a",
        profile.audio_codec,
        "-b:a",
        f"{bitrate_kbps}k",
        *encoder_args,
        "-ar",
        str(profile.audio_sample_rate),
        "-ac",
        str(profile.audio_channels),
        "-f",
        "mpegts",
        str(output_path),
    ]


def build_measure_sink_args(trim_db: float | None, *, params: RideParams) -> list[str]:
    """Sink that measures the ridden PCM: trim, limiter, ebur128, discard.

    ``trim_db=None`` is the ride-only pass: no trim and no limiter, which is how
    the level the ride alone produced is measured before a trim is chosen.
    """
    if trim_db is None:
        parts = ["ebur128=peak=true"]
    else:
        parts = [f"volume={trim_db:.3f}dB", limiter_filter(params), "ebur128=peak=true"]
    return [
        "-hide_banner",
        "-loglevel",
        "info",
        *_pcm_input_args(params=params),
        "-af",
        ",".join(parts),
        "-f",
        "null",
        "-",
    ]


def build_encode_sink_args(
    output_path: Path,
    trim_db: float,
    *,
    params: RideParams,
    profile: CanonicalProfile,
) -> list[str]:
    """Sink that writes the real emitted audio: the profile's codec, in MPEG-TS.

    The audio parameters come from the canonical profile, because the video pass
    later stream-copies this stream into the final program: whatever bitrate,
    rate or channel count is written here IS what the program carries, and
    re-encoding it there would be a second lossy generation.  This is the nominal
    emit and takes no variant -- a guard round is a *re-encode*, not a second
    nominal.
    """
    return [
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        *_pcm_input_args(params=params),
        *_encode_sink_tail(output_path, trim_db, params=params, profile=profile),
    ]


def build_reencode_args(
    pcm_path: Path,
    output_path: Path,
    *,
    trim_db: float,
    params: RideParams,
    profile: CanonicalProfile,
    variant: EncoderVariant | None = None,
) -> list[str]:
    """The guard's re-encode round: the emitted-audio sink, fed from a PCM file.

    Answer 11's guard round is not a re-convergence -- the ride PCM is already
    correct -- so it is the nominal encode with a file for an input and nothing
    else different.  ``params.limit_dbtp`` is the ceiling this round pulls the
    limiter lever to (:func:`limiter_filter`); ``variant`` is the *other* lever
    U42 added, and it moves only the audio encoder (:func:`_encode_sink_tail`).
    A caller passes one or the other: a round that lowers the ceiling and swaps
    the encoder at once would leave the keep-best selector unable to say which
    of the two bought the result.
    """
    return [
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        *_pcm_input_args(params=params, from_file=pcm_path),
        *_encode_sink_tail(output_path, trim_db, params=params, profile=profile, variant=variant),
    ]


def build_artifact_measure_args(*, artifact_path: Path) -> list[str]:
    """Measure the emitted ARTIFACT, whole: ebur128 series, peak, integrated.

    The guard's input is what airs, so this pass reads the file the encode sink
    wrote rather than the PCM it was fed -- a codec round trip is exactly the
    part the guard exists to catch.  Seek-free on purpose (no ``-ss``/``-t``):
    the window tiling is a function of the whole series, and a partial read
    would silently shrink the program the gate measures.
    """
    return [
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        str(artifact_path),
        "-vn",
        "-af",
        "ebur128=peak=true",
        "-f",
        "null",
        "-",
    ]


def build_peak_scan_args(*, artifact_path: Path, params: RideParams) -> list[str]:
    """Decode the artifact to raw float32 so the hottest sample can be found.

    The hard gate is a *sample*-peak bound on a decode, not a promise: the
    limiter enforces a true-peak ceiling on what it sees, and the codec's own
    overshoot lands between the samples after it.  Only a decode sees those.
    """
    return [
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(artifact_path),
        "-vn",
        "-f",
        "f32le",
        "-ar",
        str(params.sample_rate),
        "-ac",
        str(params.channels),
        "-",
    ]


def canonical_video_filter(profile: CanonicalProfile) -> str:
    """The canonical scale/pad/rate/format chain, in one place.

    Shared with ``preparer.build_conform_source_args`` so the two conform shapes
    cannot drift apart: the video pass below must produce exactly the frames the
    single-pass conform produced.
    """
    return (
        f"scale={profile.width}:{profile.height}:force_original_aspect_ratio=decrease,"
        f"pad={profile.width}:{profile.height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={profile.fps},format=yuv420p"
    )


def build_video_from_source_args(
    *,
    source_path: Path,
    audio_path: Path,
    output_path: Path,
    segment: EgressSourceSegment | None,
    profile: CanonicalProfile,
    threads: int | None = None,
    copy_video: bool = False,
) -> list[str]:
    """Final pass: source video copied when compatible, otherwise re-encoded.

    The video is taken from the original file (input 0) and the ride's audio
    from the ride's own MPEG-TS (input 1), so the emitted program has exactly one
    encode of each stream (or zero video encodes after compatibility approval).
    ``-c:a copy`` is what makes the ride's work survive
    into the program untouched -- any audio filter here would fight the ride.

    No ``-b:a``/``-ar``/``-ac`` may be emitted alongside ``-c:a copy``: they are
    input-format options for an encoder that is not running, and FFmpeg treats
    them as a contradiction.  The stream's parameters were fixed by the ride's
    encode sink from the same profile.

    ``-t`` is an output option and therefore has to follow the LAST ``-i``:
    between the two ``-i`` operands FFmpeg binds it to the input that follows --
    the ride's own audio, which is already exactly the window's length -- so the
    bound silently stops applying to the source.  Measured on a 4 s window of an
    8 s asset, that placement emitted 7 s of video against 3.97 s of audio,
    where ``preparer.build_conform_source_args``'s single-input ``-t`` produced
    4 s.  ``-ss`` stays ahead of the FIRST ``-i``, as it is in
    ``preparer.build_conform_source_args``: that one is an input option on
    purpose.
    """
    args = ["-hide_banner", "-loglevel", "warning"]
    if segment is not None and segment.inpoint_seconds is not None:
        args.extend(["-ss", f"{segment.inpoint_seconds:g}"])
    args.extend(["-i", str(source_path)])
    args.extend(["-i", str(audio_path)])
    if segment is not None:
        args.extend(["-t", f"{segment.duration_seconds:g}"])
    args.extend(["-map", "0:v:0", "-map", "1:a:0"])
    if not copy_video:
        args.extend(["-vf", canonical_video_filter(profile)])
    if threads is not None:
        args.extend(["-threads", str(threads)])
    if copy_video:
        args.extend(["-c:v", "copy"])
    else:
        args.extend(
            [
                "-c:v",
                profile.video_codec,
                "-b:v",
                f"{profile.video_bitrate_kbps}k",
                "-g",
                str(profile.gop_size),
            ]
        )
    args.extend(
        [
            "-c:a",
            "copy",
            "-f",
            profile.container,
            str(output_path),
        ]
    )
    return args


def ride_audio_path(final_path: Path) -> Path:
    """Where the ride's intermediate audio lives: beside its final artifact.

    Same directory as the output on purpose -- a temp on another volume turns
    the promotion's ``rename`` into a copy, and the cache directory is the one
    place the conform is allowed to write.
    """
    return final_path.with_name(final_path.name + ".ride-audio.ts")


# ---------------------------------------------------------------------------
# Parsers for ebur128's own output.
# ---------------------------------------------------------------------------


def parse_integrated_lufs(stderr: str) -> float | None:
    """The summary ``I:`` line ebur128 prints at the end of a pass.

    ``-loglevel info`` prints one ``I:`` per 100 ms *inside* a per-frame line and
    the summary on a line of its own; the anchored pattern picks up only the
    latter.  ``None`` means the pass produced no summary -- the caller treats
    that as a failed measurement.
    """
    matches = _RE_I.findall(stderr)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def parse_ebur128_series(stderr: str) -> list[tuple[float, float]]:
    """Per-100 ms ``(t, momentary)`` pairs; digital silence becomes a sentinel."""
    out: list[tuple[float, float]] = []
    for t, m, _s in _RE_SERIES.findall(stderr):
        out.append((float(t), DIGITAL_SILENCE if m in ("nan", "-inf") else float(m)))
    return out


def parse_true_peak_dbtp(stderr: str) -> float | None:
    """The summary ``Peak:`` line ebur128 prints with ``peak=true``.

    The FIRST such line, like the acceptance harness (``RE_TP.search``): a
    stderr carrying more than one pass would otherwise report the last pass's
    peak as if it were this artifact's.  ``None`` means the pass printed none.
    """
    match = _RE_TP.search(stderr)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:  # pragma: no cover - the pattern only admits floats
        return None


# ---------------------------------------------------------------------------
# The acceptance gate, ported.  Same arithmetic the station is scored by.
# ---------------------------------------------------------------------------


def series_duration_s(series: Sequence[tuple[float, float]]) -> float:
    """How far an ebur128 series reaches: the last block's own step included.

    A block at ``t`` covers ``[t, t + 0.1)``, so a 240 s program's last block
    sits at 239.9 s and the series reaches 240.0.  Reading the last ``t`` alone
    would lose that step and, on a program that is exactly one window long,
    take the only window in the program out of the tiling.
    """
    if not series:
        return 0.0
    return series[-1][0] + BLOCK_STEP_S


def window_levels(
    series: Sequence[tuple[float, float]],
    duration_s: float,
    *,
    offset_s: float = 0.0,
) -> list[tuple[float, float | None]]:
    """Every ``LOUDNESS_WINDOW_S`` window of the tiling, as ``(start, level)``.

    The harness's own geometry: windows start at ``offset_s`` and advance by one
    window while the next start is still more than
    :data:`LOUDNESS_WINDOW_MIN_TAIL_S` before the end, each clamped to the
    duration, each read half-open (``start <= t < end``) so a block exactly on a
    boundary belongs to the later window and never to both.  ``level`` is that
    window's gated loudness, or ``None`` when nothing in it cleared the gate.
    """
    out: list[tuple[float, float | None]] = []
    if duration_s <= 0.0:
        return out
    start = offset_s
    while start < duration_s - LOUDNESS_WINDOW_MIN_TAIL_S:
        end = min(start + LOUDNESS_WINDOW_S, duration_s)
        out.append((start, gated_loudness([v for t, v in series if start <= t < end])))
        start += LOUDNESS_WINDOW_S
    return out


def worst_window_deviation_lu(
    series: Sequence[tuple[float, float]],
    duration_s: float,
    *,
    target_lufs: float,
    offset_s: float | None = None,
) -> tuple[float | None, float | None]:
    """The largest gated-window deviation from target, and the window's start.

    Both tilings are searched by default -- the one from zero and the one from
    :data:`LOUDNESS_WINDOW_OFFSET_S` -- because a 4-minute stretch that only one
    of them frames is still four minutes of program the audience hears, and the
    harness judges exactly that union.  ``offset_s`` narrows it to one tiling
    when a caller is reading a single family.

    Windows with no measurable level are skipped rather than scored: an
    unmeasurable window is not evidence of a deviation, and scoring it as one
    would fail a lawful program for a silent passage.  ``(None, None)`` means no
    window was measurable at all.
    """
    offsets = (0.0, LOUDNESS_WINDOW_OFFSET_S) if offset_s is None else (offset_s,)
    worst: float | None = None
    worst_start: float | None = None
    for off in offsets:
        for start, level in window_levels(series, duration_s, offset_s=off):
            if level is None:
                continue
            deviation = abs(level - target_lufs)
            if worst is None or deviation > worst:
                worst = deviation
                worst_start = start
    return worst, worst_start


def whole_program_err_lu(
    series: Sequence[tuple[float, float]],
    *,
    target_lufs: float,
) -> float | None:
    """The signed whole-program error: the gated mean of the series, minus target.

    Signed on purpose -- the caller's diagnostics want to know which way a
    program missed -- and computed off the same blocks the windows are, so the
    two gate legs cannot disagree about what was measured.  ``None`` when
    nothing survived the gate.
    """
    level = gated_loudness([v for _t, v in series])
    if level is None:
        return None
    return level - target_lufs


# ---------------------------------------------------------------------------
# The chunked multiply, between two FFmpeg processes.
# ---------------------------------------------------------------------------


def _load_numpy() -> Any:
    """Import numpy lazily and name the failure in the operator's terms.

    numpy is not a declared dependency of this package (it arrives with the
    native app closure, via ctranslate2/onnxruntime), and this module must not
    make it one: a station without it should keep conforming sources the way it
    did before U25, not fail to import ``civiccast.egress``.
    """
    try:
        return importlib.import_module("numpy")
    except Exception as exc:  # ImportError, or a broken install's anything
        raise LoudnessRideUnavailableError(
            "the U25 speech-leveling ride needs numpy, which is not importable in "
            f"this installation ({type(exc).__name__}: {exc})"
        ) from exc


def _ffmpeg_binary() -> str:
    """Resolve FFmpeg through ``civiccast.stream._ffmpeg``.

    Imported inside the function so this module stays importable (and its pure
    functions testable) on a host with no FFmpeg at all, and so tests that
    monkeypatch ``_ffmpeg._ffmpeg_path`` control the ride's binary too.
    """
    from civiccast.stream import _ffmpeg

    return _ffmpeg._ffmpeg_path()


@dataclass(frozen=True)
class RideRender:
    """What one ride pass produced.  The measurement lives in ``stderr``."""

    frames: int
    audio_seconds: float
    wall_s: float
    stderr: str


def run_ride(
    *,
    decoder_args: list[str],
    sink_args: list[str],
    curve: list[tuple[float, float]],
    params: RideParams,
    pcm_path: Path | None = None,
    cancel_event: threading.Event | None = None,
    timeout_s: float | None = None,
) -> RideRender:
    """Feed decoded PCM through the gain curve into the sink, one chunk at a time.

    The decoder writes float32 PCM to a pipe; each chunk is multiplied by the
    curve interpolated per sample and written to the sink's stdin.  Nothing
    larger than one chunk is ever held, so peak memory does not grow with asset
    length.

    ``pcm_path`` tees those post-curve bytes to a file as they pass -- the same
    chunk object the sink receives, so the copy is bit-identical by construction
    rather than by a second render that would have to reproduce it.  It is
    ``None`` on the nominal path, which then pays nothing; when it is given the
    caller owns the file (this function never deletes it), because its whole
    purpose is to outlive the call and feed :func:`build_reencode_args` if the
    emitted artifact turns out hot.

    The sink's stderr is where the measurement lives, and ebur128 writes a block
    line every 100 ms -- ~10 MB over a 2.5 h asset.  A pipe nobody drains while
    the ride is running would fill and deadlock both processes, so it goes to a
    file and is read back at the end.

    Raises :class:`LoudnessRideCancelledError` on cancellation, and
    :class:`LoudnessRideError` on a timeout or a non-zero exit from either child.
    """
    np = _load_numpy()
    gt, gv = to_arrays(curve)
    ffmpeg = _ffmpeg_binary()
    bytes_per_frame = params.channels * 4
    bytes_per_chunk = params.chunk_frames * bytes_per_frame

    # Opened before either child starts: an unwritable tee is this call's own
    # failure and must not leave a decoder running behind it.
    pcm_file: BinaryIO | None = pcm_path.open("wb") if pcm_path is not None else None

    t_start = time.perf_counter()
    decoder = subprocess.Popen(  # noqa: S603 - explicit list, resolved binary, shell=False
        [ffmpeg, *decoder_args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=_CREATE_NO_WINDOW | _BELOW_NORMAL_PRIORITY_CLASS,
    )
    err_file = tempfile.NamedTemporaryFile(  # noqa: SIM115 - held open for the child
        prefix="civiccast-ride-", suffix=".log", delete=False
    )
    err_path = Path(err_file.name)
    sink: subprocess.Popen[bytes] | None = None
    frames_done = 0
    cancelled = False
    timed_out = False
    try:
        sink = subprocess.Popen(  # noqa: S603 - explicit list, resolved binary, shell=False
            [ffmpeg, *sink_args],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=err_file,
            creationflags=_CREATE_NO_WINDOW | _BELOW_NORMAL_PRIORITY_CLASS,
        )
        assert decoder.stdout is not None
        assert sink.stdin is not None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if timeout_s is not None and time.perf_counter() - t_start > timeout_s:
                timed_out = True
                break
            raw = decoder.stdout.read(bytes_per_chunk)
            if not raw:
                break
            if len(raw) % bytes_per_frame:
                raw = raw[: len(raw) - len(raw) % bytes_per_frame]
            block = np.frombuffer(raw, dtype="<f4").reshape(-1, params.channels).copy()
            n = block.shape[0]
            t = (frames_done + np.arange(n, dtype=np.float64)) / params.sample_rate
            gain = np.interp(t, gt, gv)
            block *= (10.0 ** (gain / 20.0)).astype(np.float32)[:, None]
            payload = block.astype("<f4").tobytes()
            if pcm_file is not None:
                pcm_file.write(payload)
            sink.stdin.write(payload)
            frames_done += n
    except BrokenPipeError:
        # The sink died first (a bad filter, a bad codec, a full disk).  Its
        # exit code below is the real error; the broken pipe is a symptom.
        pass
    finally:
        for stream in (decoder.stdout, sink.stdin if sink is not None else None):
            if stream is not None:
                with contextlib.suppress(BrokenPipeError, OSError):
                    stream.close()
        if pcm_file is not None:
            with contextlib.suppress(OSError):
                pcm_file.close()
        if cancelled or timed_out:
            for proc in (sink, decoder):
                if proc is not None and proc.poll() is None:
                    proc.kill()
        # Drain the decoder's stderr into its own void so a chatty failure
        # cannot leave the pipe full and the wait below hanging.
        if decoder.stderr is not None:
            decoder.stderr.close()
        err_file.close()

    decoder.wait()
    sink_rc = sink.wait() if sink is not None else -1
    try:
        err = err_path.read_text(encoding="utf-8", errors="replace")
    finally:
        err_path.unlink(missing_ok=True)

    if cancelled:
        raise LoudnessRideCancelledError("the speech-leveling ride was cancelled")
    if timed_out:
        raise LoudnessRideError(f"the speech-leveling ride timed out after {timeout_s:g}s")
    if decoder.returncode != 0:
        raise LoudnessRideError(
            f"the ride's source decode exited {decoder.returncode}; "
            f"last output: {err.strip()[-400:]!r}"
        )
    if sink_rc != 0:
        raise LoudnessRideError(
            f"the ride's audio sink exited {sink_rc}; last output: {err.strip()[-400:]!r}"
        )
    return RideRender(
        frames=frames_done,
        audio_seconds=round(frames_done / params.sample_rate, 3),
        wall_s=round(time.perf_counter() - t_start, 2),
        stderr=err,
    )


def run_reencode(
    *,
    pcm_path: Path,
    output_path: Path,
    trim_db: float,
    params: RideParams,
    profile: CanonicalProfile,
    timeout_s: float | None = None,
    variant: EncoderVariant | None = None,
) -> RideRender:
    """Re-encode retained ride PCM once: limiter, codec, mux -- no ride, no curve.

    This is the guard's whole round, as a process.  The samples are the ones the
    nominal emit already consumed, so this pass cannot move the loudness the
    convergence settled on; the only thing it changes is what the limiter and the
    codec do to a hot signal, which is exactly the lever answer 11 asks it to
    pull -- and ``variant`` is the same argument applied to the codec rather than
    the limiter (U42), still one input, one output, no pipe, no chunk loop.
    FFmpeg reads the file itself.

    ``frames`` and ``audio_seconds`` are zero: the caller measures the artifact it
    just wrote (that measurement is the guard's input), and this function reports
    only the wall time and the child's stderr, so the decision stays with the pure
    functions.

    Raises :class:`LoudnessRideError` on a timeout or a non-zero exit.
    """
    ffmpeg = _ffmpeg_binary()
    argv = [
        ffmpeg,
        *build_reencode_args(
            pcm_path,
            output_path,
            trim_db=trim_db,
            params=params,
            profile=profile,
            variant=variant,
        ),
    ]
    t_start = time.perf_counter()
    try:
        proc = subprocess.run(  # noqa: S603 - explicit list, resolved binary, shell=False
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
            creationflags=_CREATE_NO_WINDOW | _BELOW_NORMAL_PRIORITY_CLASS,
        )
    except subprocess.TimeoutExpired as exc:
        raise LoudnessRideError(f"the guard's re-encode timed out after {timeout_s:g}s") from exc
    wall_s = round(time.perf_counter() - t_start, 2)
    stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise LoudnessRideError(
            f"the guard's re-encode exited {proc.returncode}; "
            f"last output: {stderr.strip()[-400:]!r}"
        )
    return RideRender(frames=0, audio_seconds=0.0, wall_s=wall_s, stderr=stderr)


def run_capture(
    args: list[str],
    *,
    timeout_s: float | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    """Run one read-only FFmpeg pass and return its stderr.

    The pass writes nothing -- it is a measurement (``build_source_series_args``,
    :func:`build_artifact_measure_args`) -- so the only output is what it says on
    stderr, and that goes to a file rather than a pipe: ebur128 prints a block
    line every 100 ms, ~10 MB over a 2.5 h program, and a pipe nobody drains
    until the end would fill and deadlock the child mid-measurement.

    Raises :class:`LoudnessRideCancelledError` when the cancel event is set, and
    :class:`LoudnessRideError` on a timeout or a non-zero exit.  The caller
    decides what a failure means; this function only reports it.
    """
    ffmpeg = _ffmpeg_binary()
    err_file = tempfile.NamedTemporaryFile(  # noqa: SIM115 - held open for the child
        prefix="civiccast-measure-", suffix=".log", delete=False
    )
    err_path = Path(err_file.name)
    t_start = time.perf_counter()
    cancelled = False
    timed_out = False
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(  # noqa: S603 - explicit list, resolved binary, shell=False
            [ffmpeg, *args],
            stdout=subprocess.DEVNULL,
            stderr=err_file,
            creationflags=_CREATE_NO_WINDOW | _BELOW_NORMAL_PRIORITY_CLASS,
        )
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if timeout_s is not None and time.perf_counter() - t_start > timeout_s:
                timed_out = True
                break
            time.sleep(0.05)
        if cancelled or timed_out:
            with contextlib.suppress(OSError):
                proc.kill()
        proc.wait()
    finally:
        err_file.close()
    try:
        err = err_path.read_text(encoding="utf-8", errors="replace")
    finally:
        err_path.unlink(missing_ok=True)

    if cancelled:
        raise LoudnessRideCancelledError("the speech-leveling measurement was cancelled")
    if timed_out:
        raise LoudnessRideError(f"the measurement pass timed out after {timeout_s:g}s")
    assert proc is not None
    if proc.returncode != 0:
        raise LoudnessRideError(
            f"the measurement pass exited {proc.returncode}; last output: {err.strip()[-400:]!r}"
        )
    return err


def scan_peak_dbfs(
    artifact_path: Path,
    *,
    params: RideParams,
    timeout_s: float | None = None,
    cancel_event: threading.Event | None = None,
) -> float | None:
    """The hottest sample a decode of the artifact finds, in dBFS.

    The hard gate's only evidence: the artifact is decoded to raw float32 and
    the largest absolute sample is read off, so what is measured is what the
    codec actually produced rather than what the limiter promised.  ``None``
    means the decode held nothing to measure (a stream with no audio, or
    digital silence), which -- like a failed decode -- cannot clear the gate.

    Raises :class:`LoudnessRideCancelledError` on cancellation and
    :class:`LoudnessRideError` on a timeout or a non-zero exit.
    """
    np = _load_numpy()
    ffmpeg = _ffmpeg_binary()
    bytes_per_frame = params.channels * 4
    t_start = time.perf_counter()
    peak = 0.0
    cancelled = False
    timed_out = False
    carry = b""
    proc = subprocess.Popen(  # noqa: S603 - explicit list, resolved binary, shell=False
        [ffmpeg, *build_peak_scan_args(artifact_path=artifact_path, params=params)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=_CREATE_NO_WINDOW | _BELOW_NORMAL_PRIORITY_CLASS,
    )
    try:
        assert proc.stdout is not None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if timeout_s is not None and time.perf_counter() - t_start > timeout_s:
                timed_out = True
                break
            raw = proc.stdout.read(CHUNK_FRAMES * bytes_per_frame)
            if not raw:
                break
            # A pipe read can land mid-sample; the remainder is carried into the
            # next read so ``frombuffer`` never sees a truncated float.
            chunk = carry + raw
            usable = len(chunk) - len(chunk) % 4
            carry = chunk[usable:]
            if usable:
                samples = np.frombuffer(chunk[:usable], dtype="<f4")
                peak = max(peak, float(np.max(np.abs(samples))))
    finally:
        if proc.stdout is not None:
            with contextlib.suppress(OSError):
                proc.stdout.close()
        if cancelled or timed_out:
            with contextlib.suppress(OSError):
                proc.kill()
    rc = proc.wait()

    if cancelled:
        raise LoudnessRideCancelledError("the artifact's true-peak scan was cancelled")
    if timed_out:
        raise LoudnessRideError(f"the artifact's true-peak scan timed out after {timeout_s:g}s")
    if rc != 0:
        raise LoudnessRideError(f"the artifact's decode exited {rc}")
    if peak <= 0.0:
        return None
    return 20.0 * math.log10(peak)


class ReencodeRunner(Protocol):
    """How the guard's re-encode rounds run (the installed station:
    :func:`run_reencode`).

    Takes no ``cancel_event``: each round is one short FFmpeg pass over a file,
    so it is bounded by its input rather than by a stream, and the round's
    timeout is its whole cancellation story.

    The guard may run more than one round -- one pad round, then up to one
    round per :data:`TP_GUARD_ENCODER_VARIANTS` entry (U42) -- and every round
    goes through this one callable, so the module has exactly one place that
    spends an encode on a fix.
    """

    def __call__(
        self,
        *,
        pcm_path: Path,
        output_path: Path,
        trim_db: float,
        params: RideParams,
        profile: CanonicalProfile,
        timeout_s: float | None = None,
        variant: EncoderVariant | None = None,
    ) -> RideRender: ...


# ---------------------------------------------------------------------------
# The converge loop.  Takes a render callable, so the loop itself is pure.
# ---------------------------------------------------------------------------

#: ``render(curve, trim_db)`` -> one ride pass.  ``trim_db=None`` is the
#: ride-only pass (no trim, no limiter).
RenderFn = Callable[[list[tuple[float, float]], "float | None"], RideRender]


class RideRunner(Protocol):
    """How the preparer runs one ride pass.

    Declared so the preparer can take a substitute runner (tests substitute a
    canned stderr; the installed station takes :func:`run_ride`) without losing
    the signature -- the same shape ``FfmpegRunner`` gives ``run_ffmpeg``.
    """

    def __call__(
        self,
        *,
        decoder_args: list[str],
        sink_args: list[str],
        curve: list[tuple[float, float]],
        params: RideParams,
        pcm_path: Path | None = None,
        cancel_event: threading.Event | None = None,
        timeout_s: float | None = None,
    ) -> RideRender: ...


@dataclass(frozen=True)
class RideCurve:
    """The converged ride: what to apply, and the evidence for why."""

    curve: list[tuple[float, float]]
    trim_db: float
    iterations: int
    converged: bool
    stop_reason: str
    diagnostics: list[dict[str, Any]]


def converge(
    params: RideParams,
    *,
    source_blocks: list[tuple[float, float]],
    source_levels: list[tuple[float, float | None]],
    render: RenderFn,
) -> RideCurve:
    """Iterate the ride until the emitted whole-program level is at target.

    ``source_blocks``/``source_levels`` are this window's own ebur128 series and
    window levels (see :func:`build_source_series_args`); ``render`` runs one
    ride pass.  Returns the applied curve and the trim to emit with, plus one
    diagnostics record per iteration.

    Raises :class:`LoudnessRideError` when the ride-only pass measures no
    integrated loudness at all -- a window the ride cannot level, whose caller
    must fall back rather than emit whatever the curve happened to be.
    """
    base = gain_curve(
        source_levels,
        target_lufs=params.target_lufs,
        g_min_db=params.g_min_db,
        g_max_db=params.g_max_db,
        slew_db_per_s=params.slew_db_per_s,
        step_s=params.step_s,
    )
    applied, cap_diag = gap_cap(
        base,
        source_blocks,
        source_levels,
        cap_db=params.gap_cap_db,
        rel_lu=params.gap_rel_lu,
        abs_lufs=params.gap_abs_lufs,
        slew_db_per_s=params.slew_db_per_s,
        step_s=params.step_s,
    )
    diagnostics: list[dict[str, Any]] = [
        {"stage": "base", **curve_stats(base, "base", params.g_min_db, params.g_max_db)},
        {"stage": "base+cap", **cap_diag},
    ]

    trim0 = 0.0
    trim1 = 0.0
    slope: float | None = None
    prev_err: float | None = None
    converged = False
    stop_reason = f"stopped at max_iters={params.max_iters}"

    for iteration in range(1, params.max_iters + 1):
        ride_only = render(applied, None)
        level_a = parse_integrated_lufs(ride_only.stderr)
        if level_a is None:
            raise LoudnessRideError(
                f"iteration {iteration}: the ride-only pass measured no integrated loudness"
            )
        trim0 = params.target_lufs - level_a

        with_trim0 = render(applied, trim0)
        level_b = parse_integrated_lufs(with_trim0.stderr)
        err_b = (params.target_lufs - level_b) if level_b is not None else 0.0
        slope_used = 1.0 if slope is None else max(slope, params.slope_floor)
        trim1 = trim0 + err_b / slope_used

        with_trim1 = render(applied, trim1)
        level_c = parse_integrated_lufs(with_trim1.stderr)
        err_c = None if level_c is None else level_c - params.target_lufs
        measured_slope: float | None = None
        if level_b is not None and level_c is not None and abs(trim1 - trim0) > 1e-6:
            measured_slope = (level_c - level_b) / (trim1 - trim0)
            slope = measured_slope

        diagnostics.append(
            {
                "stage": f"iter{iteration}",
                "iteration": iteration,
                "level_ride_only": level_a,
                "trim0_db": round(trim0, 4),
                "level_trim0": level_b,
                "slope_used": round(slope_used, 4),
                "slope_measured": None if measured_slope is None else round(measured_slope, 4),
                "trim1_db": round(trim1, 4),
                "level_trim1": level_c,
                "error_db": None if err_c is None else round(err_c, 4),
                "ride_wall_s": [ride_only.wall_s, with_trim0.wall_s, with_trim1.wall_s],
                "curve": curve_stats(applied, f"iter{iteration}", params.g_min_db, params.g_max_db),
            }
        )

        if err_c is not None and abs(err_c) <= params.stop_lu:
            converged = True
            stop_reason = f"whole-program error <= {params.stop_lu:g} LU"
            break
        if (
            prev_err is not None
            and err_c is not None
            and abs(err_c) >= abs(prev_err) - params.min_improvement_lu
        ):
            stop_reason = "no improvement between iterations"
            break
        prev_err = err_c
        if iteration >= params.max_iters:
            break

        correction, _ = correction_curve(
            parse_ebur128_series(with_trim1.stderr),
            target_lufs=params.target_lufs,
            bound_db=params.correction_bound_db,
            window_s=params.window_s,
            slew_db_per_s=params.slew_db_per_s,
            step_s=params.step_s,
        )
        applied, combine_diag = combine(
            applied,
            correction,
            g_min_db=params.g_min_db,
            g_max_db=params.g_max_db,
            slew_db_per_s=params.slew_db_per_s if params.slew_summed_curve else None,
            step_s=params.step_s,
        )
        applied, _ = gap_cap(
            applied,
            source_blocks,
            source_levels,
            cap_db=params.gap_cap_db,
            rel_lu=params.gap_rel_lu,
            abs_lufs=params.gap_abs_lufs,
            slew_db_per_s=params.slew_db_per_s,
            step_s=params.step_s,
        )
        diagnostics[-1]["combine"] = combine_diag

    diagnostics.append(
        {
            "stage": "done",
            "iterations": len([d for d in diagnostics if d.get("stage", "").startswith("iter")]),
            "converged": converged,
            "stop_reason": stop_reason,
            "final_trim_db": round(trim1, 4),
            "curve": curve_stats(applied, "final", params.g_min_db, params.g_max_db),
        }
    )
    return RideCurve(
        curve=applied,
        trim_db=trim1,
        iterations=len([d for d in diagnostics if str(d.get("stage", "")).startswith("iter")]),
        converged=converged,
        stop_reason=stop_reason,
        diagnostics=diagnostics,
    )


# ---------------------------------------------------------------------------
# The emitted-artifact guard: the artifact gets the last word on its own TP.
# ---------------------------------------------------------------------------

#: The emitted true peak the artifact must not exceed (dBTP).
TP_GUARD_TARGET_DBTP = -1.0


# ---------------------------------------------------------------------------
# The guard (answer 12: keep-best and a measured bound; U43: the pad round)
# ---------------------------------------------------------------------------

#: The hard true-peak gate: no decoded sample above this.  It is a bound measured
#: on the artifact rather than promised by the limiter, because the AAC round
#: trip's overshoot is the codec's own -- U25 round 7 measured the limiter holding
#: to +0.0034 dB over its ceiling while the emit added up to +3.27 dB.  +0.1 dBFS
#: is what the product can honestly guarantee: BIG's nominal emit, the hottest
#: artifact the U25 panel produced, sat at +0.0036 dBFS on one sample in 9000 s,
#: a codec overshoot under the audibility floor and far better than the incumbent
#: two-pass conform's +2.8 dBFS on the same asset.  An artifact still over the
#: bound when every lever is spent is *reported* rather than re-emitted again:
#: :data:`TP_GUARD_TARGET_DBTP` is the best effort, and the caller airs the best
#: attempt it has with an error line.
TP_GUARD_MAX_PEAK_DBFS = 0.1

#: Headroom the pad round leaves under that bound.  Not slack in the contract --
#: the artifact still has to measure at or below the bound -- it is room for the
#: *next* decode, whose resampler is not the one the guard measured with: the
#: bound is measured on one decode of the artifact, and the station decodes it
#: again to air it.
TP_GUARD_PAD_MARGIN_DB = 0.5

#: The most ceiling a single pad round may take.  A measured need larger than
#: this is not a peak problem the round can fix -- the overshoot is the codec's,
#: and below this the drive is doing the work, not the ceiling.  The round pads
#: by the cap and lets keep-best and the caller's ERROR line report what is left,
#: rather than spending a second round chasing a fixed point (U25 round 7
#: measured a second ceiling drop buying 0.00 dB).
TP_GUARD_MAX_PAD_DB = 6.0


def guard_pad_db(
    attempt: LeveledAttempt,
    *,
    max_pad_db: float = TP_GUARD_MAX_PAD_DB,
) -> float | None:
    """How far the one pad round moves, or ``None`` when no round is owed.

    The round exists for the hard bound, so it is measured on the hard bound's
    own evidence: the *decoded sample peak* of the artifact, which is what
    :data:`TP_GUARD_MAX_PEAK_DBFS` is a bound on.  That is the U43 correction --
    answer 11 measured the emitted *true* peak instead, which the AAC round trip
    inflates well past the sample peak the bound actually names, so its round
    paid 2.6 dB of ceiling for a 1.2 dB overshoot on U43's hermetic cell and
    missed the bound entirely where the artifact was live (U42's log line 18797).

    The pad is the measured overshoot plus :data:`TP_GUARD_PAD_MARGIN_DB`, capped
    at ``max_pad_db``.  The caller spends it on both axes at once -- ceiling down
    by the pad, drive up by the pad -- so the level the round takes out of the
    ceiling is handed back to the programme rather than taken from it.
    A ``None`` peak is not evidence of an overshoot, so an unscanned attempt owes
    no round: the guard does not spend an encode on a measurement that never ran.
    """
    if not attempt.over_hard_bound():
        return None
    assert attempt.decoded_peak_dbfs is not None  # over_hard_bound() implies it
    need = attempt.decoded_peak_dbfs - TP_GUARD_MAX_PEAK_DBFS + TP_GUARD_PAD_MARGIN_DB
    return round(min(need, max_pad_db), 3)


def guard_pad_trim_db(
    level_lufs: float | None,
    *,
    trim_db: float,
    target_lufs: float,
) -> float | None:
    """The drive the pad round emits with, from the pass that measured it.

    The pad hands the drive back exactly what the ceiling took, which is the
    right arithmetic for the *bound* and the wrong arithmetic for the *level*:
    a lowered ceiling makes the limiter work harder, so it removes less loudness
    than the pad returns and the round lands loud -- U43's sweep measured +0.53 LU
    on cell A and -0.28 LU on cell B, so the residue is neither small nor even
    fixed in sign.  The round therefore measures before it spends: the caller
    runs one measured pass at the pad's own (ceiling, drive), hands this function
    that pass's integrated loudness, and emits at the drive it returns.

    This is :func:`converge`'s own correction -- the pre-limiter drive is moved
    by the whole-program error it just measured -- spent once rather than
    iterated, because the guard owes one round and not a fixed point.  The level
    read here is the same printed integrated value ``converge`` reads.

    ``level_lufs`` is ``None`` when the pass measured no integrated loudness at
    all.  There is then no error to correct with, but the round is still owed for
    the hard bound, so the caller emits the uncorrected pad rather than skipping
    the round or raising: an unreachable bound is the worse failure, and the
    attempt this round started from is still in the keep-best field.
    """
    if level_lufs is None:
        return None
    return round(trim_db + (target_lufs - level_lufs), 3)


@dataclass(frozen=True)
class EncoderVariant:
    """One encoder configuration the guard may re-emit an artifact with.

    The guard's first lever is the limiter ceiling -- :func:`build_reencode_args`
    at a lower ``params.limit_dbtp``, with the drive raised by the same amount so
    the level holds (U43's pad round; :func:`guard_pad_db`).  U42's live
    full-asset artifact showed that the ceiling alone does not reach the emitted
    peak: it is the AAC round trip's own, so lowering the ceiling without
    compensating moved the loudness gate off target while leaving the peak hot
    (log line 18797: round 0 hot at the nominal ceiling, round 1 quiet *and*
    still over the bound).  This is the second lever -- the same PCM, the same
    limiter, the same mux, a different encoder.

    ``extra_args`` are the encoder's own options, appended verbatim beside
    ``-c:a``.  A variant therefore cannot move the input, the filter chain, the
    rate, the channels or the container; it may only say more about the encoder.
    """

    bitrate_kbps: int
    extra_args: tuple[str, ...] = ()


#: The encoder variants the guard spends, in order, when the attempt it would
#: keep is measurably over the hard bound.  Both rows are measured on the live
#: defect's own already-limited PCM (``evidence/u42-aac-overshoot/out-lever-table.txt``,
#: one input, one -1.50 dBTP ceiling, only the encoder options differing): the
#: nominal 192 kbps emit peaked at +0.783 dBFS, 256 kbps at -1.064 dBFS and
#: 192 kbps with the fast coder at -0.387 dBFS -- all three at I = -16.0 LUFS,
#: so a variant is judged on the same loudness gates as the attempt it beats.
#: The order is a cost contract: the guard stops at the first variant that meets
#: the bound, and each one is a whole extra encode.
TP_GUARD_ENCODER_VARIANTS: tuple[EncoderVariant, ...] = (
    EncoderVariant(bitrate_kbps=256),
    EncoderVariant(bitrate_kbps=256, extra_args=("-aac_coder", "fast")),
)


def encoder_variants_for(profile: CanonicalProfile) -> tuple[EncoderVariant, ...]:
    """The variants worth spending on this profile's encoder, in order.

    Empty for anything but AAC: :data:`TP_GUARD_ENCODER_VARIANTS`' rows are the
    native AAC encoder's options, and the headend's profiles carry
    ``audio_codec="ac3"``, where ``-aac_coder`` beside ``-c:a ac3`` is an FFmpeg
    parse error rather than a lever.

    A variant a profile already emits is dropped too: re-emitting exactly the
    nominal's settings buys a whole encode and changes only the encoder's
    internal state, which is not what the guard measures.
    """
    if profile.audio_codec != "aac":
        return ()
    return tuple(
        variant
        for variant in TP_GUARD_ENCODER_VARIANTS
        if variant.extra_args or variant.bitrate_kbps != profile.audio_bitrate_kbps
    )


def encoder_settings_label(profile: CanonicalProfile, variant: EncoderVariant | None = None) -> str:
    """Name one encode's encoder settings, for the operator's log line.

    U42: the guard's WARNING and the caller's ERROR name what each attempt
    actually ran, so a kept artifact can be traced to the encoder configuration
    that produced it without knowing which profile was in force at the time.
    """
    bitrate = profile.audio_bitrate_kbps if variant is None else variant.bitrate_kbps
    label = f"{profile.audio_codec} {bitrate}k"
    if variant is not None and variant.extra_args:
        label += f" ({' '.join(variant.extra_args)})"
    return label


@dataclass(frozen=True)
class LeveledAttempt:
    """One emitted attempt: the ceiling it ran at, and what it emitted.

    ``emitted_dbtp``/``emitted_lufs`` are ``None`` when the emitted artifact could
    not be measured.  ``worst_window_err_lu`` is the largest absolute deviation of
    any gated window from the target and ``whole_err_lu`` the signed whole-program
    error; both are ``None`` when the loudness measurement failed.  The loudness
    errors are the attempt's *own* artifact's, not the loop's pre-encode probe --
    the gates are judged on what airs.

    ``decoded_peak_dbfs`` is the hottest sample a decode of the artifact found,
    and it is the hard gate's whole evidence.  It is ``None`` when the artifact
    was not scanned, which -- like a missing loudness measurement -- cannot clear
    the gate it cannot evidence.

    ``encoder`` is the encoder configuration this attempt was emitted with, named
    by :func:`encoder_settings_label` (U42).  It decides nothing -- it is what
    lets the guard's WARNING and the caller's ERROR say which lever produced the
    artifact that airs.
    """

    round_index: int
    limit_dbtp: float
    emitted_dbtp: float | None
    emitted_lufs: float | None
    worst_window_err_lu: float | None
    whole_err_lu: float | None
    wall_s: float
    decoded_peak_dbfs: float | None = None
    encoder: str = ""

    def loudness_ok(self, *, window_tol_lu: float, whole_tol_lu: float) -> bool:
        """Whether both loudness gates hold for this attempt's own artifact."""
        return (
            self.worst_window_err_lu is not None
            and self.whole_err_lu is not None
            and abs(self.worst_window_err_lu) <= window_tol_lu
            and abs(self.whole_err_lu) <= whole_tol_lu
        )

    def hard_tp_ok(self) -> bool:
        """Whether this attempt clears the hard true-peak gate.

        The gate is one bound on measured fact -- no decoded sample above
        :data:`TP_GUARD_MAX_PEAK_DBFS`.  The emitted true peak is deliberately not
        a leg of it: it is the codec's own overshoot, so gating on it would only
        re-impose the ceiling arithmetic the guard exists to stop trusting.

        An unscanned attempt fails: the gate is a guarantee, and a missing
        measurement is not evidence that the guarantee held.
        """
        return (
            self.decoded_peak_dbfs is not None and self.decoded_peak_dbfs <= TP_GUARD_MAX_PEAK_DBFS
        )

    def over_hard_bound(self) -> bool:
        """Whether a *measured* peak is over the hard bound, with margin to spare.

        Not the same question as ``not hard_tp_ok()``, and U42 is why: that is
        also true of an attempt nobody scanned, and another encode of the same
        samples cannot fix a measurement that never ran.  This predicate is the
        guard's cue to spend an encoder variant, so it asks the narrower thing --
        a peak was measured, and it is above the bound.
        """
        return (
            self.decoded_peak_dbfs is not None and self.decoded_peak_dbfs > TP_GUARD_MAX_PEAK_DBFS
        )


@dataclass(frozen=True)
class LeveledSelection:
    """Which attempt ships, and the one warning that explains it.

    ``warning`` is the caller's to log -- this module never logs.  ``target_met``
    is the best effort and ``hard_tp_met`` the guarantee; neither changes what
    ships.  ``hard_tp_met`` being ``False`` is the caller's cue for an *error*
    line: the artifact airs anyway, because the selector had nothing better to
    keep and a hot artifact beats a silent channel.
    """

    kept: LeveledAttempt
    attempts: list[LeveledAttempt]
    target_met: bool
    hard_tp_met: bool
    warning: str | None


def _encoder_suffix(attempt: LeveledAttempt) -> str:
    """`` (aac 256k)`` when the attempt names its encoder, else nothing.

    Optional because an attempt need not come from the guard's loop: a
    hand-built one, or one from a caller that does not track encoders, has no
    label, and an empty bracket pair in the operator's line reads worse than no
    bracket at all.
    """
    return "" if not attempt.encoder else f" ({attempt.encoder})"


def select_leveled_attempt(
    attempts: Sequence[LeveledAttempt],
    *,
    window_tol_lu: float,
    whole_tol_lu: float,
    target_dbtp: float = TP_GUARD_TARGET_DBTP,
) -> LeveledSelection:
    """Which attempt ships, and whether it reached the target.

    The order is answer 12's keep-best, and it is a hierarchy rather than a filter:

    1. **the loudness gates** -- the level the audience was promised;
    2. **the lowest decoded sample peak** -- what the hard gate is judged on, and
       the one axis the guard can actually move;
    3. **the lowest emitted true peak** -- the guard's original purpose.

    Each level only breaks ties within the one above it.  There is no fourth
    level and no over-riding clause: the guard keeps the best attempt it has and
    the caller ships it, whether or not it clears the hard gate.  Answer 11's
    last resort did the opposite -- it over-rode a loudness-lawful keep with a
    quieter re-emit -- and U25's panel measured that artifact losing on every
    gated axis at once, which is the case this ordering exists to prevent.

    An unmeasurable attempt can never win an axis it has no measurement for: a
    peak nobody scanned is ``inf``, not zero, and the guard does not trade a
    measured artifact for an unmeasured one.  When no axis separates the attempts
    at all -- nothing measured -- the *nominal* attempt is kept, because a program
    that cannot be measured still airs.  Ties go to the earlier round, so the
    nominal ceiling keeps its claim whenever a lowered one is no better.

    U42 adds no axis here.  An encoder variant enters this function as one more
    attempt and is judged by these same three levels -- which is the whole point
    of the ordering: the variant round is not privileged, and it wins only by
    being lawful on loudness and colder on peak.  What U42 does add is the label
    in ``warning``: every attempt names its own encoder settings
    (:attr:`LeveledAttempt.encoder`), so the operator can see which lever bought
    the artifact that airs.
    """
    nominal = attempts[0]
    kept = min(
        attempts,
        key=lambda a: (
            0 if a.loudness_ok(window_tol_lu=window_tol_lu, whole_tol_lu=whole_tol_lu) else 1,
            math.inf if a.decoded_peak_dbfs is None else a.decoded_peak_dbfs,
            math.inf if a.emitted_dbtp is None else a.emitted_dbtp,
            a.round_index,
        ),
    )
    target_met = kept.emitted_dbtp is not None and kept.emitted_dbtp <= target_dbtp

    warning: str | None = None
    if not target_met:
        kept_tp = "unmeasurable" if kept.emitted_dbtp is None else f"{kept.emitted_dbtp:+.2f} dBTP"
        kept_peak = (
            "unmeasurable"
            if kept.decoded_peak_dbfs is None
            else f"{kept.decoded_peak_dbfs:+.2f} dBFS"
        )
        nominal_tp = (
            "unmeasurable" if nominal.emitted_dbtp is None else f"{nominal.emitted_dbtp:+.2f} dBTP"
        )
        tried = "; ".join(
            f"round {a.round_index}{_encoder_suffix(a)} at a {a.limit_dbtp:+.2f} dBTP ceiling -> "
            + ("unmeasurable" if a.emitted_dbtp is None else f"{a.emitted_dbtp:+.2f} dBTP emitted")
            + ", "
            + (
                "unmeasurable peak"
                if a.decoded_peak_dbfs is None
                else f"peak {a.decoded_peak_dbfs:+.2f} dBFS"
            )
            + ", "
            + (
                "loudness ok"
                if a.loudness_ok(window_tol_lu=window_tol_lu, whole_tol_lu=whole_tol_lu)
                else "loudness gate failed"
            )
            + ("" if a.hard_tp_ok() else f", over the {TP_GUARD_MAX_PEAK_DBFS:+.1f} dBFS bound")
            for a in attempts
        )
        warning = (
            f"the emitted true peak is {kept_tp}, above the {target_dbtp:+.1f} dBTP "
            f"target and this is best effort only -- the hard gate is the decoded "
            f"sample peak below, which the selection above enforces and this line "
            f"reports; the nominal attempt emitted {nominal_tp}; attempts: "
            f"{tried}; keeping round {kept.round_index}{_encoder_suffix(kept)} at a "
            f"{kept.limit_dbtp:+.2f} "
            f"dBTP ceiling, peak {kept_peak} (hard gate "
            f"{'met' if kept.hard_tp_ok() else 'NOT met'})"
        )
    return LeveledSelection(
        kept=kept,
        attempts=list(attempts),
        target_met=target_met,
        hard_tp_met=kept.hard_tp_ok(),
        warning=warning,
    )


def attempt_from_measurement(
    stderr: str,
    *,
    round_index: int,
    limit_dbtp: float,
    target_lufs: float,
    duration_s: float | None = None,
    decoded_peak_dbfs: float | None = None,
    wall_s: float = 0.0,
    encoder: str = "",
) -> LeveledAttempt:
    """Score one measured artifact, by the acceptance gate's own arithmetic.

    Two things are read off ``stderr`` and they are not the same thing:
    ``emitted_lufs`` is the summary ``I`` FFmpeg printed, and the loudness gate
    is computed from the per-block series instead -- the harness's own
    computation, which U25's panel showed disagreeing with the summary.  The
    printed value is kept for the report; it decides nothing.

    ``duration_s`` is the window the caller asked for.  Without it the tiling
    falls back to the series' own coverage (last block plus one block step),
    which is what a whole-asset measurement has to use -- there is no requested
    duration for a file.

    ``decoded_peak_dbfs`` is measured by the caller (:func:`scan_peak_dbfs`) and
    passed through; a ``None`` here means the hard gate cannot be cleared, not
    that it passed.

    ``encoder`` is the settings label of the attempt's own encode
    (:func:`encoder_settings_label`), carried through untouched: this function
    scores a measurement and does not claim to know what produced it.
    """
    series = parse_ebur128_series(stderr)
    span = series_duration_s(series) if duration_s is None else duration_s
    worst, _worst_start = worst_window_deviation_lu(series, span, target_lufs=target_lufs)
    return LeveledAttempt(
        round_index=round_index,
        limit_dbtp=limit_dbtp,
        emitted_dbtp=parse_true_peak_dbtp(stderr),
        emitted_lufs=parse_integrated_lufs(stderr),
        worst_window_err_lu=worst,
        whole_err_lu=whole_program_err_lu(series, target_lufs=target_lufs),
        wall_s=wall_s,
        decoded_peak_dbfs=decoded_peak_dbfs,
        encoder=encoder,
    )


def measure_artifact(
    artifact_path: Path,
    *,
    params: RideParams,
    round_index: int,
    limit_dbtp: float,
    target_lufs: float,
    duration_s: float | None = None,
    wall_s: float = 0.0,
    timeout_s: float | None = None,
    cancel_event: threading.Event | None = None,
    encoder: str = "",
) -> LeveledAttempt:
    """Measure one emitted artifact: its loudness series, and its decoded peak.

    The two measurements are independent failure domains.  A failed *loudness*
    pass costs the artifact its loudness evidence and nothing else -- the peak
    was still measured, so the hard gate can still hold, and a ride whose
    measurement child died is reported rather than discarded.  A failed *scan*
    costs the hard gate, which is exactly what an unmeasurable artifact should
    lose.  Cancellation is neither: it is the caller shutting down, and it is
    re-raised.

    ``encoder`` is the caller's label for the encode that produced this file and
    is passed to the attempt unread (:func:`attempt_from_measurement`); the
    measurement is of the file, not of the settings that wrote it.
    """
    try:
        text = run_capture(
            build_artifact_measure_args(artifact_path=artifact_path),
            timeout_s=timeout_s,
            cancel_event=cancel_event,
        )
    except LoudnessRideCancelledError:
        raise
    except LoudnessRideError:
        text = ""
    try:
        peak = scan_peak_dbfs(
            artifact_path,
            params=params,
            timeout_s=timeout_s,
            cancel_event=cancel_event,
        )
    except LoudnessRideCancelledError:
        raise
    except LoudnessRideError:
        peak = None
    return attempt_from_measurement(
        text,
        round_index=round_index,
        limit_dbtp=limit_dbtp,
        target_lufs=target_lufs,
        duration_s=duration_s,
        decoded_peak_dbfs=peak,
        wall_s=wall_s,
        encoder=encoder,
    )


@dataclass(frozen=True)
class LeveledWindow:
    """One window, leveled: what shipped, why, and what went wrong on the way.

    ``audio_path`` is the artifact that airs -- the nominal emit's file, or the
    guard's round moved onto it.  ``round_error`` is set only when a round was
    wanted and failed; it is a report for the caller to log, not a failure of
    the window, because the attempts that did survive are still there and the
    selector still keeps the best of them.  When more than one round fails the
    *first* failure is reported: it is the one that explains why the rounds
    after it ran at all.  ``tee_path`` records where the post-curve PCM went;
    it is a *record*, not a promise -- :func:`level_window` has already unlinked
    it when the module made it, and when the caller supplied it the caller owns
    it and does with it as it likes.
    """

    selection: LeveledSelection
    curve: RideCurve
    audio_path: Path
    wall_s: float
    tee_path: Path
    round_error: str | None = None


def level_window(
    *,
    source_path: Path,
    audio_path: Path,
    params: RideParams,
    profile: CanonicalProfile,
    duration_s: float | None = None,
    segment: EgressSourceSegment | None = None,
    pcm_path: Path | None = None,
    threads: int | None = None,
    ride_runner: RideRunner | None = None,
    reencode_runner: ReencodeRunner | None = None,
    cancel_event: threading.Event | None = None,
    timeout_s: float | None = None,
) -> LeveledWindow:
    """Level one window end to end and leave the artifact that should air at
    ``audio_path``.

    The whole of answer 12, in order: measure the source, converge the ride,
    emit once through the profile's codec, measure what was emitted, and then --
    only while the attempt that would be kept is still over the hard bound --
    spend re-encode rounds against the retained ride PCM.  The pad round comes
    first (answer 11's ceiling, U43's compensating drive; :func:`guard_pad_db`);
    if the kept attempt is *still* hot after it, the encoder variants
    (:data:`TP_GUARD_ENCODER_VARIANTS`, U42) follow in their fixed order, and the
    loop stops at the first attempt that is not over the bound.  All rounds read
    the same PCM and re-run only the limiter, the codec and the mux: no
    re-convergence, and no re-render of the curve.  The drive is the one thing a
    round may move, and only the pad round moves it.  That round first *measures*
    the settings it is about to spend: one extra limited pass over the source, at
    its own ceiling and drive, measuring rather than encoding.  (The ride's own
    render is the pass that can be pointed at another ceiling; the retained PCM is
    headerless raw f32, so the module's artifact measurement, which reads a media
    file, cannot score it.)  The drive is then corrected by the
    whole-program error that pass measured (:func:`guard_pad_trim_db`), because a
    lowered ceiling makes the limiter work harder and it then removes *less*
    loudness than the pad hands back (U43's sweep: +0.53 LU on one live cell,
    -0.28 LU on another).  Once, not to a fixed point: the guard owes the bound
    one round, not a converged level.  If that measuring pass dies, the round
    still ships at the uncorrected pad -- the bound is still owed.
    Then keep the best attempt (see
    :func:`select_leveled_attempt`) and make ``audio_path`` be it: the winning
    round is moved onto the caller's path, every losing one deleted, so the
    caller has one file to publish and no choice left to make.

    ``pcm_path`` is the tee of post-curve PCM the emit writes and the round
    reads.  The caller may supply one (it then owns the file and it outlives the
    call); otherwise the module makes its own, next to the artifact, and deletes
    it on the way out -- at 48 kHz stereo float32 that is ~23 MB per minute of
    program, so it is not a file to leave behind.

    Raises :class:`LoudnessRideError` when the source cannot be measured or the
    ride cannot converge -- the caller's cue to degrade to the pre-U25 conform.
    Cancellation raises :class:`LoudnessRideCancelledError` and is re-raised
    after the files this call owns are cleaned up.  A round that fails is not an
    error: it is reported in ``round_error`` and the nominal ships.
    """
    ride = run_ride if ride_runner is None else ride_runner
    reencode = run_reencode if reencode_runner is None else reencode_runner

    source_stderr = run_capture(
        build_source_series_args(
            source_path=source_path,
            segment=segment,
            params=params,
            threads=threads,
        ),
        timeout_s=timeout_s,
        cancel_event=cancel_event,
    )
    source_series = parse_ebur128_series(source_stderr)
    if not source_series:
        raise LoudnessRideError(
            "the source pass measured no loudness series, so there is nothing to level"
        )
    source_levels = sliding_levels(source_series, params.window_s, params.step_s)

    def render(
        curve: list[tuple[float, float]],
        trim_db: float | None,
        *,
        limit_dbtp: float | None = None,
    ) -> RideRender:
        """One measured pass over the source, at the ride's ceiling or another.

        ``limit_dbtp`` is for U43's pad round, which has to measure the level it
        would actually emit at: the limiter's gain reduction -- and therefore the
        whole-program loudness -- depends on the ceiling, so a pass at the ride's
        ceiling is not evidence about a pass at the round's.  ``None`` keeps the
        ride's own ceiling, which is what convergence wants.
        """
        pass_params = params if limit_dbtp is None else replace(params, limit_dbtp=limit_dbtp)
        return ride(
            decoder_args=build_decoder_args(
                source_path=source_path,
                segment=segment,
                params=pass_params,
                threads=threads,
            ),
            sink_args=build_measure_sink_args(trim_db, params=pass_params),
            curve=curve,
            params=pass_params,
            cancel_event=cancel_event,
            timeout_s=timeout_s,
        )

    curve = converge(
        params,
        source_blocks=source_series,
        source_levels=source_levels,
        render=render,
    )

    owned_tee = pcm_path is None
    if pcm_path is None:
        tee_file = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed and unlinked below
            prefix="civiccast-ride-", suffix=".pcm", dir=audio_path.parent, delete=False
        )
        tee_path = Path(tee_file.name)
        tee_file.close()
    else:
        tee_path = pcm_path

    t_start = time.perf_counter()
    round_paths: dict[int, Path] = {}
    round_error: str | None = None
    try:
        emit = ride(
            decoder_args=build_decoder_args(
                source_path=source_path,
                segment=segment,
                params=params,
                threads=threads,
            ),
            sink_args=build_encode_sink_args(
                audio_path,
                curve.trim_db,
                params=params,
                profile=profile,
            ),
            curve=curve.curve,
            params=params,
            pcm_path=tee_path,
            cancel_event=cancel_event,
            timeout_s=timeout_s,
        )

        attempts = [
            measure_artifact(
                audio_path,
                params=params,
                round_index=0,
                limit_dbtp=params.limit_dbtp,
                target_lufs=params.target_lufs,
                duration_s=duration_s,
                wall_s=emit.wall_s,
                timeout_s=timeout_s,
                cancel_event=cancel_event,
                encoder=encoder_settings_label(profile),
            )
        ]

        #: What each round was emitted with, keyed by round index.  The nominal
        #: emit is round 0; :func:`reemit` records the rest.  The guard needs it
        #: because both of its levers are relative to an attempt that already
        #: exists: the pad round moves *the kept attempt's* drive and ceiling, and
        #: a variant re-emits at the ceiling and drive of the attempt it is trying
        #: to beat -- otherwise the encoder would not be the single varying axis.
        spent: dict[int, tuple[float, float]] = {0: (curve.trim_db, params.limit_dbtp)}

        def keep_best() -> LeveledAttempt:
            """The attempt :func:`select_leveled_attempt` would ship right now.

            The guard's stop condition is a property of the *selection*, not of
            the latest round: a round can be colder on peak and still lose on
            loudness (the live case, log line 18797), and spending an encoder
            variant to beat an attempt nobody would keep would be spending it on
            the wrong artifact.  Reusing the selector itself -- rather than
            re-stating its ordering here -- is what keeps the two in step.
            """
            return select_leveled_attempt(
                attempts,
                window_tol_lu=LOUDNESS_WINDOW_TOL_LU,
                whole_tol_lu=LOUDNESS_WHOLE_TOL_LU,
            ).kept

        def reemit(*, limit_dbtp: float, trim_db: float, variant: EncoderVariant | None) -> None:
            """Spend one re-encode round on the retained PCM, and measure it.

            Both of the guard's levers arrive here: the limiter ceiling with the
            drive that goes with it (answer 11's ceiling, U43's pad) or an
            ``EncoderVariant`` (U42).  The input is ``tee_path`` in every case --
            the very samples the nominal emit consumed -- so a round is always
            "the same audio, one setting different", which is what lets the
            selector compare the attempts at all.

            A failed round is not raised: it is recorded in ``round_error`` and
            the attempts that did survive are what the selector sees.  The first
            failure wins the report, because it is the one that explains why the
            rounds after it ran.  Cancellation is not a failure -- it is the
            caller shutting down, so the file this round was writing is removed
            and the error propagates.
            """
            nonlocal round_error
            index = len(attempts)
            path = audio_path.with_name(f"{audio_path.name}.round{index}.ts")
            round_paths[index] = path
            spent[index] = (trim_db, limit_dbtp)
            try:
                render = reencode(
                    pcm_path=tee_path,
                    output_path=path,
                    trim_db=trim_db,
                    params=replace(params, limit_dbtp=limit_dbtp),
                    profile=profile,
                    timeout_s=timeout_s,
                    variant=variant,
                )
            except LoudnessRideCancelledError:
                del round_paths[index]
                path.unlink(missing_ok=True)
                raise
            except LoudnessRideError as exc:
                round_error = round_error or str(exc)
                del round_paths[index]
                path.unlink(missing_ok=True)
                return
            attempts.append(
                measure_artifact(
                    path,
                    params=params,
                    round_index=index,
                    limit_dbtp=limit_dbtp,
                    target_lufs=params.target_lufs,
                    duration_s=duration_s,
                    wall_s=render.wall_s,
                    timeout_s=timeout_s,
                    cancel_event=cancel_event,
                    encoder=encoder_settings_label(profile, variant),
                )
            )

        # U43: one pad round, and only one, against the attempt that would ship.
        # The overshoot this chases is codec-side (U43's band-limit probe moved
        # the encoder's own cutoff by +0.00 dB), so the cure is headroom at the
        # encoder: the ceiling drops by exactly the measured pad AND the drive
        # rises by the same amount, which keeps the programme loudness where the
        # converged curve put it while the decoder's peaks land under the bound.
        # A round that does not pay is not re-tried -- the pad is the measured
        # need, not a knob to search -- and an attempt nobody would keep is not
        # worth spending a round on at all, which is what keep_best() answers.
        #
        # The pad's arithmetic is the right one for the bound and the wrong one
        # for the level: the ceiling it takes is what the limiter has to work
        # against, and harder limiting removes less loudness than the pad hands
        # back (U43's sweep measured the round +0.53 LU loud on one live cell and
        # -0.28 LU quiet on another, so the residue is neither small nor fixed in
        # sign).  So the round measures the settings it is about to spend, at the
        # ceiling it will emit at, and corrects the drive by what it finds --
        # converge()'s own correction, spent once.  The measuring pass is a
        # measurement, not a required step: if it dies, the round still ships at
        # the uncorrected pad, because the bound is still owed.
        hot = keep_best()
        pad = guard_pad_db(hot)
        if pad is not None:
            hot_trim, hot_ceiling = spent[hot.round_index]
            pad_trim = hot_trim + pad
            pad_ceiling = hot_ceiling - pad
            try:
                probe = render(curve.curve, pad_trim, limit_dbtp=pad_ceiling)
                measured = parse_integrated_lufs(probe.stderr)
            except LoudnessRideCancelledError:
                raise
            except LoudnessRideError:
                measured = None
            corrected = guard_pad_trim_db(
                measured,
                trim_db=pad_trim,
                target_lufs=params.target_lufs,
            )
            reemit(
                limit_dbtp=pad_ceiling,
                trim_db=pad_trim if corrected is None else corrected,
                variant=None,
            )

        # U42: the ceiling and the drive are spent, so if the attempt that would
        # ship is still hot, the encoder is the lever that is left.  Each variant
        # re-emits at the *kept* attempt's own ceiling and drive -- the encoder is
        # then the single varying axis, and the variant's result is comparable
        # with the attempt it is trying to beat.  That pair *is* the pad round's
        # whenever the pad round is the attempt being beaten: the pad round is the
        # kept attempt exactly when it holds loudness and peaks lower than the
        # rest, and a variant re-emits at its anchor.  A pad round that lost
        # keep-best -- quiet, or peaking above what it was trying to fix -- is not
        # what the encoder should be compared against, and the anchor stays on the
        # attempt that would actually ship.  Stop at the first attempt that is not
        # over the bound; if every variant misses it, the best attempt ships
        # anyway and the caller's ERROR line says so.
        for variant in encoder_variants_for(profile):
            kept_now = keep_best()
            if not kept_now.over_hard_bound():
                break
            kept_trim, kept_ceiling = spent[kept_now.round_index]
            reemit(limit_dbtp=kept_ceiling, trim_db=kept_trim, variant=variant)

        selection = select_leveled_attempt(
            attempts,
            window_tol_lu=LOUDNESS_WINDOW_TOL_LU,
            whole_tol_lu=LOUDNESS_WHOLE_TOL_LU,
        )
        # One file airs, and it is the kept attempt's.  Every losing round is
        # deleted rather than left beside the artifact: a second, hotter file in
        # the same cache directory is a file something can pick up by accident.
        kept_path = round_paths.get(selection.kept.round_index)
        for path in round_paths.values():
            if path is not kept_path:
                path.unlink(missing_ok=True)
        if kept_path is not None:
            kept_path.replace(audio_path)
    finally:
        if owned_tee:
            with contextlib.suppress(OSError):
                tee_path.unlink(missing_ok=True)

    return LeveledWindow(
        selection=selection,
        curve=curve,
        audio_path=audio_path,
        wall_s=time.perf_counter() - t_start,
        tee_path=tee_path,
        round_error=round_error,
    )
