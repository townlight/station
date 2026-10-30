# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Commit-to-Air dry-run service (CivicCast 3.0 — S4 slice 2).

The dry-run is the read-only half of the Commit-to-Air gate: given a
materialized occurrence's schedule item, it actively checks whether the item
*can* air and reports everything an operator needs to decide. It mutates
nothing and dispatches nothing — that is the commit step (slice 3+).

It detects three classes of problem (S4 §6):

1. **Missing / unplayable media** — the asset is absent, not in an airable
   state (``validated`` / ``recorded``), has no media file on disk, or has no
   known duration. Any of these fails the dry-run.
2. **Schedule conflicts** — other scheduled premiere items on the same channel
   whose time range overlaps the proposed air window. Any conflict fails the
   dry-run.
3. **Gaps** — dead air between the end of the immediately-prior item and the
   proposed start, when that gap exceeds 1.0s. Informational only: a gap is
   surfaced to the operator but does **not** fail the dry-run.

The only raised error is :class:`ScheduleItemNotFoundError` (the endpoint maps
it to 404). Every other outcome is reported as a :class:`CommitToAirPlan` with
``dry_run_passed`` and a human-readable reason, so the operator UI can explain
*why* an item cannot air rather than just refusing.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from civiccast.egress.dispatcher import PlayoutDispatcher
from civiccast.schedule.commit_models import (
    DISPATCH_STATUS_CANCELLED,
    DISPATCH_STATUS_ERROR,
    DISPATCH_STATUS_PENDING,
    DISPATCH_STATUS_QUEUED,
    CommitToAirPlan,
    CommitToAirReport,
    PlayoutEventPlan,
    ScheduleConflict,
)
from civiccast.schedule.models import (
    ASSET_STATE_RECORDED,
    ASSET_STATE_VALIDATED,
    SCHEDULE_MODE_PREMIERE,
    SCHEDULE_STATE_PUBLISHED,
    SCHEDULE_STATE_SCHEDULED,
    ScheduleItemResponse,
    StaffAssetRow,
)
from civiccast.schedule.store import (
    PostgresAssetStore,
    PostgresScheduleStore,
    ScheduleItemNotFoundError,
)

_LOG = logging.getLogger(__name__)

# Asset states from which an item may be committed to air. Mirrors the
# upload/finalization state machine (schedule.models): ``validated`` =
# uploaded + ffprobe-checked; ``recorded`` = born from a finalized live
# session. Anything else (pending_ingest / ingesting / rejected) cannot air.
_AIRABLE_ASSET_STATES: tuple[str, ...] = (ASSET_STATE_VALIDATED, ASSET_STATE_RECORDED)

# Gaps at or below this many seconds are treated as back-to-back, not dead air
# (S4 §6). One second absorbs the sub-second rounding between adjacent items.
_GAP_THRESHOLD_SECONDS = 1.0


def _default_token() -> str:
    return secrets.token_urlsafe(12)


def _default_clock() -> datetime:
    return datetime.now(UTC)


