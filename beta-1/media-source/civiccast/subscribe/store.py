# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Persistence seam for v0.8 subscriptions."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from civiccast.subscribe.models import (
    SubscriptionRecord,
    SubscriptionWebhookRetry,
    WebhookRetryRecord,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]


class SubscribeStore(Protocol):
    def create(self, record: SubscriptionRecord) -> SubscriptionRecord: ...
    def get(self, subscription_id: str) -> SubscriptionRecord | None: ...
    def update(self, record: SubscriptionRecord) -> SubscriptionRecord: ...
    def list_confirmed_for_target(
        self, *, target_type: str, target_id: str
    ) -> list[SubscriptionRecord]: ...
    def enqueue_webhook_retry(self, record: WebhookRetryRecord) -> WebhookRetryRecord: ...
    def get_webhook_retry(self, retry_id: str) -> WebhookRetryRecord | None: ...
    def due_webhook_retries(self, *, now: datetime) -> list[WebhookRetryRecord]: ...
    def save_webhook_retry(self, record: WebhookRetryRecord) -> WebhookRetryRecord: ...
    def list_webhook_retries(self) -> list[WebhookRetryRecord]: ...


class InMemorySubscribeStore:
    def __init__(self) -> None:
        self._records: dict[str, SubscriptionRecord] = {}
        self._webhook_retries: dict[str, WebhookRetryRecord] = {}

    def create(self, record: SubscriptionRecord) -> SubscriptionRecord:
        self._records[record.subscription_id] = record
        return record

    def get(self, subscription_id: str) -> SubscriptionRecord | None:
        return self._records.get(subscription_id)

    def update(self, record: SubscriptionRecord) -> SubscriptionRecord:
        self._records[record.subscription_id] = record
        return record

    def list_confirmed_for_target(
        self, *, target_type: str, target_id: str
    ) -> list[SubscriptionRecord]:
        return [
            record
            for record in self._records.values()
            if record.status == "confirmed"
            and record.target_type == target_type
            and record.target_id == target_id
        ]

    def enqueue_webhook_retry(self, record: WebhookRetryRecord) -> WebhookRetryRecord:
        self._webhook_retries[record.retry_id] = record
        return record

    def get_webhook_retry(self, retry_id: str) -> WebhookRetryRecord | None:
        return self._webhook_retries.get(retry_id)

    def due_webhook_retries(self, *, now: datetime) -> list[WebhookRetryRecord]:
        due = [
            record
            for record in self._webhook_retries.values()
            if record.state == "pending"
            and record.next_attempt_at is not None
            and record.next_attempt_at <= now
        ]
        return sorted(due, key=lambda record: (record.next_attempt_at, record.retry_id))

    def save_webhook_retry(self, record: WebhookRetryRecord) -> WebhookRetryRecord:
        self._webhook_retries[record.retry_id] = record
        return record

    def list_webhook_retries(self) -> list[WebhookRetryRecord]:
        return sorted(
            self._webhook_retries.values(),
            key=lambda record: (record.created_at, record.retry_id),
        )


class PostgresSubscribeStore:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def create(self, record: SubscriptionRecord) -> SubscriptionRecord:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            session.execute(
                text(
                    f"INSERT INTO {table}subscriptions "  # nosec B608
                    "(subscription_id, channel, encrypted_subscriber_handle, target_type, "
                    "target_id, status, confirmation_token, unsubscribe_token, "
                    "encrypted_webhook_secret, created_at, confirmed_at, unsubscribed_at) "
                    "VALUES (:subscription_id, :channel, :encrypted_subscriber_handle, "
                    ":target_type, :target_id, :status, :confirmation_token, "
                    ":unsubscribe_token, :encrypted_webhook_secret, :created_at, "
                    ":confirmed_at, :unsubscribed_at)"
                ),
                record.model_dump(mode="json"),
            )
            session.commit()
            return record

    def get(self, subscription_id: str) -> SubscriptionRecord | None:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            row = session.execute(
                text(f"SELECT * FROM {table}subscriptions WHERE subscription_id = :id"),  # nosec B608
                {"id": subscription_id},
            ).first()
            return None if row is None else SubscriptionRecord.model_validate(dict(row._mapping))

    def update(self, record: SubscriptionRecord) -> SubscriptionRecord:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            session.execute(
                text(
                    f"UPDATE {table}subscriptions SET status = :status, "  # nosec B608
                    "confirmed_at = :confirmed_at, unsubscribed_at = :unsubscribed_at, "
                    "confirmation_token = :confirmation_token, unsubscribe_token = :unsubscribe_token, "
                    "encrypted_webhook_secret = :encrypted_webhook_secret "
                    "WHERE subscription_id = :subscription_id"
                ),
                record.model_dump(mode="json"),
            )
            session.commit()
            return record

    def list_confirmed_for_target(
        self, *, target_type: str, target_id: str
    ) -> list[SubscriptionRecord]:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            rows = session.execute(
                text(
                    f"SELECT * FROM {table}subscriptions "  # nosec B608
                    "WHERE status = 'confirmed' AND target_type = :target_type "
                    "AND target_id = :target_id ORDER BY created_at ASC"
                ),
                {"target_type": target_type, "target_id": target_id},
            ).fetchall()
            return [SubscriptionRecord.model_validate(dict(row._mapping)) for row in rows]

    def enqueue_webhook_retry(self, record: WebhookRetryRecord) -> WebhookRetryRecord:
        with self._session_factory() as session:
            session.add(
                SubscriptionWebhookRetry(
                    retry_id=record.retry_id,
                    subscription_id=record.subscription_id,
                    payload_json=json.dumps(record.payload, sort_keys=True),
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

    def get_webhook_retry(self, retry_id: str) -> WebhookRetryRecord | None:
        with self._session_factory() as session:
            row = session.get(SubscriptionWebhookRetry, retry_id)
            return _webhook_retry_row_to_record(row) if row is not None else None

    def due_webhook_retries(self, *, now: datetime) -> list[WebhookRetryRecord]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(SubscriptionWebhookRetry)
                .where(
                    SubscriptionWebhookRetry.state == "pending",
                    SubscriptionWebhookRetry.next_attempt_at.is_not(None),
                    SubscriptionWebhookRetry.next_attempt_at <= now,
                )
                .order_by(
                    SubscriptionWebhookRetry.next_attempt_at.asc(),
                    SubscriptionWebhookRetry.retry_id.asc(),
                )
            ).all()
            return [_webhook_retry_row_to_record(row) for row in rows]

    def save_webhook_retry(self, record: WebhookRetryRecord) -> WebhookRetryRecord:
        with self._session_factory() as session:
            row = session.get(SubscriptionWebhookRetry, record.retry_id)
            if row is None:
                return self.enqueue_webhook_retry(record)
            row.state = record.state
            row.attempts = record.attempts
            row.next_attempt_at = record.next_attempt_at
            row.last_status_code = record.last_status_code
            row.last_error = record.last_error
            row.updated_at = record.updated_at
            session.commit()
            return record

    def list_webhook_retries(self) -> list[WebhookRetryRecord]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(SubscriptionWebhookRetry).order_by(
                    SubscriptionWebhookRetry.created_at.asc(),
                    SubscriptionWebhookRetry.retry_id.asc(),
                )
            ).all()
            return [_webhook_retry_row_to_record(row) for row in rows]

    @staticmethod
    def _table_prefix(session: Session) -> str:
        bind = session.get_bind()
        return "" if bind.dialect.name == "sqlite" else "civiccast."


def _coerce_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"Unsupported datetime value: {value!r}")


def _webhook_retry_row_to_record(row: SubscriptionWebhookRetry) -> WebhookRetryRecord:
    return WebhookRetryRecord(
        retry_id=row.retry_id,
        subscription_id=row.subscription_id,
        payload=json.loads(row.payload_json),
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
