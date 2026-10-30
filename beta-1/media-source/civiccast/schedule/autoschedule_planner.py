# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Daypart slot expansion + repeat-prevention (CivicCast 3.0 — S18 slice 3).

The planning logic that sits between the data layer (slice 1) / query executor
(slice 2) and the rolling-window materializer (slice 4). Two pure-ish pieces:

* :func:`expand_block_slots` — expand a :class:`ScheduleBlock` (daypart) into
  the concrete :class:`SlotWindow` datetimes it covers across a date range.
  No DB, fully deterministic.
* :func:`repeat_prevention_asset_ids` — gather the asset ids already placed on
  a channel within a rule's repeat-prevention window, to feed
  ``autoschedule_query.pick_asset(exclude_asset_ids=…)`` so the same asset is
  not re-aired too soon. Reads ``schedule_items`` (the schedule of record).

Neither writes anything — composing slots + exclusions into actual
``schedule_items`` is the materializer's job (slice 4).

**Timezone.** Daypart times are *wall-clock* ("Prime time is 18:00-22:00").
:func:`expand_block_slots` takes an explicit ``tz`` (default UTC) and builds
each slot by combining the calendar date with the wall-clock time in that zone,
so a 18:00 start is 18:00 local on every day including DST-shift days (rather
than drifting an hour as a fixed offset from midnight would). The **source** of
the station's local zone is the caller's concern — CivicCast has no per-channel
tz config yet (egress channel config carries none), so the materializer/app
must supply it; until then UTC is the safe default. See the slice-3 audit
watch-items for the DST nonexistent/ambiguous-hour edge and the missing
station-tz config.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from civiccast.schedule.autoschedule_models import ScheduleBlock
from civiccast.schedule.models import SCHEDULE_STATE_CANCELLED, ScheduleItem
from civiccast.timezones import resolve_wall_clock


@dataclass(frozen=True)
class SlotWindow:
    """One concrete daypart occurrence: a half-open [starts_at, ends_at) window
    of tz-aware datetimes. ``starts_at < ends_at`` always (a midnight-wrapping
    daypart ends on the following calendar day)."""

    starts_at: datetime
    ends_at: datetime

    @property
    def duration_seconds(self) -> float:
        # Same-zone datetime subtraction ignores offset changes across DST.
        return (self.ends_at.astimezone(UTC) - self.starts_at.astimezone(UTC)).total_seconds()


def _wall_clock(day: date, minute_of_day: int, tz: tzinfo) -> datetime | None:
    """Resolve a wall clock; a nonexistent spring-forward time returns None."""
    hours, minutes = divmod(minute_of_day, 60)
    return resolve_wall_clock(day, time(hour=hours, minute=minutes), tz)


def _slot_end(block: ScheduleBlock, day: date, tz: tzinfo) -> datetime | None:
    """End datetime for ``block``'s daypart starting on ``day``.

    * wrap (``end_minute <= start_minute``) → ends on the next calendar day;
    * ``end_minute == 1440`` (24:00, always > start) → next day 00:00;
    * otherwise → same day at ``end_minute``.
    """
    if block.end_minute <= block.start_minute:
        return _wall_clock(day + timedelta(days=1), block.end_minute, tz)
    if block.end_minute == 24 * 60:
        return _wall_clock(day + timedelta(days=1), 0, tz)
    return _wall_clock(day, block.end_minute, tz)


def expand_block_slots(
    block: ScheduleBlock,
    *,
    start_date: date,
    num_days: int,
    tz: tzinfo = UTC,
) -> list[SlotWindow]:
    """Expand ``block`` into its slot windows over ``num_days`` from ``start_date``.

    For each calendar day in ``[start_date, start_date + num_days)`` that (a)
    falls on one of the block's ``days_of_week`` (Monday=0 … Sunday=6) and (b)
    lies within the block's ``active_from`` / ``active_until`` inclusive bounds,
    one :class:`SlotWindow` is emitted. A disabled block, an empty weekday set,
    or ``num_days <= 0`` yields no slots. Windows are returned in chronological
    order.
    """
    if num_days <= 0 or not block.enabled or not block.days_of_week:
        return []
    allowed = set(block.days_of_week)
    slots: list[SlotWindow] = []
    for offset in range(num_days):
        day = start_date + timedelta(days=offset)
        if day.weekday() not in allowed:
            continue
        if block.active_from is not None and day < block.active_from:
            continue
        if block.active_until is not None and day > block.active_until:
            continue
        starts_at = _wall_clock(day, block.start_minute, tz)
        ends_at = _slot_end(block, day, tz)
        # Skip a daypart if either configured boundary falls in a spring gap.
        if starts_at is None or ends_at is None:
            continue
        slots.append(SlotWindow(starts_at=starts_at, ends_at=ends_at))
    return slots


def repeat_prevention_asset_ids(
    session: Session,
    *,
    channel_id: str,
    window_start: datetime,
    window_end: datetime,
    repeat_prevention_days: int,
) -> set[str]:
    """Asset ids already placed on ``channel_id`` near the planning window.

    Returns the distinct ``asset_id`` set from ``schedule_items`` on the channel
    whose ``scheduled_at`` falls in ``[window_start - N days, window_end + N
    days)`` (N = ``repeat_prevention_days``), excluding cancelled items. The
    materializer passes this to ``pick_asset(exclude_asset_ids=…)`` so a rule
    does not re-air an asset within N days of an existing placement. N <= 0
    disables the guard (empty set).

    ``schedule_items`` is the schedule of record (it covers both prior commits
    and items this compile run has already placed). The as-run
    ``program_slot_occurrences`` log is a possible additional "actually aired"
    source the materializer may union in later — see the slice-3 audit.
    """
    if repeat_prevention_days <= 0:
        return set()
    margin = timedelta(days=repeat_prevention_days)
    lower = (window_start - margin).astimezone(UTC)
    upper = (window_end + margin).astimezone(UTC)
    stmt = (
        select(ScheduleItem.asset_id)
        .where(
            ScheduleItem.channel_id == channel_id,
            ScheduleItem.scheduled_at >= lower,
            ScheduleItem.scheduled_at < upper,
            ScheduleItem.state != SCHEDULE_STATE_CANCELLED,
        )
        .distinct()
    )
    return set(session.scalars(stmt))
