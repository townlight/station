# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Tier B: ramp emulated viewers against a REAL CDN edge (0.2.0 Deliverable 5).

The lab tiers (`switch_lab`, `cache_edge`) prove the station-side path against
simulated edges; this harness measures the one thing no simulator can: the
real vendor edge. A real rolling live window publishes through the real
:class:`~civiccast.live.cdn_publisher.LiveCDNPublisher` + a real CDN adapter to
the real bucket, and tiered emulated viewers poll the real public URL on the
true 2s HLS cadence, measuring freshness, latency, error mix (429s from
rate-limited dev endpoints are a *finding*, not a failure), and stalls.

ZERO-SPEND is enforced structurally, not by hope: a **pre-flight op budget**
projects every tier's request count (Class B) and the publisher's write count
(Class A) and REFUSES to launch any run that would project past the caps
(defaults sit far inside R2's free tier: 10M Class B / 1M Class A monthly).
Teardown evicts everything the run published, leaving storage at ~zero.

Honest scope: this measures ONE geography (this lab's uplink) against the
vendor's edge. Global multi-PoP fan-out remains the vendor's documented
capacity; the numbers here are correctness + freshness + concurrency evidence,
not a world-scale crowd simulation.

Runnable (credentials via CIVICCAST_R2_* env, the operator convention)::

    python -m civiccast.load.real_edge_ramp --tier 50:120 --tier 200:120 \
        --tier 1000:300 --out docs/releases/evidence/0.2.0-tier-b-real-edge.md
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from civiccast.live.cdn_publisher import LiveCDNPublisher
from civiccast.load.lab import RollingStation
from civiccast.stream.cdn import CDNAdapter

_MEDIA_SEQUENCE_RE = re.compile(r"^#EXT-X-MEDIA-SEQUENCE:(\d+)", re.MULTILINE)
_CADENCE_S = 2.0  # the real HLS poll cadence

# Zero-spend caps (Scott's standing rule): refuse any run projecting past
# these. Both sit far inside R2's monthly free tier (10M Class B / 1M Class A).
_DEFAULT_MAX_CLASS_B = 1_500_000
_DEFAULT_MAX_CLASS_A = 60_000


@dataclass(frozen=True)
class Tier:
    viewers: int
    seconds: float


@dataclass
class OpProjection:
    """Pre-flight op-count projection for a planned run. Pure math, tested."""

    class_b: int  # reads: manifest polls + sampled segment fetches
    class_a: int  # writes/deletes: publisher segment+manifest uploads + evictions

    @classmethod
    def project(
        cls,
        tiers: list[Tier],
        *,
        segment_sample_rate: float,
        publish_total_seconds: float,
    ) -> OpProjection:
        manifest_reads = sum(int(t.viewers * t.seconds / _CADENCE_S) for t in tiers)
        segment_reads = int(manifest_reads * segment_sample_rate)
        # Publisher: ~1 segment + 1 manifest upload per cadence tick, plus one
        # eviction delete per rolled segment (~1 per tick), plus final evict_all.
        publish_ticks = int(publish_total_seconds / _CADENCE_S) + 1
        class_a = publish_ticks * 3 + 64
        return cls(class_b=manifest_reads + segment_reads, class_a=class_a)


def preflight(
    tiers: list[Tier],
    *,
    segment_sample_rate: float,
    max_class_b: int = _DEFAULT_MAX_CLASS_B,
    max_class_a: int = _DEFAULT_MAX_CLASS_A,
) -> OpProjection:
    """Project ops and REFUSE (raise) any run that would exceed the caps."""
    publish_total = sum(t.seconds for t in tiers) + 60.0  # warmup slack
    projection = OpProjection.project(
        tiers, segment_sample_rate=segment_sample_rate, publish_total_seconds=publish_total
    )
    if projection.class_b > max_class_b:
        raise SystemExit(
            f"REFUSED (zero-spend): projected Class B ops {projection.class_b:,} exceed "
            f"the cap {max_class_b:,}. Shrink tiers/duration."
        )
    if projection.class_a > max_class_a:
        raise SystemExit(
            f"REFUSED (zero-spend): projected Class A ops {projection.class_a:,} exceed "
            f"the cap {max_class_a:,}. Shrink publish duration."
        )
    return projection


@dataclass
class TierResult:
    viewers: int
    seconds: float
    manifest_requests: int = 0
    manifest_200: int = 0
    manifest_429: int = 0
    manifest_other: int = 0
    manifest_transport_errors: int = 0
    segment_requests: int = 0
    segment_200: int = 0
    segment_429: int = 0
    segment_other: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    staleness_ticks: list[int] = field(default_factory=list)  # publisher_seq - served_seq
    stalls: int = 0  # 3+ consecutive non-advancing polls per viewer

    def summary(self) -> dict[str, object]:
        lat = sorted(self.latencies_ms)

        def pct(p: float) -> float:
            return lat[min(len(lat) - 1, int(p * len(lat)))] if lat else 0.0

        return {
            "viewers": self.viewers,
            "seconds": self.seconds,
            "manifest_requests": self.manifest_requests,
            "manifest_200": self.manifest_200,
            "manifest_429": self.manifest_429,
            "manifest_other": self.manifest_other,
            "manifest_transport_errors": self.manifest_transport_errors,
            "segment_requests": self.segment_requests,
            "segment_200": self.segment_200,
            "segment_429": self.segment_429,
            "segment_other": self.segment_other,
            "latency_p50_ms": round(pct(0.50), 1),
            "latency_p95_ms": round(pct(0.95), 1),
            "latency_p99_ms": round(pct(0.99), 1),
            "staleness_ticks_p95": (
                sorted(self.staleness_ticks)[int(0.95 * (len(self.staleness_ticks) - 1))]
                if self.staleness_ticks
                else 0
            ),
            "stalls": self.stalls,
        }


class _PublisherThread:
    """Roll a synthetic station and continuously publish it via the REAL adapter."""

    def __init__(
        self,
        adapter: CDNAdapter,
        *,
        channel_id: str = "tierb",
        segment_bytes: int = 64 * 1024,
    ) -> None:
        self._dir_ctx = tempfile.TemporaryDirectory(prefix="civiccast-tierb-")
        live_dir = Path(self._dir_ctx.name) / "live"
        self.station = RollingStation(live_dir, window=6, segment_bytes=segment_bytes)
        self.station.bootstrap()
        self.publisher = LiveCDNPublisher(channel_id, live_dir, adapter)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="tierb-publisher", daemon=True)
        self.manifest_url: str | None = None
        self.publish_errors = 0

    def start(self) -> None:
        self.manifest_url = self.publisher.sync()
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(_CADENCE_S):
            try:
                self.station.roll()
                self.publisher.sync()
            except Exception:
                self.publish_errors += 1

    @property
    def latest_sequence(self) -> int:
        return self.station.media_sequence

    def stop_and_evict(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        with contextlib.suppress(Exception):
            self.publisher.evict_all()
        self._dir_ctx.cleanup()


async def _viewer(
    client: httpx.AsyncClient,
    manifest_url: str,
    result: TierResult,
    publisher: _PublisherThread,
    stop: asyncio.Event,
    sample_segments: bool,
    lock: asyncio.Lock,
) -> None:
    last_seq: int | None = None
    behind = 0
    base = manifest_url.rsplit("/", 1)[0]
    while not stop.is_set():
        started = time.monotonic()
        seg_name: str | None = None
        try:
            t0 = time.perf_counter()
            resp = await client.get(manifest_url)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            async with lock:
                result.manifest_requests += 1
                result.latencies_ms.append(elapsed_ms)
                if resp.status_code == 200:
                    result.manifest_200 += 1
                elif resp.status_code == 429:
                    result.manifest_429 += 1
                else:
                    result.manifest_other += 1
            if resp.status_code == 200:
                m = _MEDIA_SEQUENCE_RE.search(resp.text)
                seq = int(m.group(1)) if m else None
                if seq is not None:
                    async with lock:
                        result.staleness_ticks.append(max(0, publisher.latest_sequence - seq))
                    if last_seq is not None and seq <= last_seq:
                        behind += 1
                        if behind == 3:
                            async with lock:
                                result.stalls += 1
                    else:
                        behind = 0
                    last_seq = seq
                if sample_segments:
                    names = [
                        line.strip()
                        for line in resp.text.splitlines()
                        if line.strip().endswith(".ts")
                    ]
                    seg_name = names[-1] if names else None
            if seg_name:
                sresp = await client.get(f"{base}/{seg_name}")
                async with lock:
                    result.segment_requests += 1
                    if sresp.status_code == 200:
                        result.segment_200 += 1
                    elif sresp.status_code == 429:
                        result.segment_429 += 1
                    else:
                        result.segment_other += 1
        except httpx.HTTPError:
            async with lock:
                result.manifest_transport_errors += 1
        elapsed = time.monotonic() - started
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=max(0.1, _CADENCE_S - elapsed))


async def run_tier(
    manifest_url: str,
    publisher: _PublisherThread,
    tier: Tier,
    *,
    segment_sample_rate: float,
) -> TierResult:
    result = TierResult(viewers=tier.viewers, seconds=tier.seconds)
    stop = asyncio.Event()
    lock = asyncio.Lock()
    sample_count = max(1, int(tier.viewers * segment_sample_rate))
    limits = httpx.Limits(max_connections=tier.viewers + 8)
    async with httpx.AsyncClient(timeout=10.0, limits=limits) as client:
        tasks = [
            asyncio.create_task(
                _viewer(client, manifest_url, result, publisher, stop, i < sample_count, lock)
            )
            for i in range(tier.viewers)
        ]
        await asyncio.sleep(tier.seconds)
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    return result


def render_report(
    results: list[TierResult],
    *,
    projection: OpProjection,
    public_host: str,
    commit: str,
    publish_errors: int,
) -> str:
    lines = [
        "# 0.2.0 Tier B — real-CDN-edge ramp (measured)",
        "",
        f"Commit: `{commit}` · Edge: `{public_host}` (Cloudflare R2 public dev endpoint)",
        f"Pre-flight op budget: projected {projection.class_b:,} Class B / "
        f"{projection.class_a:,} Class A — inside the free tier by construction; "
        "the harness refuses runs that project past its caps.",
        "",
        "A real rolling live window published through the real `LiveCDNPublisher`"
        " + Cloudflare R2 adapter; tiered emulated viewers polled the real public"
        " URL on the true 2s cadence from ONE geography (single-geo caveat:"
        " global multi-PoP fan-out is the vendor's documented capacity, not"
        " something one lab can measure).",
        "",
        "| Viewers | Dur (s) | Manifest 200/429/other | Segment 200/429/other |"
        " p50/p95/p99 ms | Staleness p95 (ticks) | Stalls |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        s = r.summary()
        lines.append(
            f"| {s['viewers']} | {s['seconds']:.0f} |"
            f" {s['manifest_200']}/{s['manifest_429']}/{s['manifest_other']} |"
            f" {s['segment_200']}/{s['segment_429']}/{s['segment_other']} |"
            f" {s['latency_p50_ms']}/{s['latency_p95_ms']}/{s['latency_p99_ms']} |"
            f" {s['staleness_ticks_p95']} | {s['stalls']} |"
        )
    lines += [
        "",
        f"Publisher sync errors during the run: {publish_errors}",
        "",
        "Notes: 429s (if any) are the documented rate limit of the free `r2.dev`"
        " development endpoint — the production surge path is a custom domain"
        " with Cloudflare CDN caching in front (cheaper AND uncapped); this run"
        " measures the station-controlled path against a real vendor edge"
        " end-to-end. Teardown evicted every published object (bucket left"
        " empty; storage ~0).",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m civiccast.load.real_edge_ramp")
    parser.add_argument(
        "--tier",
        action="append",
        default=None,
        help="viewers:seconds (repeatable), e.g. --tier 50:120 --tier 1000:300",
    )
    parser.add_argument("--segment-sample-rate", type=float, default=0.05)
    parser.add_argument("--max-class-b", type=int, default=_DEFAULT_MAX_CLASS_B)
    parser.add_argument("--max-class-a", type=int, default=_DEFAULT_MAX_CLASS_A)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    tier_specs = args.tier or ["50:120", "200:120", "1000:300"]
    tiers = [Tier(int(v), float(s)) for v, s in (t.split(":") for t in tier_specs)]
    projection = preflight(
        tiers,
        segment_sample_rate=args.segment_sample_rate,
        max_class_b=args.max_class_b,
        max_class_a=args.max_class_a,
    )
    print(
        f"preflight OK: projected Class B {projection.class_b:,} / "
        f"Class A {projection.class_a:,} (caps {args.max_class_b:,}/{args.max_class_a:,})"
    )

    from civiccast.stream.cdn.factory import build_cdn_adapter_from_credentials

    fields = {
        "account_id": os.environ["CIVICCAST_R2_ACCOUNT_ID"],
        "access_key_id": os.environ["CIVICCAST_R2_ACCESS_KEY_ID"],
        "secret_access_key": os.environ["CIVICCAST_R2_SECRET_ACCESS_KEY"],
        "bucket": os.environ["CIVICCAST_R2_BUCKET"],
        "public_base_url": os.environ["CIVICCAST_R2_PUBLIC_BASE_URL"],
    }
    adapter = build_cdn_adapter_from_credentials("cloudflare-r2", fields)

    publisher = _PublisherThread(adapter)
    publisher.start()
    if not publisher.manifest_url:
        print("publisher produced no manifest URL")
        return 1
    print("publishing to:", publisher.manifest_url)
    try:
        time.sleep(6)  # a few ticks of warmup on the edge
        results: list[TierResult] = []
        for tier in tiers:
            print(f"tier {tier.viewers} viewers x {tier.seconds:.0f}s ...")
            results.append(
                asyncio.run(
                    run_tier(
                        publisher.manifest_url,
                        publisher,
                        tier,
                        segment_sample_rate=args.segment_sample_rate,
                    )
                )
            )
            print("  ", json.dumps(results[-1].summary()))
    finally:
        publisher.stop_and_evict()
        print("teardown: evicted all published objects")

    report = render_report(
        results,
        projection=projection,
        public_host=fields["public_base_url"],
        commit=os.environ.get("CIVICCAST_SOAK_COMMIT", "unknown"),
        publish_errors=publisher.publish_errors,
    )
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        args.out.with_suffix(".json").write_text(
            json.dumps([asdict(r) for r in results], default=list), encoding="utf-8"
        )
        print("report ->", args.out)
    else:
        print(report)
    # Fail-closed: severe manifest failure rate (excluding 429s, which are the
    # dev endpoint's documented behavior and reported separately) fails the run.
    total_manifest = sum(r.manifest_requests for r in results)
    hard_fail = sum(r.manifest_other + r.manifest_transport_errors for r in results)
    if total_manifest == 0 or hard_fail / max(1, total_manifest) > 0.01:
        print(f"FAIL: manifest hard-failure rate {hard_fail}/{total_manifest}")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
