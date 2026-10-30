# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""WP-08: value/unit/forever retention-term authoring (finalization plan
section 6, work package WP-08 -- "Add retention value/unit/forever
authoring without changing enforcement").

This module is deliberately small and dependency-free: it is the pure
arithmetic + validation layer for the new authoring contract. It does not
touch persistence, the API layer, or the enforcement worker.

Data contract (finalization plan, verbatim):

* Finite: a positive integer ``value`` plus a unit of ``days``, ``weeks``,
  ``months``, or ``years``.
* Infinite: ``forever``, which carries no numeric value.
* Days/weeks are elapsed-duration arithmetic (a fixed number of seconds).
* Months/years are calendar additions performed in the station's local
  timezone, clamped at end-of-month, leap-day safe, and converted to UTC
  only for the final persisted instant -- this keeps "1 year from a March
  publish" landing in March even though the number of elapsed UTC seconds
  in a year varies with DST and leap days.

This module never invents an anchor. Anchor capture/reuse is the caller's
(``civiccast.schedule.store``) responsibility -- see
``civiccast/schedule/models.py``'s ``Asset.retention_anchor_at`` docstring
for the immutability contract.
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

RETENTION_TERM_UNIT_DAYS = "days"
RETENTION_TERM_UNIT_WEEKS = "weeks"
RETENTION_TERM_UNIT_MONTHS = "months"
RETENTION_TERM_UNIT_YEARS = "years"
RETENTION_TERM_UNIT_FOREVER = "forever"

RETENTION_TERM_UNITS: tuple[str, ...] = (
    RETENTION_TERM_UNIT_DAYS,
    RETENTION_TERM_UNIT_WEEKS,
    RETENTION_TERM_UNIT_MONTHS,
    RETENTION_TERM_UNIT_YEARS,
    RETENTION_TERM_UNIT_FOREVER,
)

# Units whose arithmetic is a fixed elapsed duration, independent of
# calendar/timezone.
_ELAPSED_UNITS = (RETENTION_TERM_UNIT_DAYS, RETENTION_TERM_UNIT_WEEKS)
# Units whose arithmetic is a calendar addition in station-local time.
_CALENDAR_UNITS = (RETENTION_TERM_UNIT_MONTHS, RETENTION_TERM_UNIT_YEARS)

RetentionTermUnit = Literal["days", "weeks", "months", "years", "forever"]

# Legacy-conversion prefill suggestions (finalization plan section 6, item
# 8: "Present existing state presets as prefilled value/unit suggestions,
# not as an external legal decision.") These are display-only hints for
# an operator about to convert a legacy row to the new contract -- never
# applied to a stored row automatically. The v1.0 short-hand meaning is
# code-defined (``civiccast.schedule.models.RETENTION_SHORT``'s docstring:
# "drop after 30 days"), so representing it as 30 days is not inventing a
# new duration, only restating the existing one in the new shape.
LEGACY_SHORT_SUGGESTED_VALUE = 30
LEGACY_SHORT_SUGGESTED_UNIT: RetentionTermUnit = "days"

# Coordinator-directed fix (follow-up commit, MAJOR finding 1): an
# unbounded ``retention_term_value`` let ``timedelta(days=value)`` /
# ``timedelta(weeks=value)`` raise :class:`OverflowError` for a large
# enough integer -- a type the router only mapped from ``ValueError``, so
# it fell through to an uncaught 500 instead of a 422. A documented,
# generous ceiling closes that: 200 years, expressed per unit with a
# safety margin (366 days/year, 53 weeks/year, 12 months/year) so the
# bound never rejects a legitimate long-but-finite public-records term
# while still keeping every unit's arithmetic comfortably inside
# ``timedelta``'s own range (max ~2.7 million years) -- no unit can ever
# reach ``OverflowError`` through this bound. Enforced here (the single
# source of truth for both the Pydantic-level ``AssetMetadataUpdate``
# validator and this module's own arithmetic), not just at the API
# boundary, so a caller that bypasses Pydantic (the ephemeral store, a
# future direct caller) still cannot construct an overflow-prone term.
MAX_RETENTION_YEARS = 200
_RETENTION_TERM_MAX_VALUE: dict[str, int] = {
    RETENTION_TERM_UNIT_DAYS: MAX_RETENTION_YEARS * 366,
    RETENTION_TERM_UNIT_WEEKS: MAX_RETENTION_YEARS * 53,
    RETENTION_TERM_UNIT_MONTHS: MAX_RETENTION_YEARS * 12,
    RETENTION_TERM_UNIT_YEARS: MAX_RETENTION_YEARS,
}


# The largest per-unit ceiling above (days, at 366 days * 200 years) --
# a single outer sanity bound usable as a Pydantic ``Field(le=...)`` on
# ``retention_term_value`` before any unit-specific check runs, so a
# wildly out-of-range value (a typo, a hostile client) is rejected by
# FastAPI's own request-body validation without ever reaching a custom
# validator, the store, or the arithmetic layer.
RETENTION_TERM_VALUE_ABSOLUTE_MAX = max(_RETENTION_TERM_MAX_VALUE.values())


