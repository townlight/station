# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Rolling-window materializer (CivicCast 3.0 — S18 slice 4).

The compiler that turns auto-schedule *rules* into concrete ``schedule_items``,
composing the slice-2 query executor and the slice-3 planner. Mirrors
periodic auto-schedule compiler's periodic compile (S18 §5 gap 1).

For each enabled :class:`AutoScheduleRule` it:

1. loads the rule's :class:`SavedSearch` + :class:`ScheduleBlock`;
2. expands the block into slot windows over the rule's rolling window
   (:func:`autoschedule_planner.expand_block_slots`);
3. computes the repeat-prevention exclusion set
   (:func:`autoschedule_planner.repeat_prevention_asset_ids`);
4. for each *unoccupied* slot, picks one asset
   (:func:`autoschedule_query.pick_asset`) and writes a ``published`` premiere
   ``schedule_item`` at the slot's start (auto-approved — see below).

**Auto-approved (Commit-to-Air enforcement, owner decision 2026-07-08).**
Materialized items are written directly in the ``published`` state, not
``scheduled``. A query rule's items are auto-approved because the operator
approved the *rule* itself when they created/enabled it — the Commit-to-Air
gate is the operator's per-item approval, and re-approving every rule-picked
slot one at a time would defeat the purpose of automation. A manually-added
schedule item (``PostgresScheduleStore.create``) still lands ``scheduled``
and requires an explicit commit before it airs.

**Idempotency.** Re-running the compile must not double-book. Before filling a
slot the materializer checks (in-memory, seeded per rule from the DB) whether
any non-cancelled, time-bounded item already overlaps it — covering prior
compile runs, manually-entered items, and items this run already placed. So a
re-run over the same horizon adds nothing.

**Transaction.** The materializer owns its ``session``: it writes
``ScheduleItem`` rows directly (rather than via ``PostgresScheduleStore.create``,
which manages its own session and would contend with these reads on SQLite),
each insert wrapped in a ``SAVEPOINT`` (``begin_nested``) so a Postgres
EXCLUDE-overlap race rolls back just that slot and the compile continues. It
commits per rule, so the next rule's occupancy seed sees the prior rule's items.
``last_materialized_at`` is stamped via the store after each rule (idempotency
makes the two-write non-atomicity safe — a re-run re-stamps and adds nothing).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from civiccast.schedule.autoschedule_models import AutoScheduleRule, SavedSearch, ScheduleBlock
from civiccast.schedule.autoschedule_planner import (
    expand_block_slots,
    repeat_prevention_asset_ids,
)
from civiccast.schedule.autoschedule_query import pick_asset
from civiccast.schedule.autoschedule_store import AutoScheduleStore
from civiccast.schedule.models import (
    SCHEDULE_MODE_PREMIERE,
    SCHEDULE_STATE_CANCELLED,
    SCHEDULE_STATE_PUBLISHED,
    ScheduleItem,
)

# Mirror ScheduleItemCreate's premiere duration bounds (the schedule_items
# duration_matches_mode CHECK): a picked asset outside this range can't back a
# premiere and the slot is left for manual handling rather than crashing.
_MIN_DURATION_SECONDS = 1
_MAX_DURATION_SECONDS = 1_209_600  # 14 days

_LOG = logging.getLogger(__name__)
COMPILE_LOCK = threading.RLock()

# Upper bound on enabled rules compiled per run (matches the store's list cap).
# A station with more rules than this would otherwise truncate silently; we log
# instead (step-6 audit ENG-008).
_MAX_COMPILE_RULES = 500


