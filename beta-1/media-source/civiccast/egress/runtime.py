# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Runtime planning helpers for the channel egress encoder."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

from civiccast.captions.tap import AudioTapPlan
from civiccast.egress.branding import EgressBrandingPlan
from civiccast.egress.caption_embed import EgressCaptionEmbeddingPlan
from civiccast.egress.errors import EgressError
from civiccast.egress.loudness_plan import SinkLoudnessResolution, build_loudness_plan
from civiccast.egress.models import CanonicalProfile, EgressConfig, EgressSourcePlan
from civiccast.egress.sinks import SecretResolver, build_sink
from civiccast.stream._ffmpeg import FfmpegProcessHandle, FfmpegResult, run_ffmpeg, start_ffmpeg

FfmpegRunner = Callable[[list[str]], FfmpegResult]
FfmpegStarter = Callable[[list[str]], FfmpegProcessHandle]


def write_concat_plan(path: Path, source_plan: EgressSourcePlan) -> None:
    """Write an ffconcat plan for already-conformed MPEG-TS source segments."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["ffconcat version 1.0"]
    for segment in source_plan.segments:
        lines.append(f"file '{_escape_ffconcat_path(_ffconcat_source(segment.path))}'")
        if segment.inpoint_seconds is not None:
            lines.append(f"inpoint {segment.inpoint_seconds:g}")
        if segment.outpoint_seconds is not None:
            lines.append(f"outpoint {segment.outpoint_seconds:g}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class EgressLoudnessError(EgressError):
    """A per-sink loudness target cannot be honoured on this encode path.

    Subclasses ``EgressError`` so the daemon's start guard catches it and writes
    a clean operator-visible ERROR state (with this message) instead of letting
    the misconfiguration crash the encode loop.
    """


_REALTIME_SINK_KINDS = {"srt", "rtmp", "local-ts", "udp-ts", "hls"}


def build_persistent_encoder_args(
    *,
    concat_plan: Path,
    config: EgressConfig,
    resolve_secret: SecretResolver | None = None,
    branding_plan: EgressBrandingPlan | None = None,
    caption_plan: EgressCaptionEmbeddingPlan | None = None,
    audio_tap_plan: AudioTapPlan | None = None,
) -> list[str]:
    """Build FFmpeg args for one persistent concat input and configured sinks.

    ``audio_tap_plan`` (Beta B6, caption tap option A) adds one extra output
    to the same process: rolling mono 16 kHz WAV segments the caption tap
    worker consumes — egress stays the single owner of the media graph.

    S11b per-sink loudness: when every sink resolves to the channel's conform
    baseline (the default — all ``inherit``), this returns the historical
    single-mapping ``-c copy`` graph unchanged. When a sink's resolved target
    diverges (e.g. a cable ``atsc-a85`` -24 LKFS sink alongside a -16 LUFS
    streaming sink), each sink gets its own output group and the divergent
    sink's audio is re-normalised with ``loudnorm`` instead of copied.
    """

    plan = build_loudness_plan(config)
    # A sink needs its own output group when it diverges on LOUDNESS or needs the EAS
    # attention-tone notch (S11c gap-B) — both live in _per_sink_audio_args, which the
    # historical single-mapping path never reaches (a baseline OTT sink would otherwise
    # silently ship the tone, FCC Sec. 11.31). Track the two needs separately: only
    # loudness divergence conflicts with channel branding (a tone-strip is audio-only).
    # _OTT_SINK_KINDS is a module global resolved at call time.
    needs_loudness_per_sink = any(resolution.requires_reencode for resolution in plan.sinks)
    needs_tone_strip = any(
        resolution.eas_tone_strip_enabled and resolution.kind in _OTT_SINK_KINDS
        for resolution in plan.sinks
    )
    # Every sink wants the EAS notch — only then can a SHARED audio mapping (the branding
    # path) strip it, because a band-reject on the shared audio would otherwise wrongly
    # notch a cable sink whose audio must pass through untouched to the certified headend.
    all_tone_strip = bool(plan.sinks) and all(
        resolution.eas_tone_strip_enabled and resolution.kind in _OTT_SINK_KINDS
        for resolution in plan.sinks
    )
    needs_per_sink = needs_loudness_per_sink or needs_tone_strip

    has_realtime_sink = any(spec.kind in _REALTIME_SINK_KINDS for spec in config.sinks)
    input_args = ["-hide_banner", "-loglevel", "warning"]
    if has_realtime_sink:
        input_args.append("-re")
    input_args.extend(["-f", "concat", "-safe", "0", "-i", str(concat_plan)])
    if caption_plan is not None:
        input_args.extend(caption_plan.input_args)

    # Branding re-encodes ONE filter_complex video that cannot fan out to per-sink
    # LOUDNESS groups, so refuse that specific combination (fail-closed). A tone-strip
    # is audio-only and does NOT need per-sink video, so it must NOT trigger this refusal.
    if branding_plan is not None and needs_loudness_per_sink:
        raise EgressLoudnessError(
            "Per-sink loudness divergence is not supported together with channel "
            "branding on the ffmpeg encode path. Normalise all sinks to one "
            "loudness regime, or run branding without per-sink overrides."
        )
    # A tone-strip is per-sink, but the branding path has ONE shared audio mapping, so it
    # can only honor tone-strip when EVERY sink wants it. A branded channel with divergent
    # tone-strip needs (e.g. an OTT sink that strips alongside a cable sink that must pass
    # the headend's signal through) is unexpressible here — refuse (fail-closed) rather
    # than silently ship the EAS attention tone on the OTT sink (FCC Sec. 11.31). The gst
    # engine muxes per-sink audio and handles this mix.
    if branding_plan is not None and needs_tone_strip and not all_tone_strip:
        raise EgressLoudnessError(
            "Channel branding cannot apply a per-sink EAS tone-strip when sinks diverge "
            "on tone-strip need (some strip the 853/960 Hz attention tone, others must "
            "pass it through). Use one tone-strip posture across the channel's sinks, or "
            "the GStreamer engine, which muxes per-sink audio."
        )

    # Branding forces the single shared video mapping, so it always uses the
    # single-mapping path (never per-sink). Per-sink output groups apply only WITHOUT
    # branding. On the branding path, the EAS notch is applied to the shared audio iff
    # every sink wants it (guaranteed by the divergent-tone-strip refusal above).
    if branding_plan is not None or not needs_per_sink:
        output_args: list[str] = []
        for sink_spec in config.sinks:
            sink = build_sink(sink_spec, resolve_secret=resolve_secret)
            output_args.extend(sink.output_args())
        if audio_tap_plan is not None:
            output_args.extend(audio_tap_plan.output_args())
        input_args.extend(
            _stream_mapping_args(
                config=config,
                branding_plan=branding_plan,
                caption_plan=caption_plan,
                tone_strip=branding_plan is not None and all_tone_strip,
            )
        )
        return [*input_args, *output_args]

    # Per-sink path (no branding): each sink gets its own output group so ffmpeg applies
    # that sink's audio handling — copy at baseline, loudnorm when the target diverges,
    # plus the EAS notch on OTT tone-strip sinks.
    profile = config.canonical_profile
    caption_stream_args = caption_plan.stream_args if caption_plan is not None else []
    resolutions = {resolution.label: resolution for resolution in plan.sinks}
    output_args = []
    for sink_spec in config.sinks:
        sink = build_sink(sink_spec, resolve_secret=resolve_secret)
        resolution = resolutions[sink_spec.label]
        output_args.extend(["-map", "0:v:0?", "-map", "0:a:0?"])
        output_args.extend(["-c:v", "copy"])
        output_args.extend(_per_sink_audio_args(resolution, profile))
        output_args.extend(caption_stream_args)
        # container_args(), not output_args(): the audio decision for this group
        # is already made above (_per_sink_audio_args), and a sink's args land
        # last, so a sink that states its own -c:a/-c:v would override it by
        # position -- fatal when the group carries -filter:a, because ffmpeg
        # refuses a filter next to a stream copy ("Filtering and streamcopy
        # cannot be used together").
        output_args.extend(sink.container_args())
    if audio_tap_plan is not None:
        output_args.extend(audio_tap_plan.output_args())
    return [*input_args, *output_args]


# S11c gap-B: a band-reject notch at the two EAS attention-tone frequencies (853 +
# 960 Hz, sent together). FCC Sec. 11.31 — CivicCast is not the certified EAS
# originator, so it must not rebroadcast the attention signal on its OTT outputs.
_EAS_TONE_NOTCH = "bandreject=f=853:width_type=h:w=40,bandreject=f=960:width_type=h:w=40"
# OTT (streaming) sink kinds the tone-strip applies to. Cable (udp-ts) / SDI / file
# pass audio through untouched — the certified headend owns the Part 11 signal.
_OTT_SINK_KINDS = ("srt", "rtmp")


def _per_sink_audio_args(
    resolution: SinkLoudnessResolution,
    profile: CanonicalProfile,
) -> list[str]:
    """Audio codec/filter args for one sink's output group.

    Baseline sinks copy the shared program audio; a sink whose loudness target
    diverges gets a per-output ``loudnorm`` re-encode to its effective target (parity
    decision 1 — per-destination loudness). An OTT sink with ``eas_tone_strip_enabled``
    additionally gets the 853/960 Hz EAS-tone notch (S11c gap-B); cable/file sinks are
    never tone-stripped, so they stay byte-for-byte stream-copied at baseline.
    """

    tone_strip = resolution.eas_tone_strip_enabled and resolution.kind in _OTT_SINK_KINDS
    filters: list[str] = []
    if resolution.requires_reencode:
        filters.append(f"loudnorm=I={resolution.effective_target_lufs:g}:LRA=11:TP=-1.5")
    if tone_strip:
        filters.append(_EAS_TONE_NOTCH)
    if not filters:
        return ["-c:a", "copy"]
    return [
        "-filter:a",
        ",".join(filters),
        "-c:a",
        profile.audio_codec,
        "-b:a",
        f"{profile.audio_bitrate_kbps}k",
        "-ar",
        str(profile.audio_sample_rate),
        "-ac",
        str(profile.audio_channels),
    ]


def _stream_mapping_args(
    *,
    config: EgressConfig,
    branding_plan: EgressBrandingPlan | None,
    caption_plan: EgressCaptionEmbeddingPlan | None,
    tone_strip: bool = False,
) -> list[str]:
    caption_stream_args = caption_plan.stream_args if caption_plan is not None else []
    if branding_plan is None:
        return ["-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", *caption_stream_args]

    profile = config.canonical_profile
    # Branding re-encodes the video; the audio is copied unless this branded channel's
    # sinks all want the EAS attention-tone notch (S11c gap-B), in which case the shared
    # audio is re-encoded through the 853/960 Hz band-reject.
    audio_args = (
        [
            "-filter:a",
            _EAS_TONE_NOTCH,
            "-c:a",
            profile.audio_codec,
            "-b:a",
            f"{profile.audio_bitrate_kbps}k",
            "-ar",
            str(profile.audio_sample_rate),
            "-ac",
            str(profile.audio_channels),
        ]
        if tone_strip
        else ["-c:a", "copy"]
    )
    return [
        "-filter_complex",
        branding_plan.filter_complex,
        "-map",
        f"[{branding_plan.output_video_label}]",
        "-map",
        "0:a:0?",
        "-c:v",
        profile.video_codec,
        "-b:v",
        f"{profile.video_bitrate_kbps}k",
        "-g",
        str(profile.gop_size),
        "-pix_fmt",
        "yuv420p",
        *audio_args,
        *caption_stream_args,
    ]


def run_persistent_encoder(
    args: list[str],
    *,
    ffmpeg_runner: FfmpegRunner = run_ffmpeg,
) -> FfmpegResult:
    """Run the configured FFmpeg command through the approved wrapper seam."""

    return ffmpeg_runner(args)


def start_persistent_encoder(
    args: list[str],
    *,
    ffmpeg_starter: FfmpegStarter = start_ffmpeg,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> FfmpegProcessHandle:
    """Start the configured FFmpeg command without blocking the service loop."""

    if ffmpeg_starter is start_ffmpeg:
        return start_ffmpeg(args, stdout_path=stdout_path, stderr_path=stderr_path)
    return ffmpeg_starter(args)


def _escape_ffconcat_path(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _ffconcat_source(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme and not _looks_like_windows_path(value) and parsed.scheme.lower() != "file":
        return value
    return Path(value).resolve().as_posix()


def _looks_like_windows_path(value: str) -> bool:
    return bool(PureWindowsPath(value).drive)
