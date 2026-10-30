# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Persistence seam for durable notification-delivery outcomes (WP-05).

One logical delivery per publication x subscription x target x transport, with
numbered attempts beneath it.

Two mechanisms together make a double send impossible:

* the UNIQUE constraint on the logical identity settles the INSERT race -- only
  one caller can create the row;
* a lease column settles the already-exists race -- ``claim`` grants the send
  only to the caller whose conditional UPDATE actually touched the row, and a
  row already observed ``sent`` is never granted at all.

Gating on "not already sent" alone was a real double send: two callers could
both read the same freshly-inserted ``pending`` row, neither owning it, and
both deliver. A lease that expires also keeps the guard from becoming a
deadlock -- a process killed mid-send leaves a stale in-flight row that the
next caller may take over once the lease lapses.

No subscriber PII, webhook URL, secret or signature is written here -- see the
PII note on :class:`civiccast.subscribe.models.NotificationDeliveryOutcome`.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from civiccast.subscribe.models import (
    NotificationDeliveryAttempt,
    NotificationDeliveryAttemptRecord,
    NotificationDeliveryOutcome,
    NotificationDeliveryOutcomeRecord,
    NotificationDeliveryOutcomeValue,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]

__all__ = [
    "ClaimedDelivery",
    "InMemoryNotificationDeliveryStore",
    "NotificationDeliveryStore",
    "PostgresNotificationDeliveryStore",
    "logical_delivery_key",
]


def logical_delivery_key(
    *,
    publication_id: str,
    subscription_id: str,
    target_type: str,
    target_id: str,
    transport: str,
) -> str:
    """Stable logical delivery key.

    Deliberately excludes the attempt number: this key IS the duplicate-send
    guard, so attempt 1 and attempt 7 of the same recipient must collide on the
    same row (WP-05 plan item 4). Digested rather than concatenated so the key
    stays inside the 80-character column no matter how long a station's target
    ids are, and so it never carries a readable handle.
    """

    digest = hashlib.sha256(
        "\x1f".join((publication_id, subscription_id, target_type, target_id, transport)).encode()
    ).hexdigest()
    return f"ndk-{digest[:40]}"


#: How long one caller owns an in-flight delivery before another may take it
#: over. Long enough that a slow SMTP conversation or a webhook timeout cannot
#: hand the same recipient to a second worker; short enough that a process
#: killed mid-send does not strand the delivery forever.
DELIVERY_LEASE_SECONDS = 900


def _lease_seconds() -> int:
    raw = os.environ.get("CIVICCAST_NOTIFICATION_DELIVERY_LEASE_SECONDS", "").strip()
    if not raw:
        return DELIVERY_LEASE_SECONDS
    try:
        parsed = int(raw)
    except ValueError:
        return DELIVERY_LEASE_SECONDS
    return parsed if parsed > 0 else DELIVERY_LEASE_SECONDS


@dataclass(frozen=True)
class ClaimedDelivery:
    """Result of :meth:`NotificationDeliveryStore.claim`.

    ``granted`` is the ONLY safe send gate. It is true exactly when this caller
    now owns the delivery lease -- because it inserted the row (``created``), or
    because it took over a stale in-flight row whose lease had expired, or
    because it is an explicit retry taking over a terminal-failed/queued row.

    Gating on "not already sent" instead was a genuine double-send: two callers
    could both read a ``pending`` row neither of them owned and both send.
    ``created`` is kept separate from ``granted`` because they answer different
    questions -- "was this recipient new to this publication?" versus "may I
    send now?" -- and the first is what a caller logs and a test asserts on.
    """

    record: NotificationDeliveryOutcomeRecord
    created: bool
    granted: bool

    @property
    def already_sent(self) -> bool:
        return self.record.outcome == "sent"


class NotificationDeliveryStore(Protocol):
    def claim(
        self,
        *,
        publication_id: str,
        asset_id: str,
        subscription_id: str,
        target_type: str,
        target_id: str,
        transport: str,
        allow_reattempt: bool = False,
        now: datetime | None = None,
    ) -> ClaimedDelivery: ...

    def record_attempt(
        self,
        *,
        delivery_key: str,
        outcome: NotificationDeliveryOutcomeValue,
        error_code: str | None = None,
        detail: str = "",
        retry_id: str | None = None,
        now: datetime | None = None,
    ) -> NotificationDeliveryOutcomeRecord: ...

    def get(self, delivery_key: str) -> NotificationDeliveryOutcomeRecord | None: ...

    def list_for_publication(
        self, publication_id: str
    ) -> list[NotificationDeliveryOutcomeRecord]: ...

    def list_attempts(self, delivery_key: str) -> list[NotificationDeliveryAttemptRecord]: ...

    def record_webhook_retry_result(
        self,
        *,
        retry_id: str,
        outcome: NotificationDeliveryOutcomeValue,
        detail: str,
        now: datetime | None = None,
    ) -> NotificationDeliveryOutcomeRecord | None: ...


