# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable caption review + offline caption job storage (Stage E, K3).

Caption review decisions are operator work product on the public-record path;
until this stage they lived only in ``InMemoryCaptionReviewStore`` — even when
durable storage was active — and vanished on restart.
:class:`PostgresCaptionReviewStore` implements the existing
``CaptionReviewStore`` protocol over ``caption_review_items``
(migration ``0025_caption_review_items``) with semantics identical to the
in-memory store. Despite the name (matching the repo's other Postgres-backed
stores), it runs on the managed SQLite path too.

:class:`PostgresOfflineCaptionJobStore` is the same idea for CivicCast One's
keystone K3: the offline caption job for a published recording spans a model
pass and an operator's review, which can be hours apart and must survive a
restart in between. It implements ``OfflineCaptionJobStore`` over
``offline_caption_jobs`` (migration ``0075_offline_caption_jobs``).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from civiccast.captions.models import CaptionCue
from civiccast.captions.review import (
    CaptionReviewAudioEvidence,
    CaptionReviewAudioEvidenceRequiredError,
    CaptionReviewDecision,
    CaptionReviewEdit,
    CaptionReviewItemAlreadyExistsError,
    CaptionReviewItemCreate,
    CaptionReviewItemNotFoundError,
    CaptionReviewItemResponse,
    CaptionReviewStatus,
    edited_caption_cue,
    legacy_translation_source_window,
    require_low_confidence_approval_evidence,
)
from civiccast.captions.vod_job import (
    OFFLINE_CAPTION_JOB_ACTIVE_STATES,
    OfflineCaptionJobConflictError,
    OfflineCaptionJobRecord,
)
from civiccast.db import Base

if TYPE_CHECKING:
    from collections.abc import Sequence

SessionFactory = Callable[[], AbstractContextManager[Session]]

__all__ = [
    "CaptionReviewItem",
    "OfflineCaptionJob",
    "PostgresCaptionReviewStore",
    "PostgresOfflineCaptionJobStore",
]


class CaptionReviewItem(Base):
    """A caption cue queued for operator review, with the decision state."""

    __tablename__ = "caption_review_items"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'edited', 'rejected')",
            name="caption_review_items_status_check",
        ),
        CheckConstraint(
            "end_seconds > start_seconds",
            name="caption_review_items_cue_window_check",
        ),
        CheckConstraint(
            "("
            "audio_evidence_path IS NULL AND "
            "audio_evidence_start_seconds IS NULL AND "
            "audio_evidence_sha256 IS NULL AND "
            "audio_evidence_bytes IS NULL"
            ") OR ("
            "audio_evidence_path IS NOT NULL AND "
            "audio_evidence_start_seconds >= 0 AND "
            "audio_evidence_sha256 IS NOT NULL AND "
            "audio_evidence_bytes > 0"
            ")",
            name="caption_review_items_audio_evidence_check",
        ),
    )

    review_item_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    # BCP-47-ish language tag scoping the review pass this row belongs to
    # (``en`` transcription vs ``es`` recorded-Spanish translation). Kept
    # non-null with an ``en`` default/server_default so 0083 backfills every
    # pre-existing row as English (migration 0083_caption_review_language).
    language: Mapped[str] = mapped_column(
        String(12), nullable=False, default="en", server_default="en"
    )
    cue_id: Mapped[str] = mapped_column(String(160), nullable=False)
    translation_source_start_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    translation_source_end_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    end_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    low_confidence: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    reviewed_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    audio_evidence_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_evidence_start_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    audio_evidence_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    audio_evidence_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PostgresCaptionReviewStore:
    """DB-backed caption review store (Postgres or managed SQLite)."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def create(self, payload: CaptionReviewItemCreate) -> CaptionReviewItemResponse:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            existing = session.get(CaptionReviewItem, payload.review_item_id)
            if existing is not None:
                raise CaptionReviewItemAlreadyExistsError(payload.review_item_id)
            row = CaptionReviewItem(
                review_item_id=payload.review_item_id,
                asset_id=payload.asset_id,
                language=payload.language,
                cue_id=payload.cue.cue_id,
                start_seconds=payload.cue.start_seconds,
                end_seconds=payload.cue.end_seconds,
                confidence=payload.cue.confidence,
                low_confidence=payload.cue.low_confidence,
                status="pending",
                original_text=payload.cue.text,
                reviewed_text=None,
                reviewer_note=payload.reviewer_note,
                audio_evidence_path=(
                    payload.audio_evidence.source_path
                    if payload.audio_evidence is not None
                    else None
                ),
                audio_evidence_start_seconds=(
                    payload.audio_evidence.source_start_seconds
                    if payload.audio_evidence is not None
                    else None
                ),
                audio_evidence_sha256=(
                    payload.audio_evidence.source_sha256
                    if payload.audio_evidence is not None
                    else None
                ),
                audio_evidence_bytes=(
                    payload.audio_evidence.source_bytes
                    if payload.audio_evidence is not None
                    else None
                ),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                # The ``session.get`` above is check-then-insert with no row
                # lock, so two concurrent creators of the same review item --
                # two offline-caption worker ticks both finding the Spanish
                # translation short and both re-queueing the missing cues --
                # can both pass it before either commits. ``review_item_id``
                # is the primary key, so the DB is the real guard and one of
                # them loses here. Translate that loss into the store's own
                # "already exists" error, exactly as the offline caption job
                # queue translates its partial-unique-index loss into
                # OfflineCaptionJobConflictError: the caller
                # (civiccast.captions.vod.queue_translated_captions) already
                # treats that as "someone else queued this row" and records a
                # duplicate, instead of surfacing a raw DB error that would
                # fail an otherwise-healthy caption job.
                session.rollback()
                raise CaptionReviewItemAlreadyExistsError(payload.review_item_id) from exc
            session.refresh(row)
            return _to_response(row)

    def get(self, review_item_id: str) -> CaptionReviewItemResponse | None:
        with self._session_factory() as session:
            row = session.get(CaptionReviewItem, review_item_id)
            return _to_response(row) if row is not None else None

    def get_audio_evidence(
        self,
        review_item_id: str,
    ) -> CaptionReviewAudioEvidence | None:
        with self._session_factory() as session:
            row = session.get(CaptionReviewItem, review_item_id)
            if row is None or row.audio_evidence_path is None:
                return None
            if (
                row.audio_evidence_start_seconds is None
                or row.audio_evidence_sha256 is None
                or row.audio_evidence_bytes is None
            ):
                raise RuntimeError(
                    f"Caption review item {review_item_id!r} has incomplete audio evidence."
                )
            return CaptionReviewAudioEvidence(
                source_path=row.audio_evidence_path,
                source_start_seconds=row.audio_evidence_start_seconds,
                source_sha256=row.audio_evidence_sha256,
                source_bytes=row.audio_evidence_bytes,
            )

    def list_with_audio_evidence(
        self,
    ) -> list[tuple[CaptionReviewItemResponse, CaptionReviewAudioEvidence | None]]:
        """Read review rows and their private evidence in one database query.

        Retention runs this at worker startup, where opening a new session for
        every row turns a large review queue into an N+1 round-trip stall.  Keep
        the response/evidence pair in memory only; the public review response
        still omits the private evidence path.
        """

        with self._session_factory() as session:
            rows = session.execute(
                select(CaptionReviewItem).order_by(
                    CaptionReviewItem.created_at.asc(), CaptionReviewItem.review_item_id.asc()
                )
            ).scalars()
            return [(_to_response(row), _row_audio_evidence(row)) for row in rows]

    def list(
        self,
        *,
        asset_id: str | None = None,
        status: CaptionReviewStatus | None = None,
        language: str | None = None,
    ) -> list[CaptionReviewItemResponse]:
        # Known contract difference from ``InMemoryCaptionReviewStore.list()``
        # (item 92): the in-memory store breaks a ``created_at`` tie with a
        # per-process monotonic insertion sequence, so it always returns
        # rows in creation order. This store still breaks ties with
        # ``review_item_id`` (lexical, not creation order) because there is
        # no durable, DB-native equivalent of that in-process counter without
        # adding a monotonic sequence column via a new migration. In
        # practice a same-``created_at`` tie here would need two ``create()``
        # calls to land in the same server-side timestamp tick, which is far
        # less likely than the in-memory store's bug (a tight synchronous
        # Python loop reading the wall clock faster than its resolution) --
        # every session round-trip in between spaces the timestamps out --
        # but it is not impossible, and no caller here has been audited as
        # exact-tie-safe the way ``captions/retention.py`` documents itself.
        # If this is ever seen live, the fix is an autoincrement sequence
        # column analogous to the in-memory counter, not a semantics change.
        with self._session_factory() as session:
            query = select(CaptionReviewItem).order_by(
                CaptionReviewItem.created_at.asc(), CaptionReviewItem.review_item_id.asc()
            )
            if asset_id is not None:
                query = query.where(CaptionReviewItem.asset_id == asset_id)
            if status is not None:
                query = query.where(CaptionReviewItem.status == status)
            if language is not None:
                query = query.where(CaptionReviewItem.language == language)
            return [_to_response(row) for row in session.execute(query).scalars()]

    def approve(
        self,
        review_item_id: str,
        payload: CaptionReviewDecision,
    ) -> CaptionReviewItemResponse:
        with self._session_factory() as session:
            row = self._require_row(session, review_item_id)
            require_low_confidence_approval_evidence(
                review_item_id=review_item_id,
                cue=_row_cue(row),
                evidence=_row_audio_evidence(row),
                decision=payload,
            )
            row.status = "approved"
            row.reviewed_text = row.reviewed_text or row.original_text
            row.reviewer_note = payload.reviewer_note
            row.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(row)
            return _to_response(row)

    def edit(self, review_item_id: str, payload: CaptionReviewEdit) -> CaptionReviewItemResponse:
        with self._session_factory() as session:
            row = self._require_row(session, review_item_id)
            if (
                payload.start_seconds is not None
                and row.language != "en"
                and row.translation_source_start_seconds is None
            ):
                sources = [
                    _row_cue(source).model_copy(
                        update={"text": source.reviewed_text or source.original_text}
                    )
                    for source in session.execute(
                        select(CaptionReviewItem).where(
                            CaptionReviewItem.asset_id == row.asset_id,
                            CaptionReviewItem.language == "en",
                        )
                    ).scalars()
                ]
                window = legacy_translation_source_window(_row_cue(row), row.language, sources)
                if window is not None:
                    row.translation_source_start_seconds, row.translation_source_end_seconds = (
                        window
                    )
            changed = edited_caption_cue(
                _row_cue(row), payload, _row_audio_evidence(row), review_item_id
            )
            row.start_seconds = changed.start_seconds
            row.end_seconds = changed.end_seconds
            row.status = "edited"
            row.reviewed_text = payload.text
            row.reviewer_note = payload.reviewer_note
            row.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(row)
            return _to_response(row)

    def reject(
        self,
        review_item_id: str,
        payload: CaptionReviewDecision,
    ) -> CaptionReviewItemResponse:
        with self._session_factory() as session:
            row = self._require_row(session, review_item_id)
            row.status = "rejected"
            row.reviewed_text = None
            row.reviewer_note = payload.reviewer_note
            row.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(row)
            return _to_response(row)

    @staticmethod
    def _require_row(session: Session, review_item_id: str) -> CaptionReviewItem:
        row = session.get(CaptionReviewItem, review_item_id)
        if row is None:
            raise CaptionReviewItemNotFoundError(review_item_id)
        return row


def _to_response(row: CaptionReviewItem) -> CaptionReviewItemResponse:
    return CaptionReviewItemResponse(
        review_item_id=row.review_item_id,
        asset_id=row.asset_id,
        cue=_row_cue(row),
        language=row.language,
        translation_source_start_seconds=row.translation_source_start_seconds,
        translation_source_end_seconds=row.translation_source_end_seconds,
        status=row.status,  # type: ignore[arg-type]
        original_text=row.original_text,
        reviewed_text=row.reviewed_text,
        low_confidence=row.low_confidence,
        audio_evidence_available=row.audio_evidence_path is not None,
        reviewer_note=row.reviewer_note,
        created_at=_as_aware(row.created_at),
        updated_at=_as_aware(row.updated_at),
    )


def _row_cue(row: CaptionReviewItem) -> CaptionCue:
    return CaptionCue(
        cue_id=row.cue_id,
        start_seconds=row.start_seconds,
        end_seconds=row.end_seconds,
        text=row.original_text,
        confidence=row.confidence,
        low_confidence=row.low_confidence,
    )


def _row_audio_evidence(
    row: CaptionReviewItem,
) -> CaptionReviewAudioEvidence | None:
    if row.audio_evidence_path is None:
        return None
    if (
        row.audio_evidence_start_seconds is None
        or row.audio_evidence_sha256 is None
        or row.audio_evidence_bytes is None
    ):
        raise CaptionReviewAudioEvidenceRequiredError(
            row.review_item_id,
            "stored evidence metadata is incomplete",
        )
    return CaptionReviewAudioEvidence(
        source_path=row.audio_evidence_path,
        source_start_seconds=row.audio_evidence_start_seconds,
        source_sha256=row.audio_evidence_sha256,
        source_bytes=row.audio_evidence_bytes,
    )


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class OfflineCaptionJob(Base):
    """Durable offline-captioning job for one published recording (K3)."""

    __tablename__ = "offline_caption_jobs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'awaiting_review', 'complete', 'failed', 'cancelled')",
            name="offline_caption_jobs_state_check",
        ),
        CheckConstraint("attempts >= 0", name="offline_caption_jobs_attempts_check"),
        CheckConstraint(
            "cue_count >= 0 AND published_cue_count >= 0",
            name="offline_caption_jobs_cue_count_check",
        ),
    )

    job_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    package_dir: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cue_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    published_cue_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# Partial-unique: at most one ACTIVE (pending/awaiting_review) job per asset
# -- the DB-level guard for the race audit finding 3 named (two concurrent
# ``enqueue_offline_caption_job`` calls both passing the app-level
# check-then-insert). Declared as a module-level Index (not inside
# __table_args__) so the postgresql_where / sqlite_where dialect kwargs can
# reference the real column object -- mirrors
# civiccast/control_room/models.py's ControlRoomSessionDb pattern. Also
# created directly in 0075_offline_caption_jobs so an already-migrated DB
# picks it up via `alembic upgrade`, not just `Base.metadata.create_all`
# (which only fresh test/dev databases go through).
Index(
    "ix_offline_caption_jobs_one_active_per_asset",
    OfflineCaptionJob.asset_id,
    unique=True,
    postgresql_where=OfflineCaptionJob.state.in_(OFFLINE_CAPTION_JOB_ACTIVE_STATES),
    sqlite_where=OfflineCaptionJob.state.in_(OFFLINE_CAPTION_JOB_ACTIVE_STATES),
)


class PostgresOfflineCaptionJobStore:
    """DB-backed offline caption job queue (Postgres or managed SQLite)."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def enqueue(self, record: OfflineCaptionJobRecord) -> OfflineCaptionJobRecord:
        with self._session_factory() as session:
            session.add(_job_row(record))
            try:
                session.commit()
            except IntegrityError as exc:
                # The DB-level one-active-job-per-asset partial-unique index
                # (ix_offline_caption_jobs_one_active_per_asset,
                # 0075_offline_caption_jobs) lost the race against a
                # concurrent enqueue for this asset: roll back this
                # half-committed insert and surface a clean, catchable
                # conflict instead of a raw IntegrityError.
                session.rollback()
                raise OfflineCaptionJobConflictError(record.asset_id) from exc
        return record

    def save(
        self, record: OfflineCaptionJobRecord, *, preserve_cancelled: bool = False
    ) -> OfflineCaptionJobRecord:
        with self._session_factory() as session:
            row = session.get(OfflineCaptionJob, record.job_id, with_for_update=True)
            if preserve_cancelled and row is not None and row.state == "cancelled":
                return _to_job_record(row)
            if row is None:
                session.add(_job_row(record))
            elif preserve_cancelled:
                session.execute(
                    update(OfflineCaptionJob)
                    .where(OfflineCaptionJob.job_id == record.job_id)
                    .where(OfflineCaptionJob.state != "cancelled")
                    .values(
                        state=record.state,
                        attempts=record.attempts,
                        next_attempt_at=record.next_attempt_at,
                        cue_count=record.cue_count,
                        published_cue_count=record.published_cue_count,
                        last_error=record.last_error,
                        updated_at=record.updated_at,
                    )
                )
            else:
                row.state = record.state
                row.attempts = record.attempts
                row.next_attempt_at = record.next_attempt_at
                row.cue_count = record.cue_count
                row.published_cue_count = record.published_cue_count
                row.last_error = record.last_error
                row.updated_at = record.updated_at
            try:
                session.commit()
            except IntegrityError as exc:
                # Mirrors enqueue()'s guard above: an UPDATE that moves this
                # row into an active state (e.g. a manual retry --
                # civiccast/captions/router.py retry_offline_caption_job --
                # resetting a failed job back to pending) can lose the same
                # DB-level one-active-job-per-asset partial-unique index
                # race (ix_offline_caption_jobs_one_active_per_asset,
                # 0075_offline_caption_jobs) that enqueue() already guards.
                # The router's active_for_asset pre-check closes most of
                # that window, but not the TOCTOU gap between the check and
                # this write -- roll back and surface the same clean,
                # catchable conflict instead of a raw IntegrityError.
                session.rollback()
                raise OfflineCaptionJobConflictError(record.asset_id) from exc
            if preserve_cancelled and row is not None:
                session.refresh(row)
                return _to_job_record(row)
        return record

    def get(self, job_id: str) -> OfflineCaptionJobRecord | None:
        with self._session_factory() as session:
            row = session.get(OfflineCaptionJob, job_id)
            return _to_job_record(row) if row is not None else None

    def active_for_asset(self, asset_id: str) -> OfflineCaptionJobRecord | None:
        with self._session_factory() as session:
            row = session.execute(
                select(OfflineCaptionJob)
                .where(OfflineCaptionJob.asset_id == asset_id)
                .where(OfflineCaptionJob.state.in_(OFFLINE_CAPTION_JOB_ACTIVE_STATES))
                .order_by(OfflineCaptionJob.created_at.asc(), OfflineCaptionJob.job_id.asc())
                .limit(1)
            ).scalar_one_or_none()
            return _to_job_record(row) if row is not None else None

    def due(
        self,
        *,
        now: datetime,
        states: Sequence[str] = OFFLINE_CAPTION_JOB_ACTIVE_STATES,
    ) -> list[OfflineCaptionJobRecord]:
        with self._session_factory() as session:
            rows = session.execute(
                select(OfflineCaptionJob)
                .where(OfflineCaptionJob.state.in_(tuple(states)))
                .where(
                    (OfflineCaptionJob.next_attempt_at.is_(None))
                    | (OfflineCaptionJob.next_attempt_at <= now)
                )
                .order_by(OfflineCaptionJob.created_at.asc(), OfflineCaptionJob.job_id.asc())
            ).scalars()
            return [_to_job_record(row) for row in rows]

    def list(
        self,
        *,
        asset_id: str | None = None,
        state: str | None = None,
    ) -> list[OfflineCaptionJobRecord]:
        with self._session_factory() as session:
            query = select(OfflineCaptionJob).order_by(
                OfflineCaptionJob.created_at.asc(), OfflineCaptionJob.job_id.asc()
            )
            if asset_id is not None:
                query = query.where(OfflineCaptionJob.asset_id == asset_id)
            if state is not None:
                query = query.where(OfflineCaptionJob.state == state)
            return [_to_job_record(row) for row in session.execute(query).scalars()]


def _job_row(record: OfflineCaptionJobRecord) -> OfflineCaptionJob:
    return OfflineCaptionJob(
        job_id=record.job_id,
        asset_id=record.asset_id,
        source_path=record.source_path,
        package_dir=record.package_dir,
        state=record.state,
        attempts=record.attempts,
        next_attempt_at=record.next_attempt_at,
        cue_count=record.cue_count,
        published_cue_count=record.published_cue_count,
        last_error=record.last_error,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _to_job_record(row: OfflineCaptionJob) -> OfflineCaptionJobRecord:
    return OfflineCaptionJobRecord(
        job_id=row.job_id,
        asset_id=row.asset_id,
        source_path=row.source_path,
        package_dir=row.package_dir,
        state=row.state,  # type: ignore[arg-type]
        attempts=row.attempts,
        next_attempt_at=(
            _as_aware(row.next_attempt_at) if row.next_attempt_at is not None else None
        ),
        cue_count=row.cue_count,
        published_cue_count=row.published_cue_count,
        last_error=row.last_error,
        created_at=_as_aware(row.created_at),
        updated_at=_as_aware(row.updated_at),
    )
