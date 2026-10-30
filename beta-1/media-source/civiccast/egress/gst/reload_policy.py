# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Gi-free decision logic for seamless program-leg reloads (D-S1-6 / B3 fix).

Kept out of ``engine.py`` for the same reason ``decode_policy.py`` is: ``engine.py``
cannot be imported at all without ``gi`` + a real GStreamer install, so any logic
worth unit-testing on a bare checkout has to live in a sibling module the engine
imports rather than duplicates.

Two independent decisions live here, both born from the same soak defect
(Desktop/CIVICCAST-EVIDENCE/soak-120-4b30c99-20260904, kit 4b30c99): back-to-back
scheduled premieres EOS'd the GStreamer worker at every plan boundary because
nothing extended a live (ON_AIR) plan before it ran out.

1. **When to trigger a rollover reload.** A cold conform (``SourcePreparer``) can
   take minutes; triggering the reload only 30s before the live plan's projected
   end (the first cut of this fix) leaves the prepare step racing the pipeline's
   own EOS. ``rollover_trigger_at`` computes an EARLIER, boundary-aligned trigger
   time: the moment the plan's LAST segment begins, or ``min_lead_seconds`` before
   the plan's projected end -- whichever is earlier (the more conservative, i.e.
   earliest, of the two).

2. **Whether the engine may defer the selector switch to the outgoing leg's own
   EOS**, instead of cutting to the new leg the instant it is ready
   (``GstPlayoutEngine.reload_program``'s historic behavior). Triggering the
   rollover much earlier (per 1 above) means the new leg is typically ready WELL
   before the old leg's natural end; switching immediately on readiness would then
   TRUNCATE the tail of the still-airing item -- a regression worse than the
   EOS-restart this fix targets. Deferring the switch to the old leg's own EOS
   keeps the two legs mutually synchronized on the shared pipeline clock (both
   built from the same wall-clock join-in-progress offset) and lets the airing
   item finish naturally, with no re-decode and no jump.

   Deferring is safe ONLY for an automation-driven extension of a finite ON_AIR or
   FALLBACK_SLATE plan with no operator override in effect. An operator-initiated live takeover or
   forced slate is a deliberate "now" request -- see ``PlayoutSupervisor.
   request_live_takeover``/``request_fallback_slate`` (civiccast/egress/
   supervisor.py) -- and must still cut immediately, so ``should_defer_switch``
   returns False whenever a manual override is active. A FALLBACK_SLATE gap-replan
   has no tracked ``plan_end_at`` and still interrupts filler immediately; a finite
   filler-horizon rollover has one and defers to that boundary.
"""

from __future__ import annotations

from datetime import datetime, timedelta

#: Filename marker ``GstreamerEncoderStrategy.reload_content`` (gst/strategy.py)
#: writes into the one-shot reload sidecar filename, and both worker dispatch
#: paths (``engine._dispatch_control`` for the POSIX FIFO, ``worker.
#: _dispatch_control_with_ack`` for the Windows D2 pipe seam) read back via
#: ``reload_switch_is_deferred`` -- the flag rides the filename instead of a new
#: control-line token because ``parse_control_line``'s ``reload <path>`` grammar
#: takes the ENTIRE line remainder as the path (control.py), so a path is the
#: only slot available to carry information without a wire-format version bump.
DEFERRED_SWITCH_SUFFIX = ".defer-eos.json"
IMMEDIATE_SWITCH_SUFFIX = ".immediate.json"

#: Environment variable the DAEMON's worker launcher sets to mark its child as a
#: PERSISTENT live channel: ``strategy._default_worker_launcher``
#: (civiccast/egress/gst/strategy.py), the one place a worker's environment is
#: built, on the path ``GstPlayoutStrategy.start()`` takes for every channel the
#: daemon launches. It is the ONLY signal that can reach the worker: preparation
#: runs in the daemon and the control vocabulary (``swap``/``reload``/``caption``/
#: ``stop``) has no "you are on air" token, so nothing else distinguishes an ON_AIR
#: channel from a finite run (a smoke ``SWAPS`` run, a test harness, the beta.5
#: baseline pin) inside the worker process. ``worker.main()`` reads it and arms the
#: engine's plan-EOS slate hold only for the exact value ``"1"``; unset, ``0`` and
#: anything else keep exit-at-plan-EOS.
#:
#: Defined here, not in ``strategy.py``, so the writer (strategy) and the reader
#: (worker) share ONE name without the worker importing the strategy -- that import
#: would drag the daemon's import graph into the worker process, which the worker's
#: module docstring forbids. Both already import this gi-free module.
WORKER_PERSISTENT_ENV = "CIVICAST_WORKER_PERSISTENT"


def reload_sidecar_suffix(*, switch_at_end_of_current: bool) -> str:
    """The filename suffix ``reload_content`` appends, encoding the switch mode."""

    return DEFERRED_SWITCH_SUFFIX if switch_at_end_of_current else IMMEDIATE_SWITCH_SUFFIX


def reload_switch_is_deferred(reload_path: str) -> bool:
    """True when ``reload_path`` (as sent over the control channel) was written
    with ``switch_at_end_of_current=True``. Unknown/legacy filenames (no
    recognized suffix) default to False -- immediate switch, today's behavior --
    so an old daemon/new worker or new daemon/old-format sidecar pairing degrades
    to the pre-existing safe behavior rather than hanging waiting for an EOS
    nobody promised to defer to."""

    return reload_path.endswith(DEFERRED_SWITCH_SUFFIX)


#: The literal filename segment ``GstPlayoutStrategy.reload_content`` writes
#: every reload sidecar under, immediately before the id component --
#: ``playout-graph.reload.<id><suffix>``.
_RELOAD_SIDECAR_PREFIX = "playout-graph.reload."


def reload_id_from_sidecar_path(reload_path: str) -> str:
    """Extract the daemon-generated reload id embedded in a reload sidecar's
    FILENAME (hostile-review follow-up, 2026-09-06, item "POSIX _dispatch_
    control never settles").

    ``GstPlayoutStrategy.reload_content`` uses the daemon's own ``command_id``
    (``EgressDaemon._try_content_reload``'s freshly generated ``reload_id``,
    the same id its own ``_poll_reload_settlement`` later looks for) as this
    filename's unique component, instead of a disconnected, independently
    generated uuid -- specifically so a reload dispatched over the POSIX FIFO
    control channel can still report its settlement under an id the daemon
    can correlate. Unlike the Windows D2 named-pipe seam, the FIFO has no
    separate envelope/ack ``id`` field at all (``control.parse_control_line``'s
    ``reload <path>`` grammar takes the entire remainder as the path -- see
    ``reload_sidecar_suffix``'s docstring for why the switch-mode flag rides
    the filename for the identical reason), so the filename is the only slot
    available.

    Falls back to the bare filename stem (``Path(reload_path).name``, suffix
    included) if the expected ``playout-graph.reload.`` prefix isn't present
    -- e.g. a hand-constructed path in an older test/tool -- so a dispatch can
    never crash over this; it just correlates less precisely (effectively:
    not at all, matching the pre-fix behavior for that one caller)."""
    name = reload_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if not name.startswith(_RELOAD_SIDECAR_PREFIX):
        return name
    remainder = name[len(_RELOAD_SIDECAR_PREFIX) :]
    for suffix in (DEFERRED_SWITCH_SUFFIX, IMMEDIATE_SWITCH_SUFFIX):
        if remainder.endswith(suffix):
            return remainder[: -len(suffix)]
    return remainder


def should_defer_switch(
    *,
    previous_state: str | None,
    manual_override_active: bool,
    plan_end_at: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    """True when a content-reload should defer its selector switch to the
    outgoing leg's own EOS rather than cutting the instant the new leg is ready.

    Only an automation-driven extension of an already-ON_AIR plan, or a finite
    FALLBACK_SLATE plan carrying its tracked horizon, qualifies. A slate gap-replan
    without a horizon and any reload issued while an operator override is active
    (live takeover / forced slate -- ``manual_override_active``) cut immediately.

    Item 78 fix: ``plan_end_at``/``now`` are an optional pair (both default
    ``None`` and are ignored unless both are given, so every pre-existing
    caller/test that only passes ``previous_state``/``manual_override_active``
    keeps its exact prior behavior). When both are given and the tracked
    plan's projected end is already at or before ``now``, deferring is never
    safe: the "outgoing leg's own EOS" this mode waits for is a boundary that
    has *already passed* (a channel-automation pass stalled long enough --
    e.g. ``SourcePreparer`` blocking for ~915s inside ``daemon._start`` -- that
    the horizon it computed the reload against is now stale), so waiting for
    that EOS is waiting for something that may never arrive within any
    reasonable window. Cutting immediately is always correct here regardless
    of ``previous_state``/``manual_override_active``.
    """

    if plan_end_at is not None and now is not None and plan_end_at <= now:
        return False
    return not manual_override_active and (
        previous_state == "ON_AIR"
        or (previous_state == "FALLBACK_SLATE" and plan_end_at is not None)
    )


def rollover_trigger_at(
    *,
    plan_end_at: datetime,
    last_segment_start_at: datetime,
    min_lead_seconds: float,
) -> datetime:
    """The earliest wall-clock moment a rollover check should start re-querying
    the schedule for a live plan projected to end at ``plan_end_at``.

    Boundary-aligned: the moment the plan's LAST segment begins is the latest
    point a fresh plan could still be built and prepared without the two racing
    (a cold conform inside that final segment has the least runway of any
    segment in the plan). ``min_lead_seconds`` before the projected end is a
    floor under that for a plan with one long segment (no earlier "last segment
    start" boundary to align to). The earlier (smaller) of the two wins -- never
    later than either candidate.
    """

    if min_lead_seconds < 0:
        raise ValueError("min_lead_seconds must be zero or greater")
    lead_floor = plan_end_at - timedelta(seconds=min_lead_seconds)
    return min(last_segment_start_at, lead_floor)


__all__ = [
    "DEFERRED_SWITCH_SUFFIX",
    "IMMEDIATE_SWITCH_SUFFIX",
    "reload_id_from_sidecar_path",
    "reload_sidecar_suffix",
    "reload_switch_is_deferred",
    "rollover_trigger_at",
    "should_defer_switch",
]
