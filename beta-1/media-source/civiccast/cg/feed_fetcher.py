# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""On-demand CG feed fetcher + parsers (S6 V1 — build step 7, slice 2b).

Turns a registered :class:`~civiccast.cg.board_models.CgFeedSource` into a list
of normalized :class:`~civiccast.cg.models.CgFeedItem` for the board resolver.
Per S6 §10 open-decision 1 the strategy is **on-demand with a TTL cache**:
``fetch_all`` reuses cached items while they are inside the feed's
``refresh_seconds`` window, only hitting the network when stale, and records the
outcome on the feed (``last_fetched_at`` / ``last_fetch_error``).

The network is an **injected seam** (``fetch: FeedFetch``) so tests run offline
and production can harden it. The default ``default_http_fetch`` enforces an
http(s)-only scheme and a response-size cap. A bad feed must never break
resolution: ``fetch_all`` catches fetch/parse failures, records the error, and
falls back to the last cached items (or an empty list).

Parsers are deliberately lenient — a malformed item is skipped, not fatal — and
guarded against XML entity-expansion (``<!DOCTYPE``/``<!ENTITY`` are rejected).
``item_id`` is stable across fetches (native guid/uid/id, else a content hash)
so the feed-item approval gate keys correctly between runs.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import xml.etree.ElementTree as ET  # parsing is DTD-guarded by _reject_dtd
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import cast
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from civiccast.cg.board_models import CgFeedSource
from civiccast.cg.board_store import CgBoardStore
from civiccast.cg.models import CgFeedItem

# url -> raw text body. Injected so tests are offline and prod can add SSRF
# hardening / auth without touching the parsers.
FeedFetch = Callable[[str], str]

_MAX_FEED_BYTES = 2_000_000
_ATOM_NS = "{http://www.w3.org/2005/Atom}"

__all__ = [
    "FeedCache",
    "FeedFetch",
    "default_http_fetch",
    "fetch_all",
    "fetch_feed_items",
    "parse_ical",
    "parse_rss",
    "parse_social",
    "parse_weather",
]


# ---------------------------------------------------------------------------
# Network seam (default implementation)
# ---------------------------------------------------------------------------


