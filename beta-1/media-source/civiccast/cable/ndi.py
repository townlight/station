# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from civiccast.stream._ffmpeg import FfmpegNotFoundError, FfmpegResult, run_ffmpeg

NDI_OUTPUT_SURFACE_ID = "cable-ndi-output"
NDI_SUPPORTED_MUXERS = ("libndi_newtek", "ndi")
DEFAULT_NDI_FRAMERATE = "30000/1001"
DEFAULT_NDI_VIDEO_SIZE = "1920x1080"


class NdiOutputError(RuntimeError):
    """Raised when an NDI output plan cannot be built safely."""


@dataclass(frozen=True)
class NdiReadinessResult:
    """Host readiness for NDI output through FFmpeg."""

    status: str
    supported_muxer: str | None
    ffmpeg_detected: bool
    ndi_runtime_detected: bool
    ndi_sdk_detected: bool
    ndi_sender_detected: bool
    ndi_sender_path: Path | None
    next_step: str


@dataclass(frozen=True)
class NdiOutputPlan:
    """A concrete FFmpeg NDI output plan for an integrator or proof runner."""

    status: str
    source_media: Path
    ndi_name: str
    ffmpeg_args: list[str]
    proof_boundary: str
    next_step: str


def _clean_ndi_name(value: str) -> str:
    if any(ch in value for ch in {"\x00", "\r", "\n"}):
        raise NdiOutputError(
            "NDI output channel names cannot contain control characters. "
            "Use a plain room or program name and retry."
        )
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned:
        raise NdiOutputError(
            "NDI output needs a channel name. Use a plain name such as "
            "'CivicCast Council Room' and retry."
        )
    return cleaned


def _format_supported(output: str) -> str | None:
    supported = set(NDI_SUPPORTED_MUXERS)
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in supported:
            return parts[1]
    for muxer in NDI_SUPPORTED_MUXERS:
        if re.search(rf"^E\s+{re.escape(muxer)}\b", output, flags=re.MULTILINE):
            return muxer
    return None


def _candidate_ndi_roots() -> list[Path]:
    roots: list[Path] = []
    for env_name in ("NDI_SDK_DIR", "NDI_SDK_HOME", "NDI_RUNTIME_DIR"):
        value = os.environ.get(env_name)
        if value:
            roots.append(Path(value))
    roots.extend(
        [
            Path("C:/Program Files/NDI"),
            Path("C:/Program Files/NewTek"),
            Path("C:/Program Files (x86)/NDI"),
            Path("C:/Program Files (x86)/NewTek"),
        ]
    )
    return roots


def detect_ndi_runtime() -> bool:
    """Return whether a local NDI runtime DLL is visible on this host."""

    for root in _candidate_ndi_roots():
        if not root.exists():
            continue
        if any(root.rglob("Processing.NDI.Lib.x64.dll")):
            return True
    return False


def detect_ndi_sdk() -> bool:
    """Return whether local NDI SDK build inputs are visible on this host."""

    for root in _candidate_ndi_roots():
        if not root.exists():
            continue
        has_header = any(root.rglob("Processing.NDI.Lib.h"))
        has_import_lib = any(root.rglob("Processing.NDI.Lib*.lib"))
        if has_header and has_import_lib:
            return True
    return False


def _candidate_ndi_sender_paths() -> list[Path]:
    paths: list[Path] = []
    value = os.environ.get("CIVICCAST_NDI_SENDER")
    if value:
        paths.append(Path(value))
    repo_root = Path(__file__).resolve().parents[2]
    extension = ".exe" if os.name == "nt" else ""
    paths.extend(
        [
            repo_root
            / "tools"
            / "ndi-ffmpeg-sender"
            / "target"
            / "release"
            / f"civiccast-ndi-ffmpeg-sender{extension}",
            repo_root
            / "tools"
            / "ndi-ffmpeg-sender"
            / "target"
            / "debug"
            / f"civiccast-ndi-ffmpeg-sender{extension}",
        ]
    )
    return paths


def detect_ndi_sender() -> Path | None:
    """Return a local lab sender shim when it is available."""

    for path in _candidate_ndi_sender_paths():
        if path.exists() and path.is_file():
            return path
    return None


def check_ndi_runtime(
    *,
    ffmpeg_runner: Callable[[list[str]], FfmpegResult] = run_ffmpeg,
    ndi_sender_detector: Callable[[], Path | None] = detect_ndi_sender,
) -> NdiReadinessResult:
    """Return whether this host has an FFmpeg build with an NDI output muxer."""

    ndi_runtime_detected = detect_ndi_runtime()
    ndi_sdk_detected = detect_ndi_sdk()
    ndi_sender_path = ndi_sender_detector()
    try:
        result = ffmpeg_runner(["-hide_banner", "-muxers"])
    except FfmpegNotFoundError:
        return NdiReadinessResult(
            status="runtime_unavailable",
            supported_muxer=None,
            ffmpeg_detected=False,
            ndi_runtime_detected=ndi_runtime_detected,
            ndi_sdk_detected=ndi_sdk_detected,
            ndi_sender_detected=ndi_sender_path is not None,
            ndi_sender_path=ndi_sender_path,
            next_step=(
                "Install FFmpeg, then rerun `civiccast cable ndi-check`. "
                "NDI output also requires an FFmpeg build with an NDI muxer."
            ),
        )
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        return NdiReadinessResult(
            status="runtime_unavailable",
            supported_muxer=None,
            ffmpeg_detected=True,
            ndi_runtime_detected=ndi_runtime_detected,
            ndi_sdk_detected=ndi_sdk_detected,
            ndi_sender_detected=ndi_sender_path is not None,
            ndi_sender_path=ndi_sender_path,
            next_step=(
                "FFmpeg was found but could not list muxers. Repair FFmpeg, then rerun "
                "`civiccast cable ndi-check`."
            ),
        )
    supported = _format_supported(output)
    if supported is None:
        if ndi_sender_path is not None:
            return NdiReadinessResult(
                status="ndi_sender_ready",
                supported_muxer=None,
                ffmpeg_detected=True,
                ndi_runtime_detected=ndi_runtime_detected,
                ndi_sdk_detected=ndi_sdk_detected,
                ndi_sender_detected=True,
                ndi_sender_path=ndi_sender_path,
                next_step=(
                    "Run the local `civiccast-ndi-ffmpeg-sender` lab tool with Studio Monitor "
                    "or another receiver. This proves FFmpeg-decoded media can be published "
                    "to NDI on this workstation, but it is not an FFmpeg muxer build."
                ),
            )
        return NdiReadinessResult(
            status="ndi_muxer_missing",
            supported_muxer=None,
            ffmpeg_detected=True,
            ndi_runtime_detected=ndi_runtime_detected,
            ndi_sdk_detected=ndi_sdk_detected,
            ndi_sender_detected=False,
            ndi_sender_path=None,
            next_step=(
                "Install or build an FFmpeg binary with NDI output support, then rerun "
                "`civiccast cable ndi-check`. This host cannot prove live NDI output yet. "
                + (
                    "NDI runtime was detected, but NDI SDK headers/import libraries were not; "
                    "a local FFmpeg-with-NDI build needs the SDK, not just NDI Tools."
                    if ndi_runtime_detected and not ndi_sdk_detected
                    else "NDI SDK/runtime inputs were detected; verify the FFmpeg build flags."
                    if ndi_sdk_detected
                    else "Install NDI Runtime or SDK inputs before building an NDI-capable FFmpeg."
                )
            ),
        )
    return NdiReadinessResult(
        status="ok",
        supported_muxer=supported,
        ffmpeg_detected=True,
        ndi_runtime_detected=ndi_runtime_detected,
        ndi_sdk_detected=ndi_sdk_detected,
        ndi_sender_detected=ndi_sender_path is not None,
        ndi_sender_path=ndi_sender_path,
        next_step=(
            "Run the generated NDI output command against an NDI receiver or monitor and "
            "record receiver-side proof before claiming live NDI delivery."
        ),
    )


def build_ndi_output_plan(
    *,
    source_media: Path,
    ndi_name: str,
    muxer: str = "libndi_newtek",
    framerate: str = DEFAULT_NDI_FRAMERATE,
    video_size: str = DEFAULT_NDI_VIDEO_SIZE,
    realtime: bool = True,
) -> NdiOutputPlan:
    """Build the FFmpeg argument list for local-file-to-NDI output."""

    if muxer not in NDI_SUPPORTED_MUXERS:
        raise NdiOutputError(
            f"Unsupported NDI muxer '{muxer}'. Expected one of: {', '.join(NDI_SUPPORTED_MUXERS)}."
        )
    if not source_media.exists() or not source_media.is_file():
        raise NdiOutputError(
            f"NDI output cannot start because the source media is missing: {source_media}. "
            "Create or select a local recording file, then retry."
        )
    clean_name = _clean_ndi_name(ndi_name)
    args: list[str] = []
    if realtime:
        args.append("-re")
    args.extend(
        [
            "-i",
            str(source_media),
            "-vf",
            f"scale={video_size},fps={framerate}",
            "-pix_fmt",
            "uyvy422",
            "-f",
            muxer,
            clean_name,
        ]
    )
    return NdiOutputPlan(
        status="planned",
        source_media=source_media,
        ndi_name=clean_name,
        ffmpeg_args=args,
        proof_boundary="command-plan-and-runtime-readiness",
        next_step=(
            "Run this command only on a host with an NDI-capable FFmpeg build and an NDI "
            "receiver/monitor. Capture receiver proof before treating NDI delivery as proven."
        ),
    )
