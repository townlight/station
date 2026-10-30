# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Periodic auto-schedule compile worker (CivicCast 3.0 — S18).

The background scheduler that realizes S18 §5's "a compiler materializes rules
into ``schedule_items`` on a rolling window (matches the periodic auto-scheduler's
periodic compile)". On its interval it runs
:func:`civiccast.schedule.autoschedule_materializer.compile_rules` for every
enabled rule.

``compile_rules`` is idempotent, so a periodic run simply extends the rolling
window as days pass and back-fills any newly-eligible slots; re-running over an
already-filled horizon adds nothing. The created items are ``scheduled`` and
still pass through the S4 commit gate before air — this worker never puts
anything on air.

Everything is injected (session factory, store, clock, the compile function) so
the scheduler is unit-testable. The app wraps :meth:`run_forever` in a
``ThreadSupervisor`` gated by ``CIVICCAST_AUTOSCHEDULE`` — set it to ``off`` to
disable; any other value (including unset) runs it, matching the
``CIVICCAST_ALERTING`` / channel-automation convention. A compile failure is
logged and swallowed — a bad tick must never kill the loop.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import TYPE_CHECKING

from civiccast.schedule.autoschedule_materializer import (
    COMPILE_LOCK,
    MaterializeReport,
    compile_rules,
)
from civiccast.schedule.autoschedule_store import AutoScheduleStore

if TYPE_CHECKING:
    import threading

    from sqlalchemy.orm import Session

_LOG = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractContextManager["Session"]]
CompileFn = Callable[..., MaterializeReport]


@dataclass
class AutoScheduleCompileSettings:
    """Cadence for the periodic compile (operator-overridable)."""

    # Hourly: compile is idempotent, so a frequent tick just keeps the rolling
    # window topped up cheaply. The first tick fires on startup.
    compile_interval_seconds: float = 3600.0
    poll_seconds: float = 60.0


class AutoScheduleCompileWorker:
    """Periodically compiles enabled auto-schedule rules into ``schedule_items``."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        store: AutoScheduleStore | None = None,
        clock: Callable[[], datetime] | None = None,
        tz: tzinfo | Callable[[], tzinfo] = UTC,
        settings: AutoScheduleCompileSettings | None = None,
        compile_fn: CompileFn | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._store = store or AutoScheduleStore(session_factory)
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._tz = tz
        self._settings = settings or AutoScheduleCompileSettings()
        self._compile_fn = compile_fn or compile_rules
        self._last_monotonic: float | None = None

    def tick(self, *, monotonic: float) -> MaterializeReport | None:
        """Run a compile if the interval has elapsed; return its report (or None
        when not yet due, or on a swallowed failure). ``monotonic`` is a steady
        clock used only for the interval gate."""
        due = (
            self._last_monotonic is None
            or monotonic - self._last_monotonic >= self._settings.compile_interval_seconds
        )
        if not due:
            return None
        # Stamp before running so a failed compile can't hot-loop within the interval.
        self._last_monotonic = monotonic
        try:
            with COMPILE_LOCK, self._session_factory() as session:
                tz = self._tz() if callable(self._tz) else self._tz
                report = self._compile_fn(session, self._store, now=self._clock(), tz=tz)
            if report.items_created:
                _LOG.info(
                    "auto-schedule compile added %d scheduled items across %d rules",
                    report.items_created,
                    len(report.results),
                )
            return report
        except Exception:  # a bad compile must never kill the worker loop
            _LOG.exception("auto-schedule compile failed")
            return None

    def run_forever(self, *, poll_seconds: float, stop_event: threading.Event) -> None:
        """ThreadSupervisor entry point — tick until stopped."""
        while not stop_event.is_set():
            self.tick(monotonic=time.monotonic())
            stop_event.wait(poll_seconds)
