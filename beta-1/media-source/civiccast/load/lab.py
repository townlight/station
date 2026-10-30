# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Synthetic rolling-station lab for the HLS load harness (0.2.0 step 3).

Stands up a fake live channel -- a directory of ``seg%09d.ts`` segments plus a
``playlist.m3u8`` that rolls on the real 2s cadence, served through the *real*
:data:`civiccast.stream.media_router.live_router` -- so
:func:`civiccast.load.hls_load.run_load` can ramp concurrent emulated viewers
against the genuine serving path and observe where they start to fall behind
live (the server-capacity knee).

Honesty about what this measures. The ramp runs **in-process** over
``httpx.ASGITransport``: the emulated viewers and the ASGI app share one event
loop on one core. That makes the measured capacity a deliberate *lower bound*
-- a real deployment serves clients over TCP from a process that is not also
generating the load. If viewers stay healthy here, they stay healthy in the
field; the knee we find is pessimistic, not optimistic. And loopback of any
kind cannot exhibit the limit that actually binds direct-from-station delivery:
segment-bitrate x viewers vs the station's uplink. That ceiling is *computed*
(:func:`civiccast.load.hls_load.bandwidth_ceiling`,
:func:`render_bandwidth_table`), not measured. The results doc pairs the two.

Runnable::

    python -m civiccast.load.lab --viewers 10,25,50,100 --duration 10 \
        --uplink-mbps 1000 --per-viewer-mbps 4.628 --json-out ramp.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx
from fastapi import FastAPI

from civiccast.egress.models import EgressConfig, EgressSinkSpec
from civiccast.egress.router import get_egress_store
from civiccast.load.hls_load import LoadReport, bandwidth_ceiling, run_load
from civiccast.stream.config import ABR_LADDER, HLS_SEGMENT_DURATION
from civiccast.stream.media_router import live_router

MANIFEST_NAME = "playlist.m3u8"
_CHANNEL_ID = "lab"

# ---------------------------------------------------------------------------
# Rolling synthetic station
# ---------------------------------------------------------------------------


def _replace_with_retry(src: Path, dest: Path, *, attempts: int = 40) -> None:
    """Atomic replace that tolerates Windows sharing violations.

    ``Path.replace`` is atomic, but on Windows it fails with PermissionError
    while ANY reader holds the destination open without FILE_SHARE_DELETE
    (Starlette's StaticFiles reader does, ~every 2s under soak load). Retry
    through the brief window; 40 x 50ms = 2s worst case, far beyond any
    single-read hold time observed. POSIX never takes an iteration.
    """
    for _attempt in range(attempts):
        try:
            src.replace(dest)
            return
        except PermissionError:
            time.sleep(0.05)
    src.replace(dest)  # final attempt surfaces the real error


def segment_name(sequence: int) -> str:
    """The on-disk name HlsSink uses for segment ``sequence`` (``seg%09d.ts``)."""
    return f"seg{sequence:09d}.ts"


def render_live_manifest(
    segment_names: Sequence[str],
    *,
    target_duration: int,
    media_sequence: int,
) -> str:
    """Render a live (no ``#EXT-X-ENDLIST``) HLS media playlist over a window.

    Matches the shape ffmpeg's hls muxer writes for a rolling live stream:
    version + target duration + the media sequence of the first listed segment,
    then an ``#EXTINF`` / URI pair per segment.
    """
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}",
    ]
    for name in segment_names:
        lines.append(f"#EXTINF:{target_duration:.3f},")
        lines.append(name)
    return "\n".join(lines) + "\n"


