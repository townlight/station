# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Daily community-bulletin expiry purge worker (S6 V1 — build step 7, 4b).

The filler already hides bulletins outside their air window (slice 4); this
background job is the housekeeping that keeps the ``cg_bulletins`` table from
growing without bound. On its interval it deletes bulletins whose
``requested_end`` is more than ``retention_days`` in the past — a grace window
so a just-expired bulletin stays visible to operators for a while before it is
swept.

Everything is injected (session factory, store, clock) so it is unit-testable.
The app wraps :meth:`run_forever` in a ``ThreadSupervisor`` gated by
``CIVICCAST_BULLETIN_EXPIRY`` — set it to ``off`` to disable; any other value
(including unset) runs it, matching the ``CIVICCAST_AUTOSCHEDULE`` convention. A
failed purge is logged and swallowed — a bad tick must never kill the loop, and
it is idempotent (nothing left to delete is a no-op).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from civiccast.cg.bulletin_store import PostgresCgBulletinStore

if TYPE_CHECKING:
    import threading

    from sqlalchemy.orm import Session

_LOG = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractContextManager["Session"]]


@dataclass
class BulletinExpirySettings:
    """Cadence + grace window for the bulletin purge (operator-overridable)."""

    purge_interval_seconds: float = 86_400.0  # daily; first tick fires on startup
    poll_seconds: float = 300.0
    retention_days: int = 7  # keep just-expired bulletins this long before sweeping


class BulletinExpiryWorker:
    """Periodically purges long-expired community bulletins."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        store: PostgresCgBulletinStore | None = None,
        clock: Callable[[], datetime] | None = None,
        settings: BulletinExpirySettings | None = None,
    ) -> None:
        self._store = store or PostgresCgBulletinStore(session_factory)
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._settings = settings or BulletinExpirySettings()
        self._last_monotonic: float | None = None

    def tick(self, *, monotonic: float) -> int | None:
        """Purge if the interval has elapsed; return the rows removed (or None
        when not yet due, or on a swallowed failure). ``monotonic`` is a steady
        clock used only for the interval gate."""
        due = (
            self._last_monotonic is None
            or monotonic - self._last_monotonic >= self._settings.purge_interval_seconds
        )
        if not due:
            return None
        # Stamp before running so a failure can't hot-loop within the interval.
        self._last_monotonic = monotonic
        try:
            cutoff = self._clock() - timedelta(days=self._settings.retention_days)
            removed = self._store.delete_expired(before=cutoff)
            if removed:
                _LOG.info("purged %d expired community bulletins", removed)
            return removed
        except Exception:  # a bad purge must never kill the worker loop
            _LOG.exception("bulletin expiry purge failed")
            return None

    def run_forever(self, *, poll_seconds: float, stop_event: threading.Event) -> None:
        """ThreadSupervisor entry point — tick until stopped."""
        while not stop_event.is_set():
            self.tick(monotonic=time.monotonic())
            stop_event.wait(poll_seconds)
