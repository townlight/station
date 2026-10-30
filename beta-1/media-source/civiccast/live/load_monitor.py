# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Concurrent live-viewer load monitor (0.2.0 surge switch).

The surge switch flips a channel to the CDN when its concurrent-viewer load
crosses a threshold (spec: ~50-60% of the station's measured serving ceiling).
This is the signal it keys on.

Each live HLS player re-fetches the media playlist on the segment cadence
(``HlsSink.segment_seconds`` = 2s), so counting the distinct clients that polled
a channel's manifest within a short sliding window approximates the concurrent
count. Memory-only and per-process -- the process that serves the manifest is
the one that decides the switch -- with an injectable clock for tests.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

# A viewer re-polls every segment (~2s); a client seen within a few intervals is
# still watching. Three intervals (6s) tolerates a missed poll without dropping
# an active viewer from the count.
_DEFAULT_WINDOW_SECONDS = 6.0


class LiveLoadMonitor:
    """Tracks concurrent live viewers per channel from their manifest polls."""

    def __init__(
        self,
        *,
        window_seconds: float = _DEFAULT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window_seconds
        self._clock = clock
        self._seen: dict[str, dict[str, float]] = {}
        # record()/concurrent() run on Starlette's request threadpool (sync
        # routes), so concurrent viewer polls hit these truly in parallel.
        self._lock = threading.Lock()

    def record(self, channel_id: str, client_id: str) -> None:
        """Record that ``client_id`` polled ``channel_id``'s live manifest now."""
        with self._lock:
            self._seen.setdefault(channel_id, {})[client_id] = self._clock()

    def concurrent(self, channel_id: str) -> int:
        """Distinct clients that polled ``channel_id`` within the sliding window.

        Prunes expired clients as a side effect so the map cannot grow without
        bound over a long broadcast.
        """
        now = self._clock()
        with self._lock:
            seen = self._seen.get(channel_id)
            if not seen:
                return 0
            active = {client: ts for client, ts in seen.items() if now - ts <= self._window}
            if active:
                self._seen[channel_id] = active
            else:
                self._seen.pop(channel_id, None)
            return len(active)