def max_value_for_unit(unit: str) -> int | None:
    """The largest accepted ``value`` for ``unit``, or ``None`` for ``forever``
    (which never carries one) or an unrecognized unit (``validate_term``
    is the source of truth for unit membership; this returns ``None``
    rather than raising so callers can probe without a try/except)."""
    return _RETENTION_TERM_MAX_VALUE.get(unit)


def resolve_station_zoneinfo(tz_name: str | None) -> ZoneInfo | None:
    """Best-effort IANA zone lookup for a station timezone name.

    Returns ``None`` (caller falls back to UTC) for the empty string, the
    ``"local"`` sentinel default, or an unresolvable/invalid name -- this
    mirrors ``civiccast.app._station_tz``'s honest-fallback posture rather
    than raising on a station that has not picked a real zone yet.
    """
    if not tz_name or tz_name == "local":
        return None
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def add_calendar_months(dt: datetime, months: int) -> datetime:
    """Add whole calendar months to ``dt``, clamping at end-of-month.

    E.g. Jan 31 + 1 month -> Feb 28 (Feb 29 in a leap year), never
    "spills over" into March. Preserves ``dt``'s time-of-day and tzinfo
    (if any) unchanged; only the date fields move.
    """
    month_index = dt.month - 1 + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def validate_term(unit: str, value: int | None) -> None:
    """Raise :class:`ValueError` if ``(unit, value)`` is not a valid term.

    ``forever`` must carry no value; every finite unit must carry a
    positive integer value no larger than that unit's
    :data:`MAX_RETENTION_YEARS`-derived ceiling (coordinator-directed
    fix, MAJOR finding 1 -- see the ceiling table's comment above for
    why). ``bool`` is deliberately rejected even though it is a Python
    ``int`` subclass (coordinator-directed fix, MINOR finding 3) --
    ``True``/``False`` are never a meaningful retention length.
    """
    if unit not in RETENTION_TERM_UNITS:
        raise ValueError(f"retention term unit must be one of {RETENTION_TERM_UNITS}, got {unit!r}")
    if unit == RETENTION_TERM_UNIT_FOREVER:
        if value is not None:
            raise ValueError("retention term value must be omitted when unit is 'forever'")
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"retention term value must be a positive integer for unit {unit!r}, got {value!r}"
        )
    if value <= 0:
        raise ValueError(
            f"retention term value must be a positive integer for unit {unit!r}, got {value!r}"
        )
    max_value = _RETENTION_TERM_MAX_VALUE[unit]
    if value > max_value:
        raise ValueError(
            f"retention term value {value} exceeds the maximum of {max_value} {unit} "
            f"({MAX_RETENTION_YEARS} years)"
        )


def compute_retention_until(
    *,
    anchor_at: datetime,
    unit: str,
    value: int | None,
    station_tz_name: str | None,
) -> datetime | None:
    """Compute the UTC retention deadline for one value/unit/forever term.

    ``anchor_at`` is ``Asset.retention_anchor_at`` -- immutable once
    captured, so every call recomputes from the *same* fixed point in
    time; this function does not read or write anchor state, it is pure.

    Returns ``None`` for ``forever`` (no deadline; the caller mirrors this
    onto the legacy ``retention_policy='permanent'`` column so the
    existing, unmodified enforcement worker continues to skip it via its
    own ``retention_policy != 'permanent'`` filter).
    """
    validate_term(unit, value)

    if anchor_at.tzinfo is None:
        anchor_at = anchor_at.replace(tzinfo=UTC)
    anchor_utc = anchor_at.astimezone(UTC)

    if unit == RETENTION_TERM_UNIT_FOREVER:
        return None

    assert value is not None  # narrowed by validate_term above

    if unit == RETENTION_TERM_UNIT_DAYS:
        return anchor_utc + timedelta(days=value)
    if unit == RETENTION_TERM_UNIT_WEEKS:
        return anchor_utc + timedelta(weeks=value)

    # months / years: calendar addition in station-local wall-clock time.
    months = value if unit == RETENTION_TERM_UNIT_MONTHS else value * 12
    zone = resolve_station_zoneinfo(station_tz_name) or UTC
    anchor_local = anchor_utc.astimezone(zone)
    # Strip tzinfo, do naive calendar arithmetic, then reattach the SAME
    # zone object (not the anchor's resolved offset) so zoneinfo resolves
    # the correct standard/DST offset for the RESULT's wall-clock instant.
    # This is the DST-safe pattern for PEP 495 zoneinfo -- reusing the
    # anchor's raw utcoffset would silently apply the wrong offset across
    # a spring-forward/fall-back boundary.
    naive_result = add_calendar_months(anchor_local.replace(tzinfo=None), months)
    result_local = naive_result.replace(tzinfo=zone)
    return result_local.astimezone(UTC)
