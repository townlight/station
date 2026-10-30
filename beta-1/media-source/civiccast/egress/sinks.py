# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Output sink argument builders for channel egress."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from civiccast.egress.errors import SecretUnresolvedError
from civiccast.egress.models import EgressSinkSpec

SecretResolver = Callable[[str], str | None]


def _file_uri_path(uri: str) -> Path:
    """Resolve a ``file://`` URI to a local Path, Windows-drive aware.

    ``urlsplit`` keeps a leading "/" before a Windows drive letter
    (``file:///C:/x`` -> ``"/C:/x"``); ``Path("/C:/x")`` then resolves against
    the current drive root instead of ``C:\\x``. Shared by HlsSink, LocalTsSink,
    and FileSink (1.0-hardening: the siblings previously carried this gap —
    only HlsSink had the fix, inline).
    """
    raw = urlsplit(uri).path
    if len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    return Path(raw)


class EgressSink:
    """Base class for FFmpeg output-side sink adapters.

    ``carries_video``/``carries_audio`` declare which stream kinds this sink's
    program is expected to carry. The HLS relay consumes both
    (``civiccast.egress.hls_relay``): its argv REQUIRES every declared kind in
    the output mapping, and its post-(re)start verification requires every
    declared kind in the newest served segment. A half-program window is a
    fault in either direction -- U13 measured the government relay serving
    VIDEO-ONLY segments (TS PID 256, no PID 257) at 18:47:44-18:47:56, the
    mirror image of the audio-only lock U12 fixed, and a municipal channel with
    picture and no sound fails acceptance exactly like one with sound and no
    picture.
    """

    #: Whether this sink's program carries the channel's video stream.
    #:
    #: Declared rather than inferred because the relay's argv REQUIRES video
    #: (``-map 0:v:0``) for a video-carrying sink -- a relay child that probes
    #: an input with no video then fails fast instead of silently emitting
    #: audio-only HLS forever (beta.10 U12: the live government relay locked to
    #: audio-only after an 18:46:32 mid-stream restart) -- and requiring video
    #: is wrong for an audio-only sink. Every sink shipped today carries video
    #: (there is no audio-only egress sink mode, and ``extra_output_args``
    #: cannot express ``-vn``), so this defaults True; an audio-only variant
    #: overrides it False and gets the relay's historical argv byte-for-byte.
    carries_video: bool = True

    #: Whether this sink's program carries the channel's audio stream. Read
    #: only for a sink that also carries video (an audio-only sink is never
    #: judged on video, and needs no map to keep its audio). True for every
    #: sink shipped today; there is no video-only egress sink mode either, and
    #: ``extra_output_args`` cannot express ``-an``. A video-only variant
    #: overrides it False: the relay then leaves audio optional rather than
    #: fatal, and never restarts it for a missing audio stream.
    carries_audio: bool = True

    def __init__(
        self,
        spec: EgressSinkSpec,
        *,
        resolve_secret: SecretResolver | None = None,
    ) -> None:
        self.spec = spec
        self._resolve_secret = resolve_secret or (lambda _ref: None)

    def connect_target(self) -> str:
        return self.spec.uri

    def output_args(self) -> list[str]:
        raise NotImplementedError

    def container_args(self) -> list[str]:
        """The sink's output args with no codec decision of its own.

        ``runtime.build_persistent_encoder_args`` appends a sink's args at the
        END of an output group, so a sink that states ``-c:a``/``-c:v`` wins by
        position over anything the caller emitted earlier in that group. On the
        per-sink loudness path the caller has already chosen the codec (it emits
        ``-filter:a loudnorm`` + ``-c:a <profile codec>``, or a bare
        ``-c:a copy``), and a sink-imposed choice there is not merely redundant:
        ffmpeg refuses the whole output when a filter and a stream copy meet in
        one group ("Filtering and streamcopy cannot be used together"). This
        method lets that caller take the container half of the argv and keep the
        decision.

        Default: the sink states no codec choice, so its output args ARE its
        container args and the per-sink path loses nothing. A sink that does
        state one (``HlsSink``) overrides this to exclude the codec pair.
        """
        return self.output_args()

    def is_connected(self) -> bool:
        return False

    def describe(self) -> str:
        secret_label = " with secret ref" if self.spec.secret_ref else ""
        return f"{self.spec.kind} sink {self.spec.label} -> {self.spec.uri}{secret_label}"

    def _secret_value(self) -> str | None:
        if self.spec.secret_ref is None:
            return None
        value = self._resolve_secret(self.spec.secret_ref)
        if not value:
            raise SecretUnresolvedError(f"secret ref {self.spec.secret_ref} is not resolved")
        return value