class CommitDryRunService:
    """Builds a :class:`CommitToAirPlan` for a proposed commit.

    Reads through the two stores' public surfaces (no session of its own): the
    :class:`PostgresScheduleStore` for the schedule item + channel scan, and the
    :class:`PostgresAssetStore` for asset playability (``get_staff_row``).
    ``clock`` and ``token_factory`` are injectable so tests get deterministic
    ``created_at`` / ``plan_id`` without freezing time.
    """

    def __init__(
        self,
        schedule_store: PostgresScheduleStore,
        asset_store: PostgresAssetStore,
        *,
        clock: Callable[[], datetime] = _default_clock,
        token_factory: Callable[[], str] = _default_token,
    ) -> None:
        self._schedule_store = schedule_store
        self._asset_store = asset_store
        self._clock = clock
        self._token_factory = token_factory

    def prepare_commit(
        self,
        *,
        channel_id: str,
        occurrence_id: str,
        schedule_item_id: object,
        operator_id: str | None = None,
    ) -> CommitToAirPlan:
        """Run the dry-run for one occurrence's schedule item.

        ``channel_id`` is accepted for API symmetry but the schedule item's own
        ``channel_id`` is authoritative for conflict/gap detection and for the
        returned plan — an item can only conflict with items on the channel it
        actually belongs to. ``occurrence_id`` is provenance carried through to
        the plan (and later the persisted report).

        Raises:
            ScheduleItemNotFoundError: no schedule item matches
                ``schedule_item_id`` (the endpoint maps this to 404).
        """
        item = self._schedule_store.get(schedule_item_id)
        if item is None:
            raise ScheduleItemNotFoundError(schedule_item_id)

        asset = self._asset_store.get_staff_row(item.asset_id)
        passed, missing_detail = self._check_playability(item, asset)

        # Legacy finding fix: the target item's OWN state must be checked too
        # (the conflict scan below only looks at OTHER items). Without this, a
        # schedule item cancelled by a separate workflow between an operator's
        # prepare-commit and commit calls would still "pass" — asset
        # playability and time conflicts are unaffected by cancellation —
        # letting commit() persist a queued report for an item that will
        # never actually air (egress/source_plan.py only resolves items whose
        # state is still SCHEDULE_STATE_SCHEDULED). Treat any non-scheduled
        # state the same as an unplayable asset: fail the dry run.
        if item.state != SCHEDULE_STATE_SCHEDULED:
            passed = False
            if missing_detail is None:
                missing_detail = (
                    f"Schedule item {item.id} is {item.state!r}, not "
                    f"{SCHEDULE_STATE_SCHEDULED!r}; it cannot be committed to air."
                )

        proposed_duration = item.duration_seconds
        conflicts: list[ScheduleConflict] = []
        gaps: list[PlayoutEventPlan] = []
        if proposed_duration is None:
            # An item with no air duration (an embargo entry) is not airable.
            # Conflict/gap detection needs a time range, so it is skipped.
            passed = False
            if missing_detail is None:
                missing_detail = (
                    "This schedule item has no air duration (embargo entries "
                    "publish at a single moment and cannot be committed to air)."
                )
        else:
            conflicts, gaps = self._detect_conflicts_and_gaps(item, proposed_duration)
            if conflicts:
                passed = False

        title = asset.title if asset is not None else (item.asset_title or item.asset_id)

        return CommitToAirPlan(
            plan_id="ctap_" + self._token_factory(),
            channel_id=item.channel_id,
            occurrence_id=occurrence_id,
            schedule_item_id=str(item.id),
            asset_id=item.asset_id,
            title=title,
            scheduled_at=item.scheduled_at,
            duration_seconds=proposed_duration if proposed_duration is not None else 0,
            dry_run_passed=passed,
            conflicts_detected=conflicts,
            missing_media_detail=missing_detail,
            gaps_detected=gaps,
            created_at=self._clock(),
            operator_id=operator_id,
        )

    @staticmethod
    def _check_playability(
        item: ScheduleItemResponse,
        asset: StaffAssetRow | None,
    ) -> tuple[bool, str | None]:
        """Return ``(passed, missing_detail)`` for the asset's airability.

        Checks run in the spec's order; the first failure wins the detail so
        the operator sees the most fundamental reason.
        """
        if asset is None:
            return False, f"Asset {item.asset_id!r} does not exist."
        if asset.state not in _AIRABLE_ASSET_STATES:
            return False, (
                f"Asset {item.asset_id!r} is {asset.state!r}; only validated or "
                f"recorded assets can air."
            )
        if not asset.file_path:
            return False, f"Asset {item.asset_id!r} has no media file on disk yet."
        if asset.duration_seconds is None:
            return False, f"Asset {item.asset_id!r} has no known duration."
        return True, None

    def _detect_conflicts_and_gaps(
        self,
        item: ScheduleItemResponse,
        proposed_duration: int,
    ) -> tuple[list[ScheduleConflict], list[PlayoutEventPlan]]:
        """Detect overlapping premiere items (conflicts) and prior dead air
        (gaps) on the item's channel.

        ``scheduled`` and ``published`` premiere items are considered —
        a published item is an already-approved, airing program and
        occupies real airtime just like a scheduled one (0071_published_
        blocks_overlap); cancelled items and single-moment embargo entries
        never compete for air time (mirrors ``PostgresScheduleStore.
        _find_conflicting``'s default filter). The item under test is
        excluded from its own channel scan.
        """
        proposed_start = item.scheduled_at
        proposed_end = proposed_start + timedelta(seconds=proposed_duration)

        conflicts: list[ScheduleConflict] = []
        latest_prior_end: datetime | None = None
        latest_prior: ScheduleItemResponse | None = None

        for other in self._schedule_store.list(
            channel_id=item.channel_id,
            states=(SCHEDULE_STATE_SCHEDULED, SCHEDULE_STATE_PUBLISHED),
        ):
            if other.id == item.id:
                continue
            if other.mode != SCHEDULE_MODE_PREMIERE or other.duration_seconds is None:
                continue
            other_start = other.scheduled_at
            other_end = other_start + timedelta(seconds=other.duration_seconds)

            if other_start < proposed_end and other_end > proposed_start:
                # Half-open overlap of [other_start, other_end) and
                # [proposed_start, proposed_end).
                overlap = min(other_end, proposed_end) - max(other_start, proposed_start)
                conflicts.append(
                    ScheduleConflict(
                        existing_schedule_item_id=str(other.id),
                        existing_asset_id=other.asset_id,
                        existing_asset_title=other.asset_title or other.asset_id,
                        existing_scheduled_at=other_start,
                        existing_duration_seconds=other.duration_seconds,
                        proposed_scheduled_at=proposed_start,
                        proposed_duration_seconds=proposed_duration,
                        overlap_seconds=int(overlap.total_seconds()),
                    )
                )
            elif other_end <= proposed_start:
                # A strictly-prior item: track the one that ends latest so the
                # gap is measured against the nearest preceding program.
                if latest_prior_end is None or other_end > latest_prior_end:
                    latest_prior_end = other_end
                    latest_prior = other

        gaps: list[PlayoutEventPlan] = []
        if latest_prior_end is not None:
            gap_seconds = (proposed_start - latest_prior_end).total_seconds()
            if gap_seconds > _GAP_THRESHOLD_SECONDS:
                prior_label = (
                    latest_prior.asset_title or latest_prior.asset_id
                    if latest_prior is not None
                    else "the previous program"
                )
                gaps.append(
                    PlayoutEventPlan(
                        kind="gap",
                        starts_at=latest_prior_end,
                        ends_at=proposed_start,
                        duration_seconds=gap_seconds,
                        label=f"Dead air after {prior_label}",
                    )
                )
        return conflicts, gaps


