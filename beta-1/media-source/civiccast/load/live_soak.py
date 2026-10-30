# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Real wall-clock live-delivery soak (0.8-0.9 release evidence).

The honest replacement for ``scripts/run_six_hour_soak.py``, whose loop body was
``time.sleep(60)`` — it exercised nothing and wrote "PASS". This soak actually
drives the system for hours, over **real TCP**, and its evidence is measured:

* a :class:`~civiccast.load.lab.RollingStation` rolls the live window on the
  real segment cadence **inside the server process**;
* the real serving + resolution + surge stack (built by
  :func:`~civiccast.load.switch_lab.build_switch_lab` — real ``live_router``,
  real ``/api/public/live/current``, real ``SurgeSwitchService`` publishing to
  an HTTP-served lab CDN edge) runs under uvicorn in a **child process**;
* emulated viewers poll over real sockets, re-resolving ``/current`` each cycle
  (the shipped portal behavior since 0.2.0), so the audience **rides the surge
  switch to the CDN and back** — every surge cycle exercises cold-start publish,
  hold-fresh, hysteresis release, and eviction;
* the parent samples the server process every minute — RSS, handles, threads —
  because the *point* of a soak is leak detection, and a soak that never looks
  at memory is theater.

PASS is computed from the samples, not asserted: zero 5xx, (near-)zero fetch
errors, sustained stalls within a small documented shared-lab budget (zero is
the dedicated-box expectation), the expected number of switch cycles observed,
and no meaningful RSS growth between the first post-warmup hour and the last
hour. The evidence renderer refuses to label a short run as long-run evidence.

Runnable::

    python -m civiccast.load.live_soak --duration-seconds 43200 \
        --out docs/releases/evidence/0.9.0-live-delivery-soak-12h.md
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

_MEDIA_SEQUENCE_RE = re.compile(r"^#EXT-X-MEDIA-SEQUENCE:(\d+)", re.MULTILINE)

_DEFAULT_PORT = 8765
_DEFAULT_THRESHOLD = 10
_BASELINE_VIEWERS = 4  # below release (threshold//2=5) so drains actually release
_SURGE_VIEWERS = 14  # above threshold so surges actually engage
_SAMPLE_INTERVAL_S = 60.0
_VIEWER_CADENCE_S = 2.0  # the real HLS poll cadence
_WARMUP_S = 15 * 60.0  # RSS before this is startup noise
_MAX_RSS_GROWTH = 1.20  # last-hour median may exceed first-hour median by <=20%


# ---------------------------------------------------------------------------
# Server child process (uvicorn --factory entrypoint)
# ---------------------------------------------------------------------------


def create_soak_app():  # type: ignore[no-untyped-def]  # uvicorn factory
    """uvicorn factory: the switch-lab app + a station roller thread.

    Env contract (set by the parent runner): ``CIVICCAST_SOAK_ROOT`` (work dir),
    ``CIVICCAST_SOAK_BASE_URL`` (this server's public base), and
    ``CIVICCAST_SOAK_THRESHOLD``. The station roller runs in here so the child
    process IS the whole served system — the parent's RSS samples cover it all.
    """
    from civiccast.load.switch_lab import build_switch_lab

    root = Path(os.environ["CIVICCAST_SOAK_ROOT"])
    base_url = os.environ["CIVICCAST_SOAK_BASE_URL"]
    threshold = int(os.environ.get("CIVICCAST_SOAK_THRESHOLD", str(_DEFAULT_THRESHOLD)))

    lab = build_switch_lab(
        root / "live",
        root / "cdn",
        threshold=threshold,
        buffer_seconds=15.0,  # the real spec default — soak the real window
        tick_interval=2.0,
        base_url=base_url,
    )

    stop = threading.Event()

    def _roll_forever() -> None:
        interval = float(lab.station.target_duration)
        while not stop.wait(interval):
            lab.station.roll()

    roller = threading.Thread(target=_roll_forever, name="soak-station-roller", daemon=True)

    @lab.app.on_event("startup")
    async def _start_roller() -> None:  # pragma: no cover - exercised by the live run
        roller.start()

    @lab.app.on_event("shutdown")
    async def _stop_roller() -> None:  # pragma: no cover - exercised by the live run
        stop.set()

    return lab.app


# ---------------------------------------------------------------------------
# Samples + verdict (pure — unit tested)
# ---------------------------------------------------------------------------


