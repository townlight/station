# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Live caption FEED worker (S11a) — the production caller of send_caption_cue.

The S11a embed lane (appsrc -> tttocea608 -> cccombiner -> h264ccinserter) is only
useful if something pushes timed-text cues into the live appsrc. This worker is that
producer: for each ON_AIR channel that embeds captions, it reads the channel's active
caption cues (the same sidecar the decode-back proof reads as "expected") and pushes
each NEW cue to the running gst worker via ``GstPlayoutStrategy.send_caption_cue`` (the
``caption`` control command). Already-pushed cues are tracked so a re-scan never
double-sends. On the ffmpeg engine there is no control FIFO, so the send simply drops
(captions embed only on the gst engine — documented).

The cue PTS->pipeline-running-time alignment is the live edge (WSL/LPM-validated); the
scan/dedup/feed logic is unit-tested here. This closes the loop the decode-back proof
verifies: sidecar cues -> feed -> embed -> emitted stream -> decode-back -> caption_status.
"""

from __future__ import annotations

import logging
import textwrap
import threading
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from civiccast.captions.models import CaptionCue
from civiccast.captions.tap import build_audio_tap_plan
from civiccast.egress.caption_embed import (
    count_caption_timing_windows,
    load_caption_cues_from_timed_text,
)
from civiccast.egress.gst.audio_tap import read_program_boundary

_LOG = logging.getLogger(__name__)

OnAirChannelsProvider = Callable[[], list[str]]
CaptionCueProvider = Callable[[str], list[CaptionCue]]
# (channel_id, work_dir, *, text, pts_seconds, duration_seconds) -> sent?
SendCaptionCue = Callable[..., bool]
SessionFactory = Callable[[], AbstractContextManager[Session]]

_CEA_COLUMNS = 32
_CEA_ROWS_PER_PAGE = 2
_CAPTION_DELIVERY_NAMESPACE = uuid.UUID("e0adb299-d51d-4f2e-b47b-c9eefc1a9046")


def cea_caption_pages(text: str) -> tuple[str, ...]:
    """Return lossless CEA-608-safe display pages for one machine cue.

    CEA-608 rows are 32 columns wide. ``tttocea608`` truncates a longer
    unbroken row rather than preserving the overflow, so the feed must insert
    row breaks before handing text to GStreamer. Longer cues are paginated in
    two-row displays; no source word is silently discarded.
    """

    normalized = " ".join(text.split())
    if not normalized:
        raise ValueError("caption text must contain visible characters")
    lines = textwrap.wrap(
        normalized,
        width=_CEA_COLUMNS,
        break_long_words=True,
        break_on_hyphens=False,
        drop_whitespace=True,
        replace_whitespace=True,
    )
    return tuple(
        "\n".join(lines[index : index + _CEA_ROWS_PER_PAGE])
        for index in range(0, len(lines), _CEA_ROWS_PER_PAGE)
    )


def caption_page_delivery_id(
    channel_id: str,
    cue_id: str,
    page_index: int,
) -> str:
    """Return the stable worker-envelope id for one cue display page."""
    if not channel_id or not cue_id or page_index < 0:
        raise ValueError("caption page delivery identity is invalid")
    return str(
        uuid.uuid5(
            _CAPTION_DELIVERY_NAMESPACE,
            f"{channel_id}\0{cue_id}\0{page_index}",
        )
    )


@dataclass(frozen=True)
class CaptionFeedScanResult:
    """Outcome of one ``run_once`` scan."""

    channels: int = 0
    cues_sent: int = 0
    cues_dropped: int = 0  # send returned False (worker FIFO not ready / not gst engine)
    sent_channels: tuple[str, ...] = field(default_factory=tuple)


class CaptionFeedWorker:
    """Push each ON_AIR channel's new caption cues into its live caption appsrc."""

    def __init__(
        self,
        *,
        work_dir: Path,
        on_air_channels: OnAirChannelsProvider,
        caption_cue_provider: CaptionCueProvider,
        send_caption_cue: SendCaptionCue,
        is_enabled: Callable[[], bool] | None = None,
        program_token_provider: Callable[[str], str] | None = None,
    ) -> None:
        self._work_dir = work_dir
        self._on_air_channels = on_air_channels
        self._caption_cue_provider = caption_cue_provider
        self._send_caption_cue = send_caption_cue
        # The operator's live-captions switch (``StationProfile.live_captions_
        # enabled`` via ``resolve_live_captions_enabled``), consulted on EVERY
        # scan: with live captions off there is no caption appsrc to feed, so a
        # scan would only poll sidecars every 2 s and retry every cue forever
        # (round-2 review MAJOR 3). Flipping the switch on later resumes the
        # feed on the next cycle with no control-plane restart. Default: always
        # enabled, so an injected worker in a test behaves as before.
        self._is_enabled = is_enabled or (lambda: True)
        self._program_token_provider = program_token_provider or (lambda _channel: "")
        self._disabled_announced = False
        # Already-pushed IDs within each channel's rolling provider window.
        self._sent: dict[str, set[str]] = {}
        # A multi-page cue can be only partly acknowledged. Retain each page's
        # acknowledgement independently so a retry never repeats pages already
        # accepted by the worker.
        self._acknowledged_pages: dict[str, set[tuple[str, int]]] = {}

    def run_forever(
        self, *, poll_seconds: float = 2.0, stop_event: threading.Event | None = None
    ) -> None:
        """Run the feed loop until ``stop_event`` is set; scan errors are logged."""
        while stop_event is None or not stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                _LOG.exception("Caption feed scan failed; continuing.")
            if stop_event is None:
                threading.Event().wait(poll_seconds)  # pragma: no cover - loop shape
            else:
                stop_event.wait(poll_seconds)

    def run_once(self) -> CaptionFeedScanResult:
        if not self._is_enabled():
            # Forget everything sent so far: the pipeline built after the switch
            # comes back on has a fresh appsrc, and must be fed from scratch.
            self._sent.clear()
            self._acknowledged_pages.clear()
            if not self._disabled_announced:
                _LOG.info(
                    "Live captions are switched off for this station "
                    "(StationProfile.live_captions_enabled); the caption feed is idle "
                    "until they are switched on."
                )
                self._disabled_announced = True
            return CaptionFeedScanResult()
        self._disabled_announced = False
        on_air = self._on_air_channels()
        # Forget channels that went off air so a later return re-sends from scratch.
        self._sent = {cid: seen for cid, seen in self._sent.items() if cid in on_air}
        self._acknowledged_pages = {
            cid: seen for cid, seen in self._acknowledged_pages.items() if cid in on_air
        }
        sent = 0
        dropped = 0
        touched: list[str] = []
        for channel_id in on_air:
            token = self._program_token_provider(channel_id)
            cues = self._caption_cue_provider(channel_id)
            if token:
                cues = [cue for cue in cues if cue.cue_id.startswith(f"{channel_id}-{token}-")]
            current_ids = {cue.cue_id for cue in cues}
            seen = self._sent.setdefault(channel_id, set())
            acknowledged_pages = self._acknowledged_pages.setdefault(channel_id, set())
            # Retain partial acknowledgements for every cue still offered by
            # the provider, but retire tracking when a cue leaves its window.
            # Read the provider first: a raised read error must not erase state.
            seen.intersection_update(current_ids)
            acknowledged_pages.intersection_update(
                {key for key in acknowledged_pages if key[0] in current_ids}
            )
            channel_sent = False
            for cue in cues:
                if cue.cue_id in seen:
                    continue
                pages = cea_caption_pages(cue.text)
                cue_duration = max(0.0, cue.end_seconds - cue.start_seconds)
                # Share the original cue envelope by displayed text length so
                # a full page does not flash as briefly as a short final page.
                # A row break counts as one space, not an extra visible glyph.
                page_weights = [len(" ".join(page.split())) for page in pages]
                total_weight = sum(page_weights)
                elapsed_weight = 0
                page_start = cue.start_seconds
                for index, page in enumerate(pages):
                    elapsed_weight += page_weights[index]
                    page_end = (
                        cue.end_seconds
                        if index == len(pages) - 1
                        else cue.start_seconds + cue_duration * elapsed_weight / total_weight
                    )
                    page_pts = page_start
                    page_duration = page_end - page_start
                    page_start = page_end
                    # Advance timing even for already acknowledged pages; a
                    # retried page keeps its original PTS and delivery identity.
                    page_key = (cue.cue_id, index)
                    if page_key in acknowledged_pages:
                        continue
                    if self._send_caption_cue(
                        channel_id,
                        self._work_dir,
                        text=page,
                        pts_seconds=page_pts,
                        duration_seconds=page_duration,
                        **({"program_token": token} if token else {}),
                        delivery_id=caption_page_delivery_id(
                            channel_id,
                            cue.cue_id,
                            index,
                        ),
                    ):
                        acknowledged_pages.add(page_key)
                if all((cue.cue_id, index) in acknowledged_pages for index in range(len(pages))):
                    seen.add(cue.cue_id)  # only mark sent on a successful push
                    sent += 1
                    channel_sent = True
                else:
                    dropped += 1
            if channel_sent:
                touched.append(channel_id)
        return CaptionFeedScanResult(
            channels=len(on_air),
            cues_sent=sent,
            cues_dropped=dropped,
            sent_channels=tuple(touched),
        )


