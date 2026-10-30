# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Concurrent HLS live-viewer load generator (0.2.0 step 1: the control number).

Emulates N simultaneous HLS players pulling a live broadcast on its real
rolling cadence, to measure how many concurrent viewers a serving endpoint
sustains before it starts stalling them. Two endpoints matter for 0.2.0:

* **Direct-from-station** -- ``/media/live/{channel}/playlist.m3u8`` served by
  :mod:`civiccast.stream.media_router` straight off the station's disk. The
  control number; expected to be modest.
* **CDN-fronted** -- the same manifest served through a CDN; expected to hold
  thousands. (Measured later, once the live CDN-publish path exists.)

Each simulated viewer does what a real HLS player does: GET the media playlist,
parse it, then GET each *new* segment as it appears, re-polling the playlist
every target-duration seconds. Two things count against a viewer:

* a segment fetch that fails (non-2xx -- e.g. the file already rolled out of the
  sliding window because the server could not serve it in time): a **hard stall**;
* a segment that takes longer than one segment-duration to download: a **slow
  fetch**, i.e. the viewer is falling behind the live edge.

The stall rate = (hard stalls + slow fetches) / segment attempts is the signal:
the ceiling is the concurrency at which it climbs off zero.

Ceilings, honestly: on loopback there is no uplink bottleneck, so a local run
measures the *server-capacity* ceiling (event loop + file serving), not the
*bandwidth* ceiling (segment bitrate x viewers vs the station's real uplink),
which is usually the binding one in the field. :func:`bandwidth_ceiling`
computes the latter from stated bitrate + uplink so a results report can state
both rather than overclaiming from a loopback number.

Runnable::

    python -m civiccast.load.hls_load --manifest-url http://host/media/live/ch/playlist.m3u8 \
        --viewers 50 --duration 30 --uplink-mbps 100 --per-viewer-mbps 3

httpx (already a dependency) drives the concurrency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin

import httpx

# ---------------------------------------------------------------------------
# Media-playlist parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MediaPlaylist:
    """The parts of an HLS playlist a load client acts on.

    Handles both a media playlist (``segment_uris`` populated) and a
    multivariant playlist (``variant_uris`` populated, no segments) -- a live
    viewer follows the first variant when handed a multivariant manifest.
    """

    target_duration: float
    segment_uris: tuple[str, ...]
    variant_uris: tuple[str, ...]
    media_sequence: int
    is_endlist: bool  # VOD / finished stream -> stop polling

    @property
    def is_multivariant(self) -> bool:
        return not self.segment_uris and bool(self.variant_uris)


def parse_media_playlist(text: str) -> MediaPlaylist:
    """Parse an ``.m3u8`` into the fields a load client needs.

    Any non-comment line is a URI; whether it is a segment or a variant is
    decided by whether the immediately preceding tag was ``#EXT-X-STREAM-INF``
    (variant) -- the standard HLS shape.
    """
    target_duration = 0.0
    media_sequence = 0
    is_endlist = False
    segments: list[str] = []
    variants: list[str] = []
    previous_was_stream_inf = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            target_duration = float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-ENDLIST"):
            is_endlist = True
        elif line.startswith("#EXT-X-STREAM-INF:"):
            previous_was_stream_inf = True
            continue
        elif not line.startswith("#"):
            if previous_was_stream_inf:
                variants.append(line)
            else:
                segments.append(line)
        previous_was_stream_inf = False
    return MediaPlaylist(
        target_duration=target_duration,
        segment_uris=tuple(segments),
        variant_uris=tuple(variants),
        media_sequence=media_sequence,
        is_endlist=is_endlist,
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass
class _ViewerTally:
    manifest_ok: int = 0
    manifest_failed: int = 0
    segments_ok: int = 0
    segments_failed: int = 0  # hard stall: non-2xx (e.g. rolled out of window)
    segments_slow: int = 0  # soft stall: fetch took > one segment duration
    bytes_downloaded: int = 0
    join_latency_s: float | None = None  # time from start to first segment byte


@dataclass
class LoadReport:
    """Aggregate result of a load run. ``as_dict()`` is JSON-serializable."""

    viewers: int
    duration_s: float
    wall_time_s: float
    manifest_ok: int
    manifest_failed: int
    segments_ok: int
    segments_failed: int
    segments_slow: int
    bytes_downloaded: int
    join_latency_p50_s: float | None
    join_latency_p95_s: float | None
    bandwidth_ceiling_viewers: int | None = None
    per_viewer_mbps: float | None = None
    uplink_mbps: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def segment_attempts(self) -> int:
        return self.segments_ok + self.segments_failed

    @property
    def stall_rate(self) -> float:
        """(hard stalls + slow fetches) / segment attempts. 0.0 == healthy."""
        attempts = self.segment_attempts
        if attempts == 0:
            return 0.0
        return (self.segments_failed + self.segments_slow) / attempts

    @property
    def aggregate_mbps(self) -> float:
        if self.wall_time_s <= 0:
            return 0.0
        return (self.bytes_downloaded * 8) / self.wall_time_s / 1_000_000

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = asdict(self)
        data["stall_rate"] = round(self.stall_rate, 4)
        data["aggregate_mbps"] = round(self.aggregate_mbps, 3)
        return data

    def summary(self) -> str:
        lines = [
            f"viewers={self.viewers}  duration={self.duration_s:.0f}s  wall={self.wall_time_s:.1f}s",
            f"manifest: {self.manifest_ok} ok / {self.manifest_failed} failed",
            f"segments: {self.segments_ok} ok / {self.segments_failed} failed(hard-stall) "
            f"/ {self.segments_slow} slow(behind-live)",
            f"stall_rate={self.stall_rate:.2%}   aggregate={self.aggregate_mbps:.1f} Mbps "
            f"({self.bytes_downloaded / 1_000_000:.1f} MB)",
        ]
        if self.join_latency_p50_s is not None and self.join_latency_p95_s is not None:
            lines.append(
                f"join latency: p50={self.join_latency_p50_s * 1000:.0f}ms  "
                f"p95={self.join_latency_p95_s * 1000:.0f}ms"
            )
        if self.bandwidth_ceiling_viewers is not None:
            lines.append(
                f"bandwidth ceiling (modeled): ~{self.bandwidth_ceiling_viewers} viewers "
                f"@ {self.per_viewer_mbps} Mbps/viewer over {self.uplink_mbps} Mbps uplink"
            )
        lines.extend(f"note: {n}" for n in self.notes)
        return "\n".join(lines)


def bandwidth_ceiling(uplink_mbps: float, per_viewer_mbps: float) -> int:
    """Max concurrent viewers a single uplink can serve directly, by bandwidth.

    The real binding limit for direct-from-station delivery: one uplink of
    ``uplink_mbps`` serving a stream of ``per_viewer_mbps`` to each viewer
    saturates at ``uplink / per_viewer`` viewers, after which everyone stalls.
    A loopback load test cannot exhibit this (no uplink bottleneck), so it is
    computed, not measured.
    """
    if per_viewer_mbps <= 0:
        raise ValueError("per_viewer_mbps must be positive")
    if uplink_mbps <= 0:
        raise ValueError("uplink_mbps must be positive")
    return int(uplink_mbps // per_viewer_mbps)


# ---------------------------------------------------------------------------
# Load run
# ---------------------------------------------------------------------------


async def _run_viewer(
    client: httpx.AsyncClient,
    manifest_url: str,
    *,
    deadline: float,
    clock: Callable[[], float],
) -> _ViewerTally:
    """Emulate one HLS player until ``deadline``."""
    tally = _ViewerTally()
    started = clock()
    active_manifest_url = manifest_url
    seen_segments: set[str] = set()
    resolved_variant = False

    while clock() < deadline:
        cycle_start = clock()
        try:
            response = await client.get(active_manifest_url)
        except httpx.HTTPError:
            tally.manifest_failed += 1
            await asyncio.sleep(1.0)
            continue
        if not response.is_success:
            tally.manifest_failed += 1
            await asyncio.sleep(1.0)
            continue
        try:
            playlist = parse_media_playlist(response.text)
        except ValueError:
            # A malformed manifest body (e.g. a truncated tag) is treated like
            # an unreachable manifest, not a crash that takes the whole run down.
            tally.manifest_failed += 1
            await asyncio.sleep(1.0)
            continue
        tally.manifest_ok += 1

        # A multivariant manifest: follow the first variant once, then treat
        # that as the media playlist for the rest of the run.
        if playlist.is_multivariant and not resolved_variant:
            active_manifest_url = urljoin(active_manifest_url, playlist.variant_uris[0])
            resolved_variant = True
            continue

        for segment_uri in playlist.segment_uris:
            segment_url = urljoin(active_manifest_url, segment_uri)
            if segment_url in seen_segments:
                continue
            seen_segments.add(segment_url)
            fetch_start = clock()
            try:
                segment_response = await client.get(segment_url)
            except httpx.HTTPError:
                tally.segments_failed += 1
                continue
            if not segment_response.is_success:
                tally.segments_failed += 1  # rolled out of window / server overwhelmed
                continue
            tally.segments_ok += 1
            tally.bytes_downloaded += len(segment_response.content)
            if tally.join_latency_s is None:
                tally.join_latency_s = fetch_start - started
            if playlist.target_duration and (clock() - fetch_start) > playlist.target_duration:
                tally.segments_slow += 1  # took longer than a segment to fetch -> behind live

        if playlist.is_endlist:
            break

        # Re-poll at the segment cadence (default 2s if the manifest omits it).
        interval = playlist.target_duration or 2.0
        elapsed = clock() - cycle_start
        await asyncio.sleep(max(0.0, interval - elapsed))

    return tally


async def run_load(
    manifest_url: str,
    *,
    viewers: int,
    duration_s: float,
    client: httpx.AsyncClient | None = None,
    uplink_mbps: float | None = None,
    per_viewer_mbps: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> LoadReport:
    """Run ``viewers`` concurrent emulated HLS players for ``duration_s`` seconds.

    Pass ``client`` to reuse an existing :class:`httpx.AsyncClient` (tests pass
    one backed by an ASGI transport); otherwise a real network client is built.
    When ``uplink_mbps`` and ``per_viewer_mbps`` are given, the report also
    states the modeled bandwidth ceiling.
    """
    if viewers < 1:
        raise ValueError("viewers must be >= 1")
    owns_client = client is None
    if client is None:
        limits = httpx.Limits(max_connections=viewers * 2, max_keepalive_connections=viewers)
        client = httpx.AsyncClient(timeout=httpx.Timeout(10.0), limits=limits)

    wall_start = clock()
    deadline = wall_start + duration_s
    try:
        raw_results = await asyncio.gather(
            *(
                _run_viewer(client, manifest_url, deadline=deadline, clock=clock)
                for _ in range(viewers)
            ),
            return_exceptions=True,
        )
    finally:
        if owns_client:
            await client.aclose()
    wall_time = clock() - wall_start

    # return_exceptions=True keeps one viewer's unexpected exception from
    # discarding every other viewer's already-collected tallies (the parse
    # branch above is the known case; this is defense-in-depth for the rest).
    tallies = [t for t in raw_results if isinstance(t, _ViewerTally)]
    crashed = len(raw_results) - len(tallies)

    join_latencies = sorted(t.join_latency_s for t in tallies if t.join_latency_s is not None)
    notes: list[str] = []
    connected = len(join_latencies)
    if connected < len(tallies):
        notes.append(f"{len(tallies) - connected}/{viewers} viewers never received a segment")
    if crashed:
        notes.append(f"{crashed}/{viewers} viewers crashed with an unhandled exception")

    ceiling = (
        bandwidth_ceiling(uplink_mbps, per_viewer_mbps)
        if uplink_mbps is not None and per_viewer_mbps is not None
        else None
    )

    return LoadReport(
        viewers=viewers,
        duration_s=duration_s,
        wall_time_s=wall_time,
        manifest_ok=sum(t.manifest_ok for t in tallies),
        manifest_failed=sum(t.manifest_failed for t in tallies),
        segments_ok=sum(t.segments_ok for t in tallies),
        segments_failed=sum(t.segments_failed for t in tallies),
        segments_slow=sum(t.segments_slow for t in tallies),
        bytes_downloaded=sum(t.bytes_downloaded for t in tallies),
        join_latency_p50_s=_percentile(join_latencies, 50),
        join_latency_p95_s=_percentile(join_latencies, 95),
        bandwidth_ceiling_viewers=ceiling,
        per_viewer_mbps=per_viewer_mbps,
        uplink_mbps=uplink_mbps,
        notes=notes,
    )


def _percentile(sorted_values: list[float], pct: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100) * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    frac = rank - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _exit_code(report: LoadReport) -> int:
    """Non-zero if any viewer stalled -- lets CI / scripts gate on a clean run."""
    return 1 if report.stall_rate > 0 or report.manifest_failed > 0 else 0


def _render_report(report: LoadReport, *, as_json: bool) -> str:
    return json.dumps(report.as_dict(), indent=2) if as_json else report.summary()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m civiccast.load.hls_load",
        description="Concurrent HLS live-viewer load generator.",
    )
    parser.add_argument("--manifest-url", required=True, help="Live media playlist URL (.m3u8)")
    parser.add_argument("--viewers", type=int, default=50, help="Concurrent emulated viewers")
    parser.add_argument("--duration", type=float, default=30.0, help="Run duration (seconds)")
    parser.add_argument(
        "--uplink-mbps",
        type=float,
        default=None,
        help="Station uplink, for the modeled bandwidth ceiling",
    )
    parser.add_argument(
        "--per-viewer-mbps",
        type=float,
        default=None,
        help="Per-viewer stream bitrate, for the modeled bandwidth ceiling",
    )
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON")
    args = parser.parse_args(argv)

    report = asyncio.run(
        run_load(
            args.manifest_url,
            viewers=args.viewers,
            duration_s=args.duration,
            uplink_mbps=args.uplink_mbps,
            per_viewer_mbps=args.per_viewer_mbps,
        )
    )
    print(_render_report(report, as_json=args.json))
    return _exit_code(report)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
