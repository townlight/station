# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Adaptive surge switch: local-default, flip to CDN under load (0.2.0).

The architecture from docs/spec/0.2.0-scope.md: a channel serves its live stream
**directly** by default; when concurrent load crosses a threshold the station
**cold-starts** CDN publishing and, after a short delay buffer, switches viewers
to the CDN edge. This is the state machine that decides that, per channel:

    LOCAL  --load >= threshold-->  WARMING  --buffer elapsed-->  CDN
      ^                                |                          |
      +--------- load < release -------+--------------------------+

* **Cold start** (spec default): nothing touches the CDN until the threshold;
  then the ``LiveCDNPublisher`` begins pushing segments.
* **Delay buffer** (spec: ~15s, 30s ceiling): while WARMING, viewers still get
  the local URL. The buffer is the window during which the player keeps playing
  already-buffered content while the CDN warms; only after it do viewers switch.
* **Hysteresis release**: once load falls back below ``release_threshold`` the
  channel returns to local and publishing stops (zero CDN cost at idle).
* **Clean fallback**: no CDN configured, or a publish that raises (edge
  unreachable) -> stay/return local. Live serving never breaks.

``tick`` advances the state (and keeps the CDN copy current) and is called on
the segment cadence by the background driver; ``manifest_url`` is the cheap read
the public live endpoint uses to hand a viewer the local or CDN URL.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from enum import Enum
from typing import Protocol

_DEFAULT_BUFFER_SECONDS = 15.0


class LoadSource(Protocol):
    """The concurrent-load signal (satisfied by :class:`~civiccast.live.load_monitor.LiveLoadMonitor`)."""

    def concurrent(self, channel_id: str) -> int: ...


class LivePublisher(Protocol):
    """The subset of :class:`~civiccast.live.cdn_publisher.LiveCDNPublisher` used here."""

    def sync(self) -> str | None: ...

    def manifest_url(self) -> str: ...

    def evict_all(self) -> None: ...


class _State(Enum):
    LOCAL = "local"
    WARMING = "warming"
    CDN = "cdn"


class SurgeSwitch:
    """Per-channel local<->CDN surge switch driven by the live load monitor."""

    def __init__(
        self,
        load_source: LoadSource,
        publisher_factory: Callable[[str], LivePublisher | None],
        *,
        threshold: int,
        release_threshold: int | None = None,
        buffer_seconds: float = _DEFAULT_BUFFER_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        self._load = load_source
        self._make_publisher = publisher_factory
        self._threshold = threshold
        # Default release is half the trigger — enough hysteresis that a viewer
        # count hovering at the threshold does not flap the switch.
        self._release = (
            release_threshold if release_threshold is not None else max(1, threshold // 2)
        )
        self._buffer = buffer_seconds
        self._clock = clock
        self._state: dict[str, _State] = {}
        self._warm_at: dict[str, float] = {}
        self._publisher: dict[str, LivePublisher] = {}

    def state(self, channel_id: str) -> str:
        return self._state.get(channel_id, _State.LOCAL).value

    def tick(self, channel_id: str) -> None:
        """Advance ``channel_id``'s switch state and keep its CDN copy current."""
        load = self._load.concurrent(channel_id)
        state = self._state.get(channel_id, _State.LOCAL)
        now = self._clock()

        if state is _State.LOCAL:
            if load >= self._threshold:
                publisher = self._make_publisher(channel_id)
                if publisher is None:
                    return  # no CDN configured -> clean fallback, stay local
                self._publisher[channel_id] = publisher
                self._warm_at[channel_id] = now
                self._state[channel_id] = _State.WARMING
        elif state is _State.WARMING:
            if load < self._release:
                self._to_local(channel_id)
                return
            if now - self._warm_at[channel_id] >= self._buffer:
                self._state[channel_id] = _State.CDN
        elif state is _State.CDN and load < self._release:
            self._to_local(channel_id)
            return

        # While warming or switched, keep pushing the rolling window to the CDN.
        # A publish that raises means the edge is unreachable -> fall back local.
        publisher = self._publisher.get(channel_id)
        if publisher is not None:
            try:
                publisher.sync()
            except Exception:
                self._to_local(channel_id)

    def manifest_url(self, channel_id: str) -> str | None:
        """CDN manifest URL for a switched channel, else None (serve local)."""
        if self._state.get(channel_id) is _State.CDN:
            publisher = self._publisher.get(channel_id)
            if publisher is not None:
                return publisher.manifest_url()
        return None

    def _to_local(self, channel_id: str) -> None:
        publisher = self._publisher.pop(channel_id, None)
        self._state[channel_id] = _State.LOCAL
        self._warm_at.pop(channel_id, None)
        if publisher is not None:
            # Delete what this publisher pushed so CDN cost goes to zero at idle.
            # A cleanup failure (e.g. the edge is the reason we're falling back)
            # must not break the fallback -- the same-prefix objects get
            # overwritten on the next warm-up.
            with contextlib.suppress(Exception):
                publisher.evict_all()
