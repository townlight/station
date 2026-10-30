# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Egress service loop for background workers and CLI entrypoints."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from civiccast.egress.daemon import EgressDaemon

SleepFn = Callable[[float], None]
StopPredicate = Callable[[], bool]


@dataclass(frozen=True)
class EgressServiceReport:
    """Summary of one bounded or stopped egress service run."""

    channel_ids: tuple[str, ...]
    iterations: int
    commands_processed: int
    stopped_by: str
    last_iteration_at: datetime | None


class EgressService:
    """Poll configured channels and let the egress daemon consume commands."""

    def __init__(
        self,
        daemon: EgressDaemon,
        *,
        channel_ids: Sequence[str],
        poll_seconds: float = 2.0,
        sleep: SleepFn = time.sleep,
        should_stop: StopPredicate | None = None,
    ) -> None:
        normalized = tuple(channel_id.strip() for channel_id in channel_ids if channel_id.strip())
        if not normalized:
            raise ValueError("At least one channel id is required for the egress service.")
        if poll_seconds < 0:
            raise ValueError("poll_seconds must be zero or greater.")
        self._daemon = daemon
        self._channel_ids = normalized
        self._poll_seconds = poll_seconds
        self._sleep = sleep
        self._should_stop = should_stop

    def run(self, *, max_iterations: int | None = None) -> EgressServiceReport:
        """Run until stopped, or for a bounded number of iterations in tests/CLI probes."""

        if max_iterations is not None and max_iterations <= 0:
            raise ValueError("max_iterations must be greater than zero when provided.")

        iterations = 0
        commands_processed = 0
        last_iteration_at: datetime | None = None
        stopped_by = "stop_predicate"
        while True:
            if self._should_stop is not None and self._should_stop():
                break
            iterations += 1
            last_iteration_at = datetime.now(UTC)
            for channel_id in self._channel_ids:
                commands_processed += self._daemon.process_once(channel_id)
            if max_iterations is not None and iterations >= max_iterations:
                stopped_by = "max_iterations"
                break
            self._sleep(self._poll_seconds)

        return EgressServiceReport(
            channel_ids=self._channel_ids,
            iterations=iterations,
            commands_processed=commands_processed,
            stopped_by=stopped_by,
            last_iteration_at=last_iteration_at,
        )