def build_caption_feed_worker(
    session_factory: SessionFactory,
    *,
    send_caption_cue: SendCaptionCue,
    work_dir: Path | None = None,
    caption_sidecar_for: Callable[[str], Path] | None = None,
    is_enabled: Callable[[], bool] | None = None,
) -> CaptionFeedWorker:
    """Wire the production caption feed worker.

    ``on_air_channels`` reads channel state; ``caption_cue_provider`` loads the channel's
    active caption cues from its sidecar (``<work_dir>/<channel>/captions/active.vtt`` by
    default — the same file the decode-back proof reads); ``send_caption_cue`` is the gst
    strategy's FIFO command (a no-op drop on the ffmpeg engine); ``is_enabled`` is the
    operator's live-captions switch, re-read every scan (``civiccast.app`` passes
    ``resolve_live_captions_enabled``). The sidecar's production by the captions
    pipeline + the live PTS alignment are WSL/LPM-validated."""
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

    # U46 (2026-09-26): a sidecar the parser cannot read is the one caption fault
    # this feed used to swallow whole. Public's sidecar grew normally while its
    # emitted stream carried no captions for hours: the feed parsed 0 cues, sent 0
    # commands, and the daemon/strategy/engine legs below it never saw a single
    # command, so every warning they own stayed silent too. Announced once per
    # channel per broken spell, never once per 2 s scan.
    unreadable_announced: set[str] = set()

    def _cues(channel_id: str) -> list[CaptionCue]:
        sidecar = (
            caption_sidecar_for(channel_id)
            if caption_sidecar_for is not None
            else resolved_work_dir / channel_id / "captions" / "active.vtt"
        )
        if not sidecar.exists():
            return []
        cues = load_caption_cues_from_timed_text(sidecar, source_id=channel_id)
        if cues:
            unreadable_announced.discard(channel_id)
            return cues
        windows = count_caption_timing_windows(sidecar)
        if windows and channel_id not in unreadable_announced:
            unreadable_announced.add(channel_id)
            _LOG.warning(
                "Caption sidecar for channel %s holds %d timing windows but the parser "
                "read 0 cues from it (%s): this channel is emitting a caption-free "
                "stream while its sidecar keeps growing.",
                channel_id,
                windows,
                sidecar,
            )
        return cues

    return CaptionFeedWorker(
        work_dir=resolved_work_dir,
        on_air_channels=_on_air,
        caption_cue_provider=_cues,
        send_caption_cue=send_caption_cue,
        is_enabled=is_enabled,
        program_token_provider=lambda channel_id: (
            read_program_boundary(plan.tap_dir)[0]
            if (plan := build_audio_tap_plan(channel_id)) is not None
            else ""
        ),
    )
