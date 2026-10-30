# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable store for EAS sources, normalized alerts, and display decisions (S11c).

Dedup is keyed on ``(sender, identifier)`` (CAP's natural identity); an incoming
alert with an older ``sent`` than what we hold is ignored (idempotent / out-of-order
safe). Supersession: an Update/Cancel that carries CAP ``references`` marks the
referenced alerts ``superseded`` / ``cancelled``. Expiry is a sweep that flips active,
past-``expires`` alerts to ``expired``. Fail-closed: a fetch/parse failure upstream
never reaches here as a fabricated alert, and nothing here ever clears an active alert
except an explicit Cancel or a real expiry.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from civiccast.eas.models import (
    EasCapAlert,
    EasCapAlertDb,
    EasCapSource,
    EasCapSourceDb,
    EasDisplayDecision,
    EasDisplayDecisionDb,
    EasDisplayState,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]


class EasStoreError(RuntimeError):
    """Base error for EAS persistence failures."""


class SourceNotFoundError(EasStoreError):
    """Raised when a source id does not resolve."""


class AlertNotFoundError(EasStoreError):
    """Raised when an alert id does not resolve."""


class DecisionNotFoundError(EasStoreError):
    """Raised when a display-decision id does not resolve."""


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """Normalize a possibly-naive datetime (SQLite round-trips drop tz) to UTC-aware."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def parse_cap_references(references: str | None) -> list[tuple[str, str]]:
    """Extract ``(sender, identifier)`` pairs from a CAP ``references`` string.

    CAP 1.2 references are space-separated ``sender,identifier,sent`` triples."""
    if not references:
        return []
    pairs: list[tuple[str, str]] = []
    for token in references.split():
        parts = token.split(",")
        if len(parts) >= 2 and parts[0] and parts[1]:
            pairs.append((parts[0], parts[1]))
    return pairs


class EasStore:
    """CRUD + dedup/supersede/expire for the EAS ingest+display tables."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def _session(self) -> AbstractContextManager[Session]:
        return self._session_factory()

    # --- sources ---------------------------------------------------------

    def upsert_source(self, source: EasCapSource) -> EasCapSource:
        with self._session() as session:
            row = session.get(EasCapSourceDb, source.source_id)
            if row is None:
                row = EasCapSourceDb(source_id=source.source_id, created_at=source.created_at)
                session.add(row)
            row.label = source.label
            row.kind = source.kind
            row.endpoint_url = source.endpoint_url
            row.geocode_filter = list(source.geocode_filter)
            row.severity_floor = source.severity_floor
            row.poll_seconds = source.poll_seconds
            row.enabled = source.enabled
            row.credential_ref = source.credential_ref
            row.notes = source.notes
            row.updated_at = _now()
            session.commit()
            return _source_to_model(row)

    def get_source(self, source_id: str) -> EasCapSource | None:
        with self._session() as session:
            row = session.get(EasCapSourceDb, source_id)
            return _source_to_model(row) if row is not None else None

    def list_sources(self, *, enabled_only: bool = False) -> list[EasCapSource]:
        with self._session() as session:
            stmt = select(EasCapSourceDb).order_by(EasCapSourceDb.label)
            if enabled_only:
                stmt = stmt.where(EasCapSourceDb.enabled.is_(True))
            return [_source_to_model(r) for r in session.execute(stmt).scalars().all()]

    def delete_source(self, source_id: str) -> None:
        with self._session() as session:
            row = session.get(EasCapSourceDb, source_id)
            if row is None:
                raise SourceNotFoundError(f"EAS source {source_id!r} not found")
            session.delete(row)
            session.commit()

    # --- alerts ----------------------------------------------------------

    def ingest_alert(self, alert: EasCapAlert) -> tuple[EasCapAlert, bool]:
        """Insert or update by ``(sender, identifier)``; apply supersession.

        Returns ``(persisted_alert, is_new)``. An incoming alert older than the one
        held (by ``sent``) is a no-op (idempotent re-poll / out-of-order delivery)."""
        with self._session() as session:
            row = session.execute(
                select(EasCapAlertDb).where(
                    EasCapAlertDb.sender == alert.sender,
                    EasCapAlertDb.identifier == alert.identifier,
                )
            ).scalar_one_or_none()
            is_new = row is None
            if row is not None and _as_utc(alert.sent) <= _as_utc(row.sent):
                # A re-poll (or out-of-order delivery) of an alert we already hold at
                # the same or an older ``sent`` is a NO-OP. This is the steady state:
                # feeds keep an alert in the active list until its own expiry, so the
                # poller re-sees the same (sender, identifier, sent) every cycle. It
                # must NEVER resurrect a non-active lifecycle (cancelled / superseded /
                # expired) — the parser always defaults status='active' and cannot know
                # lifecycle, so re-applying it would put a recalled alert back on air.
                # Lifecycle changes only via _apply_supersession (Update/Cancel) or the
                # expiry sweep.
                return _alert_to_model(row), False
            if row is None:
                row = EasCapAlertDb(
                    alert_id=alert.alert_id,
                    sender=alert.sender,
                    identifier=alert.identifier,
                    created_at=alert.created_at,
                )
                row.status = alert.status  # initial insert only ('active')
                session.add(row)
            # Fresh insert OR a genuinely newer-sent refresh of the same alert: refresh
            # content, but the row's STATUS is preserved (owned by supersession/expiry,
            # not the parser) so a newer re-issue of a cancelled alert stays cancelled.
            row.source_id = alert.source_id
            row.sent = alert.sent
            row.msg_type = alert.msg_type
            row.event = alert.event
            row.severity = alert.severity
            row.urgency = alert.urgency
            row.certainty = alert.certainty
            row.headline = alert.headline
            row.description = alert.description
            row.instruction = alert.instruction
            row.areas = list(alert.areas)
            row.references = alert.references
            row.effective = alert.effective
            row.onset = alert.onset
            row.expires = alert.expires
            row.updated_at = _now()
            self._apply_supersession(session, alert, current_alert_id=row.alert_id)
            session.commit()
            return _alert_to_model(row), is_new

    @staticmethod
    def _apply_supersession(session: Session, alert: EasCapAlert, *, current_alert_id: str) -> None:
        """An Update/Cancel marks the alerts it references superseded/cancelled."""
        if alert.msg_type not in ("update", "cancel"):
            return
        new_status = "cancelled" if alert.msg_type == "cancel" else "superseded"
        for sender, identifier in parse_cap_references(alert.references):
            ref = session.execute(
                select(EasCapAlertDb).where(
                    EasCapAlertDb.sender == sender,
                    EasCapAlertDb.identifier == identifier,
                )
            ).scalar_one_or_none()
            if ref is not None and ref.alert_id != current_alert_id and ref.status == "active":
                ref.status = new_status
                ref.updated_at = _now()

    def get_alert(self, alert_id: str) -> EasCapAlert | None:
        with self._session() as session:
            row = session.get(EasCapAlertDb, alert_id)
            return _alert_to_model(row) if row is not None else None

    def list_alerts(
        self, *, active_only: bool = False, source_id: str | None = None, limit: int = 200
    ) -> list[EasCapAlert]:
        with self._session() as session:
            stmt = select(EasCapAlertDb)
            if active_only:
                stmt = stmt.where(EasCapAlertDb.status == "active")
            if source_id is not None:
                stmt = stmt.where(EasCapAlertDb.source_id == source_id)
            stmt = stmt.order_by(EasCapAlertDb.sent.desc()).limit(limit)
            return [_alert_to_model(r) for r in session.execute(stmt).scalars().all()]

    def expire_alerts(self, *, now: datetime | None = None) -> int:
        """Flip active, past-``expires`` alerts to ``expired``. Returns the count."""
        when = now or _now()
        with self._session() as session:
            result = session.execute(
                update(EasCapAlertDb)
                .where(
                    EasCapAlertDb.status == "active",
                    EasCapAlertDb.expires.is_not(None),
                    EasCapAlertDb.expires < when,
                )
                .values(status="expired", updated_at=when)
            )
            session.commit()
            return int(cast(CursorResult[object], result).rowcount or 0)

    # --- display decisions ----------------------------------------------

    def upsert_decision(self, decision: EasDisplayDecision) -> EasDisplayDecision:
        with self._session() as session:
            row = session.get(EasDisplayDecisionDb, decision.decision_id)
            if row is None:
                row = EasDisplayDecisionDb(
                    decision_id=decision.decision_id, created_at=decision.created_at
                )
                session.add(row)
            row.alert_id = decision.alert_id
            row.channel_id = decision.channel_id
            row.mode = decision.mode
            row.state = decision.state
            row.decided_by = decision.decided_by
            row.auto_surfaced = decision.auto_surfaced
            row.overlay_id = decision.overlay_id
            row.eas_claim = "not_eas"
            row.reason = decision.reason
            row.displayed_at = decision.displayed_at
            row.cleared_at = decision.cleared_at
            row.expires_at = decision.expires_at
            row.updated_at = _now()
            session.commit()
            return _decision_to_model(row)

    def get_decision(self, decision_id: str) -> EasDisplayDecision | None:
        with self._session() as session:
            row = session.get(EasDisplayDecisionDb, decision_id)
            return _decision_to_model(row) if row is not None else None

    def list_decisions(
        self,
        *,
        channel_id: str | None = None,
        state: EasDisplayState | None = None,
        limit: int = 200,
    ) -> list[EasDisplayDecision]:
        with self._session() as session:
            stmt = select(EasDisplayDecisionDb)
            if channel_id is not None:
                stmt = stmt.where(EasDisplayDecisionDb.channel_id == channel_id)
            if state is not None:
                stmt = stmt.where(EasDisplayDecisionDb.state == state)
            stmt = stmt.order_by(EasDisplayDecisionDb.created_at.desc()).limit(limit)
            return [_decision_to_model(r) for r in session.execute(stmt).scalars().all()]

    def set_decision_state(
        self,
        decision_id: str,
        state: EasDisplayState,
        *,
        displayed_at: datetime | None = None,
        cleared_at: datetime | None = None,
    ) -> EasDisplayDecision:
        with self._session() as session:
            row = session.get(EasDisplayDecisionDb, decision_id)
            if row is None:
                raise DecisionNotFoundError(f"EAS display decision {decision_id!r} not found")
            row.state = state
            if displayed_at is not None:
                row.displayed_at = displayed_at
            if cleared_at is not None:
                row.cleared_at = cleared_at
            row.updated_at = _now()
            session.commit()
            return _decision_to_model(row)


# --- row → model converters ----------------------------------------------------


def _source_to_model(row: EasCapSourceDb) -> EasCapSource:
    return EasCapSource(
        source_id=row.source_id,
        label=row.label,
        kind=row.kind,  # type: ignore[arg-type]
        endpoint_url=row.endpoint_url,
        geocode_filter=list(row.geocode_filter or []),
        severity_floor=row.severity_floor,  # type: ignore[arg-type]
        poll_seconds=row.poll_seconds,
        enabled=row.enabled,
        credential_ref=row.credential_ref,
        notes=row.notes,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def _alert_to_model(row: EasCapAlertDb) -> EasCapAlert:
    return EasCapAlert(
        alert_id=row.alert_id,
        source_id=row.source_id,
        sender=row.sender,
        identifier=row.identifier,
        sent=_as_utc(row.sent),
        msg_type=row.msg_type,  # type: ignore[arg-type]
        status=row.status,  # type: ignore[arg-type]
        event=row.event,
        severity=row.severity,  # type: ignore[arg-type]
        urgency=row.urgency,
        certainty=row.certainty,
        headline=row.headline,
        description=row.description,
        instruction=row.instruction,
        areas=list(row.areas or []),
        references=row.references,
        effective=_as_utc(row.effective) if row.effective else None,
        onset=_as_utc(row.onset) if row.onset else None,
        expires=_as_utc(row.expires) if row.expires else None,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def _decision_to_model(row: EasDisplayDecisionDb) -> EasDisplayDecision:
    return EasDisplayDecision(
        decision_id=row.decision_id,
        alert_id=row.alert_id,
        channel_id=row.channel_id,
        mode=row.mode,  # type: ignore[arg-type]
        state=row.state,  # type: ignore[arg-type]
        decided_by=row.decided_by,
        auto_surfaced=row.auto_surfaced,
        overlay_id=row.overlay_id,
        eas_claim="not_eas",
        reason=row.reason,
        displayed_at=_as_utc(row.displayed_at) if row.displayed_at else None,
        cleared_at=_as_utc(row.cleared_at) if row.cleared_at else None,
        expires_at=_as_utc(row.expires_at) if row.expires_at else None,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )
