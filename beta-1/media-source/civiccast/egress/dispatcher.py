# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""PlayoutDispatcher — bridge a committed schedule item to the egress engine.

CivicCast 3.0 — S4 slice 3. When an operator commits an occurrence to air
(the Commit-to-Air gate), the committed schedule item is already present in
``schedule_items`` (scheduled, premiere). The egress engine is **pull-based**:
``ScheduleSourcePlanProvider`` / ``build_source_plan_from_schedule`` resolves
each channel's source plan dynamically from its scheduled items every cycle,
and re-resolves on a ``reload`` command. There is no persisted "active source
plan" table to write.

So the honest dispatch action — and what ``channel_automation`` itself already
does — is simply to **nudge the engine** with an existing :data:`EgressCommand`
action so it re-resolves promptly instead of waiting for its next natural
reload:

* if the channel is currently running (on air / starting / on slate /
  transitioning), enqueue ``reload`` so the daemon re-resolves and the
  committed program takes over when its window is current;
* if the channel is dark (no state / stopped / stopping / draining / error),
  enqueue ``start`` so a committed program brings the channel up.

This deliberately does **not** build or persist an ``EgressSourcePlan`` (the
spec text predates the pull-based engine; a plan built here would be an unused
artifact that could drift from what the resolver actually airs) and does
**not** add a new ``EgressCommand`` action — S5 owns ``takeover``/``handback``.
The proven resolver is the single source of truth for what airs.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from civiccast.egress.models import EgressCommand, EgressCommandAction
from civiccast.egress.store import EgressStore

# Channel states in which the engine already has (or is bringing up) a live
# process — a ``reload`` makes it re-resolve. Any other state (including no
# state row at all) is treated as dark and gets a ``start``. Mirrors
# channel_automation's "start dark channels, reload running ones" logic.
# Public so ``civiccast.egress.router``'s headend-preset route reads the SAME
# set (review round 2 delta, MINOR 3: it carried a by-hand copy).
RUNNING_STATES: frozenset[str] = frozenset(
    {"STARTING", "ON_AIR", "TRANSITIONING", "FALLBACK_SLATE"}
)
_RUNNING_STATES = RUNNING_STATES

_DEFAULT_ISSUED_BY = "commit-to-air"


def _default_id() -> str:
    return uuid.uuid4().hex[:12]


def _default_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class DispatchOutcome:
    """Result of dispatching a committed item to the egress engine."""

    channel_id: str
    action: EgressCommandAction  # "start" (dark channel) or "reload" (running)
    command_id: str
    dispatched_at: datetime


class PlayoutDispatcher:
    """Enqueues the engine nudge for a committed schedule item.

    Depends only on the egress store's command queue + state read. ``clock``
    and ``id_factory`` are injectable for deterministic tests.
    """

    def __init__(
        self,
        egress_store: EgressStore,
        *,
        clock: Callable[[], datetime] = _default_clock,
        id_factory: Callable[[], str] = _default_id,
    ) -> None:
        self._store = egress_store
        self._clock = clock
        self._id_factory = id_factory

    def dispatch(self, *, channel_id: str, issued_by: str | None = None) -> DispatchOutcome:
        """Enqueue a ``start`` (dark channel) or ``reload`` (running channel)
        command so the engine re-resolves and airs the committed item.

        Raises whatever the store's ``enqueue_command`` raises — the caller
        (the commit orchestration) translates a failure into a report with
        ``dispatch_status="error"``.
        """
        state = self._store.read_state(channel_id)
        action: EgressCommandAction = (
            "reload" if state is not None and state.state in _RUNNING_STATES else "start"
        )
        now = self._clock()
        command_id = f"commit-{action}-{self._id_factory()}"
        self._store.enqueue_command(
            EgressCommand(
                channel_id=channel_id,
                action=action,
                issued_at=now,
                issued_by=issued_by or _DEFAULT_ISSUED_BY,
                command_id=command_id,
            )
        )
        return DispatchOutcome(
            channel_id=channel_id,
            action=action,
            command_id=command_id,
            dispatched_at=now,
        )
