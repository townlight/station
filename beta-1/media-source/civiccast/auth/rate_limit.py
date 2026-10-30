# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""In-process keyed failure budgets for authentication-sensitive routes.

Setup callers supply peer-IP-plus-route keys. Staff authentication supplies
one failure key per observed peer IP across every staff route. After saturation,
exact valid tokens bypass the budget and exact misses receive immediate 429
without expensive verification. Loopback is not exempt: another process or user
on a shared station remains an authentication threat.
"""

from __future__ import annotations

import os
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

from fastapi import Request


class AuthRateLimiter:
    """Sliding-window request limiter keyed by an arbitrary string."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[datetime]] = defaultdict(deque)

    def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        now = datetime.now(UTC)
        window_start = now - timedelta(seconds=window_seconds)
        hits = self._hits[key]
        while hits and hits[0] < window_start:
            hits.popleft()
        if len(hits) >= limit:
            if not hits:
                del self._hits[key]
            return False
        hits.append(now)
        return True

    def saturated(self, key: str, *, limit: int, window_seconds: int) -> bool:
        """Return whether a key has spent its failure budget without recording a hit."""

        hits = self._hits.get(key)
        if hits is None:
            return False
        now = datetime.now(UTC)
        window_start = now - timedelta(seconds=window_seconds)
        while hits and hits[0] < window_start:
            hits.popleft()
        if not hits:
            del self._hits[key]
            return False
        return len(hits) >= limit

    def retry_after_seconds(self, key: str, *, window_seconds: int) -> int:
        """Seconds until the oldest hit in the window expires."""

        hits = self._hits.get(key)
        if not hits:
            return window_seconds
        oldest = hits[0]
        remaining = window_seconds - (datetime.now(UTC) - oldest).total_seconds()
        return max(1, int(remaining) + 1)


def auth_rate_limit_config() -> tuple[int, int]:
    """Read (limit, window_seconds) for auth-sensitive routes.

    Defaults are ten failures per 60 seconds. Setup routes use one key per
    observed peer and route; staff routes share one peer key across the staff
    API and count only failed bearer verification.
    """

    limit = _positive_int("CIVICCAST_AUTH_RATE_LIMIT", default=10)
    window_seconds = _positive_int("CIVICCAST_AUTH_RATE_LIMIT_WINDOW_SECONDS", default=60)
    return limit, window_seconds


def validate_auth_rate_limit_config() -> None:
    """Fail at app startup instead of locking out callers on first use."""

    auth_rate_limit_config()


def _positive_int(name: str, *, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer; got {raw!r}.") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer; got {raw!r}.")
    return value


def client_ip(request: Request) -> str:
    """Return the direct peer; forwarded headers are intentionally untrusted.

    A supported reverse-proxy deployment is therefore keyed by the proxy peer
    unless a future trusted-proxy resolver is explicitly configured.
    """

    if request.client is None:
        return "unknown"
    return request.client.host
