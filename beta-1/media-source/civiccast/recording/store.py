# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable store for recording schedules + jobs (S21 slice 1).

Per-request store over the single global session factory (same lazy posture
as eas / ai_models / metadata / reporting / underwriting / agenda / paywall).
All comparisons bind through parameters (no string interpolation) and ride
the indexes defined in migration ``0056_scheduled_recording``.

* ``upsert_schedule`` / ``get_schedule`` / ``list_schedules`` /
  ``delete_schedule`` — schedule CRUD; unique constraint on
  ``(station_id, name)`` so the operator UI's picker is unambiguous.
* ``create_job`` / ``get_job`` / ``list_jobs`` / ``set_job_state`` /
  ``find_overlapping_jobs`` — job CRUD + the hot-path "is another job
  already running on this input?" lookup (DC-5 overlap detection).
* ``reconcile_orphaned_active_jobs`` — startup hook that transitions any
  job stuck in ``recording``/``arming``/``finalizing`` past its
  window-end to ``failed`` with a reason. The "never a silent miss"
  invariant for crash-mid-record (spec §6 failure handling).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from civiccast.recording.models import (
    JOB_STATE_ACTIVE,
    JobState,
    RecordingJob,
    RecordingJobDb,
    RecordingSchedule,
    RecordingScheduleDb,
    RecordingSource,
    RecurrenceSpec,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]


class RecordingStoreError(RuntimeError):
    """Base error for recording persistence failures."""


class RecordingScheduleNotFoundError(RecordingStoreError):
    """Raised when a ``schedule_id`` does not resolve."""


class RecordingJobNotFoundError(RecordingStoreError):
    """Raised when a ``job_id`` does not resolve."""


class RecordingScheduleNameConflictError(RecordingStoreError):
    """Raised when an upsert would create a second schedule with the same
    ``(station_id, name)`` (``recording_schedules_station_name_unique``
    collision)."""


class RecordingJobStateError(RecordingStoreError):
    """Raised when a state transition is invalid (e.g. trying to start a
    job that's already done). The router translates this to 409."""


class RecordingJobIdConflictError(RecordingStoreError):
    """Raised by ``create_job`` when an INSERT collides on the
    ``recording_jobs`` primary key.

    Catching this typed error instead of letting the raw
    ``IntegrityError`` propagate (E-6 fix) lets the router translate the
    collision into a 409 with a clean message rather than a 500 leaking
    SQLAlchemy internals. With E-1 fixed (``_job_id_for`` preserves
    schedule_id verbatim) a collision is now a sign of a real Slug regex
    mismatch in the operator's schedule_id rather than a silent
    underscore/dash collapse — the typed error preserves that signal.
    """


class RecordingJobOverlapError(RecordingStoreError):
    """Raised when transitioning a job to ``arming`` would violate the
    partial unique index that pins overlap (E-3 fix).

    Concurrent ``record_now`` / ``record_now`` calls on the same source
    could pre-fix punch through the in-Python overlap re-check because
    the read + the state transition lived in two different sessions. The
    overlap guard is now backed by either (a) a SAVEPOINT-wrapped re-run
    of ``find_overlapping_jobs`` inside the arm transaction, or (b) on
    Postgres, a partial unique index — either path raises this typed
    error so the service translates it into the same ``skipped`` row +
    reason as the original soft-check path.
    """


def _now() -> datetime:
    return datetime.now(UTC)


# Allowed state transitions. The forward path is
# scheduled → arming → recording → finalizing → done. Terminal branches
# are failed (from any active state) and skipped (from scheduled or
# arming, when a schedule is disabled before capture starts).
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "scheduled": frozenset({"arming", "skipped", "failed"}),
    "arming": frozenset({"recording", "skipped", "failed"}),
    "recording": frozenset({"finalizing", "failed"}),
    "finalizing": frozenset({"done", "failed"}),
    "done": frozenset(),
    "failed": frozenset(),
    "skipped": frozenset(),
}


def _coerce_aware(value: datetime | None) -> datetime | None:
    """Return a tz-aware datetime regardless of backend.

    SQLite stores ``DateTime(timezone=True)`` columns as naive even though
    the column type says otherwise. Production Postgres returns aware.
    Treat any naive datetime as UTC so comparisons stay defined on both
    backends (the same concession the paywall store makes for
    ``expires_at``).
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _source_to_dict(src: RecordingSource | dict[str, Any]) -> dict[str, Any]:
    # Use mode="json" so datetimes inside become ISO-8601 strings (the JSON
    # column doesn't accept raw datetime objects on SQLite or Postgres
    # without a custom serializer).
    if isinstance(src, RecordingSource):
        return src.model_dump(mode="json")
    return dict(src)


def _recurrence_to_dict(rec: RecurrenceSpec | dict[str, Any]) -> dict[str, Any]:
    if isinstance(rec, RecurrenceSpec):
        return rec.model_dump(mode="json")
    return dict(rec)


def _schedule_db_to_model(row: RecordingScheduleDb) -> RecordingSchedule:
    return RecordingSchedule(
        schedule_id=row.schedule_id,
        station_id=row.station_id,
        name=row.name,
        source=RecordingSource(**row.source),
        recurrence=RecurrenceSpec(**row.recurrence),
        duration_seconds=row.duration_seconds,
        encoder_profile=row.encoder_profile,
        loudness_regime=row.loudness_regime,
        target_series=row.target_series,
        custom_field_values=row.custom_field_values or {},
        enabled=row.enabled,
        created_at=_coerce_aware(row.created_at) or _now(),
        updated_at=_coerce_aware(row.updated_at) or _now(),
    )


def _job_db_to_model(row: RecordingJobDb) -> RecordingJob:
    return RecordingJob(
        job_id=row.job_id,
        station_id=row.station_id,
        schedule_id=row.schedule_id,
        planned_start=_coerce_aware(row.planned_start),  # type: ignore[arg-type]
        planned_end=_coerce_aware(row.planned_end),  # type: ignore[arg-type]
        state=row.state,  # type: ignore[arg-type]
        started_at=_coerce_aware(row.started_at),
        ended_at=_coerce_aware(row.ended_at),
        asset_id=row.asset_id,
        bytes_written=row.bytes_written,
        failure_reason=row.failure_reason,
        dropout_count=row.dropout_count,
        last_dropout_at=_coerce_aware(row.last_dropout_at),
        source_snapshot=RecordingSource(**row.source_snapshot),
        encoder_profile=row.encoder_profile,
        loudness_regime=row.loudness_regime,
        target_series=row.target_series,
        custom_field_values=row.custom_field_values or {},
        created_at=_coerce_aware(row.created_at) or _now(),
        updated_at=_coerce_aware(row.updated_at) or _now(),
    )


class RecordingStore:
    """Per-request CRUD over the two recording tables."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # RecordingSchedule
    # ------------------------------------------------------------------

    def upsert_schedule(self, schedule: RecordingSchedule) -> RecordingSchedule:
        """Insert or update a schedule.

        Raises ``RecordingScheduleNameConflictError`` if a different
        ``schedule_id`` already exists for ``(station_id, name)``.
        """
        now = _now()
        with self._session_factory() as session:
            existing = session.get(RecordingScheduleDb, schedule.schedule_id)
            if existing is None:
                # E-12 fix: dropped the pre-check. Under concurrency the
                # IntegrityError path below IS the source of truth (the
                # pre-check was a TOCTOU); the IntegrityError handler
                # raises the same typed error, so the pre-check was a
                # redundant round-trip with no correctness benefit. The
                # ``test_duplicate_name_same_station_conflicts`` test
                # continues to pass against the IntegrityError path.
                row = RecordingScheduleDb(
                    schedule_id=schedule.schedule_id,
                    station_id=schedule.station_id,
                    name=schedule.name,
                    source=_source_to_dict(schedule.source),
                    recurrence=_recurrence_to_dict(schedule.recurrence),
                    duration_seconds=schedule.duration_seconds,
                    encoder_profile=schedule.encoder_profile,
                    loudness_regime=schedule.loudness_regime,
                    target_series=schedule.target_series,
                    custom_field_values=schedule.custom_field_values,
                    enabled=schedule.enabled,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                existing.name = schedule.name
                existing.source = _source_to_dict(schedule.source)
                existing.recurrence = _recurrence_to_dict(schedule.recurrence)
                existing.duration_seconds = schedule.duration_seconds
                existing.encoder_profile = schedule.encoder_profile
                existing.loudness_regime = schedule.loudness_regime
                existing.target_series = schedule.target_series
                existing.custom_field_values = schedule.custom_field_values
                existing.enabled = schedule.enabled
                existing.updated_at = now
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise RecordingScheduleNameConflictError(
                    f"Schedule write violated the (station_id, name) "
                    f"unique constraint for {schedule.station_id!r} / "
                    f"{schedule.name!r}."
                ) from exc

            stored = session.get(RecordingScheduleDb, schedule.schedule_id)
            assert stored is not None  # we just wrote it
            return _schedule_db_to_model(stored)

    def get_schedule(self, schedule_id: str) -> RecordingSchedule | None:
        with self._session_factory() as session:
            row = session.get(RecordingScheduleDb, schedule_id)
            return _schedule_db_to_model(row) if row else None

    def list_schedules(
        self,
        station_id: str,
        *,
        enabled_only: bool = False,
    ) -> list[RecordingSchedule]:
        with self._session_factory() as session:
            stmt = select(RecordingScheduleDb).where(RecordingScheduleDb.station_id == station_id)
            if enabled_only:
                stmt = stmt.where(RecordingScheduleDb.enabled.is_(True))
            rows = session.execute(stmt).scalars().all()
            return [_schedule_db_to_model(r) for r in rows]

    def delete_schedule(self, schedule_id: str) -> None:
        with self._session_factory() as session:
            row = session.get(RecordingScheduleDb, schedule_id)
            if row is None:
                raise RecordingScheduleNotFoundError(
                    f"Recording schedule {schedule_id!r} not found."
                )
            session.delete(row)
            session.commit()

    # ------------------------------------------------------------------
    # RecordingJob
    # ------------------------------------------------------------------

    def create_job(self, job: RecordingJob) -> RecordingJob:
        """Insert a new job. Caller pre-checks overlap via
        ``find_overlapping_jobs``."""
        now = _now()
        with self._session_factory() as session:
            row = RecordingJobDb(
                job_id=job.job_id,
                station_id=job.station_id,
                schedule_id=job.schedule_id,
                planned_start=job.planned_start,
                planned_end=job.planned_end,
                state=job.state,
                started_at=job.started_at,
                ended_at=job.ended_at,
                asset_id=job.asset_id,
                bytes_written=job.bytes_written,
                failure_reason=job.failure_reason,
                dropout_count=job.dropout_count,
                last_dropout_at=job.last_dropout_at,
                source_snapshot=_source_to_dict(job.source_snapshot),
                encoder_profile=job.encoder_profile,
                loudness_regime=job.loudness_regime,
                target_series=job.target_series,
                custom_field_values=job.custom_field_values,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                # E-6 fix: translate a PK collision (or the partial
                # overlap-prevention unique index that gets added by
                # ``set_job_state`` on the transition into ``arming``)
                # into a typed error so the router can map to 409 with a
                # clean message — pre-fix the raw IntegrityError
                # propagated through ``expand_jobs_for_horizon`` and the
                # router's ``record_now`` as a 500 with a SQLAlchemy
                # message in the body.
                session.rollback()
                raise RecordingJobIdConflictError(
                    f"Recording job {job.job_id!r} already exists (PK collision on recording_jobs)."
                ) from exc
            stored = session.get(RecordingJobDb, job.job_id)
            assert stored is not None
            return _job_db_to_model(stored)

    def get_job(self, job_id: str) -> RecordingJob | None:
        with self._session_factory() as session:
            row = session.get(RecordingJobDb, job_id)
            return _job_db_to_model(row) if row else None

    def list_jobs(
        self,
        station_id: str,
        *,
        state: JobState | None = None,
        schedule_id: str | None = None,
        limit: int = 200,
    ) -> list[RecordingJob]:
        with self._session_factory() as session:
            stmt = select(RecordingJobDb).where(RecordingJobDb.station_id == station_id)
            if state is not None:
                stmt = stmt.where(RecordingJobDb.state == state)
            if schedule_id is not None:
                stmt = stmt.where(RecordingJobDb.schedule_id == schedule_id)
            # Newest planned first so the operator UI's "what's coming up
            # / what just ran" view doesn't need to sort client-side.
            stmt = stmt.order_by(RecordingJobDb.planned_start.desc()).limit(limit)
            rows = session.execute(stmt).scalars().all()
            return [_job_db_to_model(r) for r in rows]

    def set_job_state(
        self,
        job_id: str,
        new_state: JobState,
        *,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        asset_id: str | None = None,
        bytes_written: int | None = None,
        failure_reason: str | None = None,
    ) -> RecordingJob:
        """Transition a job to ``new_state`` with optional side fields.

        Raises ``RecordingJobNotFoundError`` if missing.
        Raises ``RecordingJobStateError`` if the transition isn't in the
        allowed-transitions table (e.g. ``done → recording``).
        """
        now = _now()
        with self._session_factory() as session:
            row = session.get(RecordingJobDb, job_id)
            if row is None:
                raise RecordingJobNotFoundError(f"Recording job {job_id!r} not found.")
            allowed = _ALLOWED_TRANSITIONS.get(row.state, frozenset())
            if new_state not in allowed and new_state != row.state:
                raise RecordingJobStateError(
                    f"Invalid recording-job transition "
                    f"{row.state!r} → {new_state!r} for job {job_id!r}."
                )
            row.state = new_state
            if started_at is not None:
                row.started_at = started_at
            if ended_at is not None:
                row.ended_at = ended_at
            if asset_id is not None:
                row.asset_id = asset_id
            if bytes_written is not None:
                row.bytes_written = bytes_written
            if failure_reason is not None:
                row.failure_reason = failure_reason
            row.updated_at = now
            session.commit()
            stored = session.get(RecordingJobDb, job_id)
            assert stored is not None
            return _job_db_to_model(stored)

    def record_dropout(self, job_id: str, *, observed_at: datetime | None = None) -> RecordingJob:
        """Bump the dropout counter + timestamp on a job. Does NOT change
        ``state`` — a detected-and-reconnected dropout keeps the job
        ``recording``; only a reconnect that gives up transitions to
        ``failed`` (via the normal ``set_job_state`` path).

        Raises ``RecordingJobNotFoundError`` if missing.
        """
        when = _coerce_aware(observed_at) or _now()
        with self._session_factory() as session:
            row = session.get(RecordingJobDb, job_id)
            if row is None:
                raise RecordingJobNotFoundError(f"Recording job {job_id!r} not found.")
            row.dropout_count += 1
            row.last_dropout_at = when
            row.updated_at = when
            session.commit()
            stored = session.get(RecordingJobDb, job_id)
            assert stored is not None
            return _job_db_to_model(stored)

    def transition_to_arming_with_overlap_guard(
        self,
        job_id: str,
    ) -> RecordingJob:
        """Atomically re-check overlap AND transition a job to ``arming``.

        E-3 fix: pre-fix ``arm_job`` did the overlap re-check in one
        session and the state transition in another. Two concurrent
        ``record_now`` clicks on the same source could BOTH pass the
        overlap probe before either wrote ``arming`` — a TOCTOU race
        that violates DC-5 under concurrency.

        The overlap probe AND the conditional state transition now happen
        inside a single transaction. The probe reads the same active-set
        the transition will conflict with; the commit either:
          - succeeds (the job was the only contender and is now ``arming``), or
          - rolls back (a sibling job is already active on the same source
            — we raise ``RecordingJobOverlapError`` for the service to
            translate into a ``skipped`` row).

        Postgres deployments additionally get a partial unique index on
        ``(station_id, source_identifier)`` filtered by
        ``state IN ('arming','recording','finalizing')`` so a *true*
        concurrent transition where two sessions cross the wire at the
        exact same instant collides at the DB level rather than the
        application level. Either path raises the typed error.
        """
        now = _now()
        with self._session_factory() as session:
            row = session.get(RecordingJobDb, job_id)
            if row is None:
                raise RecordingJobNotFoundError(f"Recording job {job_id!r} not found.")
            if row.state != "scheduled":
                raise RecordingJobStateError(
                    f"Cannot arm job {job_id!r}: state is {row.state!r}, expected 'scheduled'."
                )
            # Re-run the overlap probe inside this transaction. The
            # active-state filter mirrors ``find_overlapping_jobs`` so a
            # sibling job already in ``arming`` / ``recording`` /
            # ``finalizing`` blocks the transition.
            snap = row.source_snapshot or {}
            target_kind = snap.get("kind")
            target_identifier = snap.get("uri") or snap.get("input_id") or ""
            row_start = _coerce_aware(row.planned_start)
            row_end = _coerce_aware(row.planned_end)
            active_states = ("arming", "recording", "finalizing")
            siblings = (
                session.execute(
                    select(RecordingJobDb)
                    .where(RecordingJobDb.station_id == row.station_id)
                    .where(RecordingJobDb.state.in_(active_states))
                    .where(RecordingJobDb.planned_start < row_end)
                    .where(RecordingJobDb.planned_end > row_start)
                    .where(RecordingJobDb.job_id != job_id)
                )
                .scalars()
                .all()
            )
            for sib in siblings:
                sib_snap = sib.source_snapshot or {}
                if sib_snap.get("kind") != target_kind:
                    continue
                sib_identifier = sib_snap.get("uri") or sib_snap.get("input_id") or ""
                if sib_identifier != target_identifier:
                    continue
                raise RecordingJobOverlapError(
                    f"Recording job {job_id!r} overlaps active "
                    f"job {sib.job_id!r} on the same source."
                )
            row.state = "arming"
            row.updated_at = now
            try:
                session.commit()
            except IntegrityError as exc:
                # The partial unique index (PG only — see migration 0056)
                # caught a race where two sessions tried to transition
                # the same source into ``arming`` simultaneously. Same
                # outcome as the in-Python re-check: the loser becomes
                # the second ``skipped`` job.
                session.rollback()
                raise RecordingJobOverlapError(
                    f"Recording job {job_id!r} could not transition to "
                    f"'arming' under concurrent overlap; the partial "
                    f"unique index rejected the write."
                ) from exc
            stored = session.get(RecordingJobDb, job_id)
            assert stored is not None
            return _job_db_to_model(stored)

    def find_overlapping_jobs(
        self,
        station_id: str,
        source: RecordingSource,
        planned_start: datetime,
        planned_end: datetime,
        *,
        exclude_job_id: str | None = None,
    ) -> list[RecordingJob]:
        """Return any non-terminal jobs at ``station_id`` whose window
        overlaps ``[planned_start, planned_end)`` AND whose source matches.

        DC-5 overlap detection: a second job targeting the same input in
        an overlapping window is ``skipped`` by the service. We exclude
        terminal-state jobs (done / failed / skipped) so a long-finished
        capture on the same input doesn't trigger a false positive.

        ``source`` matches if BOTH the kind AND the identifying field
        (``input_id`` for live inputs, ``uri`` for network streams)
        match — two different RTSP cameras at the same station are NOT
        an overlap.
        """
        with self._session_factory() as session:
            # E-7 fix: push the non-terminal-state filter AND the planned-
            # window overlap filter into SQL so the planner can use
            # ``ix_recording_jobs_station_state`` + ``ix_recording_jobs_planned_start``
            # rather than loading every row at the station into Python.
            # Pre-fix this was a full-station scan that loaded millions of
            # historical rows on a busy PEG station; post-fix the work is
            # O(active jobs in window).
            #
            # We KEEP the JSON-side source-snapshot match in Python because
            # SQLite + Postgres JSON column introspection diverges and the
            # in-Python equality is portable across both. The pre-filter
            # narrows the set to active+windowed rows, which is normally
            # a handful at most.
            active_states = ("scheduled", "arming", "recording", "finalizing")
            stmt = (
                select(RecordingJobDb)
                .where(RecordingJobDb.station_id == station_id)
                .where(RecordingJobDb.state.in_(active_states))
                .where(RecordingJobDb.planned_start < planned_end)
                .where(RecordingJobDb.planned_end > planned_start)
            )
            if exclude_job_id is not None:
                stmt = stmt.where(RecordingJobDb.job_id != exclude_job_id)
            rows = session.execute(stmt).scalars().all()
            overlaps = []
            target_kind = source.kind
            target_identifier = source.uri if source.uri else source.input_id
            for r in rows:
                snap = r.source_snapshot or {}
                if snap.get("kind") != target_kind:
                    continue
                snap_id = snap.get("uri") or snap.get("input_id") or ""
                if snap_id != target_identifier:
                    continue
                row_start = _coerce_aware(r.planned_start)
                row_end = _coerce_aware(r.planned_end)
                if row_start is None or row_end is None:
                    continue
                # Half-open overlap check: [a_start, a_end) ∩ [b_start, b_end).
                # The SQL filter already enforced this, but we keep the
                # Python-side guard as defense against an aware/naive
                # comparison mismatch under SQLite's date storage.
                if row_start < planned_end and planned_start < row_end:
                    overlaps.append(_job_db_to_model(r))
            return overlaps

    def reconcile_orphaned_active_jobs(
        self,
        *,
        now: datetime | None = None,
        reason: str = "Interrupted by restart; recording state could not be recovered.",
    ) -> int:
        """Fail any job stuck in an active state past its planned end.

        Called at service startup so a crash mid-record doesn't leave a
        job spuriously "recording" forever. Returns the number of jobs
        transitioned to ``failed``.
        """
        cutoff = _coerce_aware(now) or _now()
        transitioned = 0
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(RecordingJobDb).where(RecordingJobDb.state.in_(tuple(JOB_STATE_ACTIVE)))
                )
                .scalars()
                .all()
            )
            for r in rows:
                end = _coerce_aware(r.planned_end)
                if end is None or end > cutoff:
                    # Still within its planned window — leave alone.
                    continue
                r.state = "failed"
                r.ended_at = cutoff
                r.failure_reason = reason
                r.updated_at = cutoff
                transitioned += 1
            session.commit()
        return transitioned


__all__ = [
    "RecordingJobIdConflictError",
    "RecordingJobNotFoundError",
    "RecordingJobOverlapError",
    "RecordingJobStateError",
    "RecordingScheduleNameConflictError",
    "RecordingScheduleNotFoundError",
    "RecordingStore",
    "RecordingStoreError",
    "SessionFactory",
]