class SrtSink(EgressSink):
    """SRT caller sink."""

    def connect_target(self) -> str:
        uri = _append_query_defaults(
            self.spec.uri,
            {
                "mode": "caller",
                "latency": str(self.spec.latency_ms * 1000),
                "linger": "5",
            },
        )
        passphrase = self._secret_value()
        if passphrase is not None:
            uri = _append_query_defaults(uri, {"passphrase": passphrase})
        return uri

    def output_args(self) -> list[str]:
        return [*self.spec.extra_output_args, "-f", "mpegts", self.connect_target()]

    def describe(self) -> str:
        uri = _append_query_defaults(
            self.spec.uri,
            {
                "mode": "caller",
                "latency": str(self.spec.latency_ms * 1000),
                "linger": "5",
            },
        )
        if self.spec.secret_ref is not None:
            uri = _append_query_defaults(uri, {"passphrase": "<redacted>"})
        return f"srt sink {self.spec.label} -> {_redact_query(uri)}"


class RtmpSink(EgressSink):
    """RTMP/RTMPS sink for platform restreams and appliances."""

    def connect_target(self) -> str:
        stream_key = self._secret_value()
        if stream_key is None:
            return self.spec.uri
        return f"{self.spec.uri.rstrip('/')}/{stream_key}"

    def output_args(self) -> list[str]:
        return [*self.spec.extra_output_args, "-f", "flv", self.connect_target()]

    def describe(self) -> str:
        target = self.spec.uri.rstrip("/")
        suffix = "/<secret>" if self.spec.secret_ref else ""
        return f"rtmp sink {self.spec.label} -> {target}{suffix}"


class LocalTsSink(EgressSink):
    """Local MPEG-TS sink for UDP or same-host appliance handoff."""

    def connect_target(self) -> str:
        parsed = urlsplit(self.spec.uri)
        if parsed.scheme == "file":
            return str(_file_uri_path(self.spec.uri))
        return self.spec.uri

    def output_args(self) -> list[str]:
        return [*self.spec.extra_output_args, "-f", "mpegts", self.connect_target()]

    def describe(self) -> str:
        return f"local-ts sink {self.spec.label} -> {self.connect_target()}"


class UdpTsSink(EgressSink):
    """Headend-grade SPTS over UDP (cable automation CA-6).

    Cable headends (Comcast MTD, TelVue HyperCaster, Harmonic Spectrum)
    ingest a constant-multiplex-rate single-program transport stream over
    UDP unicast or multicast. The constant rate itself rides the sink's
    allowlisted ``-muxrate`` extra arg (the mpegts muxer null-pads), and the
    datagram payload defaults to 1316 bytes — seven 188-byte TS packets —
    unless the operator pinned a ``pkt_size`` on the URI themselves.
    """

    default_pkt_size = 1316

    def connect_target(self) -> str:
        return _append_query_defaults(self.spec.uri, {"pkt_size": str(self.default_pkt_size)})

    def is_multicast(self) -> bool:
        host = urlsplit(self.spec.uri).hostname or ""
        first_octet = host.split(".", 1)[0]
        return first_octet.isdigit() and 224 <= int(first_octet) <= 239

    def output_args(self) -> list[str]:
        return [*self.spec.extra_output_args, "-f", "mpegts", self.connect_target()]

    def describe(self) -> str:
        mode = "multicast" if self.is_multicast() else "unicast"
        return f"udp-ts sink {self.spec.label} ({mode}) -> {self.connect_target()}"


class FileSink(EgressSink):
    """MPEG-TS file sink for CI, verification, and optional as-run capture."""

    segment_seconds = 3600

    def connect_target(self) -> str:
        parsed = urlsplit(self.spec.uri)
        if parsed.scheme == "file":
            return str(_file_uri_path(self.spec.uri))
        return self.spec.uri

    def output_args(self) -> list[str]:
        target = self.connect_target()
        if _looks_like_strftime_pattern(target):
            return [
                *self.spec.extra_output_args,
                "-f",
                "segment",
                "-segment_time",
                str(self.segment_seconds),
                "-strftime",
                "1",
                "-reset_timestamps",
                "1",
                target,
            ]
        return [*self.spec.extra_output_args, "-f", "mpegts", target]