class CommitConflictError(RuntimeError):
    """Raised when the commit-time re-run of the dry-run fails.

    Something changed between the operator's prepare-commit review and the
    commit itself — a conflicting item was scheduled, or the asset became
    unplayable. Carries the failing :class:`CommitToAirPlan` so the API layer
    can choose its status code (409 when ``conflicts_detected`` is non-empty,
    422 when only ``missing_media_detail`` is set).
    """

    def __init__(self, plan: CommitToAirPlan) -> None:
        self.plan = plan
        detail = (
            "A schedule conflict appeared since you reviewed this item."
            if plan.conflicts_detected
            else (plan.missing_media_detail or "This item can no longer be aired.")
        )
        super().__init__(detail)


class CommitReportNotFoundError(KeyError):
    """Raised when a rollback / detail lookup names an unknown report_id.

    The endpoint maps this to HTTP 404. Subclasses ``KeyError`` to match the
    schedule module's not-found convention (see ``ScheduleItemNotFoundError``).
    """

    def __init__(self, report_id: str) -> None:
        self.report_id = report_id
        super().__init__(f"Commit-to-air report not found: {report_id!r}")


class CommitService:
    """Orchestrates the Commit-to-Air approval (S4 §6 commit workflow).

    Composes the dry-run (race check), the report store (durable audit), and
    the :class:`PlayoutDispatcher` (engine nudge). ``clock`` and
    ``report_id_factory`` are injectable for deterministic tests.
    """

    def __init__(
        self,
        dry_run: CommitDryRunService,
        schedule_store: PostgresScheduleStore,
        dispatcher: PlayoutDispatcher,
        *,
        clock: Callable[[], datetime] = _default_clock,
        report_id_factory: Callable[[], str] = _default_token,
    ) -> None:
        self._dry_run = dry_run
        self._schedule_store = schedule_store
        self._dispatcher = dispatcher
        self._clock = clock
        self._report_id_factory = report_id_factory

    def prepare(
        self,
        *,
        channel_id: str,
        occurrence_id: str,
        schedule_item_id: object,
        operator_id: str | None = None,
    ) -> CommitToAirPlan:
        """Run the read-only dry-run for the prepare-commit endpoint.

        Delegates to the composed :class:`CommitDryRunService` so the API has a
        single service dependency for both the preview and the commit.
        """
        return self._dry_run.prepare_commit(
            channel_id=channel_id,
            occurrence_id=occurrence_id,
            schedule_item_id=schedule_item_id,
            operator_id=operator_id,
        )

    def commit(
        self,
        *,
        channel_id: str,
        occurrence_id: str,
        schedule_item_id: object,
        operator_id: str,
        operator_notes: str | None = None,
    ) -> CommitToAirReport:
        """Approve and air a committed occurrence; persist the audit report.

        Steps (S4 §6): (1) re-run the dry-run as a race check; (2) if it now
        fails, raise :class:`CommitConflictError` and dispatch nothing — no
        report is written; (3) atomically flip the item scheduled -> published
        AND persist the ``pending`` report in one transaction
        (``store.commit_to_air``): this UPDATE ... WHERE state='scheduled' is
        the true-concurrency guard (a lost race matches 0 rows → the store
        writes nothing and returns False → raise :class:`CommitConflictError`,
        dispatching nothing), and sharing the transaction guarantees the item is
        never published without a durable approval report; (4) dispatch the
        engine nudge; (5) update the report to ``queued`` on success or
        ``error`` (with a non-leaking detail) on dispatch failure. The stored
        report is returned in every non-conflict case.

        Raises:
            ScheduleItemNotFoundError: the schedule item does not exist (404).
            CommitConflictError: the re-run dry-run failed (409 / 422).
        """
        plan = self._dry_run.prepare_commit(
            channel_id=channel_id,
            occurrence_id=occurrence_id,
            schedule_item_id=schedule_item_id,
            operator_id=operator_id,
        )
        if not plan.dry_run_passed:
            raise CommitConflictError(plan)

        now = self._clock()
        report = CommitToAirReport(
            report_id="ctar_" + self._report_id_factory(),
            channel_id=plan.channel_id,
            occurrence_id=plan.occurrence_id,
            schedule_item_id=plan.schedule_item_id,
            asset_id=plan.asset_id,
            title=plan.title,
            scheduled_at=plan.scheduled_at,
            duration_seconds=plan.duration_seconds,
            approved_by_operator_id=operator_id,
            approved_at=now,
            conflicts_found=len(plan.conflicts_detected),
            gaps_found=len(plan.gaps_detected),
            dispatch_status=DISPATCH_STATUS_PENDING,
            operator_notes=operator_notes,
            created_at=now,
            updated_at=now,
        )
        # Commit-to-Air gate + concurrency guard, done ATOMICALLY: the store
        # flips the item scheduled -> published AND persists this pending report
        # in a single transaction. The UPDATE ... WHERE state='scheduled'
        # matches the row for exactly one of any concurrent commits, so a lost
        # race (published is False) persists nothing and dispatches nothing —
        # while a win can never leave the item published without a durable
        # approval report (that is why the flip and the report share one
        # transaction rather than being two separate commits).
        published = self._schedule_store.commit_to_air(uuid.UUID(plan.schedule_item_id), report)
        if not published:
            # Another commit won (or the item left 'scheduled' since the
            # dry-run). Same end-state as a sequential recommit: refuse with a
            # conflict, write no report, dispatch nothing.
            raise CommitConflictError(
                plan.model_copy(
                    update={
                        "dry_run_passed": False,
                        "missing_media_detail": (
                            "This item was committed to air by another request; "
                            "it is no longer awaiting approval."
                        ),
                    }
                )
            )

        # The item is now published with a durable pending report. Dispatch is
        # an external nudge AFTER the durable commit: a dispatch failure leaves
        # the item approved (published + pending/error report), and only
        # rollback un-publishes it.
        try:
            outcome = self._dispatcher.dispatch(channel_id=plan.channel_id, issued_by=operator_id)
        except Exception as exc:  # any dispatch failure → durable error report
            # Record a non-leaking detail (exception TYPE only, never its
            # message, which could carry a DB DSN or other secret); the full
            # exception goes to the server log for operators/admins.
            _LOG.exception(
                "Commit-to-air dispatch failed for report %s on channel %s.",
                report.report_id,
                plan.channel_id,
            )
            errored = report.model_copy(
                update={
                    "dispatch_status": DISPATCH_STATUS_ERROR,
                    "dispatch_error_detail": (
                        f"Could not hand the program to the broadcast engine "
                        f"({type(exc).__name__}). The approval is recorded but the "
                        f"item is not on air yet — retry, or check the egress daemon."
                    ),
                }
            )
            return self._schedule_store.upsert_commit_report(errored)

        queued = report.model_copy(
            update={
                "dispatch_status": DISPATCH_STATUS_QUEUED,
                "dispatch_timestamp": outcome.dispatched_at,
            }
        )
        return self._schedule_store.upsert_commit_report(queued)

    def rollback(
        self,
        *,
        report_id: str,
        reason: str,
        operator_id: str,
    ) -> CommitToAirReport:
        """Undo a committed airing: cancel the schedule item, hand back to the
        engine, and mark the report ``cancelled`` with the operator's reason.

        Steps (S4 §6 rollback): (1) load the report (404 if absent); (2) cancel
        the linked schedule item so the resolver stops airing it — tolerating
        an already-gone item; (3) nudge the engine (``reload``) so it
        re-resolves to slate / the next program; (4) persist the report as
        ``cancelled`` with ``rollback_reason`` + ``rolled_back_at``.

        The schedule item is cancelled before the engine nudge, so a failed
        nudge does not make ``cancelled`` untrue — the item will not air either
        way (the automation loop re-resolves on its own cycle); the nudge just
        makes the handback prompt. A nudge failure is logged, not raised.

        Raises:
            CommitReportNotFoundError: no report matches ``report_id`` (404).
        """
        report = self._schedule_store.get_commit_report(report_id)
        if report is None:
            raise CommitReportNotFoundError(report_id)

        try:
            self._schedule_store.cancel(uuid.UUID(report.schedule_item_id))
        except ScheduleItemNotFoundError:
            # The item was already removed — the airing is undone regardless.
            _LOG.info(
                "Rollback of report %s: schedule item %s already gone; "
                "marking the report cancelled anyway.",
                report_id,
                report.schedule_item_id,
            )
        except ValueError:
            # A non-UUID schedule_item_id (legacy/malformed) — nothing to cancel
            # at the schedule layer; proceed with the report-level rollback.
            _LOG.warning(
                "Rollback of report %s: schedule_item_id %r is not a UUID; "
                "skipping schedule cancel.",
                report_id,
                report.schedule_item_id,
            )

        try:
            self._dispatcher.dispatch(channel_id=report.channel_id, issued_by=operator_id)
        except Exception:
            # Best-effort handback: the item is already cancelled, so the engine
            # will drop it on its next resolve regardless. Log, don't fail.
            _LOG.exception(
                "Handback dispatch failed during rollback of report %s on "
                "channel %s; the schedule item is cancelled regardless.",
                report_id,
                report.channel_id,
            )

        now = self._clock()
        cancelled = report.model_copy(
            update={
                "dispatch_status": DISPATCH_STATUS_CANCELLED,
                "rollback_reason": reason,
                "rolled_back_at": now,
                "updated_at": now,
            }
        )
        return self._schedule_store.upsert_commit_report(cancelled)

    def list_commits(
        self,
        *,
        channel_id: str,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        limit: int = 50,
    ) -> list[CommitToAirReport]:
        """List a channel's commit reports (newest commit first) for the API."""
        return self._schedule_store.list_commit_reports(
            channel_id=channel_id, start_at=start_at, end_at=end_at, limit=limit
        )

    def get_commit(self, report_id: str) -> CommitToAirReport | None:
        """Return one commit report by id, or None if absent."""
        return self._schedule_store.get_commit_report(report_id)
