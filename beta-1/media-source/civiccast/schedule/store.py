# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""PostgresAssetStore — SQLAlchemy-backed AssetStore implementation.

Per Director Decisions 2 + 5:

* Implements :class:`civiccast.vod.store.AssetStore` Protocol unchanged
  (the Protocol stays at its v0.2 location; this module imports from it).
* No I/O at construction. The first DB call happens on the first
  :meth:`get` invocation. Mirrors task 1a's lazy-engine posture so the
  app factory can register the store in ``dependency_overrides`` without
  triggering a connectivity probe at import.

Sprint 0.3 task 2 adds :meth:`list` and :meth:`create`. Per Decision 3,
``create`` translates SA's :class:`IntegrityError` into the domain-level
:class:`civiccast.vod.store.AssetAlreadyExistsError` so the router (and
any other caller) sees one consistent exception type across both store
implementations. ``rollback()`` is invoked explicitly on the IntegrityError
path so the session is reusable for any subsequent operation.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from civiccast.schedule.commit_models import CommitToAirReport, CommitToAirReportRow
from civiccast.schedule.ingest import FfprobeResult
from civiccast.schedule.media_lifecycle_models import MediaLifecycleAuditEntry
from civiccast.schedule.models import (
    _SCHEDULE_STATES,
    ASSET_STATE_VALIDATED,
    FILE_STATUS_MISSING,
    FILE_STATUS_RELINKED,
    RETENTION_DEFAULT,
    RETENTION_PERMANENT,
    SCHEDULE_MODE_PREMIERE,
    SCHEDULE_STATE_CANCELLED,
    SCHEDULE_STATE_PUBLISHED,
    SCHEDULE_STATE_SCHEDULED,
    Asset,
    AssetMetadataUpdate,
    Chapter,
    ScheduleItem,
    ScheduleItemCreate,
    ScheduleItemResponse,
    StaffAssetRow,
    UploadedAssetResponse,
)
from civiccast.schedule.retention_terms import (
    RETENTION_TERM_UNIT_FOREVER,
    compute_retention_until,
)
from civiccast.vod.models import AssetMetadata
from civiccast.vod.store import AssetAlreadyExistsError


def _resolve_station_timezone_for_retention() -> str:
    """Local-import wrapper around the S1 station-timezone loader.

    Deferred import (matching ``civiccast.app._station_tz``'s own
    pattern) rather than a module-level import: ``civiccast.installer``
    pulls in a wide dependency graph, and this keeps that graph out of
    ``civiccast.schedule.store``'s import-time footprint for callers that
    never touch retention terms.
    """
    from civiccast.installer.station_state import resolve_station_timezone

    return resolve_station_timezone()


CommitReportList = list[CommitToAirReport]

SessionFactory = Callable[[], AbstractContextManager[Session]]

# Module-level alias so the inner methods can declare their return types
# without colliding with ``PostgresAssetStore.list`` in class scope (mypy
# resolves the bare name ``list`` to the method, not the builtin, when a
# new method is added below an existing ``list`` method on the same class).
_AssetMetadataList = list["AssetMetadata"]
_StaffAssetRowList = list["StaffAssetRow"]
_StaffAssetRowGroupList = list[list["StaffAssetRow"]]