class RollingStation:
    """A directory of rolling HLS segments plus a live manifest.

    :meth:`bootstrap` writes the initial ``window`` segments and the manifest;
    :meth:`roll` appends one new segment and evicts the oldest -- exactly what
    ffmpeg's hls muxer with ``-hls_list_size window -hls_flags delete_segments``
    does. The manifest is written before the evicted file is unlinked, so a
    viewer never receives a manifest that references a file already deleted.
    """

    def __init__(
        self,
        directory: Path,
        *,
        window: int = 6,
        segment_bytes: int = 200 * 1024,
        target_duration: int = HLS_SEGMENT_DURATION,
    ) -> None:
        if window < 1:
            raise ValueError("window must be >= 1")
        if segment_bytes < 1:
            raise ValueError("segment_bytes must be >= 1")
        self.directory = directory
        self.window = window
        self.segment_bytes = segment_bytes
        self.target_duration = target_duration
        self._next_sequence = 0  # sequence of the next segment to write
        self._payload = b"\x00" * segment_bytes
        self._pending_unlink: list[int] = []  # evictions a reader still holds open

    @property
    def media_sequence(self) -> int:
        """MEDIA-SEQUENCE of the current manifest (the oldest in-window segment)."""
        return max(0, self._next_sequence - self.window)

    def _window_names(self) -> list[str]:
        return [segment_name(n) for n in range(self.media_sequence, self._next_sequence)]

    def _write_segment(self, sequence: int) -> None:
        (self.directory / segment_name(sequence)).write_bytes(self._payload)

    def _write_manifest(self) -> None:
        text = render_live_manifest(
            self._window_names(),
            target_duration=self.target_duration,
            media_sequence=self.media_sequence,
        )
        # Write-then-replace so a concurrent reader never sees a half-written
        # manifest (rename is atomic within a directory).
        tmp = self.directory / (MANIFEST_NAME + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        _replace_with_retry(tmp, self.directory / MANIFEST_NAME)

    def bootstrap(self) -> None:
        """Write the initial full window of segments and the first manifest."""
        self.directory.mkdir(parents=True, exist_ok=True)
        while self._next_sequence < self.window:
            self._write_segment(self._next_sequence)
            self._next_sequence += 1
        self._write_manifest()

    def _try_unlink(self, sequence: int) -> None:
        """Delete an evicted segment, deferring if a reader still holds it open.

        On Windows a file open by a concurrent ``FileResponse`` cannot be
        unlinked (WinError 32). The segment is already out of the manifest, so
        no *new* reads start; retry it on the next roll once the in-flight read
        finishes. On POSIX (unlink-while-open is allowed) this never defers.
        """
        try:
            (self.directory / segment_name(sequence)).unlink(missing_ok=True)
        except PermissionError:
            self._pending_unlink.append(sequence)

    def roll(self) -> None:
        """Advance the window by one segment (append newest, evict oldest)."""
        evicted = self._next_sequence - self.window
        self._write_segment(self._next_sequence)
        self._next_sequence += 1
        self._write_manifest()
        deferred, self._pending_unlink = self._pending_unlink, []
        for sequence in deferred:
            self._try_unlink(sequence)
        if evicted >= 0:
            self._try_unlink(evicted)


# ---------------------------------------------------------------------------
# The real serving path, pointed at a lab directory
# ---------------------------------------------------------------------------


class _LabEgressStore:
    """Minimal egress-store stand-in: resolves one channel to one live dir.

    :func:`civiccast.stream.media_router._live_dir_for_channel` only reads
    ``get_config(channel).sinks[*].kind / .uri``, so a real
    :class:`~civiccast.egress.models.EgressConfig` carrying a single ``hls``
    sink (a ``file://`` URI at the lab directory) is a faithful stand-in.
    """

    def __init__(self, channel_id: str, live_dir: Path) -> None:
        self._channel_id = channel_id
        self._config = EgressConfig(
            channel_id=channel_id,
            enabled=True,
            slate_message="lab",
            sinks=[
                EgressSinkSpec(
                    kind="hls",
                    label="lab-hls",
                    uri=live_dir.resolve().as_uri(),
                )
            ],
        )

    def get_config(self, channel_id: str) -> EgressConfig | None:
        return self._config if channel_id == self._channel_id else None


def build_lab_app(directory: Path, *, channel_id: str = _CHANNEL_ID) -> FastAPI:
    """A FastAPI app serving ``directory`` as ``channel_id``'s live HLS output.

    Mounts the real ``live_router`` and overrides only the egress-store
    dependency, so requests traverse the genuine
    :func:`~civiccast.stream.media_router.get_live_media_file` path (traversal
    guard, content types, cache-control) -- not a lab reimplementation of it.
    """
    app = FastAPI()
    app.include_router(live_router)
    store = _LabEgressStore(channel_id, directory)
    app.dependency_overrides[get_egress_store] = lambda: store
    return app


def live_manifest_url(port_base_url: str, *, channel_id: str = _CHANNEL_ID) -> str:
    """The live media-playlist URL for ``channel_id`` under ``port_base_url``."""
    return f"{port_base_url.rstrip('/')}/media/live/{channel_id}/{MANIFEST_NAME}"


# ---------------------------------------------------------------------------
# In-process viewer ramp
# ---------------------------------------------------------------------------


async def _roll_forever(station: RollingStation, stop: asyncio.Event) -> None:
    """Roll the station every ``target_duration`` seconds until ``stop`` is set.

    ``station.roll()`` can block on ``_replace_with_retry``'s ``time.sleep()``
    (a Windows sharing-violation retry, up to 2s worst case); offload it to a
    thread so that never freezes the event loop the viewer requests share.
    """
    interval = float(station.target_duration)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            await asyncio.to_thread(station.roll)


async def _ramp_level(
    station: RollingStation,
    app: FastAPI,
    *,
    viewers: int,
    duration_s: float,
    uplink_mbps: float | None,
    per_viewer_mbps: float | None,
    base_url: str,
    clock: Callable[[], float],
) -> LoadReport:
    """Ramp one viewer level against the lab app while the station rolls."""
    transport = httpx.ASGITransport(app=app)
    stop = asyncio.Event()
    roller = asyncio.create_task(_roll_forever(station, stop))
    try:
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            report = await run_load(
                live_manifest_url(base_url),
                viewers=viewers,
                duration_s=duration_s,
                client=client,
                uplink_mbps=uplink_mbps,
                per_viewer_mbps=per_viewer_mbps,
                clock=clock,
            )
    except BaseException:
        # run_load failed: stop and drain the roller, but do NOT let a
        # roller-side error mask the primary failure (return_exceptions).
        stop.set()
        await asyncio.gather(roller, return_exceptions=True)
        raise
    # Clean path: surface a roller error (rather than swallow it) now that the
    # primary run succeeded.
    stop.set()
    await roller
    return report


async def run_ramp(
    viewer_levels: Sequence[int],
    *,
    duration_s: float,
    window: int = 6,
    segment_bytes: int = 200 * 1024,
    uplink_mbps: float | None = None,
    per_viewer_mbps: float | None = None,
    base_url: str = "http://lab",
    clock: Callable[[], float] = time.monotonic,
) -> list[LoadReport]:
    """Run the viewer sweep against one rolling lab station; one report per level.

    A fresh :class:`RollingStation` is bootstrapped in a temp dir and served
    through the real ``live_router``; each level in ``viewer_levels`` is run in
    turn against it (the station keeps rolling between levels).
    """
    with tempfile.TemporaryDirectory(prefix="civiccast-loadlab-") as tmp:
        directory = Path(tmp) / "live"
        # The media router serves only folders inside CIVICCAST_LIVE_HLS_ROOT
        # (or the egress work dir); the lab's temp station must be inside it.
        os.environ["CIVICCAST_LIVE_HLS_ROOT"] = tmp
        station = RollingStation(directory, window=window, segment_bytes=segment_bytes)
        station.bootstrap()
        app = build_lab_app(directory)
        reports: list[LoadReport] = []
        for viewers in viewer_levels:
            reports.append(
                await _ramp_level(
                    station,
                    app,
                    viewers=viewers,
                    duration_s=duration_s,
                    uplink_mbps=uplink_mbps,
                    per_viewer_mbps=per_viewer_mbps,
                    base_url=base_url,
                    clock=clock,
                )
            )
        return reports


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_ramp_table(reports: Sequence[LoadReport]) -> str:
    """A fixed-width text table of the ramp, one row per viewer level."""
    header = (
        f"{'viewers':>8} {'stall%':>7} {'seg_ok':>8} {'hard':>6} {'slow':>6} "
        f"{'p50_ms':>8} {'p95_ms':>8} {'agg_mbps':>9}"
    )
    rows = [header, "-" * len(header)]
    for r in reports:
        p50 = "" if r.join_latency_p50_s is None else f"{r.join_latency_p50_s * 1000:.0f}"
        p95 = "" if r.join_latency_p95_s is None else f"{r.join_latency_p95_s * 1000:.0f}"
        rows.append(
            f"{r.viewers:>8} {r.stall_rate * 100:>6.2f}% {r.segments_ok:>8} "
            f"{r.segments_failed:>6} {r.segments_slow:>6} {p50:>8} {p95:>8} "
            f"{r.aggregate_mbps:>9.1f}"
        )
    return "\n".join(rows)


def render_bandwidth_table(uplinks_mbps: Sequence[float]) -> str:
    """Markdown table: max concurrent direct viewers per rendition x uplink.

    Code-derived from :data:`civiccast.stream.config.ABR_LADDER` and
    :func:`~civiccast.load.hls_load.bandwidth_ceiling`, so it cannot drift from
    the encoder ladder the station actually ships.
    """
    uplink_cols = " | ".join(f"{u:g} Mbps" for u in uplinks_mbps)
    lines = [
        f"| Rendition | Mbps/viewer | {uplink_cols} |",
        "|---|---|" + "---|" * len(uplinks_mbps),
    ]
    for rendition in ABR_LADDER:
        per_viewer = rendition.bandwidth_bps / 1_000_000
        ceilings = " | ".join(str(bandwidth_ceiling(u, per_viewer)) for u in uplinks_mbps)
        lines.append(f"| {rendition.name} | {per_viewer:.3f} | {ceilings} |")
    return "\n".join(lines)


def _ramp_payload(reports: Sequence[LoadReport]) -> dict[str, object]:
    return {"levels": [r.as_dict() for r in reports]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m civiccast.load.lab",
        description="Ramp emulated viewers against a synthetic rolling HLS station.",
    )
    parser.add_argument(
        "--viewers",
        default="10,25,50,100",
        help="Comma-separated viewer levels to ramp (default: 10,25,50,100)",
    )
    parser.add_argument("--duration", type=float, default=10.0, help="Seconds per level")
    parser.add_argument("--window", type=int, default=6, help="Segments in the sliding window")
    parser.add_argument(
        "--segment-bytes", type=int, default=200 * 1024, help="Bytes per synthetic segment"
    )
    parser.add_argument("--uplink-mbps", type=float, default=None, help="For the modeled ceiling")
    parser.add_argument(
        "--per-viewer-mbps", type=float, default=None, help="Per-viewer bitrate for the ceiling"
    )
    parser.add_argument("--json-out", default=None, help="Write the ramp as JSON to this path")
    args = parser.parse_args(argv)

    viewer_levels = [int(v) for v in args.viewers.split(",") if v.strip()]
    reports = asyncio.run(
        run_ramp(
            viewer_levels,
            duration_s=args.duration,
            window=args.window,
            segment_bytes=args.segment_bytes,
            uplink_mbps=args.uplink_mbps,
            per_viewer_mbps=args.per_viewer_mbps,
        )
    )
    print(render_ramp_table(reports))
    if args.json_out is not None:
        Path(args.json_out).write_text(
            json.dumps(_ramp_payload(reports), indent=2), encoding="utf-8"
        )
    # Non-zero if any level stalled -- lets a CI smoke gate on a clean sweep.
    return 1 if any(r.stall_rate > 0 or r.manifest_failed > 0 for r in reports) else 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
