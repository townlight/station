# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Small in-process rate limiter for the public ActivityPub inbox."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta


class InboxRateLimiter:
    """Sliding-window request limiter keyed by remote actor domain."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[datetime]] = defaultdict(deque)

    def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        now = datetime.now(UTC)
        window_start = now - timedelta(seconds=window_seconds)
        hits = self._hits[key]
        while hits and hits[0] < window_start:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True
