# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Switch-validation harness for the adaptive surge switch (0.2.0 Deliverable 2).

Where :mod:`civiccast.load.lab` measures the *direct-from-station* ceiling, this
harness validates **the switch itself** -- the spec's "one critical part". It
stands up the real end-to-end path:

* a :class:`~civiccast.load.lab.RollingStation` writing the local live window;
* the real :data:`~civiccast.stream.media_router.live_router` (local serving +
  the load-signal ``observe`` hook);
* the real ``/api/public/live/current`` resolution endpoint (which hands a viewer
  the local URL, or the CDN URL once switched);
* a real :class:`~civiccast.live.surge_service.SurgeSwitchService` driving a real
  :class:`~civiccast.live.cdn_publisher.LiveCDNPublisher`;
* an **HTTP-served lab CDN edge** (:class:`_LabCDNAdapter` + a ``StaticFiles``
  mount) so an in-process emulated viewer can actually fetch the CDN manifest and
  segments -- the stub ``file://`` adapter cannot be fetched over ASGI.

The emulated viewer is **switch-aware**: each cycle it re-resolves via
``/current`` and follows whatever manifest URL it is handed (local or CDN). That
models the spec's "existing viewers are signaled to swap source" and is exactly
what exercises the post-switch dynamics -- once viewers follow the switch to the
CDN they stop polling the local manifest, so whether the CDN copy stays fresh
depends on the switch being *driven* independently of the local-manifest poll.

No real CDN and no credentials: the lab edge is a local directory served over the
same in-process app. Per the release scope, the real-CDN thousand-viewer fan-out
is a beta-time measurement and is deliberately **not** claimed here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from civiccast.egress.models import EgressConfig, EgressSinkSpec
from civiccast.egress.router import get_egress_store
from civiccast.live.router import get_live_session_store, public_router
from civiccast.live.surge_service import SurgeSwitchService
from civiccast.load.cache_edge import CachingEdgeASGI
from civiccast.load.lab import RollingStation
from civiccast.stream.cdn import cache_control
from civiccast.stream.media_router import live_router

_CHANNEL_ID = "lab"
_CDN_MOUNT = "/cdn-edge"
_MEDIA_SEQUENCE_RE = re.compile(r"^#EXT-X-MEDIA-SEQUENCE:(\d+)", re.MULTILINE)


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