def _new_record(
    *,
    delivery_key: str,
    publication_id: str,
    asset_id: str,
    subscription_id: str,
    target_type: str,
    target_id: str,
    transport: str,
    now: datetime,
) -> NotificationDeliveryOutcomeRecord:
    return NotificationDeliveryOutcomeRecord(
        delivery_key=delivery_key,
        publication_id=publication_id,
        asset_id=asset_id,
        subscription_id=subscription_id,
        target_type=target_type,  # type: ignore[arg-type]
        target_id=target_id,
        transport=transport,  # type: ignore[arg-type]
        outcome="pending",
        attempts=0,
        lease_expires_at=now + timedelta(seconds=_lease_seconds()),
        created_at=now,
        updated_at=now,
    )


def _lease_is_free(record: NotificationDeliveryOutcomeRecord, *, now: datetime) -> bool:
    """True when nobody currently owns this in-flight delivery."""

    return record.lease_expires_at is None or record.lease_expires_at <= now


def _may_take_over(
    record: NotificationDeliveryOutcomeRecord, *, allow_reattempt: bool, now: datetime
) -> bool:
    """Whether a caller may take over an existing row's lease.

    A recipient already observed ``sent`` is never taken over -- that is the
    duplicate-send guard. A ``pending`` row is taken over only once its lease
    has expired (the previous sender died mid-send). A ``failed`` row is taken
    over only by an explicit retry. A ``queued`` row belongs to the background
    worker until it succeeds or dead-letters.
    """

    if record.outcome == "sent":
        return False
    if record.outcome == "pending":
        return _lease_is_free(record, now=now)
    # The background worker owns queued webhooks. A surface retry must not
    # send the same notice while that durable job is still pending.
    return allow_reattempt and record.outcome == "failed"


def _applied(
    record: NotificationDeliveryOutcomeRecord,
    *,
    outcome: NotificationDeliveryOutcomeValue,
    error_code: str | None,
    detail: str,
    retry_id: str | None,
    now: datetime,
) -> NotificationDeliveryOutcomeRecord:
    attempts = record.attempts + 1
    return record.model_copy(
        update={
            "outcome": outcome,
            "attempts": attempts,
            "error_code": error_code,
            "detail": detail,
            # A successful retry clears the queue linkage; a queued one keeps
            # it so the dead-letter row stays reachable from the publish run.
            "retry_id": retry_id,
            "first_attempted_at": record.first_attempted_at or now,
            "last_attempted_at": now,
            "succeeded_at": now if outcome == "sent" else record.succeeded_at,
            # The delivery is no longer in flight: release the lease so a retry
            # (or a lease-recovery scan) sees a result, not an owner.
            "lease_expires_at": None,
            "updated_at": now,
        }
    )


