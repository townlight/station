# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Continuity proof helpers for real egress FileSink output."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Annotated, Literal, Protocol, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field

from civiccast.egress.models import (
    EgressConfig,
    EgressSinkSpec,
    EgressSourcePlan,
    uri_looks_secret_bearing,
)
from civiccast.egress.runtime import FfmpegRunner, build_persistent_encoder_args, write_concat_plan
from civiccast.stream._ffmpeg import FfmpegNotFoundError, run_ffmpeg, start_ffmpeg
from civiccast.stream.loudness import LoudnessGateResult, check_streaming_loudness

CONTINUITY_PROOF_BOUNDARY: Literal["civiccast-egress-filesink-continuity-boundary"] = (
    "civiccast-egress-filesink-continuity-boundary"
)
CONTINUITY_NOT_CLAIMED = (
    "This proof does not validate a real cable headend.",
    "This proof does not validate QAM modulation, SDI output, EAS, or CEA-708 captions.",
    "FileSink output is CI proof of CivicCast-controlled FFmpeg behavior, not downstream acceptance.",
)


class EgressContinuityBoundary(BaseModel):
    """Expected source transition in one continuity proof run."""

    model_config = ConfigDict(extra="forbid")

    index: Annotated[int, Field(ge=1)]
    source_label: Annotated[str, Field(min_length=1, max_length=200)]
    expected_start_seconds: Annotated[float, Field(ge=0)]


