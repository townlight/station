# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Streaming loudness compliance gate through the ffmpeg wrapper boundary."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path

from civiccast.stream._ffmpeg import check_ffmpeg, run_ffmpeg


@dataclass(frozen=True)
class LoudnessGateResult:
    """ITU-R BS.1770 / EBU R128 loudness gate result."""

    status: str
    standard: str
    target_lufs: float
    used_ffmpeg_wrapper: bool
    measured_lufs: float | None
    operator_action: str


# The standard CivicCast has always reported; the default keeps every existing
# caller (streaming -16 LUFS) byte-for-byte the same.
DEFAULT_LOUDNESS_STANDARD = "ITU-R BS.1770 / EBU R128"


def _loudness_failure(
    *, standard: str, target_lufs: float, operator_action: str
) -> LoudnessGateResult:
    return LoudnessGateResult(
        status="failed",
        standard=standard,
        target_lufs=target_lufs,
        used_ffmpeg_wrapper=True,
        measured_lufs=None,
        operator_action=operator_action,
    )


def check_loudness(
    *,
    media_path: Path,
    target_lufs: float,
    tolerance_lufs: float,
    standard_label: str = DEFAULT_LOUDNESS_STANDARD,
    probe_start_seconds: float | None = None,
    probe_duration_seconds: float | None = None,
    threads: int | None = None,
    cancel_event: threading.Event | None = None,
) -> LoudnessGateResult:
    """Measure a media asset's integrated loudness and gate it against a target.

    ``target_lufs`` / ``standard_label`` are parameterised (S11b per-sink
    loudness): cable normalises to ATSC A/85 -24 LKFS, streaming to -16 LUFS,
    EBU R128 to -23 LUFS — all measured by the same ITU-R BS.1770 meter. The
    result reports the *destination's* standard instead of a single hardcoded
    one, and the remediation hint names the actual target.

    ``probe_start_seconds``/``probe_duration_seconds`` (item 66 round-3,
    Opus review): both ``None`` (every caller except the source preparer)
    measures the WHOLE file, unchanged. When given, they bound the probe to
    a window instead -- ``probe_start_seconds`` seeks (``-ss`` before
    ``-i``) and ``probe_duration_seconds`` limits (``-t`` after ``-i``),
    the same convention ``build_conform_source_args`` uses. This is a
    deliberate accuracy/speed trade the caller opts into; it does not
    change what a whole-file probe would report for material with uniform
    loudness, and it can differ for material that varies significantly
    across its length -- callers that need the whole-file measurement must
    leave both ``None``.

    ``threads`` (item 66 round-4, Opus review; position fixed round-5):
    caps ffmpeg's decode AND filter-graph threading at that many threads
    (``-threads <N>`` before ``-i`` -- an input/decoder option, not an
    output one -- plus ``-filter_complex_threads <N>`` for the
    ``ebur128`` filter graph itself). A round-4 version of this placed
    ``-threads`` after ``-i``/``-t``, which ffmpeg parses as an OUTPUT
    option applying only to the ``-f null`` output -- it never actually
    capped the decode or filter-graph threads it was meant to bound.
    Exercised by every probe the source preparer takes: ``_prepare_segment``
    (``civiccast/egress/preparer.py``) passes ``threads=_foreground_thread_
    cap()`` on every synchronous probe call -- the trimmed-window probe, the
    untrimmed mid-file/head sample, and the silence-floor corroboration
    resample alike (item 66 round-6, point 6: this correction was itself
    prompted by finding that no production caller had ever exercised the
    round-5 arg-order fix until then).
    """

    if not media_path.exists():
        return _loudness_failure(
            standard=standard_label,
            target_lufs=target_lufs,
            operator_action=(
                f"Media file {media_path} is missing; render the release audio proof and "
                "rerun the loudness gate."
            ),
        )
    ffmpeg = check_ffmpeg()
    if ffmpeg is None:
        return _loudness_failure(
            standard=standard_label,
            target_lufs=target_lufs,
            operator_action=(
                "Install ffmpeg, verify it with civiccast doctor, then rerun loudness."
            ),
        )
    args = ["-hide_banner", "-nostats"]
    if threads is not None:
        # Item 66 round-5 (Opus review, point 4): ``-threads`` is an
        # INPUT/decoder option in ffmpeg's arg grammar -- it must come
        # before ``-i`` to cap the decode. Placed after ``-i``/``-t`` (the
        # round-4 shape) it is parsed as an OUTPUT option and caps only the
        # ``-f null`` output, which does no decoding of its own -- the
        # decode itself ran fully unthrottled regardless.
        # ``-filter_complex_threads`` caps the ``ebur128`` filter graph's
        # own threading separately; ``-threads`` alone does not bound it.
        args.extend(["-threads", str(threads), "-filter_complex_threads", str(threads)])
    if probe_start_seconds is not None:
        args.extend(["-ss", f"{probe_start_seconds:g}"])
    args.extend(["-i", str(media_path)])
    # Item 66 follow-up (measured on HALO): the loudness gate only needs
    # the audio stream -- ``-vn`` drops video decode from the ebur128 pass
    # entirely. Measured 46.7s -> far less on a 39-min clip; unmeasured
    # exact delta here, but audio-only decode is strictly cheaper and never
    # changes the LUFS measurement (video frames never feed ebur128).
    args.append("-vn")
    if probe_duration_seconds is not None:
        args.extend(["-t", f"{probe_duration_seconds:g}"])
    args.extend(["-filter_complex", "ebur128=peak=true", "-f", "null", "-"])
    if cancel_event is None:
        result = run_ffmpeg(args)
    else:
        result = run_ffmpeg(args, cancel_event=cancel_event)
    if result.returncode != 0:
        return _loudness_failure(
            standard=standard_label,
            target_lufs=target_lufs,
            operator_action="ffmpeg loudness analysis failed; inspect the media file and rerun.",
        )
    measured = _parse_integrated_lufs(result.stderr)
    if measured is None:
        return _loudness_failure(
            standard=standard_label,
            target_lufs=target_lufs,
            operator_action="ffmpeg did not report integrated LUFS; rerun with valid audio media.",
        )
    status = "ok" if abs(measured - target_lufs) <= tolerance_lufs else "failed"
    return LoudnessGateResult(
        status=status,
        standard=standard_label,
        target_lufs=target_lufs,
        used_ffmpeg_wrapper=True,
        measured_lufs=measured,
        operator_action="Loudness is within tolerance."
        if status == "ok"
        else f"Normalize audio to {target_lufs:g} LUFS and rerun the loudness gate.",
    )


