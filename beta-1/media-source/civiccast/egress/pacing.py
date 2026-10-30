# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Uniform pacing latch (S9 §6.4) — one cooldown primitive for every operation that
must not churn: start retries, reload pacing, pipeline restart, readiness probes.

Replaces the per-operation ``dict[str, float]`` cooldowns scattered through the
automation loop with a single, testable class. Uses a monotonic clock by default
(cooldowns are durations, not wall-clock instants); the clock is injectable for
deterministic tests.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class UniformPacingLatch:
    """Cooldown latch keyed by operation. ``should_run_now`` returns whether the
    operation may run *and* advances the latch when it returns True."""

    def __init__(
        self,
        default_cooldown_seconds: float = 30.0,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._next_allowed: dict[str, float] = {}
        self._default_cooldown = default_cooldown_seconds
        self._clock = clock or time.monotonic

    def should_run_now(
        self, key: str, *, now: float | None = None, cooldown: float | None = None
    ) -> bool:
        """True if ``key`` may run now (and arm the next cooldown); False if still
        cooling down. ``cooldown`` overrides the default for this call."""
        now = self._clock() if now is None else now
        if now >= self._next_allowed.get(key, 0.0):
            self._next_allowed[key] = now + (
                self._default_cooldown if cooldown is None else cooldown
            )
            return True
        return False

    def force_reset(self, key: str) -> None:
        """Clear the cooldown for ``key`` so the next ``should_run_now`` runs
        immediately (used after a success to allow an immediate retry)."""
        self._next_allowed.pop(key, None)

    def next_allowed_at(self, key: str) -> float:
        """The clock time at which ``key`` may next run (0.0 if never armed) — for
        introspection / health surfaces."""
        return self._next_allowed.get(key, 0.0)