class EgressContinuityProof(BaseModel):
    """Machine-readable result for one FileSink continuity proof."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["PASS", "FAIL"]
    sink_kind: Literal["file", "srt"] = "file"
    channel_id: Annotated[str, Field(min_length=1, max_length=80)]
    proof_boundary: Literal["civiccast-egress-filesink-continuity-boundary"]
    boundary_count: Annotated[int, Field(ge=0)]
    expected_duration_seconds: Annotated[float, Field(gt=0)]
    measured_duration_seconds: Annotated[float | None, Field(default=None, ge=0)] = None
    duration_within_tolerance: bool
    loudness_status: str
    loudness_target_lufs: float
    loudness_tolerance_lufs: Annotated[float, Field(gt=0)]
    measured_lufs: float | None
    ffmpeg_returncode: int
    output_path: str
    receiver_returncode: int | None = None
    receiver_output_path: str | None = None
    receiver_stderr_tail: str | None = None
    concat_plan_path: str
    boundary_events: tuple[EgressContinuityBoundary, ...]
    canonical_profile: dict[str, object]
    blocker: str | None
    next_step: str
    not_claimed: tuple[str, ...] = CONTINUITY_NOT_CLAIMED


def build_boundary_events(source_plan: EgressSourcePlan) -> tuple[EgressContinuityBoundary, ...]:
    """Return expected source boundaries from an ordered source plan."""

    elapsed = 0.0
    events: list[EgressContinuityBoundary] = []
    for index, segment in enumerate(source_plan.segments):
        if index > 0:
            events.append(
                EgressContinuityBoundary(
                    index=index,
                    source_label=segment.label,
                    expected_start_seconds=round(elapsed, 3),
                )
            )
        elapsed += segment.duration_seconds
    return tuple(events)


def run_filesink_continuity_proof(
    *,
    source_plan: EgressSourcePlan,
    config: EgressConfig,
    output_path: Path,
    work_dir: Path,
    duration_tolerance_seconds: float = 1.0,
    ffmpeg_runner: FfmpegRunner = run_ffmpeg,
) -> EgressContinuityProof:
    """Run one real FFmpeg FileSink proof and classify duration plus loudness."""

    if duration_tolerance_seconds < 0:
        raise ValueError("duration_tolerance_seconds must be zero or greater")
    if source_plan.channel_id != config.channel_id:
        raise ValueError(
            f"Source plan channel {source_plan.channel_id!r} does not match "
            f"egress config channel {config.channel_id!r}."
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    concat_plan_path = work_dir / config.channel_id / "continuity-proof.ffconcat"
    write_concat_plan(concat_plan_path, source_plan)
    proof_config = EgressConfig(
        **(
            config.model_dump()
            | {
                "sinks": [
                    EgressSinkSpec(
                        kind="file",
                        label="FileSink continuity proof",
                        uri=str(output_path),
                    ).model_dump()
                ]
            }
        )
    )
    args = build_persistent_encoder_args(concat_plan=concat_plan_path, config=proof_config)
    try:
        encode_result = ffmpeg_runner(args)
    except FfmpegNotFoundError as exc:
        return _failed_proof(
            source_plan=source_plan,
            config=config,
            output_path=output_path,
            concat_plan_path=concat_plan_path,
            ffmpeg_returncode=-1,
            blocker="EGRESS_CONTINUITY_FFMPEG_MISSING",
            next_step=str(exc),
        )

    expected_duration = sum(segment.duration_seconds for segment in source_plan.segments)
    measured_duration = probe_duration(output_path) if encode_result.returncode == 0 else None
    duration_ok = measured_duration is not None and abs(
        measured_duration - expected_duration
    ) <= max(
        duration_tolerance_seconds,
        expected_duration * 0.10,
    )
    loudness = (
        check_streaming_loudness(
            media_path=output_path,
            target_lufs=config.loudness_target_lufs,
            tolerance_lufs=config.loudness_tolerance_lufs,
        )
        if encode_result.returncode == 0 and output_path.exists()
        else LoudnessGateResult(
            status="failed",
            standard="ITU-R BS.1770 / EBU R128",
            target_lufs=config.loudness_target_lufs,
            used_ffmpeg_wrapper=True,
            measured_lufs=None,
            operator_action="Continuity output was not created; inspect FFmpeg output first.",
        )
    )
    blocker = _classify_blocker(
        ffmpeg_returncode=encode_result.returncode,
        duration_ok=duration_ok,
        loudness_status=loudness.status,
    )
    return EgressContinuityProof(
        status="PASS" if blocker is None else "FAIL",
        sink_kind="file",
        channel_id=source_plan.channel_id,
        proof_boundary=CONTINUITY_PROOF_BOUNDARY,
        boundary_count=max(0, len(source_plan.segments) - 1),
        expected_duration_seconds=round(expected_duration, 3),
        measured_duration_seconds=round(measured_duration, 3)
        if measured_duration is not None
        else None,
        duration_within_tolerance=duration_ok,
        loudness_status=loudness.status,
        loudness_target_lufs=config.loudness_target_lufs,
        loudness_tolerance_lufs=config.loudness_tolerance_lufs,
        measured_lufs=loudness.measured_lufs,
        ffmpeg_returncode=encode_result.returncode,
        output_path=str(output_path),
        concat_plan_path=str(concat_plan_path),
        boundary_events=build_boundary_events(source_plan),
        canonical_profile=config.canonical_profile.model_dump(),
        blocker=blocker,
        next_step=_next_step(blocker, loudness),
    )


def run_srt_receiver_continuity_proof(
    *,
    source_plan: EgressSourcePlan,
    config: EgressConfig,
    sender_url: str,
    receiver_url: str,
    receiver_output_path: Path,
    work_dir: Path,
    receiver_timeout_seconds: float = 10.0,
    receiver_startup_seconds: float = 1.0,
    duration_tolerance_seconds: float = 1.0,
    ffmpeg_runner: FfmpegRunner = run_ffmpeg,
) -> EgressContinuityProof:
    """Run sender and receiver FFmpeg processes for representative SRT proof."""

    if uri_looks_secret_bearing(sender_url) or uri_looks_secret_bearing(receiver_url):
        raise ValueError("SRT continuity proof URLs must not include secrets")
    if source_plan.channel_id != config.channel_id:
        raise ValueError(
            f"Source plan channel {source_plan.channel_id!r} does not match "
            f"egress config channel {config.channel_id!r}."
        )
    receiver_output_path.parent.mkdir(parents=True, exist_ok=True)
    if receiver_output_path.exists():
        receiver_output_path.unlink()
    concat_plan_path = work_dir / config.channel_id / "srt-continuity-proof.ffconcat"
    write_concat_plan(concat_plan_path, source_plan)
    proof_config = EgressConfig(
        **(
            config.model_dump()
            | {
                "sinks": [
                    _proof_srt_sink_from_config(
                        config=config,
                        sender_url=sender_url,
                    ).model_dump()
                ]
            }
        )
    )
    receiver_log_path = receiver_output_path.with_suffix(".receiver.stderr.log")
    receiver_mode, receiver_input_url = split_srt_receiver_options(receiver_url)
    receiver_args = [
        "-hide_banner",
        *(["-mode", receiver_mode] if receiver_mode else []),
        "-i",
        receiver_input_url,
        "-c",
        "copy",
        "-f",
        "mpegts",
        str(receiver_output_path),
    ]
    try:
        receiver = start_ffmpeg(receiver_args, stderr_path=receiver_log_path)
    except FfmpegNotFoundError as exc:
        return _failed_proof(
            source_plan=source_plan,
            config=config,
            output_path=receiver_output_path,
            concat_plan_path=concat_plan_path,
            ffmpeg_returncode=-1,
            blocker="EGRESS_CONTINUITY_FFMPEG_MISSING",
            next_step=str(exc),
            sink_kind="srt",
            receiver_returncode=-1,
            receiver_output_path=receiver_output_path,
            receiver_stderr_tail=None,
        )
    try:
        if receiver_startup_seconds > 0:
            import time

            time.sleep(receiver_startup_seconds)
        sender_result = ffmpeg_runner(
            build_persistent_encoder_args(concat_plan=concat_plan_path, config=proof_config)
        )
        receiver_returncode = _wait_for_receiver(receiver, timeout_seconds=receiver_timeout_seconds)
    finally:
        receiver.close()

    expected_duration = sum(segment.duration_seconds for segment in source_plan.segments)
    measured_duration = (
        probe_duration(receiver_output_path)
        if sender_result.returncode == 0 and receiver_output_path.exists()
        else None
    )
    duration_ok = measured_duration is not None and abs(
        measured_duration - expected_duration
    ) <= max(
        duration_tolerance_seconds,
        expected_duration * 0.10,
    )
    loudness = (
        check_streaming_loudness(
            media_path=receiver_output_path,
            target_lufs=config.loudness_target_lufs,
            tolerance_lufs=config.loudness_tolerance_lufs,
        )
        if sender_result.returncode == 0 and receiver_output_path.exists()
        else LoudnessGateResult(
            status="failed",
            standard="ITU-R BS.1770 / EBU R128",
            target_lufs=config.loudness_target_lufs,
            used_ffmpeg_wrapper=True,
            measured_lufs=None,
            operator_action="SRT receiver output was not created; inspect FFmpeg logs first.",
        )
    )
    blocker = _classify_srt_blocker(
        sender_returncode=sender_result.returncode,
        receiver_returncode=receiver_returncode,
        duration_ok=duration_ok,
        loudness_status=loudness.status,
    )
    return EgressContinuityProof(
        status="PASS" if blocker is None else "FAIL",
        sink_kind="srt",
        channel_id=source_plan.channel_id,
        proof_boundary=CONTINUITY_PROOF_BOUNDARY,
        boundary_count=max(0, len(source_plan.segments) - 1),
        expected_duration_seconds=round(expected_duration, 3),
        measured_duration_seconds=round(measured_duration, 3)
        if measured_duration is not None
        else None,
        duration_within_tolerance=duration_ok,
        loudness_status=loudness.status,
        loudness_target_lufs=config.loudness_target_lufs,
        loudness_tolerance_lufs=config.loudness_tolerance_lufs,
        measured_lufs=loudness.measured_lufs,
        ffmpeg_returncode=sender_result.returncode,
        output_path=sender_url,
        receiver_returncode=receiver_returncode,
        receiver_output_path=str(receiver_output_path),
        receiver_stderr_tail=_read_tail(receiver_log_path),
        concat_plan_path=str(concat_plan_path),
        boundary_events=build_boundary_events(source_plan),
        canonical_profile=config.canonical_profile.model_dump(),
        blocker=blocker,
        next_step=_next_step(blocker, loudness),
    )


def split_srt_receiver_options(url: str) -> tuple[str | None, str]:
    """Move receiver mode out of an SRT URL for Windows FFmpeg builds."""

    parsed = urlsplit(url)
    if parsed.scheme.lower() != "srt":
        return None, url
    query = parse_qsl(parsed.query, keep_blank_values=True)
    mode = None
    kept_query: list[tuple[str, str]] = []
    for key, value in query:
        if key.lower() == "mode" and mode is None:
            mode = value
        elif key.lower() == "latency":
            continue
        else:
            kept_query.append((key, value))
    if mode is None:
        return None, url
    return mode, urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(kept_query),
            parsed.fragment,
        )
    )


def _proof_srt_sink_from_config(*, config: EgressConfig, sender_url: str) -> EgressSinkSpec:
    """Return an SRT proof sink while preserving caller-selected SRT options."""

    template = next((sink for sink in config.sinks if sink.kind == "srt"), None)
    if template is None:
        return EgressSinkSpec(kind="srt", label="SRT continuity proof", uri=sender_url)
    return EgressSinkSpec(
        **(
            template.model_dump()
            | {
                "kind": "srt",
                "label": "SRT continuity proof",
                "uri": sender_url,
            }
        )
    )


def probe_duration(path: Path) -> float | None:
    """Measure output duration through ffprobe without making sink claims."""

    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    completed = subprocess.run(  # noqa: S603
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        return None
    try:
        return float(completed.stdout.strip())
    except ValueError:
        return None


def _failed_proof(
    *,
    source_plan: EgressSourcePlan,
    config: EgressConfig,
    output_path: Path,
    concat_plan_path: Path,
    ffmpeg_returncode: int,
    blocker: str,
    next_step: str,
    sink_kind: Literal["file", "srt"] = "file",
    receiver_returncode: int | None = None,
    receiver_output_path: Path | None = None,
    receiver_stderr_tail: str | None = None,
) -> EgressContinuityProof:
    expected_duration = sum(segment.duration_seconds for segment in source_plan.segments)
    return EgressContinuityProof(
        status="FAIL",
        sink_kind=sink_kind,
        channel_id=source_plan.channel_id,
        proof_boundary=CONTINUITY_PROOF_BOUNDARY,
        boundary_count=max(0, len(source_plan.segments) - 1),
        expected_duration_seconds=round(expected_duration, 3),
        duration_within_tolerance=False,
        loudness_status="failed",
        loudness_target_lufs=config.loudness_target_lufs,
        loudness_tolerance_lufs=config.loudness_tolerance_lufs,
        measured_lufs=None,
        ffmpeg_returncode=ffmpeg_returncode,
        output_path=str(output_path),
        receiver_returncode=receiver_returncode,
        receiver_output_path=str(receiver_output_path) if receiver_output_path else None,
        receiver_stderr_tail=receiver_stderr_tail,
        concat_plan_path=str(concat_plan_path),
        boundary_events=build_boundary_events(source_plan),
        canonical_profile=config.canonical_profile.model_dump(),
        blocker=blocker,
        next_step=next_step,
    )


def _classify_blocker(
    *,
    ffmpeg_returncode: int,
    duration_ok: bool,
    loudness_status: str,
) -> str | None:
    if ffmpeg_returncode != 0:
        return "EGRESS_CONTINUITY_FFMPEG_FAILED"
    if not duration_ok:
        return "EGRESS_CONTINUITY_DURATION_OUT_OF_TOLERANCE"
    if loudness_status != "ok":
        return "EGRESS_CONTINUITY_LOUDNESS_OUT_OF_TOLERANCE"
    return None


def _classify_srt_blocker(
    *,
    sender_returncode: int,
    receiver_returncode: int | None,
    duration_ok: bool,
    loudness_status: str,
) -> str | None:
    if sender_returncode != 0:
        return "EGRESS_CONTINUITY_SRT_SENDER_FAILED"
    if receiver_returncode != 0:
        return "EGRESS_CONTINUITY_SRT_RECEIVER_FAILED"
    if not duration_ok:
        return "EGRESS_CONTINUITY_DURATION_OUT_OF_TOLERANCE"
    if loudness_status != "ok":
        return "EGRESS_CONTINUITY_LOUDNESS_OUT_OF_TOLERANCE"
    return None


class _ReceiverProtocol(Protocol):
    process: object | None

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...


class _WaitableProcessProtocol(Protocol):
    returncode: int | None

    def wait(self, timeout: float | None = None) -> int | None: ...


def _wait_for_receiver(receiver: object, *, timeout_seconds: float) -> int | None:
    receiver_process = cast(_ReceiverProtocol, receiver)
    process = receiver_process.process
    if process is None:
        return receiver_process.poll()
    waitable_process = cast(_WaitableProcessProtocol, process)
    try:
        return waitable_process.wait(timeout=max(0.1, timeout_seconds))
    except subprocess.TimeoutExpired:
        receiver_process.terminate()
        return waitable_process.returncode


def _read_tail(path: Path, *, limit: int = 1200) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")[-limit:]


def _next_step(blocker: str | None, loudness: LoudnessGateResult) -> str:
    if blocker is None:
        return "Use this FileSink proof as CivicCast-controlled evidence, then run representative SRT receiver proof before broader continuity claims."
    if blocker == "EGRESS_CONTINUITY_LOUDNESS_OUT_OF_TOLERANCE":
        return loudness.operator_action
    if blocker == "EGRESS_CONTINUITY_DURATION_OUT_OF_TOLERANCE":
        return "Inspect the emitted transport stream duration and boundary plan before accepting continuity evidence."
    return "Inspect FFmpeg output and rerun the continuity proof after fixing the encoder path."
