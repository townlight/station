# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable storage for the summary generation job queue.

:class:`PostgresSummaryGenerationJobStore` implements
:class:`~civiccast.summary.job.SummaryGenerationJobStore` over
``summary_generation_jobs`` (migration ``0081_summary_generation_jobs``), the same
raw-SQL-plus-schema-prefix idiom :class:`~civiccast.summary.store.PostgresSummaryStore`
already uses for the summaries/sourced_claims tables — kept as raw SQL text rather than
an ORM model so this module matches the rest of ``civiccast.summary`` (whose migration
owns the tables directly) instead of mixing persistence styles within one feature.
Despite the name (matching the repo's other Postgres-backed stores), it also runs on
the managed SQLite path, same as every other store in this pattern.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, cast

from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from civiccast.captions import CaptionCue
from civiccast.summary.job import (
    SUMMARY_JOB_ACTIVE_STATES,
    SummaryGenerationJobConflictError,
    SummaryGenerationJobRecord,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]


class PostgresSummaryGenerationJobStore:
    """SQLAlchemy-backed summary generation job queue."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def enqueue(self, record: SummaryGenerationJobRecord) -> SummaryGenerationJobRecord:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            try:
                session.execute(
                    text(
                        f"INSERT INTO {table}summary_generation_jobs "  # nosec B608
                        "(job_id, meeting_id, cues_json, state, attempts, "
                        "next_attempt_at, summary_id, last_error, created_at, updated_at) "
                        "VALUES (:job_id, :meeting_id, :cues_json, :state, :attempts, "
                        ":next_attempt_at, :summary_id, :last_error, :created_at, :updated_at)"
                    ),
                    _params(record),
                )
                session.commit()
            except IntegrityError as exc:
                # The DB-level one-active-job-per-meeting partial-unique index
                # (ix_summary_generation_jobs_one_active_per_meeting,
                # 0081_summary_generation_jobs) lost the race against a concurrent
                # enqueue for this meeting: roll back and surface a clean,
                # catchable conflict instead of a raw IntegrityError.
                session.rollback()
                raise SummaryGenerationJobConflictError(record.meeting_id) from exc
        return record

    def save(self, record: SummaryGenerationJobRecord) -> SummaryGenerationJobRecord:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            existing = session.execute(
                text(
                    f"SELECT job_id FROM {table}summary_generation_jobs "  # nosec B608
                    "WHERE job_id = :job_id"
                ),
                {"job_id": record.job_id},
            ).first()
            try:
                if existing is None:
                    session.execute(
                        text(
                            f"INSERT INTO {table}summary_generation_jobs "  # nosec B608
                            "(job_id, meeting_id, cues_json, state, attempts, "
                            "next_attempt_at, summary_id, last_error, created_at, updated_at) "
                            "VALUES (:job_id, :meeting_id, :cues_json, :state, :attempts, "
                            ":next_attempt_at, :summary_id, :last_error, :created_at, "
                            ":updated_at)"
                        ),
                        _params(record),
                    )
                else:
                    session.execute(
                        text(
                            f"UPDATE {table}summary_generation_jobs SET "  # nosec B608
                            "state = :state, attempts = :attempts, "
                            "next_attempt_at = :next_attempt_at, summary_id = :summary_id, "
                            "last_error = :last_error, updated_at = :updated_at "
                            "WHERE job_id = :job_id"
                        ),
                        _params(record),
                    )
                session.commit()
            except IntegrityError as exc:
                # Mirrors enqueue()'s guard: an UPDATE that moves this row back into
                # an active state (a manual retry resetting a failed job to pending)
                # can lose the same partial-unique-index race enqueue() guards. The
                # router's active_for_meeting pre-check closes most of that window,
                # not the TOCTOU gap between the check and this write.
                session.rollback()
                raise SummaryGenerationJobConflictError(record.meeting_id) from exc
        return record

    def get(self, job_id: str) -> SummaryGenerationJobRecord | None:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            row = session.execute(
                text(
                    f"SELECT * FROM {table}summary_generation_jobs "  # nosec B608
                    "WHERE job_id = :job_id"
                ),
                {"job_id": job_id},
            ).first()
            return _to_job_record(row) if row is not None else None

    def active_for_meeting(self, meeting_id: str) -> SummaryGenerationJobRecord | None:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            row = session.execute(
                text(
                    f"SELECT * FROM {table}summary_generation_jobs "  # nosec B608
                    "WHERE meeting_id = :meeting_id AND state IN :active_states "
                    "ORDER BY created_at ASC, job_id ASC LIMIT 1"
                ).bindparams(_states_bindparam("active_states")),
                {"meeting_id": meeting_id, "active_states": list(SUMMARY_JOB_ACTIVE_STATES)},
            ).first()
            return _to_job_record(row) if row is not None else None

    def due(
        self,
        *,
        now: datetime,
        states: Sequence[str] = SUMMARY_JOB_ACTIVE_STATES,
    ) -> list[SummaryGenerationJobRecord]:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            rows = session.execute(
                text(
                    f"SELECT * FROM {table}summary_generation_jobs "  # nosec B608
                    "WHERE state IN :states "
                    "AND (next_attempt_at IS NULL OR next_attempt_at <= :now) "
                    "ORDER BY created_at ASC, job_id ASC"
                ).bindparams(_states_bindparam("states")),
                {"states": list(states), "now": now},
            ).fetchall()
            return [_to_job_record(row) for row in rows]

    def list(
        self,
        *,
        meeting_id: str | None = None,
        state: str | None = None,
    ) -> list[SummaryGenerationJobRecord]:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            clauses = []
            params: dict[str, Any] = {}
            if meeting_id is not None:
                clauses.append("meeting_id = :meeting_id")
                params["meeting_id"] = meeting_id
            if state is not None:
                clauses.append("state = :state")
                params["state"] = state
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = session.execute(
                text(
                    f"SELECT * FROM {table}summary_generation_jobs{where} "  # nosec B608
                    "ORDER BY created_at ASC, job_id ASC"
                ),
                params,
            ).fetchall()
            return [_to_job_record(row) for row in rows]

    @staticmethod
    def _table_prefix(session: Session) -> str:
        bind = session.get_bind()
        return "" if bind.dialect.name == "sqlite" else "civiccast."


def _params(record: SummaryGenerationJobRecord) -> dict[str, Any]:
    return {
        "job_id": record.job_id,
        "meeting_id": record.meeting_id,
        "cues_json": json.dumps(
            [cue.model_dump(mode="json") for cue in record.cues], sort_keys=True
        ),
        "state": record.state,
        "attempts": record.attempts,
        "next_attempt_at": record.next_attempt_at,
        "summary_id": record.summary_id,
        "last_error": record.last_error,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _states_bindparam(name: str) -> Any:
    return bindparam(name, expanding=True)


def _to_job_record(row: object) -> SummaryGenerationJobRecord:
    row_any = cast(Any, row)
    return SummaryGenerationJobRecord.model_validate(
        {
            "job_id": row_any.job_id,
            "meeting_id": row_any.meeting_id,
            "cues": [CaptionCue.model_validate(cue) for cue in json.loads(row_any.cues_json)],
            "state": row_any.state,
            "attempts": row_any.attempts,
            "next_attempt_at": row_any.next_attempt_at,
            "summary_id": row_any.summary_id,
            "last_error": row_any.last_error,
            "created_at": row_any.created_at,
            "updated_at": row_any.updated_at,
        }
    )


__all__ = ["PostgresSummaryGenerationJobStore"]