def _assert_safe_feed_url(url: str) -> None:
    """Fail closed unless ``url`` is http(s) AND its host resolves only to
    globally-routable addresses.

    Blocks SSRF to loopback, RFC-1918 private, link-local (incl. the cloud
    metadata endpoint 169.254.169.254), reserved, multicast, and IPv6
    unique-local ranges. A DNS-resolution failure is treated as a block.
    Applied to the initial URL and re-applied to every redirect hop.
    """

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"unsupported feed URL scheme: {url[:48]!r}")
    host = parts.hostname
    if not host:
        raise ValueError(f"feed URL has no host: {url[:48]!r}")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError as exc:
        raise ValueError(f"could not resolve feed host: {host!r}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"feed host {host!r} resolves to a non-public address: {ip}")


class _SsrfGuardRedirectHandler(HTTPRedirectHandler):
    """Re-validate the destination host on every redirect so a public URL
    cannot 30x-bounce the fetch into a private / metadata address."""

    def redirect_request(  # type: ignore[no-untyped-def]
        self, req, fp, code, msg, headers, newurl
    ):
        _assert_safe_feed_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_SSRF_GUARDED_OPENER = build_opener(_SsrfGuardRedirectHandler)


def default_http_fetch(url: str, *, timeout: float = 10.0, max_bytes: int = _MAX_FEED_BYTES) -> str:
    """Fetch a feed body over http(s) with a timeout, size cap, and SSRF guard.

    Rejects non-http(s) schemes and any URL whose host resolves to a non-public
    address (loopback, RFC-1918, link-local incl. cloud metadata, reserved).
    Redirects are re-validated per hop. Residual: DNS rebinding between this
    resolution and the socket connect is not pinned — acceptable for
    staff-registered feeds; revisit if feeds ever become user-supplied.
    """

    _assert_safe_feed_url(url)
    request = Request(url, headers={"User-Agent": "CivicCast-CG/1.0"})  # noqa: S310 - scheme + host validated in _assert_safe_feed_url
    with _SSRF_GUARDED_OPENER.open(request, timeout=timeout) as response:
        raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("feed body exceeds the size cap")
    return cast(str, raw.decode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# Parsers (kind -> list[CgFeedItem]); lenient, skip malformed items
# ---------------------------------------------------------------------------


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _clip(value: str | None, length: int) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed[:length] if trimmed else None


def _reject_dtd(text: str) -> None:
    # Guard against XML entity-expansion (billion-laughs) and external-entity
    # tricks before handing the document to ElementTree.
    head = text[:4096].upper()
    if "<!DOCTYPE" in head or "<!ENTITY" in head:
        raise ValueError("feed XML with a DTD/entity declaration is rejected")


def _parse_rfc822(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_ical_dt(value: str) -> datetime | None:
    candidate = value.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            parsed = datetime.strptime(candidate, fmt)  # tz added below
        except ValueError:
            continue
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def parse_rss(text: str) -> list[CgFeedItem]:
    """Parse RSS ``<item>`` or Atom ``<entry>`` elements into feed items."""

    _reject_dtd(text)
    root = ET.fromstring(text)  # noqa: S314 - DTD-guarded by _reject_dtd  # nosec B314
    items: list[CgFeedItem] = []

    rss_items = root.findall(".//item")
    if rss_items:
        for node in rss_items:
            title = _clip(node.findtext("title"), 200)
            if not title:
                continue
            link = _clip(node.findtext("link"), 500)
            guid = _clip(node.findtext("guid"), 120)
            items.append(
                CgFeedItem(
                    item_id=guid or _stable_id("rss", title, link or ""),
                    title=title,
                    summary=_clip(node.findtext("description"), 500),
                    url=link,
                    starts_at=_parse_rfc822(node.findtext("pubDate")),
                )
            )
        return items

    for node in root.findall(f".//{_ATOM_NS}entry"):
        title = _clip(node.findtext(f"{_ATOM_NS}title"), 200)
        if not title:
            continue
        link_el = node.find(f"{_ATOM_NS}link")
        link = _clip(link_el.get("href") if link_el is not None else None, 500)
        entry_id = _clip(node.findtext(f"{_ATOM_NS}id"), 120)
        items.append(
            CgFeedItem(
                item_id=entry_id or _stable_id("atom", title, link or ""),
                title=title,
                summary=_clip(node.findtext(f"{_ATOM_NS}summary"), 500),
                url=link,
                starts_at=_parse_iso(node.findtext(f"{_ATOM_NS}published")),
            )
        )
    return items


def parse_ical(text: str) -> list[CgFeedItem]:
    """Parse VEVENT blocks (iCalendar / CalDAV) into feed items."""

    items: list[CgFeedItem] = []
    current: dict[str, str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current is not None:
                title = _clip(current.get("summary"), 200)
                if title:
                    items.append(
                        CgFeedItem(
                            item_id=_clip(current.get("uid"), 120)
                            or _stable_id("ical", title, current.get("dtstart", "")),
                            title=title,
                            summary=_clip(current.get("description"), 500),
                            starts_at=_parse_ical_dt(current.get("dtstart", "")),
                        )
                    )
            current = None
        elif current is not None and ":" in line:
            key, _, value = line.partition(":")
            name = key.split(";")[0].upper()  # drop params like DTSTART;TZID=...
            if name == "SUMMARY":
                current["summary"] = value
            elif name == "DTSTART":
                current["dtstart"] = value
            elif name == "DESCRIPTION":
                current["description"] = value
            elif name == "UID":
                current["uid"] = value
    return items


def _json_records(text: str, list_key: str) -> list[dict[str, object]]:
    data = json.loads(text)
    records = data.get(list_key, []) if isinstance(data, dict) else data
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def parse_weather(text: str) -> list[CgFeedItem]:
    """Parse a weather-alerts JSON document (``{"alerts": [...]}``)."""

    items: list[CgFeedItem] = []
    for alert in _json_records(text, "alerts"):
        headline = _clip(str(alert.get("headline") or alert.get("event") or ""), 200)
        if not headline:
            continue
        native_id = _clip(str(alert.get("id") or ""), 120)
        items.append(
            CgFeedItem(
                item_id=native_id or _stable_id("wx", headline),
                title=headline,
                summary=_clip(_as_str(alert.get("description")), 500),
                starts_at=_parse_iso(_as_str(alert.get("onset"))),
            )
        )
    return items


def parse_social(text: str) -> list[CgFeedItem]:
    """Parse a permitted-social JSON document (``{"posts": [...]}``)."""

    items: list[CgFeedItem] = []
    for post in _json_records(text, "posts"):
        body = _clip(str(post.get("text") or post.get("title") or ""), 200)
        if not body:
            continue
        native_id = _clip(str(post.get("id") or ""), 120)
        items.append(
            CgFeedItem(
                item_id=native_id or _stable_id("soc", body),
                title=body,
                url=_clip(_as_str(post.get("url")), 500),
                starts_at=_parse_iso(_as_str(post.get("created_at"))),
            )
        )
    return items


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


_PARSERS: dict[str, Callable[[str], list[CgFeedItem]]] = {
    "rss": parse_rss,
    "ical": parse_ical,
    "caldav": parse_ical,  # CalDAV returns iCalendar bodies
    "weather": parse_weather,
    "social": parse_social,
}


def fetch_feed_items(feed: CgFeedSource, *, fetch: FeedFetch) -> list[CgFeedItem]:
    """Fetch + parse one feed source into normalized items (may raise).

    The feed's configured tags are stamped onto each item so a zone with
    ``allowed_tags`` can include them (CG depth, DC-CG3).
    """

    body = fetch(feed.source_url)
    items = _PARSERS[feed.kind](body)
    if feed.tags:
        items = [item.model_copy(update={"tags": list(feed.tags)}) for item in items]
    return items


# ---------------------------------------------------------------------------
# TTL cache + fetch_all
# ---------------------------------------------------------------------------


class FeedCache:
    """In-memory per-feed item cache (keyed by feed_source_id)."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[datetime, list[CgFeedItem]]] = {}

    def get(self, feed_source_id: str) -> tuple[datetime, list[CgFeedItem]] | None:
        return self._entries.get(feed_source_id)

    def put(self, feed_source_id: str, items: list[CgFeedItem], *, now: datetime) -> None:
        self._entries[feed_source_id] = (now, list(items))


def fetch_all(
    feeds: Iterable[CgFeedSource],
    store: CgBoardStore,
    *,
    fetch: FeedFetch,
    now: datetime,
    cache: FeedCache | None = None,
) -> dict[str, list[CgFeedItem]]:
    """Fetch all enabled feeds (cache-fresh ones skip the network).

    Records ``last_fetched_at`` / ``last_fetch_error`` per feed. A fetch or
    parse failure is caught, recorded, and falls back to the last cached items
    (or an empty list) — it never raises.
    """

    cache = cache if cache is not None else FeedCache()
    result: dict[str, list[CgFeedItem]] = {}
    for feed in feeds:
        if not feed.enabled:
            continue
        entry = cache.get(feed.feed_source_id)
        ttl = timedelta(seconds=feed.refresh_seconds)
        if entry is not None and (now - entry[0]) < ttl:
            result[feed.feed_source_id] = entry[1]  # fresh cache hit, no network
            continue
        try:
            items = fetch_feed_items(feed, fetch=fetch)
        except Exception as exc:  # a bad feed must never break resolution / the worker
            store.mark_feed_fetch(feed.feed_source_id, fetched_at=now, error=str(exc)[:500])
            result[feed.feed_source_id] = entry[1] if entry is not None else []
            continue
        cache.put(feed.feed_source_id, items, now=now)
        store.mark_feed_fetch(feed.feed_source_id, fetched_at=now, error=None)
        result[feed.feed_source_id] = items
    return result