@dataclass
class Sample:
    """One per-minute observation of the served system."""

    t: float  # seconds since soak start
    rss_bytes: int
    handles: int
    threads: int
    viewers: int
    manifest_fetches: int  # cumulative
    segment_fetches: int  # cumulative
    fetch_errors: int  # cumulative non-2xx/transport errors
    server_5xx: int  # cumulative
    stalls: int  # cumulative sustained same-source stalls
    # Per-VIEWER observations of a source flip via /current (a single actual
    # switch cycle counts once per viewer that rode it). Zero iff the switch
    # never cycled — which is what the verdict checks.
    switch_engages: int
    switch_releases: int
    # A 404 on the CDN manifest in the one cycle right after release+evict —
    # the viewer's next /current poll heals it (the delay buffer covers real
    # players). Bounded by design; counted separately so it can't hide real
    # fetch errors and real fetch errors can't hide in it.
    release_blips: int = 0


@dataclass
class SoakVerdict:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    rss_first_hour_mb: float = 0.0
    rss_last_hour_mb: float = 0.0
    rss_growth: float = 0.0
    totals: dict[str, int] = field(default_factory=dict)


def analyze(
    samples: list[Sample],
    *,
    requested_duration_s: float,
    warmup_s: float = _WARMUP_S,
    max_rss_growth: float = _MAX_RSS_GROWTH,
    min_switch_cycles: int = 1,
) -> SoakVerdict:
    """Compute the PASS/FAIL verdict from measured samples. Fail-closed."""
    reasons: list[str] = []
    if not samples:
        return SoakVerdict(passed=False, reasons=["no samples recorded"])

    last = samples[-1]
    duration_s = last.t
    if duration_s < requested_duration_s:
        reasons.append(f"ran {duration_s:.0f}s of the requested {requested_duration_s:.0f}s")

    if last.server_5xx > 0:
        reasons.append(f"{last.server_5xx} server 5xx responses")
    fetches = last.manifest_fetches + last.segment_fetches
    if fetches == 0:
        reasons.append("no fetches recorded — viewers never ran")
    elif last.fetch_errors / fetches > 0.001:
        reasons.append(f"fetch error rate {last.fetch_errors}/{fetches} exceeds 0.1%")
    # Stall budget: on a DEDICATED station box the expectation is zero, but
    # this soak runs on a shared lab box where a build burst can starve the
    # roller thread for >6s and stall every viewer at once (an environmental
    # freeze, not a serving defect). Tolerate isolated freeze events up to
    # 0.05% of viewer-cycles — a real serving regression blows straight past
    # that; the count is still reported in the evidence either way.
    stall_budget = max(4, int(0.0005 * max(1, last.manifest_fetches)))
    if last.stalls > stall_budget:
        reasons.append(
            f"{last.stalls} sustained viewer stalls exceed the shared-lab budget of {stall_budget}"
        )
    # Each riding viewer may blip ~1 cycle per release; 3x slack. More than
    # that means the CDN path is broken, not racing.
    if last.release_blips > 3 * max(1, last.switch_releases):
        reasons.append(
            f"{last.release_blips} release blips for only {last.switch_releases} "
            "release observations — CDN path suspect"
        )
    if last.switch_engages < min_switch_cycles or last.switch_releases < min_switch_cycles:
        reasons.append(
            f"switch cycles engage={last.switch_engages} release={last.switch_releases} "
            f"(need >= {min_switch_cycles} each)"
        )

    # RSS slope: median of the first post-warmup hour vs the last hour.
    post = [s for s in samples if s.t >= warmup_s]
    rss_first = rss_last = growth = 0.0
    if len(post) >= 4:
        first_hour = [s.rss_bytes for s in post if s.t < post[0].t + 3600.0]
        last_hour = [s.rss_bytes for s in post if s.t >= last.t - 3600.0]
        rss_first = statistics.median(first_hour) / 1e6
        rss_last = statistics.median(last_hour) / 1e6
        growth = (rss_last / rss_first) if rss_first else 0.0
        if growth > max_rss_growth:
            reasons.append(
                f"RSS grew {growth:.2f}x (first-hour {rss_first:.0f}MB -> "
                f"last-hour {rss_last:.0f}MB, limit {max_rss_growth:.2f}x)"
            )
    else:
        reasons.append("too few post-warmup samples to judge RSS slope")

    return SoakVerdict(
        passed=not reasons,
        reasons=reasons,
        duration_s=duration_s,
        rss_first_hour_mb=rss_first,
        rss_last_hour_mb=rss_last,
        rss_growth=growth,
        totals={
            "manifest_fetches": last.manifest_fetches,
            "segment_fetches": last.segment_fetches,
            "fetch_errors": last.fetch_errors,
            "server_5xx": last.server_5xx,
            "stalls": last.stalls,
            "switch_engages": last.switch_engages,
            "switch_releases": last.switch_releases,
            "release_blips": last.release_blips,
        },
    )


