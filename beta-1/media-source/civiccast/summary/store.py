# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Persistence contracts for v0.6 sourced summaries."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from threading import RLock
from typing import Any, Protocol, cast

from sqlalchemy import bindparam, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from civiccast.summary.fingerprint import sha256_fingerprint
from civiccast.summary.minutes import edited_minutes, require_minutes_approval
from civiccast.summary.models import MinutesContent, OperatorApproval, SourcedClaim, SummaryDraft

SessionFactory = Callable[[], AbstractContextManager[Session]]


class SummaryStoreConflictError(RuntimeError):
    """Raised when a summary id already exists."""


class SummaryStoreNotFoundError(KeyError):
    """Raised when a summary id is missing."""


class SummaryStore(Protocol):
    def create_summary(self, summary: SummaryDraft) -> SummaryDraft: ...
    def get_summary(self, summary_id: str) -> SummaryDraft | None: ...
    def list_review_items(self) -> list[SummaryDraft]: ...
    def edit_summary(
        self,
        summary_id: str,
        *,
        narrative: str,
        expected_audit_fingerprint: str,
    ) -> SummaryDraft: ...
    def edit_minutes(
        self,
        summary_id: str,
        *,
        content: MinutesContent,
        operator_id: str,
        expected_audit_fingerprint: str,
    ) -> SummaryDraft: ...
    def approve_summary(
        self, approval: OperatorApproval, *, expected_audit_fingerprint: str
    ) -> SummaryDraft: ...
    def get_approval(self, summary_id: str) -> OperatorApproval | None: ...