class InMemoryNotificationDeliveryStore:
    """In-memory outcomes for tests and no-durable-storage app instances.

    Single-process only: it cannot provide the cross-process concurrency
    guarantee the UNIQUE constraint gives the Postgres/SQLite store. It is
    still a real duplicate-send guard within one process, which is what an
    ephemeral instance can honestly offer.
    """

    def __init__(self) -> None:
        self._records: dict[str, NotificationDeliveryOutcomeRecord] = {}
        self._attempts: dict[str, list[NotificationDeliveryAttemptRecord]] = {}

    def claim(
        self,
        *,
        publication_id: str,
        asset_id: str,
        subscription_id: str,
        target_type: str,
        target_id: str,
        transport: str,
        allow_reattempt: bool = False,
        now: datetime | None = None,
    ) -> ClaimedDelivery:
        resolved_now = now or datetime.now(UTC)
        key = logical_delivery_key(
            publication_id=publication_id,
            subscription_id=subscription_id,
            target_type=target_type,
            target_id=target_id,
            transport=transport,
        )
        existing = self._records.get(key)
        if existing is not None:
            if not _may_take_over(existing, allow_reattempt=allow_reattempt, now=resolved_now):
                return ClaimedDelivery(record=existing, created=False, granted=False)
            taken = existing.model_copy(
                update={
                    "lease_expires_at": resolved_now + timedelta(seconds=_lease_seconds()),
                    "updated_at": resolved_now,
                }
            )
            self._records[key] = taken
            return ClaimedDelivery(record=taken, created=False, granted=True)
        record = _new_record(
            delivery_key=key,
            publication_id=publication_id,
            asset_id=asset_id,
            subscription_id=subscription_id,
            target_type=target_type,
            target_id=target_id,
            transport=transport,
            now=resolved_now,
        )
        self._records[key] = record
        return ClaimedDelivery(record=record, created=True, granted=True)

    def record_attempt(
        self,
        *,
        delivery_key: str,
        outcome: NotificationDeliveryOutcomeValue,
        error_code: str | None = None,
        detail: str = "",
        retry_id: str | None = None,
        now: datetime | None = None,
    ) -> NotificationDeliveryOutcomeRecord:
        resolved_now = now or datetime.now(UTC)
        record = self._records.get(delivery_key)
        if record is None:
            raise KeyError(delivery_key)
        updated = _applied(
            record,
            outcome=outcome,
            error_code=error_code,
            detail=detail,
            retry_id=retry_id,
            now=resolved_now,
        )
        self._records[delivery_key] = updated
        self._attempts.setdefault(delivery_key, []).append(
            NotificationDeliveryAttemptRecord(
                delivery_key=delivery_key,
                attempt_number=updated.attempts,
                attempted_at=resolved_now,
                outcome=outcome,
                error_code=error_code,
                detail=detail,
                retry_id=retry_id,
            )
        )
        return updated

    def get(self, delivery_key: str) -> NotificationDeliveryOutcomeRecord | None:
        return self._records.get(delivery_key)

    def list_for_publication(self, publication_id: str) -> list[NotificationDeliveryOutcomeRecord]:
        return sorted(
            (
                record
                for record in self._records.values()
                if record.publication_id == publication_id
            ),
            key=lambda record: record.delivery_key,
        )

    def list_attempts(self, delivery_key: str) -> list[NotificationDeliveryAttemptRecord]:
        return sorted(
            self._attempts.get(delivery_key, []), key=lambda attempt: attempt.attempt_number
        )

    def record_webhook_retry_result(
        self,
        *,
        retry_id: str,
        outcome: NotificationDeliveryOutcomeValue,
        detail: str,
        now: datetime | None = None,
    ) -> NotificationDeliveryOutcomeRecord | None:
        for record in self._records.values():
            if record.retry_id == retry_id and record.outcome == "queued":
                return self.record_attempt(
                    delivery_key=record.delivery_key,
                    outcome=outcome,
                    detail=detail,
                    retry_id=retry_id if outcome == "queued" else None,
                    now=now,
                )
        return None