class HlsSink(EgressSink):
    """Local rolling live-HLS sink: writes a sliding-window manifest + segments.

    Reuses the ffmpeg ``-f hls`` muxer (the same muxer family as the VOD
    packager's ``pack_vod_asset``), teed off the persistent egress encoder's
    program feed. ``delete_segments`` means ffmpeg itself prunes old segments
    as the window slides (no custom cleanup code needed for the steady-state
    case); ``program_date_time`` lets a resuming player anchor to wall-clock
    time.

    Video is STREAM-COPIED (``-c:v copy``), like every other sink in this
    module (see ``egress.runtime``). It is deliberately NOT re-encoded: a
    re-encode through the bundled H.264 encoders (``libopenh264``,
    ``h264_mf``) strips the A/53 closed-caption SEI, so a captioned channel's
    HLS leg silently lost every caption the upstream embed leg had produced.

    Cadence consequence: ffmpeg's HLS muxer cuts only on a keyframe, and
    ``-hls_time`` is therefore a *target*, not a guarantee -- the real segment
    length follows the video's IDR interval, and stream-copy means this sink
    cannot force keyframes (``-force_key_frames``/``-g`` are encoder-side).
    The "2s segments / 12s window" promise therefore depends on the upstream
    encoder emitting a bounded IDR cadence. For the native GStreamer leg that
    is contractual: ``gst/graph.py`` pins ``openh264enc``'s ``gop-size`` to
    ``segment_seconds`` worth of frames. A channel whose video reaches this
    sink with a longer GOP than ``segment_seconds`` (e.g. an arbitrarily long
    source recording's original GOP, or a branded path that re-encodes
    without an equivalent keyframe pin) will produce correspondingly longer
    segments -- the sink cannot correct that without discarding captions.

    ``self.spec.uri`` is a local directory (validated in
    ``EgressSinkSpec._kind_matches_uri``); ``civiccast.stream.media_router``
    serves it at ``/media/live/{channel_id}/...``.

    U51: the manifest has TWO names, because one of them is a file a reader can
    hold open and the other must not be. ``connect_target()`` -- what the ffmpeg
    child's ``-f hls`` muxer writes -- is the relay-private
    :attr:`mux_playlist_name`; the advertised :attr:`manifest_name` that
    ``media_router`` serves, ``civiccast.live.cdn_publisher`` republishes, and
    every player fetches is :meth:`manifest_target`, which the relay publishes
    from the muxer's staging window by in-place rewrite (a held handle blocks
    the muxer's own rename, and no reader of the served name holds the staging
    one). The served name itself did not change: readers, the router and the
    daemon's stale-playlist discard all still see ``playlist.m3u8``.
    """

    segment_seconds = 2
    playlist_size = 6  # 6 x 2s segments = 12s sliding window

    #: The advertised manifest: what the media router serves, what players
    #: fetch, and what ``civiccast.live.cdn_publisher`` republishes. Named here
    #: so the relay and the router cannot drift apart.
    manifest_name = "playlist.m3u8"
    #: The muxer's private staging manifest. Deliberately NOT the served name:
    #: FFmpeg's hls muxer publishes temp-file-then-rename on every update, so
    #: giving it the served name means a reader holding that file blocks the
    #: muxer's rename (``WinError 5``/``32``) and freezes the manifest silently
    #: for as long as the handle lives. Nothing outside the relay reads this
    #: name; the relay mirrors it to :meth:`manifest_target` in place.
    mux_playlist_name = "playlist.mux.m3u8"

    def _directory(self) -> Path:
        parsed = urlsplit(self.spec.uri)
        if parsed.scheme != "file":
            return Path(self.spec.uri)
        return _file_uri_path(self.spec.uri)

    def manifest_target(self) -> str:
        """The ADVERTISED manifest -- the file viewers, the router and the CDN
        publisher read. Written by the relay, never by the muxer (U51)."""
        return str(self._directory() / self.manifest_name)

    def connect_target(self) -> str:
        """The file the muxer writes: the relay-private staging playlist.

        Not the served name -- see the class docstring's U51 note. A caller that
        means "the manifest viewers read" wants :meth:`manifest_target`.
        """
        return str(self._directory() / self.mux_playlist_name)

    def output_args(self) -> list[str]:
        # Stream-copy BOTH streams instead of re-encoding them.
        #
        # Video: a re-encode through the bundled H.264 encoders (libopenh264,
        # h264_mf) strips the A/53 closed-caption SEI, so the HLS leg lost every
        # caption even though the upstream GStreamer embed leg produced it --
        # the decode-back verifier then saw zero cues on a captioned channel.
        # Copy preserves the SEI byte-for-byte. Cadence consequence:
        # -force_key_frames/-g are encoder-side and are impossible with copy, so
        # the ~2s cut points now depend on the upstream encoder's IDR interval.
        # That interval is contractual: gst/graph.py pins openh264enc's
        # ``gop-size`` to segment_seconds worth of frames. Copy also means the
        # video bitrate is whatever upstream already produced, so -b:v is
        # intentionally absent.
        #
        # Audio: a bare ``-c:a aac`` here (no ``-b:a``) was a SECOND lossy AAC
        # generation on the only audio path that reaches viewers, and beta.10
        # U49 measured that generation as the one that adds overshoot -- the
        # relay's input holds -1.1 dBTP while the emitted segment decodes to
        # +1.16 dBFS on real samples, four of them in a row. The worker's AAC is
        # ADTS (48 kHz stereo LC), which is exactly what HLS carries, so copy is
        # valid here and removes the overshoot by construction rather than
        # bounding it after the fact with a limiter. It also costs no CPU and
        # gives the viewer the worker's measured -1.1 dBTP / -16.1 LUFS instead
        # of a re-encode's. The trade is bandwidth on the public HLS leg
        # (~133 -> ~198 kbps, +48 %), accepted for this fix. This is also what
        # the spec already documents for the live/persistent encoder (MASTER
        # spec section 3 loudness row: "playout encoder is -c:a copy").
        return [
            *self.spec.extra_output_args,
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            *self.container_args(),
        ]

    def container_args(self) -> list[str]:
        """``output_args()`` minus the codec decision -- see the base method.

        The per-sink loudness path has already chosen the audio codec (loudnorm
        re-encode or copy) and must not have it overridden by position.
        """
        directory = self._directory()
        directory.mkdir(parents=True, exist_ok=True)
        segment_pattern = str(directory / "seg%09d.ts")
        return [
            "-f",
            "hls",
            "-hls_time",
            str(self.segment_seconds),
            "-hls_list_size",
            str(self.playlist_size),
            "-hls_flags",
            "delete_segments+append_list+program_date_time+independent_segments",
            "-hls_segment_filename",
            segment_pattern,
            self.connect_target(),
        ]

    def describe(self) -> str:
        # The ADVERTISED manifest: this string is what an operator reads to
        # find the file a viewer is being served (U51).
        return f"hls sink {self.spec.label} -> {self.manifest_target()}"