def _ensure_utc(value: datetime) -> datetime:
    """Re-attach UTC to a naive datetime (SQLite drops tzinfo on read)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class SlotFill:
    """One slot the materializer filled."""

    starts_at: datetime
    asset_id: str
    schedule_item_id: str


@dataclass
class RuleMaterializeResult:
    """What one rule produced (or why it produced nothing)."""

    rule_id: str
    channel_id: str
    slots_considered: int = 0
    filled: list[SlotFill] = field(default_factory=list)
    skipped_occupied: int = 0
    skipped_no_asset: int = 0
    skipped_unplayable: int = 0
    skipped_conflict: int = 0
    missing_dependency: bool = False

    @property
    def items_created(self) -> int:
        return len(self.filled)


@dataclass
class MaterializeReport:
    """The whole compile run."""

    results: list[RuleMaterializeResult] = field(default_factory=list)

    @property
    def items_created(self) -> int:
        return sum(r.items_created for r in self.results)


# What a single slot's planning resolved to, before any write happens:
#   fill       — an asset was picked and the slot would be scheduled
#   occupied   — an existing/earlier-planned item already covers the slot
#   no_asset   — the saved search returned nothing eligible for the slot
#   unplayable — the picked asset has no valid duration for a premiere
SlotAction = Literal["fill", "occupied", "no_asset", "unplayable"]


@dataclass(frozen=True)
class SlotDecision:
    """One slot's plan-time decision (no write performed). Shared by the
    materializer's write loop and the dry-run preview so they never diverge."""

    starts_at: datetime
    ends_at: datetime
    action: SlotAction
    asset_id: str | None = None
    title: str | None = None
    duration_seconds: int | None = None