class ManualClock:
    """A monotonic-style clock the ramp advances explicitly.

    The surge service, load monitor, and switch all read one ``clock()``; driving
    it by hand makes the whole ramp -- threshold crossing, the delay buffer, the
    monitor's sliding window -- deterministic and fast, so the published result
    is reproducible (a Definition-of-Done requirement) rather than wall-clock
    flaky.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _PerRequestPeerASGI:
    """ASGI shim: set ``scope['client']`` from an ``x-lab-viewer-ip`` header.

    ``TestClient`` labels every request's peer ``"testclient"``, so without this
    all emulated viewers collapse to one distinct client and the switch never
    crosses its concurrent-viewer threshold. Each viewer sends a distinct public
    IP; it is a non-trusted peer, so ``resolve_client_ip`` returns it verbatim --
    genuinely distinct clients, no trusted-proxy env setup required.
    """

    _HEADER = b"x-lab-viewer-ip"

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            for key, value in scope.get("headers", []):
                if key == self._HEADER:
                    scope = {**scope, "client": (value.decode("latin1"), 40000)}
                    break
        await self._app(scope, receive, send)


def _viewer_ip(index: int) -> str:
    """A distinct, non-trusted public IP per viewer index (11.0.0.0/8 is global,
    not in any private/trusted-proxy range), good for ~16M viewers."""
    return f"11.{(index >> 16) & 0xFF}.{(index >> 8) & 0xFF}.{index & 0xFF}"


# ---------------------------------------------------------------------------
# HTTP-served lab CDN edge
# ---------------------------------------------------------------------------


class _LabCDNAdapter:
    """A :class:`~civiccast.stream.cdn.CDNAdapter` whose "edge" is a local dir
    served over HTTP by the same app, so an in-process viewer can fetch it.

    Unlike :class:`~civiccast.stream.cdn.stub.StubCDNAdapter` (which returns
    ``file://`` URLs that ``httpx``'s ASGI transport cannot GET), this returns
    ``{base_url}{mount}/{key}`` URLs backed by a ``StaticFiles`` mount at
    ``mount`` over ``root``.
    """

    def __init__(self, root: Path, *, public_base: str) -> None:
        self._root = root
        self._public_base = public_base.rstrip("/")

    def upload_file(self, local_path: Path, remote_key: str) -> str:
        dest = self._root / remote_key
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Windows: copy2 writes IN PLACE — a concurrent reader holding the
        # destination open raises PermissionError and killed a 12h soak at
        # t=2.2h. Copy to a sibling tmp then atomically replace, retrying
        # through the brief sharing-violation window a reader can hold.
        tmp = dest.with_name(dest.name + ".uploading")
        shutil.copy2(local_path, tmp)
        _replace_with_retry(tmp, dest)
        return self.public_url(remote_key)

    def delete_file(self, remote_key: str) -> None:
        target = self._root / remote_key
        if target.exists():
            for _attempt in range(20):
                try:
                    target.unlink()
                    return
                except PermissionError:
                    # A reader still holds it (Windows sharing) — the eviction
                    # retries next sync; never kill the publisher over it.
                    time.sleep(0.05)

    def public_url(self, remote_key: str) -> str:
        return f"{self._public_base}/{remote_key.lstrip('/')}"

    def health_check(self) -> bool:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        return True


class _CacheControlStaticFiles(StaticFiles):
    """``StaticFiles`` that emits the same ``Cache-Control`` the real CDN
    adapters upload (:func:`civiccast.stream.cdn.cache_control`).

    The bare ``StaticFiles`` mount sends no ``Cache-Control`` at all, so a
    caching edge in front of it could never observe what a real edge would --
    it would only ever see the "no header" case. This is the CDN-side origin
    :class:`~civiccast.load.cache_edge.CachingEdgeASGI` fronts.
    """

    def file_response(
        self, full_path: Any, stat_result: Any, scope: Any, status_code: int = 200
    ) -> Any:
        response = super().file_response(full_path, stat_result, scope, status_code=status_code)
        remote_key = self.get_path(scope).replace(os.sep, "/")
        response.headers["cache-control"] = cache_control(remote_key)
        return response


class _LabLiveSessionStore:
    """Minimal live-session store: one channel is permanently on-air.

    ``/api/public/live/current`` only reads ``list_sessions(channel_id, states)``
    and then ``.channel_id / .live_session_id / .title / .started_at`` off the
    first row, so a single ``SimpleNamespace`` row is a faithful stand-in.
    """

    def __init__(self, channel_id: str) -> None:
        self._row = SimpleNamespace(
            live_session_id=f"{channel_id}-live",
            channel_id=channel_id,
            title="Lab meeting",
            started_at=None,
        )

    def list_sessions(self, *, channel_id: str | None, states: Any) -> list[Any]:
        if channel_id is not None and channel_id != self._row.channel_id:
            return []
        return [self._row]


def _lab_egress_store(channel_id: str, live_dir: Path) -> Any:
    config = EgressConfig(
        channel_id=channel_id,
        enabled=True,
        slate_message="lab",
        sinks=[EgressSinkSpec(kind="hls", label="lab-hls", uri=live_dir.resolve().as_uri())],
    )
    return SimpleNamespace(get_config=lambda cid: config if cid == channel_id else None)


@dataclass
class SwitchLab:
    """The wired switch-validation app plus the handles a ramp needs."""

    app: FastAPI
    service: SurgeSwitchService
    station: RollingStation
    channel_id: str
    base_url: str
    cdn_dir: Path
    local_dir: Path
    cache_edge: CachingEdgeASGI | None = None

    def current_url(self) -> str:
        return f"/api/public/live/current?channel_id={self.channel_id}"


def build_switch_lab(
    local_dir: Path,
    cdn_dir: Path,
    *,
    channel_id: str = _CHANNEL_ID,
    threshold: int,
    buffer_seconds: float,
    tick_interval: float,
    window: int = 6,
    segment_bytes: int = 200 * 1024,
    base_url: str = "http://lab",
    clock: Any = None,
    cache_edge: bool = False,
    edge_default_ttl_seconds: float | None = None,
) -> SwitchLab:
    """Wire the real serving + resolution + surge path over a rolling station.

    The CDN edge (``cdn_dir``) is served at :data:`_CDN_MOUNT`; the surge
    service's publisher writes there via :class:`_LabCDNAdapter`, so a viewer
    handed the CDN URL fetches real, freshly-published segments.

    ``cache_edge=True`` mounts a :class:`~civiccast.load.cache_edge.
    CachingEdgeASGI` in *front of* the ``StaticFiles`` origin, so a viewer's
    CDN fetches go through a caching edge that honors the origin's
    ``Cache-Control`` (:func:`civiccast.stream.cdn.cache_control`) exactly as a
    real CDN edge would -- the default (bare ``StaticFiles``) mount caches
    nothing, which can never exhibit a stale-edge stall. ``edge_default_ttl_
    seconds`` simulates a provider edge's own default TTL when the origin
    sends no ``Cache-Control`` at all (used only to falsify the bug class the
    real headers prevent -- see ``tests/load/test_cache_edge.py``).
    """
    station = RollingStation(local_dir, window=window, segment_bytes=segment_bytes)
    station.bootstrap()
    cdn_dir.mkdir(parents=True, exist_ok=True)

    store = _lab_egress_store(channel_id, local_dir)
    session_store = _LabLiveSessionStore(channel_id)
    adapter = _LabCDNAdapter(cdn_dir, public_base=f"{base_url}{_CDN_MOUNT}")

    service_kwargs: dict[str, Any] = {
        "egress_store_provider": lambda: store,
        "cdn_adapter_provider": lambda: adapter,
        "threshold": threshold,
        "buffer_seconds": buffer_seconds,
        "tick_interval": tick_interval,
    }
    if clock is not None:
        service_kwargs["clock"] = clock
    service = SurgeSwitchService(**service_kwargs)

    app = FastAPI()
    app.include_router(live_router)
    app.include_router(public_router)
    app.dependency_overrides[get_egress_store] = lambda: store
    app.dependency_overrides[get_live_session_store] = lambda: session_store
    app.state.surge_switch_service = service

    origin = _CacheControlStaticFiles(directory=cdn_dir)
    cache_edge_instance: CachingEdgeASGI | None = None
    mounted_app: Any = origin
    if cache_edge:
        cache_edge_instance = CachingEdgeASGI(
            origin, clock=clock or time.monotonic, default_ttl_seconds=edge_default_ttl_seconds
        )
        mounted_app = cache_edge_instance
    app.mount(_CDN_MOUNT, mounted_app, name="cdn-edge")

    return SwitchLab(
        app=app,
        service=service,
        station=station,
        channel_id=channel_id,
        base_url=base_url,
        cdn_dir=cdn_dir,
        local_dir=local_dir,
        cache_edge=cache_edge_instance,
    )


@dataclass
class SwitchRampResult:
    """Outcome of a switch-validation ramp."""

    viewers: int
    switched_to_cdn: bool
    stalls: int
    segment_fetches: int
    cdn_segment_fetches: int
    local_segment_fetches: int
    final_state: str
    state_timeline: list[str] = field(default_factory=list)

    @property
    def stall_rate(self) -> float:
        return self.stalls / self.segment_fetches if self.segment_fetches else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "viewers": self.viewers,
            "switched_to_cdn": self.switched_to_cdn,
            "stalls": self.stalls,
            "stall_rate": round(self.stall_rate, 4),
            "segment_fetches": self.segment_fetches,
            "cdn_segment_fetches": self.cdn_segment_fetches,
            "local_segment_fetches": self.local_segment_fetches,
            "final_state": self.final_state,
            "state_timeline": self.state_timeline,
        }


def _media_sequence(manifest_text: str) -> int | None:
    match = _MEDIA_SEQUENCE_RE.search(manifest_text)
    return int(match.group(1)) if match else None


def _url_path(url: str, base_url: str) -> str:
    """Strip the lab origin so ``TestClient`` fetches the path (local or CDN)."""
    return url[len(base_url) :] if url.startswith(base_url) else url


def simulate_switch(
    lab: SwitchLab,
    clock: ManualClock,
    *,
    viewers: int,
    cycles: int,
    tick_interval: float,
    roll_every: int = 1,
    warmup_cycles: int = 1,
) -> SwitchRampResult:
    """Ramp ``viewers`` switch-aware viewers over ``cycles`` and measure stalls.

    Each cycle every viewer **re-resolves** ``/current`` and follows the URL it is
    handed (local pre-switch, CDN once switched) -- the spec's "existing viewers
    are signaled to swap source". A fetch of the *local* manifest also feeds the
    load signal via ``media_router``'s ``observe`` hook. The station rolls one
    segment every ``roll_every`` cycles, so live content is always advancing.

    A **stall** is a viewer whose served manifest window did *not* advance since
    its previous cycle **on the same source** -- i.e. it is stuck behind live.
    The one-cycle source swap itself is not counted (it is the hiccup the delay
    buffer is meant to cover); a *frozen CDN* (segments stop being published once
    the switch stops being driven) shows up as sustained same-source stalls.
    """
    client = TestClient(_PerRequestPeerASGI(lab.app))
    last_seq: dict[int, int] = {}
    last_src: dict[int, bool] = {}
    stalls = 0
    seg_fetches = 0
    cdn_fetches = 0
    local_fetches = 0
    switched = False
    timeline: list[str] = []
    cdn_prefix = f"{lab.base_url}{_CDN_MOUNT}"

    for cycle in range(cycles):
        clock.advance(tick_interval)
        for i in range(viewers):
            headers = {"x-lab-viewer-ip": _viewer_ip(i)}
            resolved = client.get(lab.current_url(), headers=headers).json().get("manifest_url")
            if resolved is None:
                continue
            is_cdn = resolved.startswith(cdn_prefix)
            resp = client.get(_url_path(resolved, lab.base_url), headers=headers)
            seg_fetches += 1
            if is_cdn:
                cdn_fetches += 1
                switched = True
            else:
                local_fetches += 1
            if resp.status_code != 200:
                # A resolved manifest URL that 404s = the viewer has nothing to play.
                stalls += 1
                continue
            seq = _media_sequence(resp.text)
            source_changed = last_src.get(i) is not None and last_src[i] != is_cdn
            if (
                cycle >= warmup_cycles
                and not source_changed
                and seq is not None
                and i in last_seq
                and seq <= last_seq[i]
            ):
                stalls += 1
            if seq is not None:
                last_seq[i] = seq
            last_src[i] = is_cdn
        timeline.append(lab.service.switch.state(lab.channel_id))
        if roll_every and (cycle + 1) % roll_every == 0:
            lab.station.roll()

    return SwitchRampResult(
        viewers=viewers,
        switched_to_cdn=switched,
        stalls=stalls,
        segment_fetches=seg_fetches,
        cdn_segment_fetches=cdn_fetches,
        local_segment_fetches=local_fetches,
        final_state=lab.service.switch.state(lab.channel_id),
        state_timeline=timeline,
    )


# ---------------------------------------------------------------------------
# Runnable entrypoint
# ---------------------------------------------------------------------------


def render_switch_report(result: SwitchRampResult) -> str:
    """A fixed-width text summary of one switch-validation ramp."""
    return "\n".join(
        [
            f"{'viewers':<18}: {result.viewers}",
            f"{'switched to CDN':<18}: {result.switched_to_cdn}",
            f"{'final state':<18}: {result.final_state}",
            f"{'segment fetches':<18}: {result.segment_fetches} "
            f"(local {result.local_segment_fetches} / cdn {result.cdn_segment_fetches})",
            f"{'stalls':<18}: {result.stalls} ({result.stall_rate * 100:.2f}%)",
            f"{'state timeline':<18}: {' '.join(result.state_timeline)}",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m civiccast.load.switch_lab",
        description=(
            "Validate the adaptive surge switch end-to-end: ramp switch-aware "
            "viewers past the threshold against an in-process HTTP-served CDN edge "
            "and confirm they ride the switch without the CDN freezing."
        ),
    )
    parser.add_argument("--viewers", type=int, default=25, help="Concurrent emulated viewers")
    parser.add_argument("--threshold", type=int, default=10, help="Switch threshold (viewers)")
    parser.add_argument("--cycles", type=int, default=20, help="Poll cycles to run")
    parser.add_argument("--buffer-seconds", type=float, default=0.0, help="Delay buffer")
    parser.add_argument("--tick-interval", type=float, default=1.0, help="Seconds per cycle/tick")
    parser.add_argument("--json-out", default=None, help="Write the result as JSON to this path")
    args = parser.parse_args(argv)

    # The local manifest URL /current builds must route back through this app.
    os.environ["CIVICCAST_LOCAL_MEDIA_BASE_URL"] = "http://lab"
    with tempfile.TemporaryDirectory(prefix="civiccast-switchlab-") as tmp:
        root = Path(tmp)
        # The lab's live folder must be inside the root the media router is
        # allowed to serve (it refuses anything else with a 404).
        os.environ["CIVICCAST_LIVE_HLS_ROOT"] = str(root)
        clock = ManualClock()
        lab = build_switch_lab(
            root / "live",
            root / "cdn",
            threshold=args.threshold,
            buffer_seconds=args.buffer_seconds,
            tick_interval=args.tick_interval,
            clock=clock,
        )
        result = simulate_switch(
            lab,
            clock,
            viewers=args.viewers,
            cycles=args.cycles,
            tick_interval=args.tick_interval,
        )

    print(render_switch_report(result))
    if args.json_out is not None:
        Path(args.json_out).write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")
    # Non-zero if it failed to switch or stalled -- lets a CI job gate on it.
    return 0 if (result.switched_to_cdn and result.stalls == 0) else 1


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
