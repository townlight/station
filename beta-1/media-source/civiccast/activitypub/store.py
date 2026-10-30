# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Persistence seam for ActivityPub local federation state."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from civiccast.activitypub.models import (
    ActivityPubDeliveryAttempt,
    ActivityPubDeliveryRetry,
    ActivityPubFollower,
    ActivityPubOutboxActivity,
    DeliveryRecord,
    DeliveryRetryRecord,
    FollowerRecord,
    OutboxRecord,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]


class ActivityPubStore(Protocol):
    def upsert_follower(self, record: FollowerRecord) -> FollowerRecord: ...
    def get_follower(self, actor: str) -> FollowerRecord | None: ...
    def list_followers(self, *, status: str = "accepted") -> list[FollowerRecord]: ...
    def set_follower_status(self, *, actor: str, status: str) -> FollowerRecord | None: ...
    def append_outbox(self, record: OutboxRecord) -> OutboxRecord: ...
    def list_outbox(self) -> list[OutboxRecord]: ...
    def append_delivery(self, record: DeliveryRecord) -> DeliveryRecord: ...
    def list_deliveries(self, *, activity_id: str | None = None) -> list[DeliveryRecord]: ...
    def enqueue_delivery_retry(self, record: DeliveryRetryRecord) -> DeliveryRetryRecord: ...
    def get_delivery_retry(self, retry_id: str) -> DeliveryRetryRecord | None: ...
    def list_delivery_retries(self) -> list[DeliveryRetryRecord]: ...
    def due_delivery_retries(self, *, now: datetime) -> list[DeliveryRetryRecord]: ...
    def save_delivery_retry(self, record: DeliveryRetryRecord) -> DeliveryRetryRecord: ...


class InMemoryActivityPubStore:
    """App-scoped in-memory ActivityPub store for local tests and dev installs."""

    def __init__(self) -> None:
        self._followers: dict[str, FollowerRecord] = {}
        self._outbox: list[OutboxRecord] = []
        self._deliveries: list[DeliveryRecord] = []
        self._delivery_retries: dict[str, DeliveryRetryRecord] = {}

    def upsert_follower(self, record: FollowerRecord) -> FollowerRecord:
        self._followers[str(record.actor)] = record
        return record

    def get_follower(self, actor: str) -> FollowerRecord | None:
        return self._followers.get(actor)

    def list_followers(self, *, status: str = "accepted") -> list[FollowerRecord]:
        return [record for record in self._followers.values() if record.status == status]

    def set_follower_status(self, *, actor: str, status: str) -> FollowerRecord | None:
        existing = self._followers.get(actor)
        if existing is None:
            return None
        updated = existing.model_copy(update={"status": status})
        self._followers[actor] = updated
        return updated

    def append_outbox(self, record: OutboxRecord) -> OutboxRecord:
        if not any(existing.activity_id == record.activity_id for existing in self._outbox):
            self._outbox.insert(0, record)
        return record

    def list_outbox(self) -> list[OutboxRecord]:
        return list(self._outbox)

    def append_delivery(self, record: DeliveryRecord) -> DeliveryRecord:
        self._deliveries.append(record)
        return record

    def list_deliveries(self, *, activity_id: str | None = None) -> list[DeliveryRecord]:
        if activity_id is None:
            return list(self._deliveries)
        return [record for record in self._deliveries if record.activity_id == activity_id]

    def enqueue_delivery_retry(self, record: DeliveryRetryRecord) -> DeliveryRetryRecord:
        self._delivery_retries[record.retry_id] = record
        return record

    def get_delivery_retry(self, retry_id: str) -> DeliveryRetryRecord | None:
        return self._delivery_retries.get(retry_id)

    def list_delivery_retries(self) -> list[DeliveryRetryRecord]:
        return sorted(
            self._delivery_retries.values(), key=lambda row: (row.created_at, row.retry_id)
        )

    def due_delivery_retries(self, *, now: datetime) -> list[DeliveryRetryRecord]:
        return [
            row
            for row in self.list_delivery_retries()
            if row.state == "pending"
            and row.next_attempt_at is not None
            and _as_comparable(row.next_attempt_at) <= _as_comparable(now)
        ]

    def save_delivery_retry(self, record: DeliveryRetryRecord) -> DeliveryRetryRecord:
        self._delivery_retries[record.retry_id] = record
        return record


