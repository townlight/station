# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Committed-schedule adapter: PostgresScheduleStore → ``CommittedSlot`` list.

The EPG exporter (``civiccast.reporting.epg``) speaks the
``CommittedScheduleReader`` protocol — given a station+channel+window it expects
``CommittedSlot`` rows. The schedule store already knows about every published
slot for a channel; this adapter narrows by the half-open ``[from_ts, to_ts)``
window and re-shapes each item into a ``CommittedSlot``.

"Committed" = ``SCHEDULE_STATE_PUBLISHED`` (the operator has explicitly committed
the slot for air, distinct from the in-flight ``scheduled`` work state). EPG
aggregators consume committed plans only — a still-mutable plan would churn the
TV guide.

Category enrichment is optional: callers may pass an ``S22_cf_resolver`` that
looks up an asset's value for a chosen S22 custom-field key (e.g. ``category``).
When absent, ``CommittedSlot.category`` is ``None`` — the EPG exporter's
``field_map`` still controls aggregator column names, but there is nothing to
populate. The resolver is intentionally pluggable so we can wire S22 in without
the adapter taking a hard dependency on the metadata module.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from civiccast.reporting.epg import CommittedSlot
from civiccast.schedule.models import SCHEDULE_STATE_PUBLISHED


class PostgresCommittedScheduleReader:
    """Wraps a ``PostgresScheduleStore`` and surfaces the committed-schedule view.

    Parameters
    ----------
    schedule_store:
        Any object with ``list(channel_id, states)`` returning
        ``ScheduleItemResponse`` rows (``schedule_id``, ``asset_id``,
        ``asset_title``, ``channel_id``, ``scheduled_at``, ``duration_seconds``).
        The real production type is ``civiccast.schedule.store.PostgresScheduleStore``;
        any duck-type that matches works for tests.
    s22_cf_resolver:
        Optional callback ``(asset_id) -> category_or_None`` driving the
        ``CommittedSlot.category`` column. Absent → category is ``None``.
        The S22 field key the resolver looks up is its own concern; the
        adapter is field-agnostic.
    """

    def __init__(
        self,
        schedule_store: object,
        *,
        s22_cf_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        self._schedule_store = schedule_store
        self._s22_cf_resolver = s22_cf_resolver

    def list_committed(
        self,
        *,
        station_id: str,
        channel_id: str,
        from_ts: datetime,
        to_ts: datetime,
    ) -> list[CommittedSlot]:
        """Committed slots for ``channel_id`` whose ``scheduled_at`` lies in
        ``[from_ts, to_ts)``. Ordered ascending by start time."""
        # The store sorts by scheduled_at ASC already.
        items = self._schedule_store.list(  # type: ignore[attr-defined]
            channel_id=channel_id,
            states=(SCHEDULE_STATE_PUBLISHED,),
        )
        slots: list[CommittedSlot] = []
        for item in items:
            start = item.scheduled_at
            if start < from_ts or start >= to_ts:
                continue
            duration_s = int(item.duration_seconds or 0)
            end = start + timedelta(seconds=duration_s)
            title = item.asset_title or item.asset_id or "Untitled"
            category = (
                self._s22_cf_resolver(item.asset_id)
                if (self._s22_cf_resolver is not None and item.asset_id is not None)
                else None
            )
            slots.append(
                CommittedSlot(
                    slot_id=str(item.id),
                    asset_id=item.asset_id,
                    title=title,
                    start=start,
                    end=end,
                    duration_s=duration_s,
                    description=None,
                    category=category,
                    rating=None,
                )
            )
        return slots


__all__ = ["PostgresCommittedScheduleReader"]
