# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S24 underwriting service layer.

Two slices share this module:

* **Slice 2 (this file, top section): trafficking compiler.** Decides which
  underwriting spot fills each candidate program-log break slot, respecting
  the S24 DC-1 / DC-4 constraints (flight window + daypart + per-day cap)
  and the DC-5 station-policy FCC-ack gate. The compiler does NOT discover
  candidate break slots — those come from the S4 program-log builder; this
  layer just decides WHICH spot goes WHERE and writes a deterministic
  :class:`~civiccast.underwriting.models.SpotPlacement` per slot so a re-run
  with the same candidates produces the same placements (idempotent on
  ``placement_id = "pl-<schedule_item_id>"``).
* **Slice 3 (appended below): underwriter affidavit report.** A view over
  S23 ``as_run_log`` joined through ``spot_placements`` →
  ``spot_flights`` → ``underwriting_spots`` to attribute each aired second
  back to an underwriter for billing. Owned by a separate agent.

Each top-level class carries a ``# slice <n>:`` comment header so the two
slices stay clearly demarcated.
"""

from __future__ import annotations

import csv
import io
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from civiccast.common.csv_safety import csv_safe
from civiccast.underwriting.models import (
    Slug,
    SpotFlight,
    SpotPlacement,
    UnderwritingSpot,
)
from civiccast.underwriting.store import UnderwritingStore

if TYPE_CHECKING:
    from civiccast.reporting.store import ReportingStore
    from civiccast.schedule.autoschedule_models import ScheduleBlock


# ===========================================================================
# slice 2: trafficking compiler
# ===========================================================================


# A resolver maps a flight's loose ``daypart_block_id`` to an S19 ScheduleBlock.
# It returns ``None`` when the id does not resolve (a dangling reference); the
# compiler treats that as "drop this flight" rather than silently ignoring the
# daypart, which is the safer default — a flight that *says* it has a daypart
# but cannot prove its window must not air everywhere.
DaypartResolver = Callable[[str], "ScheduleBlock | None"]

SkipReason = Literal[
    "no_active_flight_for_channel",
    "all_eligible_flights_at_cap",
    "all_eligible_flights_outside_daypart",
    "no_eligible_after_filters",
]


class CandidateBreakSlot(BaseModel):
    """A break/interstitial slot in the program log that wants an underwriting spot.

    The ``schedule_item`` already exists in the program log (placed by the
    S4 program-log builder); the trafficking compiler does NOT create or
    modify schedule_items — it only DECIDES which underwriting spot fills
    each slot and records a :class:`SpotPlacement` that points back to the
    existing ``schedule_item_id``. Slice 4 wires this to the S4 break-slot
    discovery path.
    """

    model_config = ConfigDict(extra="forbid")

    channel_id: Slug
    scheduled_at: datetime  # tz-aware UTC; the daypart filter converts to local
    schedule_item_id: Slug  # the program-log row this break slot maps to


class SkippedSlot(BaseModel):
    """A candidate break slot the compiler could not fill, plus the reason.

    Reason hierarchy (most-specific first when computing): if the channel
    filter drops everyone → ``no_active_flight_for_channel``; if the daypart
    filter is the last gate that drops everyone → ``all_eligible_flights_outside_daypart``;
    if the per-day cap is the last gate that drops everyone →
    ``all_eligible_flights_at_cap``; otherwise (FCC-ack gate or missing-spot
    drop is the last filter to empty the pool) → ``no_eligible_after_filters``.
    """

    model_config = ConfigDict(extra="forbid")

    candidate: CandidateBreakSlot
    reason: SkipReason


class CompileResult(BaseModel):
    """Outcome of one :meth:`TraffickingCompiler.compile_for_date` call.

    ``placements`` is ordered by the input candidate order (the compiler walks
    candidates sequentially, picking one flight per slot); ``skipped`` lists
    every candidate that could not be filled with the reason hierarchy above.
    """

    model_config = ConfigDict(extra="forbid")

    for_date: date
    placements: list[SpotPlacement]
    skipped: list[SkippedSlot]


def _utc_day_window(for_date: date) -> tuple[datetime, datetime]:
    """Half-open UTC-midnight ``[from_ts, to_ts)`` window for the per-day cap.

    Note: DC-4 says "per day"; the compiler uses **UTC-midnight day boundaries**
    for the cap count, not the operator's local calendar day. This is a
    deliberate-and-documented choice for slice 2 — switching to local-day
    boundaries would require the cap query to know the operator's tz and is a
    follow-up if a station's compliance contract demands it.
    """
    start = datetime(for_date.year, for_date.month, for_date.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _local_minute_and_dow(
    scheduled_at: datetime,
    local_tz_offset_minutes: int,
) -> tuple[int, int]:
    """Translate a UTC-aware ``scheduled_at`` to (local-minute-of-day, local-DOW).

    ``local_tz_offset_minutes`` is the offset from UTC of the operator's local
    wall clock (e.g. ``-300`` for EST, ``-240`` for EDT). Days are 0=Monday..
    6=Sunday, matching :class:`ScheduleBlock.days_of_week`.
    """
    # Promote naive to UTC defensively — the public surface declares UTC, but
    # tests may construct datetimes through factories that drop tz.
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=UTC)
    local = scheduled_at.astimezone(UTC) + timedelta(minutes=local_tz_offset_minutes)
    minute_of_day = local.hour * 60 + local.minute
    return minute_of_day, local.weekday()


class TraffickingCompiler:
    """Decides which underwriting spot fills each candidate break slot.

    Algorithm (per candidate, in input order):

    1. Resolve active flights via ``store.list_flights(active_on=for_date)``.
    2. Keep flights whose ``channels`` contain ``candidate.channel_id``.
    3. Resolve each flight's spot; drop the flight when the spot is missing,
       OR when ``require_fcc_ack=True`` and the spot's ``fcc_compliant_ack``
       is False (DC-5 station-policy gate).
    4. Daypart filter (DC-1): when a ``daypart_resolver`` is provided AND the
       flight has a ``daypart_block_id``, resolve it and require the local
       wall-clock minute-of-day to be in ``[start_minute, end_minute)`` AND
       the local weekday to be in ``days_of_week``. A resolver that returns
       ``None`` drops the flight (dangling-ref defensive).
    5. Frequency cap filter (DC-4): per-(channel, flight, UTC-midnight day)
       count must stay below ``frequency_cap_per_day``. Placements chosen
       **earlier in the same compile_for_date call** count toward the cap so
       two simultaneous candidates cannot together exceed it.
    6. Picker: among eligible flights, pick the one with the **fewest
       placements over the flight's lifetime** (round-robin-fair across the
       campaign), ties broken by earliest ``flight.created_at`` then
       ``flight_id`` ASC.
    7. Materialize a :class:`SpotPlacement` with a deterministic
       ``placement_id = "pl-<schedule_item_id>"`` so a re-run idempotently
       overwrites instead of duplicating (the store's ``record_placement`` is
       also idempotent on ``placement_id``).
    """

    def __init__(
        self,
        store: UnderwritingStore,
        *,
        daypart_resolver: DaypartResolver | None = None,
        require_fcc_ack: bool = False,
        station_id: str = "civiccast-station",
    ) -> None:
        self._store = store
        self._daypart_resolver = daypart_resolver
        self._require_fcc_ack = require_fcc_ack
        self._station_id = station_id
        # E-4: serialize concurrent ``compile_for_date(station, date)`` callers
        # in the same process so two threads cannot each see ``count < cap``
        # and both insert. The ``_locks_guard`` protects the lock dictionary
        # itself; per-(station, date) locks live in ``_locks``. This is an
        # in-process lock — for multi-process deployments the operator-facing
        # docstring documents the contract; a PG advisory lock would be the
        # cross-process equivalent and is a follow-up if the playout host
        # ever runs more than one compiler process.
        self._locks_guard = threading.Lock()
        self._locks: dict[tuple[str, date], threading.Lock] = {}

    def _lock_for(self, for_date: date) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get((self._station_id, for_date))
            if lock is None:
                lock = threading.Lock()
                self._locks[(self._station_id, for_date)] = lock
            return lock

    def compile_for_date(
        self,
        *,
        for_date: date,
        candidates: list[CandidateBreakSlot],
        local_tz_offset_minutes: int = 0,
    ) -> CompileResult:
        """Place candidate break slots onto eligible flights for one day.

        Concurrent callers on the same ``(station_id, for_date)`` serialize via
        an in-process lock (E-4): the cap accounting + idempotent
        ``placement_id = "pl-<schedule_item_id>"`` mean two concurrent runs
        with the same candidate set converge on the same placements rather
        than double-counting or producing nondeterministic flight attribution.
        """
        if not candidates:
            return CompileResult(for_date=for_date, placements=[], skipped=[])

        with self._lock_for(for_date):
            return self._compile_locked(
                for_date=for_date,
                candidates=candidates,
                local_tz_offset_minutes=local_tz_offset_minutes,
            )

    def _compile_locked(
        self,
        *,
        for_date: date,
        candidates: list[CandidateBreakSlot],
        local_tz_offset_minutes: int,
    ) -> CompileResult:
        active_flights = self._store.list_flights(active_on=for_date)
        day_from, day_to = _utc_day_window(for_date)

        # E-1 (perf): one bulk query for the per-day cap tallies + one bulk
        # query for the lifetime picker tallies, instead of two ``list_placements``
        # calls per candidate per surviving flight. The per-day cap is
        # GLOBAL across the channels a flight targets (T-2: spec says "per day"
        # with no per-channel qualifier — a flight targeting four channels with
        # ``cap=3`` must not air 12 times that day). Lifetime picker counts are
        # also flight-wide.
        flight_ids = [f.flight_id for f in active_flights]
        per_day_tally: dict[str, int] = self._store.count_placements_by_flight(
            flight_ids=flight_ids,
            from_ts=day_from,
            to_ts=day_to,
        )
        lifetime_tally: dict[str, int] = self._store.count_placements_by_flight(
            flight_ids=flight_ids,
        )

        # Resolve spots up-front so the per-candidate hot loop is pure-Python.
        spot_by_flight: dict[str, UnderwritingSpot | None] = {
            f.flight_id: self._store.get_spot(f.spot_id) for f in active_flights
        }

        placements: list[SpotPlacement] = []
        skipped: list[SkippedSlot] = []

        for candidate in candidates:
            picked, reason = self._pick_flight(
                candidate=candidate,
                active_flights=active_flights,
                spot_by_flight=spot_by_flight,
                per_day_tally=per_day_tally,
                lifetime_tally=lifetime_tally,
                local_tz_offset_minutes=local_tz_offset_minutes,
            )
            if picked is None:
                assert reason is not None  # invariant of _pick_flight
                skipped.append(SkippedSlot(candidate=candidate, reason=reason))
                continue
            placement = SpotPlacement(
                placement_id=f"pl-{candidate.schedule_item_id}",
                flight_id=picked.flight_id,
                channel_id=candidate.channel_id,
                scheduled_at=candidate.scheduled_at,
                schedule_item_id=candidate.schedule_item_id,
            )
            persisted = self._store.record_placement(placement)
            placements.append(persisted)
            # In-memory increments so the next candidate's cap + picker see
            # this run's earlier choices without a DB round-trip.
            per_day_tally[picked.flight_id] = per_day_tally.get(picked.flight_id, 0) + 1
            lifetime_tally[picked.flight_id] = lifetime_tally.get(picked.flight_id, 0) + 1

        return CompileResult(for_date=for_date, placements=placements, skipped=skipped)

    # --- internals ------------------------------------------------------

    def _pick_flight(
        self,
        *,
        candidate: CandidateBreakSlot,
        active_flights: list[SpotFlight],
        spot_by_flight: dict[str, UnderwritingSpot | None],
        per_day_tally: dict[str, int],
        lifetime_tally: dict[str, int],
        local_tz_offset_minutes: int,
    ) -> tuple[SpotFlight | None, SkipReason | None]:
        # Stage 1 — channel filter.
        on_channel = [f for f in active_flights if candidate.channel_id in f.channels]
        if not on_channel:
            return None, "no_active_flight_for_channel"

        # Stage 2 — spot resolve + FCC-ack gate. A missing spot or an
        # unattested spot under station policy both drop the flight; we
        # collect them here so the skip-reason hierarchy can fall through to
        # "no_eligible_after_filters" when this stage is what emptied the pool.
        after_spot: list[SpotFlight] = []
        for flight in on_channel:
            spot = spot_by_flight.get(flight.flight_id)
            if spot is None:
                continue
            if self._require_fcc_ack and not spot.fcc_compliant_ack:
                continue
            after_spot.append(flight)
        if not after_spot:
            return None, "no_eligible_after_filters"

        # Stage 3 — daypart filter.
        after_daypart: list[SpotFlight] = []
        daypart_dropped_anyone = False
        for flight in after_spot:
            if not self._daypart_in_window(
                flight=flight,
                scheduled_at=candidate.scheduled_at,
                local_tz_offset_minutes=local_tz_offset_minutes,
            ):
                daypart_dropped_anyone = True
                continue
            after_daypart.append(flight)
        if not after_daypart:
            # The daypart was the last gate that emptied the pool.
            if daypart_dropped_anyone:
                return None, "all_eligible_flights_outside_daypart"
            return None, "no_eligible_after_filters"

        # Stage 4 — frequency cap. The tally is across ALL channels the flight
        # targets (T-2): the spec's ``frequency_cap_per_day`` carries no
        # per-channel qualifier, so a multi-channel flight cap is a single
        # flight-wide budget for the day.
        after_cap: list[SpotFlight] = []
        cap_dropped_anyone = False
        for flight in after_daypart:
            cap = flight.frequency_cap_per_day
            if cap is None:
                after_cap.append(flight)
                continue
            if per_day_tally.get(flight.flight_id, 0) >= cap:
                cap_dropped_anyone = True
                continue
            after_cap.append(flight)
        if not after_cap:
            if cap_dropped_anyone:
                return None, "all_eligible_flights_at_cap"
            return None, "no_eligible_after_filters"

        # Stage 5 — pick the round-robin-fair winner.
        chosen = self._pick_least_aired(after_cap, lifetime_tally=lifetime_tally)
        return chosen, None  # caller ignores reason when chosen is not None

    def _daypart_in_window(
        self,
        *,
        flight: SpotFlight,
        scheduled_at: datetime,
        local_tz_offset_minutes: int,
    ) -> bool:
        """Daypart filter (DC-1).

        * No ``daypart_block_id`` → flight is unconstrained by daypart.
        * Resolver is None → daypart not enforceable in this caller → keep.
          (Slice 4 wires the real resolver; tests pass one in to exercise.)
        * Resolver returns None → dangling ref → DROP (defensive default).
        * Otherwise: local-weekday must be in ``days_of_week`` AND local
          minute-of-day must be in ``[start_minute, end_minute)``.
        """
        if flight.daypart_block_id is None:
            return True
        if self._daypart_resolver is None:
            return True
        block = self._daypart_resolver(flight.daypart_block_id)
        if block is None:
            return False
        minute_of_day, dow = _local_minute_and_dow(scheduled_at, local_tz_offset_minutes)
        if dow not in block.days_of_week:
            return False
        return block.start_minute <= minute_of_day < block.end_minute

    def _pick_least_aired(
        self,
        candidates: list[SpotFlight],
        *,
        lifetime_tally: dict[str, int],
    ) -> SpotFlight:
        """Round-robin-fair picker.

        Equalizes per-flight exposure across the campaign by selecting the
        flight with the fewest lifetime placements. The lifetime tally is
        a per-call dict primed from one bulk COUNT-GROUP-BY query at the top
        of ``_compile_locked`` and incremented in-memory as this run places
        spots — so the picker is O(F) per candidate with zero per-candidate
        DB calls. Tiebreaker: earliest ``flight.created_at``, then ``flight_id``
        ascending — deterministic so the compiler is stable under re-runs.
        """

        def key(flight: SpotFlight) -> tuple[int, datetime, str]:
            return (lifetime_tally.get(flight.flight_id, 0), flight.created_at, flight.flight_id)

        return min(candidates, key=key)


# ============================================================================
# Slice 3 — Underwriter affidavits (proof-of-airing join over S23 as_run_log)
# ============================================================================
#
# Per-underwriter proof-of-airing report: walk S23's append-only as-run ledger,
# filter to spots, join to placements, attribute each aired second back to the
# underwriter for billing. Spec §6 (algorithm) + §7 DC-3 + §5 (PDF/CSV).
#
# The join walks ``as_run_log`` → ``spot_placements`` (by ``schedule_item_id``)
# → ``spot_flights`` (transitively, via spot_id from the spot's set) →
# ``underwriting_spots`` (by underwriter). Asset+channel set membership is the
# primary filter; the placement join is only used to populate
# ``placement_id`` for traceability — an as-run row with no matching placement
# is still counted (legacy / manual ad-hoc spots).
#
# Half-open UTC window: ``[period_start, 0:00 UTC, period_end + 1d, 0:00 UTC)``.
# The inclusive ``period_end`` date is honored down to ``23:59:59.999999`` UTC.


class AffidavitAiring(BaseModel):
    """One aired second of one spot for one underwriter."""

    model_config = ConfigDict(extra="forbid")

    spot_id: Slug
    asset_id: Slug
    channel_id: Slug
    # tz-aware UTC, pulled from as_run_log.actual_start.
    aired_at: datetime
    duration_s: int
    # The originating placement, when the as-run row's schedule_item_id matches
    # a row in spot_placements. ``None`` = legacy / manual / ad-hoc as-run row
    # that the compiler did not place but that still aired (DC-3 says include).
    placement_id: Slug | None = None


class UnderwriterAffidavit(BaseModel):
    """Per-underwriter proof-of-airing for a period — billing-ready (DC-3)."""

    model_config = ConfigDict(extra="forbid")

    station_id: Slug
    underwriter: str
    period_start: date
    # Inclusive end-of-period date (the half-open UTC window the service uses
    # internally covers ``[period_start 00:00, period_end + 1d 00:00)``).
    period_end: date
    aired: list[AffidavitAiring]
    total_airings: int
    total_seconds: int


class AffidavitService:
    """Compute a per-underwriter proof-of-airing affidavit over S23 as-run.

    The service is read-only — it never writes the as-run ledger or any
    underwriting row. Both stores are injected so tests can wire SQLite
    in-memory engines with both reporting + underwriting tables created via
    ``civiccast.db.Base.metadata.create_all(engine)``.
    """

    def __init__(
        self,
        underwriting_store: UnderwritingStore,
        reporting_store: ReportingStore,
    ) -> None:
        self._underwriting = underwriting_store
        self._reporting = reporting_store

    def for_underwriter(
        self,
        *,
        station_id: str,
        underwriter: str,
        period_start: date,
        period_end: date,
    ) -> UnderwriterAffidavit:
        """Build the affidavit for one underwriter over an inclusive date period."""
        # 1. Spots for this underwriter. ``asset_to_spot_ids`` is a list-valued
        # dict (T-4): two underwriter spots can legitimately share the same
        # ``asset_id`` (a reused acknowledgment video re-tagged across Q3/Q4).
        # The compiler attributes an as-run row to EVERY matching spot in
        # deterministic ``spot_id`` order so per-spot billing rollups stay
        # accurate when a station reuses ack videos.
        spots = self._underwriting.list_spots(station_id, underwriter=underwriter)
        spot_id_to_spot: dict[str, UnderwritingSpot] = {s.spot_id: s for s in spots}
        asset_to_spot_ids: dict[str, list[str]] = {}
        for s in spots:
            asset_to_spot_ids.setdefault(s.asset_id, []).append(s.spot_id)
        # Stable order on the per-asset list so a re-run produces identical
        # affidavits (deterministic billing artifact).
        for spot_ids in asset_to_spot_ids.values():
            spot_ids.sort()

        # 4. Half-open UTC window from the inclusive [period_start, period_end].
        from_ts = datetime(period_start.year, period_start.month, period_start.day, tzinfo=UTC)
        end_next = period_end + timedelta(days=1)
        to_ts = datetime(end_next.year, end_next.month, end_next.day, tzinfo=UTC)

        # Short-circuit: no spots → no airings can attribute. The half-open
        # window is still well-defined; return an empty affidavit.
        if not spot_id_to_spot:
            return UnderwriterAffidavit(
                station_id=station_id,
                underwriter=underwriter,
                period_start=period_start,
                period_end=period_end,
                aired=[],
                total_airings=0,
                total_seconds=0,
            )

        # 5. Scan as-run for the window, narrowed at the DB to ``source_kind="spot"``
        # (E-5: the affidavit is the billing hot path; without the SQL-side
        # filter the entire month's as-run materializes only to be ~99%
        # discarded in Python).
        as_run = self._reporting.list_as_run(
            station_id, source_kind="spot", from_ts=from_ts, to_ts=to_ts
        )

        # 6. Build the placement lookup over the same window (used to populate
        # placement_id on each surviving as-run row). We do not channel-filter
        # the placement query because a single underwriter can target many
        # channels; the per-row schedule_item_id join is unique.
        placement_by_schedule_item: dict[str, SpotPlacement] = {}
        for placement in self._underwriting.list_placements(from_ts=from_ts, to_ts=to_ts):
            placement_by_schedule_item[placement.schedule_item_id] = placement

        aired: list[AffidavitAiring] = []
        for row in as_run:
            if row.asset_id is None or row.asset_id not in asset_to_spot_ids:
                continue
            # NOTE (Q-6): the prior ``row.channel_id not in channel_set`` filter
            # was removed — the as-run ledger is the source of truth for what
            # aired, and the flight set is prospective policy. A historical
            # airing on a channel the underwriter no longer targets is still
            # billable; the asset → spot map is the strong attribution proof.
            row_placement = (
                placement_by_schedule_item.get(row.schedule_item_id)
                if row.schedule_item_id is not None
                else None
            )
            for spot_id in asset_to_spot_ids[row.asset_id]:
                aired.append(
                    AffidavitAiring(
                        spot_id=spot_id,
                        asset_id=row.asset_id,
                        channel_id=row.channel_id,
                        aired_at=row.actual_start,
                        duration_s=row.duration_s,
                        placement_id=(
                            row_placement.placement_id if row_placement is not None else None
                        ),
                    )
                )

        # 8. Stable sort: aired_at ASC, then spot_id ASC.
        aired.sort(key=lambda a: (a.aired_at, a.spot_id))

        # 9. Totals.
        return UnderwriterAffidavit(
            station_id=station_id,
            underwriter=underwriter,
            period_start=period_start,
            period_end=period_end,
            aired=aired,
            total_airings=len(aired),
            total_seconds=sum(a.duration_s for a in aired),
        )


# --- Export helpers ---------------------------------------------------------
#
# CSV / XML / PDF projections of an UnderwriterAffidavit. These are module-
# level functions (NOT methods on AffidavitService) so they can be reused by
# the router/UI layer without instantiating the service.


def export_affidavit_csv(affidavit: UnderwriterAffidavit) -> str:
    """Render an affidavit as RFC-4180-ish CSV.

    Header row + one row per airing (columns:
    ``aired_at_iso, channel_id, spot_id, asset_id, duration_s, placement_id``);
    plus a trailing summary row that carries the totals and a literal
    ``SUMMARY`` token in the ``aired_at_iso`` column so a downstream parser
    can find it without column-count gymnastics. ``csv.writer`` handles RFC-
    4180 quoting (including the underwriter's commas / quotes).
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        ["aired_at_iso", "channel_id", "spot_id", "asset_id", "duration_s", "placement_id"]
    )
    for airing in affidavit.aired:
        # csv_safe guards every free-text/id cell against spreadsheet formula
        # injection (SEC-3): affidavits are opened in Excel/LibreOffice by
        # underwriters/finance, and `underwriter` in particular is echoed from a
        # caller-supplied query parameter.
        writer.writerow(
            [
                airing.aired_at.isoformat(),
                csv_safe(airing.channel_id),
                csv_safe(airing.spot_id),
                csv_safe(airing.asset_id),
                airing.duration_s,
                csv_safe(airing.placement_id) if airing.placement_id is not None else "",
            ]
        )
    writer.writerow(
        [
            "SUMMARY",
            csv_safe(affidavit.underwriter),
            affidavit.period_start.isoformat(),
            affidavit.period_end.isoformat(),
            affidavit.total_seconds,
            affidavit.total_airings,
        ]
    )
    return buf.getvalue()