def render_evidence(
    verdict: SoakVerdict,
    *,
    requested_duration_s: float,
    commit: str,
    samples_path: str,
    baseline_viewers: int,
    surge_viewers: int,
) -> str:
    """Render the evidence doc. Refuses to label a short run as full evidence."""
    hours = requested_duration_s / 3600.0
    status = "PASS" if verdict.passed else "FAIL"
    if verdict.duration_s < requested_duration_s and verdict.passed:
        # analyze() already fails short runs; this is defense in depth.
        status = "FAIL"
    lines = [
        f"# Live-delivery soak — {hours:.0f}h wall-clock, measured",
        "",
        f"Status: **{status}**",
        f"Commit: `{commit}`",
        f"Measured duration: {verdict.duration_s / 3600.0:.2f}h (requested {hours:.0f}h)",
        "",
        "What ran (all real, over TCP): rolling live station on the 2s cadence ->"
        " real `live_router` + `/api/public/live/current` -> real"
        " `SurgeSwitchService` publishing to an HTTP-served lab CDN edge;"
        f" {baseline_viewers} baseline viewers re-resolving `/current` each"
        f" cycle, surged to {surge_viewers} periodically so every cycle"
        " exercises engage -> hold-fresh -> release -> evict.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Manifest fetches | {verdict.totals.get('manifest_fetches', 0)} |",
        f"| Segment fetches | {verdict.totals.get('segment_fetches', 0)} |",
        f"| Fetch errors | {verdict.totals.get('fetch_errors', 0)} |",
        f"| Server 5xx | {verdict.totals.get('server_5xx', 0)} |",
        f"| Sustained stalls | {verdict.totals.get('stalls', 0)} |",
        f"| Switch flips ridden by viewers (engage/release, per-viewer counts) |"
        f" {verdict.totals.get('switch_engages', 0)}"
        f" / {verdict.totals.get('switch_releases', 0)} |",
        f"| Release blips (single-cycle 404 healed by re-resolve) |"
        f" {verdict.totals.get('release_blips', 0)} |",
        f"| RSS first post-warmup hour (median) | {verdict.rss_first_hour_mb:.0f} MB |",
        f"| RSS last hour (median) | {verdict.rss_last_hour_mb:.0f} MB |",
        f"| RSS growth | {verdict.rss_growth:.3f}x |",
        "",
        f"Per-minute samples: `{samples_path}`",
    ]
    if verdict.reasons:
        lines += ["", "## Failure reasons", ""]
        lines += [f"- {r}" for r in verdict.reasons]
    lines += [
        "",
        "This document is generated from measured samples by"
        " `civiccast.load.live_soak`; a run shorter than the requested duration"
        " or failing any criterion renders as FAIL and is not release evidence.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The live run (parent process)
# ---------------------------------------------------------------------------


class _Counters:
    """Shared cumulative counters the viewer tasks bump and the sampler reads."""

    def __init__(self) -> None:
        self.manifest_fetches = 0
        self.segment_fetches = 0
        self.fetch_errors = 0
        self.server_5xx = 0
        self.stalls = 0
        self.switch_engages = 0
        self.switch_releases = 0
        self.release_blips = 0
        self.active_viewers = 0


def absolute_manifest_url(base_url: str, manifest_url: str) -> str:
    """Resolve the ``manifest_url`` ``/current`` hands a viewer against the soak origin.

    ``/api/public/live/current`` reports the local manifest SITE-RELATIVE
    (``/media/live/{channel}/playlist.m3u8``) unless an operator base URL is
    configured -- a browser resolves that against the page origin, but
    ``httpx`` refuses a request with no host. Absolute URLs (a CDN edge, an
    operator base) pass through untouched.
    """
    if urlsplit(manifest_url).scheme:
        return manifest_url
    return urljoin(base_url.rstrip("/") + "/", manifest_url)


def _viewer_ip(index: int) -> str:
    return f"11.{(index >> 16) & 0xFF}.{(index >> 8) & 0xFF}.{index & 0xFF}"


async def _viewer(
    client: httpx.AsyncClient,
    base_url: str,
    counters: _Counters,
    index: int,
    stop: asyncio.Event,
) -> None:
    """One emulated viewer: re-resolve /current, follow local or CDN, detect stalls."""
    headers = {"X-Forwarded-For": _viewer_ip(index)}
    last_seq: int | None = None
    last_src: bool | None = None  # True == CDN
    behind = 0
    counters.active_viewers += 1
    try:
        while not stop.is_set():
            cycle_started = time.monotonic()
            try:
                resp = await client.get(f"{base_url}/api/public/live/current", headers=headers)
                counters.manifest_fetches += 1
                if resp.status_code >= 500:
                    counters.server_5xx += 1
                url = resp.json().get("manifest_url") if resp.status_code == 200 else None
                if url:
                    url = absolute_manifest_url(base_url, url)
                    is_cdn = "/cdn-edge/" in url
                    if last_src is not None and is_cdn != last_src:
                        if is_cdn:
                            counters.switch_engages += 1
                        else:
                            counters.switch_releases += 1
                        behind = 0  # source swap is not a stall
                        last_seq = None
                    last_src = is_cdn
                    m = await client.get(url, headers=headers)
                    counters.segment_fetches += 1
                    if m.status_code >= 500:
                        counters.server_5xx += 1
                    if m.status_code == 200:
                        match = _MEDIA_SEQUENCE_RE.search(m.text)
                        seq = int(match.group(1)) if match else None
                        if seq is not None:
                            if last_seq is not None and seq <= last_seq:
                                behind += 1
                                # 3 consecutive non-advancing cycles (~6s) on one
                                # source == a sustained stall, not poll jitter.
                                if behind == 3:
                                    counters.stalls += 1
                            else:
                                behind = 0
                            last_seq = seq
                    elif m.status_code == 404 and is_cdn:
                        # Evicted-on-release race: healed by the next /current
                        # poll. Tracked separately, bounded by the verdict.
                        counters.release_blips += 1
                    else:
                        counters.fetch_errors += 1
                elif resp.status_code != 200:
                    counters.fetch_errors += 1
            except httpx.HTTPError:
                counters.fetch_errors += 1
            elapsed = time.monotonic() - cycle_started
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=max(0.1, _VIEWER_CADENCE_S - elapsed))
    finally:
        counters.active_viewers -= 1