class SdiSink(EgressSink):
    """Declared stub: SDI is delivered by the supervised relay, not a sink."""

    def output_args(self) -> list[str]:
        raise NotImplementedError(
            "Direct SDI output is out of scope for the encoder leg; "
            "configure the channel's SDI output device instead (issue #117) - "
            "the automation driver supervises a BYO-ffmpeg DeckLink relay."
        )


def build_sink(
    spec: EgressSinkSpec,
    *,
    resolve_secret: SecretResolver | None = None,
) -> EgressSink:
    """Build a sink adapter for one validated sink spec."""

    if spec.kind == "srt":
        return SrtSink(spec, resolve_secret=resolve_secret)
    if spec.kind == "rtmp":
        return RtmpSink(spec, resolve_secret=resolve_secret)
    if spec.kind == "local-ts":
        return LocalTsSink(spec, resolve_secret=resolve_secret)
    if spec.kind == "udp-ts":
        return UdpTsSink(spec, resolve_secret=resolve_secret)
    if spec.kind == "file":
        return FileSink(spec, resolve_secret=resolve_secret)
    if spec.kind == "hls":
        return HlsSink(spec, resolve_secret=resolve_secret)
    return SdiSink(spec, resolve_secret=resolve_secret)


def _append_query_defaults(uri: str, defaults: dict[str, str]) -> str:
    parsed = urlsplit(uri)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    existing = {key.lower() for key, _value in query}
    for key, value in defaults.items():
        if key.lower() not in existing:
            query.append((key, value))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _redact_query(uri: str) -> str:
    parsed = urlsplit(uri)
    redacted: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in {"passphrase", "streamkey", "stream_key", "token", "secret"}:
            redacted.append((key, "<redacted>"))
        else:
            redacted.append((key, value))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(redacted), parsed.fragment)
    )


def _looks_like_strftime_pattern(value: str) -> bool:
    return any(pattern in value for pattern in ("%Y", "%m", "%d", "%H", "%M", "%S"))
