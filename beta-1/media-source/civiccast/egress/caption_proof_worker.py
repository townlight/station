# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Per-ON_AIR caption decode-back proof loop (S11a).

The embed side (GStreamer-native ``cccombiner``/``h264ccinserter``) inserts CEA-708
SEI; THIS loop is what proves it actually survived to the *emitted* stream and flips
``caption_status`` to ``on``. Each scan, for every ON_AIR channel: capture a short
segment of the emitted stream, decode its embedded captions, compare to the channel's
expected caption cues, and persist an ``EgressCaptionProofSample`` (which
``build_caption_status_provider`` reads — fail-closed: a fresh PASS → ``on``, anything
else → ``not-verified``).

The loop logic, cue selection, and persistence are unit-tested here with fakes. The
two live edges — capturing the emitted broadcast stream and the binding to the channel
caption pipeline's cues — are WSL/LPM-validated (a real live udp/srt egress + a real
caption source). Follows the ``ThreadSupervisor`` ``run_forever``/``run_once`` shape.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from civiccast.captions.models import CaptionCue
from civiccast.egress.caption_proof import (
    DEFAULT_CAPTION_PROOF_TIMEOUT_SECONDS,
    CaptionMode,
    Clock,
    FfmpegRunner,
    _run_bounded_ffmpeg,
    sample_caption_decode_back,
)
from civiccast.egress.store import EgressStore
from civiccast.stream._ffmpeg import run_ffmpeg

_LOG = logging.getLogger(__name__)

OnAirChannelsProvider = Callable[[], list[str]]
SegmentCapture = Callable[[str], Path | None]
ExpectedCuesProvider = Callable[[str], list[CaptionCue]]


@dataclass(frozen=True)
class CaptionProofScanResult:
    """Outcome of one ``run_once`` scan."""

    scanned: int = 0
    passed: int = 0
    failed: int = 0
    skipped_no_capture: int = 0
    channels: tuple[str, ...] = field(default_factory=tuple)