def export_affidavit_xml(affidavit: UnderwriterAffidavit) -> str:
    """Render an affidavit as a well-formed XML document.

    Built with :mod:`xml.etree.ElementTree` (never f-strings) so the
    underwriter name's ``<``, ``>``, ``&``, ``"`` are entity-escaped without
    hand-rolling the encoder.
    """
    root = ET.Element(
        "underwriter_affidavit",
        {"underwriter": affidavit.underwriter, "station_id": affidavit.station_id},
    )
    ET.SubElement(
        root,
        "period",
        {
            "start": affidavit.period_start.isoformat(),
            "end": affidavit.period_end.isoformat(),
        },
    )
    airings_el = ET.SubElement(root, "airings")
    for airing in affidavit.aired:
        attrs = {
            "aired_at": airing.aired_at.isoformat(),
            "channel_id": airing.channel_id,
            "spot_id": airing.spot_id,
            "asset_id": airing.asset_id,
            "duration_s": str(airing.duration_s),
        }
        if airing.placement_id is not None:
            attrs["placement_id"] = airing.placement_id
        ET.SubElement(airings_el, "airing", attrs)
    ET.SubElement(
        root,
        "totals",
        {
            "airings": str(affidavit.total_airings),
            "seconds": str(affidavit.total_seconds),
        },
    )
    return ET.tostring(root, encoding="unicode")


