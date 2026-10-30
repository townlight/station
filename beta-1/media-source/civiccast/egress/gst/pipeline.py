# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Pure pipeline-description builders for the S15 GStreamer playout engine.

No ``gi``/``Gst`` import lives here: these helpers assemble ``gst-launch``-style
description fragments as plain strings, so they are unit-testable on any platform
(Windows included). Live pipeline construction/execution lives in
``civiccast.egress.gst.engine`` (imports ``gi``; available on native
Windows through the pinned ``gstreamer-*`` wheels).

Swap mechanism (Stage-0 decision, 2026-06-14): ``input-selector`` (core GStreamer).
``build_playout_pipeline_desc`` keeps the selector pluggable so a GstInterpipe
variant can drop in later behind the same builder seam.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from civiccast.egress.models import EgressSinkSpec

_VIDEO_CAPS = "video/x-raw,width={w},height={h},framerate={fps}/1"
DEFAULT_H264_ENCODER = "openh264enc"


@dataclass(frozen=True)
class EncodeProfile:
    """Minimal encode parameters the pipeline builder needs.

    The bundled public-beta base/CPU tier uses ``openh264enc``. Operators who supply
    GPL x264 themselves can still select ``x264enc`` explicitly; hardware encoders
    (``nvh264enc``/``vah264enc``) substitute via ``encoder`` per tier.
    """

    width: int = 1280
    height: int = 720
    fps: int = 30
    video_bitrate_kbps: int = 4000
    gop_size: int = 60
    encoder: str = DEFAULT_H264_ENCODER


@dataclass(frozen=True)
class PlayoutSource:
    """One hot-swappable source feeding the selector.

    ``upstream`` is a raw-video-producing element chain already conformed to the
    channel's common caps (S15 §3 glitch-free-swap rule), e.g.
    ``"filesrc location=/m/clip.ts ! decodebin ! videoconvert"``.
    """

    label: str
    upstream: str


def _is_multicast(host: str) -> bool:
    if ":" in host:  # IPv6 — the multicast block is ff00::/8
        return host.lower().startswith("ff")
    first = host.split(".", 1)[0]
    return first.isdigit() and 224 <= int(first) <= 239


def _file_location(uri: str) -> str:
    # The engine runs under WSL (posix); keep the URL path verbatim rather than
    # OS-normalising, so descriptions are deterministic across Windows/Linux.
    parsed = urlsplit(uri)
    if parsed.scheme == "file":
        return parsed.path
    return uri


def gst_sink_element(spec: EgressSinkSpec) -> str:
    """Return the terminal GStreamer sink element for an egress target.

    RTMP's returned element is used after its per-target ``flvmux`` branch.
    """
    kind = spec.kind
    if kind == "file":
        return f"filesink location={_file_location(spec.uri)}"
    if kind in {"udp-ts", "local-ts"}:
        parsed = urlsplit(spec.uri)
        if parsed.scheme == "file":
            return f"filesink location={_file_location(spec.uri)}"
        host = parsed.hostname or "127.0.0.1"
        if parsed.port is None:
            raise ValueError(f"{kind} sink requires an explicit port: {spec.uri}")
        element = f"udpsink host={host} port={parsed.port}"
        if _is_multicast(host):
            element += " auto-multicast=true"
        return element
    if kind == "srt":
        return f'srtsink uri="{spec.uri}"'
    if kind == "rtmp":
        if spec.secret_ref is not None:
            raise ValueError(
                "rtmp stream keys are credential handles and cannot be rendered into a "
                "launch string; use the structured GStreamer output graph"
            )
        uri = spec.uri.replace("\\", "\\\\").replace('"', '\\"')
        return f'rtmp2sink location="{uri}"'
    if kind == "sdi":
        raise ValueError(
            "sdi output is a pre-mux decklinkvideosink branch (SDI tier), not a TS sink"
        )
    if kind == "hls":
        raise ValueError(
            "hls output is an ffmpeg '-f hls' muxer branch (civiccast.egress.sinks.HlsSink), "
            "not a GStreamer TS-mux element — the persistent ffmpeg encoder handles it directly"
        )
    raise ValueError(f"unknown sink kind: {kind}")


