# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Pure occurrence computation for recurring program slots (CA-1).

Given a slot and a half-open UTC window [window_start, window_end), return
UTC occurrence starts that fall inside it. Recurring dates and clock times
follow the station timezone; one-shot values remain absolute instants. No I/O,
no clock reads — callers supply the window and timezone.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, tzinfo

from civiccast.programlog.models import ProgramSlot
from civiccast.timezones import resolve_wall_clock

_WEEKDAY_FRIDAY = 4  # Monday == 0


def compute_occurrences(
    slot: ProgramSlot,
    *,
    window_start: datetime,
    window_end: datetime,
    tz: tzinfo = UTC,
) -> list[datetime]:
    """Return UTC slot starts inside [window_start, window_end).

    ``first_start_at`` remains the absolute first instant. Later recurring
    dates repeat the persisted local calendar/clock anchor in ``tz``.
    """

    if not slot.enabled:
        return []
    first_start = _as_utc(slot.first_start_at)
    if first_start >= _as_utc(window_end):
        return []

    if slot.recurrence == "once":
        if _as_utc(window_start) <= first_start < _as_utc(window_end) and _within_repeat(
            slot, first_start
        ):
            return [first_start]
        return []

    # Legacy pure-function callers retain prior interpretation until the
    # materializer pins their current local anchor. New/stored anchors preserve
    # the original calendar date and clock when the station zone changes.
    first_local = slot.recurrence_anchor_local or first_start.astimezone(tz)
    first_day = first_local.date()
    wall_time = time(
        first_local.hour,
        first_local.minute,
        first_local.second,
        first_local.microsecond,
    )
    step_days = 7 if slot.recurrence == "weekly" else 1
    local_window_start = _as_utc(window_start).astimezone(tz).date()
    local_window_end = _as_utc(window_end).astimezone(tz).date()

    # Jump close to the window using calendar dates, preserving the weekday
    # and wall clock even when the UTC offset changes during the horizon.
    days_behind = max(0, (local_window_start - first_day).days)
    step_count = (days_behind + step_days - 1) // step_days
    candidate_day = first_day + timedelta(days=step_days * step_count)
    start_utc = _as_utc(window_start)
    end_utc = _as_utc(window_end)
    results: list[datetime] = []
    candidate: datetime | None
    while candidate_day <= local_window_end:
        if candidate_day == first_day:
            # The configured first occurrence is an explicit instant. Keep
            # it even when the operator chose the later side of a fall-back.
            candidate = first_start
        else:
            candidate_local = resolve_wall_clock(candidate_day, wall_time, tz)
            candidate = candidate_local.astimezone(UTC) if candidate_local is not None else None
        if candidate is not None and not _within_repeat(slot, candidate):
            break
        if (
            candidate is not None
            and start_utc <= candidate < end_utc
            and (slot.recurrence != "weekdays" or candidate_day.weekday() <= _WEEKDAY_FRIDAY)
        ):
            results.append(candidate)
        candidate_day += timedelta(days=step_days)
    return results


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _within_repeat(slot: ProgramSlot, candidate: datetime) -> bool:
    return slot.repeat_until is None or candidate <= _as_utc(slot.repeat_until)