class PostgresNotificationDeliveryStore:
    """SQLAlchemy-backed durable outcomes (Postgres in production, SQLite locally)."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def claim(
        self,
        *,
        publication_id: str,
        asset_id: str,
        subscription_id: str,
        target_type: str,
        target_id: str,
        transport: str,
        allow_reattempt: bool = False,
        now: datetime | None = None,
    ) -> ClaimedDelivery:
        """Insert-or-lease this logical delivery; ``granted`` says who may send.

        Two concurrent callers cannot both be granted. The insert race is
        settled by the UNIQUE constraint on the logical identity; the
        already-exists race is settled by a conditional UPDATE whose WHERE
        clause repeats the lease predicate, so only the caller whose UPDATE
        touches a row (``rowcount == 1``) takes the lease -- the loser's WHERE
        no longer matches once the winner has committed.
        """

        resolved_now = now or datetime.now(UTC)
        lease_until = resolved_now + timedelta(seconds=_lease_seconds())
        key = logical_delivery_key(
            publication_id=publication_id,
            subscription_id=subscription_id,
            target_type=target_type,
            target_id=target_id,
            transport=transport,
        )
        with self._session_factory() as session:
            existing = session.get(NotificationDeliveryOutcome, key)
            if existing is None:
                session.add(
                    NotificationDeliveryOutcome(
                        delivery_key=key,
                        publication_id=publication_id,
                        asset_id=asset_id,
                        subscription_id=subscription_id,
                        target_type=target_type,
                        target_id=target_id,
                        transport=transport,
                        outcome="pending",
                        attempts=0,
                        detail="",
                        lease_expires_at=lease_until,
                        created_at=resolved_now,
                        updated_at=resolved_now,
                    )
                )
                try:
                    session.commit()
                except IntegrityError:
                    # Another caller won the insert. Fall through to the
                    # take-over path below against the winner's row.
                    session.rollback()
                    existing = session.get(NotificationDeliveryOutcome, key)
                    if existing is None:
                        existing = session.execute(
                            select(NotificationDeliveryOutcome).where(
                                NotificationDeliveryOutcome.publication_id == publication_id,
                                NotificationDeliveryOutcome.subscription_id == subscription_id,
                                NotificationDeliveryOutcome.target_type == target_type,
                                NotificationDeliveryOutcome.target_id == target_id,
                                NotificationDeliveryOutcome.transport == transport,
                            )
                        ).scalar_one()
                else:
                    row = session.get(NotificationDeliveryOutcome, key)
                    assert row is not None  # nosec B101 - just committed above
                    return ClaimedDelivery(record=_to_record(row), created=True, granted=True)

            current = _to_record(existing)
            if not _may_take_over(current, allow_reattempt=allow_reattempt, now=resolved_now):
                return ClaimedDelivery(record=current, created=False, granted=False)

            # The WHERE clause is the lock: it re-states the exact condition
            # that made the take-over legal, so a concurrent caller that
            # already took the lease makes this UPDATE match zero rows.
            result = session.execute(
                update(NotificationDeliveryOutcome)
                .where(
                    NotificationDeliveryOutcome.delivery_key == current.delivery_key,
                    # Optimistic guard: if a concurrent caller recorded a result
                    # between our read and this write, these no longer match.
                    NotificationDeliveryOutcome.outcome == current.outcome,
                    NotificationDeliveryOutcome.attempts == current.attempts,
                    or_(
                        NotificationDeliveryOutcome.lease_expires_at.is_(None),
                        NotificationDeliveryOutcome.lease_expires_at <= resolved_now,
                    ),
                )
                .values(lease_expires_at=lease_until, updated_at=resolved_now)
                # The lock lives in the database, so the WHERE clause must be
                # evaluated there and only there. The ORM's default
                # post-synchronize strategy re-evaluates it in Python against
                # the identity map, which both defeats the point and breaks on
                # SQLite (whose round-tripped datetimes are naive).
                .execution_options(synchronize_session=False)
            )
            # ``rowcount`` is the whole point of this statement -- how many rows
            # the conditional UPDATE actually touched -- but SQLAlchemy types it
            # only on CursorResult, which an ORM-enabled update returns at
            # runtime and not in the stub.
            rows_updated = int(result.rowcount)  # type: ignore[attr-defined]
            session.commit()
            if rows_updated != 1:
                refreshed = session.get(NotificationDeliveryOutcome, current.delivery_key)
                losing = current if refreshed is None else _to_record(refreshed)
                return ClaimedDelivery(record=losing, created=False, granted=False)
            taken = session.get(NotificationDeliveryOutcome, current.delivery_key)
            assert taken is not None  # nosec B101 - just updated above
            return ClaimedDelivery(record=_to_record(taken), created=False, granted=True)

    def record_attempt(
        self,
        *,
        delivery_key: str,
        outcome: NotificationDeliveryOutcomeValue,
        error_code: str | None = None,
        detail: str = "",
        retry_id: str | None = None,
        now: datetime | None = None,
    ) -> NotificationDeliveryOutcomeRecord:
        resolved_now = now or datetime.now(UTC)
        with self._session_factory() as session:
            row = session.get(NotificationDeliveryOutcome, delivery_key)
            if row is None:
                raise KeyError(delivery_key)
            updated = _applied(
                _to_record(row),
                outcome=outcome,
                error_code=error_code,
                detail=detail,
                retry_id=retry_id,
                now=resolved_now,
            )
            row.outcome = updated.outcome
            row.attempts = updated.attempts
            row.error_code = updated.error_code
            row.detail = updated.detail
            row.retry_id = updated.retry_id
            row.lease_expires_at = updated.lease_expires_at
            row.first_attempted_at = updated.first_attempted_at
            row.last_attempted_at = updated.last_attempted_at
            row.succeeded_at = updated.succeeded_at
            row.updated_at = updated.updated_at
            session.add(
                NotificationDeliveryAttempt(
                    delivery_key=delivery_key,
                    attempt_number=updated.attempts,
                    attempted_at=resolved_now,
                    outcome=outcome,
                    error_code=error_code,
                    detail=detail,
                    retry_id=retry_id,
                )
            )
            session.commit()
            return updated

    def get(self, delivery_key: str) -> NotificationDeliveryOutcomeRecord | None:
        with self._session_factory() as session:
            row = session.get(NotificationDeliveryOutcome, delivery_key)
            return None if row is None else _to_record(row)

    def list_for_publication(self, publication_id: str) -> list[NotificationDeliveryOutcomeRecord]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(NotificationDeliveryOutcome)
                .where(NotificationDeliveryOutcome.publication_id == publication_id)
                .order_by(NotificationDeliveryOutcome.delivery_key.asc())
            ).all()
            return [_to_record(row) for row in rows]

    def list_attempts(self, delivery_key: str) -> list[NotificationDeliveryAttemptRecord]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(NotificationDeliveryAttempt)
                .where(NotificationDeliveryAttempt.delivery_key == delivery_key)
                .order_by(NotificationDeliveryAttempt.attempt_number.asc())
            ).all()
            return [
                NotificationDeliveryAttemptRecord(
                    delivery_key=row.delivery_key,
                    attempt_number=row.attempt_number,
                    attempted_at=_aware(row.attempted_at),
                    outcome=row.outcome,  # type: ignore[arg-type]
                    error_code=row.error_code,
                    detail=row.detail,
                    retry_id=row.retry_id,
                )
                for row in rows
            ]

    def record_webhook_retry_result(
        self,
        *,
        retry_id: str,
        outcome: NotificationDeliveryOutcomeValue,
        detail: str,
        now: datetime | None = None,
    ) -> NotificationDeliveryOutcomeRecord | None:
        """Apply a worker result only to the still-queued matching delivery."""
        resolved_now = now or datetime.now(UTC)
        with self._session_factory() as session:
            row = session.scalars(
                select(NotificationDeliveryOutcome)
                .where(
                    NotificationDeliveryOutcome.retry_id == retry_id,
                    NotificationDeliveryOutcome.outcome == "queued",
                )
                .with_for_update()
            ).one_or_none()
            if row is None:
                return None
            updated = _applied(
                _to_record(row),
                outcome=outcome,
                error_code=None,
                detail=detail,
                retry_id=retry_id if outcome == "queued" else None,
                now=resolved_now,
            )
            row.outcome = updated.outcome
            row.attempts = updated.attempts
            row.error_code = updated.error_code
            row.detail = updated.detail
            row.retry_id = updated.retry_id
            row.lease_expires_at = updated.lease_expires_at
            row.first_attempted_at = updated.first_attempted_at
            row.last_attempted_at = updated.last_attempted_at
            row.succeeded_at = updated.succeeded_at
            row.updated_at = updated.updated_at
            session.add(
                NotificationDeliveryAttempt(
                    delivery_key=updated.delivery_key,
                    attempt_number=updated.attempts,
                    attempted_at=resolved_now,
                    outcome=outcome,
                    detail=detail,
                    retry_id=retry_id if outcome == "queued" else None,
                )
            )
            session.commit()
            return updated


def _aware(value: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip; re-attach UTC so callers see aware datetimes."""

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _optional_aware(value: datetime | None) -> datetime | None:
    return None if value is None else _aware(value)


def _to_record(row: NotificationDeliveryOutcome) -> NotificationDeliveryOutcomeRecord:
    return NotificationDeliveryOutcomeRecord(
        delivery_key=row.delivery_key,
        publication_id=row.publication_id,
        asset_id=row.asset_id,
        subscription_id=row.subscription_id,
        target_type=row.target_type,  # type: ignore[arg-type]
        target_id=row.target_id,
        transport=row.transport,  # type: ignore[arg-type]
        outcome=row.outcome,  # type: ignore[arg-type]
        attempts=row.attempts,
        error_code=row.error_code,
        detail=row.detail,
        retry_id=row.retry_id,
        lease_expires_at=_optional_aware(row.lease_expires_at),
        first_attempted_at=_optional_aware(row.first_attempted_at),
        last_attempted_at=_optional_aware(row.last_attempted_at),
        succeeded_at=_optional_aware(row.succeeded_at),
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )
