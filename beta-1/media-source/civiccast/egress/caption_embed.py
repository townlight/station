# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Caption embedding boundary and decode-back proof for egress."""

from __future__ import annotations

import re
import time
from html import unescape
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from civiccast.captions.models import CaptionCue

CAPTION_EMBED_PROOF_BOUNDARY = "egress-caption-embed-to-emitted-stream-decode-back"
# The hours field is unbounded, not two digits wide: a live sidecar's program
# clock is an absolute second count since the channel's program start and
# ``format_webvtt_timestamp`` renders it with ``divmod(total_ms, 3_600_000)``,
# so a station that has been on air for more than 100 hours writes three-digit
# hours (``112:59:50.000``). Bounding hours at two digits silently dropped every
# cue of a channel past that mark -- see U46 (2026-09-26), where public emitted a
# caption-free stream while its sidecar kept growing.
_TIMED_TEXT_RE = re.compile(
    r"(?P<start>\d+:\d{2}(?::\d{2})?[\.,]\d{3})\s*-->\s*"
    r"(?P<end>\d+:\d{2}(?::\d{2})?[\.,]\d{3})"
)


class EgressCaptionEmbeddingPlan(BaseModel):
    """Operator-visible caption embedding posture for one egress run."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    status: Literal["not-verified", "on"]
    mode: Literal["passthrough", "cea-708", "sidecar"]
    cue_count: Annotated[int, Field(ge=0)]
    ffmpeg_args: list[str] = Field(default_factory=list)
    input_args: list[str] = Field(default_factory=list)
    stream_args: list[str] = Field(default_factory=list)
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]
    decode_back_required: bool = True
    operator_action: Annotated[str, Field(min_length=1, max_length=500)]
    not_claimed: list[str] = Field(default_factory=list)


class EgressCaptionDecodeBackProof(BaseModel):
    """Result of comparing decoded emitted-stream captions with expected cues."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    status: Literal["PASS", "FAIL"]
    caption_status: Literal["not-verified", "on"]
    emitted_stream_path: Annotated[str, Field(min_length=1, max_length=1000)]
    decoder_name: Annotated[str, Field(min_length=1, max_length=120)]
    expected_cue_count: Annotated[int, Field(ge=0)]
    decoded_cue_count: Annotated[int, Field(ge=0)]
    matched_cue_count: Annotated[int, Field(ge=0)]
    max_timing_delta_seconds: Annotated[float, Field(ge=0)]
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]
    blocker: Annotated[str | None, Field(default=None, max_length=200)] = None
    not_claimed: list[str] = Field(default_factory=list)


class CaptionEmbedder(Protocol):
    """Caption embedding seam used by the egress encoder plan."""

    def build_plan(self, *, channel_id: str, cues: list[CaptionCue]) -> EgressCaptionEmbeddingPlan:
        """Return FFmpeg caption args and operator-visible status."""


class PassThroughCaptionEmbedder:
    """Default caption embedder until E.4 decode-back proof exists."""

    def build_plan(self, *, channel_id: str, cues: list[CaptionCue]) -> EgressCaptionEmbeddingPlan:
        return EgressCaptionEmbeddingPlan(
            channel_id=channel_id,
            status="not-verified",
            mode="passthrough",
            cue_count=len(cues),
            ffmpeg_args=[],
            input_args=[],
            stream_args=[],
            proof_boundary=CAPTION_EMBED_PROOF_BOUNDARY,
            operator_action=(
                "Caption embedding has not passed emitted-stream decode-back proof yet; "
                "show captions as not verified."
            ),
            not_claimed=[
                "This plan does not claim CEA-708 captions are embedded.",
                "This plan does not claim caption compliance or accessibility readiness.",
                "Caption status may become on only after emitted-stream decode-back proof passes.",
            ],
        )


class SidecarCaptionEmbedder:
    """Build an FFmpeg sidecar-caption plan without claiming decode-back success."""

    def __init__(self, *, sidecar_path: Path, subtitle_input_index: int = 1) -> None:
        if subtitle_input_index < 1:
            raise ValueError("subtitle_input_index must leave index 0 for program media.")
        self._sidecar_path = sidecar_path
        self._subtitle_input_index = subtitle_input_index

    def build_plan(self, *, channel_id: str, cues: list[CaptionCue]) -> EgressCaptionEmbeddingPlan:
        input_args = ["-i", str(self._sidecar_path)]
        stream_args = ["-map", f"{self._subtitle_input_index}:s:0?", "-c:s", "copy"]
        return EgressCaptionEmbeddingPlan(
            channel_id=channel_id,
            status="not-verified",
            mode="sidecar",
            cue_count=len(cues),
            ffmpeg_args=[*input_args, *stream_args],
            input_args=input_args,
            stream_args=stream_args,
            proof_boundary=CAPTION_EMBED_PROOF_BOUNDARY,
            operator_action=(
                "CivicCast will pass the configured caption sidecar into FFmpeg, but captions "
                "remain not verified until the emitted stream is decoded and checked."
            ),
            not_claimed=[
                "This plan does not claim CEA-708 ancillary caption embedding.",
                "This plan does not claim captions survived the emitted stream.",
                "Caption status may become on only after emitted-stream decode-back proof passes.",
            ],
        )


def evaluate_caption_decode_back(
    *,
    channel_id: str,
    emitted_stream_path: Path,
    expected_cues: list[CaptionCue],
    decoded_cues: list[CaptionCue],
    decoder_name: str,
    timing_tolerance_seconds: float = 0.75,
) -> EgressCaptionDecodeBackProof:
    """Evaluate decoded emitted-stream captions against expected captions."""

    if timing_tolerance_seconds < 0:
        raise ValueError("timing_tolerance_seconds must be zero or greater.")
    if not expected_cues:
        return _caption_proof(
            channel_id=channel_id,
            emitted_stream_path=emitted_stream_path,
            decoder_name=decoder_name,
            expected_cues=expected_cues,
            decoded_cues=decoded_cues,
            matched_cue_count=0,
            max_delta=0,
            blocker="EGRESS_CAPTION_DECODE_BACK_NO_EXPECTED_CUES",
        )
    matched = 0
    deltas: list[float] = []
    remaining = list(decoded_cues)
    for expected in expected_cues:
        match_index = _find_matching_decoded_cue(
            expected,
            remaining,
            timing_tolerance_seconds=timing_tolerance_seconds,
        )
        if match_index is None:
            continue
        decoded = remaining.pop(match_index)
        matched += 1
        deltas.extend(
            [
                abs(decoded.start_seconds - expected.start_seconds),
                abs(decoded.end_seconds - expected.end_seconds),
            ]
        )
    blocker = None if matched == len(expected_cues) else "EGRESS_CAPTION_DECODE_BACK_MISMATCH"
    return _caption_proof(
        channel_id=channel_id,
        emitted_stream_path=emitted_stream_path,
        decoder_name=decoder_name,
        expected_cues=expected_cues,
        decoded_cues=decoded_cues,
        matched_cue_count=matched,
        max_delta=max(deltas, default=0),
        blocker=blocker,
    )


#: Bounded retry for reading a live caption sidecar on Windows.
#:
#: Field defect (beta.10, 2026-09-25 23:44:45 and 23:45:29): the egress service
#: logged ``PermissionError [WinError 32]`` ("the process cannot access the file
#: because it is being used by another process") reading a channel's
#: ``captions/active.vtt``. The writer is the caption tap's atomic publish
#: (``LiveWebVttPublisher`` -> ``live_sidecar._atomic_write_text``): a
#: ``tempfile`` + ``replace``. Windows does not serialize a reader against that
#: rename -- a read whose ``CreateFile`` lands inside the window while the
#: replace holds the destination is REFUSED, where POSIX would simply have
#: opened the old or the new inode. That window is one rename wide, so it is
#: retried here rather than left to surface as a caption read failure.
#:
#: The retry is bounded and last-error-preserving: a refusal that outlives every
#: attempt is re-raised, never degraded to an empty cue list. "No cues readable"
#: and "no cues" must not be the same answer -- the decode-back proof and the
#: feed both act on the difference. Worst case added wait: 2 x 50ms.
_ACTIVE_VTT_READ_ATTEMPTS = 3
_ACTIVE_VTT_READ_BACKOFF_SECONDS = 0.05


def _read_timed_text(path: Path) -> str:
    """``path.read_text`` with a bounded Windows sharing-violation retry."""

    for _attempt in range(_ACTIVE_VTT_READ_ATTEMPTS - 1):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            time.sleep(_ACTIVE_VTT_READ_BACKOFF_SECONDS)
    return path.read_text(encoding="utf-8")  # final attempt surfaces the real error


def count_caption_timing_windows(path: Path) -> int:
    """Count the cue timing windows a timed-text file *looks* like it has.

    Deliberately structural, not regex-validated: the reader's whole failure mode
    (U46, 2026-09-26) is a file full of windows that the parser cannot read, and a
    count that reused the parser's own pattern would report 0 there and hide it.
    A window is any line carrying the ``-->`` arrow, which no VTT/SRT writer in
    this product emits for any other purpose.
    """

    return sum(1 for line in _read_timed_text(path).splitlines() if "-->" in line)


def load_caption_cues_from_timed_text(
    path: Path, *, source_id: str | None = None
) -> list[CaptionCue]:
    """Load proof-input captions from a WebVTT or SRT-style timed-text file.

    A live sidecar is republished by an atomic replace while readers are active
    (see ``_ACTIVE_VTT_READ_ATTEMPTS``), so the read is retried a bounded number
    of times against a Windows sharing violation before the failure is allowed
    through.
    """

    return parse_caption_cues_from_timed_text(
        _read_timed_text(path),
        source_id=source_id or path.stem or "caption-proof",
    )


