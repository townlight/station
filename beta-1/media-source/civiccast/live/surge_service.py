# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""App-level wiring for the adaptive surge switch (0.2.0).

Bundles the :class:`~civiccast.live.load_monitor.LiveLoadMonitor` and
:class:`~civiccast.live.surge_switch.SurgeSwitch` into one service the app holds
on ``app.state`` and the live routes drive:

* the live media route calls :meth:`observe` on every manifest poll -- that both
  feeds the load signal and advances the switch (throttled to the segment
  cadence, so no background loop is needed: live players poll every ~2s, which is
  exactly the tick rate the publisher wants);
* the public live endpoint calls :meth:`manifest_url` -- a cheap read that
  returns the CDN URL for a switched channel, else ``None`` (serve local).

**Off by default.** :meth:`from_env` returns ``None`` unless
``CIVICCAST_LIVE_SURGE_THRESHOLD`` is set, so a stock single-PC install is
unchanged. Even when enabled, nothing touches a CDN until load crosses the
threshold *and* a CDN is configured (the publisher factory returns ``None``
otherwise -> clean local fallback).
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from fastapi import HTTPException, Request

from civiccast.egress.store import EgressStore
from civiccast.live.cdn_publisher import LiveCDNPublisher
from civiccast.live.load_monitor import LiveLoadMonitor
from civiccast.live.surge_switch import SurgeSwitch
from civiccast.stream.cdn import CDNAdapter

# Spec: the delay buffer targets ~15s with a 30s hard ceiling.
_BUFFER_CEILING_SECONDS = 30.0
# Advance the switch (and re-sync the CDN) at most once per segment interval,
# even though many viewers may poll within it.
_TICK_INTERVAL_SECONDS = 2.0


class SurgeSwitchService:
    """Holds the load monitor + switch and drives them from the live routes."""

    def __init__(
        self,
        *,
        egress_store_provider: Callable[[], EgressStore | None],
        cdn_adapter_provider: Callable[[], CDNAdapter | None],
        threshold: int,
        buffer_seconds: float = 15.0,
        tick_interval: float = _TICK_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._egress_store_provider = egress_store_provider
        self._cdn_adapter_provider = cdn_adapter_provider
        self._clock = clock
        self._tick_interval = tick_interval
        self._last_tick: dict[str, float] = {}
        # Serialize ticks per channel: observe() runs on Starlette's request
        # threadpool, so concurrent polls for one channel must not both advance
        # its state (which would build two publishers and orphan one on the CDN).
        self._channel_locks: dict[str, threading.Lock] = {}
        self._channel_locks_guard = threading.Lock()
        self.monitor = LiveLoadMonitor(clock=clock)
        self.switch = SurgeSwitch(
            self.monitor,
            self._make_publisher,
            threshold=threshold,
            buffer_seconds=min(buffer_seconds, _BUFFER_CEILING_SECONDS),
            clock=clock,
        )

    def observe(self, channel_id: str, client_id: str) -> None:
        """Record a manifest poll and advance the switch (throttled)."""
        self.monitor.record(channel_id, client_id)
        # Non-blocking: if another thread is already ticking this channel, this
        # poll has contributed to the count above and returns -- a viewer request
        # never blocks on a publish. Only the lock holder advances the state.
        lock = self._channel_lock(channel_id)
        if not lock.acquire(blocking=False):
            return
        try:
            now = self._clock()
            if now - self._last_tick.get(channel_id, -math.inf) >= self._tick_interval:
                self._last_tick[channel_id] = now
                self.switch.tick(channel_id)
        finally:
            lock.release()

    def _channel_lock(self, channel_id: str) -> threading.Lock:
        with self._channel_locks_guard:
            lock = self._channel_locks.get(channel_id)
            if lock is None:
                lock = threading.Lock()
                self._channel_locks[channel_id] = lock
            return lock

    def manifest_url(self, channel_id: str) -> str | None:
        """CDN manifest URL for a switched channel, else None (serve local)."""
        return self.switch.manifest_url(channel_id)

    def _make_publisher(self, channel_id: str) -> LiveCDNPublisher | None:
        adapter = self._cdn_adapter_provider()
        if adapter is None:
            return None
        live_dir = self._resolve_live_dir(channel_id)
        if live_dir is None:
            return None
        return LiveCDNPublisher(channel_id, live_dir, adapter)

    def _resolve_live_dir(self, channel_id: str) -> Path | None:
        # Reuse the media router's tested resolution (channel -> hls sink dir);
        # it raises 404 for "not configured", which here just means "no dir".
        from civiccast.stream.media_router import _live_dir_for_channel

        try:
            return _live_dir_for_channel(channel_id, self._egress_store_provider())
        except HTTPException:
            return None

    @classmethod
    def from_env(
        cls,
        *,
        egress_store_provider: Callable[[], EgressStore | None],
        cdn_adapter_provider: Callable[[], CDNAdapter | None],
    ) -> SurgeSwitchService | None:
        """Build from env, or None when the surge switch is not enabled.

        ``CIVICCAST_LIVE_SURGE_THRESHOLD`` (concurrent viewers) enables it;
        ``CIVICCAST_LIVE_SURGE_BUFFER_SECONDS`` (default 15, capped at 30) sizes
        the switch window. Invalid values fail fast at startup.
        """
        raw = os.environ.get("CIVICCAST_LIVE_SURGE_THRESHOLD", "").strip()
        if not raw:
            return None
        threshold = int(raw)
        if threshold < 1:
            raise ValueError("CIVICCAST_LIVE_SURGE_THRESHOLD must be a positive integer.")
        buffer_seconds = float(
            os.environ.get("CIVICCAST_LIVE_SURGE_BUFFER_SECONDS", "").strip() or 15.0
        )
        if buffer_seconds <= 0:
            raise ValueError("CIVICCAST_LIVE_SURGE_BUFFER_SECONDS must be a positive number.")
        return cls(
            egress_store_provider=egress_store_provider,
            cdn_adapter_provider=cdn_adapter_provider,
            threshold=threshold,
            buffer_seconds=buffer_seconds,
        )


def get_surge_switch_service(request: Request) -> SurgeSwitchService | None:
    """FastAPI dependency: the app's surge switch service, or None when disabled."""
    return getattr(request.app.state, "surge_switch_service", None)