def check_streaming_loudness(
    *,
    media_path: Path,
    target_lufs: float,
    tolerance_lufs: float,
    probe_start_seconds: float | None = None,
    probe_duration_seconds: float | None = None,
    threads: int | None = None,
    cancel_event: threading.Event | None = None,
) -> LoudnessGateResult:
    """Back-compat wrapper: gate streaming audio against its -16 LUFS target.

    Retained so existing callers (the source preparer, the FileSink/SRT
    continuity proofs, the CLI) and their test monkeypatches keep working while
    new per-sink callers use :func:`check_loudness` with a destination label.
    ``probe_start_seconds``/``probe_duration_seconds``/``threads`` forward to
    :func:`check_loudness` -- see its docstring (item 66 round-3: the source
    preparer bounds every probe to a window instead of the whole file;
    round-5: ``threads`` caps decode + filter-graph threading, positioned
    correctly as an input option -- see that docstring for why the
    original round-4 placement never actually capped anything).
    """

    return check_loudness(
        media_path=media_path,
        target_lufs=target_lufs,
        tolerance_lufs=tolerance_lufs,
        probe_start_seconds=probe_start_seconds,
        probe_duration_seconds=probe_duration_seconds,
        threads=threads,
        cancel_event=cancel_event,
    )


def _parse_integrated_lufs(stderr: str) -> float | None:
    matches = re.findall(r"\bI:\s*(-?\d+(?:\.\d+)?)\s+LUFS\b", stderr)
    if not matches:
        return None
    return float(matches[-1])