class PostgresActivityPubStore:
    """SQLAlchemy-backed ActivityPub store for Postgres and SQLite tests."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def upsert_follower(self, record: FollowerRecord) -> FollowerRecord:
        with self._session_factory() as session:
            existing = session.get(ActivityPubFollower, record.actor)
            if existing is None:
                session.add(_follower_row(record))
            else:
                existing.domain = record.domain
                existing.status = record.status
                existing.activity_id = record.activity_id
                existing.inbox_url = record.inbox_url
                existing.shared_inbox_url = record.shared_inbox_url
                existing.public_key_id = record.public_key_id
                existing.public_key_pem = record.public_key_pem
                existing.created_at = record.created_at
            session.commit()
            return record

    def get_follower(self, actor: str) -> FollowerRecord | None:
        with self._session_factory() as session:
            row = session.get(ActivityPubFollower, actor)
            return None if row is None else row.to_record()

    def list_followers(self, *, status: str = "accepted") -> list[FollowerRecord]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(ActivityPubFollower)
                .where(ActivityPubFollower.status == status)
                .order_by(ActivityPubFollower.created_at.asc(), ActivityPubFollower.actor.asc())
            ).all()
            return [row.to_record() for row in rows]

    def set_follower_status(self, *, actor: str, status: str) -> FollowerRecord | None:
        with self._session_factory() as session:
            row = session.get(ActivityPubFollower, actor)
            if row is None:
                return None
            row.status = status
            session.commit()
            session.refresh(row)
            return row.to_record()

    def append_outbox(self, record: OutboxRecord) -> OutboxRecord:
        with self._session_factory() as session:
            if session.get(ActivityPubOutboxActivity, record.activity_id) is None:
                session.add(
                    ActivityPubOutboxActivity(
                        activity_id=record.activity_id,
                        activity_json=json.dumps(record.activity, sort_keys=True),
                        created_at=record.created_at,
                    )
                )
                session.commit()
            return record

    def list_outbox(self) -> list[OutboxRecord]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(ActivityPubOutboxActivity).order_by(
                    ActivityPubOutboxActivity.created_at.desc(),
                    ActivityPubOutboxActivity.activity_id.asc(),
                )
            ).all()
            return [
                OutboxRecord(
                    activity_id=row.activity_id,
                    activity=json.loads(row.activity_json),
                    created_at=_coerce_datetime(row.created_at),
                )
                for row in rows
            ]

    def append_delivery(self, record: DeliveryRecord) -> DeliveryRecord:
        with self._session_factory() as session:
            session.add(
                ActivityPubDeliveryAttempt(
                    delivery_id=record.delivery_id,
                    activity_id=record.activity_id,
                    inbox_url=record.inbox_url,
                    status_code=record.status_code,
                    response_body=record.response_body,
                    created_at=record.created_at,
                )
            )
            session.commit()
            return record

    def list_deliveries(self, *, activity_id: str | None = None) -> list[DeliveryRecord]:
        with self._session_factory() as session:
            statement = select(ActivityPubDeliveryAttempt)
            if activity_id is not None:
                statement = statement.where(ActivityPubDeliveryAttempt.activity_id == activity_id)
            rows = session.scalars(
                statement.order_by(
                    ActivityPubDeliveryAttempt.created_at.asc(),
                    ActivityPubDeliveryAttempt.delivery_id.asc(),
                )
            ).all()
            return [
                DeliveryRecord(
                    delivery_id=row.delivery_id,
                    activity_id=row.activity_id,
                    inbox_url=row.inbox_url,
                    status_code=row.status_code,
                    response_body=row.response_body,
                    created_at=_coerce_datetime(row.created_at),
                )
                for row in rows
            ]

    def enqueue_delivery_retry(self, record: DeliveryRetryRecord) -> DeliveryRetryRecord:
        with self._session_factory() as session:
            session.add(
                ActivityPubDeliveryRetry(
                    retry_id=record.retry_id,
                    activity_id=record.activity_id,
                    inbox_url=record.inbox_url,
                    activity_json=json.dumps(record.activity, sort_keys=True),
                    state=record.state,
                    attempts=record.attempts,
                    next_attempt_at=record.next_attempt_at,
                    last_status_code=record.last_status_code,
                    last_error=record.last_error,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                )
            )
            session.commit()
            return record

    def get_delivery_retry(self, retry_id: str) -> DeliveryRetryRecord | None:
        with self._session_factory() as session:
            row = session.get(ActivityPubDeliveryRetry, retry_id)
            return _retry_row_to_record(row) if row is not None else None

    def list_delivery_retries(self) -> list[DeliveryRetryRecord]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(ActivityPubDeliveryRetry).order_by(
                    ActivityPubDeliveryRetry.created_at.asc(),
                    ActivityPubDeliveryRetry.retry_id.asc(),
                )
            ).all()
            return [_retry_row_to_record(row) for row in rows]

    def due_delivery_retries(self, *, now: datetime) -> list[DeliveryRetryRecord]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(ActivityPubDeliveryRetry)
                .where(
                    ActivityPubDeliveryRetry.state == "pending",
                    ActivityPubDeliveryRetry.next_attempt_at.is_not(None),
                    ActivityPubDeliveryRetry.next_attempt_at <= now,
                )
                .order_by(
                    ActivityPubDeliveryRetry.next_attempt_at.asc(),
                    ActivityPubDeliveryRetry.retry_id.asc(),
                )
            ).all()
            return [_retry_row_to_record(row) for row in rows]

    def save_delivery_retry(self, record: DeliveryRetryRecord) -> DeliveryRetryRecord:
        with self._session_factory() as session:
            row = session.get(ActivityPubDeliveryRetry, record.retry_id)
            if row is None:
                return self.enqueue_delivery_retry(record)
            row.state = record.state
            row.attempts = record.attempts
            row.next_attempt_at = record.next_attempt_at
            row.last_status_code = record.last_status_code
            row.last_error = record.last_error
            row.updated_at = record.updated_at
            session.commit()
            return record


def new_delivery_id() -> str:
    return "apd_" + secrets.token_urlsafe(16).replace("-", "").replace("_", "")


def _follower_row(record: FollowerRecord) -> ActivityPubFollower:
    return ActivityPubFollower(
        actor=record.actor,
        domain=record.domain,
        status=record.status,
        activity_id=record.activity_id,
        inbox_url=record.inbox_url,
        shared_inbox_url=record.shared_inbox_url,
        public_key_id=record.public_key_id,
        public_key_pem=record.public_key_pem,
        created_at=record.created_at,
    )


def _coerce_datetime(value: object) -> datetime:
    from datetime import UTC

    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"Unsupported datetime value: {value!r}")


def _as_comparable(value: datetime) -> datetime:
    from datetime import UTC

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _retry_row_to_record(row: ActivityPubDeliveryRetry) -> DeliveryRetryRecord:
    return DeliveryRetryRecord(
        retry_id=row.retry_id,
        activity_id=row.activity_id,
        inbox_url=row.inbox_url,
        activity=json.loads(row.activity_json),
        state=row.state,  # type: ignore[arg-type]
        attempts=row.attempts,
        next_attempt_at=(
            _coerce_datetime(row.next_attempt_at) if row.next_attempt_at is not None else None
        ),
        last_status_code=row.last_status_code,
        last_error=row.last_error,
        created_at=_coerce_datetime(row.created_at),
        updated_at=_coerce_datetime(row.updated_at),
    )