class CaptionProofWorker:
    """Sample caption decode-back for ON_AIR channels and persist the proof.

    All collaborators are injected so the loop is unit-testable without GStreamer,
    ffmpeg, or a live stream:

    * ``on_air_channels`` — the channels currently ON_AIR (and embedding captions).
    * ``capture_segment`` — capture a short emitted-stream segment for a channel,
      or None if no segment is available (then the channel is skipped — fail-closed,
      ``caption_status`` stays not-verified until a real capture proves a PASS).
    * ``expected_cues_provider`` — the cues the channel is embedding (the channel
      caption pipeline: review queue / ASR tap / sidecar). Empty → the sample is a
      FAIL with ``NO_EXPECTED_CUES`` (we never fabricate a PASS).
    * ``is_enabled`` — the operator's live-captions switch
      (``StationProfile.live_captions_enabled`` via
      ``resolve_live_captions_enabled``), consulted on EVERY scan. With live
      captions off no embed leg is built and the tap blanks every sidecar, so a
      scan would only spend a 6 s ffmpeg capture per channel every 30 s to write
      a guaranteed ``NO_EXPECTED_CUES`` FAIL row (round-2 review MAJOR 3). The
      scan short-circuits instead; flipping the switch on later resumes proofs
      on the next cycle with no control-plane restart. Default: always enabled.
    """

    def __init__(
        self,
        *,
        store: EgressStore,
        on_air_channels: OnAirChannelsProvider,
        capture_segment: SegmentCapture,
        expected_cues_provider: ExpectedCuesProvider,
        mode: CaptionMode = "cea-708",
        runner: FfmpegRunner = run_ffmpeg,
        clock: Clock = lambda: datetime.now(UTC),
        is_enabled: Callable[[], bool] | None = None,
        ffmpeg_timeout_seconds: float | None = DEFAULT_CAPTION_PROOF_TIMEOUT_SECONDS,
        align_transport_clock: bool = False,
    ) -> None:
        self._store = store
        self._on_air_channels = on_air_channels
        self._capture_segment = capture_segment
        self._expected_cues_provider = expected_cues_provider
        self._mode = mode
        self._runner = runner
        self._clock = clock
        self._is_enabled = is_enabled or (lambda: True)
        self._ffmpeg_timeout_seconds = ffmpeg_timeout_seconds
        self._align_transport_clock = align_transport_clock
        self._disabled_announced = False

    def run_forever(
        self,
        *,
        poll_seconds: float = 30.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Run the scan loop until ``stop_event`` is set; scan errors are logged."""
        while stop_event is None or not stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                _LOG.exception("Caption proof scan failed; continuing.")
            if stop_event is None:
                threading.Event().wait(poll_seconds)  # pragma: no cover - loop shape
            else:
                stop_event.wait(poll_seconds)

    def run_once(self) -> CaptionProofScanResult:
        """Sample every ON_AIR channel once and persist each proof sample.

        Returns an empty result -- no capture, no DB row -- while the operator
        has live captions switched off (see ``is_enabled``)."""
        if not self._is_enabled():
            if not self._disabled_announced:
                _LOG.info(
                    "Live captions are switched off for this station "
                    "(StationProfile.live_captions_enabled); the caption decode-back "
                    "proof is idle until they are switched on."
                )
                self._disabled_announced = True
            return CaptionProofScanResult()
        self._disabled_announced = False
        passed = 0
        failed = 0
        skipped = 0
        sampled: list[str] = []
        for channel_id in self._on_air_channels():
            segment = self._capture_segment(channel_id)
            if segment is None:
                skipped += 1
                continue
            sample = sample_caption_decode_back(
                channel_id=channel_id,
                emitted_stream_path=segment,
                expected_cues=self._expected_cues_provider(channel_id),
                mode=self._mode,
                runner=self._runner,
                clock=self._clock,
                timeout_seconds=self._ffmpeg_timeout_seconds,
                align_transport_clock=self._align_transport_clock,
            )
            self._store.append_caption_proof_sample(sample)
            sampled.append(channel_id)
            if sample.status == "PASS":
                passed += 1
            else:
                failed += 1
        return CaptionProofScanResult(
            scanned=passed + failed,
            passed=passed,
            failed=failed,
            skipped_no_capture=skipped,
            channels=tuple(sampled),
        )


# --- live wiring (WSL/LPM edge) ------------------------------------------------

# Sink schemes whose emitted stream is captured live by ffmpeg for decode-back.
_CAPTURE_SINK_SCHEMES = ("udp", "srt")


def _emitted_stream_target(store: EgressStore, channel_id: str) -> tuple[str, bool] | None:
    """``(uri, is_file)`` for the channel's emitted stream to decode back, or None.

    Prefers a file/local-ts sink (the written TS — read its tail); else a live
    udp/srt broadcast sink (captured by ffmpeg). rtmp/sdi are not TS-decode targets."""
    config = store.get_config(channel_id)
    if config is None:
        return None
    for sink in config.sinks:
        if sink.kind in ("file", "local-ts"):
            parsed = urlsplit(sink.uri)
            return (parsed.path if parsed.scheme == "file" else sink.uri, True)
    for sink in config.sinks:
        if urlsplit(sink.uri).scheme.lower() in _CAPTURE_SINK_SCHEMES:
            return (sink.uri, False)
    return None


def capture_emitted_segment(
    store: EgressStore,
    channel_id: str,
    *,
    work_dir: Path,
    capture_seconds: float = 6.0,
    runner: FfmpegRunner = run_ffmpeg,
    timeout_seconds: float | None = DEFAULT_CAPTION_PROOF_TIMEOUT_SECONDS,
) -> Path | None:
    """Capture a short, bounded segment of the channel's emitted stream (WSL/LPM edge).

    A live udp/srt sink is captured for ``capture_seconds``; a file/local-ts sink's
    LAST ``capture_seconds`` are copied (``-sseof``) so a 24/7 channel's growing file is
    never decoded whole. Returns the segment path, or None if there is nothing to
    capture / the capture produced no bytes (then the channel is skipped, fail-closed)."""
    target = _emitted_stream_target(store, channel_id)
    if target is None:
        return None
    uri, is_file = target
    out = work_dir / channel_id / "caption-proof" / "segment.ts"
    out.parent.mkdir(parents=True, exist_ok=True)
    seconds = f"{capture_seconds:g}"
    if is_file:
        if not Path(uri).exists():
            return None
        args = ["-y", "-hide_banner", "-nostats", "-sseof", f"-{seconds}", "-i", uri]
    else:
        args = ["-y", "-hide_banner", "-nostats", "-i", uri]
    # Preserve the emitted transport clock, including wrap, rather than
    # replacing the capture's epoch with ffmpeg's default near-zero clock.
    args.insert(0, "-copyts")
    # Input duration is relative to input start. Output -t with -copyts would
    # stop immediately when a days-old PTS already exceeds six seconds.
    args[args.index("-i") : args.index("-i")] = ["-t", seconds]
    args += ["-c", "copy", "-mpegts_copyts", "1", "-f", "mpegts", str(out)]
    with contextlib.suppress(OSError):
        # Never let a failed/partial proof capture be mistaken for the prior
        # successful segment at this fixed path.
        out.unlink(missing_ok=True)
    try:
        result = _run_bounded_ffmpeg(runner, args, timeout_seconds=timeout_seconds)
    except subprocess.TimeoutExpired:
        _LOG.warning(
            "Caption proof capture timed out after %ss for channel %s; "
            "failing closed with no segment.",
            timeout_seconds,
            channel_id,
        )
        with contextlib.suppress(OSError):
            out.unlink(missing_ok=True)
        return None
    if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        return None
    return out


SessionFactory = Callable[[], AbstractContextManager[Session]]


def build_caption_proof_worker(
    session_factory: SessionFactory,
    *,
    work_dir: Path | None = None,
    capture_seconds: float = 6.0,
    mode: CaptionMode = "cea-708",
    caption_sidecar_for: Callable[[str], Path] | None = None,
    runner: FfmpegRunner = run_ffmpeg,
    is_enabled: Callable[[], bool] | None = None,
    ffmpeg_timeout_seconds: float | None = DEFAULT_CAPTION_PROOF_TIMEOUT_SECONDS,
) -> CaptionProofWorker:
    """Wire the production caption proof worker (Postgres store + live providers).

    ``on_air_channels`` reads channel state; ``capture_segment`` taps the emitted
    stream (live edge); ``expected_cues_provider`` loads accepted caption-page
    delivery times from ``<work_dir>/<channel>/captions/delivered.json``. Source
    sidecars retain audio times and cannot substitute for these transport times.
    ``caption_sidecar_for`` remains a compatibility argument; production proof
    always uses the worker delivery receipt. Missing receipt or capture clock
    fails closed (caption_status stays not-verified). ``is_enabled`` is the
    operator's live-captions switch, re-read every scan (``civiccast.app`` passes
    ``resolve_live_captions_enabled``)."""
    from civiccast.egress.automation import default_egress_work_dir
    from civiccast.egress.store import PostgresEgressStore

    store = PostgresEgressStore(session_factory)
    resolved_work_dir = (work_dir or default_egress_work_dir()).expanduser()

    def _on_air() -> list[str]:
        channels: list[str] = []
        for config in store.list_configs():
            state_row = store.read_state(config.channel_id)
            if state_row is not None and state_row.state == "ON_AIR":
                channels.append(config.channel_id)
        return channels

    def _capture(channel_id: str) -> Path | None:
        return capture_emitted_segment(
            store,
            channel_id,
            work_dir=resolved_work_dir,
            capture_seconds=capture_seconds,
            runner=runner,
            timeout_seconds=ffmpeg_timeout_seconds,
        )

    def _expected(channel_id: str) -> list[CaptionCue]:
        # Source-audio sidecars cannot predict the live appsrc's re-timing.
        # Only pages actually accepted by that worker carry expected TS PTS.
        receipt = resolved_work_dir / channel_id / "captions" / "delivered.json"
        try:
            pages = json.loads(receipt.read_text(encoding="utf-8"))
            if not isinstance(pages, list) or len(pages) > 512:
                return []
            return [
                CaptionCue(
                    cue_id=f"{channel_id}-delivery-{index}",
                    start_seconds=page["start_seconds"],
                    end_seconds=page["end_seconds"],
                    text=page["text"],
                    confidence=1.0,
                    low_confidence=False,
                )
                for index, page in enumerate(pages)
            ]
        except (OSError, ValueError, KeyError, TypeError):
            return []

    return CaptionProofWorker(
        store=store,
        on_air_channels=_on_air,
        capture_segment=_capture,
        expected_cues_provider=_expected,
        mode=mode,
        runner=runner,
        is_enabled=is_enabled,
        ffmpeg_timeout_seconds=ffmpeg_timeout_seconds,
        align_transport_clock=True,
    )