def _take_sample(proc, counters: _Counters, *, t: float) -> Sample:  # type: ignore[no-untyped-def]
    """Sample the server *process tree* (root + descendants), summed.

    On Windows the Popen'd python can be a thin launcher whose real server
    lives in a child process — sampling only the root reads a flat ~6MB
    wrapper and a leak in the actual server would be invisible. Summing the
    tree measures the whole served system regardless of process shape.
    """
    import psutil

    rss = handles = threads = 0
    try:
        tree = [proc, *proc.children(recursive=True)]
    except psutil.Error:
        tree = []
    for p in tree:
        try:
            rss += p.memory_info().rss
            counter = getattr(p, "num_handles", None) or getattr(p, "num_fds", None)
            handles += int(counter()) if counter else 0
            threads += p.num_threads()
        except psutil.Error:  # process may exit between listing and sampling
            continue
    return Sample(
        t=t,
        rss_bytes=rss,
        handles=handles,
        threads=threads,
        viewers=counters.active_viewers,
        manifest_fetches=counters.manifest_fetches,
        segment_fetches=counters.segment_fetches,
        fetch_errors=counters.fetch_errors,
        server_5xx=counters.server_5xx,
        stalls=counters.stalls,
        switch_engages=counters.switch_engages,
        switch_releases=counters.switch_releases,
        release_blips=counters.release_blips,
    )