def parse_caption_cues_from_timed_text(
    payload: str,
    *,
    source_id: str = "caption-proof",
    default_confidence: float = 1.0,
) -> list[CaptionCue]:
    """Parse WebVTT/SRT-style captions into stable cues for decode-back proof."""

    lines = payload.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n").splitlines()
    cues: list[CaptionCue] = []
    cue_label: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or line.upper() == "WEBVTT" or line.startswith(("NOTE", "STYLE", "REGION")):
            cue_label = None
            index += 1
            continue
        timing = _TIMED_TEXT_RE.search(line)
        if timing is None:
            cue_label = None if line.isdigit() else line
            index += 1
            continue
        text_lines: list[str] = []
        index += 1
        while index < len(lines) and lines[index].strip():
            text_lines.append(lines[index].strip())
            index += 1
        text = _clean_caption_text(" ".join(text_lines))
        if text:
            start_seconds = _parse_caption_timestamp(timing.group("start"))
            end_seconds = _parse_caption_timestamp(timing.group("end"))
            # Skip degenerate timings rather than letting CaptionCue raise: ffmpeg can emit
            # instantaneous CEA-608/708 cues whose start equals their end, and one invalid
            # cue here would otherwise abort the whole proof scan instead of skipping it.
            if end_seconds > start_seconds:
                cue_number = len(cues) + 1
                cues.append(
                    CaptionCue(
                        cue_id=_caption_cue_id(
                            source_id=source_id,
                            cue_label=cue_label,
                            cue_number=cue_number,
                        ),
                        start_seconds=start_seconds,
                        end_seconds=end_seconds,
                        text=text,
                        confidence=default_confidence,
                        low_confidence=default_confidence < 0.8,
                    )
                )
        cue_label = None
        index += 1
    return cues


def _find_matching_decoded_cue(
    expected: CaptionCue,
    decoded_cues: list[CaptionCue],
    *,
    timing_tolerance_seconds: float,
) -> int | None:
    expected_text = _normalize_caption_text(expected.text)
    for index, decoded in enumerate(decoded_cues):
        if _normalize_caption_text(decoded.text) != expected_text:
            continue
        if abs(decoded.start_seconds - expected.start_seconds) > timing_tolerance_seconds:
            continue
        if abs(decoded.end_seconds - expected.end_seconds) > timing_tolerance_seconds:
            continue
        return index
    return None


def _caption_proof(
    *,
    channel_id: str,
    emitted_stream_path: Path,
    decoder_name: str,
    expected_cues: list[CaptionCue],
    decoded_cues: list[CaptionCue],
    matched_cue_count: int,
    max_delta: float,
    blocker: str | None,
) -> EgressCaptionDecodeBackProof:
    passed = blocker is None
    return EgressCaptionDecodeBackProof(
        channel_id=channel_id,
        status="PASS" if passed else "FAIL",
        caption_status="on" if passed else "not-verified",
        emitted_stream_path=str(emitted_stream_path),
        decoder_name=decoder_name,
        expected_cue_count=len(expected_cues),
        decoded_cue_count=len(decoded_cues),
        matched_cue_count=matched_cue_count,
        max_timing_delta_seconds=max_delta,
        proof_boundary=CAPTION_EMBED_PROOF_BOUNDARY,
        blocker=blocker,
        not_claimed=[
            "Decode-back proof covers the emitted stream under test only.",
            "Decode-back proof does not certify legal caption compliance by itself.",
        ],
    )


def _normalize_caption_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def _parse_caption_timestamp(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    seconds = float(parts[-1])
    minutes = int(parts[-2])
    hours = int(parts[-3]) if len(parts) == 3 else 0
    return hours * 3600 + minutes * 60 + seconds


def _clean_caption_text(value: str) -> str:
    cleaned = re.sub(r"<br\s*/?>", " ", unescape(value), flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    # Strip ASS/SSA override blocks (e.g. "{\an7}") that ffmpeg's eia_608/cc_dec ->
    # srt encoder wraps real decoded closed-caption text in for on-screen position
    # (\anX). Discovered empirically 2026-08-25 building the CEA-708 decode-back
    # fixture (docs/spec/3.0/sections/S11-captions-loudness-eas-compliance.md): every
    # real ffmpeg-decoded CEA-608/708 cue carries this prefix, so without stripping it
    # here, evaluate_caption_decode_back's text comparison never matches a real
    # decode against a clean expected cue -- this parser had never been exercised
    # against real ffmpeg SRT output before, only against hand-written fixture text.
    cleaned = re.sub(r"\{\\[^}]*\}", "", cleaned)
    return " ".join(cleaned.split())


def _caption_cue_id(*, source_id: str, cue_label: str | None, cue_number: int) -> str:
    label = cue_label or f"{cue_number:06d}"
    safe_source = _safe_caption_id_fragment(source_id)
    safe_label = _safe_caption_id_fragment(label)
    return f"{safe_source}-{safe_label}"[:160]


def _safe_caption_id_fragment(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return safe or "caption-proof"
