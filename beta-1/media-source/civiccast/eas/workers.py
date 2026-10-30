# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Supervised EAS poll worker (S11c, S9 no-unguarded-waits).

Each scan: expire past alerts, then for every enabled non-manual source fetch the
feed, parse it, filter by severity+geocode, and ingest (dedup/supersede). FAIL-CLOSED:
a fetch or parse failure surfaces the source as unhealthy (→ S8) and is otherwise a
no-op — it NEVER fabricates an alert and NEVER clears an active one (only a real CAP
Cancel or a real expiry does that). The fetch is injected so the loop is unit-testable
without network; the live HTTP fetch (IPAWS COG / NWS / AMBER) is the WSL/LPM edge.

Follows the ``ThreadSupervisor`` ``run_forever``/``run_once`` shape.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from civiccast.eas.cap import alert_passes_source_filter, parse_cap_xml, parse_nws_geojson
from civiccast.eas.models import EasCapAlert, EasCapSource
from civiccast.eas.store import EasStore

_LOG = logging.getLogger(__name__)

# Returns the raw feed body, or None on a fetch failure (→ source-health-down).
SourceFetcher = Callable[[EasCapSource], str | None]
# (source, healthy, detail) — surfaces source health (the build_ factory maps it to S8).
SourceHealthHook = Callable[[EasCapSource, bool, str], None]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class EasPollScanResult:
    """Outcome of one ``run_once`` scan."""

    sources_polled: int = 0
    alerts_ingested: int = 0
    sources_unhealthy: int = 0
    expired: int = 0
    unhealthy_source_ids: tuple[str, ...] = field(default_factory=tuple)


def parse_for_source(source: EasCapSource, body: str) -> list[EasCapAlert]:
    """Dispatch parsing by source kind: NWS = GeoJSON, IPAWS/AMBER = CAP XML."""
    if source.kind == "nws-cap":
        return parse_nws_geojson(body, source_id=source.source_id)
    return parse_cap_xml(body, source_id=source.source_id)


class EasPollWorker:
    """Poll enabled CAP sources, ingest filtered alerts, expire stale ones."""

    def __init__(
        self,
        *,
        store: EasStore,
        fetcher: SourceFetcher,
        health_hook: SourceHealthHook | None = None,
        post_scan: Callable[[], None] | None = None,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._fetcher = fetcher
        self._health_hook = health_hook
        # Optional hook run after each ingest/expire scan — e.g. auto-surface severe+
        # alerts to ON_AIR channels (crawl/overlay; forced_slate stays operator-gated).
        self._post_scan = post_scan
        self._clock = clock

    def run_forever(
        self, *, poll_seconds: float = 60.0, stop_event: threading.Event | None = None
    ) -> None:
        """Run the poll loop until ``stop_event`` is set; scan errors are logged."""
        while stop_event is None or not stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                _LOG.exception("EAS poll scan failed; continuing.")
            if stop_event is None:
                threading.Event().wait(poll_seconds)  # pragma: no cover - loop shape
            else:
                stop_event.wait(poll_seconds)

    def run_once(self) -> EasPollScanResult:
        expired = self._store.expire_alerts(now=self._clock())
        ingested = 0
        unhealthy: list[str] = []
        polled = 0
        for source in self._store.list_sources(enabled_only=True):
            if source.kind == "manual" or not source.endpoint_url:
                continue  # operator-entered alerts have no feed to poll
            polled += 1
            ingested_here, healthy, detail = self._poll_one(source)
            ingested += ingested_here
            if not healthy:
                unhealthy.append(source.source_id)
            if self._health_hook is not None:
                self._health_hook(source, healthy, detail)
        if self._post_scan is not None:
            # Auto-surfacing runs after ingest so newly-active alerts can reach air the
            # same scan. Errors must not kill the poll loop.
            try:
                self._post_scan()
            except Exception:
                _LOG.exception("EAS post-scan (auto-surface) failed; continuing.")
        return EasPollScanResult(
            sources_polled=polled,
            alerts_ingested=ingested,
            sources_unhealthy=len(unhealthy),
            expired=expired,
            unhealthy_source_ids=tuple(unhealthy),
        )

    def _poll_one(self, source: EasCapSource) -> tuple[int, bool, str]:
        """Fetch+parse+ingest one source. Returns (ingested, healthy, detail).

        Fail-closed: a fetch failure (None) or a parse exception is reported unhealthy
        and ingests nothing — existing alerts are untouched."""
        try:
            body = self._fetcher(source)
        except Exception as exc:
            _LOG.warning("EAS fetch failed for %s: %r", source.source_id, exc)
            return 0, False, f"fetch error: {exc!r}"[:200]
        if body is None:
            return 0, False, "fetch failed (no body)"
        try:
            parsed = parse_for_source(source, body)
        except Exception as exc:  # parsing is defensive, but never let a feed crash the loop
            _LOG.warning("EAS parse failed for %s: %r", source.source_id, exc)
            return 0, False, f"parse error: {exc!r}"[:200]
        ingested = 0
        for alert in parsed:
            # Always process Update/Cancel (for supersession) even below the floor;
            # otherwise apply the severity+geocode filter. ack/error are dropped.
            if alert.msg_type in ("update", "cancel") or alert_passes_source_filter(alert, source):
                self._store.ingest_alert(alert)
                ingested += 1
        return ingested, True, f"ok ({len(parsed)} parsed, {ingested} ingested)"


SecretResolver = Callable[[str], str | None]


def build_http_fetcher(
    *, timeout_seconds: float = 10.0, resolve_secret: SecretResolver | None = None
) -> SourceFetcher:
    """A live HTTP feed fetcher (the WSL/LPM edge — real network).

    GETs the source endpoint with the right Accept header (GeoJSON for NWS, CAP XML
    otherwise) and, for IPAWS, a Bearer token resolved from the source's
    ``credential_ref`` (FEMA COG credentialing). Any HTTP error returns None →
    fail-closed source-health-down; it never raises into the poll loop."""
    import httpx

    def _fetch(source: EasCapSource) -> str | None:
        if not source.endpoint_url:
            return None
        accept = (
            "application/geo+json"
            if source.kind == "nws-cap"
            else "application/cap+xml, application/xml"
        )
        headers = {"Accept": accept, "User-Agent": "CivicCast/3.0 (public-safety ingest)"}
        if source.credential_ref and resolve_secret is not None:
            token = resolve_secret(source.credential_ref)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        try:
            response = httpx.get(source.endpoint_url, headers=headers, timeout=timeout_seconds)
            response.raise_for_status()
            return response.text
        except httpx.HTTPError as exc:
            _LOG.warning("EAS HTTP fetch failed for %s: %r", source.source_id, exc)
            return None

    return _fetch