class PostgresAssetStore:
    """:class:`civiccast.vod.store.AssetStore` Protocol implementation
    backed by SQLAlchemy + Postgres (or any SA-supported dialect).

    Constructor takes a session-factory callable that yields a context-
    managed :class:`sqlalchemy.orm.Session`. Tests inject an ephemeral
    SQLite-bound factory; production injects a Postgres-bound one. The
    store does not know which dialect it talks to.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def get(self, asset_id: str) -> AssetMetadata | None:
        """Return the asset metadata for ``asset_id`` or ``None`` if absent.

        Returns None for uploaded-but-not-yet-packaged assets (manifest_url
        IS NULL). Callers serving the public API must additionally require an
        explicit publication timestamp.
        """
        with self._session_factory() as session:
            row = session.execute(
                select(Asset).where(
                    Asset.asset_id == asset_id,
                    Asset.manifest_url.is_not(None),
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return row.to_metadata()

    def list(self) -> list[AssetMetadata]:
        """Return packaged assets (manifest_url IS NOT NULL) only.

        Uploaded-but-not-yet-packaged assets are excluded. Public-facing
        callers must additionally filter out rows without ``published_at``.
        Ordered by published_at DESC NULLS LAST, asset_id ASC.
        """
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(Asset)
                    .where(Asset.manifest_url.is_not(None))
                    .order_by(
                        Asset.published_at.desc().nulls_last(),
                        Asset.asset_id.asc(),
                    )
                )
                .scalars()
                .all()
            )
            return [row.to_metadata() for row in rows]

    def create(self, asset: AssetMetadata) -> AssetMetadata:
        """Persist ``asset`` and return the canonical persisted form.

        Raises :class:`AssetAlreadyExistsError` (domain exception) when
        the underlying INSERT trips a primary-key constraint. Calls
        ``session.rollback()`` before re-raising so the session remains
        reusable for subsequent operations (the conformance test
        ``TestRollbackAfterDuplicate`` asserts this end-to-end).
        """
        with self._session_factory() as session:
            row = Asset.from_metadata(asset)
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AssetAlreadyExistsError(asset_id=asset.asset_id) from exc
            session.refresh(row)
            return row.to_metadata()

    def list_all(self) -> _StaffAssetRowList:
        """Return every asset (including uploaded-but-not-packaged) for the
        operator's asset library.

        The narrower ``list()`` method filters to packaged assets
        (``manifest_url IS NOT NULL``). Public-facing callers also require
        ``published_at``. The operator console needs to see
        everything — pending_ingest, ingesting, validated (not yet packaged),
        and rejected — so this method intentionally skips that filter. Ordered
        by ``published_at DESC NULLS LAST, asset_id ASC`` to match ``list()``'s
        natural ordering.
        """
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(Asset).order_by(
                        Asset.published_at.desc().nulls_last(),
                        Asset.asset_id.asc(),
                    )
                )
                .scalars()
                .all()
            )
            return [row.to_staff_row() for row in rows]

    def list_all_page(self, *, limit: int = 50, offset: int = 0) -> tuple[_StaffAssetRowList, int]:
        """Paginated sibling of :meth:`list_all` for the operator library.

        4.0 media-library-hardening item 5 (pagination). ``list_all()``
        itself is left unbounded and unchanged — ``civiccast.publish.router``
        already calls it expecting a full unpaginated list, and changing
        its signature or shape would ripple into a router outside this
        worktree's scope. This is a separate method so the paginated
        staff-library endpoint doesn't disturb that caller. Returns
        ``(rows, total_count)`` — ``total_count`` is the full matching row
        count regardless of ``limit``/``offset``, for the caller to surface
        as a response header.
        """
        with self._session_factory() as session:
            total = session.execute(select(func.count()).select_from(Asset)).scalar_one()
            rows = (
                session.execute(
                    select(Asset)
                    .order_by(
                        Asset.published_at.desc().nulls_last(),
                        Asset.asset_id.asc(),
                    )
                    .limit(limit)
                    .offset(offset)
                )
                .scalars()
                .all()
            )
            return [row.to_staff_row() for row in rows], total

    def get_staff_row(self, asset_id: str) -> StaffAssetRow | None:
        """Return the operator-side projection for a single asset, or None.

        Mirrors :meth:`list_all` for one row. Includes uploaded-but-not-
        packaged assets (no manifest_url filter).
        """
        with self._session_factory() as session:
            row = session.execute(
                select(Asset).where(Asset.asset_id == asset_id)
            ).scalar_one_or_none()
            if row is None:
                return None
            return row.to_staff_row()

    def mark_published(self, asset_id: str, *, published_at: datetime) -> StaffAssetRow:
        """Make a packaged asset resident-visible after portal approval.

        WP-08: also captures ``retention_anchor_at`` the FIRST time an
        asset is published -- it is intentionally never overwritten on a
        later republish (that is exactly what would defeat its purpose as
        a fixed anchor: ``published_at`` itself is cleared by
        ``mark_unpublished`` and overwritten by every later publish, so it
        cannot serve as the anchor -- see ``Asset.retention_anchor_at``).
        """
        with self._session_factory() as session:
            row = session.execute(
                select(Asset).where(Asset.asset_id == asset_id)
            ).scalar_one_or_none()
            if row is None:
                raise ValueError(f"Asset not found: {asset_id}")
            if not row.manifest_url:
                raise ValueError(f"Asset is not packaged: {asset_id}")
            row.published_at = published_at
            if row.retention_anchor_at is None:
                row.retention_anchor_at = published_at
            session.commit()
            session.refresh(row)
            return row.to_staff_row()

    def mark_unpublished(self, asset_id: str) -> StaffAssetRow:
        """Withdraw an asset from Portal visibility (the inverse of :meth:`mark_published`).

        ``schedule.router.list_assets`` / ``get_asset`` (the public
        surfaces) gate visibility on ``published_at is not None`` alone, so
        clearing it here is sufficient to make the asset stop appearing in
        ``GET /api/public/assets`` and 404 from ``GET /api/public/assets/{id}``
        -- the exact lever ``mark_published`` sets to make it appear. Added
        for A-1: the first-run seeded sample's own description promises "Delete
        it like any other asset", but no removal path existed anywhere in the
        product before this. Idempotent -- unpublishing an already-unpublished
        asset is a no-op, mirroring ``cancel_schedule_item``'s "cancelling an
        already-cancelled item" idiom, so a retried request from a flaky
        connection can't error.

        Deliberately scoped to Portal visibility only: it does not touch
        Internet Archive, YouTube, or ActivityPub delivery records (those
        surfaces are peers, not children, of Portal per the three-tier
        publish model -- spec Sec 2.6) and does not attempt to reverse
        ``approve_publish``'s per-surface ``PublishRunRecord`` bookkeeping.
        A full multi-surface "withdraw" workflow is future work; this method
        only closes the concrete gap the seeded sample's promise depends on.
        """
        with self._session_factory() as session:
            row = session.execute(
                select(Asset).where(Asset.asset_id == asset_id)
            ).scalar_one_or_none()
            if row is None:
                raise AssetNotFoundError(asset_id)
            if row.published_at is None:
                return row.to_staff_row()
            row.published_at = None
            session.commit()
            session.refresh(row)
            return row.to_staff_row()

    @staticmethod
    def _apply_retention_term(
        session: Session,
        row: Asset,
        *,
        unit: str,
        value: int | None,
    ) -> None:
        """Author/convert ``row`` onto the WP-08 value/unit/forever contract.

        Reuses ``row.retention_anchor_at`` if already captured (the normal
        case -- the asset was published at some point, so
        :meth:`mark_published` already set it). If the asset has never
        been published, there is no reliable anchor to reuse; the
        finalization plan (WP-08, section 6, item 6) requires that case to
        set-and-audit the anchor at conversion time rather than silently
        inventing a historical one. Every recompute -- this call included
        -- reads the anchor, never writes a new one once it exists.

        Keeps the legacy ``retention_policy``/``retention_until`` columns
        in sync so ``civiccast.schedule.retention_worker`` (unmodified by
        WP-08) keeps enforcing correctly: ``forever`` mirrors onto
        ``retention_policy='permanent'`` (the worker's own
        ``!= 'permanent'`` skip) with ``retention_until=None``; every
        finite unit mirrors onto ``retention_policy='default'`` with a
        computed ``retention_until``.
        """
        anchor_fallback_used = False
        if row.retention_anchor_at is None:
            row.retention_anchor_at = datetime.now(UTC)
            anchor_fallback_used = True

        row.retention_term_unit = unit
        row.retention_term_value = value
        row.retention_until = compute_retention_until(
            anchor_at=row.retention_anchor_at,
            unit=unit,
            value=value,
            station_tz_name=_resolve_station_timezone_for_retention(),
        )
        row.retention_policy = (
            RETENTION_PERMANENT if unit == RETENTION_TERM_UNIT_FOREVER else RETENTION_DEFAULT
        )

        if anchor_fallback_used:
            session.add(
                MediaLifecycleAuditEntry(
                    asset_id=row.asset_id,
                    action="retention_term_anchor_fallback",
                    detail=(
                        f"Asset {row.asset_id} had no publication history to anchor its "
                        "retention term to; retention_anchor_at was set to the conversion "
                        f"time ({row.retention_anchor_at.isoformat()}) instead of a first-"
                        "publication instant. Future edits recompute from this fixed point."
                    ),
                )
            )

    def update_metadata(
        self,
        asset_id: str,
        update: AssetMetadataUpdate,
    ) -> StaffAssetRow:
        """Apply an :class:`AssetMetadataUpdate` to the named asset row.

        Sprint 0.3 task 5 metadata-edit endpoint. PATCH semantics
        clarified after audit-team v0.3.0 ENG-008:

        - **Missing key** → "leave this field unchanged."
          ``model_dump(exclude_unset=True)`` omits the field; the store
          skips it.
        - **Explicit ``None``** for a nullable field → "clear the field."
          The DB column becomes NULL.
        - **``chapters``** is a full replacement; ``None`` or ``[]`` both
          clear all chapters. A list fully replaces.
        - **``retention_policy``**: cannot be cleared (server enforces a
          default). Sending ``None`` for retention_policy is rejected by
          Pydantic.

        Optimistic concurrency (QA-008): ``update.expected_version``
        must match the row's current version. If it doesn't, raises
        :class:`AssetVersionConflictError`. On success the row's
        version is incremented and the new version is returned.

        Published-schedule guard (QA-007): if ANY ``schedule_items``
        row referencing this asset is in state ``published``, the
        edit is refused with :class:`AssetAlreadyPublishedError`. Once
        an asset is exposed to residents via a published schedule item,
        its trim/title/chapters/etc. are part of that publication's
        record; silent edits underneath a published surface change
        what residents see without an explicit re-publish step. The
        guard fires inside the same transaction as the OCC check so
        a race between concurrent publish and update is resolved
        deterministically.

        Chapter validation (QA-012): if ``chapters`` is set and the
        asset has a known ``duration_seconds``, every chapter ``t``
        must be strictly less than the duration. Raises
        :class:`ValueError` (router maps to 422) on violation.

        Raises:
            AssetNotFoundError: when no row matches ``asset_id``.
            AssetVersionConflictError: when ``expected_version`` doesn't
                match the row's current version.
            AssetAlreadyPublishedError: when the asset has one or more
                linked schedule items in state ``published``.
            ValueError: when a chapter timestamp is past the asset's
                duration.
        """
        with self._session_factory() as session:
            row = session.execute(
                select(Asset).where(Asset.asset_id == asset_id)
            ).scalar_one_or_none()
            if row is None:
                raise AssetNotFoundError(asset_id=asset_id)

            # QA-008 OCC check.
            if row.version != update.expected_version:
                raise AssetVersionConflictError(
                    asset_id=asset_id,
                    current_version=row.version,
                    expected_version=update.expected_version,
                )

            # QA-007 (audit-team v0.3.0): block trim/metadata edits when
            # the asset is already exposed to residents via a published
            # schedule item. The check runs inside this transaction so a
            # race with a concurrent schedule-publish is deterministic:
            # whichever transaction commits second sees the updated state
            # and either raises here (if the publish landed first) or
            # commits the edit (if the edit landed first and the publish
            # then publishes the edited asset). The list of conflicting
            # published_schedule_item_ids feeds the router's 409 detail
            # body so the operator can see exactly which publications
            # block the edit without a follow-up query.
            published_ids = list(
                session.execute(
                    select(ScheduleItem.id)
                    .where(ScheduleItem.asset_id == asset_id)
                    .where(ScheduleItem.state == SCHEDULE_STATE_PUBLISHED)
                    .order_by(ScheduleItem.scheduled_at.asc())
                ).scalars()
            )
            if published_ids:
                raise AssetAlreadyPublishedError(
                    asset_id=asset_id,
                    published_schedule_item_ids=[str(sid) for sid in published_ids],
                )

            data = update.model_dump(exclude_unset=True)
            data.pop("expected_version", None)
            if "title" in data:
                row.title = data["title"]
            if "description" in data:
                row.description = data["description"]
            if "meeting_body" in data:
                row.meeting_body = data["meeting_body"]
            if "trim_in_seconds" in data:
                row.trim_in_seconds = data["trim_in_seconds"]
            if "trim_out_seconds" in data:
                row.trim_out_seconds = data["trim_out_seconds"]
            if "chapters" in data:
                # Persist as JSON text for SQLite + Postgres parity.
                # None means "no chapters" (renders as empty list on read);
                # an empty list explicitly clears.
                if data["chapters"] is None:
                    row.chapters_json = None
                else:
                    chapters = [Chapter.model_validate(c) for c in data["chapters"]]
                    # QA-012: chapter t must be < asset duration if known.
                    if row.duration_seconds is not None:
                        bad = [c for c in chapters if c.t >= row.duration_seconds]
                        if bad:
                            raise ValueError(
                                f"Chapter timestamp {bad[0].t} is past the "
                                f"asset's duration of {row.duration_seconds}s."
                            )
                    row.chapters_json = json.dumps([c.model_dump() for c in chapters])
            # Coordinator-directed fix (follow-up commit, MAJOR finding 2):
            # a legacy-only PATCH (retention_policy/retention_until, no
            # retention_term_unit) against a row already authored under
            # the new value/unit/forever contract used to write the
            # legacy columns directly, silently desyncing them from the
            # authored term/anchor -- the next term edit would then
            # recompute retention_until from the anchor and clobber
            # whatever the legacy-only PATCH had just set, and in the
            # meantime retention_policy/retention_until would disagree
            # with retention_term_unit/retention_term_value. Refused
            # outright rather than silently accepted or silently
            # rerouted: AssetMetadataUpdate's own validator already
            # forbids retention_term_unit and retention_policy/
            # retention_until in the SAME payload, so if we're here with
            # a legacy field set, this payload carries no
            # retention_term_unit -- and if the row is already converted,
            # the client needs to say so explicitly by submitting a new
            # term, not the legacy pair.
            touches_legacy_retention = (
                "retention_policy" in data and data["retention_policy"] is not None
            ) or ("retention_until" in data)
            if touches_legacy_retention and row.retention_term_unit is not None:
                raise ValueError(
                    f"Asset {asset_id} already uses the value/unit/forever retention "
                    f"contract (retention_term_unit={row.retention_term_unit!r}); the "
                    "legacy retention_policy/retention_until fields can no longer be "
                    "edited directly. Submit a new retention_term_unit (and "
                    "retention_term_value, or 'forever') to change this asset's "
                    "retention term."
                )
            if "retention_policy" in data and data["retention_policy"] is not None:
                row.retention_policy = data["retention_policy"]
            if "retention_until" in data:
                row.retention_until = data["retention_until"]
            if "retention_term_unit" in data and data["retention_term_unit"] is not None:
                self._apply_retention_term(
                    session,
                    row,
                    unit=data["retention_term_unit"],
                    value=data.get("retention_term_value"),
                )

            # QA-008: every successful update increments version. Doing it
            # before commit so a failed commit still increments correctly
            # would be wrong — keep it inside the try block via the SA
            # column default + explicit increment here.
            row.version = row.version + 1

            try:
                session.commit()
            except IntegrityError:
                # CHECK-constraint violations (trim ordering, retention enum)
                # surface here on Postgres. The Pydantic surface already
                # blocks the same shapes; this is the last line of defense.
                session.rollback()
                raise
            session.refresh(row)
            return row.to_staff_row()

    def ingest_upload(
        self,
        *,
        asset_id: str,
        title: str,
        description: str | None,
        file_path: str,
        file_size_bytes: int,
        ffprobe_result: FfprobeResult,
        content_hash: str | None = None,
        thumbnail_path: str | None = None,
    ) -> UploadedAssetResponse:
        """Persist a newly uploaded + ffprobe-ingested asset.

        Creates the row with ``manifest_url=None`` (not yet packaged) and
        ``state='validated'`` (ffprobe passed the validation gate before
        this method is called). Raises :class:`AssetAlreadyExistsError`
        on duplicate ``asset_id``. ``content_hash``/``thumbnail_path`` (4.0
        media-library-hardening) are optional: the router computes them
        best-effort before calling this method and passes None if either
        step failed, so a hashing/thumbnail hiccup never blocks ingest.
        """
        with self._session_factory() as session:
            row = Asset.from_upload(
                asset_id=asset_id,
                title=title,
                description=description,
                file_path=file_path,
                file_size_bytes=file_size_bytes,
                state=ASSET_STATE_VALIDATED,
                duration_seconds=ffprobe_result.duration_seconds,
                codec_video=ffprobe_result.codec_video,
                codec_audio=ffprobe_result.codec_audio,
                width_px=ffprobe_result.width_px,
                height_px=ffprobe_result.height_px,
                bitrate_bps=ffprobe_result.bitrate_bps,
                format_name=ffprobe_result.format_name,
                content_hash=content_hash,
                thumbnail_path=thumbnail_path,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AssetAlreadyExistsError(asset_id=asset_id) from exc
            session.refresh(row)
            return row.to_upload_response()

    def mark_packaged(self, asset_id: str, manifest_url: str) -> StaffAssetRow:
        """Record a successfully written HLS package without publishing it."""
        with self._session_factory() as session:
            row = session.execute(
                select(Asset).where(Asset.asset_id == asset_id)
            ).scalar_one_or_none()
            if row is None:
                raise AssetNotFoundError(asset_id=asset_id)
            row.manifest_url = manifest_url
            session.commit()
            session.refresh(row)
            return row.to_staff_row()

    # ------------------------------------------------------------------
    # Media-library hardening (4.0 scope item 5): missing-file / relink /
    # duplicate detection.
    # ------------------------------------------------------------------

    def list_missing_thumbnails(self) -> _StaffAssetRowList:
        """Return assets that have a backing file but no thumbnail yet.

        Feeds the ``civiccast media thumbnails-backfill`` CLI command
        (generation-on-ingest is best-effort; this is the catch-up path
        for assets ingested before thumbnailing existed, or where
        generation failed at ingest time — e.g. ffmpeg wasn't installed
        yet).
        """
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(Asset)
                    .where(Asset.file_path.is_not(None), Asset.thumbnail_path.is_(None))
                    .order_by(Asset.asset_id.asc())
                )
                .scalars()
                .all()
            )
            return [row.to_staff_row() for row in rows]

    def set_thumbnail_path(self, asset_id: str, thumbnail_path: str) -> None:
        """Persist a generated thumbnail's path for ``asset_id``.

        Used only by the backfill command — ingest-time generation goes
        through :meth:`ingest_upload`'s ``thumbnail_path`` parameter
        instead. Silently no-ops if the asset no longer exists (the
        backfill command lists rows and generates thumbnails for each in
        a separate step; tolerate a row disappearing in between rather
        than failing the whole batch).
        """
        with self._session_factory() as session:
            row = session.execute(
                select(Asset).where(Asset.asset_id == asset_id)
            ).scalar_one_or_none()
            if row is None:
                return
            row.thumbnail_path = thumbnail_path
            session.commit()

    def list_broken(self) -> _StaffAssetRowList:
        """Return every asset whose ``file_status`` is ``missing``.

        Written by :class:`civiccast.schedule.media_integrity_worker
        .MediaIntegrityWorker`. Ordered by ``file_status_checked_at`` ASC
        (oldest-flagged first) so an operator working the queue clears the
        longest-standing gaps first.
        """
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(Asset)
                    .where(Asset.file_status == FILE_STATUS_MISSING)
                    .order_by(
                        Asset.file_status_checked_at.asc().nulls_last(),
                        Asset.asset_id.asc(),
                    )
                )
                .scalars()
                .all()
            )
            return [row.to_staff_row() for row in rows]

    def relink(
        self,
        asset_id: str,
        *,
        new_file_path: str,
        ffprobe_result: FfprobeResult,
        content_hash: str | None,
        now: datetime | None = None,
    ) -> StaffAssetRow:
        """Point ``asset_id`` at ``new_file_path`` after a probe-based match.

        The router is responsible for running ffprobe on the candidate file
        and comparing it against the stored duration/codec within tolerance
        (see ``router.relink_asset`` for the tolerance policy) *before*
        calling this method — the store only persists the already-approved
        result. This mirrors ``update_metadata``'s division of labour
        (Pydantic/cross-field validation upstream, persistence here).

        Sets ``file_status='relinked'`` and refreshes the ffprobe-derived
        columns (duration/codec/etc.) from the new file's own probe, since
        a relinked file is not guaranteed byte-identical to the original
        (that's the whole point of a tolerance window). Raises
        :class:`AssetNotFoundError` if the asset doesn't exist.
        """
        resolved_now = now or datetime.now(UTC)
        with self._session_factory() as session:
            row = session.execute(
                select(Asset).where(Asset.asset_id == asset_id)
            ).scalar_one_or_none()
            if row is None:
                raise AssetNotFoundError(asset_id=asset_id)

            row.file_path = new_file_path
            row.file_status = FILE_STATUS_RELINKED
            row.file_status_checked_at = resolved_now
            row.duration_seconds = ffprobe_result.duration_seconds
            row.codec_video = ffprobe_result.codec_video
            row.codec_audio = ffprobe_result.codec_audio
            row.width_px = ffprobe_result.width_px
            row.height_px = ffprobe_result.height_px
            row.bitrate_bps = ffprobe_result.bitrate_bps
            row.format_name = ffprobe_result.format_name
            if content_hash is not None:
                row.content_hash = content_hash
            row.version = row.version + 1
            session.commit()
            session.refresh(row)
            return row.to_staff_row()

    def list_duplicates(self) -> _StaffAssetRowGroupList:
        """Group assets sharing a non-null ``content_hash``.

        Report-only per the spec's "non-destructive, report never
        auto-delete" framing — returns groups of 2+ assets with identical
        content, sorted by asset_id within each group; groups sorted by
        their first asset_id so the response is deterministic. Assets
        without a hash (content_hash IS NULL — ingested before this
        migration, or created without a backing file) are never grouped.
        """
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(Asset)
                    .where(Asset.content_hash.is_not(None))
                    .order_by(Asset.content_hash.asc(), Asset.asset_id.asc())
                )
                .scalars()
                .all()
            )
        groups: dict[str, list[StaffAssetRow]] = {}
        for row in rows:
            assert row.content_hash is not None  # guarded by the WHERE clause
            groups.setdefault(row.content_hash, []).append(row.to_staff_row())
        return [group for group in groups.values() if len(group) > 1]


# ---------------------------------------------------------------------------
# Asset metadata-edit error (Sprint 0.3 task 5)
# ---------------------------------------------------------------------------


class AssetNotFoundError(KeyError):
    """Raised when a metadata-edit targets a missing asset."""

    def __init__(self, asset_id: str) -> None:
        super().__init__(asset_id)
        self.asset_id = asset_id


class AssetVersionConflictError(Exception):
    """Raised when an OCC version check fails on PATCH /api/staff/assets/{id}.

    Audit-team v0.3.0 — QA-008. The router maps this to HTTP 409 with
    a structured payload that exposes both the client's expected version
    and the current row version so the operator UI can surface a helpful
    "this row was updated elsewhere; reload?" prompt.
    """

    def __init__(
        self,
        *,
        asset_id: str,
        current_version: int,
        expected_version: int,
    ) -> None:
        super().__init__(
            f"asset {asset_id!r}: expected version {expected_version} "
            f"but current is {current_version}"
        )
        self.asset_id = asset_id
        self.current_version = current_version
        self.expected_version = expected_version


class AssetAlreadyPublishedError(Exception):
    """Raised when ``update_metadata`` is called against an asset that has
    at least one linked ``schedule_items`` row in state ``published``.

    Audit-team v0.3.0 -- QA-007 (TOCTOU edit-trim button + state guard at
    update_metadata). Once an asset is exposed to residents via a
    published schedule item, the trim window, chapters, title, and other
    metadata are part of that publication's record; silent edits
    underneath a published surface change what residents are seeing
    without an explicit re-publish step.

    The guard fires inside the same transaction as the OCC version
    check (:class:`AssetVersionConflictError`) so a race between a
    concurrent schedule-publish and an in-flight metadata update is
    resolved deterministically: whichever transaction commits second
    sees the updated state and raises.

    Carries the list of conflicting ``published_schedule_item_ids`` so
    the router can produce a 409 detail body that names every published
    item the operator may want to inspect or unpublish before retrying
    the edit. The audit's "fix and retry" flow needs that list to be
    actionable; an opaque "asset is published" response would force the
    operator into a separate lookup.
    """

    def __init__(
        self,
        *,
        asset_id: str,
        published_schedule_item_ids: list[str],
    ) -> None:
        super().__init__(
            f"asset {asset_id!r} cannot be edited: "
            f"{len(published_schedule_item_ids)} linked schedule item(s) "
            f"already published ({', '.join(published_schedule_item_ids[:3])}"
            f"{', ...' if len(published_schedule_item_ids) > 3 else ''})"
        )
        self.asset_id = asset_id
        self.published_schedule_item_ids = published_schedule_item_ids


# ---------------------------------------------------------------------------
# Schedule store — premiere / embargo (Sprint 0.3 task 4; ``live`` retired in
# migration 0005 per audit-team v0.3.0 ENG-004)
# ---------------------------------------------------------------------------


class ScheduleConflictError(RuntimeError):
    """Raised when a schedule item would overlap an existing one.

    The DB-level btree_gist EXCLUDE constraint enforces the contract on
    Postgres; this exception is the domain translation of the resulting
    IntegrityError so the router (and any other caller) sees a single
    type and can populate an HTTP 409 with operator-readable detail.

    ``conflicting_item`` is the existing schedule item whose time range
    overlaps the proposed insert. Looking it up requires a follow-up
    query because Postgres' constraint-violation error message does not
    include the conflicting row's id; the store does that lookup so
    callers receive structured information.
    """

    def __init__(
        self,
        message: str,
        conflicting_item: ScheduleItemResponse | None = None,
    ) -> None:
        super().__init__(message)
        self.conflicting_item = conflicting_item


class ScheduleItemNotFoundError(KeyError):
    """Raised when a get/cancel targets a missing schedule item."""

    def __init__(self, schedule_id: object) -> None:
        super().__init__(schedule_id)
        self.schedule_id = schedule_id


class PostgresScheduleStore:
    """SQLAlchemy-backed store for ``civiccast.schedule_items``.

    Mirrors :class:`PostgresAssetStore`'s lazy-engine + session-factory
    posture. Constructor takes a session-factory callable that yields a
    context-managed :class:`sqlalchemy.orm.Session`.

    DB-level conflict detection lives in the Postgres EXCLUDE constraint
    (migration 0003). On SQLite (the fast-test path) the EXCLUDE
    constraint is absent — the structural CHECK constraints still ship
    via the SA model's ``__table_args__``, but two overlapping live or
    premiere events on the same channel will NOT be rejected by SQLite.
    The conflict-detection contract is asserted exclusively against the
    real-Postgres testcontainers fixture.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def create(self, payload: ScheduleItemCreate) -> ScheduleItemResponse:
        """Persist a new schedule item.

        Raises:
            AssetNotFoundError: when the referenced asset_id does not
                exist. The router maps this to HTTP 404. (audit-team
                v0.3.0 — QA-004; the schedule_items table will gain a
                real FK in a follow-up migration but the application-
                layer existence check is the immediate fix.)
            ScheduleConflictError: when the Postgres EXCLUDE constraint
                rejects the insert (only relevant on Postgres). The
                exception carries the conflicting existing item when
                a follow-up lookup can identify it.
        """
        with self._session_factory() as session:
            # QA-004: existence check before insert. Without this, a
            # scripted client can post a schedule pointing at any
            # pattern-conforming slug — including one that doesn't exist
            # or hasn't been validated. The schedule_items.asset_id
            # column has no FK in v0.3 so this is the only guard.
            asset_exists = (
                session.execute(
                    select(Asset.asset_id).where(Asset.asset_id == payload.asset_id)
                ).scalar_one_or_none()
                is not None
            )
            if not asset_exists:
                raise AssetNotFoundError(f"asset_id {payload.asset_id!r} does not exist.")
            # Normalize scheduled_at to UTC at the persistence boundary so
            # round-trips compare cleanly across SQLite (which strips tzinfo)
            # and Postgres (which keeps it).
            scheduled_at = payload.scheduled_at.astimezone(UTC)
            # Compute scheduled_at_end at write time. Storing it as a
            # plain column lets the Postgres EXCLUDE constraint reference
            # two columns directly — the alternative (computing the end
            # via ``scheduled_at + interval``) is STABLE not IMMUTABLE
            # in Postgres and is rejected as an index expression.
            scheduled_at_end = (
                scheduled_at + timedelta(seconds=payload.duration_seconds)
                if payload.duration_seconds is not None
                else None
            )

            row = ScheduleItem(
                asset_id=payload.asset_id,
                channel_id=payload.channel_id,
                mode=payload.mode,
                scheduled_at=scheduled_at,
                scheduled_at_end=scheduled_at_end,
                duration_seconds=payload.duration_seconds,
                notes=payload.notes,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                # Postgres EXCLUDE-constraint violations carry the
                # constraint name in the underlying pgcode/diag. We
                # match by constraint name in the error message string —
                # robust across psycopg v3's error-info shapes.
                msg = str(exc.orig) if exc.orig is not None else str(exc)
                if "schedule_items_no_overlap" in msg:
                    # Lookup at the EXCLUDE constraint's own WHERE filter
                    # (migration 0071: state IN ('scheduled', 'published')):
                    # the conflicting row was in one of those two states at
                    # the moment of EXCLUDE, so this is the common case.
                    conflict = self._find_conflicting(
                        channel_id=payload.channel_id,
                        scheduled_at=scheduled_at,
                        duration_seconds=payload.duration_seconds,
                    )
                    # QA-005 (audit-team v0.3.0): the conflicting row may
                    # have transitioned out of 'scheduled'/'published' (to
                    # 'cancelled') between the EXCLUDE rejection and this
                    # lookup. The default filter would then miss it and the
                    # 409 response loses the conflicting-item enrichment.
                    # Retry once with the broadened state filter; the
                    # returned response's ``state`` field tells the caller
                    # (and downstream router) what state the conflict was in
                    # when finally observed. This is the minimal-blast-radius
                    # fix per the design note (Option A); Option B (full
                    # serializable wrap) remains deferred unless this proves
                    # unsound.
                    used_fallback_lookup = False
                    if conflict is None:
                        conflict = self._find_conflicting(
                            channel_id=payload.channel_id,
                            scheduled_at=scheduled_at,
                            duration_seconds=payload.duration_seconds,
                            states=_SCHEDULE_STATES,
                        )
                        used_fallback_lookup = True
                    # Compose a state-aware message that operators can act
                    # on. Only the fallback lookup implies the row raced out
                    # from under us — a conflict found on the first (default
                    # scheduled+published) lookup is a normal, unraced
                    # conflict even when its state is 'published'.
                    if conflict is not None and used_fallback_lookup:
                        conflict_msg = (
                            f"Schedule conflict on channel {payload.channel_id!r} "
                            f"at {scheduled_at.isoformat()}. The conflicting "
                            f"item ({conflict.id}) transitioned to "
                            f"{conflict.state!r} during your request; please "
                            f"retry the create."
                        )
                    else:
                        conflict_msg = (
                            f"Schedule conflict on channel {payload.channel_id!r} "
                            f"at {scheduled_at.isoformat()}."
                        )
                    raise ScheduleConflictError(
                        conflict_msg,
                        conflicting_item=conflict,
                    ) from exc
                # Anything else — re-raise the original so the operator
                # gets the real cause, not a misleading 409.
                raise
            session.refresh(row)
            # UX-003: fill asset_title in the create response too. The
            # asset existence check above (QA-004) means this lookup can
            # only fail under a vanishingly small race; tolerate None.
            title = session.execute(
                select(Asset.title).where(Asset.asset_id == row.asset_id)
            ).scalar_one_or_none()
            return row.to_response(asset_title=title)

    def list(
        self,
        *,
        channel_id: str | None = None,
        states: tuple[str, ...] | None = None,
    ) -> list[ScheduleItemResponse]:
        """Return schedule items, optionally filtered by channel / state.

        Default ordering: ``scheduled_at ASC`` (chronological). LEFT JOIN
        on Asset to fill ``asset_title`` per UX-003.
        """
        if states is not None:
            for s in states:
                if s not in _SCHEDULE_STATES:
                    raise ValueError(
                        f"Unknown schedule state {s!r}; expected one of {_SCHEDULE_STATES}."
                    )

        with self._session_factory() as session:
            stmt = (
                select(ScheduleItem, Asset.title)
                .outerjoin(Asset, Asset.asset_id == ScheduleItem.asset_id)
                .order_by(ScheduleItem.scheduled_at.asc())
            )
            if channel_id is not None:
                stmt = stmt.where(ScheduleItem.channel_id == channel_id)
            if states is not None:
                stmt = stmt.where(ScheduleItem.state.in_(states))
            rows = session.execute(stmt).all()
            return [row[0].to_response(asset_title=row[1]) for row in rows]

    def get(self, schedule_id: object) -> ScheduleItemResponse | None:
        """Return one schedule item by id, or None if absent."""
        with self._session_factory() as session:
            result = session.execute(
                select(ScheduleItem, Asset.title)
                .outerjoin(Asset, Asset.asset_id == ScheduleItem.asset_id)
                .where(ScheduleItem.id == schedule_id)
            ).first()
            if result is None:
                return None
            row, title = result
            response: ScheduleItemResponse = row.to_response(asset_title=title)
            return response

    def cancel(self, schedule_id: object) -> ScheduleItemResponse:
        """Transition a ``scheduled`` or ``published`` item to ``cancelled``.

        Raises:
            ScheduleItemNotFoundError: when no row matches ``schedule_id``.

        Commit-to-Air state machine: cancel must work from BOTH
        ``scheduled`` (an operator pulls an item before it was ever
        approved) and ``published`` (a rollback, or a plain cancel, of an
        already-approved/aired item — see ``CommitService.rollback``).
        Cancelling an already-cancelled item is a no-op that returns the
        current state. The operator UI can decide whether to surface that
        as a "nothing to cancel" toast.
        """
        with self._session_factory() as session:
            row = session.get(ScheduleItem, schedule_id)
            if row is None:
                raise ScheduleItemNotFoundError(schedule_id)
            if row.state in (SCHEDULE_STATE_SCHEDULED, SCHEDULE_STATE_PUBLISHED):
                row.state = SCHEDULE_STATE_CANCELLED
                session.commit()
                session.refresh(row)
            # Re-fetch with asset title for consistent response shape.
            title_row = session.execute(
                select(Asset.title).where(Asset.asset_id == row.asset_id)
            ).scalar_one_or_none()
            return row.to_response(asset_title=title_row)

    def mark_published(self, schedule_ids: Sequence[object]) -> int:
        """Transition scheduled items to ``published`` directly, bypassing Commit-to-Air.

        NOT called by production code — :class:`CommitService.commit` uses
        :meth:`commit_to_air` instead, which flips the row AND persists its
        ``CommitToAirReport`` in one transaction (the real concurrency gate).
        This method is the shared test-suite shortcut for reaching a
        ``published`` row without the full report/playout round-trip (see
        ``tests/schedule/test_schedule_router.py``'s ``_publish`` helper and
        callers in ``test_commit_service.py``/``test_real_postgres.py``).
        Only rows currently ``scheduled`` are flipped — an already-cancelled
        row is left alone rather than resurrected. Silently ignores ids that
        don't exist.

        Returns the number of rows actually transitioned.
        """
        if not schedule_ids:
            return 0
        with self._session_factory() as session:
            result = session.execute(
                update(ScheduleItem)
                .where(
                    ScheduleItem.id.in_(schedule_ids),
                    ScheduleItem.state == SCHEDULE_STATE_SCHEDULED,
                )
                .values(state=SCHEDULE_STATE_PUBLISHED)
            )
            session.commit()
            return int(cast(CursorResult[object], result).rowcount or 0)

    def commit_to_air(self, schedule_id: object, report: CommitToAirReport) -> bool:
        """Approve one item to air atomically: flip ``scheduled -> published``
        AND persist its pending Commit-to-Air report in a SINGLE transaction.

        Returns ``True`` when the item was still ``scheduled`` and is now
        ``published`` (report written, committed). Returns ``False`` when the
        item was no longer ``scheduled`` — a concurrent commit already won, or
        it was cancelled — in which case nothing is written and the transaction
        rolls back, so the caller must dispatch nothing.

        Doing the flip and the report insert in one transaction is the
        Commit-to-Air integrity guarantee: the item can never end up
        ``published`` (airable, publicly advertised) without a durable approval
        report, and the ``UPDATE ... WHERE state='scheduled'`` matches exactly
        one of any racing commits (the loser gets 0 rows and is refused).
        """
        with self._session_factory() as session:
            result = session.execute(
                update(ScheduleItem)
                .where(
                    ScheduleItem.id == schedule_id,
                    ScheduleItem.state == SCHEDULE_STATE_SCHEDULED,
                )
                .values(state=SCHEDULE_STATE_PUBLISHED)
            )
            if int(cast(CursorResult[object], result).rowcount or 0) == 0:
                # Lost the race / no longer scheduled: roll back, persist nothing.
                session.rollback()
                return False
            row = CommitToAirReportRow.from_report(report)
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.commit()
            return True

    def _find_conflicting(
        self,
        *,
        channel_id: str,
        scheduled_at: datetime,
        duration_seconds: int | None,
        states: tuple[str, ...] = (SCHEDULE_STATE_SCHEDULED, SCHEDULE_STATE_PUBLISHED),
    ) -> ScheduleItemResponse | None:
        """Lookup the existing schedule item that overlaps the proposed insert.

        Used to enrich the :class:`ScheduleConflictError` with the
        conflicting item's id + title so the operator can act on it.
        Returns None if no overlap is found via the lookup query.

        ``states`` controls which schedule_items.state values the lookup
        considers. Defaults to the (``scheduled``, ``published``) filter
        that matches the EXCLUDE constraint's WHERE clause (migration
        0071_published_blocks_overlap: ``state IN ('scheduled',
        'published') AND mode = 'premiere'`` — a published item occupies
        real airtime, so it blocks overlapping inserts exactly like a
        scheduled one). Callers handling the QA-005 race (conflicting row
        cancelled between the EXCLUDE rejection and this lookup) pass the
        broader ``_SCHEDULE_STATES`` tuple to find rows that were active at
        the moment of EXCLUDE but have since transitioned out of both
        ``scheduled`` and ``published``. The returned
        :class:`ScheduleItemResponse` carries ``state``, so the caller
        can inspect whether the conflict is still scheduled or has
        already transitioned (cancelled / published) since the lookup
        started.
        """
        if duration_seconds is None:
            # An embargo proposal cannot trigger the EXCLUDE; defensive.
            return None
        proposed_end = scheduled_at + timedelta(seconds=duration_seconds)

        with self._session_factory() as session:
            stmt = (
                select(ScheduleItem)
                .where(
                    ScheduleItem.channel_id == channel_id,
                    ScheduleItem.state.in_(states),
                    # Audit-team v0.3.0 ENG-004: ``live`` was retired from
                    # the schedule_items mode enum in migration 0005. Only
                    # ``premiere`` participates in time-range conflict
                    # detection (embargo is exempt by spec §1070).
                    ScheduleItem.mode == SCHEDULE_MODE_PREMIERE,
                    # Existing event ends > proposed start AND
                    # existing event starts < proposed end → overlap.
                    ScheduleItem.scheduled_at < proposed_end,
                )
                .order_by(ScheduleItem.scheduled_at.asc())
            )
            for row in session.execute(stmt).scalars():
                if row.duration_seconds is None:
                    continue
                row_start = row.scheduled_at
                if row_start.tzinfo is None:
                    row_start = row_start.replace(tzinfo=UTC)
                row_end = row_start + timedelta(seconds=row.duration_seconds)
                if row_end > scheduled_at:
                    return row.to_response()
            return None

    # ------------------------------------------------------------------
    # Commit-to-Air reports (S4 slice 1)
    # ------------------------------------------------------------------
    def upsert_commit_report(self, report: CommitToAirReport) -> CommitToAirReport:
        """Insert a new commit-to-air report or update an existing one by id.

        The commit workflow writes the row ``pending`` then re-upserts it as
        dispatch advances (queued / acknowledged / error) or on rollback
        (cancelled). On update the original ``created_at`` is preserved and
        ``updated_at`` is refreshed to now; ``report_id`` is immutable. Returns
        the stored record with datetimes normalized to UTC.
        """
        with self._session_factory() as session:
            existing = session.get(CommitToAirReportRow, report.report_id)
            now = datetime.now(UTC)
            if existing is None:
                row = CommitToAirReportRow.from_report(report)
                # The store stamps the write instant so a re-upsert can't move
                # ``updated_at`` backwards relative to the previous write.
                row.updated_at = now
                session.add(row)
            else:
                # Update mutable fields in place; ``created_at`` is preserved.
                existing.channel_id = report.channel_id
                existing.occurrence_id = report.occurrence_id
                existing.schedule_item_id = report.schedule_item_id
                existing.asset_id = report.asset_id
                existing.title = report.title
                existing.scheduled_at = report.scheduled_at.astimezone(UTC)
                existing.duration_seconds = report.duration_seconds
                existing.approved_by_operator_id = report.approved_by_operator_id
                existing.approved_at = report.approved_at.astimezone(UTC)
                existing.conflicts_found = report.conflicts_found
                existing.gaps_found = report.gaps_found
                existing.dispatch_status = report.dispatch_status
                existing.dispatch_error_detail = report.dispatch_error_detail
                existing.dispatch_timestamp = (
                    report.dispatch_timestamp.astimezone(UTC)
                    if report.dispatch_timestamp is not None
                    else None
                )
                existing.operator_notes = report.operator_notes
                existing.rollback_reason = report.rollback_reason
                existing.rolled_back_at = (
                    report.rolled_back_at.astimezone(UTC)
                    if report.rolled_back_at is not None
                    else None
                )
                existing.updated_at = now
                row = existing
            session.commit()
            session.refresh(row)
            return row.to_report()

    def get_commit_report(self, report_id: str) -> CommitToAirReport | None:
        """Return one commit-to-air report by id, or None if absent."""
        with self._session_factory() as session:
            row = session.get(CommitToAirReportRow, report_id)
            return row.to_report() if row is not None else None

    def list_commit_reports(
        self,
        *,
        channel_id: str,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        limit: int = 50,
    ) -> CommitReportList:
        """Return commit-to-air reports for a channel, most-recently-committed
        first.

        Filters by ``channel_id`` (required) and an optional ``approved_at``
        half-open range ``[start_at, end_at)`` — the commit-action timeline
        that backs the operator's "recent commits" panel. Ordered by
        ``approved_at`` DESC. ``limit`` is clamped to ``[1, 500]`` so a caller
        cannot request an unbounded scan.
        """
        bounded_limit = max(1, min(limit, 500))
        with self._session_factory() as session:
            stmt = (
                select(CommitToAirReportRow)
                .where(CommitToAirReportRow.channel_id == channel_id)
                .order_by(CommitToAirReportRow.approved_at.desc())
                .limit(bounded_limit)
            )
            if start_at is not None:
                stmt = stmt.where(CommitToAirReportRow.approved_at >= start_at.astimezone(UTC))
            if end_at is not None:
                stmt = stmt.where(CommitToAirReportRow.approved_at < end_at.astimezone(UTC))
            return [row.to_report() for row in session.execute(stmt).scalars()]