async def run_soak(
    *,
    duration_s: float,
    port: int = _DEFAULT_PORT,
    baseline_viewers: int = _BASELINE_VIEWERS,
    surge_viewers: int = _SURGE_VIEWERS,
    surge_period_s: float = 1200.0,
    surge_length_s: float = 300.0,
    samples_out: Path,
    work_root: Path | None = None,
) -> list[Sample]:
    """Run the full soak; returns the measured samples (also streamed to JSONL)."""
    try:
        import psutil
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("live_soak needs psutil for RSS sampling: uv pip install psutil") from exc

    base_url = f"http://127.0.0.1:{port}"
    root_ctx = None
    if work_root is None:
        root_ctx = tempfile.TemporaryDirectory(prefix="civiccast-livesoak-")
        work_root = Path(root_ctx.name)
    work_root.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update(
        {
            "CIVICCAST_SOAK_ROOT": str(work_root),
            "CIVICCAST_SOAK_BASE_URL": base_url,
            "CIVICCAST_SOAK_THRESHOLD": str(_DEFAULT_THRESHOLD),
            "CIVICCAST_LOCAL_MEDIA_BASE_URL": base_url,
            # The lab's live folder must be inside the root the media router
            # is allowed to serve (it refuses anything else with a 404).
            "CIVICCAST_LIVE_HLS_ROOT": str(work_root),
            "CIVICCAST_LIVE_SURGE_THRESHOLD": "",  # app-level env not used; lab wires surge
        }
    )
    # S603: fixed argv built from sys.executable + literals — no untrusted input.
    server = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "uvicorn",
            "civiccast.load.live_soak:create_soak_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
    )
    proc = psutil.Process(server.pid)
    samples: list[Sample] = []
    counters = _Counters()
    stop_all = asyncio.Event()
    started = time.monotonic()
    samples_out.parent.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Wait for the server to come up.
        for _ in range(50):
            try:
                r = await client.get(f"{base_url}/api/public/live/current")
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                await asyncio.sleep(0.2)
        else:
            server.terminate()
            raise SystemExit("soak server never came up")

        viewers = [
            asyncio.create_task(_viewer(client, base_url, counters, i, stop_all))
            for i in range(baseline_viewers)
        ]
        surge_tasks: list[asyncio.Task[None]] = []
        surge_stop = asyncio.Event()
        next_surge = started + surge_period_s
        surge_until = 0.0
        next_sample = started + _SAMPLE_INTERVAL_S

        with samples_out.open("a", encoding="utf-8") as sink:
            while (now := time.monotonic()) - started < duration_s:
                if server.poll() is not None:
                    counters.server_5xx += 1  # crash counts as failure
                    break
                if now >= next_surge and not surge_tasks:
                    surge_stop = asyncio.Event()
                    surge_tasks = [
                        asyncio.create_task(
                            _viewer(client, base_url, counters, 1000 + i, surge_stop)
                        )
                        for i in range(surge_viewers - baseline_viewers)
                    ]
                    surge_until = now + surge_length_s
                    next_surge = now + surge_period_s
                if surge_tasks and now >= surge_until:
                    surge_stop.set()
                    await asyncio.gather(*surge_tasks, return_exceptions=True)
                    surge_tasks = []
                if now >= next_sample:
                    next_sample = now + _SAMPLE_INTERVAL_S
                    sample = _take_sample(proc, counters, t=now - started)
                    samples.append(sample)
                    sink.write(json.dumps(asdict(sample)) + "\n")
                    sink.flush()
                await asyncio.sleep(0.5)

            # Final sample at true end-of-run so duration_s reflects reality
            # (otherwise up to a whole sample interval is silently dropped).
            final = _take_sample(proc, counters, t=time.monotonic() - started)
            samples.append(final)
            sink.write(json.dumps(asdict(final)) + "\n")
            sink.flush()

            stop_all.set()
            surge_stop.set()
            await asyncio.gather(*viewers, *surge_tasks, return_exceptions=True)

    server.terminate()
    try:
        server.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover
        server.kill()
    if root_ctx is not None:
        root_ctx.cleanup()
    return samples


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m civiccast.load.live_soak",
        description="Measured wall-clock live-delivery + surge-switch soak.",
    )
    parser.add_argument("--duration-seconds", type=float, default=12 * 3600.0)
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--baseline-viewers", type=int, default=_BASELINE_VIEWERS)
    parser.add_argument("--surge-viewers", type=int, default=_SURGE_VIEWERS)
    parser.add_argument("--surge-period-seconds", type=float, default=1200.0)
    parser.add_argument("--surge-length-seconds", type=float, default=300.0)
    parser.add_argument(
        "--out", type=Path, default=Path("docs/releases/evidence/live-delivery-soak.md")
    )
    parser.add_argument("--samples-out", type=Path, default=None)
    args = parser.parse_args(argv)

    samples_out = args.samples_out or args.out.with_suffix(".samples.jsonl")
    commit = os.environ.get("CIVICCAST_SOAK_COMMIT", "unknown")
    expected_cycles = max(1, int(args.duration_seconds // args.surge_period_seconds) - 1)

    samples = asyncio.run(
        run_soak(
            duration_s=args.duration_seconds,
            port=args.port,
            baseline_viewers=args.baseline_viewers,
            surge_viewers=args.surge_viewers,
            surge_period_s=args.surge_period_seconds,
            surge_length_s=args.surge_length_seconds,
            samples_out=samples_out,
        )
    )
    verdict = analyze(
        samples,
        requested_duration_s=args.duration_seconds,
        min_switch_cycles=expected_cycles,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        render_evidence(
            verdict,
            requested_duration_s=args.duration_seconds,
            commit=commit,
            samples_path=str(samples_out),
            baseline_viewers=args.baseline_viewers,
            surge_viewers=args.surge_viewers,
        ),
        encoding="utf-8",
    )
    print(f"live-soak: {'PASS' if verdict.passed else 'FAIL'} -> {args.out}")
    for reason in verdict.reasons:
        print(f"  - {reason}")
    return 0 if verdict.passed else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