def _parser_for(encoder: str) -> str | None:
    """The bitstream parser that pairs with an encoder for in-band codec config.

    Headend/set-top mid-stream tune-in needs SPS/PPS (or VPS/SPS/PPS) repeated
    in-band; the parser emits it with ``config-interval=-1`` (config before every
    IDR). Returns ``None`` for encoders that need no parser before mpegtsmux.
    """
    lowered = encoder.lower()
    if "265" in lowered or "hevc" in lowered:
        return "h265parse"
    if "264" in lowered or "avc" in lowered:
        return "h264parse"
    return None


def encoder_chain(profile: EncodeProfile) -> str:
    """The conform→encode→parse chain feeding the muxer (base/CPU tier default).

    Mirrors ``graph.encode_chain_specs`` and, like it, carries NO ``videorate``:
    each source subchain conforms its own stream to the target framerate before
    the selector, so a second one at this hop would only be the element that
    carries its output grid frontier across a changeover (U56 round 7).
    """
    caps = _VIDEO_CAPS.format(w=profile.width, h=profile.height, fps=profile.fps)
    if profile.encoder == "x264enc":
        enc = (
            f"x264enc tune=zerolatency bitrate={profile.video_bitrate_kbps} "
            f"key-int-max={profile.gop_size}"
        )
    else:
        # hardware/alt encoders share the kbps ``bitrate`` property name
        enc = f"{profile.encoder} bitrate={profile.video_bitrate_kbps}"
    chain = f"videoconvert ! videoscale ! {caps} ! {enc}"
    parser = _parser_for(profile.encoder)
    if parser is not None:
        # config-interval=-1 → codec config before every IDR, so a set-top tuning
        # mid-stream at the headend decodes without waiting for the next GOP.
        chain += f" ! {parser} config-interval=-1"
    return chain


def build_playout_pipeline_desc(
    *,
    sources: Sequence[PlayoutSource],
    profile: EncodeProfile,
    sinks: Sequence[EgressSinkSpec],
    selector_name: str = "sel",
) -> str:
    """Assemble the persistent playout pipeline description.

    Persistent output half (stays PLAYING): selector → encode → mpegtsmux → sink(s).
    Source halves (hot-swappable): each ``PlayoutSource.upstream`` feeds a selector
    sink pad. The mux never restarts across swaps → MPEG-TS continuity is unbroken
    (the #151 fix, validated in Stage 0).
    """
    if not sources:
        raise ValueError("at least one playout source is required")
    if not sinks:
        raise ValueError("at least one egress sink is required")

    rtmp_sinks = [spec for spec in sinks if spec.kind == "rtmp"]
    ts_sinks = [spec for spec in sinks if spec.kind != "rtmp"]
    if not rtmp_sinks:
        head = (
            f"input-selector name={selector_name} ! {encoder_chain(profile)} ! mpegtsmux name=mux"
        )
        sink_elements = [gst_sink_element(spec) for spec in ts_sinks]
        if len(sink_elements) == 1:
            output = f"mux. ! queue ! {sink_elements[0]}"
        else:
            output = "mux. ! tee name=t " + " ".join(
                f"t. ! queue ! {element}" for element in sink_elements
            )
    else:
        # This pure description helper carries video only. The active structured
        # graph adds the corresponding per-output AAC chain before each FLV mux.
        # Keep a separate mux for TS and a branch-local FLV mux for each RTMP
        # target, with the common encoded video tee shared across them.
        head = (
            f"input-selector name={selector_name} ! {encoder_chain(profile)} ! "
            "tee name=encoded_video"
        )
        outputs: list[str] = []
        if ts_sinks:
            if len(ts_sinks) == 1:
                outputs.append(
                    "encoded_video. ! queue ! mpegtsmux name=mux ! queue ! "
                    + gst_sink_element(ts_sinks[0])
                )
            else:
                outputs.append("encoded_video. ! queue ! mpegtsmux name=mux")
                outputs.append(
                    "mux. ! tee name=t "
                    + " ".join(f"t. ! queue ! {gst_sink_element(spec)}" for spec in ts_sinks)
                )
        for index, spec in enumerate(rtmp_sinks):
            outputs.append(
                f"encoded_video. ! queue ! flvmux name=flv_{index} streamable=true ! "
                f"{gst_sink_element(spec)}"
            )
        output = " ".join(outputs)

    source_legs = " ".join(
        f"{source.upstream} ! {selector_name}.sink_{index}" for index, source in enumerate(sources)
    )

    return f"{head} {output} {source_legs}"
