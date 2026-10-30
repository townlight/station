# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Timezone helpers for station wall-clock scheduling."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, tzinfo


def resolve_wall_clock(day: date, wall_time: time, tz: tzinfo) -> datetime | None:
    """Resolve a station wall clock to one aware local datetime.

    A nonexistent time during a spring-forward gap returns ``None`` so a
    recurring occurrence is skipped. If a fall-back transition repeats the
    clock, the earlier instant is selected once (``fold=0`` for IANA zones).
    """

    naive = datetime.combine(day, wall_time.replace(tzinfo=None))
    valid: dict[datetime, datetime] = {}
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=tz, fold=fold)
        instant = candidate.astimezone(UTC)
        if instant.astimezone(tz).replace(tzinfo=None) == naive:
            valid[instant] = candidate
    if not valid:
        return None
    return valid[min(valid)]
