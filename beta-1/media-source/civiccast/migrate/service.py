# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Dry-run diff planning, apply, and rollback — the core of 0.4.0 migration.

``apply`` writes into the REAL ``civiccast.schedule`` stores, not a parallel
database:

* Shows become :class:`civiccast.schedule.models.Asset` rows in
  ``pending_ingest`` state with ``manifest_url=None`` and ``file_path`` set
  to the source pointer — the exact shape an uploaded-but-not-yet-packaged
  asset has today (see ``Asset.from_upload``). This module constructs the
  row directly rather than going through
  :meth:`civiccast.schedule.store.PostgresAssetStore.create`, because that
  method requires an already-packaged HLS ``manifest_url`` an import does
  not have (0.4.0 imports metadata + a media pointer, not the media itself
  — see the module docstring in ``civiccast/migrate/__init__.py``).
* Schedule items are created through the REAL
  :class:`civiccast.schedule.store.PostgresScheduleStore.create` — the same
  path the staff schedule API uses, so the existing asset-exists check and
  (on Postgres) the overlap-detecting EXCLUDE constraint both apply to
  imported rows exactly as they would to an operator-created one.

Every row an apply call creates is recorded in the provenance ledger
(:class:`civiccast.migrate.store.MigrationStore`) BEFORE the response is
returned, so :meth:`MigrationService.rollback` can delete EXACTLY those rows
— never a broader "anything that looks like this batch" scan.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from civiccast.migrate.models import (
    ApplyOutcome,
    ImportBatch,
    ImportPlan,
    NormalizedInventory,
    PlanConflict,
    PlanScheduleItem,
    PlanShow,
    PlanSkip,
    new_id,
)
from civiccast.migrate.store import BatchNotFoundError, MigrationStore
from civiccast.schedule.models import ASSET_STATE_PENDING, Asset, ScheduleItem, ScheduleItemCreate
from civiccast.schedule.store import (
    AssetNotFoundError,
    PostgresScheduleStore,
    ScheduleConflictError,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]

# 14 days, mirroring ``schedule_items_duration_matches_mode`` (QA-010).
_MAX_SCHEDULE_DURATION_SECONDS = 1_209_600


class MigrationServiceError(RuntimeError):
    """Base error for the migration service."""


def _asset_id(source_system: str, source_ref: str) -> str:
    """Deterministic CivicCast asset id for one imported show.

    Same ``(source_system, source_ref)`` always maps to the same
    ``asset_id`` so re-running a dry-run against the same source proposes
    the same target and correctly reports "already imported" on a re-run.
    """
    safe_ref = "".join(ch for ch in source_ref.lower() if ch.isalnum() or ch == "-") or "ref"
    return f"{source_system}-show-{safe_ref}"[:64]


def _channel_id(source_system: str, channel_ref: str | None) -> str:
    safe_ref = (
        "".join(ch for ch in (channel_ref or "default").lower() if ch.isalnum() or ch == "-")
        or "default"
    )
    return f"{source_system}-ch-{safe_ref}"[:64]


def _windows_overlap(
    start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime
) -> bool:
    return start_a < end_b and start_b < end_a