class InMemorySummaryStore:
    """In-memory summary store used by tests and no-DB local runs."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._summaries: dict[str, SummaryDraft] = {}
        self._approvals: dict[str, OperatorApproval] = {}

    def create_summary(self, summary: SummaryDraft) -> SummaryDraft:
        if summary.summary_id in self._summaries:
            raise SummaryStoreConflictError(f"Summary already exists: {summary.summary_id}")
        self._summaries[summary.summary_id] = summary
        return summary

    def get_summary(self, summary_id: str) -> SummaryDraft | None:
        return self._summaries.get(summary_id)

    def list_review_items(self) -> list[SummaryDraft]:
        return [
            summary
            for summary in self._summaries.values()
            if summary.status in {"pending_review", "approved", "refused"}
        ]

    def edit_summary(
        self,
        summary_id: str,
        *,
        narrative: str,
        expected_audit_fingerprint: str,
    ) -> SummaryDraft:
        with self._lock:
            summary = self._summaries.get(summary_id)
            if summary is None:
                raise SummaryStoreNotFoundError(summary_id)
            if (
                summary.status != "pending_review"
                or summary.audit_fingerprint != expected_audit_fingerprint
            ):
                raise SummaryStoreConflictError(
                    f"Summary {summary_id!r} changed or is no longer pending review. Reload before editing."
                )
            if summary.agenda_minutes is not None:
                raise SummaryStoreConflictError(
                    "Edit agenda minutes through the structured minutes editor."
                )
            if summary.narrative == narrative:
                return summary
            updated = summary.model_copy(
                update={
                    "narrative": narrative,
                    "audit_fingerprint": _edited_audit_fingerprint(summary, narrative),
                }
            )
            self._summaries[summary_id] = updated
            return updated

    def edit_minutes(
        self,
        summary_id: str,
        *,
        content: MinutesContent,
        operator_id: str,
        expected_audit_fingerprint: str,
    ) -> SummaryDraft:
        with self._lock:
            summary = self._summaries.get(summary_id)
            _require_editable_minutes(summary, expected_audit_fingerprint)
            assert summary is not None
            updated = edited_minutes(summary, content, operator_id)
            self._summaries[summary_id] = updated
            return updated

    def approve_summary(
        self, approval: OperatorApproval, *, expected_audit_fingerprint: str
    ) -> SummaryDraft:
        with self._lock:
            summary = self._summaries.get(approval.summary_id)
            if summary is None:
                raise SummaryStoreNotFoundError(approval.summary_id)
            if (
                summary.status != "pending_review"
                or summary.audit_fingerprint != expected_audit_fingerprint
            ):
                raise SummaryStoreConflictError(
                    f"Summary {approval.summary_id!r} changed or is no longer pending review. "
                    "Reload and review it before approving."
                )
            _require_confirmed_minutes(summary)
            approved = summary.model_copy(update={"status": "approved"})
            self._summaries[approval.summary_id] = approved
            self._approvals[approval.summary_id] = approval
            return approved

    def get_approval(self, summary_id: str) -> OperatorApproval | None:
        return self._approvals.get(summary_id)


class PostgresSummaryStore:
    """SQLAlchemy-backed summary store for release persistence.

    The implementation uses SQL text instead of ORM models because the v0.6
    migration owns these tables directly. It still supports SQLite-backed test
    sessions by dropping the schema prefix when the bound dialect is SQLite.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def create_summary(self, summary: SummaryDraft) -> SummaryDraft:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            try:
                session.execute(
                    text(
                        f"INSERT INTO {table}summaries "  # nosec B608
                        "(summary_id, meeting_id, status, narrative, provenance_json, "
                        "audit_fingerprint, operator_message, created_at, agenda_minutes_json) "
                        "VALUES (:summary_id, :meeting_id, :status, :narrative, "
                        ":provenance_json, :audit_fingerprint, :operator_message, :created_at, :agenda_minutes_json)"
                    ),
                    {
                        "summary_id": summary.summary_id,
                        "meeting_id": summary.meeting_id,
                        "status": summary.status,
                        "narrative": summary.narrative,
                        "provenance_json": summary.provenance.model_dump_json(),
                        "audit_fingerprint": summary.audit_fingerprint,
                        "operator_message": summary.operator_message,
                        "created_at": (
                            datetime.now(UTC).isoformat()
                            if session.get_bind().dialect.name == "sqlite"
                            else datetime.now(UTC)
                        ),
                        "agenda_minutes_json": summary.agenda_minutes.model_dump_json()
                        if summary.agenda_minutes
                        else None,
                    },
                )
                for claim in summary.sourced_claims:
                    session.execute(
                        text(
                            f"INSERT INTO {table}sourced_claims "  # nosec B608
                            "(claim_id, summary_id, claim_type, text, transcript_ranges_json) "
                            "VALUES (:claim_id, :summary_id, :claim_type, :claim_text, :ranges)"
                        ),
                        {
                            "claim_id": claim.claim_id,
                            "summary_id": summary.summary_id,
                            "claim_type": claim.claim_type,
                            "claim_text": claim.text,
                            "ranges": json.dumps(
                                [
                                    transcript_range.model_dump(mode="json")
                                    for transcript_range in claim.transcript_ranges
                                ],
                                sort_keys=True,
                            ),
                        },
                    )
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise SummaryStoreConflictError(
                    f"Summary already exists: {summary.summary_id}"
                ) from exc
            return summary

    def get_summary(self, summary_id: str) -> SummaryDraft | None:
        with self._session_factory() as session:
            return self._load_summary(session, summary_id)

    def list_review_items(self) -> list[SummaryDraft]:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            rows = session.execute(
                text(
                    f"SELECT summary_id, meeting_id, status, narrative, provenance_json, "  # nosec B608
                    f"audit_fingerprint, operator_message, agenda_minutes_json FROM {table}summaries "
                    "WHERE status IN ('pending_review', 'approved', 'refused') "
                    "ORDER BY created_at ASC, summary_id ASC"
                )
            ).fetchall()
            if not rows:
                return []
            summary_ids = [str(row.summary_id) for row in rows]
            claims = session.execute(
                text(
                    f"SELECT summary_id, claim_id, claim_type, text, transcript_ranges_json "  # nosec B608
                    f"FROM {table}sourced_claims WHERE summary_id IN :summary_ids "
                    "ORDER BY summary_id ASC, claim_id ASC"
                ).bindparams(bindparam("summary_ids", expanding=True)),
                {"summary_ids": summary_ids},
            ).fetchall()
            claims_by_summary: dict[str, list[object]] = {
                summary_id: [] for summary_id in summary_ids
            }
            for claim in claims:
                claims_by_summary[str(claim.summary_id)].append(claim)
            return [
                self._summary_from_rows(row, claims_by_summary[str(row.summary_id)]) for row in rows
            ]

    def edit_summary(
        self,
        summary_id: str,
        *,
        narrative: str,
        expected_audit_fingerprint: str,
    ) -> SummaryDraft:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            summary = self._load_summary(session, summary_id)
            if summary is None:
                raise SummaryStoreNotFoundError(summary_id)
            if (
                summary.status != "pending_review"
                or summary.audit_fingerprint != expected_audit_fingerprint
            ):
                raise SummaryStoreConflictError(
                    f"Summary {summary_id!r} changed or is no longer pending review. Reload before editing."
                )
            if summary.agenda_minutes is not None:
                raise SummaryStoreConflictError(
                    "Edit agenda minutes through the structured minutes editor."
                )
            if summary.narrative == narrative:
                return summary
            updated_fingerprint = _edited_audit_fingerprint(summary, narrative)
            result = cast(
                CursorResult[Any],
                session.execute(
                    text(
                        f"UPDATE {table}summaries SET narrative = :narrative, "  # nosec B608
                        "audit_fingerprint = :audit_fingerprint "
                        "WHERE summary_id = :summary_id AND status = 'pending_review' "
                        "AND audit_fingerprint = :expected_audit_fingerprint"
                    ),
                    {
                        "summary_id": summary_id,
                        "narrative": narrative,
                        "audit_fingerprint": updated_fingerprint,
                        "expected_audit_fingerprint": expected_audit_fingerprint,
                    },
                ),
            )
            if result.rowcount != 1:
                session.rollback()
                raise SummaryStoreConflictError(
                    f"Summary {summary_id!r} changed or is no longer pending review. Reload before editing."
                )
            session.commit()
            updated = self._load_summary(session, summary_id)
            if updated is None:
                raise SummaryStoreNotFoundError(summary_id)
            return updated

    def edit_minutes(
        self,
        summary_id: str,
        *,
        content: MinutesContent,
        operator_id: str,
        expected_audit_fingerprint: str,
    ) -> SummaryDraft:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            summary = self._load_summary(session, summary_id)
            _require_editable_minutes(summary, expected_audit_fingerprint)
            assert summary is not None
            updated = edited_minutes(summary, content, operator_id)
            assert updated.agenda_minutes is not None
            result = cast(
                CursorResult[Any],
                session.execute(
                    text(
                        f"UPDATE {table}summaries SET narrative=:narrative, agenda_minutes_json=:minutes, "  # nosec B608
                        "audit_fingerprint=:fingerprint WHERE summary_id=:summary_id "
                        "AND status='pending_review' AND audit_fingerprint=:expected"
                    ),
                    {
                        "narrative": updated.narrative,
                        "minutes": updated.agenda_minutes.model_dump_json(),
                        "fingerprint": updated.audit_fingerprint,
                        "summary_id": summary_id,
                        "expected": expected_audit_fingerprint,
                    },
                ),
            )
            if result.rowcount != 1:
                session.rollback()
                raise SummaryStoreConflictError("Minutes changed; reload before editing.")
            session.commit()
            return updated

    def approve_summary(
        self, approval: OperatorApproval, *, expected_audit_fingerprint: str
    ) -> SummaryDraft:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            summary = self._load_summary(session, approval.summary_id)
            if summary is None:
                raise SummaryStoreNotFoundError(approval.summary_id)
            _require_confirmed_minutes(summary)
            result = cast(
                CursorResult[Any],
                session.execute(
                    text(
                        f"UPDATE {table}summaries SET status = 'approved' "  # nosec B608
                        "WHERE summary_id = :summary_id AND status = 'pending_review' "
                        "AND audit_fingerprint = :expected_audit_fingerprint"
                    ),
                    {
                        "summary_id": approval.summary_id,
                        "expected_audit_fingerprint": expected_audit_fingerprint,
                    },
                ),
            )
            if result.rowcount != 1:
                session.rollback()
                if self._load_summary(session, approval.summary_id) is None:
                    raise SummaryStoreNotFoundError(approval.summary_id)
                raise SummaryStoreConflictError(
                    f"Summary {approval.summary_id!r} changed or is no longer pending review. "
                    "Reload and review it before approving."
                )
            existing = session.execute(
                text(
                    f"SELECT summary_id FROM {table}summary_approvals "  # nosec B608
                    "WHERE summary_id = :summary_id"
                ),
                {"summary_id": approval.summary_id},
            ).first()
            params = {
                "summary_id": approval.summary_id,
                "operator_id": approval.operator_id,
                "operator_display_name": approval.operator_display_name,
                # Raw SQL bypasses SQLAlchemy's DateTime binder. Serialize
                # explicitly on SQLite instead of its deprecated DBAPI adapter.
                "approved_at": (
                    approval.approved_at.isoformat()
                    if session.get_bind().dialect.name == "sqlite"
                    else approval.approved_at
                ),
                "approval_note": approval.approval_note,
            }
            if existing is None:
                session.execute(
                    text(
                        f"INSERT INTO {table}summary_approvals "  # nosec B608
                        "(summary_id, operator_id, operator_display_name, approved_at, approval_note) "
                        "VALUES (:summary_id, :operator_id, :operator_display_name, "
                        ":approved_at, :approval_note)"
                    ),
                    params,
                )
            else:
                session.execute(
                    text(
                        f"UPDATE {table}summary_approvals SET "  # nosec B608
                        "operator_id = :operator_id, "
                        "operator_display_name = :operator_display_name, "
                        "approved_at = :approved_at, approval_note = :approval_note "
                        "WHERE summary_id = :summary_id"
                    ),
                    params,
                )
            session.commit()
            approved = self._load_summary(session, approval.summary_id)
            if approved is None:
                raise SummaryStoreNotFoundError(approval.summary_id)
            return approved

    def get_approval(self, summary_id: str) -> OperatorApproval | None:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            row = session.execute(
                text(
                    f"SELECT summary_id, operator_id, operator_display_name, "  # nosec B608
                    f"approved_at, approval_note FROM {table}summary_approvals "
                    "WHERE summary_id = :summary_id"
                ),
                {"summary_id": summary_id},
            ).first()
            if row is None:
                return None
            return OperatorApproval.model_validate(dict(row._mapping))

    def _load_summary(self, session: Session, summary_id: str) -> SummaryDraft | None:
        table = self._table_prefix(session)
        row = session.execute(
            text(
                f"SELECT summary_id, meeting_id, status, narrative, provenance_json, "  # nosec B608
                f"audit_fingerprint, operator_message, agenda_minutes_json FROM {table}summaries "
                "WHERE summary_id = :summary_id"
            ),
            {"summary_id": summary_id},
        ).first()
        if row is None:
            return None
        claims = session.execute(
            text(
                f"SELECT claim_id, claim_type, text, transcript_ranges_json "  # nosec B608
                f"FROM {table}sourced_claims WHERE summary_id = :summary_id "
                "ORDER BY claim_id ASC"
            ),
            {"summary_id": summary_id},
        ).fetchall()
        return self._summary_from_rows(row, claims)

    @staticmethod
    def _summary_from_rows(row: object, claims: Sequence[object]) -> SummaryDraft:
        row_any = cast(Any, row)
        claim_rows = [cast(Any, claim) for claim in claims]
        return SummaryDraft.model_validate(
            {
                "summary_id": row_any.summary_id,
                "meeting_id": row_any.meeting_id,
                "status": row_any.status,
                "narrative": row_any.narrative,
                "sourced_claims": [
                    SourcedClaim.model_validate(
                        {
                            "claim_id": claim.claim_id,
                            "text": claim.text,
                            "claim_type": claim.claim_type,
                            "transcript_ranges": json.loads(claim.transcript_ranges_json),
                        }
                    )
                    for claim in claim_rows
                ],
                "provenance": json.loads(row_any.provenance_json),
                "audit_fingerprint": row_any.audit_fingerprint,
                "operator_message": row_any.operator_message,
                "agenda_minutes": json.loads(row_any.agenda_minutes_json)
                if row_any.agenda_minutes_json
                else None,
            }
        )

    @staticmethod
    def _table_prefix(session: Session) -> str:
        bind = session.get_bind()
        return "" if bind.dialect.name == "sqlite" else "civiccast."


def _edited_audit_fingerprint(summary: SummaryDraft, narrative: str) -> str:
    """Bind an operator narrative edit to the generated summary fingerprint."""

    return sha256_fingerprint(
        {"previous_audit_fingerprint": summary.audit_fingerprint, "narrative": narrative}
    )


def _require_editable_minutes(summary: SummaryDraft | None, expected: str) -> None:
    if summary is None:
        raise SummaryStoreNotFoundError("Minutes not found")
    if (
        summary.status != "pending_review"
        or summary.audit_fingerprint != expected
        or summary.agenda_minutes is None
    ):
        raise SummaryStoreConflictError(
            "Minutes changed or are no longer pending; reload before editing."
        )


def _require_confirmed_minutes(summary: SummaryDraft) -> None:
    if summary.agenda_minutes is not None:
        try:
            require_minutes_approval(summary.agenda_minutes)
        except ValueError as exc:
            raise SummaryStoreConflictError(str(exc)) from exc
