# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S8-5 alerting maintenance worker (spec §6.6).

A single background worker (the retention/retry-worker shape, driven by
``ThreadSupervisor.run_forever(poll_seconds, stop_event)``) that owns the
system-level periodic jobs S8 needs:

- **resource sampling** every ``resource_interval_seconds`` — build a
  ``SystemResourceSample`` from the platform probes and route threshold breaches
  through the alert hub (fire/resolve);
- **self-tests** — a daily run at the configured local time (OD-4) and a weekly
  run on the configured weekday (OD-5), each run **once per period** (a catch-up
  run after downtime, never a duplicate);
- **delivery retries** — drive the injected ``AlertRetryWorker`` each tick.

Everything is injected (probes, self-test deps, retry worker, clocks) so the
scheduler is unit-testable; the app constructs the real probes/deps and wraps
``run_forever`` in a ``ThreadSupervisor``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from civiccast.alerting.models import SelfTestKind
from civiccast.alerting.resource_sampler import (
    ResourceProbes,
    ResourceThresholds,
    build_resource_sample,
    sample_and_record,
)
from civiccast.alerting.self_test import (
    SelfTestDeps,
    assemble_available_self_test_checks,
    default_self_test_availability,
    run_self_test,
)

if TYPE_CHECKING:
    import threading

    from sqlalchemy.orm import Session

_LOG = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractContextManager["Session"]]


class _RetryWorker(Protocol):
    def tick(self, now: datetime | None = ...) -> int: ...


@dataclass
class AlertingMaintenanceSettings:
    """Cadence + schedule for the maintenance worker (operator-overridable)."""

    resource_interval_seconds: float = 60.0
    poll_seconds: float = 30.0
    daily_hour: int = 2  # 02:00 local (OD-4)
    daily_minute: int = 0
    weekly_weekday: int = 6  # Sunday (Mon=0 .. Sun=6), 03:00 local (OD-5)
    weekly_hour: int = 3
    weekly_minute: int = 0
    extra: dict[str, object] = field(default_factory=dict)


class AlertingMaintenanceWorker:
    """Periodic resource sampling + scheduled self-tests + delivery retries."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        resource_probes: ResourceProbes | None = None,
        resource_thresholds: ResourceThresholds | None = None,
        self_test_deps: SelfTestDeps | None = None,
        self_test_availability: dict[str, bool] | None = None,
        retry_worker: _RetryWorker | None = None,
        settings: AlertingMaintenanceSettings | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._probes = resource_probes
        self._thresholds = resource_thresholds or ResourceThresholds()
        self._self_test_deps = self_test_deps
        self._self_test_availability = self_test_availability
        self._retry_worker = retry_worker
        self._settings = settings or AlertingMaintenanceSettings()
        self._last_resource_monotonic: float | None = None
        self._last_daily_date: object | None = None
        self._last_weekly_key: tuple[int, int] | None = None

    def tick(self, now: datetime, *, monotonic: float) -> None:
        """Run any due periodic work. ``now`` is local wall-clock; ``monotonic``
        is a steady clock used only for the resource-sampling interval."""
        self._maybe_sample_resources(now, monotonic)
        self._maybe_run_self_tests(now)
        if self._retry_worker is not None:
            try:
                self._retry_worker.tick(now)
            except Exception:  # a retry sweep must never kill the worker loop
                _LOG.exception("alert retry sweep failed")

    def _maybe_sample_resources(self, now: datetime, monotonic: float) -> None:
        if self._probes is None:
            return
        due = (
            self._last_resource_monotonic is None
            or monotonic - self._last_resource_monotonic >= self._settings.resource_interval_seconds
        )
        if not due:
            return
        try:
            sample = build_resource_sample(self._probes, now=now)
            with self._session_factory() as session:
                sample_and_record(session, sample, self._thresholds, now=now)
                session.commit()
        except Exception:
            _LOG.exception("resource sampling failed")
        finally:
            self._last_resource_monotonic = monotonic

    def _maybe_run_self_tests(self, now: datetime) -> None:
        if self._self_test_deps is None:
            return
        s = self._settings
        today = now.date()
        daily_due = (now.hour, now.minute) >= (s.daily_hour, s.daily_minute)
        if daily_due and self._last_daily_date != today:
            self._run_self_test("daily", now)
            self._last_daily_date = today

        week_key = now.isocalendar()[:2]  # (iso-year, iso-week)
        weekly_due = now.weekday() == s.weekly_weekday and (now.hour, now.minute) >= (
            s.weekly_hour,
            s.weekly_minute,
        )
        if weekly_due and self._last_weekly_key != week_key:
            self._run_self_test("weekly", now)
            self._last_weekly_key = week_key

    def _run_self_test(self, kind: SelfTestKind, now: datetime) -> None:
        assert self._self_test_deps is not None
        # Recompute availability per run (not a startup snapshot): on a 24/7 box an
        # operator enables cable verification / adds the first alert channel / configures
        # backup AFTER boot, and those checks must start running on the scheduled cadence
        # without a restart. An explicit dict is honored only as a test override.
        availability = (
            self._self_test_availability
            if self._self_test_availability is not None
            else default_self_test_availability(session_factory=self._session_factory)
        )
        try:
            checks = assemble_available_self_test_checks(
                kind,
                self._self_test_deps,
                availability,
            )
            with self._session_factory() as session:
                run_self_test(session, kind, checks, now=now)
                session.commit()
        except Exception:
            _LOG.exception("%s self-test failed to run", kind)

    def run_forever(self, *, poll_seconds: float, stop_event: threading.Event) -> None:
        """ThreadSupervisor entry point — tick until stopped."""
        import time

        while not stop_event.is_set():
            self.tick(datetime.now().astimezone(), monotonic=time.monotonic())
            stop_event.wait(poll_seconds)