def export_affidavit_pdf(affidavit: UnderwriterAffidavit) -> bytes:
    """Render an affidavit as a simple deterministic PDF (one or more pages).

    Uses :mod:`reportlab` (a pinned project dep — see ``pyproject.toml``).
    The output is NOT a PDF/A-3 signed record (that is :mod:`civiccast.records.pdfa`
    for the SummaryDraft path); this is an underwriter-facing billing artifact,
    so a plain PDF body is the contract.

    The returned bytes always start with ``b"%PDF"`` so a Content-Type sniff
    + a fast smoke test ("did the renderer produce a PDF?") work without
    parsing.
    """
    # Local import so the module remains importable in environments where
    # reportlab is not installed (e.g. lint-only CI shards) — the PDF path
    # is opt-in and the import error surfaces here at call time.
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=letter, pageCompression=0, invariant=1)
    pdf.setTitle(f"CivicCast underwriter affidavit — {affidavit.underwriter}")
    pdf.setAuthor("CivicCast")
    pdf.setSubject("Underwriting proof-of-airing affidavit")

    _page_width, page_height = letter
    left_margin = 54.0
    top_margin = page_height - 54.0
    line_height = 14.0
    body_bottom = 72.0

    def _start_page(page_y: float) -> float:
        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawString(left_margin, page_y, "CivicCast underwriter affidavit")
        page_y -= line_height + 4
        pdf.setFont("Helvetica", 10)
        pdf.drawString(left_margin, page_y, f"Station: {affidavit.station_id}")
        page_y -= line_height
        pdf.drawString(left_margin, page_y, f"Underwriter: {affidavit.underwriter}")
        page_y -= line_height
        pdf.drawString(
            left_margin,
            page_y,
            f"Period: {affidavit.period_start.isoformat()} → "
            f"{affidavit.period_end.isoformat()} (inclusive)",
        )
        page_y -= line_height
        pdf.drawString(
            left_margin,
            page_y,
            f"Totals: {affidavit.total_airings} airing(s) / {affidavit.total_seconds} second(s)",
        )
        page_y -= line_height + 6
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(
            left_margin,
            page_y,
            "aired_at_iso          channel_id   spot_id            duration_s  placement_id",
        )
        page_y -= line_height
        pdf.setFont("Helvetica", 9)
        return page_y

    y = _start_page(top_margin)
    for airing in affidavit.aired:
        if y < body_bottom:
            pdf.showPage()
            y = _start_page(top_margin)
        line = (
            f"{airing.aired_at.isoformat():<22}"
            f"{airing.channel_id:<13}"
            f"{airing.spot_id:<19}"
            f"{airing.duration_s:<11}"
            f"{airing.placement_id or '-'}"
        )
        pdf.drawString(left_margin, y, line)
        y -= line_height
    pdf.showPage()
    pdf.save()
    return buf.getvalue()


__all__ = [
    "AffidavitAiring",
    "AffidavitService",
    "CandidateBreakSlot",
    "CompileResult",
    "DaypartResolver",
    "SkipReason",
    "SkippedSlot",
    "TraffickingCompiler",
    "UnderwriterAffidavit",
    "export_affidavit_csv",
    "export_affidavit_pdf",
    "export_affidavit_xml",
]