def _overlaps(intervals: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    """True if [start, end) overlaps any existing [s, e) (half-open)."""
    return any(s < end and e > start for s, e in intervals)


def _seed_occupied(
    session: Session, channel_id: str, horizon_start: datetime, horizon_end: datetime
) -> list[tuple[datetime, datetime]]:
    """Existing non-cancelled, time-bounded item intervals on the channel that
    overlap the horizon — the idempotency / no-double-book baseline."""
    stmt = select(ScheduleItem.scheduled_at, ScheduleItem.scheduled_at_end).where(
        ScheduleItem.channel_id == channel_id,
        ScheduleItem.state != SCHEDULE_STATE_CANCELLED,
        ScheduleItem.scheduled_at_end.is_not(None),
        ScheduleItem.scheduled_at < horizon_end.astimezone(UTC),
        ScheduleItem.scheduled_at_end > horizon_start.astimezone(UTC),
    )
    return [(_ensure_utc(start), _ensure_utc(end)) for start, end in session.execute(stmt)]


_PROVENANCE_PREFIX = "auto-schedule:"


def _protected_through(
    session: Session, rule: AutoScheduleRule, block: ScheduleBlock, tz: tzinfo
) -> date | None:
    """Preserve the generated calendar horizon across station timezone edits.

    Notes carry ownership/date for new rows. Pre-station-time compiles wrote
    UTC rows without ownership; matching published UTC block starts are kept
    conservatively, including a manual placement at that same start.
    """
    days: list[date] = []
    notes = session.execute(
        select(ScheduleItem.notes, ScheduleItem.scheduled_at).where(
            ScheduleItem.channel_id == rule.channel_id,
            ScheduleItem.notes.startswith(_PROVENANCE_PREFIX),
        )
    )
    for note, start in notes:
        if note is None:
            continue
        try:
            provenance = json.loads(note[len(_PROVENANCE_PREFIX) :])
            if provenance["rule_id"] == rule.rule_id:
                days.append(
                    max(
                        date.fromisoformat(provenance["day"]),
                        _ensure_utc(start).astimezone(tz).date(),
                    )
                )
        except (ValueError, KeyError, TypeError):
            continue
    if rule.last_materialized_at is not None:
        legacy_slots = expand_block_slots(
            block,
            start_date=_ensure_utc(rule.last_materialized_at).astimezone(UTC).date(),
            num_days=rule.rolling_window_days,
            tz=UTC,
        )
        if legacy_slots:
            expected = {slot.starts_at for slot in legacy_slots}
            starts = session.scalars(
                select(ScheduleItem.scheduled_at).where(
                    ScheduleItem.channel_id == rule.channel_id,
                    ScheduleItem.notes.is_(None),
                    ScheduleItem.state == SCHEDULE_STATE_PUBLISHED,
                    ScheduleItem.scheduled_at >= legacy_slots[0].starts_at,
                    ScheduleItem.scheduled_at <= legacy_slots[-1].starts_at,
                )
            )
            days.extend(
                max(_ensure_utc(start).date(), _ensure_utc(start).astimezone(tz).date())
                for start in starts
                if _ensure_utc(start) in expected
            )
    return max(days, default=None)


def plan_rule_slots(
    session: Session,
    *,
    rule: AutoScheduleRule,
    search: SavedSearch,
    block: ScheduleBlock,
    now: datetime,
    tz: tzinfo,
    rng: object | None = None,
) -> list[SlotDecision]:
    """Resolve every slot a rule would fill, WITHOUT writing anything.

    Reads only (occupancy seed, repeat-prevention, asset pick) and simulates the
    fill progression in memory (occupancy + within-window repeat-prevention
    accumulate as if each ``fill`` were placed). One :class:`SlotDecision` per
    slot, in chronological order. Both the materializer's write loop and the
    dry-run preview consume this, so a preview can never diverge from what a
    compile would do (modulo a Postgres write-time conflict, which only the
    write loop can observe).
    """
    slots = expand_block_slots(
        block,
        start_date=now.astimezone(tz).date(),
        num_days=rule.rolling_window_days,
        tz=tz,
    )
    if not slots:
        return []

    horizon_start, horizon_end = slots[0].starts_at, slots[-1].ends_at
    occupied = _seed_occupied(session, rule.channel_id, horizon_start, horizon_end)
    protected_through = _protected_through(session, rule, block, tz)
    # Repeat-prevention is a coarse, horizon-relative window ([start-N, end+N]),
    # not precise per-slot N-day spacing — intentional (step-6 audit ENG-003): the
    # in-run dedup below + the DB window across runs prevent a within-N-days
    # double-air; sparse-daypart precision is a documented next-sprint item.
    recent = repeat_prevention_asset_ids(
        session,
        channel_id=rule.channel_id,
        window_start=horizon_start,
        window_end=horizon_end,
        repeat_prevention_days=rule.repeat_prevention_days,
    )
    run_excluded: set[str] = set()
    decisions: list[SlotDecision] = []

    for slot in slots:
        if (
            protected_through is not None and slot.starts_at.date() <= protected_through
        ) or _overlaps(occupied, slot.starts_at, slot.ends_at):
            decisions.append(SlotDecision(slot.starts_at, slot.ends_at, "occupied"))
            continue
        asset = pick_asset(
            session,
            search.query,
            rule.pick_strategy,
            rng=rng,  # type: ignore[arg-type]
            exclude_asset_ids=recent | run_excluded,
        )
        if asset is None:
            decisions.append(SlotDecision(slot.starts_at, slot.ends_at, "no_asset"))
            continue
        duration = asset.duration_seconds
        if duration is None or not (_MIN_DURATION_SECONDS <= duration <= _MAX_DURATION_SECONDS):
            # A premiere needs a valid duration; leave the slot for an operator.
            decisions.append(
                SlotDecision(
                    slot.starts_at,
                    slot.ends_at,
                    "unplayable",
                    asset_id=asset.asset_id,
                    title=asset.title,
                )
            )
            continue

        decisions.append(
            SlotDecision(
                slot.starts_at,
                slot.ends_at,
                "fill",
                asset_id=asset.asset_id,
                title=asset.title,
                duration_seconds=duration,
            )
        )
        # Simulate the placement so later slots see it occupied + (when the rule
        # asks for repeat prevention) don't re-pick the same asset this run.
        occupied.append((slot.starts_at, slot.ends_at))
        if rule.repeat_prevention_days > 0:
            run_excluded.add(asset.asset_id)

    return decisions


def _materialize_rule(
    session: Session,
    *,
    rule: AutoScheduleRule,
    search: SavedSearch,
    block: ScheduleBlock,
    now: datetime,
    tz: tzinfo,
    rng: object | None,
) -> RuleMaterializeResult:
    result = RuleMaterializeResult(rule_id=rule.rule_id, channel_id=rule.channel_id)
    decisions = plan_rule_slots(
        session, rule=rule, search=search, block=block, now=now, tz=tz, rng=rng
    )
    result.slots_considered = len(decisions)

    for decision in decisions:
        if decision.action == "occupied":
            result.skipped_occupied += 1
            continue
        if decision.action == "no_asset":
            result.skipped_no_asset += 1
            continue
        if decision.action == "unplayable":
            result.skipped_unplayable += 1
            continue

        # action == "fill": persist the planned premiere. Auto-approved
        # (Commit-to-Air enforcement): born ``published``, not ``scheduled``
        # — see the module docstring.
        assert decision.duration_seconds is not None
        scheduled_at = decision.starts_at.astimezone(UTC)
        item = ScheduleItem(
            asset_id=decision.asset_id,
            channel_id=rule.channel_id,
            mode=SCHEDULE_MODE_PREMIERE,
            state=SCHEDULE_STATE_PUBLISHED,
            scheduled_at=scheduled_at,
            scheduled_at_end=scheduled_at + timedelta(seconds=decision.duration_seconds),
            duration_seconds=decision.duration_seconds,
            notes=_PROVENANCE_PREFIX
            + json.dumps(
                {
                    "rule_id": rule.rule_id,
                    "day": decision.starts_at.date().isoformat(),
                },
                separators=(",", ":"),
            ),
        )
        try:
            with session.begin_nested():
                session.add(item)
                session.flush()
        except IntegrityError:
            # Postgres EXCLUDE-overlap race (the in-memory occupancy check in
            # plan_rule_slots already prevents the common case). Skip + continue.
            result.skipped_conflict += 1
            continue

        result.filled.append(
            SlotFill(
                starts_at=decision.starts_at,
                asset_id=decision.asset_id,  # type: ignore[arg-type]
                schedule_item_id=str(item.id),
            )
        )

    return result


def compile_rules(
    session: Session,
    autoschedule_store: AutoScheduleStore,
    *,
    now: datetime,
    tz: tzinfo | Callable[[], tzinfo] = UTC,
    rng: object | None = None,
) -> MaterializeReport:
    """Serialize API/background compiles through all per-rule commits."""
    with COMPILE_LOCK:
        resolved_tz = tz() if callable(tz) else tz
        return _compile_rules(session, autoschedule_store, now=now, tz=resolved_tz, rng=rng)


def _compile_rules(
    session: Session,
    autoschedule_store: AutoScheduleStore,
    *,
    now: datetime,
    tz: tzinfo = UTC,
    rng: object | None = None,
) -> MaterializeReport:
    """Compile every enabled auto-schedule rule into ``schedule_items``.

    ``session`` is the materializer's write/read session (committed per rule).
    ``autoschedule_store`` loads the rules / searches / blocks and stamps
    ``last_materialized_at``. ``now`` is the injected clock (slot expansion
    anchors on its date in ``tz``); ``tz`` is the station's wall-clock zone (default
    UTC — see the planner's timezone note); ``rng`` is an optional
    ``random.Random`` for deterministic ``random_result`` picks in tests.
    """
    report = MaterializeReport()
    rules = autoschedule_store.list_auto_schedule_rules(enabled_only=True, limit=_MAX_COMPILE_RULES)
    if len(rules) >= _MAX_COMPILE_RULES:
        _LOG.warning(
            "auto-schedule compile hit the %d enabled-rule cap; some rules may "
            "not be compiled this run.",
            _MAX_COMPILE_RULES,
        )
    for rule in rules:
        search = autoschedule_store.get_saved_search(rule.saved_search_id)
        block = autoschedule_store.get_schedule_block(rule.schedule_block_id)
        if search is None or block is None:
            # Dangling soft reference (no FK by design) — record and move on.
            report.results.append(
                RuleMaterializeResult(
                    rule_id=rule.rule_id,
                    channel_id=rule.channel_id,
                    missing_dependency=True,
                )
            )
            continue

        result = _materialize_rule(
            session, rule=rule, search=search, block=block, now=now, tz=tz, rng=rng
        )
        session.commit()

        rule.last_materialized_at = now
        autoschedule_store.upsert_auto_schedule_rule(rule)
        report.results.append(result)

    return report