class MigrationService:
    """Orchestrates dry-run / apply / rollback over the real schedule stores
    plus this module's own provenance ledger."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._ledger = MigrationStore(session_factory)
        self._schedule_store = PostgresScheduleStore(session_factory)

    # ------------------------------------------------------------------
    # Dry run
    # ------------------------------------------------------------------

    def dry_run(self, inventory: NormalizedInventory) -> ImportPlan:
        plan = ImportPlan(source_system=inventory.source_system)

        with self._session_factory() as session:
            resolved_show_ids: dict[str, str] = {}
            seen_source_refs: set[str] = set()
            claimed_asset_ids: dict[str, str] = {}

            for show in inventory.shows:
                if show.source_ref in seen_source_refs:
                    plan.skipped.append(
                        PlanSkip(
                            kind="show",
                            source_ref=show.source_ref,
                            reason="Duplicate source_ref within this export.",
                        )
                    )
                    continue
                seen_source_refs.add(show.source_ref)

                asset_id = _asset_id(inventory.source_system, show.source_ref)
                # Two distinct source_refs (e.g. "Show 1" and "show_1") can
                # strip down to the same safe_ref and collide on one derived
                # asset_id. Flag it here instead of silently letting both
                # into shows_to_create -- apply() would insert the first and
                # IntegrityError the second with no warning from dry_run.
                colliding_source_ref = claimed_asset_ids.get(asset_id)
                if colliding_source_ref is not None:
                    plan.conflicts.append(
                        PlanConflict(
                            kind="show",
                            source_ref=show.source_ref,
                            reason=(
                                f"Derived asset id {asset_id!r} collides with show "
                                f"{colliding_source_ref!r} in this same export "
                                "(distinct source_refs that normalize to the same id)."
                            ),
                        )
                    )
                    continue
                claimed_asset_ids[asset_id] = show.source_ref
                resolved_show_ids[show.source_ref] = asset_id
                existing_asset = session.get(Asset, asset_id)
                if existing_asset is not None:
                    plan.conflicts.append(
                        PlanConflict(
                            kind="show",
                            source_ref=show.source_ref,
                            reason=(
                                f"Already imported as asset {asset_id!r} "
                                "(re-running a prior import, or a real title/id collision)."
                            ),
                        )
                    )
                    continue
                # Exact case-insensitive comparison -- NOT ilike(), which
                # treats "%"/"_" in show.title as SQL wildcards rather than
                # literal characters a file-based adapter's filename-derived
                # title routinely contains (e.g. "council_mtg_2026").
                title_collision = session.execute(
                    select(Asset.asset_id).where(func.lower(Asset.title) == show.title.lower())
                ).scalar_one_or_none()
                if title_collision is not None:
                    plan.conflicts.append(
                        PlanConflict(
                            kind="show",
                            source_ref=show.source_ref,
                            reason=(
                                f"Title {show.title!r} collides with existing asset "
                                f"{title_collision!r}."
                            ),
                        )
                    )
                    continue
                plan.shows_to_create.append(
                    PlanShow(
                        source_ref=show.source_ref,
                        asset_id=asset_id,
                        title=show.title,
                        description=show.description,
                        category=show.category,
                        duration_seconds=show.duration_seconds,
                        media_ref=show.media_ref,
                    )
                )

            planned_windows: dict[str, list[tuple[datetime, datetime]]] = {}

            for item in inventory.schedule_items:
                schedule_asset_id = resolved_show_ids.get(item.show_source_ref)
                if schedule_asset_id is None:
                    plan.skipped.append(
                        PlanSkip(
                            kind="schedule_item",
                            source_ref=item.source_ref,
                            reason=(
                                f"References show {item.show_source_ref!r}, which is not "
                                "in this export."
                            ),
                        )
                    )
                    continue
                duration = item.duration_seconds
                if not duration or duration <= 0 or duration > _MAX_SCHEDULE_DURATION_SECONDS:
                    plan.skipped.append(
                        PlanSkip(
                            kind="schedule_item",
                            source_ref=item.source_ref,
                            reason=f"No usable duration ({duration!r}).",
                        )
                    )
                    continue

                channel_id = _channel_id(inventory.source_system, item.channel_ref)
                start = item.scheduled_at
                end = start + timedelta(seconds=duration)

                conflict_row = session.execute(
                    select(ScheduleItem.id).where(
                        ScheduleItem.channel_id == channel_id,
                        ScheduleItem.state == "scheduled",
                        ScheduleItem.scheduled_at < end,
                        ScheduleItem.scheduled_at_end > start,
                    )
                ).scalar_one_or_none()
                if conflict_row is not None:
                    plan.conflicts.append(
                        PlanConflict(
                            kind="schedule_item",
                            source_ref=item.source_ref,
                            reason=(
                                f"Time collision with existing schedule item {conflict_row} "
                                f"on channel {channel_id!r}."
                            ),
                        )
                    )
                    continue

                own_conflict = any(
                    _windows_overlap(start, end, other_start, other_end)
                    for other_start, other_end in planned_windows.get(channel_id, [])
                )
                if own_conflict:
                    plan.conflicts.append(
                        PlanConflict(
                            kind="schedule_item",
                            source_ref=item.source_ref,
                            reason=(
                                f"Time collision with another item in this same import "
                                f"on channel {channel_id!r}."
                            ),
                        )
                    )
                    continue

                planned_windows.setdefault(channel_id, []).append((start, end))
                plan.schedule_items_to_create.append(
                    PlanScheduleItem(
                        source_ref=item.source_ref,
                        show_source_ref=item.show_source_ref,
                        asset_id=schedule_asset_id,
                        channel_id=channel_id,
                        scheduled_at=start,
                        duration_seconds=duration,
                    )
                )

        return plan

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def apply(self, plan: ImportPlan) -> ImportBatch:
        batch_id = new_id()
        self._ledger.create_batch(batch_id, plan.source_system)
        failures: list[ApplyOutcome] = []

        for show in plan.shows_to_create:
            with self._session_factory() as session:
                row = Asset(
                    asset_id=show.asset_id,
                    title=show.title,
                    description=show.description,
                    meeting_body=show.category,
                    manifest_url=None,
                    file_path=show.media_ref,
                    duration_seconds=show.duration_seconds,
                    state=ASSET_STATE_PENDING,
                )
                session.add(row)
                try:
                    session.commit()
                except IntegrityError as exc:
                    session.rollback()
                    failures.append(
                        ApplyOutcome(
                            kind="show",
                            source_ref=show.source_ref,
                            reason=f"Insert failed: {exc.orig if exc.orig else exc}",
                        )
                    )
                    continue
            try:
                self._ledger.add_item(
                    import_batch_id=batch_id,
                    entity_type="asset",
                    entity_id=show.asset_id,
                    source_ref=show.source_ref,
                )
            except Exception as exc:
                # The Asset row above is already committed. If it can't be
                # ledgered, rollback() can never find it -- delete it right
                # back out so it doesn't become a permanent, un-rollbackable
                # orphan, and surface this as a recorded failure instead of
                # letting the exception escape apply().
                with self._session_factory() as cleanup_session:
                    orphan = cleanup_session.get(Asset, show.asset_id)
                    if orphan is not None:
                        cleanup_session.delete(orphan)
                        cleanup_session.commit()
                failures.append(
                    ApplyOutcome(
                        kind="show",
                        source_ref=show.source_ref,
                        reason=f"Created but failed to record in provenance ledger: {exc}",
                    )
                )

        for item in plan.schedule_items_to_create:
            try:
                created = self._schedule_store.create(
                    ScheduleItemCreate(
                        asset_id=item.asset_id,
                        channel_id=item.channel_id,
                        mode="premiere",
                        scheduled_at=item.scheduled_at,
                        duration_seconds=item.duration_seconds,
                        notes=(
                            f"Imported from {plan.source_system} (source_ref={item.source_ref})"
                        ),
                    )
                )
            except (AssetNotFoundError, ScheduleConflictError) as exc:
                failures.append(
                    ApplyOutcome(kind="schedule_item", source_ref=item.source_ref, reason=str(exc))
                )
                continue
            try:
                self._ledger.add_item(
                    import_batch_id=batch_id,
                    entity_type="schedule_item",
                    entity_id=str(created.id),
                    source_ref=item.source_ref,
                )
            except Exception as exc:
                # Same reasoning as the show loop above: the ScheduleItem
                # row is already committed, so an un-ledgered row would be
                # a permanent orphan rollback() can never find.
                with self._session_factory() as cleanup_session:
                    orphan_item = cleanup_session.get(ScheduleItem, created.id)
                    if orphan_item is not None:
                        cleanup_session.delete(orphan_item)
                        cleanup_session.commit()
                failures.append(
                    ApplyOutcome(
                        kind="schedule_item",
                        source_ref=item.source_ref,
                        reason=f"Created but failed to record in provenance ledger: {exc}",
                    )
                )

        batch = self._ledger.get_batch(batch_id)
        assert batch is not None  # just created above
        return batch.model_copy(update={"apply_failures": failures})

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    def rollback(self, import_batch_id: str) -> ImportBatch:
        batch = self._ledger.get_batch(import_batch_id)
        if batch is None:
            raise BatchNotFoundError(f"Import batch {import_batch_id!r} not found.")
        items = self._ledger.list_items(import_batch_id)

        with self._session_factory() as session:
            # Schedule items first (no DB FK to assets, but this order keeps
            # the deletion sequence the mirror image of creation).
            for entry in items:
                if entry.entity_type == "schedule_item":
                    schedule_row = session.get(ScheduleItem, uuid.UUID(entry.entity_id))
                    if schedule_row is not None:
                        session.delete(schedule_row)
            session.commit()
            for entry in items:
                if entry.entity_type == "asset":
                    # schedule_items.asset_id has no FK (see this module's
                    # docstring), so a schedule_item created OUTSIDE this
                    # batch's ledger -- e.g. staff scheduling a rerun via
                    # the normal API shortly after apply -- can reference
                    # this asset. This batch's own ledgered schedule_items
                    # were already deleted above, so any row still
                    # referencing it now is external: skip deleting the
                    # asset rather than leaving that row's asset lookup
                    # broken.
                    still_referenced = (
                        session.execute(
                            select(ScheduleItem.id).where(ScheduleItem.asset_id == entry.entity_id)
                        ).first()
                        is not None
                    )
                    if still_referenced:
                        continue
                    asset_row = session.get(Asset, entry.entity_id)
                    if asset_row is not None:
                        session.delete(asset_row)
            session.commit()

        return self._ledger.mark_rolled_back(import_batch_id)

    # ------------------------------------------------------------------
    # Batch history
    # ------------------------------------------------------------------

    def list_batches(self) -> list[ImportBatch]:
        return self._ledger.list_batches()


__all__ = ["MigrationService", "MigrationServiceError"]
