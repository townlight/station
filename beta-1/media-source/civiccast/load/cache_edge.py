# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""A caching CDN-edge simulator that honors ``Cache-Control`` (0.2.0 Tier A).

The switch-validation harness (:mod:`civiccast.load.switch_lab`) originally
served its lab CDN edge from a bare ``StaticFiles`` mount, which never caches
anything -- so it could not exhibit the classic live-HLS-over-CDN failure: an
edge caching the churning manifest past its freshness window and serving a
playlist that references a segment the origin has already evicted.

:func:`civiccast.stream.cdn.cache_control` now tells the real adapters what
``Cache-Control`` to upload (``max-age=1`` for the manifest, a year
``immutable`` for segments). :class:`CachingEdgeASGI` is a from-scratch ASGI
proxy that honors exactly that header the way a real edge would: it caches a
response for as long as the *origin* said to, serves cache hits without
touching the origin, and refetches once stale. Its ``default_ttl_seconds``
override additionally simulates a provider edge's own default TTL when the
origin sends **no** ``Cache-Control`` at all -- the exact pre-fix failure mode
(see ``tests/load/test_cache_edge.py``'s falsification test).

Honesty: this is a software model of edge caching semantics, not a real CDN
vendor's edge (no geo-distribution, no LRU/capacity eviction, no purge API).
It proves the *contract* -- what a correct edge does with the headers we
send -- not a real vendor's behavior under real network conditions.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["CachingEdgeASGI", "path_class"]

_MAX_AGE_RE = re.compile(r"max-age=(\d+)")


def path_class(path: str) -> str:
    """``"manifest"`` for ``*.m3u8``, else ``"segment"`` -- the two very
    different cacheability classes :func:`civiccast.stream.cdn.cache_control`
    distinguishes; counts are kept per class so a test can assert the
    offload/scaling *shape*, not just an opaque total."""
    return "manifest" if path.endswith(".m3u8") else "segment"


class _CapturingSend:
    """Captures one ASGI HTTP response instead of sending it, so the edge can
    inspect and cache it before relaying the same bytes to the real client."""

    def __init__(self) -> None:
        self.status = 0
        self.headers: list[tuple[bytes, bytes]] = []
        self.body = bytearray()

    async def __call__(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")


async def _empty_receive() -> dict[str, Any]:
    """Stand-in ``receive`` for the in-process origin fetch -- GETs carry no body."""
    return {"type": "http.request", "body": b"", "more_body": False}


def _parse_cache_control(headers: list[tuple[bytes, bytes]]) -> tuple[bool, int | None] | None:
    """``(immutable, max_age)`` parsed from a ``Cache-Control`` response header,
    or ``None`` if the origin sent no such header at all (the no-Cache-Control
    provider-default-TTL case :class:`CachingEdgeASGI` can simulate)."""
    for key, value in headers:
        if key.lower() == b"cache-control":
            text = value.decode("latin1")
            match = _MAX_AGE_RE.search(text)
            return ("immutable" in text, int(match.group(1)) if match else None)
    return None


@dataclass
class _CacheEntry:
    status: int
    headers: list[tuple[bytes, bytes]]
    body: bytes
    expires_at: float | None  # None => cache-forever ("immutable") within this run

    def fresh(self, now: float) -> bool:
        return self.expires_at is None or now < self.expires_at


class CachingEdgeASGI:
    """An ASGI CDN-edge simulator that caches per the *origin's* ``Cache-Control``.

    Wraps an origin ASGI app. GET responses are cached per path using the
    max-age (or cache-forever for ``immutable``) the origin sent; while fresh
    they are served without touching the origin at all, and refetched once
    stale. Non-GET requests (and non-http scopes) are simply proxied through,
    uncached. A ``clock`` callable (e.g. :class:`~civiccast.load.switch_lab.
    ManualClock`) makes freshness deterministic in tests.

    ``default_ttl_seconds``, when set, is applied whenever the origin sends
    **no** ``Cache-Control`` header -- simulating a real provider edge's own
    default TTL (commonly minutes) rather than the ``None`` (never cache)
    behavior of a bare loopback origin. That is what lets the lab both prove
    the fix (headers set -> correct behavior) and falsify the bug it fixes
    (headers absent -> the edge serves stale, since-evicted state).
    """

    def __init__(
        self,
        origin_app: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
        default_ttl_seconds: float | None = None,
    ) -> None:
        self._origin = origin_app
        self._clock = clock
        self._default_ttl = default_ttl_seconds
        self._cache: dict[str, _CacheEntry] = {}
        self.origin_fetches: dict[str, int] = {"manifest": 0, "segment": 0}
        self.edge_hits: dict[str, int] = {"manifest": 0, "segment": 0}

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "GET":
            await self._origin(scope, receive, send)
            return

        path = scope["path"]
        cls = path_class(path)
        now = self._clock()
        entry = self._cache.get(path)
        if entry is not None and entry.fresh(now):
            self.edge_hits[cls] += 1
            await self._replay(entry.status, entry.headers, entry.body, send)
            return

        self.origin_fetches[cls] += 1
        capture = _CapturingSend()
        await self._origin(scope, _empty_receive, capture)
        body = bytes(capture.body)
        if capture.status == 200:
            new_entry = self._to_entry(capture.headers, capture.status, body, now)
            if new_entry is not None:
                self._cache[path] = new_entry
            else:
                self._cache.pop(path, None)  # not cacheable -> drop any stale copy
        await self._replay(capture.status, capture.headers, body, send)

    def _to_entry(
        self, headers: list[tuple[bytes, bytes]], status: int, body: bytes, now: float
    ) -> _CacheEntry | None:
        parsed = _parse_cache_control(headers)
        if parsed is None:
            if self._default_ttl is None:
                return None  # no Cache-Control, no override -> not cacheable
            ttl: float | None = self._default_ttl
        else:
            immutable, max_age = parsed
            if immutable:
                ttl = None
            elif max_age is not None:
                ttl = max_age
            else:
                return None  # e.g. "no-store" -- not a directive we cache on
        expires_at = None if ttl is None else now + ttl
        return _CacheEntry(status=status, headers=headers, body=body, expires_at=expires_at)

    @staticmethod
    async def _replay(
        status: int, headers: list[tuple[bytes, bytes]], body: bytes, send: Any
    ) -> None:
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
